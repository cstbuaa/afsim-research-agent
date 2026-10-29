"""Tests for the local PaperQA2 evidence-query boundary."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import litellm
import pytest

from afsim_research_agent.config import Settings
from afsim_research_agent.literature import (
    PaperQABackend,
    PaperQADocument,
    build_paperqa_settings,
)
from afsim_research_agent.literature.paperqa_backend import is_insufficient_answer


def app_settings() -> SimpleNamespace:
    return SimpleNamespace(
        dashscope_api_key="test-dashscope-key",
        dashscope_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        chat_model="qwen3.8-flash",
        embedding_model="text-embedding-v4",
        paperqa_embedding_dimension=1024,
        paperqa_disable_thinking=True,
        paperqa_evidence_k=4,
        paperqa_max_sources=2,
        paperqa_concurrency=1,
        data_dir=Path("/tmp/afsim-research-data"),
    )


def test_build_settings_configures_dashscope_openai_compatibility() -> None:
    settings = build_paperqa_settings(app_settings())

    assert settings.llm == "openai/qwen3.8-flash"
    assert settings.embedding == "openai/text-embedding-v4"
    assert settings.embedding_config["ndim"] == 1024
    llm_params = settings.llm_config["model_list"][0]["litellm_params"]
    assert llm_params["api_base"] == ("https://dashscope.aliyuncs.com/compatible-mode/v1")
    assert llm_params["api_key"] == "test-dashscope-key"
    assert llm_params["extra_body"] == {"enable_thinking": False}
    embedding_params = settings.embedding_config["model_list"][0]["litellm_params"]
    assert embedding_params["api_key"] == "test-dashscope-key"
    assert settings.parsing.multimodal is False
    assert settings.parsing.use_doc_details is False
    assert settings.answer.evidence_k == 4
    assert settings.answer.answer_max_sources == 2
    assert settings.answer.max_concurrent_requests == 1
    data = Path("/tmp/afsim-research-data")  # separators follow the OS (Windows: backslashes)
    assert settings.agent.index.index_directory == str(data / "index" / "paperqa")
    assert settings.agent.index.paper_directory == str(data / "raw" / "paperqa")


def test_embedding_metadata_supports_lmi_truncation() -> None:
    configured = app_settings()
    configured.embedding_model = "test-embedding-v4"
    model_name = "openai/test-embedding-v4"
    litellm.model_cost.pop(model_name, None)

    try:
        settings = build_paperqa_settings(configured)
        embedding_model = settings.get_embedding_model()
        _ = embedding_model.router

        assert embedding_model.config["batch_size"] == 10
        assert litellm.model_cost[model_name]["max_input_tokens"] == 8192
        assert embedding_model._truncate_if_large(["short text"]) == ["short text"]
    finally:
        litellm.model_cost.pop(model_name, None)


def test_application_settings_expose_paperqa_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("DASHSCOPE_CHAT_MODEL", "test-chat-model")
    monkeypatch.setenv("DASHSCOPE_EMBEDDING_MODEL", "test-embedding-model")
    monkeypatch.setenv("PAPERQA_EMBEDDING_DIMENSION", "768")
    monkeypatch.setenv("PAPERQA_DISABLE_THINKING", "false")
    monkeypatch.setenv("PAPERQA_EVIDENCE_K", "6")
    monkeypatch.setenv("PAPERQA_MAX_SOURCES", "4")
    monkeypatch.setenv("PAPERQA_CONCURRENCY", "2")

    settings = Settings.from_environment()

    assert settings.dashscope_api_key == "test-key"
    assert settings.dashscope_base_url == "https://example.test/v1"
    assert settings.chat_model == "test-chat-model"
    assert settings.embedding_model == "test-embedding-model"
    assert settings.paperqa_embedding_dimension == 768
    assert settings.paperqa_disable_thinking is False
    assert settings.paperqa_evidence_k == 6
    assert settings.paperqa_max_sources == 4
    assert settings.paperqa_concurrency == 2


class FakeDocs:
    def __init__(self, failing_paths: set[Path] | None = None) -> None:
        self.added: list[dict[str, object]] = []
        self.queries: list[tuple[str, object]] = []
        self.failing_paths = failing_paths or set()

    async def aadd(self, path: Path, **kwargs: object) -> None:
        if path in self.failing_paths:
            raise ValueError("could not parse PDF")
        self.added.append({"path": path, **kwargs})

    async def aquery(self, question: str, *, settings: object) -> SimpleNamespace:
        self.queries.append((question, settings))
        context = SimpleNamespace(
            context="The threat threshold triggers the transition.",
            score=9,
            text=SimpleNamespace(
                text="Switch when threat range is below 40 km.",
                name="lines 10-12",
                doc=SimpleNamespace(citation="Test Paper, 2026.", dockey="pdf:abc"),
            ),
        )
        return SimpleNamespace(answer="Switch below 40 km (Test2026).", contexts=[context])


def document(tmp_path: Path, name: str = "paper.md", source_id: str = "pdf:abc"):
    path = tmp_path / name
    path.write_text("test")
    return PaperQADocument(
        path=path,
        citation="Test Paper, 2026.",
        title="Test Paper",
        doi="10.1/x",
        authors=["A. Li"],
        source_id=source_id,
    )


def test_corpus_is_built_once_and_reused_for_every_question(tmp_path: Path) -> None:
    docs = FakeDocs()
    backend = PaperQABackend(app_settings=app_settings(), docs_factory=lambda: docs)

    async def run() -> list[object]:
        corpus = await backend.build_corpus([document(tmp_path)])
        return [
            await backend.query_corpus("When should the formation switch?", corpus),
            await backend.query_corpus("Who leads?", corpus),
        ]

    first, second = asyncio.run(run())

    assert len(docs.added) == 1
    assert docs.added[0]["dockey"] == "pdf:abc"
    assert docs.added[0]["doi"] == "10.1/x"
    assert docs.added[0]["authors"] == ["A. Li"]
    assert [q for q, _ in docs.queries] == ["When should the formation switch?", "Who leads?"]
    assert first.evidence[0].model_dump() == {
        "citation": "Test Paper, 2026.",
        "excerpt": "Switch when threat range is below 40 km.",
        "summary": "The threat threshold triggers the transition.",
        "location": "lines 10-12",
        "score": 9,
        "source_id": "pdf:abc",
        "source_quality_tier": None,
        "source_quality_score": None,
    }
    assert second.question == "Who leads?"
    assert first.elapsed_seconds is not None


def test_one_unparseable_document_does_not_abort_the_corpus(tmp_path: Path) -> None:
    good = document(tmp_path, "good.md", "pdf:good")
    bad = document(tmp_path, "bad.pdf", "pdf:bad")
    docs = FakeDocs(failing_paths={bad.path})
    backend = PaperQABackend(app_settings=app_settings(), docs_factory=lambda: docs)

    corpus = asyncio.run(backend.build_corpus([good, bad]))

    assert [item["path"] for item in docs.added] == [good.path]
    assert corpus.ingest_failures[0].source_id == "pdf:bad"
    assert "could not parse PDF" in corpus.ingest_failures[0].detail
    assert not corpus.is_empty


def test_query_convenience_wrapper_still_works(tmp_path: Path) -> None:
    docs = FakeDocs()
    backend = PaperQABackend(app_settings=app_settings(), docs_factory=lambda: docs)

    report = asyncio.run(backend.query("When should the formation switch?", [document(tmp_path)]))

    assert report.answer == "Switch below 40 km (Test2026)."
    assert report.insufficient_evidence is False


@pytest.mark.parametrize(
    ("answer", "flag"),
    [
        ("I cannot answer.", True),
        ("There is insufficient information to determine spacing.", True),
        ("资料不足，无法回答该问题。", True),
        ("Platoons use a wedge when contact is likely (FM2025).", False),
    ],
)
def test_insufficient_answer_heuristic(answer: str, flag: bool) -> None:
    assert is_insufficient_answer(answer) is flag


@pytest.mark.parametrize("question", ["", "   "])
def test_query_rejects_blank_question(question: str, tmp_path: Path) -> None:
    backend = PaperQABackend(app_settings=app_settings(), docs_factory=FakeDocs)

    with pytest.raises(ValueError, match="question"):
        asyncio.run(backend.query(question, [document(tmp_path)]))


def test_query_rejects_empty_document_set() -> None:
    backend = PaperQABackend(app_settings=app_settings(), docs_factory=FakeDocs)

    with pytest.raises(ValueError, match="document"):
        asyncio.run(backend.query("question", []))


def test_follow_up_documents_extend_the_same_corpus(tmp_path: Path) -> None:
    docs = FakeDocs()
    factory_calls: list[int] = []
    backend = PaperQABackend(
        app_settings=app_settings(),
        docs_factory=lambda: factory_calls.append(1) or docs,
    )

    async def run() -> object:
        corpus = await backend.build_corpus([document(tmp_path, "a.md", "pdf:a")])
        failures = await backend.add_to_corpus(corpus, [document(tmp_path, "b.md", "pdf:b")])
        return corpus, failures

    corpus, failures = asyncio.run(run())

    assert factory_calls == [1]
    assert [item["dockey"] for item in docs.added] == ["pdf:a", "pdf:b"]
    assert [d.source_id for d in corpus.documents] == ["pdf:a", "pdf:b"]
    assert failures == []


class TextCheckDocs(FakeDocs):
    """Rejects like PaperQA's first-chunk text check unless the check is disabled."""

    async def aadd(self, path: Path, **kwargs: object) -> None:
        settings = kwargs.get("settings")
        parsing = getattr(settings, "parsing", None)
        disabled = kwargs.get("disable_check") or getattr(parsing, "disable_doc_valid_check", False)
        if not disabled:
            raise ValueError(f"This does not look like a text document: {path}. Pass disable_check")
        self.added.append({"path": path, **kwargs})


def test_manual_rejected_by_first_chunk_check_is_rechecked(tmp_path: Path, monkeypatch) -> None:
    from afsim_research_agent.literature import paperqa_backend

    docs = TextCheckDocs()
    backend = PaperQABackend(app_settings=app_settings(), docs_factory=lambda: docs)
    monkeypatch.setattr(paperqa_backend, "has_text_layer", lambda path: True)
    corpus = asyncio.run(backend.build_corpus([document(tmp_path, "manual.pdf", "pdf:manual")]))
    assert not corpus.ingest_failures and corpus.rechecked == ["pdf:manual"]
    assert docs.added[0]["dockey"] == "pdf:manual"
    assert corpus.source_ids_by_dockey["pdf:manual"] == "pdf:manual"


def test_document_without_text_layer_stays_rejected(tmp_path: Path, monkeypatch) -> None:
    from afsim_research_agent.literature import paperqa_backend

    docs = TextCheckDocs()
    backend = PaperQABackend(app_settings=app_settings(), docs_factory=lambda: docs)
    monkeypatch.setattr(paperqa_backend, "has_text_layer", lambda path: False)
    corpus = asyncio.run(backend.build_corpus([document(tmp_path, "scan.pdf", "pdf:scan")]))
    assert corpus.ingest_failures[0].source_id == "pdf:scan" and not docs.added
    assert not corpus.rechecked


def test_has_text_layer_on_text_files(tmp_path: Path) -> None:
    from afsim_research_agent.literature.paperqa_backend import has_text_layer

    prose = tmp_path / "prose.txt"
    prose.write_text("The platoon moves in a wedge formation. " * 200)
    leaders = tmp_path / "toc.txt"
    leaders.write_text("Chapter 1 ............................. 3\n" * 400)
    assert has_text_layer(prose)
    assert not has_text_layer(leaders)
