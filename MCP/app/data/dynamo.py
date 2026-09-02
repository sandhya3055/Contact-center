"""DynamoDB access layer (boto3 resource). [M1]

``DATA_MODE=local`` targets DynamoDB Local via ``DYNAMODB_ENDPOINT`` with dummy creds;
``DATA_MODE=aws`` uses real tables via the standard credential chain. The same code path
serves both — only the resource wiring differs.

Reads are naturally idempotent. Lookups use the ``mobileNo-index`` GSI, never a scan
(decision D15 mandates GSIs for scale; CK §1.4 forbids open/unbounded queries).
"""

from __future__ import annotations

import threading

import boto3
from boto3.dynamodb.conditions import Key
from botocore.config import Config

from app.config import AppConfig
from app.data.models import Policy

_MOBILE_INDEX = "mobileNo-index"

# Flow nodes run on threadpool threads (`asyncio.to_thread`), and boto3 resources are
# not documented thread-safe — unlike clients. A thread-local cache gives each worker
# thread its own resource instead of sharing one across the process (defect 4).
#
# The resource is built from a thread-local `boto3.session.Session` rather than the
# module-level `boto3.resource()` helper: that helper resolves against the shared
# default session, whose botocore loader is itself not thread-safe, so concurrent
# first-time construction from several worker threads can race even though each
# thread ends up with its own resource. Per-thread Session is boto3's documented
# guidance for exactly this.
_local = threading.local()


def _build_resource(
    data_mode: str,
    endpoint: str,
    region: str,
    connect_timeout: float,
    read_timeout: float,
    max_attempts: int,
):
    """Construct a new boto3 DynamoDB resource with fail-fast client timeouts."""
    boto_config = Config(
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        retries={"mode": "adaptive", "max_attempts": max_attempts},
    )
    session = boto3.session.Session()
    if data_mode == "local":
        return session.resource(
            "dynamodb",
            endpoint_url=endpoint,
            region_name=region,
            aws_access_key_id="local",
            aws_secret_access_key="local",
            config=boto_config,
        )
    # AWS: resolve credentials from the standard chain (env / profile / SSO / role).
    return session.resource("dynamodb", region_name=region, config=boto_config)


def get_dynamo_resource(config: AppConfig):
    """Return this thread's DynamoDB resource, built once per (thread, config)."""
    key = (
        config.data_mode,
        config.dynamodb_endpoint,
        config.aws_region,
        config.boto_connect_timeout_secs,
        config.boto_read_timeout_secs,
        config.boto_retry_max_attempts,
    )
    cached = getattr(_local, "resource", None)
    if cached is not None and cached[0] == key:
        return cached[1]
    resource = _build_resource(*key)
    _local.resource = (key, resource)
    return resource


class PolicyRepository:
    """Read access to the ``policies`` table."""

    def __init__(self, config: AppConfig) -> None:
        """Bind the repository to the configured table + data mode."""
        self._config = config

    @property
    def _table(self):
        """Resolve the table fresh each access, off this thread's own resource."""
        return get_dynamo_resource(self._config).Table(self._config.policies_table)

    def find_by_mobile(self, mobile_no: str) -> list[Policy]:
        """Return all policies registered to ``mobile_no`` via the mobileNo GSI.

        Args:
            mobile_no: The caller's number in the stored format (e.g. ``+61400000001``).

        Returns:
            Matching policies (possibly empty). Never scans the table.
        """
        if not mobile_no:
            return []
        response = self._table.query(
            IndexName=_MOBILE_INDEX,
            KeyConditionExpression=Key("mobileNo").eq(mobile_no),
        )
        return [Policy.from_item(item) for item in response.get("Items", [])]

    def get(self, policy_id: str) -> Policy | None:
        """Return the policy row, or ``None`` if it doesn't exist."""
        response = self._table.get_item(Key={"policyId": policy_id})
        item = response.get("Item")
        return Policy.from_item(item) if item else None
