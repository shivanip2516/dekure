from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

from dekure_custom.comp_off import (
	APPROVED,
	DRAFT,
	PENDING_APPROVAL,
	REJECTED,
	SUBMISSION_ERROR,
	prepare_pwa_comp_off,
	prevent_pwa_employee_comp_off_submission,
	request_comp_off_approval,
	review_pwa_comp_off,
)


def _comp_off(status=PENDING_APPROVAL, marker=1, docstatus=0, is_new=False):
	doc = frappe._dict(
		doctype="Compensatory Leave Request",
		name="COMP-1",
		employee="EMP-1",
		employee_name="Employee One",
		custom_pwa_comp_off=marker,
		custom_status=status,
		custom_leave_approver="approver@example.com",
		docstatus=docstatus,
		flags=frappe._dict(),
	)
	doc.is_new = lambda: is_new
	doc.set = lambda fieldname, value: doc.update({fieldname: value})
	return doc


class TestPWACompOffWorkflow(UnitTestCase):
	@patch("dekure_custom.comp_off.get_employee_leave_approver", return_value="approver@example.com")
	@patch("dekure_custom.comp_off._employee_users", return_value=["employee@example.com"])
	def test_employee_save_sets_pending_approval(self, employee_users, get_approver):
		doc = _comp_off(status="Draft", is_new=True)
		with patch.object(frappe.local, "session", frappe._dict(user="employee@example.com")):
			prepare_pwa_comp_off(doc)

		self.assertEqual(doc.custom_status, PENDING_APPROVAL)
		self.assertEqual(doc.docstatus, 0)
		self.assertEqual(doc.custom_leave_approver, "approver@example.com")

	@patch("dekure_custom.comp_off._employee_users", return_value=["employee@example.com"])
	def test_employee_cannot_submit_pwa_comp_off(self, employee_users):
		with patch.object(frappe.local, "session", frappe._dict(user="employee@example.com")):
			with self.assertRaises(frappe.PermissionError):
				prevent_pwa_employee_comp_off_submission(_comp_off(status=APPROVED))

	@patch("dekure_custom.comp_off._employee_users", return_value=["employee@example.com"])
	def test_configured_approver_can_submit_approved_request(self, employee_users):
		doc = _comp_off(status=APPROVED)
		doc.flags.pwa_comp_off_review = True
		with patch.object(frappe.local, "session", frappe._dict(user="approver@example.com")):
			prevent_pwa_employee_comp_off_submission(doc)

	@patch("dekure_custom.comp_off._employee_users", return_value=["employee@example.com"])
	def test_unapproved_request_cannot_be_submitted(self, employee_users):
		with patch.object(frappe.local, "session", frappe._dict(user="approver@example.com")):
			with self.assertRaises(frappe.ValidationError):
				prevent_pwa_employee_comp_off_submission(_comp_off())

	@patch("dekure_custom.comp_off._employee_users", return_value=["employee@example.com"])
	def test_unauthorized_user_cannot_submit_approved_request(self, employee_users):
		with patch.object(frappe.local, "session", frappe._dict(user="other@example.com")):
			with self.assertRaises(frappe.PermissionError):
				prevent_pwa_employee_comp_off_submission(_comp_off(status=APPROVED))

	def test_non_pwa_draft_cannot_be_submitted(self):
		doc = _comp_off(status=DRAFT, marker=0)
		with self.assertRaises(frappe.ValidationError) as error:
			prevent_pwa_employee_comp_off_submission(doc)

		self.assertEqual(str(error.exception), SUBMISSION_ERROR)

	def test_submission_validation_uses_required_message_during_validate(self):
		doc = _comp_off(status=PENDING_APPROVAL, marker=0, docstatus=1)
		with self.assertRaises(frappe.ValidationError) as error:
			prepare_pwa_comp_off(doc)

		self.assertEqual(str(error.exception), SUBMISSION_ERROR)

	@patch("dekure_custom.comp_off.frappe.db.get_value")
	def test_cancellation_of_historical_inconsistent_request_is_not_blocked(self, get_value):
		prepare_pwa_comp_off(_comp_off(status=DRAFT, marker=0, docstatus=2))
		get_value.assert_not_called()

	@patch("dekure_custom.comp_off._employee_users", return_value=["employee@example.com"])
	def test_direct_status_change_is_blocked(self, employee_users):
		doc = _comp_off(status=APPROVED, marker=0)
		with patch("dekure_custom.comp_off.frappe.db.get_value", return_value=(PENDING_APPROVAL, "approver@example.com")):
			with patch.object(frappe.local, "session", frappe._dict(user="approver@example.com")):
				with self.assertRaises(frappe.PermissionError):
					prepare_pwa_comp_off(doc)

	@patch("dekure_custom.comp_off.notify_pwa_comp_off_approver")
	@patch("dekure_custom.comp_off._employee_users", return_value=["employee@example.com"])
	@patch("dekure_custom.comp_off.frappe.get_doc")
	def test_employee_can_send_desk_draft_for_approval(self, get_doc, employee_users, notify):
		doc = _comp_off(status=DRAFT, marker=0)
		doc.check_permission = MagicMock()
		doc.save = MagicMock()
		get_doc.return_value = doc

		with patch.object(frappe.local, "session", frappe._dict(user="employee@example.com")):
			result = request_comp_off_approval(doc.name)

		doc.check_permission.assert_called_once_with("write")
		doc.save.assert_called_once_with()
		notify.assert_called_once_with(doc)
		self.assertEqual(result["status"], PENDING_APPROVAL)

	@patch("dekure_custom.comp_off._is_pwa_comp_off", return_value=True)
	@patch("dekure_custom.comp_off.frappe.get_doc")
	def test_approval_uses_standard_submission(self, get_doc, is_pwa):
		doc = _comp_off()
		doc.submit = MagicMock()
		get_doc.return_value = doc
		with patch.object(frappe.local, "session", frappe._dict(user="approver@example.com")):
			result = review_pwa_comp_off(doc.name, APPROVED)

		doc.submit.assert_called_once_with()
		self.assertEqual(doc.custom_status, APPROVED)
		self.assertEqual(result["status"], APPROVED)

	@patch("dekure_custom.comp_off._is_pwa_comp_off", return_value=True)
	@patch("dekure_custom.comp_off.frappe.get_doc")
	def test_rejection_saves_without_submitting(self, get_doc, is_pwa):
		doc = _comp_off()
		doc.save = MagicMock()
		doc.submit = MagicMock()
		get_doc.return_value = doc
		with patch.object(frappe.local, "session", frappe._dict(user="approver@example.com")):
			result = review_pwa_comp_off(doc.name, REJECTED)

		doc.save.assert_called_once_with()
		doc.submit.assert_not_called()
		self.assertEqual(result["status"], REJECTED)

	@patch("dekure_custom.comp_off._is_pwa_comp_off", return_value=True)
	@patch("dekure_custom.comp_off.frappe.get_doc")
	def test_unauthorized_user_cannot_review(self, get_doc, is_pwa):
		get_doc.return_value = _comp_off()
		with patch.object(frappe.local, "session", frappe._dict(user="other@example.com")):
			with self.assertRaises(frappe.PermissionError):
				review_pwa_comp_off("COMP-1", APPROVED)
