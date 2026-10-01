"""Step 6 — Tavily public web search, bucketed by vendor."""
import logging

from ..search_service import SearchService

logger = logging.getLogger(__name__)


async def run(search: SearchService, queries_a: dict, queries_b: dict) -> dict:
    a_queries = queries_a.get("web_queries", [])
    b_queries = queries_b.get("web_queries", [])
    a_domain = queries_a.get("domain_hints") or None
    b_domain = queries_b.get("domain_hints") or None

    if not a_queries:
        logger.warning(
            "Step 6: No web queries for vendor A. Keys in queries_a: %s",
            list(queries_a.keys()) if isinstance(queries_a, dict) else type(queries_a).__name__,
        )
    if not b_queries:
        logger.warning(
            "Step 6: No web queries for vendor B. Keys in queries_b: %s",
            list(queries_b.keys()) if isinstance(queries_b, dict) else type(queries_b).__name__,
        )

    vendor_a = await search.search_tavily(a_queries, a_domain) if a_queries else []
    vendor_b = await search.search_tavily(b_queries, b_domain) if b_queries else []
    search.stamp(vendor_a + vendor_b)

    return {
        "vendor_a_results": [r.model_dump() for r in vendor_a],
        "vendor_b_results": [r.model_dump() for r in vendor_b],
        "public_results": [],
    }
