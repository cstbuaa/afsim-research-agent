"""Runtime configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DOCTRINE_DOMAINS = (
    "armypubs.army.mil",
    "army.mil",
    "dtic.mil",
    "marines.mil",
    "af.mil",
    "navy.mil",
    "defense.gov",
    "nato.int",
    "gov.uk",
    "rand.org",
    "cna.org",
    "nps.edu",
    "westpoint.edu",
    "globalsecurity.org",
    "fas.org",
    "bits.de",
)


DEFAULT_SCHOLARLY_PROVIDERS = ("openalex", "semantic_scholar", "arxiv", "core")
DEFAULT_TECHNICAL_REPORT_DOMAINS = (
    "apps.dtic.mil",
    "dtic.mil",
    "calhoun.nps.edu",
    "nps.edu",
    "scholar.afit.edu",
    "afit.edu",
    "rand.org",
    "cna.org",
    "mitre.org",
    "ntrs.nasa.gov",
    "sto.nato.int",
    "foi.se",
)


def _csv(value: str | None) -> tuple[str, ...]:
    return tuple(item.strip() for item in (value or "").split(",") if item.strip())


def _flag(value: str | None, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


@dataclass(frozen=True)
class Settings:
    """Settings shared by the agent and its tools."""

    dashscope_api_key: str | None
    dashscope_base_url: str
    chat_model: str
    embedding_model: str
    tavily_api_key: str | None
    data_dir: Path
    paperqa_embedding_dimension: int
    paperqa_disable_thinking: bool
    paperqa_evidence_k: int
    paperqa_max_sources: int
    paperqa_concurrency: int
    tavily_search_depth: str = "advanced"
    crow_contact_email: str | None = None
    crow_http_proxy: str | None = None
    crow_allowed_proxy_cidrs: tuple[str, ...] = ()
    crow_max_sources: int = 12
    crow_enable_scholarly: bool = True
    crow_enable_doctrine: bool = True
    crow_doctrine_domains: tuple[str, ...] = field(default=DEFAULT_DOCTRINE_DOMAINS)
    openalex_mailto: str | None = None
    openalex_api_key_configured: bool = False
    semantic_scholar_api_key_configured: bool = False
    core_api_key_configured: bool = False
    crow_scholarly_providers: tuple[str, ...] = DEFAULT_SCHOLARLY_PROVIDERS
    crow_enable_technical_reports: bool = True
    crow_technical_report_domains: tuple[str, ...] = DEFAULT_TECHNICAL_REPORT_DOMAINS
    crow_enable_screening: bool = True
    crow_screen_min_score: int = 1
    crow_max_screened_candidates: int = 80
    crow_snowball_seeds: int = 3
    crow_snowball_per_seed: int = 10
    crow_gap_rounds: int = 1
    crow_gap_round_sources: int = 6
    crow_library_dirs: tuple[str, ...] = ()
    crow_max_library_sources: int = 6
    crow_fetch_workers: int = 4
    crow_host_failure_limit: int = 2
    # Tactic proposal (docs/design/crow-tactic-proposal-v1.md §10).
    crow_proposer_model: str | None = None
    crow_capability_model: str | None = None
    crow_redteam_model: str = "deepseek-v4-pro"
    crow_max_candidates: int = 5
    crow_max_innovative: int = 2
    crow_ideas_per_gap: int = 3
    crow_enable_transfer: bool = False
    crow_capability_catalog: str | None = None

    @classmethod
    def from_environment(cls) -> Settings:
        """Build settings from environment variables."""
        return cls(
            dashscope_api_key=os.getenv("DASHSCOPE_API_KEY") or None,
            dashscope_base_url=os.getenv(
                "DASHSCOPE_BASE_URL",
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
            ),
            chat_model=os.getenv("DASHSCOPE_CHAT_MODEL", "qwen3.8-flash"),
            embedding_model=os.getenv(
                "DASHSCOPE_EMBEDDING_MODEL", "text-embedding-v4"
            ),
            tavily_api_key=os.getenv("TAVILY_API_KEY") or None,
            data_dir=Path(os.getenv("AFSIM_RESEARCH_DATA_DIR", "./data")),
            paperqa_embedding_dimension=int(
                os.getenv("PAPERQA_EMBEDDING_DIMENSION", "1024")
            ),
            paperqa_disable_thinking=os.getenv(
                "PAPERQA_DISABLE_THINKING", "true"
            ).lower()
            not in {"0", "false", "no", "off"},
            paperqa_evidence_k=int(os.getenv("PAPERQA_EVIDENCE_K", "5")),
            paperqa_max_sources=int(os.getenv("PAPERQA_MAX_SOURCES", "3")),
            paperqa_concurrency=int(os.getenv("PAPERQA_CONCURRENCY", "1")),
            tavily_search_depth=(os.getenv("TAVILY_SEARCH_DEPTH") or "advanced").strip().lower(),
            crow_contact_email=os.getenv("CROW_CONTACT_EMAIL") or None,
            crow_http_proxy=os.getenv("CROW_HTTP_PROXY") or None,
            crow_allowed_proxy_cidrs=_csv(os.getenv("CROW_ALLOWED_PROXY_CIDRS")),
            crow_max_sources=int(os.getenv("CROW_MAX_SOURCES", "12")),
            crow_enable_scholarly=_flag(os.getenv("CROW_ENABLE_SCHOLARLY"), True),
            crow_enable_doctrine=_flag(os.getenv("CROW_ENABLE_DOCTRINE"), True),
            crow_doctrine_domains=_csv(os.getenv("CROW_DOCTRINE_DOMAINS"))
            or DEFAULT_DOCTRINE_DOMAINS,
            openalex_mailto=os.getenv("OPENALEX_MAILTO") or os.getenv("CROW_CONTACT_EMAIL") or None,
            openalex_api_key_configured=bool((os.getenv("OPENALEX_API_KEY") or "").strip()),
            semantic_scholar_api_key_configured=bool(
                (os.getenv("SEMANTIC_SCHOLAR_API_KEY") or "").strip()
            ),
            core_api_key_configured=bool((os.getenv("CORE_API_KEY") or "").strip()),
            crow_scholarly_providers=_csv(os.getenv("CROW_SCHOLARLY_PROVIDERS"))
            or DEFAULT_SCHOLARLY_PROVIDERS,
            crow_enable_technical_reports=_flag(os.getenv("CROW_ENABLE_TECHNICAL_REPORTS"), True),
            crow_proposer_model=os.getenv("CROW_PROPOSER_MODEL") or None,
            crow_capability_model=os.getenv("CROW_CAPABILITY_MODEL") or None,
            crow_redteam_model=os.getenv("CROW_REDTEAM_MODEL") or "deepseek-v4-pro",
            crow_max_candidates=int(os.getenv("CROW_MAX_CANDIDATES", "5")),
            crow_max_innovative=int(os.getenv("CROW_MAX_INNOVATIVE", "2")),
            crow_ideas_per_gap=int(os.getenv("CROW_IDEAS_PER_GAP", "3")),
            crow_enable_transfer=_flag(os.getenv("CROW_ENABLE_TRANSFER"), False),
            crow_capability_catalog=os.getenv("CROW_CAPABILITY_CATALOG") or None,
            crow_technical_report_domains=_csv(os.getenv("CROW_TECHNICAL_REPORT_DOMAINS"))
            or DEFAULT_TECHNICAL_REPORT_DOMAINS,
            crow_enable_screening=_flag(os.getenv("CROW_ENABLE_SCREENING"), True),
            crow_screen_min_score=int(os.getenv("CROW_SCREEN_MIN_SCORE", "1")),
            crow_max_screened_candidates=int(os.getenv("CROW_MAX_SCREENED_CANDIDATES", "80")),
            crow_snowball_seeds=int(os.getenv("CROW_SNOWBALL_SEEDS", "3")),
            crow_snowball_per_seed=int(os.getenv("CROW_SNOWBALL_PER_SEED", "10")),
            crow_gap_rounds=int(os.getenv("CROW_GAP_ROUNDS", "1")),
            crow_gap_round_sources=int(os.getenv("CROW_GAP_ROUND_SOURCES", "6")),
            crow_library_dirs=_csv(os.getenv("CROW_LIBRARY_DIRS")),
            crow_max_library_sources=int(os.getenv("CROW_MAX_LIBRARY_SOURCES", "6")),
            crow_fetch_workers=max(1, int(os.getenv("CROW_FETCH_WORKERS", "4"))),
            crow_host_failure_limit=max(1, int(os.getenv("CROW_HOST_FAILURE_LIMIT", "2"))),
        )


settings = Settings.from_environment()
