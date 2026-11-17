"""Central configuration for the Alstom AO orchestration layer.

Every setting is environment driven (``AO_*``) so the same image can run in dev,
staging and production without code changes. Settings are immutable and cached.
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic import field_validator
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
    default_user_roles: str = "user"
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
    planner_min_confidence: float = 0.35
    planner_max_agents: int = 4

    # --- adapters --------------------------------------------------------
    a2a_timeout_seconds: float = 30.0
    a2a_max_retries: int = 2
    a2a_discovery_path: str = "/.well-known/agent-card.json"
    a2a_legacy_discovery_path: str = "/.well-known/agent.json"
    max_parallel_agents: int = 4
    run_timeout_seconds: float = 120.0

    # --- policy ----------------------------------------------------------
    policy_mode: str = "observe"  # observe | enforce
    opa_url: str | None = "http://localhost:8181"
    opa_decision_path: str = "alstom/authz/decision"
    opa_timeout_seconds: float = 2.0
    policy_local_fallback: bool = True

    # --- approvals -------------------------------------------------------
    approval_risk_threshold: str = "high"
    approval_ttl_minutes: int = 120
    approval_admin_roles: str = "platform-admin,approver"
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

    @property
    def approval_admin_role_list(self) -> list[str]:
        return [item.strip() for item in self.approval_admin_roles.split(",") if item.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"prod", "production"}

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
