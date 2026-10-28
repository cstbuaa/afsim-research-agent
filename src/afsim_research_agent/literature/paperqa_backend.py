"""Alibaba Cloud Model Studio-backed PaperQA2 evidence-query boundary."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from paperqa import Docs
from paperqa import Settings as PaperQASettings

from afsim_research_agent.config import settings as default_app_settings
from afsim_research_agent.literature.models import (
    CorpusIngestFailure,
    PaperQADocument,
    PaperQAEvidence,
    PaperQAReport,
)


class PaperQAConfig(Protocol):
    """Application settings consumed by the PaperQA2 adapter."""

    dashscope_api_key: str | None
    dashscope_base_url: str
    chat_model: str
    embedding_model: str
    paperqa_embedding_dimension: int
    paperqa_disable_thinking: bool
    paperqa_evidence_k: int
    paperqa_max_sources: int
    paperqa_concurrency: int
    data_dir: Path


class DocsLike(Protocol):
    """Subset of PaperQA ``Docs`` used by this adapter."""

    async def aadd(self, path: object, **kwargs: object) -> object: ...

    async def aquery(self, question: str, *, settings: object) -> object: ...


def build_paperqa_settings(app_settings: PaperQAConfig) -> PaperQASettings:
    """Translate application configuration into PaperQA2 settings."""
    if not app_settings.dashscope_api_key:
        raise ValueError("DASHSCOPE_API_KEY is required")
    chat_model = _openai_model_name(app_settings.chat_model)
    embedding_model = _openai_model_name(app_settings.embedding_model)
    llm_config = _router_config(
        chat_model,
        app_settings.dashscope_base_url,
        app_settings.dashscope_api_key,
        disable_thinking=app_settings.paperqa_disable_thinking,
    )
    embedding_config = _router_config(
        embedding_model,
        app_settings.dashscope_base_url,
        app_settings.dashscope_api_key,
        model_info={
            "max_input_tokens": 8192,
            "output_vector_size": app_settings.paperqa_embedding_dimension,
            "mode": "embedding",
        },
    )
    embedding_config["batch_size"] = 10
    embedding_config["ndim"] = app_settings.paperqa_embedding_dimension

    return PaperQASettings(
        llm=chat_model,
        llm_config=llm_config,
        summary_llm=chat_model,
        summary_llm_config=llm_config,
        embedding=embedding_model,
        embedding_config=embedding_config,
        temperature=0,
        batch_size=1,
        parsing={
            "multimodal": False,
            "use_doc_details": False,
        },
        answer={
            "evidence_k": app_settings.paperqa_evidence_k,
            "answer_max_sources": app_settings.paperqa_max_sources,
            "max_concurrent_requests": app_settings.paperqa_concurrency,
        },
        agent={
            "index": {
                "index_directory": str(app_settings.data_dir / "index" / "paperqa"),
                "paper_directory": str(app_settings.data_dir / "raw" / "paperqa"),
            }
        },
    )


TEXT_CHECK_MESSAGE = "does not look like a text document"


def has_text_layer(path: Path, *, min_letters: int = 3000, min_ratio: float = 0.5) -> bool:
    """True when the document has real text beyond its first pages.

    Samples up to 20 pages from the body (skipping cover and contents) and
    requires enough letters, mostly letters rather than symbols. Returns False
    when the text cannot be inspected, so nothing is accepted blindly.
    """
    text = ""
    if path.suffix.lower() == ".pdf":
        try:
            import fitz  # pymupdf, installed with paper-qa[pymupdf]
        except ImportError:
            return False
        try:
            with fitz.open(path) as pdf:
                count = pdf.page_count
                start = min(count - 1, max(0, count // 10)) if count else 0
                for index in range(start, min(count, start + 20)):
                    text += pdf.load_page(index).get_text()
        except Exception:  # noqa: BLE001 - unreadable PDF means no usable text layer
            return False
    else:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")[:200_000]
        except OSError:
            return False
    visible = [c for c in text if not c.isspace()]
    letters = sum(1 for c in visible if c.isalpha())
    return letters >= min_letters and letters / max(1, len(visible)) >= min_ratio


@dataclass
class PaperQACorpus:
    """Documents parsed and embedded once, then queried for every question."""

    docs: DocsLike
    documents: list[PaperQADocument]
    ingest_failures: list[CorpusIngestFailure] = field(default_factory=list)
    source_ids_by_dockey: dict[str, str] = field(default_factory=dict)
    source_ids_by_citation: dict[str, str] = field(default_factory=dict)
    # documents PaperQA's first-chunk text check rejected but our body check accepted
    rechecked: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return len(self.documents) == len(self.ingest_failures)


class PaperQABackend:
    """Build one PaperQA2 corpus per run and answer questions with traceable evidence."""

    def __init__(
        self,
        *,
        app_settings: PaperQAConfig = default_app_settings,
        docs_factory: Callable[[], DocsLike] = Docs,
    ) -> None:
        self._settings = build_paperqa_settings(app_settings)
        self._docs_factory = docs_factory

    async def build_corpus(self, documents: Sequence[PaperQADocument]) -> PaperQACorpus:
        """Parse and embed each document exactly once; one bad file does not abort."""
        if not documents:
            raise ValueError("at least one document is required")
        corpus = PaperQACorpus(docs=self._docs_factory(), documents=[])
        await self.add_to_corpus(corpus, documents)
        return corpus

    async def add_to_corpus(
        self, corpus: PaperQACorpus, documents: Sequence[PaperQADocument]
    ) -> list[CorpusIngestFailure]:
        """Embed additional documents (e.g. from a follow-up round) into a corpus."""
        new_failures: list[CorpusIngestFailure] = []
        for document in documents:
            corpus.documents.append(document)
            dockey = document.source_id or str(document.path)
            kwargs = {
                "citation": document.citation,
                "title": document.title,
                "doi": document.doi,
                "authors": document.authors or None,
                "dockey": dockey,
            }
            try:
                try:
                    await corpus.docs.aadd(document.path, settings=self._settings, **kwargs)
                except ValueError as exc:
                    # PaperQA judges "is this text?" from the first chunks only; manuals
                    # whose first pages are a cover and dot-leader contents fail it even
                    # with a good text layer. Re-add only after our own check of body pages.
                    if TEXT_CHECK_MESSAGE not in str(exc) or not has_text_layer(document.path):
                        raise
                    await self._add_without_text_check(corpus, document, kwargs)
                    corpus.rechecked.append(document.source_id or str(document.path))
            except Exception as exc:  # noqa: BLE001 - parsing failures are per document
                failure = CorpusIngestFailure(
                    source_id=document.source_id,
                    path=str(document.path),
                    detail=f"{type(exc).__name__}: {str(exc)[:200]}",
                )
                corpus.ingest_failures.append(failure)
                new_failures.append(failure)
                continue
            if document.source_id:
                corpus.source_ids_by_dockey[dockey] = document.source_id
                corpus.source_ids_by_citation[document.citation] = document.source_id
        return new_failures

    async def _add_without_text_check(
        self, corpus: PaperQACorpus, document: PaperQADocument, kwargs: dict[str, object]
    ) -> None:
        settings = self._settings
        parsing = getattr(settings, "parsing", None)
        if parsing is not None and hasattr(parsing, "disable_doc_valid_check"):
            settings = settings.model_copy(deep=True)
            settings.parsing.disable_doc_valid_check = True
            await corpus.docs.aadd(document.path, settings=settings, **kwargs)
            return
        # older PaperQA releases take the flag as a keyword argument
        await corpus.docs.aadd(document.path, settings=settings, disable_check=True, **kwargs)

    async def query_corpus(self, question: str, corpus: PaperQACorpus) -> PaperQAReport:
        """Answer one question against an already-embedded corpus."""
        normalized_question = question.strip()
        if not normalized_question:
            raise ValueError("question must not be blank")
        started = time.perf_counter()
        session = await corpus.docs.aquery(normalized_question, settings=self._settings)
        evidence = []
        for context in session.contexts:
            doc = context.text.doc
            dockey = str(getattr(doc, "dockey", "") or "")
            citation = str(getattr(doc, "citation", ""))
            evidence.append(
                PaperQAEvidence(
                    citation=citation,
                    excerpt=context.text.text,
                    summary=context.context,
                    location=context.text.name,
                    score=context.score,
                    source_id=corpus.source_ids_by_dockey.get(dockey)
                    or corpus.source_ids_by_citation.get(citation),
                )
            )
        answer = str(session.answer)
        return PaperQAReport(
            question=normalized_question,
            answer=answer,
            evidence=evidence,
            insufficient_evidence=is_insufficient_answer(answer),
            elapsed_seconds=round(time.perf_counter() - started, 2),
        )

    async def query(
        self,
        question: str,
        documents: Sequence[PaperQADocument],
    ) -> PaperQAReport:
        """Convenience wrapper for one-off questions; prefer a shared corpus."""
        if not question.strip():
            raise ValueError("question must not be blank")
        corpus = await self.build_corpus(documents)
        return await self.query_corpus(question, corpus)


_INSUFFICIENT_MARKERS = (
    "cannot answer",
    "can't answer",
    "insufficient information",
    "insufficient evidence",
    "not enough information",
    "无法回答",
    "信息不足",
    "证据不足",
)


def is_insufficient_answer(answer: str) -> bool:
    """Heuristic flag for answers where PaperQA2 reports missing evidence."""
    lowered = answer.lower()
    return any(marker in lowered for marker in _INSUFFICIENT_MARKERS)


def _openai_model_name(model: str) -> str:
    return model if model.startswith("openai/") else f"openai/{model}"


def _router_config(
    model: str,
    base_url: str,
    api_key: str,
    *,
    disable_thinking: bool = False,
    model_info: dict[str, object] | None = None,
) -> dict:
    params: dict[str, object] = {
        "model": model,
        "api_base": base_url,
        "api_key": api_key,
        "timeout": 600,
    }
    if disable_thinking:
        params["extra_body"] = {"enable_thinking": False}
    deployment: dict[str, object] = {
        "model_name": model,
        "litellm_params": params,
    }
    if model_info is not None:
        deployment["model_info"] = model_info
    return {"model_list": [deployment]}
