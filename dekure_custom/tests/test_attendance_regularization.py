from datetime import date, datetime, time
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from dekure_custom import api


def _checkin(**values):
	doc = frappe._dict(
		name="OUT-1",
		employee="EMP-1",
		log_type="OUT",
		time=datetime(2026, 10, 24, 18, 30),
		shift="General",
		shift_start=datetime(2026, 10, 24, 9),
		offshift=0,
		attendance=None,
	)
	doc.update(values)
	return doc


def _attendance(**values):
	doc = frappe._dict(
		name="ATT-1",
		employee="EMP-1",
		attendance_date=date(2026, 10, 24),
		shift="General",
		docstatus=1,
		status="Present",
		attendance_request="AR-1",
		leave_application=None,
		leave_type=None,
	)
	doc.update(values)
	return doc


def _shift():
	return frappe._dict(
		name="General",
		enable_auto_attendance=1,
		should_mark_attendance=MagicMock(return_value=True),
	)


def _regularization(**values):
	doc = MagicMock()
	doc.name = "ARR-OLD"
	doc.employee = "EMP-1"
	doc.attendance_date = date(2026, 10, 24)
	doc.request_type = api.MISSED_CHECKOUT
	doc.requested_time = time(18, 30)
	doc.attendance_approver = "approver@example.com"
	doc.status = api.PENDING
	doc.latitude = 18.5204
	doc.longitude = 73.8567
	doc.has_permission.return_value = True
	for field, value in values.items():
		setattr(doc, field, value)
	return doc


class TestLegacyAttendanceRegularization(TestCase):
	def setUp(self):
		self.db = MagicMock()
		for patcher in (
			patch.object(frappe.local, "db", self.db, create=True),
			patch.object(frappe.local, "session", frappe._dict(user="approver@example.com"), create=True),
			patch.object(api, "_", side_effect=lambda value: value),
			patch.object(api, "now_datetime", return_value=datetime(2026, 10, 29, 12)),
			patch.object(frappe, "throw", side_effect=self.throw),
		):
			patcher.start()
			self.addCleanup(patcher.stop)

	@staticmethod
	def throw(message, exc=frappe.ValidationError, **kwargs):
		raise exc(message)

	@patch("dekure_custom.api._calculate_attendance_from_shift_logs")
	@patch("dekure_custom.api._is_approved_attendance_request_correction", return_value=True)
	@patch("dekure_custom.api._get_regularization_shift_logs")
	@patch("dekure_custom.api._get_existing_attendance")
	@patch("dekure_custom.api.frappe.get_doc")
	def test_protected_attendance_is_preserved_and_logs_are_linked(
		self, get_doc, get_existing, get_logs, protected, calculate
	):
		attendance = _attendance()
		logs = [
			frappe._dict(name="IN-1", attendance="ATT-1", shift="General", offshift=0),
			frappe._dict(name="OUT-1", attendance=None, shift="General", offshift=0),
		]
		get_doc.return_value = _shift()
		get_existing.return_value = attendance
		get_logs.return_value = logs

		api._reconcile_regularized_attendance(_checkin())

		calculate.assert_not_called()
		self.db.set_value.assert_called_once_with(
			"Employee Checkin",
			{"name": ["in", ["OUT-1"]]},
			"attendance",
			"ATT-1",
			update_modified=True,
		)
		self.assertEqual(logs[1].attendance, "ATT-1")
		self.assertEqual((attendance.status, attendance.attendance_request), ("Present", "AR-1"))

	def test_linking_existing_out_is_idempotent(self):
		attendance = _attendance()
		logs = [frappe._dict(name="OUT-1", attendance=None)]

		api._link_logs_to_existing_attendance(attendance, logs)
		api._link_logs_to_existing_attendance(attendance, logs)

		self.db.set_value.assert_called_once()
		self.assertEqual(logs[0].attendance, attendance.name)

	@patch("dekure_custom.api._override_existing_attendance")
	@patch("dekure_custom.api._validate_attendance_can_be_overridden")
	@patch("dekure_custom.api._calculate_attendance_from_shift_logs", return_value=("Present",))
	@patch("dekure_custom.api._is_approved_attendance_request_correction", return_value=False)
	@patch("dekure_custom.api._get_regularization_shift_logs")
	@patch("dekure_custom.api._get_existing_attendance")
	@patch("dekure_custom.api.frappe.get_doc")
	def test_unprotected_attendance_keeps_existing_recalculation(
		self, get_doc, get_existing, get_logs, protected, calculate, validate, override
	):
		attendance = _attendance(attendance_request=None)
		logs = [frappe._dict(name="IN-1", attendance="ATT-1", shift="General", offshift=0)]
		get_doc.return_value = _shift()
		get_existing.return_value = attendance
		get_logs.return_value = logs

		api._reconcile_regularized_attendance(_checkin())

		calculate.assert_called_once()
		validate.assert_called_once_with(attendance, logs)
		override.assert_called_once_with(attendance, logs, ("Present",))

	@patch("dekure_custom.auto_half_day_leave.apply_auto_half_day_adjustment")
	@patch("hrms.hr.doctype.employee_checkin.employee_checkin.mark_attendance_and_link_log")
	@patch("dekure_custom.api._calculate_attendance_from_shift_logs")
	@patch("dekure_custom.api._get_regularization_shift_logs")
	@patch("dekure_custom.api._get_existing_attendance", return_value=None)
	@patch("dekure_custom.api.frappe.get_doc")
	def test_new_regularized_half_day_gets_one_automatic_adjustment(
		self, get_doc, get_existing, get_logs, calculate, mark, apply_adjustment
	):
		logs = [frappe._dict(
			name="IN-1", employee="EMP-1", attendance=None, shift="General", offshift=0,
			overtime_type=None,
		)]
		attendance = _attendance(status="Half Day", attendance_request=None)
		get_doc.return_value = _shift()
		get_logs.return_value = logs
		calculate.return_value = (
			"Half Day", date(2026, 10, 24), 4.0, False, True,
			datetime(2026, 10, 24, 9), datetime(2026, 10, 24, 13),
		)
		mark.return_value = attendance

		api._reconcile_regularized_attendance(_checkin())

		apply_adjustment.assert_called_once_with(attendance)

	@patch("dekure_custom.auto_half_day_leave.reconcile_auto_half_day_adjustment")
	@patch("dekure_custom.api._link_logs_to_existing_attendance")
	@patch("dekure_custom.api._attendance_already_matches", return_value=False)
	def test_regularization_submits_eligible_draft_attendance(
		self, already_matches, link_logs, reconcile
	):
		attendance = _attendance(docstatus=0, attendance_request=None)
		attendance.flags = frappe._dict()
		attendance.save = MagicMock()
		attendance.submit = MagicMock(side_effect=lambda: attendance.update(docstatus=1))
		logs = [frappe._dict(name="IN-1", attendance="ATT-1", shift="General")]
		values = (
			"Half Day",
			date(2026, 10, 24),
			4.0,
			False,
			True,
			datetime(2026, 10, 24, 9),
			datetime(2026, 10, 24, 13),
		)

		api._override_existing_attendance(attendance, logs, values)

		attendance.save.assert_called_once_with(ignore_permissions=True)
		attendance.submit.assert_called_once_with()
		self.assertEqual(attendance.docstatus, 1)
		reconcile.assert_called_once_with(attendance, "Half Day")

	@patch("dekure_custom.api._is_approved_attendance_request_correction", return_value=True)
	def test_general_override_protection_remains_effective(self, protected):
		with self.assertRaisesRegex(frappe.ValidationError, "protected by an approved Attendance Request"):
			api._validate_attendance_can_be_overridden(_attendance(), [])

	@patch("dekure_custom.api._reconcile_regularized_attendance")
	@patch("dekure_custom.api._get_equivalent_checkin")
	@patch("dekure_custom.api._validate_requested_punch_time")
	@patch("dekure_custom.api._lock_employee")
	@patch("dekure_custom.api.frappe.get_doc")
	def test_existing_out_is_reused_and_pending_request_is_approved(
		self, get_doc, lock, validate_time, get_equivalent, reconcile
	):
		request = _regularization()
		out = _checkin()
		get_doc.return_value = request
		get_equivalent.return_value = out

		result = api.review_missed_punch_request(request.name, "Approve")

		get_equivalent.assert_called_once_with("EMP-1", out.time, "OUT")
		reconcile.assert_called_once_with(out)
		request.db_set.assert_called_once()
		self.assertEqual(request.db_set.call_args.args[0]["status"], "Approved")
		self.assertEqual(result, {"ok": True, "status": request.status})

	@patch("dekure_custom.api._reconcile_regularized_attendance", side_effect=frappe.ValidationError("unsafe"))
	@patch("dekure_custom.api._get_equivalent_checkin")
	@patch("dekure_custom.api._validate_coordinates", return_value=(18.5204, 73.8567))
	@patch("dekure_custom.api._validate_checkin_has_shift")
	@patch("dekure_custom.api._validate_missed_punch")
	@patch("dekure_custom.api._validate_requested_punch_time")
	@patch("dekure_custom.api._lock_employee")
	@patch("dekure_custom.api.frappe.get_doc")
	def test_failed_reconciliation_does_not_mark_request_approved(
		self, get_doc, lock, validate_time, validate_missed, validate_shift,
		validate_coordinates, get_equivalent, reconcile
	):
		request = _regularization()
		out = _checkin()
		out.flags = frappe._dict()
		out.insert = MagicMock()
		get_doc.side_effect = [request, out]
		get_equivalent.return_value = None

		with self.assertRaisesRegex(frappe.ValidationError, "unsafe"):
			api.review_missed_punch_request(request.name, "Approve")

		out.insert.assert_called_once_with(ignore_permissions=True)
		request.db_set.assert_not_called()

	@patch("dekure_custom.api.frappe.get_doc")
	def test_unauthorized_approver_is_rejected_before_writes(self, get_doc):
		request = _regularization(attendance_approver="other@example.com")
		get_doc.return_value = request

		with self.assertRaises(frappe.PermissionError):
			api.review_missed_punch_request(request.name, "Approve")

		request.db_set.assert_not_called()

	@patch("dekure_custom.api._lock_employee")
	@patch("dekure_custom.api.frappe.get_doc")
	def test_missing_legacy_requested_time_fails_before_checkin_creation(self, get_doc, lock):
		request = _regularization(requested_time=None)
		get_doc.return_value = request

		with self.assertRaisesRegex(frappe.ValidationError, "No requested punch time"):
			api.review_missed_punch_request(request.name, "Approve")

		request.db_set.assert_not_called()

	def test_missing_legacy_location_is_rejected(self):
		with self.assertRaisesRegex(frappe.ValidationError, "valid current location"):
			api._validate_coordinates(None, None)
