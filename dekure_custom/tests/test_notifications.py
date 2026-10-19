from unittest.mock import MagicMock, call, patch

import frappe
from frappe.tests import UnitTestCase

from dekure_custom.notifications import (
	classify_pwa_notification,
	get_active_unread_notifications_count,
	resolve_actionable_notifications,
)


def _status_doc(previous_status="Pending Approval", status="Approved"):
	previous = frappe._dict(approval_status=previous_status)
	doc = frappe._dict(
		doctype="Expense Claim",
		name="EXP-1",
		approval_status=status,
	)
	doc.meta = MagicMock()
	doc.meta.has_field.side_effect = lambda fieldname: fieldname == "approval_status"
	doc.get_doc_before_save = lambda: previous
	return doc


class TestActionableNotificationResolution(UnitTestCase):
	def test_opening_notification_only_marks_it_read(self):
		notification = frappe._dict(message="Expense Claim opened", custom_action_type=None)
		classify_pwa_notification(notification)
		self.assertIsNone(notification.custom_action_type)

	def test_approval_notification_is_classified(self):
		notification = frappe._dict(
			message="<b>Employee</b> raised a new <b>Expense Claim</b> for approval",
			custom_action_type=None,
		)
		classify_pwa_notification(notification)
		self.assertEqual(notification.custom_action_type, "Approval")

	@patch("dekure_custom.notifications.frappe.get_all")
	def test_unchanged_status_keeps_notification_active(self, get_all):
		resolve_actionable_notifications(_status_doc("Pending Approval", "Pending Approval"))
		get_all.assert_not_called()

	@patch("hrms.refetch_resource")
	@patch("dekure_custom.notifications.frappe.db.set_value")
	@patch("dekure_custom.notifications.frappe.get_all")
	def test_approval_resolves_only_matching_action(self, get_all, set_value, refetch):
		get_all.return_value = [frappe._dict(name="NOTIF-1", to_user="approver@example.com")]

		resolve_actionable_notifications(_status_doc())

		filters = get_all.call_args.kwargs["filters"]
		self.assertEqual(filters["reference_document_type"], "Expense Claim")
		self.assertEqual(filters["reference_document_name"], "EXP-1")
		self.assertEqual(filters["custom_action_type"], ["in", ["Approval"]])
		self.assertEqual(filters["custom_resolved"], 0)
		set_value.assert_called_once()
		self.assertEqual(
			refetch.call_args_list,
			[
				call("hrms:notifications", "approver@example.com"),
				call("hrms:unread_notifications_count", "approver@example.com"),
			],
		)

	@patch("dekure_custom.notifications.frappe.get_all", return_value=[])
	@patch("dekure_custom.notifications.frappe.db.set_value")
	def test_repeated_resolution_is_idempotent(self, set_value, get_all):
		resolve_actionable_notifications(_status_doc())
		set_value.assert_not_called()

	@patch("dekure_custom.notifications.frappe.db.count", return_value=2)
	def test_bell_count_only_includes_active_unread_notifications(self, count):
		self.assertEqual(get_active_unread_notifications_count(), 2)
		count.assert_called_once_with(
			"PWA Notification",
			{
				"to_user": frappe.session.user,
				"read": 0,
				"custom_resolved": 0,
			},
		)
