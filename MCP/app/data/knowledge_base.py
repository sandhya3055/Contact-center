"""Bedrock Knowledge Base access layer (boto3 ``bedrock-agent-runtime`` client).

Wraps the Knowledge Base's ``Retrieve`` API only -- raw passages + citations,
no generation. Mirrors the split ``dynamo.py``/``policy_tools.py`` already use:
this layer fetches grounding text, and the calling agent (Ava, via Nova Sonic)
is the one that turns it into a spoken answer, same as every other tool here
returns facts rather than pre-written phrasing.
"""

from __future__ import annotations

import boto3
from botocore.config import Config

from app.config import AppConfig
from app.data.models import KnowledgeBasePassage

# Lambda handles one invocation at a time per execution environment (see
# mcp_tools.py's own reasoning for its module-level _LOG_CONTEXT), so a plain
# module-level cache is enough here -- no thread-local dance like dynamo.py's,
# which serves a genuinely multi-threaded pipeline elsewhere.
_cached: tuple | None = None


def _build_client(
    region: str, connect_timeout: float, read_timeout: float, max_attempts: int
):
    """Construct a new boto3 ``bedrock-agent-runtime`` client with fail-fast timeouts."""
    boto_config = Config(
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        retries={"mode": "adaptive", "max_attempts": max_attempts},
    )
    session = boto3.session.Session()
    return session.client("bedrock-agent-runtime", region_name=region, config=boto_config)


def get_bedrock_agent_client(config: AppConfig):
    """Return the cached ``bedrock-agent-runtime`` client, built once per config."""
    global _cached
    key = (
        config.kb_region,
        config.boto_connect_timeout_secs,
        config.boto_read_timeout_secs,
        config.boto_retry_max_attempts,
    )
    if _cached is not None and _cached[0] == key:
        return _cached[1]
    client = _build_client(*key)
    _cached = (key, client)
    return client


def _source_uri(result: dict) -> str | None:
    """Best-effort source location for a retrieval result.

    The Retrieve API reports one of a few location shapes depending on the
    Knowledge Base's data source (S3 today; web crawlers report differently) --
    checked in order, first one present wins.
    """
    location = result.get("location") or {}
    s3 = location.get("s3Location") or {}
    if s3.get("uri"):
        return s3["uri"]
    web = location.get("webLocation") or {}
    return web.get("url")


class KnowledgeBaseRepository:
    """Read access to the configured Bedrock Knowledge Base."""

    def __init__(self, config: AppConfig) -> None:
        """Bind the repository to the configured knowledge base + region."""
        self._config = config

    def retrieve(self, query: str, max_results: int) -> list[KnowledgeBasePassage]:
        """Return up to ``max_results`` passages for ``query``, best score first."""
        client = get_bedrock_agent_client(self._config)
        response = client.retrieve(
            knowledgeBaseId=self._config.require_knowledge_base_id(),
            retrievalQuery={"text": query},
            # `managedSearchConfiguration`, not `vectorSearchConfiguration`:
            # the configured knowledge base is a MANAGED one (its
            # `knowledgeBaseConfiguration.type`), and a managed base rejects the
            # vector shape outright -- "Incompatible configuration:
            # vectorSearchConfiguration is not supported for managed knowledge
            # bases", a ValidationException on every call, not a degraded
            # result. The two are mutually exclusive, so a base rebuilt as a
            # customer-managed vector store needs this key swapped back.
            retrievalConfiguration={
                "managedSearchConfiguration": {"numberOfResults": max_results}
            },
        )
        return [
            KnowledgeBasePassage(
                text=result.get("content", {}).get("text", ""),
                score=float(result.get("score", 0.0)),
                source_uri=_source_uri(result),
            )
            for result in response.get("retrievalResults", [])
        ]
