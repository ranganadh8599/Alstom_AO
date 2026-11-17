"""Demo agent cards used for local development and the integration tests.

Each card is a complete identity + capability card: without these fields an agent
cannot be registered at all.
"""

from __future__ import annotations

import os

from alstom_ao.config import Settings
from alstom_ao.domain.models import AgentAuth, AgentCardCreate, AgentPermissions, CapabilityDetail


def _base_url() -> str:
    return os.environ.get("AO_DEMO_AGENT_BASE_URL", "http://localhost:9101").rstrip("/")


def demo_cards(settings: Settings) -> list[AgentCardCreate]:
    base = _base_url()
    environment = settings.environment
    text_part_schema = {
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "required": ["message"],
    }
    answer_schema = {
        "type": "object",
        "properties": {
            "answer": {"type": "string"},
            "citations": {"type": "array", "items": {"type": "string"}},
        },
    }

    return [
        AgentCardCreate(
            agent_id="finance-invoice-auditor",
            name="Finance Invoice Auditor",
            description=(
                "Audits vendor invoices, checks purchase-order and contract alignment, flags duplicate or "
                "out-of-policy charges, and prepares correction requests. Can create adjustment tasks but "
                "never pays an invoice without human approval."
            ),
            platform="alstom-finance",
            protocol="a2a",
            endpoint=f"{base}/agents/finance",
            owner="Finance Operations",
            owner_contact="finance-ops@example.com",
            capabilities=[
                "invoice audit",
                "duplicate invoice detection",
                "purchase order reconciliation",
                "vendor payment status",
                "expense policy checks",
            ],
            capability_details=[
                CapabilityDetail(
                    name="invoice audit",
                    description="Compare an invoice with its purchase order, contract and payment history.",
                    input_schema=text_part_schema,
                    output_schema=answer_schema,
                )
            ],
            input_schema=text_part_schema,
            output_schema=answer_schema,
            auth=AgentAuth(type="none"),
            permissions=AgentPermissions(
                allowed_roles=["user", "admin"],
                allowed_actions=["read", "query", "analyze", "write"],
                requires_approval_for=["write", "update", "payment"],
                data_classification="confidential",
            ),
            version="2.3.0",
            status="active",
            environment=environment,
            risk_level="high",
            tags=["finance", "invoice", "ap", "audit", "vendor"],
            examples=[
                "Can you check invoice INV-2024-0912 for duplicate charges?",
                "Does this vendor invoice match purchase order PO-7781?",
                "What is the payment status of supplier Schneider Electric?",
            ],
            metadata={
                "cost_center": "AP-001",
                "sla_seconds": 20,
                "litellm_agent_name": "finance-invoice-auditor",
            },
        ),
        AgentCardCreate(
            agent_id="hr-policy-advisor",
            name="HR Policy Advisor",
            description=(
                "Answers employee questions about HR policy: leave, travel, remote work, benefits and "
                "onboarding. Read-only; it cites the policy document and section for every answer."
            ),
            platform="alstom-people",
            protocol="a2a",
            endpoint=f"{base}/agents/hr",
            owner="People & Culture",
            owner_contact="people@example.com",
            capabilities=[
                "hr policy lookup",
                "leave and absence rules",
                "travel and expense policy",
                "benefits questions",
                "onboarding guidance",
            ],
            input_schema=text_part_schema,
            output_schema=answer_schema,
            auth=AgentAuth(type="none"),
            permissions=AgentPermissions(
                allowed_roles=["user", "admin"],
                allowed_actions=["read", "query", "analyze"],
                data_classification="internal",
            ),
            version="1.8.2",
            status="active",
            environment=environment,
            risk_level="low",
            tags=["hr", "policy", "leave", "benefits", "people"],
            examples=[
                "How many days of annual leave do I get after three years?",
                "What is the travel policy for economy class in Europe?",
                "Can I work remotely from another country for two weeks?",
            ],
            metadata={"policy_corpus": "hr-2024.4", "litellm_agent_name": "hr-policy-advisor"},
        ),
        AgentCardCreate(
            agent_id="it-service-desk",
            name="IT Service Desk",
            description=(
                "Resolves IT knowledge questions: account access, VPN, laptop provisioning, software "
                "requests. Provides instructions and can open a ticket; it cannot reset credentials by "
                "itself."
            ),
            platform="alstom-it",
            protocol="a2a",
            endpoint=f"{base}/agents/it",
            owner="IT Service Management",
            owner_contact="servicedesk@example.com",
            capabilities=[
                "vpn troubleshooting",
                "account access guidance",
                "software catalog",
                "laptop provisioning",
                "ticket creation",
            ],
            input_schema=text_part_schema,
            output_schema=answer_schema,
            auth=AgentAuth(type="none"),
            permissions=AgentPermissions(
                allowed_roles=["user", "admin"],
                allowed_actions=["read", "query", "analyze", "write"],
                requires_approval_for=["write", "execute"],
                data_classification="internal",
            ),
            version="4.1.0",
            status="active",
            environment=environment,
            risk_level="medium",
            tags=["it", "support", "vpn", "access", "laptop"],
            examples=[
                "How do I connect to the VPN from a Mac?",
                "Which software can I install without a ticket?",
                "My laptop is slow, what should I do?",
            ],
            metadata={"ticket_system": "servicenow", "litellm_agent_name": "it-service-desk"},
        ),
    ]
