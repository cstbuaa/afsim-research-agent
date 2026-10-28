"""Web search, fetching, reading, and network-safety boundaries."""

from afsim_research_agent.web.fetch import FetchedResource, FetchError, FetchPolicy, fetch_url
from afsim_research_agent.web.pdf import DownloadedPDF, PDFDownloadError, download_pdf
from afsim_research_agent.web.reader import WebReadError, extract_webpage, read_webpage
from afsim_research_agent.web.scholarly import OpenAlexSearchProvider
from afsim_research_agent.web.search import SearchError, TavilySearchProvider, search_web
from afsim_research_agent.web.security import URLValidationError

__all__ = [
    "DownloadedPDF",
    "FetchError",
    "FetchPolicy",
    "FetchedResource",
    "OpenAlexSearchProvider",
    "PDFDownloadError",
    "SearchError",
    "TavilySearchProvider",
    "URLValidationError",
    "WebReadError",
    "download_pdf",
    "extract_webpage",
    "fetch_url",
    "read_webpage",
    "search_web",
]
