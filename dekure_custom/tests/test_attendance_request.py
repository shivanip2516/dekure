from datetime import date
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from dekure_custom import attendance as pwa_attendance
from dekure_custom.attendance_request import (
	DekureAttendanceRequest,
	_validate_existing_attendance,
	is_approved_attendance_request_correction,
)


def _request(**values):
	request = DekureAttendanceRequest.__new__(DekureAttendanceRequest)
	request.employee = "EMP-1"
	request.name = "AR-1"
	request.shift = None
	for field, value in values.items():
		setattr(request, field, value)
	return request


def _attendance(status="Half Day", **values):
	doc = MagicMock()
	doc.name = "ATT-1"
	doc.employee = "EMP-1"
	doc.attendance_date = "2026-10-17"
	doc.status = status
	doc.docstatus = 1
	doc.leave_application = None
	doc.leave_type = None
	doc.attendance_request = None
	for field, value in values.items():
		setattr(doc, field, value)
	return doc


class TestAttendanceRequestOverride(TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.local.session = frappe._dict(user="approver@example.com")
		frappe.local.conf = frappe._dict(host_name="http://test.local")
		frappe.local.db = MagicMock()
		frappe.local.flags = frappe._dict(mute_messages=True)
		frappe.local.message_log = []
		frappe.local.user_date_format = "yyyy-mm-dd"

	@patch("dekure_custom.attendance_request._validate_existing_attendance")
	def test_half_day_is_updated_in_place_to_present(self, validate):
		doc = _attendance("Half Day")
		request = _request()
		request.get_attendance_doc = MagicMock(return_value=doc)
		request.get_attendance_status = MagicMock(return_value="Present")

		DekureAttendanceRequest.create_or_update_attendance(request, "2026-10-17")

		doc.db_set.assert_called_once_with(
			{"status": "Present", "attendance_request": "AR-1"}
		)
		doc.add_comment.assert_called_once()

	@patch("dekure_custom.attendance_request._validate_existing_attendance")
	def test_absent_is_updated_in_place_to_present(self, validate):
		doc = _attendance("Absent")
		request = _request()
		request.get_attendance_doc = MagicMock(return_value=doc)
		request.get_attendance_status = MagicMock(return_value="Present")

		DekureAttendanceRequest.create_or_update_attendance(request, "2026-10-17")

		doc.db_set.assert_called_once_with(
			{"status": "Present", "attendance_request": "AR-1"}
		)

	@patch("dekure_custom.attendance_request._validate_existing_attendance")
	def test_present_is_idempotent(self, validate):
		doc = _attendance("Present")
		request = _request()
		request.get_attendance_doc = MagicMock(return_value=doc)
		request.get_attendance_status = MagicMock(return_value="Present")

		DekureAttendanceRequest.create_or_update_attendance(request, "2026-10-17")

		doc.db_set.assert_called_once_with("attendance_request", "AR-1")
		doc.add_comment.assert_called_once()

	@patch("dekure_custom.attendance_request._validate_existing_attendance")
	def test_reprocessing_same_present_request_is_idempotent(self, validate):
		doc = _attendance("Present", attendance_request="AR-1")
		request = _request()
		request.get_attendance_doc = MagicMock(return_value=doc)
		request.get_attendance_status = MagicMock(return_value="Present")

		DekureAttendanceRequest.create_or_update_attendance(request, "2026-10-17")

		doc.db_set.assert_not_called()
		doc.add_comment.assert_not_called()

	@patch(
		"hrms.hr.doctype.attendance_request.attendance_request.AttendanceRequest.create_or_update_attendance"
	)
	def test_missing_attendance_uses_standard_creation(self, standard_create):
		request = _request()
		request.get_attendance_doc = MagicMock(return_value=None)

		DekureAttendanceRequest.create_or_update_attendance(request, "2026-10-17")

		standard_create.assert_called_once_with("2026-10-17")

	def test_multi_date_request_processes_only_requested_dates(self):
		request = _request(from_date=date(2026, 10, 17), to_date=date(2026, 10, 19))
		request.should_mark_attendance = MagicMock(return_value=True)
		request.create_or_update_attendance = MagicMock()

		request.create_attendance_records()

		self.assertEqual(
			[call.args[0] for call in request.create_or_update_attendance.call_args_list],
			[date(2026, 10, 17), date(2026, 10, 18), date(2026, 10, 19)],
		)

	@patch("dekure_custom.attendance_request.frappe.has_permission")
	def test_submit_permission_is_checked_server_side(self, has_permission):
		request = _request()

		request.before_submit()

		has_permission.assert_called_once_with(
			"Attendance Request",
			ptype="submit",
			doc=request,
			user=frappe.session.user,
			throw=True,
		)

	@patch(
		"dekure_custom.attendance_request.frappe.has_permission",
		side_effect=frappe.PermissionError("Not permitted"),
	)
	def test_unauthorized_submit_is_rejected(self, has_permission):
		with self.assertRaises(frappe.PermissionError):
			_request().before_submit()

	@patch("dekure_custom.attendance_request.frappe.get_doc")
	@patch("dekure_custom.attendance_request.frappe.db.sql")
	def test_request_without_shift_finds_existing_shifted_attendance(self, db_sql, get_doc):
		db_sql.return_value = [frappe._dict(name="ATT-SHIFTED")]
		get_doc.return_value = _attendance()

		result = _request().get_attendance_doc("2026-10-17")

		self.assertEqual(result, get_doc.return_value)
		get_doc.assert_called_once_with("Attendance", "ATT-SHIFTED")

	@patch("dekure_custom.attendance_request.frappe.db.sql")
	def test_request_without_shift_rejects_ambiguous_attendance(self, db_sql):
		db_sql.return_value = [frappe._dict(name="ATT-1"), frappe._dict(name="ATT-2")]

		with self.assertRaises(frappe.ValidationError):
			_request().get_attendance_doc("2026-10-17")

	@patch("dekure_custom.attendance_request._has_submitted_salary_slip", return_value=True)
	@patch("dekure_custom.attendance_request.frappe.db.exists", return_value=None)
	def test_submitted_salary_slip_blocks_override(self, db_exists, has_salary_slip):
		with self.assertRaises(frappe.ValidationError):
			_validate_existing_attendance(_attendance())

	@patch("dekure_custom.attendance_request._has_submitted_salary_slip", return_value=False)
	@patch("dekure_custom.attendance_request.frappe.db.exists", return_value="LEAVE-1")
	def test_approved_leave_blocks_override(self, db_exists, has_salary_slip):
		with self.assertRaises(frappe.ValidationError):
			_validate_existing_attendance(_attendance())

	@patch("dekure_custom.attendance_request.frappe.db.exists", return_value="AR-1")
	def test_submitted_request_protects_corrected_attendance(self, db_exists):
		doc = _attendance("Present", attendance_request="AR-1")

		self.assertTrue(is_approved_attendance_request_correction(doc))
		db_exists.assert_called_once_with(
			"Attendance Request", {"name": "AR-1", "docstatus": 1}
		)

	@patch("dekure_custom.attendance._get_shift_occurrence_logs", return_value=[])
	@patch(
		"dekure_custom.attendance_request.is_approved_attendance_request_correction",
		return_value=True,
	)
	def test_pwa_recalculation_leaves_request_correction_unchanged(self, protected, get_logs):
		result = pwa_attendance._reconcile_early_attendance(
			frappe._dict(),
			frappe._dict(),
			frappe._dict(name="ATT-1", attendance_request="AR-1"),
		)

		self.assertEqual(result["status"], pwa_attendance.PROCESSING_EXISTING)
		self.assertEqual(result["attendance"], "ATT-1")
