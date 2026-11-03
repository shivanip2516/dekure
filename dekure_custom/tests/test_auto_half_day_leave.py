from datetime import date
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from dekure_custom import auto_half_day_leave as automatic


def _attendance(**values):
	doc = frappe._dict(
		name="ATT-1",
		employee="EMP-1",
		company="Test Company",
		attendance_date=date(2026, 10, 17),
		docstatus=1,
		status="Half Day",
		leave_application=None,
		leave_type=None,
		custom_auto_half_day_leave_application=None,
	)
	doc.update(values)
	doc.db_set = MagicMock()
	doc.add_comment = MagicMock()
	return doc


def _leave(**values):
	doc = frappe._dict(
		name="AUTO-LEAVE-1",
		leave_type="Privilege Leave",
		docstatus=1,
		status="Approved",
		flags=frappe._dict(),
	)
	doc.update(values)
	doc.insert = MagicMock()
	doc.submit = MagicMock()
	doc.cancel = MagicMock()
	doc.db_set = MagicMock()
	doc.add_comment = MagicMock()
	return doc


class TestAutomaticHalfDayLeave(TestCase):
	def setUp(self):
		self.db = MagicMock()
		self.db.get_value.side_effect = lambda doctype, name, field, **kwargs: (
			"Test Company" if field == "company" else name
		)
		for patcher in (
			patch.object(frappe.local, "db", self.db, create=True),
			patch.object(frappe.local, "conf", frappe._dict(host_name="http://test.local"), create=True),
		):
			patcher.start()
			self.addCleanup(patcher.stop)

	@patch("hrms.hr.doctype.leave_application.leave_application.get_employee_leave_approver", return_value="approver@example.com")
	@patch("dekure_custom.auto_half_day_leave._get_automatic_leave_type", return_value="Privilege Leave")
	@patch("dekure_custom.auto_half_day_leave.get_active_auto_adjustment", return_value=None)
	@patch("dekure_custom.auto_half_day_leave.frappe.new_doc")
	def test_half_day_creates_one_submitted_standard_leave_application(
		self, new_doc, existing, leave_type, approver
	):
		attendance = _attendance()
		leave = _leave()
		new_doc.return_value = leave

		result = automatic.apply_auto_half_day_adjustment(attendance)

		self.assertIs(result, leave)
		self.assertEqual(leave.employee, "EMP-1")
		self.assertEqual(leave.leave_type, "Privilege Leave")
		self.assertEqual(leave.half_day, 1)
		self.assertEqual(leave.half_day_date, attendance.attendance_date)
		self.assertEqual(leave.status, "Approved")
		self.assertEqual(leave.custom_is_auto_half_day_adjustment, 1)
		self.assertEqual(leave.custom_auto_half_day_attendance, "ATT-1")
		leave.insert.assert_called_once_with(ignore_permissions=True)
		leave.submit.assert_called_once_with()
		attendance.db_set.assert_called_once_with(
			{
				"custom_auto_half_day_leave_application": "AUTO-LEAVE-1",
				"leave_application": "AUTO-LEAVE-1",
				"leave_type": "Privilege Leave",
			},
			update_modified=True,
		)

	@patch("dekure_custom.auto_half_day_leave._link_adjustment")
	@patch("dekure_custom.auto_half_day_leave.get_active_auto_adjustment")
	def test_repeated_processing_reuses_existing_adjustment(self, existing, link):
		attendance = _attendance()
		leave = _leave()
		existing.return_value = leave

		self.assertIs(automatic.apply_auto_half_day_adjustment(attendance), leave)
		link.assert_called_once_with(attendance, leave)

	@patch("dekure_custom.auto_half_day_leave.apply_auto_half_day_adjustment")
	def test_submit_hook_applies_only_to_working_hours_calculated_half_day(self, apply_adjustment):
		attendance = _attendance(
			shift="General",
			in_time="2026-10-17 09:00:00",
			out_time="2026-10-17 13:00:00",
			working_hours=4,
			attendance_request=None,
		)

		automatic.apply_calculated_half_day_on_submit(attendance)

		apply_adjustment.assert_called_once_with(attendance)

	@patch("dekure_custom.auto_half_day_leave.apply_auto_half_day_adjustment")
	def test_submit_hook_ignores_attendance_request_and_manual_leave_half_days(self, apply_adjustment):
		for values in (
			{"attendance_request": "AR-1"},
			{"leave_application": "LEAVE-1"},
			{"status": "Present"},
		):
			attendance = _attendance(
				shift="General",
				in_time="2026-10-17 09:00:00",
				out_time="2026-10-17 13:00:00",
				working_hours=4,
				attendance_request=None,
			)
			attendance.update(values)
			automatic.apply_calculated_half_day_on_submit(attendance)

		apply_adjustment.assert_not_called()

	@patch("dekure_custom.leave_reservations.effective_balance")
	def test_exact_half_day_privilege_balance_uses_privilege_leave(self, balance):
		self.db.get_value.side_effect = None
		self.db.get_value.return_value = frappe._dict(is_lwp=0, is_ppl=0)
		balance.return_value = frappe._dict(leave_balance_for_consumption=0.5)

		result = automatic._get_automatic_leave_type("EMP-1", date(2026, 10, 17))

		self.assertEqual(result, "Privilege Leave")

	@patch("dekure_custom.leave_reservations.effective_balance")
	@patch("dekure_custom.auto_half_day_leave.frappe.get_all", return_value=["Leave Without Pay"])
	def test_insufficient_privilege_balance_uses_lwp(self, get_all, balance):
		self.db.get_value.side_effect = None
		self.db.get_value.return_value = frappe._dict(is_lwp=0, is_ppl=0)
		balance.return_value = frappe._dict(leave_balance_for_consumption=0.25)

		result = automatic._get_automatic_leave_type("EMP-1", date(2026, 10, 17))

		self.assertEqual(result, "Leave Without Pay")

	@patch("dekure_custom.auto_half_day_leave.get_active_auto_adjustment")
	def test_reversal_cancels_only_the_marked_automatic_application(self, existing):
		attendance = _attendance(
			leave_application="AUTO-LEAVE-1",
			leave_type="Privilege Leave",
			custom_auto_half_day_leave_application="AUTO-LEAVE-1",
		)
		leave = _leave()
		request = frappe._dict(name="AR-1")
		existing.return_value = leave

		result = automatic.reverse_auto_half_day_adjustment(attendance, request)

		self.assertIs(result, leave)
		leave.db_set.assert_called_once_with(
			"custom_reversed_by_attendance_request", "AR-1", update_modified=True
		)
		leave.cancel.assert_called_once_with()
		self.assertTrue(leave.flags.dekure_auto_half_day_adjustment)
		attendance.db_set.assert_called_once_with(
			{
				"leave_application": None,
				"leave_type": None,
				"custom_auto_half_day_leave_application": None,
			},
			update_modified=True,
		)

	@patch("dekure_custom.auto_half_day_leave.get_active_auto_adjustment", return_value=None)
	def test_repeated_reversal_does_not_credit_leave_twice(self, existing):
		self.assertIsNone(
			automatic.reverse_auto_half_day_adjustment(_attendance(), frappe._dict(name="AR-1"))
		)

	@patch("dekure_custom.auto_half_day_leave.reverse_auto_half_day_adjustment")
	@patch("dekure_custom.auto_half_day_leave.has_submitted_salary_slip", return_value=False)
	@patch("dekure_custom.auto_half_day_leave.get_active_auto_adjustment")
	def test_attendance_cancel_reverses_active_automatic_adjustment(
		self, get_adjustment, salary_slip, reverse
	):
		attendance = _attendance()
		get_adjustment.return_value = _leave()

		automatic.reverse_auto_adjustment_before_attendance_cancel(attendance)

		reverse.assert_called_once_with(attendance)


class _ManualLeaveParent:
	def on_cancel(self):
		self.parent_cancelled = True


class _AutomaticLeave(automatic.AutoHalfDayLeaveMixin, _ManualLeaveParent, frappe._dict):
	def create_leave_ledger_entry(self, submit=True):
		self.ledger_submit = submit

	def publish_update(self):
		self.published = True

	def notify_employee(self):
		self.notified = True


class TestAutomaticLeaveCancellationMixin(TestCase):
	def test_automatic_cancel_reverses_ledger_without_cancelling_attendance(self):
		doc = _AutomaticLeave(custom_is_auto_half_day_adjustment=1)
		database = MagicMock()
		database.get_single_value.return_value = 0
		with patch.object(automatic.frappe, "db", database):
			doc.on_cancel()

		self.assertFalse(doc.ledger_submit)
		self.assertTrue(doc.published)
		self.assertNotIn("parent_cancelled", doc)

	def test_manual_leave_uses_standard_cancel_path(self):
		doc = _AutomaticLeave(custom_is_auto_half_day_adjustment=0)

		doc.on_cancel()

		self.assertTrue(doc.parent_cancelled)
