"""Source quality, deduplication, and (later) claim-level evidence merging."""

from afsim_research_agent.evidence.deduplicate import normalize_doi, normalize_url
from afsim_research_agent.evidence.quality import QualityInput, score_source

__all__ = ["QualityInput", "normalize_doi", "normalize_url", "score_source"]
