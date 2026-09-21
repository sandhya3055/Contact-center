"""Knowledge Base search tool.

``app/data/models.py`` documents a deliberate decision that caller-facing
*policy* questions are answered from structured DynamoDB fields only, never a
document store. This tool doesn't reopen that decision -- it covers the
general/FAQ/procedural content that was never a candidate for a policy field
in the first place (product wording, T&Cs, how-things-work questions).

Typed, idempotent, no branching -- same as ``policy_tools``/``claim_tools``:
this returns facts (passages), and the calling agent decides what to do with
them. The one difference from a policy lookup is what "the model phrases it"
means downstream: there it reads back a single field, here it's expected to
compose an answer out of several passages, so passages (not a rewritten
summary) are what this hands back.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import AppConfig
from app.data.knowledge_base import KnowledgeBaseRepository
from app.data.models import KnowledgeBasePassage


@dataclass(frozen=True)
class KnowledgeSearchResult:
    """Result of :func:`search_knowledge_base`.

    Attributes:
        found: Whether any passage cleared ``min_score``.
        passages: Matching passages, best score first.
    """

    found: bool
    passages: list[KnowledgeBasePassage]


def search_knowledge_base(
    config: AppConfig, query: str, max_results: int, min_score: float
) -> KnowledgeSearchResult:
    """Search the configured Bedrock Knowledge Base for ``query``.

    Args:
        config: Application configuration.
        query: The caller's question, in natural language.
        max_results: Upper bound on passages requested from the vector search.
        min_score: Passages scoring below this are dropped. A weak vector-search
            hit is worse than none: a model asked to answer from it may still
            try, rather than say the knowledge base has nothing on this.

    Returns:
        A :class:`KnowledgeSearchResult` over passages that cleared ``min_score``.
    """
    passages = [
        passage
        for passage in KnowledgeBaseRepository(config).retrieve(query, max_results)
        if passage.score >= min_score
    ]
    return KnowledgeSearchResult(found=bool(passages), passages=passages)
