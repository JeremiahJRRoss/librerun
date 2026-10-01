"""Step 5 — Pinecone internal KB search. Gracefully skips if no keys."""
from uuid import UUID

from ..search_service import SearchService


async def run(search: SearchService, queries: list[str], tenant_id: UUID) -> dict:
    if not queries:
        return {"results": [], "skipped": True, "skip_reason": "no vector queries"}
    results = await search.search_pinecone(queries, tenant_id)
    if not results and not search.pinecone_available():
        return {"results": [], "skipped": True, "skip_reason": "no internal KB configured"}
    search.stamp(results)
    return {"results": [r.model_dump() for r in results], "skipped": False, "skip_reason": None}
