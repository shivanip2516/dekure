import frappe
from frappe.utils import strip_html


ACTION_TERMINAL_STATUSES = {
	"Approval": {"approved", "rejected", "cancelled"},
	"Completion": {"completed", "cancelled"},
}
STATUS_FIELDS = ("approval_status", "status", "custom_status", "workflow_state")


def classify_pwa_notification(doc, method=None):
	"""Classify standard actionable notifications without changing read state."""
	if doc.get("custom_action_type"):
		return

	message = strip_html(doc.get("message") or "").lower()
	if "for approval" in message:
		doc.custom_action_type = "Approval"


def resolve_actionable_notifications(doc, method=None):
	"""Resolve matching actions only after a persisted, terminal status change."""
	previous = doc.get_doc_before_save()
	if not previous:
		return

	terminal_actions = _get_completed_actions(doc, previous)
	if not terminal_actions:
		return

	notifications = frappe.get_all(
		"PWA Notification",
		filters={
			"reference_document_type": doc.doctype,
			"reference_document_name": doc.name,
			"custom_action_type": ["in", list(terminal_actions)],
			"custom_resolved": 0,
		},
		fields=["name", "to_user"],
	)
	if not notifications:
		return

	frappe.db.set_value(
		"PWA Notification",
		{"name": ["in", [row.name for row in notifications]]},
		"custom_resolved",
		1,
		update_modified=False,
	)

	from hrms import refetch_resource

	for user in {row.to_user for row in notifications if row.to_user}:
		refetch_resource("hrms:notifications", user)
		refetch_resource("hrms:unread_notifications_count", user)


def _get_completed_actions(doc, previous):
	changed_statuses = {
		str(doc.get(fieldname)).strip().lower()
		for fieldname in STATUS_FIELDS
		if doc.meta.has_field(fieldname)
		and doc.get(fieldname) != previous.get(fieldname)
		and doc.get(fieldname)
	}

	return {
		action
		for action, terminal_statuses in ACTION_TERMINAL_STATUSES.items()
		if changed_statuses & terminal_statuses
	}


@frappe.whitelist()
def get_active_unread_notifications_count():
	return frappe.db.count(
		"PWA Notification",
		{
			"to_user": frappe.session.user,
			"read": 0,
			"custom_resolved": 0,
		},
	)
