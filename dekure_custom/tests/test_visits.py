from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from dekure_custom.api import (
	_get_user_visit,
	get_today_visits,
	get_visit_history,
	get_visit_history_detail,
)


class TestUserVisitAccess(UnitTestCase):
	@patch("dekure_custom.api.frappe.get_all", return_value=[])
	@patch("dekure_custom.api._current_employee", return_value="EMP-SHIVANI")
	@patch("dekure_custom.api._ensure_visit_doctype")
	def test_planning_remains_limited_to_todays_visits(self, ensure_visit_doctype, current_employee, get_all):
		get_today_visits()

		filters = get_all.call_args.kwargs["filters"]
		self.assertEqual(filters["employee"], "EMP-SHIVANI")
		self.assertIn("visit_date", filters)

	@patch("dekure_custom.api.frappe.get_list", return_value=[])
	@patch("dekure_custom.api._current_user_employees", return_value=["EMP-SHIVANI"])
	@patch("dekure_custom.api._ensure_visit_doctype")
	def test_history_uses_session_user_relationship_without_date_filter(
		self, ensure_visit_doctype, current_user_employees, get_list
	):
		get_visit_history(start=50, page_length=51)

		filters = get_list.call_args.kwargs["filters"]
		self.assertEqual(filters, {"employee": ["in", ["EMP-SHIVANI"]]})
		self.assertNotIn("visit_date", filters)
		self.assertEqual(get_list.call_args.kwargs["start"], 50)
		self.assertEqual(get_list.call_args.kwargs["page_length"], 51)

	def test_history_rejects_an_employee_parameter(self):
		with self.assertRaises(TypeError):
			get_visit_history(employee="EMP-OTHER")

	@patch("dekure_custom.api.frappe.get_list", return_value=[])
	@patch("dekure_custom.api._current_user_employees", return_value=["EMP-RAHUL"])
	@patch("dekure_custom.api._ensure_visit_doctype")
	def test_history_applies_search_status_and_bounded_pagination(
		self, ensure_visit_doctype, current_user_employees, get_list
	):
		get_visit_history(start=-10, page_length=1000, search="Acme", status="Completed")

		kwargs = get_list.call_args.kwargs
		self.assertEqual(
			kwargs["filters"],
			{"employee": ["in", ["EMP-RAHUL"]], "status": "Completed"},
		)
		self.assertTrue(all(row[3] == "%Acme%" for row in kwargs["or_filters"]))
		self.assertEqual(kwargs["start"], 0)
		self.assertEqual(kwargs["page_length"], 100)

	@patch("dekure_custom.api._current_user_employees", return_value=["EMP-SHIVANI"])
	@patch("dekure_custom.api.frappe.get_doc")
	def test_user_cannot_open_another_users_visit(self, get_doc, current_user_employees):
		get_doc.return_value = frappe._dict(name="VIS-RAHUL", employee="EMP-RAHUL")

		with self.assertRaises(frappe.PermissionError):
			_get_user_visit("VIS-RAHUL")

	@patch("dekure_custom.api.frappe.get_all")
	@patch("dekure_custom.api._get_user_visit")
	@patch("dekure_custom.api._ensure_visit_doctype")
	def test_history_detail_returns_selected_visit_fields_and_attachments(
		self, ensure_visit_doctype, get_user_visit, get_all
	):
		get_user_visit.return_value = frappe._dict(
			name="VIS-0002",
			employee="EMP-SHIVANI",
			customer_name="Test Company",
			visit_date="2026-08-23",
			status="Completed",
		)
		get_all.return_value = [
			frappe._dict(name="FILE-1", file_name="visit.jpg", file_url="/private/files/visit.jpg")
		]

		details = get_visit_history_detail("VIS-0002")

		get_user_visit.assert_called_once_with("VIS-0002")
		self.assertEqual(details["name"], "VIS-0002")
		self.assertEqual(details["customer_name"], "Test Company")
		self.assertEqual(details["attachments"][0].file_name, "visit.jpg")
