"""Literature collection and evidence-query boundaries."""

from afsim_research_agent.literature.collection import (
    SearchChannel,
    build_default_channels,
    collect_research_sources,
)
from afsim_research_agent.literature.models import (
    CorpusIngestFailure,
    PaperQADocument,
    PaperQAEvidence,
    PaperQAReport,
    SourceCandidate,
    SourceCollection,
    SourceCollectionFailure,
    SourceCollectionRequest,
)
from afsim_research_agent.literature.paperqa_backend import (
    PaperQABackend,
    PaperQACorpus,
    build_paperqa_settings,
)

__all__ = [
    "CorpusIngestFailure",
    "PaperQABackend",
    "PaperQACorpus",
    "PaperQADocument",
    "PaperQAEvidence",
    "PaperQAReport",
    "SearchChannel",
    "SourceCandidate",
    "SourceCollection",
    "SourceCollectionFailure",
    "SourceCollectionRequest",
    "build_default_channels",
    "build_paperqa_settings",
    "collect_research_sources",
]
