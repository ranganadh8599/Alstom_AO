"""Central configuration for the Alstom AO orchestration layer.

Every setting is environment driven (``AO_*``) so the same image can run in dev,
staging and production without code changes. Settings are immutable and cached.
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AO_", env_file=".env", extra="ignore")

    # --- service ---------------------------------------------------------
    app_name: str = "Alstom AO"
    environment: str = "dev"
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 8080
    cors_origins: str = "*"
    db_auto_create: bool = True
    seed_demo_agents: bool = True

    # --- database --------------------------------------------------------
    database_url: str = "sqlite+aiosqlite:///./alstom_ao.db"
    db_echo: bool = False

    # --- identity --------------------------------------------------------
    require_user_identity: bool = True
    trust_forwarded_identity: bool = True
    # --- identity / roles -------------------------------------------------
    # Role vocabulary is deliberately just ADMIN/USER, matching what the chat UI
    # (LibreChat) presents. Business roles are not modelled yet: they belong to the
    # IdP mapping, which does not exist in this deployment.
    default_user_roles: str = "user"
    admin_roles: str = "admin"
    approver_roles: str = "admin"
    reviewer_roles: str = "admin"
    # Incoming role names are normalised with this map (case-insensitive), so a UI
    # sending "ADMIN"/"USER" (or "Admin") resolves to the canonical names.
    role_aliases: str = "admin:admin,user:user"
    service_id: str = "alstom-ao"
    service_name: str = "agent-orchestrator"

    # --- LLM gateway -----------------------------------------------------
    llm_mode: str = "mock"  # mock | litellm
    llm_model: str = "openai/gpt-4o-mini"
    llm_api_base: str | None = None
    llm_api_key: str | None = None
    llm_fallback_models: str = ""
    llm_temperature: float = 0.0
    llm_max_tokens: int = 1200
    llm_timeout_seconds: float = 45.0
    llm_max_retries: int = 2
    llm_max_tokens_per_run: int = 20_000
    llm_max_cost_usd_per_run: float = 0.50
    llm_max_calls_per_run: int = 12
    # Tiered budget: warn (and optionally downgrade) at this fraction of any limit.
    budget_soft_threshold: float = 0.8
    # Cheaper model used for the rest of the run once the soft threshold is crossed.
    budget_degrade_model: str | None = None
    # Fire-and-forget alert on soft/hard crossings (Slack-compatible incoming webhook).
    budget_alert_webhook: str | None = None
    budget_alert_timeout_seconds: float = 3.0
    # Runaway-loop protection: caps that bite long before dollars do.
    max_agent_hops: int = 8
    max_a2a_depth: int = 3
    # Numerical grounding for synthesized answers: off | warn | strict.
    synthesis_grounding: str = "warn"
    # In strict mode, one rewrite attempt before redacting (0 disables).
    synthesis_repair_attempts: int = 1
    # Content guardrails on untrusted input / outgoing text: off | warn | block.
    guardrails_mode: str = "warn"
    # Agent output validated against its card's output_schema: off | warn | enforce.
    output_schema_mode: str = "warn"
    # Allow `$ref` values that point outside the schema document. Off by default:
    # resolving them would make a validation verdict depend on a network fetch.
    allow_remote_schema_refs: bool = False
    planner_min_confidence: float = 0.35
    planner_max_agents: int = 4

    # --- adapters --------------------------------------------------------
    a2a_timeout_seconds: float = 30.0
    a2a_max_retries: int = 2
    a2a_discovery_path: str = "/.well-known/agent-card.json"
    a2a_legacy_discovery_path: str = "/.well-known/agent.json"
    max_parallel_agents: int = 4
    run_timeout_seconds: float = 120.0

    # --- A2A routing: direct to the agent, or through the LiteLLM agent gateway
    a2a_routing: str = "direct"  # direct | litellm_proxy
    litellm_proxy_url: str = "http://localhost:4000"
    litellm_proxy_api_key: str | None = None
    litellm_proxy_api_key_ref: str | None = None
    litellm_agent_prefix: str = "/a2a"
    litellm_a2a_protocol_version: str = "0.3"  # wire format the proxy serves us
    litellm_forward_trace_headers: bool = True

    # --- policy ----------------------------------------------------------
    policy_mode: str = "observe"  # observe | enforce
    opa_url: str | None = "http://localhost:8181"
    opa_decision_path: str = "alstom/authz/decision"
    opa_timeout_seconds: float = 2.0
    policy_local_fallback: bool = True

    # --- approvals -------------------------------------------------------
    approval_risk_threshold: str = "high"
    approval_ttl_minutes: int = 120
    approval_admin_roles: str = "admin"
    approval_admin_bypass: bool = False
    approval_allow_self: bool = False

    # --- observability ---------------------------------------------------
    langfuse_enabled: bool = False
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str = "https://cloud.langfuse.com"
    trace_sample_rate: float = 1.0

    # --- misc ------------------------------------------------------------
    virtual_model_id: str = "alstom-ao"
    max_query_chars: int = 6000
    metrics_enabled: bool = True
    metrics_token: str | None = None

    @field_validator("policy_mode")
    @classmethod
    def _validate_policy_mode(cls, value: str) -> str:
        value = value.lower().strip()
        if value not in {"observe", "enforce"}:
            raise ValueError("policy_mode must be 'observe' or 'enforce'")
        return value

    @field_validator("llm_mode")
    @classmethod
    def _validate_llm_mode(cls, value: str) -> str:
        value = value.lower().strip()
        if value not in {"mock", "litellm"}:
            raise ValueError("llm_mode must be 'mock' or 'litellm'")
        return value

    @field_validator("a2a_routing")
    @classmethod
    def _validate_a2a_routing(cls, value: str) -> str:
        value = value.lower().strip()
        if value not in {"direct", "litellm_proxy"}:
            raise ValueError("a2a_routing must be 'direct' or 'litellm_proxy'")
        return value

    @model_validator(mode="after")
    def _production_defaults(self) -> Settings:
        """Tighten unsafe defaults automatically when the environment is production."""

        if self.is_production:
            # `model_config` allows mutation here: this runs once at construction.
            if self.debug:
                raise ValueError("AO_DEBUG must be false in production")
            # Seeding is refused at startup (see initialize_app) because a hard
            # failure is clearer than a silent flip; we only note other hazards.
        return self

    @field_validator("synthesis_grounding")
    @classmethod
    def _validate_grounding(cls, value: str) -> str:
        value = value.lower().strip()
        if value not in {"off", "warn", "strict"}:
            raise ValueError("synthesis_grounding must be 'off', 'warn' or 'strict'")
        return value

    @field_validator("guardrails_mode")
    @classmethod
    def _validate_guardrails(cls, value: str) -> str:
        value = value.lower().strip()
        if value not in {"off", "warn", "block"}:
            raise ValueError("guardrails_mode must be 'off', 'warn' or 'block'")
        return value

    @field_validator("output_schema_mode")
    @classmethod
    def _validate_schema_mode(cls, value: str) -> str:
        value = value.lower().strip()
        if value not in {"off", "warn", "enforce"}:
            raise ValueError("output_schema_mode must be 'off', 'warn' or 'enforce'")
        return value

    @field_validator("litellm_a2a_protocol_version")
    @classmethod
    def _validate_protocol_version(cls, value: str) -> str:
        value = str(value).strip()
        if value not in {"0.3", "1.0"}:
            raise ValueError("litellm_a2a_protocol_version must be '0.3' or '1.0'")
        return value

    # --- convenience accessors -------------------------------------------
    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def fallback_models(self) -> list[str]:
        return [item.strip() for item in self.llm_fallback_models.split(",") if item.strip()]

    @property
    def default_roles(self) -> list[str]:
        return [item.strip() for item in self.default_user_roles.split(",") if item.strip()]

    def _role_set(self, value: str) -> list[str]:
        return [item.strip().lower() for item in value.split(",") if item.strip()]

    @property
    def admin_role_list(self) -> list[str]:
        return self._role_set(self.admin_roles)

    @property
    def approver_role_list(self) -> list[str]:
        return self._role_set(self.approver_roles)

    @property
    def reviewer_role_list(self) -> list[str]:
        return self._role_set(self.reviewer_roles)

    @property
    def role_alias_map(self) -> dict[str, str]:
        """Incoming role name (lowercase) -> canonical role name."""

        mapping: dict[str, str] = {}
        for pair in self.role_aliases.split(","):
            if not pair.strip() or ":" not in pair:
                continue
            source, target = pair.split(":", 1)
            mapping[source.strip().lower()] = target.strip().lower()
        return mapping

    def canonical_roles(self, roles: list[str]) -> list[str]:
        """Normalise presented roles to the platform vocabulary.

        Unknown roles are dropped rather than propagated: the vocabulary is just
        ADMIN/USER, and forwarding names that no rule recognises only produces
        confusing "role not permitted" denials.
        """

        aliases = self.role_alias_map
        canonical: list[str] = []
        for role in roles:
            key = role.strip().lower()
            if not key:
                continue
            mapped = aliases.get(key)
            if mapped and mapped not in canonical:
                canonical.append(mapped)
        if not canonical and self.default_roles:
            canonical = [item.strip().lower() for item in self.default_roles]
        return canonical

    @property
    def approval_admin_role_list(self) -> list[str]:
        return [item.strip() for item in self.approval_admin_roles.split(",") if item.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"prod", "production"}

    @property
    def uses_litellm_proxy(self) -> bool:
        """True when agent traffic is routed through the LiteLLM agent gateway."""

        return self.a2a_routing == "litellm_proxy" and bool(self.litellm_proxy_url)

    @property
    def litellm_proxy_base(self) -> str:
        return self.litellm_proxy_url.rstrip("/")

    def litellm_proxy_key(self) -> str | None:
        """Virtual key for the proxy: explicit value wins over an ``env:`` reference."""

        if self.litellm_proxy_api_key:
            return self.litellm_proxy_api_key
        return self.resolve_secret_ref(self.litellm_proxy_api_key_ref)

    def resolve_secret_ref(self, reference: str | None) -> str | None:
        """Resolve ``env:NAME`` secret references. Raw secrets are never stored in cards."""
        if not reference:
            return None
        if reference.startswith("env:"):
            return os.environ.get(reference[4:])
        return reference


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
