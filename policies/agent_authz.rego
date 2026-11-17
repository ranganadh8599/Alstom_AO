# Agent authorization policy for the Alstom AO orchestration layer.
#
# Evaluated *before* an agent is invoked. Input is the document built by
# `alstom_ao.policy.service.build_policy_input`.
#
# `import rego.v1` keeps this file valid on OPA 0.5x (keywords enabled) and
# OPA 1.x (v1 is the default).

package alstom.authz

import rego.v1

default allow := false
default requires_approval := false

high_impact_actions := {"write", "update", "delete", "send", "execute", "payment"}

roles := object.get(input.user, "roles", [])

has_role(role) if {
	some candidate in roles
	lower(candidate) == lower(role)
}

is_admin if has_role("admin")

# --- individual checks -----------------------------------------------------

status_ok if input.agent.status == "active"

environment_ok if input.agent.environment == input.environment

environment_ok if is_admin

roles_ok if {
	allowed := object.get(input.agent.permissions, "allowed_roles", [])
	count(allowed) == 0
}

roles_ok if {
	allowed := object.get(input.agent.permissions, "allowed_roles", [])
	some role in allowed
	has_role(role)
}

roles_ok if is_admin

action_ok if {
	allowed := object.get(input.agent.permissions, "allowed_actions", [])
	count(allowed) > 0
	input.action in allowed
}

action_ok if {
	allowed := object.get(input.agent.permissions, "allowed_actions", [])
	count(allowed) == 0
	not input.action in high_impact_actions
}

action_ok if is_admin

scopes_ok if {
	required := object.get(input.agent.permissions, "required_scopes", [])
	count(required) == 0
}

scopes_ok if {
	required := object.get(input.agent.permissions, "required_scopes", [])
	granted := object.get(input.service, "scopes", [])
	every scope in required {
		scope in granted
	}
}

scopes_ok if is_admin

tenant_ok if {
	agent_tenant := object.get(object.get(input.agent, "metadata", {}), "tenant_id", "")
	agent_tenant == ""
}

tenant_ok if {
	agent_tenant := object.get(object.get(input.agent, "metadata", {}), "tenant_id", "")
	user_tenant := object.get(input.user, "tenant_id", "")
	agent_tenant == user_tenant
}

tenant_ok if is_admin

classification_ok if object.get(input.agent.permissions, "data_classification", "internal") != "restricted"

classification_ok if {
	has_role("admin")
}

classification_ok if is_admin

# --- decision --------------------------------------------------------------

allow if {
	status_ok
	environment_ok
	roles_ok
	action_ok
	scopes_ok
	tenant_ok
	classification_ok
}

requires_approval if {
	input.agent.risk_level == "high"
	input.action in high_impact_actions
}

requires_approval if input.action in object.get(input.agent.permissions, "requires_approval_for", [])

requires_approval if input.action in object.get(input.agent.permissions, "high_impact_actions", [])

reasons contains "agent_not_active" if not status_ok
reasons contains "environment_mismatch" if not environment_ok
reasons contains "role_not_permitted" if not roles_ok
reasons contains "action_not_permitted" if not action_ok
reasons contains "service_scope_missing" if not scopes_ok
reasons contains "tenant_mismatch" if not tenant_ok
reasons contains "restricted_data_role_missing" if not classification_ok

decision := {
	"allow": allow,
	"reasons": sort([reason | some reason in reasons]),
	"obligations": {"requires_approval": requires_approval},
}
