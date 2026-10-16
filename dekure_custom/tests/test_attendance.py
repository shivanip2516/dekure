from datetime import datetime
from unittest.mock import MagicMock, call, patch

import frappe
from frappe.tests import UnitTestCase

from dekure_custom import api, attendance


def _checkin(name="OUT-1", employee="EMP-1", **values):
	doc = frappe._dict(
		name=name,
		employee=employee,
		log_type="OUT",
		custom_checkin_source=attendance.PWA_CHECKIN_SOURCE,
		time=datetime(2026, 10, 15, 18),
		shift="General",
		shift_start=datetime(2026, 10, 15, 9),
		shift_actual_end=datetime(2026, 10, 15, 17, 30),
		offshift=0,
		skip_auto_attendance=0,
		attendance=None,
	)
	doc.update(values)
	doc.reload = MagicMock()
	return doc


def _shift(**values):
	doc = frappe._dict(
		name="General",
		enable_auto_attendance=1,
		process_attendance_after="2026-01-01",
		last_sync_of_checkin=datetime(2026, 10, 15, 18),
	)
	doc.update(values)
	doc.has_incorrect_shift_config = MagicMock(return_value=False)
	doc.should_mark_attendance = MagicMock(return_value=True)
	return doc


class TestPWACheckoutAttendance(UnitTestCase):
	@patch("dekure_custom.attendance.frappe.enqueue")
	def test_only_pwa_out_is_enqueued_after_commit(self, enqueue):
		for index in range(5):
			attendance.enqueue_pwa_checkout_attendance(_checkin(name=f"OUT-{index}"))
		attendance.enqueue_pwa_checkout_attendance(_checkin(log_type="IN"))
		attendance.enqueue_pwa_checkout_attendance(_checkin(custom_checkin_source="Biometric"))

		self.assertEqual(enqueue.call_count, 5)
		self.assertEqual(
			enqueue.call_args_list[0],
			call(
				"dekure_custom.attendance.process_pwa_checkout_attendance",
				queue="short",
				enqueue_after_commit=True,
				job_id="pwa_checkout_attendance_OUT-0",
				deduplicate=True,
				checkin_name="OUT-0",
			),
		)

	def test_checkout_is_eligible_before_actual_shift_cutoff_and_last_sync(self):
		checkin = _checkin(
			time=datetime(2026, 10, 15, 18, 35),
			shift_actual_end=datetime(2026, 10, 15, 23, 45),
		)
		shift = _shift(last_sync_of_checkin=datetime(2026, 10, 15, 18, 30))
		self.assertIsNone(attendance._get_eligibility_message(shift, checkin))

	def test_eligible_after_shift_synchronization(self):
		self.assertIsNone(attendance._get_eligibility_message(_shift(), _checkin()))

	def test_overnight_shift_uses_shift_occurrence_date(self):
		checkin = _checkin(
			time=datetime(2026, 10, 16, 6),
			shift_start=datetime(2026, 10, 15, 22),
			shift_actual_end=datetime(2026, 10, 16, 6),
		)
		shift = _shift(last_sync_of_checkin=datetime(2026, 10, 16, 6, 1))
		self.assertIsNone(attendance._get_eligibility_message(shift, checkin))

	def test_missing_pair_is_not_finalized(self):
		values = ("Present", datetime(2026, 10, 15).date(), 0, False, False, None, None)
		self.assertFalse(attendance._has_valid_in_out_pair(values))

	def test_valid_pair_is_accepted_without_hardcoded_status(self):
		values = (
			"Half Day",
			datetime(2026, 10, 15).date(),
			4,
			True,
			True,
			datetime(2026, 10, 15, 9),
			datetime(2026, 10, 15, 13),
		)
		self.assertTrue(attendance._has_valid_in_out_pair(values))

	@patch(
		"dekure_custom.attendance._get_existing_attendance",
		return_value=frappe._dict(name="ATT-1"),
	)
	@patch("dekure_custom.attendance.frappe.db.sql")
	@patch("dekure_custom.attendance.frappe.get_doc")
	@patch("dekure_custom.attendance._reconcile_early_attendance")
	def test_existing_attendance_uses_controlled_reconciliation(
		self, reconcile, get_doc, db_sql, get_existing
	):
		reconcile.return_value = {"status": attendance.PROCESSING_COMPLETED, "attendance": "ATT-1"}
		get_doc.side_effect = [_checkin(), _shift()]
		result = attendance._process_pwa_checkout_attendance("OUT-1")

		self.assertEqual(result["status"], attendance.PROCESSING_COMPLETED)
		self.assertEqual(result["attendance"], "ATT-1")
		reconcile.assert_called_once()

	@patch("dekure_custom.attendance.frappe.db.sql")
	@patch("dekure_custom.attendance.frappe.get_doc")
	def test_offshift_checkin_is_not_processed(self, get_doc, db_sql):
		checkin = _checkin(offshift=1, shift=None)
		get_doc.return_value = checkin
		result = attendance._process_pwa_checkout_attendance(checkin.name)
		self.assertEqual(result["status"], attendance.PROCESSING_NOT_APPLICABLE)

	@patch("dekure_custom.attendance._get_existing_attendance", return_value=None)
	@patch("dekure_custom.attendance._get_shift_occurrence_logs", return_value=[frappe._dict(name="IN-1")])
	@patch("dekure_custom.attendance.frappe.db.sql")
	@patch("dekure_custom.attendance.frappe.get_doc")
	def test_holiday_configuration_is_respected(self, get_doc, db_sql, get_logs, get_existing):
		shift = _shift()
		shift.should_mark_attendance.return_value = False
		get_doc.side_effect = [_checkin(), shift]
		result = attendance._process_pwa_checkout_attendance("OUT-1")
		self.assertEqual(result["status"], attendance.PROCESSING_NOT_APPLICABLE)

	@patch(
		"dekure_custom.attendance._get_existing_attendance",
		return_value=frappe._dict(name="ATT-SCHEDULER"),
	)
	@patch("hrms.hr.doctype.employee_checkin.employee_checkin.update_attendance_in_checkins")
	@patch(
		"hrms.hr.doctype.employee_checkin.employee_checkin.create_or_update_attendance",
		side_effect=frappe.ValidationError("duplicate"),
	)
	@patch("dekure_custom.attendance.frappe.db.rollback")
	@patch("dekure_custom.attendance.frappe.db.savepoint")
	def test_scheduler_race_does_not_mark_logs_as_skipped(
		self, savepoint, rollback, create_attendance, update_logs, get_existing
	):
		logs = [frappe._dict(name="IN-1", employee="EMP-1", skip_auto_attendance=0)]
		values = (
			"Present",
			datetime(2026, 10, 15).date(),
			8,
			False,
			False,
			datetime(2026, 10, 15, 9),
			datetime(2026, 10, 15, 17),
		)

		result = attendance._create_attendance_and_link_logs(logs, values, _shift())

		self.assertIsNone(result)
		self.assertEqual(logs[0].skip_auto_attendance, 0)
		rollback.assert_called_once_with(save_point="pwa_attendance_creation")
		update_logs.assert_not_called()

	def test_manual_attendance_is_not_reconciled(self):
		manual_attendance = frappe._dict(name="ATT-MANUAL")
		logs = [
			frappe._dict(
				name="OUT-2",
				attendance=None,
				log_type="OUT",
				custom_checkin_source=attendance.PWA_CHECKIN_SOURCE,
			)
		]
		with patch("dekure_custom.attendance._get_shift_occurrence_logs", return_value=logs):
			with self.assertRaisesRegex(frappe.ValidationError, "authorized user"):
				attendance._reconcile_early_attendance(_checkin(), _shift(), manual_attendance)

	@patch("dekure_custom.attendance._update_submitted_attendance")
	@patch(
		"dekure_custom.api._validate_attendance_can_be_overridden",
		side_effect=frappe.ValidationError("submitted Salary Slip period"),
	)
	@patch("dekure_custom.attendance._get_shift_occurrence_logs")
	def test_payroll_protected_attendance_is_not_changed(self, get_logs, validate, update):
		existing = frappe._dict(name="ATT-PAYROLL")
		get_logs.return_value = [
			frappe._dict(
				name="OUT-1",
				attendance="ATT-PAYROLL",
				log_type="OUT",
				custom_checkin_source=attendance.PWA_CHECKIN_SOURCE,
			)
		]

		with self.assertRaisesRegex(frappe.ValidationError, "Salary Slip"):
			attendance._reconcile_early_attendance(_checkin(), _shift(), existing)
		update.assert_not_called()

	@patch("hrms.hr.doctype.employee_checkin.employee_checkin.update_attendance_in_checkins")
	def test_later_checkout_updates_same_attendance_id(self, update_logs):
		doc = MagicMock()
		doc.name = "ATT-1"
		doc.flags = frappe._dict()
		logs = [
			frappe._dict(name="IN-1", attendance="ATT-1", shift="General"),
			frappe._dict(name="OUT-2", attendance=None, shift="General"),
		]
		values = (
			"Present",
			datetime(2026, 10, 15).date(),
			8.5,
			False,
			False,
			datetime(2026, 10, 15, 10),
			datetime(2026, 10, 15, 18, 45),
		)

		attendance._update_submitted_attendance(doc, logs, values)

		self.assertTrue(doc.flags.ignore_validate_update_after_submit)
		doc.save.assert_called_once_with(ignore_permissions=True)
		update_logs.assert_called_once_with(["OUT-2"], "ATT-1")

	@patch("dekure_custom.api.frappe.get_all")
	def test_pwa_sequence_allows_another_pair_but_rejects_duplicate_checkout(self, get_all):
		get_all.return_value = [frappe._dict(name="OUT-1", log_type="OUT")]
		api._validate_pwa_punch_sequence(_checkin(log_type="IN"))

		with self.assertRaisesRegex(frappe.ValidationError, "Check in before checking out"):
			api._validate_pwa_punch_sequence(_checkin(log_type="OUT"))

	@patch("dekure_custom.attendance._process_pwa_checkout_attendance", side_effect=RuntimeError("boom"))
	@patch("dekure_custom.attendance.frappe.log_error")
	def test_failure_is_logged_and_raised_for_retry(self, log_error, process):
		with self.assertRaisesRegex(RuntimeError, "boom"):
			attendance.process_pwa_checkout_attendance("OUT-1")
		log_error.assert_called_once()
