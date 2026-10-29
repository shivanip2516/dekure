import frappe
from frappe.model.workflow import get_workflow_name, get_workflow_state_field
from frappe.utils import strip_html


ACTION_TERMINAL_STATUSES = {
	"Approval": {"approved", "rejected", "cancelled"},
	"Completion": {"completed", "cancelled"},
}
STATUS_FIELDS = ("approval_status", "status", "custom_status", "workflow_state")


class ExpenseClaimNotificationCleanupMixin:
	def run_before_save_methods(self):
		# Snapshot before validate/before-save hooks can generate new-stage logs.
		# The parent lifecycle has already checked write permissions and locked
		# the Expense Claim. New claims must keep all their initial notifications.
		self.flags.expense_notification_snapshot = None
		previous = self.get_doc_before_save()
		if previous:
			fields = {"status", "approval_status", "docstatus"}
			workflow = get_workflow_name(self.doctype)
			if workflow:
				fields.add(get_workflow_state_field(workflow))
			self.flags.expense_notification_snapshot = {
				"state": {field: previous.get(field) for field in fields if field},
				"logs": frappe.get_all("Notification Log", filters={
					"document_type": "Expense Claim", "document_name": self.name,
				}, fields=["name", "for_user"]),
			}
		return super().run_before_save_methods()

	def run_post_save_methods(self):
		# Run the full Save/Submit/Cancel lifecycle first, including on_change
		# notifications. Delete only the IDs captured before this transition.
		result = super().run_post_save_methods()
		snapshot = self.flags.get("expense_notification_snapshot")
		if snapshot and any(self.get(field) != value for field, value in snapshot["state"].items()):
			users = set()
			for log in snapshot["logs"]:
				# A retry or another cleanup may already have deleted a log. The
				# trusted lifecycle is the only entry point for cross-user deletion.
				frappe.delete_doc("Notification Log", log.name, ignore_permissions=True,
					ignore_missing=True, delete_permanently=True)
				if log.for_user:
					users.add(log.for_user)
			for user in users:
				frappe.publish_realtime("dekure_expense_notifications_cleared",
					{"expense_claim": self.name}, user=user, after_commit=True)
		self.flags.expense_notification_snapshot = None
		return result


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
