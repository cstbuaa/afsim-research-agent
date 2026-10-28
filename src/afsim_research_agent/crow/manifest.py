"""Run manifests: the configuration and environment behind one research run."""

from __future__ import annotations

import hashlib
import platform
import subprocess
import sys
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path

from afsim_research_agent.config import Settings
from afsim_research_agent.crow.models import RunManifest
from afsim_research_agent.crow.planner import PLANNER_PROMPT_VERSION
from afsim_research_agent.evidence.quality import RULES_VERSION
from afsim_research_agent.literature.screening import SCREENING_PROMPT_VERSION

_TRACKED_PACKAGES = (
    "afsim-research-agent",
    "paper-qa",
    "litellm",
    "tavily-python",
    "trafilatura",
    "requests",
    "pydantic",
    "langchain-openai",
    "deepagents",
)


def dependency_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for package in _TRACKED_PACKAGES:
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            versions[package] = "not installed"
    return versions


def git_commit(cwd: Path | None = None) -> str | None:
    """Return ``<sha>`` or ``<sha>-dirty`` when the project is under Git."""
    root = cwd or Path(__file__).resolve().parents[3]
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root, capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=root, capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return f"{sha}-dirty" if dirty else sha or None


def new_manifest(
    requirement: str,
    *,
    run_id: str,
    app_settings: Settings,
    search_settings: dict[str, object],
) -> RunManifest:
    versions = dependency_versions()
    return RunManifest(
        run_id=run_id,
        started_at=datetime.now(UTC),
        requirement_sha256=hashlib.sha256(requirement.strip().encode()).hexdigest(),
        package_version=versions.get("afsim-research-agent", "unknown"),
        python_version=sys.version.split()[0],
        platform=platform.platform(),
        dependency_versions=versions,
        git_commit=git_commit(),
        planner_prompt_version=PLANNER_PROMPT_VERSION,
        quality_rules_version=RULES_VERSION,
        chat_model=app_settings.chat_model,
        embedding_model=app_settings.embedding_model,
        embedding_dimension=app_settings.paperqa_embedding_dimension,
        paperqa_settings={
            "evidence_k": app_settings.paperqa_evidence_k,
            "answer_max_sources": app_settings.paperqa_max_sources,
            "max_concurrent_requests": app_settings.paperqa_concurrency,
            "disable_thinking": app_settings.paperqa_disable_thinking,
            "temperature": 0,
        },
        search_settings={
            "tavily_search_depth": app_settings.tavily_search_depth,
            "scholarly_enabled": app_settings.crow_enable_scholarly,
            "doctrine_enabled": app_settings.crow_enable_doctrine,
            "doctrine_domains": list(app_settings.crow_doctrine_domains),
            "proxy_configured": bool(app_settings.crow_http_proxy),
            "allowed_proxy_cidrs": list(app_settings.crow_allowed_proxy_cidrs),
            "contact_configured": bool(app_settings.crow_contact_email),
            "openalex_api_key_configured": app_settings.openalex_api_key_configured,
            "semantic_scholar_api_key_configured": (
                app_settings.semantic_scholar_api_key_configured
            ),
            "core_api_key_configured": app_settings.core_api_key_configured,
            "scholarly_providers": list(app_settings.crow_scholarly_providers),
            "technical_reports_enabled": app_settings.crow_enable_technical_reports,
            "screening_enabled": app_settings.crow_enable_screening,
            "screening_prompt_version": SCREENING_PROMPT_VERSION,
            "screen_min_score": app_settings.crow_screen_min_score,
            "snowball_seeds": app_settings.crow_snowball_seeds,
            "snowball_per_seed": app_settings.crow_snowball_per_seed,
            "gap_round_sources": app_settings.crow_gap_round_sources,
            "max_library_sources": app_settings.crow_max_library_sources,
            **search_settings,
        },
    )
