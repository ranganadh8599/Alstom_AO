"""SQLAlchemy tables: the central agent registry plus the audit ledger.

Design notes
------------
* ``agent_cards`` is the single source of truth for who an agent is and what it
  may do. Cards are versioned by the agent itself; the registry keeps the
  currently published card per (agent_id).
* ``runs`` and ``trace_events`` form an append-only operational ledger. Even if
  Langfuse is unavailable, we can answer "who did what, on whose behalf, when,
  with which inputs and outputs".
* ``approvals`` is separate from runs so a pending approval survives restarts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from alstom_ao.db.base import Base, TimestampMixin, utcnow


class AgentCardRow(TimestampMixin, Base):
    __tablename__ = "agent_cards"

    agent_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    platform: Mapped[str] = mapped_column(String(64), nullable=False, default="custom")
    protocol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    endpoint: Mapped[str] = mapped_column(String(500), nullable=False)
    owner: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    owner_contact: Mapped[str | None] = mapped_column(String(200))
    capabilities: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    capability_details: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    input_schema: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    output_schema: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    auth: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    permissions: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False, default="1.0.0")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active", index=True)
    environment: Mapped[str] = mapped_column(String(24), nullable=False, default="dev", index=True)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False, default="low")
    tags: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    examples: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    health: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    last_health_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict, nullable=False)

    __table_args__ = (
        UniqueConstraint("name", "version", "environment", name="uq_agent_cards_name_version_env"),
        Index("ix_agent_cards_env_status", "environment", "status"),
    )


class RunRow(TimestampMixin, Base):
    __tablename__ = "runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    query: Mapped[str] = mapped_column(Text, nullable=False, default="")
    environment: Mapped[str] = mapped_column(String(24), nullable=False, default="dev", index=True)
    use_case: Mapped[str] = mapped_column(String(128), nullable=False, default="general")
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, default="unknown", index=True)
    tenant_id: Mapped[str | None] = mapped_column(String(128), index=True)
    identity: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    plan: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    selected_agents: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    response: Mapped[str] = mapped_column(Text, nullable=False, default="")
    error: Mapped[str | None] = mapped_column(Text)
    approval_id: Mapped[str | None] = mapped_column(String(64), index=True)
    usage: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    langfuse_url: Mapped[str | None] = mapped_column(String(500))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TraceEventRow(Base):
    __tablename__ = "trace_events"

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    span_id: Mapped[str] = mapped_column(String(32), nullable=False)
    parent_span_id: Mapped[str | None] = mapped_column(String(32))
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    agent_id: Mapped[str | None] = mapped_column(String(128), index=True)
    agent_version: Mapped[str | None] = mapped_column(String(32))
    protocol: Mapped[str | None] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="started")
    initiated_by: Mapped[str | None] = mapped_column(String(128))
    authenticated_service: Mapped[str | None] = mapped_column(String(128))
    use_case: Mapped[str | None] = mapped_column(String(128))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    input: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict, nullable=False)


class ApprovalRow(Base):
    __tablename__ = "approvals"

    approval_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    agent_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False, default="query")
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False, default="low")
    reasons: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending", index=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[str | None] = mapped_column(String(200))
    decision_note: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class PolicyDecisionRow(Base):
    __tablename__ = "policy_decisions"

    decision_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    agent_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="local")
    reasons: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    obligations: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
