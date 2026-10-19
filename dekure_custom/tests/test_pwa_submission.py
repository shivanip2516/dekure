from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from dekure_custom.pwa_submission import (
	SUBMISSION_ERROR,
	prevent_employee_pwa_submission,
)


def _request_doc(doctype="Leave Application", employee="EMP-1"):
	return frappe._dict(doctype=doctype, employee=employee)


class TestPWASubmissionProtection(UnitTestCase):
	@patch("dekure_custom.pwa_submission.frappe.get_all", return_value=["EMP-1"])
	def test_employee_cannot_submit_own_request_through_pwa_api(self, get_all):
		with patch.object(
			frappe.local,
			"form_dict",
			frappe._dict(cmd="frappe.client.set_value"),
			create=True,
		):
			with self.assertRaises(frappe.PermissionError) as error:
				prevent_employee_pwa_submission(_request_doc())

		self.assertEqual(str(error.exception), SUBMISSION_ERROR)

	@patch("dekure_custom.pwa_submission.frappe.get_all", return_value=["EMP-1"])
	def test_direct_submit_api_is_blocked(self, get_all):
		with patch.object(
			frappe.local,
			"form_dict",
			frappe._dict(cmd="frappe.client.submit"),
			create=True,
		):
			with self.assertRaises(frappe.PermissionError):
				prevent_employee_pwa_submission(_request_doc("Employee Advance"))

	@patch("dekure_custom.pwa_submission.frappe.get_all", return_value=["EMP-APPROVER"])
	def test_approver_can_submit_another_employees_request(self, get_all):
		with patch.object(
			frappe.local,
			"form_dict",
			frappe._dict(cmd="frappe.client.submit"),
			create=True,
		):
			prevent_employee_pwa_submission(_request_doc(employee="EMP-REQUESTER"))

	@patch("dekure_custom.pwa_submission.frappe.get_all", return_value=["EMP-1"])
	def test_desk_submission_is_not_changed(self, get_all):
		with patch.object(
			frappe.local,
			"form_dict",
			frappe._dict(cmd="frappe.desk.form.save.savedocs"),
			create=True,
		):
			prevent_employee_pwa_submission(_request_doc())

	@patch("dekure_custom.pwa_submission.frappe.get_all")
	def test_operational_doctype_is_out_of_scope(self, get_all):
		with patch.object(
			frappe.local,
			"form_dict",
			frappe._dict(cmd="frappe.client.submit"),
			create=True,
		):
			prevent_employee_pwa_submission(_request_doc("Visit"))

		get_all.assert_not_called()
