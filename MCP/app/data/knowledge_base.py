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
        # No `retrievalConfiguration` at all, deliberately -- it is an optional
        # field and every way of filling it in is worse here.
        #
        # This knowledge base is a MANAGED one (`knowledgeBaseConfiguration.type`),
        # so the only shape the service accepts is `managedSearchConfiguration`:
        # `vectorSearchConfiguration` is refused server-side ("Incompatible
        # configuration: vectorSearchConfiguration is not supported for managed
        # knowledge bases"). But `managedSearchConfiguration` only exists in the
        # botocore service model from 1.43.32 onward, and the Lambda runtime's
        # built-in botocore is older than that, so sending it fails *client-side*
        # validation before any request leaves the process. Carrying a newer
        # botocore in the deployment zip to gain one field costs ~16MB.
        #
        # Omitting the field sidesteps the version split entirely and works on
        # every botocore: the service applies its own default page size (5
        # today), and `max_results` is applied below instead. The tradeoff is
        # that `max_results` is a cap rather than a request -- asking for more
        # than the service default still returns the service default.
        response = client.retrieve(
            knowledgeBaseId=self._config.require_knowledge_base_id(),
            retrievalQuery={"text": query},
        )
        return [
            KnowledgeBasePassage(
                text=result.get("content", {}).get("text", ""),
                score=float(result.get("score", 0.0)),
                source_uri=_source_uri(result),
            )
            for result in response.get("retrievalResults", [])[:max_results]
        ]
