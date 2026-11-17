# Who may approve a pending human-in-the-loop request.
#
# Two invariants:
#   1. the approver must hold an approver-class role
#   2. nobody approves their own request (separation of duties)

package alstom.approval

import rego.v1

default allow := false

approver_roles := {"approver", "platform-admin", "compliance-officer"}

roles := object.get(input.user, "roles", [])

has_role(role) if {
	some candidate in roles
	lower(candidate) == lower(role)
}

has_approver_role if {
	some role in approver_roles
	has_role(role)
}

not_self_approval if input.user.user_id != input.requested_by

allow if {
	has_approver_role
	not_self_approval
}

reasons contains "approver_role_required" if not has_approver_role
reasons contains "cannot_approve_own_request" if not not_self_approval

decision := {
	"allow": allow,
	"reasons": sort([reason | some reason in reasons]),
}
