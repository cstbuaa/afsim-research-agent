"""User-curated local library (e.g. Chinese papers downloaded from CNKI/Wanfang).

Put files in the library directory. For each file you may add a sidecar
``<file name>.meta.toml`` such as::

    title = "多无人机协同编队控制方法研究"
    authors = ["张三", "李四"]
    year = 2023
    venue = "兵工学报"
    doi = "10.xxxx/xxxx"          # optional
    language = "zh"
    document_type = "article"     # article | dissertation | report | book
    abstract = "..."              # optional; otherwise the first page is used

Library files become ordinary candidates: they are screened against the
research questions like search results, so an unrelated file in the library is
not fed to PaperQA.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterable
from pathlib import Path

from afsim_research_agent.models import SearchHit, SourceMetadata

LIBRARY_EXTENSIONS = frozenset({".pdf", ".md", ".txt", ".docx", ".html"})
_SNIPPET_CHARS = 1500


def scan_library(roots: Iterable[Path], *, limit: int = 500) -> tuple[list[SearchHit], list[str]]:
    hits: list[SearchHit] = []
    problems: list[str] = []
    for root in roots:
        resolved = root.expanduser().resolve()
        if not resolved.is_dir():
            problems.append(f"library directory not found: {root}")
            continue
        for path in sorted(resolved.rglob("*")):
            if len(hits) >= limit:
                break
            if (
                not path.is_file()
                or path.suffix.lower() not in LIBRARY_EXTENSIONS
                or any(part.startswith(".") for part in path.relative_to(resolved).parts)
            ):
                continue
            try:
                hits.append(_library_hit(path))
            except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
                problems.append(f"{path.name}: {type(exc).__name__}")
    return hits, problems


def _library_hit(path: Path) -> SearchHit:
    sidecar = path.with_name(path.name + ".meta.toml")
    meta = tomllib.loads(sidecar.read_text(encoding="utf-8")) if sidecar.exists() else {}
    title = str(meta.get("title") or path.stem.replace("_", " "))
    abstract = str(meta.get("abstract") or "") or _leading_text(path)
    year = meta.get("year")
    return SearchHit(
        title=title,
        url=path.as_uri(),
        snippet=abstract[:_SNIPPET_CHARS],
        provider="local_library",
        metadata=SourceMetadata(
            doi=(str(meta["doi"]).lower() if meta.get("doi") else None),
            authors=[str(a) for a in meta.get("authors", [])],
            year=int(year) if isinstance(year, int) else None,
            venue=str(meta["venue"]) if meta.get("venue") else None,
            document_type=str(meta["document_type"]) if meta.get("document_type") else None,
            language=str(meta["language"]) if meta.get("language") else None,
            landing_url=str(meta["url"]) if meta.get("url") else None,
            abstract=abstract[:_SNIPPET_CHARS] or None,
            is_open_access=True,
        ),
    )


def _leading_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".md", ".txt", ".html"}:
        return path.read_text(encoding="utf-8", errors="replace")[:_SNIPPET_CHARS]
    if suffix == ".pdf":
        try:
            import pymupdf  # provided by paper-qa[pymupdf]
        except ImportError:
            try:
                import fitz as pymupdf  # older module name
            except ImportError:
                return ""
        try:
            with pymupdf.open(path) as document:
                text = "".join(page.get_text() for page in list(document)[:2])
        except Exception:  # noqa: BLE001 - unreadable PDFs are screened by title only
            return ""
        return " ".join(text.split())[:_SNIPPET_CHARS]
    return ""
