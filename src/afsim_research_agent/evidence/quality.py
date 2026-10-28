"""Deterministic, explainable source-quality scoring.

The score answers "how much can this *source* be trusted as a basis for
tactical behaviour rules?" It is computed only from the URL, title, file type
and bibliographic metadata, so the same inputs always give the same score and
every point can be traced to a named reason. It is stored separately from
PaperQA's per-question relevance score.

Tiers:
    A  official doctrine / peer-reviewed or well-identified scholarly work
    B  institutional reports, official documents hosted on mirrors
    C  press, trade articles, encyclopedias, unidentified web pages
    D  social media, video, forums, or otherwise unusable for evidence
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from afsim_research_agent.domain.doctrine import has_designation
from afsim_research_agent.models import SourceKind, SourceMetadata, SourceQuality

RULES_VERSION = "quality-rules-2026.10.2"

_OFFICIAL_SUFFIXES = (
    ".mil",
    ".gov",
    ".gov.uk",
    ".mod.uk",
    ".gov.au",
    ".gc.ca",
    ".europa.eu",
    ".gouv.fr",
    ".bund.de",
)
_OFFICIAL_HOSTS = frozenset({"nato.int", "www.nato.int", "jwc.nato.int", "act.nato.int"})

_SCHOLARLY_SUFFIXES = (".edu", ".ac.uk", ".ac.cn", ".edu.cn", ".ac.jp", ".edu.au")
_SCHOLARLY_HOSTS = frozenset(
    {
        "arxiv.org",
        "doi.org",
        "ieeexplore.ieee.org",
        "link.springer.com",
        "www.sciencedirect.com",
        "www.mdpi.com",
        "mdpi-res.com",
        "www.tandfonline.com",
        "onlinelibrary.wiley.com",
        "journals.sagepub.com",
        "dl.acm.org",
        "arc.aiaa.org",
        "www.nature.com",
        "www.frontiersin.org",
        "journals.plos.org",
        "www.hindawi.com",
        "openreview.net",
        "proceedings.mlr.press",
        "papers.nips.cc",
        "ojs.aaai.org",
        "www.ifaamas.org",
        "europepmc.org",
        "www.semanticscholar.org",
        "core.ac.uk",
        "hal.science",
        "zenodo.org",
        "www.cnki.net",
        "kns.cnki.net",
        "www.wanfangdata.com.cn",
    }
)
_INSTITUTIONAL_HOSTS = frozenset(
    {
        "www.rand.org",
        "www.cna.org",
        "www.csis.org",
        "rusi.org",
        "www.rusi.org",
        "www.iiss.org",
        "www.sipri.org",
        "www.foi.se",
        "www.ffi.no",
        "www.usni.org",
        "mwi.westpoint.edu",
        "www.nps.edu",
        "calhoun.nps.edu",
        "www.armyupress.army.mil",
        "warontherocks.com",
        "www.japcc.org",
        "www.tno.nl",
    }
)
# Archives that commonly re-host official doctrine; authority comes from the
# document itself, so they are trusted only when a doctrine designation is seen.
_MIRROR_HOSTS = frozenset(
    {
        "www.bits.de",
        "bits.de",
        "www.globalsecurity.org",
        "irp.fas.org",
        "fas.org",
        "man.fas.org",
        "upload.wikimedia.org",
        "commons.wikimedia.org",
        "archive.org",
        "www.archive.org",
        "everyspec.com",
        "www.everyspec.com",
        "www.marines.mil",
        "www.trngcmd.marines.mil",
        "www.researchgate.net",
        "www.academia.edu",
        "www.scribd.com",
    }
)
_PRESS_HOSTS = frozenset(
    {
        "www.defensenews.com",
        "breakingdefense.com",
        "www.janes.com",
        "www.twz.com",
        "www.thedrive.com",
        "battle-updates.com",
        "www.armyrecognition.com",
        "militaryleak.com",
        "nationalinterest.org",
        "www.popularmechanics.com",
        "www.armytimes.com",
        "www.defenseone.com",
        "www.c4isrnet.com",
        "en.wikipedia.org",
        "zh.wikipedia.org",
    }
)
# Not fetchable as text evidence, or not acceptable as evidence at all.
BLOCKED_HOSTS = frozenset(
    {
        "www.youtube.com",
        "youtube.com",
        "m.youtube.com",
        "youtu.be",
        "www.bilibili.com",
        "www.tiktok.com",
        "www.douyin.com",
        "www.reddit.com",
        "reddit.com",
        "old.reddit.com",
        "www.quora.com",
        "www.facebook.com",
        "x.com",
        "twitter.com",
        "www.instagram.com",
        "www.pinterest.com",
        "www.linkedin.com",
        "www.zhihu.com",
        "zhuanlan.zhihu.com",
        "tieba.baidu.com",
        "www.amazon.com",
    }
)

_PEER_REVIEW_TYPES = frozenset({"article", "review", "book-chapter", "proceedings-article"})
_REPORT_TYPES = frozenset({"report", "standard", "dissertation", "book"})


@dataclass(frozen=True)
class QualityInput:
    """Everything quality scoring is allowed to look at."""

    url: str
    title: str = ""
    is_pdf: bool | None = None
    metadata: SourceMetadata | None = None
    provider: str | None = None
    superseded_by: str | None = None


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def is_blocked_host(url: str) -> bool:
    host = host_of(url)
    return host in BLOCKED_HOSTS or any(host.endswith(f".{blocked}") for blocked in BLOCKED_HOSTS)


def has_doctrine_designation(*texts: str) -> bool:
    return any(has_designation(text) for text in texts)


def score_source(item: QualityInput) -> tuple[SourceQuality, SourceKind]:
    """Return an explainable quality score and the inferred source kind."""
    metadata = item.metadata or SourceMetadata()
    host = host_of(item.url)
    reasons: list[str] = []
    score = 0.30
    kind: SourceKind = "unknown"

    if is_blocked_host(item.url):
        return (
            SourceQuality(
                score=0.05,
                tier="D",
                reasons=[f"blocked host {host}: social, video, forum or retail content"],
                rules_version=RULES_VERSION,
            ),
            "forum",
        )

    doctrine = has_doctrine_designation(item.title, urlparse(item.url).path)
    if host in _OFFICIAL_HOSTS or host.endswith(_OFFICIAL_SUFFIXES):
        score += 0.40
        kind = "official_documentation"
        reasons.append(f"+0.40 official government/military host ({host})")
    elif metadata.doi or host in _SCHOLARLY_HOSTS or host.endswith(_SCHOLARLY_SUFFIXES):
        score += 0.30
        kind = "paper"
        reasons.append("+0.30 scholarly host or DOI-identified work")
    elif host in _INSTITUTIONAL_HOSTS:
        score += 0.25
        kind = "institutional_report"
        reasons.append(f"+0.25 research institution or professional journal ({host})")
    elif host in _MIRROR_HOSTS:
        score += 0.10
        kind = "official_documentation" if doctrine else "article"
        reasons.append(f"+0.10 document archive or mirror ({host}); verify against original")
    elif item.provider == "local_library":
        score += 0.20
        kind = "local_document"
        reasons.append("+0.20 user-curated library document")
    elif host in _PRESS_HOSTS:
        score += 0.05
        kind = "article"
        reasons.append(f"+0.05 press, trade or encyclopedia source ({host})")
    else:
        kind = "article"
        reasons.append(f"+0.00 unclassified host ({host or 'unknown'})")

    if doctrine:
        score += 0.15
        reasons.append("+0.15 doctrine publication designation in title or path")
    if metadata.doi:
        score += 0.10
        reasons.append(f"+0.10 DOI {metadata.doi}")
    if metadata.authors:
        score += 0.05
        reasons.append("+0.05 named authors")
    if metadata.year:
        score += 0.03
        reasons.append("+0.03 publication year known")
    if metadata.venue:
        score += 0.03
        reasons.append("+0.03 venue known")
    document_type = (metadata.document_type or "").lower()
    if document_type in _PEER_REVIEW_TYPES:
        score += 0.05
        reasons.append(f"+0.05 likely peer-reviewed type ({document_type})")
    elif document_type in _REPORT_TYPES:
        score += 0.03
        reasons.append(f"+0.03 report-type work ({document_type})")
    elif document_type == "preprint":
        score -= 0.05
        reasons.append("-0.05 preprint, not peer-reviewed")
    if metadata.cited_by_count is not None:
        if metadata.cited_by_count >= 100:
            score += 0.08
            reasons.append(f"+0.08 highly cited ({metadata.cited_by_count})")
        elif metadata.cited_by_count >= 10:
            score += 0.05
            reasons.append(f"+0.05 cited ({metadata.cited_by_count})")
    if item.is_pdf:
        score += 0.03
        reasons.append("+0.03 full document (PDF)")
    if item.superseded_by:
        score -= 0.15
        reasons.append(f"-0.15 superseded doctrine edition (current: {item.superseded_by})")
    if metadata.is_retracted:
        score -= 0.50
        reasons.append("-0.50 retracted publication")

    score = round(min(1.0, max(0.0, score)), 3)
    tier = "A" if score >= 0.75 else "B" if score >= 0.55 else "C" if score >= 0.35 else "D"
    return (
        SourceQuality(score=score, tier=tier, reasons=reasons, rules_version=RULES_VERSION),
        kind,
    )
