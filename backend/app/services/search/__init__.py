"""Global search (issue #879)."""

from app.services.search.engine import execute, visible_providers
from app.services.search.providers import PROVIDERS, SearchProvider
from app.services.search.schemas import (
    QueryShape,
    SearchResponse,
    SearchResult,
    SearchTypeInfo,
    shape_of,
)

__all__ = [
    "PROVIDERS",
    "QueryShape",
    "SearchProvider",
    "SearchResponse",
    "SearchResult",
    "SearchTypeInfo",
    "execute",
    "shape_of",
    "visible_providers",
]
