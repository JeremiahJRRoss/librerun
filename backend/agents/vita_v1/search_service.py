"""Tavily + Pinecone search service."""
from __future__ import annotations

from uuid import UUID

import httpx
import structlog
from pydantic import BaseModel

from app.logging_pii import user_content

logger = structlog.get_logger(__name__)


class SearchResult(BaseModel):
    title: str
    url: str
    snippet: str
    relevance_score: float
    doc_type: str


class SearchService:
    """VITA's search surface, backed by the run's GRANTED capabilities.

    Blueprint B13: the manifest ``capabilities:`` list is the grant, so
    this takes the run façade and reaches ``kb``/``llm`` through it —
    constructing the capability classes directly would read the same
    data while bypassing the grant the manifest declares.
    """

    def __init__(self, capabilities, *, search_depth: str = "advanced", top_k: int = 10):
        self._caps = capabilities
        # This tenant's tavily_search_depth and pinecone_top_k, which the
        # agent reads when the phase starts (K5b). The defaults are the
        # values hard-coded here before, so a service built without them
        # searches as it always did.
        self.search_depth = search_depth
        self.top_k = top_k

    def pinecone_available(self) -> bool:
        """Internal-KB availability, answered by the granted ``kb``
        capability (raises if the manifest doesn't grant it)."""
        return self._caps.kb.available()

    def stamp(self, results: list[SearchResult]) -> int:
        """Put the documents on the current step span (blueprint S2): the
        chassis stamps nothing it did not fetch, so each retrieval step
        stamps what it found — internal KB and Tavily alike — through the
        granted ``kb`` capability's helper."""
        return self._caps.kb.stamp([r.model_dump() for r in results])

    async def search_tavily(
        self,
        queries: list[str],
        domain_hints: list[str] | None = None,
    ) -> list[SearchResult]:
        # The agent's own tool secret, declared in agent.yaml's secrets[]
        # (K8a, D20): this tenant's value, else every tenant's default,
        # else the TAVILY_API_KEY this process was given (L20). Unset
        # still means "no web results", as it always has.
        from app.capabilities import SecretNotSet

        try:
            tavily_key = await self._caps.secrets.get("tavily_api_key")
        except SecretNotSet:
            return []
        seen: set[str] = set()
        out: list[SearchResult] = []
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                for q in queries:
                    body = {
                        "api_key": tavily_key,
                        "query": q,
                        "max_results": 5,
                        "search_depth": self.search_depth,
                    }
                    if domain_hints:
                        body["include_domains"] = domain_hints
                    resp = await client.post("https://api.tavily.com/search", json=body)
                    if not resp.is_success:
                        continue
                    for r in resp.json().get("results", []):
                        u = r.get("url")
                        if not u or u in seen:
                            continue
                        seen.add(u)
                        out.append(
                            SearchResult(
                                title=r.get("title", ""),
                                url=u,
                                snippet=(r.get("content") or "")[:500],
                                relevance_score=float(r.get("score", 0.5) or 0.5),
                                doc_type="kb_article",
                            )
                        )
        except Exception as e:
            logger.warning("tavily_search_failed", error=user_content(str(e)))
        return out

    async def search_pinecone(
        self, queries: list[str], tenant_id: UUID | None = None  # noqa: ARG002
    ) -> list[SearchResult]:
        """Internal-KB search via the granted ``kb`` capability.

        ``tenant_id`` is accepted for call-site compatibility but no
        longer used: the façade is already bound to this run's tenant,
        which removes the chance of an agent searching another tenant's
        namespace by passing the wrong id.
        """
        results = await self._caps.kb.search(queries, top_k=self.top_k)
        return [
            SearchResult(
                title=r.get("title", "KB entry"),
                url=r.get("url", ""),
                snippet=r.get("snippet", ""),
                relevance_score=float(r.get("relevance_score", 0.5)),
                doc_type="kb_article",
            )
            for r in results
        ]
