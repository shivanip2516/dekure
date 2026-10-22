from datetime import date, datetime
from types import MethodType
from unittest import TestCase
from unittest.mock import MagicMock, call, patch

import frappe

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
		shift_end=datetime(2026, 10, 15, 17),
		shift_actual_end=datetime(2026, 10, 15, 17, 30),
		offshift=0,
		skip_auto_attendance=0,
		attendance=None,
	)
	doc.update(values)
	doc.reload = MagicMock()
	doc.set_onload = MagicMock()
	return doc


def _shift(**values):
	doc = frappe._dict(
		name="General",
		enable_auto_attendance=1,
		process_attendance_after="2026-01-01",
		last_sync_of_checkin=datetime(2026, 10, 15, 18),
		determine_check_in_and_check_out="Strictly based on Log Type in Employee Checkin",
		working_hours_calculation_based_on="First Check-in and Last Check-out",
		working_hours_threshold_for_half_day=6,
		working_hours_threshold_for_absent=3,
		enable_late_entry_marking=1,
		enable_early_exit_marking=1,
		late_entry_grace_period=5,
		early_exit_grace_period=5,
	)
	doc.update(values)
	doc.has_incorrect_shift_config = MagicMock(return_value=False)
	doc.should_mark_attendance = MagicMock(return_value=True)
	doc.is_half_holiday = MagicMock(return_value=False)
	return doc


def _attendance_doc(**values):
	doc = frappe._dict(name="ATT-1", employee="EMP-1", attendance_date=date(2026, 10, 15),
		shift="General", docstatus=0, status="Present", flags=frappe._dict())
	doc.update(values)
	doc.insert = MagicMock(return_value=doc)
	doc.save = MagicMock(return_value=doc)
	doc.submit = MagicMock(side_effect=lambda: doc.update(docstatus=1))
	return doc


class TestPWACheckoutAttendance(TestCase):
	def setUp(self):
		self.db = MagicMock()
		for patcher in (
			patch.object(frappe.local, "db", self.db, create=True),
			patch.object(frappe.local, "session", frappe._dict(user="employee@example.com"), create=True),
			patch.object(frappe, "get_system_settings", return_value=None),
			patch.object(attendance, "_", side_effect=lambda value: value),
			patch.object(api, "_", side_effect=lambda value: value),
			patch.object(frappe, "throw", side_effect=self.throw),
		):
			patcher.start()
			self.addCleanup(patcher.stop)

	@staticmethod
	def throw(message, exc=frappe.ValidationError, **kwargs):
		raise exc(message)

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

	@patch("dekure_custom.attendance._update_draft_attendance")
	@patch(
		"dekure_custom.api._validate_attendance_can_be_overridden",
		side_effect=frappe.ValidationError("submitted Salary Slip period"),
	)
	@patch("dekure_custom.attendance._get_shift_occurrence_logs")
	def test_payroll_protected_attendance_is_not_changed(self, get_logs, validate, update):
		existing = frappe._dict(name="ATT-PAYROLL", docstatus=0)
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
	def test_checkout_saves_and_submits_same_draft_attendance_id(self, update_logs):
		doc = MagicMock()
		doc.name = "ATT-1"
		doc.flags = frappe._dict()
		doc.docstatus = 0
		doc.submit.side_effect = lambda: setattr(doc, "docstatus", 1)
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

		attendance._update_draft_attendance(doc, logs, values, finalize=True)

		self.assertFalse(doc.flags.get("ignore_validate_update_after_submit"))
		doc.save.assert_called_once_with(ignore_permissions=True)
		doc.submit.assert_called_once()
		update_logs.assert_called_once_with(["OUT-2"], "ATT-1")

	@patch("dekure_custom.api.frappe.get_all")
	def test_pwa_sequence_allows_another_pair_but_rejects_duplicate_checkout(self, get_all):
		get_all.return_value.for_update.return_value.run.return_value = [frappe._dict(name="OUT-1", log_type="OUT")]
		api._validate_pwa_punch_sequence(_checkin(log_type="IN"))

		with self.assertRaisesRegex(frappe.ValidationError, "Check in before checking out"):
			api._validate_pwa_punch_sequence(_checkin(log_type="OUT"))

	@patch("dekure_custom.attendance._process_pwa_checkout_attendance", side_effect=RuntimeError("boom"))
	@patch("dekure_custom.attendance.frappe.log_error")
	def test_failure_is_logged_and_raised_for_retry(self, log_error, process):
		with self.assertRaisesRegex(RuntimeError, "boom"):
			attendance.process_pwa_checkout_attendance("OUT-1")
		log_error.assert_called_once()

	@patch("dekure_custom.attendance._notify_employee")
	@patch("dekure_custom.attendance._link_logs")
	@patch("dekure_custom.attendance._get_existing_attendance", return_value=None)
	@patch("dekure_custom.attendance.frappe.new_doc")
	@patch("dekure_custom.attendance.frappe.get_doc")
	def test_first_in_creates_present_draft_at_actual_time(self, get_doc, new_doc, existing, link, notify):
		checkin = _checkin(name="IN-1", log_type="IN", time=datetime(2026, 10, 15, 9, 12))
		get_doc.return_value = _shift()
		doc = _attendance_doc()
		new_doc.return_value = doc
		result = attendance._create_pwa_draft_attendance(checkin)
		self.assertEqual(result["attendance"], "ATT-1")
		self.assertEqual(result["status"], attendance.PROCESSING_DRAFT)
		self.assertEqual(doc.status, "Present")
		self.assertEqual(doc.docstatus, 0)
		self.assertEqual(doc.in_time, checkin.time)
		self.assertIsNone(doc.out_time)
		self.assertEqual(doc.attendance_date, checkin.shift_start.date())
		doc.insert.assert_called_once_with(ignore_permissions=True)
		doc.submit.assert_not_called()
		link.assert_called_once_with([checkin], "ATT-1")

	@patch("dekure_custom.attendance._create_pwa_draft_attendance")
	def test_punch_service_is_synchronous_and_authorized(self, create):
		self.db.get_value.return_value = "employee@example.com"
		create.return_value = attendance._result(attendance.PROCESSING_DRAFT, attendance="ATT-1")
		doc = _checkin(name="IN-1", log_type="IN")
		attendance.process_pwa_punch_attendance(doc)
		create.assert_called_once_with(doc)
		self.db.get_value.return_value = "another.employee@example.com"
		with self.assertRaises(frappe.PermissionError):
			attendance.process_pwa_punch_attendance(doc)
		self.assertEqual(create.call_count, 1)

	@patch("dekure_custom.attendance._process_pwa_checkout_attendance", side_effect=frappe.ValidationError("submit failed"))
	def test_checkout_failure_propagates_to_punch_transaction(self, process):
		self.db.get_value.return_value = "employee@example.com"
		with self.assertRaisesRegex(frappe.ValidationError, "submit failed"):
			attendance.process_pwa_punch_attendance(_checkin())

	@patch("dekure_custom.attendance._create_pwa_draft_attendance")
	def test_other_sources_are_unchanged(self, create):
		self.assertIsNone(attendance.process_pwa_punch_attendance(_checkin(custom_checkin_source="Biometric")))
		create.assert_not_called()

	def test_checkout_submits_before_shift_window_closure_and_sync_in_both_modes(self):
		from hrms.hr.doctype.shift_type.shift_type import ShiftType

		for mode in ("First Check-in and Last Check-out", "Every Valid Check-in and Check-out"):
			with self.subTest(mode=mode):
				shift = _shift(working_hours_calculation_based_on=mode,
					last_sync_of_checkin=datetime(2026, 10, 15, 12))
				shift.get_attendance = MethodType(ShiftType.get_attendance, shift)
				checkout = _checkin(time=datetime(2026, 10, 15, 13))
				logs = [_checkin(name="IN-1", log_type="IN", time=datetime(2026, 10, 15, 9), attendance="ATT-1"), checkout]
				doc = _attendance_doc()
				with patch.object(frappe, "get_doc", side_effect=[checkout, shift]), patch.object(
					attendance, "_get_existing_attendance", return_value=doc), patch.object(
					attendance, "_get_shift_occurrence_logs", return_value=logs), patch.object(
					api, "_validate_attendance_can_be_overridden"), patch.object(attendance, "_link_logs"), patch.object(
					attendance, "_notify_employee"):
					result = attendance._process_pwa_checkout_attendance(checkout.name)
				self.assertEqual(result["status"], attendance.PROCESSING_COMPLETED)
				self.assertEqual(doc.docstatus, 1)
				self.assertEqual(doc.status, "Half Day")
				self.assertEqual(doc.working_hours, 4)
				doc.submit.assert_called_once()

	def test_existing_calculator_determines_final_present_half_day_absent_and_flags(self):
		from hrms.hr.doctype.shift_type.shift_type import ShiftType

		shift = _shift()
		shift.get_attendance = MethodType(ShiftType.get_attendance, shift)
		for out_hour, expected, hours in [(18, "Present", 9), (13, "Half Day", 4), (10, "Absent", 1)]:
			with self.subTest(status=expected):
				doc = _attendance_doc()
				logs = [_checkin(name="IN-1", log_type="IN", time=datetime(2026, 10, 15, 9), attendance="ATT-1"),
					_checkin(time=datetime(2026, 10, 15, out_hour))]
				with patch.object(attendance, "_get_shift_occurrence_logs", return_value=logs), patch.object(
					api, "_validate_attendance_can_be_overridden"), patch.object(attendance, "_link_logs"), patch.object(
					attendance, "_notify_employee"):
					attendance._reconcile_early_attendance(logs[-1], shift, doc)
				self.assertEqual(doc.status, expected)
				self.assertEqual(doc.working_hours, hours)
				self.assertEqual(doc.in_time, logs[0].time)
				self.assertEqual(doc.out_time, logs[-1].time)
				self.assertEqual(doc.early_exit, out_hour < 17)
				self.assertEqual(doc.docstatus, 1)
				doc.save.assert_called_once_with(ignore_permissions=True)
				doc.submit.assert_called_once()

	def test_final_checkout_submits_same_record_and_reuses_multiple_pair_break_calculation(self):
		from hrms.hr.doctype.shift_type.shift_type import ShiftType

		shift = _shift(working_hours_calculation_based_on="Every Valid Check-in and Check-out")
		shift.get_attendance = MethodType(ShiftType.get_attendance, shift)
		doc = _attendance_doc()
		logs = [_checkin(name="IN-1", log_type="IN", time=datetime(2026, 10, 15, 9), attendance="ATT-1"),
			_checkin(name="OUT-1", time=datetime(2026, 10, 15, 12)),
			_checkin(name="IN-2", log_type="IN", time=datetime(2026, 10, 15, 13)),
			_checkin(name="OUT-2", time=datetime(2026, 10, 15, 17))]
		with patch.object(attendance, "_get_shift_occurrence_logs", return_value=logs), patch.object(
			api, "_validate_attendance_can_be_overridden"), patch.object(attendance, "_link_logs"), patch.object(
			attendance, "_notify_employee"):
			result = attendance._reconcile_early_attendance(logs[-1], shift, doc)
		self.assertEqual(result["attendance"], "ATT-1")
		self.assertEqual(result["status"], attendance.PROCESSING_COMPLETED)
		self.assertEqual(doc.docstatus, 1)
		self.assertEqual(doc.working_hours, 7)
		doc.submit.assert_called_once()

	def test_submitted_and_regularized_records_are_protected(self):
		log = _checkin(attendance="ATT-1")
		for values in ({"docstatus": 1}, {"attendance_request": "ATT-REQ-1"}):
			with self.assertRaisesRegex(frappe.ValidationError, "protected"):
				attendance._validate_pwa_draft(_attendance_doc(**values), [log])
		log.custom_checkin_source = None
		with self.assertRaises(frappe.ValidationError):
			attendance._validate_pwa_draft(_attendance_doc(), [log])

	@patch("dekure_custom.attendance.frappe.get_doc")
	def test_duplicate_completed_checkout_does_not_submit_again(self, get_doc):
		get_doc.return_value = _checkin(attendance="ATT-1")
		self.db.get_value.return_value = 1
		with patch.object(attendance, "_reconcile_early_attendance") as reconcile:
			result = attendance._process_pwa_checkout_attendance("OUT-1")
		self.assertEqual(result["status"], attendance.PROCESSING_COMPLETED)
		reconcile.assert_not_called()

	@patch("hrms.api.get_current_employee", return_value="EMP-1")
	@patch("hrms.api.get_attendance_calendar_events", return_value={"2026-10-15": "Holiday", "2026-10-16": "Half Day"})
	@patch("dekure_custom.attendance.frappe.get_all")
	def test_pwa_calendar_includes_owned_drafts_and_preserves_submitted_status(self, get_all, events, employee):
		get_all.side_effect = [["ATT-1", "ATT-2"], [
			frappe._dict(attendance_date=date(2026, 10, 15), status="Present"),
			frappe._dict(attendance_date=date(2026, 10, 16), status="Present")]]
		result = attendance.get_attendance_calendar_events("2026-10-01", "2026-10-31")
		self.assertEqual(result, {"2026-10-15": "Present", "2026-10-16": "Half Day"})
		for call_args in get_all.call_args_list:
			self.assertEqual(call_args.kwargs["filters"]["employee"], "EMP-1")

	def test_scheduler_finalizes_each_owned_draft_once_and_keeps_standard_processing(self):
		class Parent:
			def get_employee_checkins(self):
				return ["standard biometric logs"]

		class Shift(attendance.PWADraftAttendanceShiftMixin, Parent):
			pass

		shift = Shift()
		shift.__dict__.update(_shift())
		draft, submitted = _attendance_doc(), _attendance_doc(name="ATT-DONE", docstatus=1)
		logs = [frappe._dict(name="IN-1", employee="EMP-1", attendance="ATT-1"),
			frappe._dict(name="IN-2", employee="EMP-1", attendance="ATT-1"),
			frappe._dict(name="IN-DONE", employee="EMP-1", attendance="ATT-DONE")]

		def get_doc(doctype, name, **kwargs):
			if doctype == "Attendance":
				return draft if name == "ATT-1" else submitted
			return _checkin(name=name, log_type="IN")

		with patch.object(frappe, "get_all", side_effect=[["ATT-1", "ATT-DONE"], logs]) as get_all, patch.object(
			frappe, "get_doc", side_effect=get_doc), patch.object(attendance, "_reconcile_early_attendance") as reconcile:
			result = shift.get_employee_checkins()
		self.assertEqual(result, ["standard biometric logs"])
		self.assertEqual(reconcile.call_count, 1)
		self.assertTrue(reconcile.call_args.kwargs["shift_closed"])
		self.assertEqual(get_all.call_args.kwargs["filters"]["shift_actual_end"], ["<", shift.last_sync_of_checkin])
		submitted.submit.assert_not_called()

	def test_overnight_in_creates_attendance_on_shift_start_date(self):
		checkin = _checkin(log_type="IN", time=datetime(2026, 10, 16, 0, 15),
			shift_start=datetime(2026, 10, 15, 22), shift_actual_end=datetime(2026, 10, 16, 7))
		doc = _attendance_doc()
		with patch.object(attendance, "_get_existing_attendance", return_value=None), patch.object(
			frappe, "new_doc", return_value=doc), patch.object(frappe, "get_doc", return_value=_shift()), patch.object(
			attendance, "_link_logs"), patch.object(attendance, "_notify_employee"):
			attendance._create_pwa_draft_attendance(checkin)
		self.assertEqual(doc.attendance_date, date(2026, 10, 15))
		self.assertEqual(doc.in_time, datetime(2026, 10, 16, 0, 15))

	def test_repeated_in_keeps_same_draft_and_original_in_time(self):
		doc = _attendance_doc(in_time=datetime(2026, 10, 15, 9), out_time=datetime(2026, 10, 15, 12))
		first_in = _checkin(name="IN-1", log_type="IN", attendance="ATT-1")
		with patch.object(attendance, "_get_existing_attendance", return_value=doc), patch.object(
			attendance, "_get_shift_occurrence_logs", return_value=[first_in]), patch.object(
			api, "_validate_attendance_can_be_overridden"), patch.object(frappe, "new_doc") as new_doc, patch.object(
			frappe, "get_doc", return_value=_shift()), patch.object(attendance, "_link_logs"), patch.object(attendance, "_notify_employee"):
			attendance._create_pwa_draft_attendance(_checkin(name="IN-2", log_type="IN", time=datetime(2026, 10, 15, 13)))
		self.assertEqual(doc.name, "ATT-1")
		self.assertEqual(doc.in_time, datetime(2026, 10, 15, 9))
		self.assertIsNone(doc.out_time)
		new_doc.assert_not_called()
		doc.submit.assert_not_called()

	def test_status_api_distinguishes_present_draft_and_final_half_day(self):
		for docstatus, status, expected in [(0, "Present", attendance.PROCESSING_DRAFT), (1, "Half Day", attendance.PROCESSING_COMPLETED)]:
			self.db.get_value.return_value = _attendance_doc(docstatus=docstatus, status=status)
			result = attendance.get_checkout_attendance_status(_checkin(attendance="ATT-1"))
			self.assertEqual(result["attendance_processing_status"], expected)
			self.assertEqual(result["attendance"].status, status)

	def test_legacy_unlinked_in_is_recovered_at_checkout(self):
		checkin, first_in, draft = _checkin(), _checkin(name="IN-1", log_type="IN"), _attendance_doc()
		with patch.object(frappe, "get_doc", side_effect=[checkin, _shift()]), patch.object(
			attendance, "_get_existing_attendance", side_effect=[None, draft]), patch.object(
			attendance, "_get_shift_occurrence_logs", return_value=[first_in, checkin]), patch.object(
			attendance, "_create_pwa_draft_attendance") as create, patch.object(
			attendance, "_reconcile_early_attendance", return_value={"status": attendance.PROCESSING_COMPLETED}) as reconcile:
			attendance._process_pwa_checkout_attendance(checkin.name)
		create.assert_called_once_with(first_in)
		self.assertIs(reconcile.call_args.args[2], draft)

	def test_manipulated_employee_or_attendance_link_is_rejected_before_processing(self):
		doc = _checkin()
		doc.is_new = lambda: True
		self.db.get_value.return_value = "another.employee@example.com"
		with self.assertRaises(frappe.PermissionError):
			api.validate_checkin(doc, None)
		self.db.get_value.return_value = "employee@example.com"
		doc.attendance, doc.shift = "SOMEONE-ELSES-ATTENDANCE", None
		with self.assertRaisesRegex(frappe.PermissionError, "managed by the server"):
			api.validate_checkin(doc, None)

	def test_rollback_guard_refuses_to_strand_open_pwa_drafts(self):
		with patch.object(frappe, "get_all", return_value=["ATT-1"]):
			self.db.exists.return_value = True
			with self.assertRaisesRegex(frappe.ValidationError, "before rolling back"):
				attendance.ensure_rollback_ready()
			self.db.exists.return_value = False
			self.assertIn("rollback can proceed", attendance.ensure_rollback_ready())

	def test_submission_failure_is_raised_before_linking_checkout(self):
		logs = [_checkin(name="IN-1", log_type="IN", attendance="ATT-1"), _checkin()]
		values = ("Half Day", date(2026, 10, 15), 4, False, True,
			datetime(2026, 10, 15, 9), datetime(2026, 10, 15, 13))
		for fails_silently in (False, True):
			with self.subTest(fails_silently=fails_silently):
				doc = _attendance_doc()
				doc.submit.side_effect = None if fails_silently else frappe.ValidationError("Submission validation failed")
				with patch.object(attendance, "_link_logs") as link:
					with self.assertRaises(frappe.ValidationError):
						attendance._update_draft_attendance(doc, logs, values, finalize=True)
				link.assert_not_called()
				self.assertEqual(doc.docstatus, 0)

	def test_pwa_checkout_response_contains_confirmed_submission_status_and_hours(self):
		checkout = _checkin()
		self.db.get_value.return_value = "employee@example.com"
		confirmed = {"attendance_processing_status": attendance.PROCESSING_COMPLETED,
			"attendance": {"name": "ATT-1", "docstatus": 1, "status": "Half Day", "working_hours": 4,
				"in_time": datetime(2026, 10, 15, 9), "out_time": datetime(2026, 10, 15, 13)}}
		with patch.object(attendance, "_process_pwa_checkout_attendance", return_value={
			"status": attendance.PROCESSING_COMPLETED, "attendance": "ATT-1"}), patch.object(
			attendance, "get_checkout_attendance_status", return_value=confirmed):
			attendance.process_pwa_punch_attendance(checkout)
		checkout.set_onload.assert_called_once_with("pwa_attendance", confirmed)

	def test_checkout_cannot_report_success_with_draft_result_or_unsubmitted_snapshot(self):
		self.db.get_value.return_value = "employee@example.com"
		for processing_status, snapshot_docstatus in [(attendance.PROCESSING_DRAFT, 0), (attendance.PROCESSING_COMPLETED, 0)]:
			checkout = _checkin()
			with patch.object(attendance, "_process_pwa_checkout_attendance", return_value={
				"status": processing_status}), patch.object(attendance, "get_checkout_attendance_status", return_value={
					"attendance_processing_status": attendance.PROCESSING_COMPLETED,
					"attendance": {"docstatus": snapshot_docstatus}}):
				with self.assertRaises(frappe.ValidationError):
					attendance.process_pwa_punch_attendance(checkout)
			checkout.set_onload.assert_not_called()

	def test_older_out_cannot_finalize_when_a_later_punch_exists(self):
		checkout = _checkin()
		for log_type in ("IN", "OUT"):
			doc = _attendance_doc()
			logs = [_checkin(name="IN-1", log_type="IN", attendance="ATT-1"), checkout,
				_checkin(name="LATER-PUNCH", log_type=log_type)]
			with patch.object(attendance, "_get_shift_occurrence_logs", return_value=logs):
				result = attendance._reconcile_early_attendance(checkout, _shift(), doc)
			self.assertEqual(result["status"], attendance.PROCESSING_PENDING)
			doc.save.assert_not_called()
			doc.submit.assert_not_called()

	def test_overnight_checkout_submits_original_shift_attendance_before_window_end(self):
		from hrms.hr.doctype.shift_type.shift_type import ShiftType

		start, end = datetime(2026, 10, 15, 22), datetime(2026, 10, 16, 6)
		shift = _shift()
		shift.get_attendance = MethodType(ShiftType.get_attendance, shift)
		checkout = _checkin(time=end, shift_start=start, shift_end=end,
			shift_actual_end=datetime(2026, 10, 16, 6, 30))
		logs = [_checkin(name="IN-1", log_type="IN", time=start, shift_start=start,
			shift_end=end, attendance="ATT-1"), checkout]
		doc = _attendance_doc()
		with patch.object(frappe, "get_doc", side_effect=[checkout, shift]), patch.object(
			attendance, "_get_existing_attendance", return_value=doc) as existing, patch.object(
			attendance, "_get_shift_occurrence_logs", return_value=logs), patch.object(
			api, "_validate_attendance_can_be_overridden"), patch.object(attendance, "_link_logs"), patch.object(
			attendance, "_notify_employee"):
			result = attendance._process_pwa_checkout_attendance(checkout.name)
		existing.assert_called_with("EMP-1", date(2026, 10, 15), "General")
		self.assertEqual(result["status"], attendance.PROCESSING_COMPLETED)
		self.assertEqual((doc.docstatus, doc.status, doc.working_hours), (1, "Present", 8))
		self.assertEqual((doc.attendance_date, doc.in_time, doc.out_time), (date(2026, 10, 15), start, end))
