"""Database-free contract tests; run with python -m unittest (never touch site data)."""

from datetime import date
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from dekure_custom import hooks
from dekure_custom import leave_reservations as reservations

STANDARD_DAY_CALCULATOR = reservations.standard.get_number_of_leave_days
PENDING_READER = reservations._pending_requests
LEDGER_READER = reservations._ledger_days

class ReportBase:
	def execute_module(self, filters):
		return self.result


class BalanceReport(reservations.LeaveBalanceReportMixin, ReportBase):
	pass


class CoreValidation:
	def validate_balance_leaves(self):
		self.total_leave_days = reservations.standard.get_number_of_leave_days(
			self.employee, self.leave_type, self.from_date, self.to_date, self.half_day, self.half_day_date)


class Request(reservations.LeaveReservationMixin, CoreValidation, frappe._dict):
	def has_permission(self, permission):
		return self.can_approve

	def has_permlevel_access_to(self, fieldname, permission_type="read"):
		return self.can_review

	def get_doc_before_save(self):
		return self.previous

	def show_insufficient_balance_message(self, balance):
		if not self.allow_negative:
			raise reservations.standard.InsufficientLeaveBalanceError


class TestLeaveReservations(TestCase):
	def setUp(self):
		self.requests, self.ledger = [], []
		self.holidays = set()
		self.start_patch(patch.object(reservations, "nowdate", return_value="2026-04-01"))
		self.start_patch(patch.object(frappe, "get_system_settings", return_value=None))
		self.allocation = frappe._dict(
			from_date=date(2026, 1, 1), to_date=date(2026, 12, 31),
			total_leaves_allocated=10, unused_leaves=0, new_leaves_allocated=10)
		self.db = MagicMock()
		self.db.get_single_value.return_value = 2
		self.start_patch(patch.object(frappe.local, "db", self.db, create=True))
		self.start_patch(patch.object(reservations.standard, "validate_leave_access"))
		self.start_patch(patch.object(reservations.standard, "get_leave_allocation_records",
			side_effect=lambda *args: {"Casual": self.allocation, "Sick": self.allocation}))
		self.expiry = self.start_patch(patch.object(reservations.standard,
			"get_allocation_expiry_for_cf_leaves", return_value=None))
		self.start_patch(patch.object(reservations.standard, "get_manually_expired_leaves", return_value=0))
		self.start_patch(patch.object(reservations.standard, "is_lwp", side_effect=lambda kind: kind == "LWP"))
		self.start_patch(patch.object(reservations.standard, "get_number_of_leave_days", side_effect=self.days))
		self.start_patch(patch.object(reservations.standard, "get_leaves_for_period", side_effect=self.taken))
		self.start_patch(patch.object(reservations, "_ledger_days",
			side_effect=lambda employee, kind, start, end, lock: self.taken(employee, kind, start, end)))
		self.query = self.start_patch(patch.object(reservations, "_pending_requests", side_effect=self.query_requests))
		self.core_balance = self.start_patch(patch.object(reservations.standard,
			"get_leave_balance_on", return_value=10))

	def start_patch(self, patcher):
		value = patcher.start()
		self.addCleanup(patcher.stop)
		return value

	def request(self, name="REQ-1", start="2026-04-01", end="2026-04-02", **values):
		return Request(name=name, employee="EMP-1", leave_type="Casual", from_date=start,
			to_date=end, half_day=0, half_day_date=None, status="Open", docstatus=0,
			can_approve=True, can_review=False, previous=None, allow_negative=False, **values)

	def days(self, employee, kind, start, end, half_day=0, half_day_date=None, holiday_list=None):
		start, end = reservations.getdate(start), reservations.getdate(end)
		days = (end - start).days + 1
		days -= sum(start <= day <= end for day in self.holidays)
		if half_day and half_day_date and start <= reservations.getdate(half_day_date) <= end:
			days -= 0.5
		return days

	def query_requests(self, filters, lock):
		return [r for r in self.requests if r.docstatus == 0 and r.status in filters["status"][1]
			and r.employee == filters["employee"] and r.leave_type == filters["leave_type"]
			and r.name != (filters["name"][1] if "name" in filters else None)
			and reservations.getdate(r.from_date) <= reservations.getdate(filters["from_date"][1])
			and reservations.getdate(r.to_date) >= reservations.getdate(filters["to_date"][1])]

	def taken(self, employee, kind, start, end):
		return -sum(self.days(employee, kind, max(reservations.getdate(start), reservations.getdate(r.from_date)),
			min(reservations.getdate(end), reservations.getdate(r.to_date)), r.half_day, r.half_day_date)
			for r in self.ledger if r.employee == employee and r.leave_type == kind
			and reservations.getdate(r.from_date) <= reservations.getdate(end)
			and reservations.getdate(r.to_date) >= reservations.getdate(start))

	def balance(self, **kwargs):
		return reservations.effective_balance("EMP-1", "Casual", "2026-04-01",
			consider_all_leaves_in_the_allocation_period=True, **kwargs)

	def test_save_resave_approve_submit_and_existing_ledger(self):
		doc = self.request()
		doc.before_validate()
		doc.validate_balance_leaves()
		self.assertEqual(doc.docstatus, 0)
		self.requests.append(doc)
		self.assertEqual(self.balance(), 8)
		doc.validate_balance_leaves()
		self.assertEqual(self.balance(), 8)
		doc.status = "Approved"
		self.assertEqual(self.balance(), 8)
		doc.validate_balance_leaves()
		doc.docstatus = 1
		self.ledger.append(doc)
		self.assertEqual(self.balance(), 8)
		self.assertEqual(self.balance(), 8)
		self.assertEqual(len(self.ledger), 1)
		self.assertEqual(doc.status, "Approved")
		self.core_balance.assert_not_called()

	def test_reject_twice_and_resave(self):
		doc = self.request()
		self.requests.append(doc)
		self.assertEqual(self.balance(), 8)
		for _ in range(2):
			doc.status = "Rejected"
			doc.validate_balance_leaves()
			self.assertEqual(self.balance(), 10)
		self.assertEqual(doc.docstatus, 0)

	def test_edit_increase_then_decrease(self):
		doc = self.request()
		self.requests.append(doc)
		for end, expected in [("2026-04-03", 7), ("2026-04-01", 9)]:
			doc.to_date = end
			doc.validate_balance_leaves()
			self.assertEqual(self.balance(), expected)

	def test_multiple_requests_types_and_insufficient_balance(self):
		self.requests.extend([self.request(), self.request("REQ-2", "2026-04-10", "2026-04-12")])
		self.assertEqual(self.balance(), 5)
		other = self.request("REQ-3", "2026-05-01", "2026-05-06")
		with self.assertRaises(reservations.standard.InsufficientLeaveBalanceError):
			other.validate_balance_leaves()
		other.allow_negative = True
		other.validate_balance_leaves()
		self.assertEqual(reservations.effective_balance("EMP-1", "Sick", "2026-04-01", None, True), 10)

	def test_delete_and_cancel_release(self):
		doc = self.request()
		self.requests.append(doc)
		doc.status = "Cancelled"
		self.assertEqual(self.balance(), 10)
		doc.status = "Open"
		self.requests.clear()
		self.assertEqual(self.balance(), 10)

	def test_half_days_holidays_and_period_clipping(self):
		doc = self.request()
		doc.half_day, doc.half_day_date = 1, "2026-04-01"
		self.requests.append(doc)
		self.assertEqual(self.balance(), 8.5)
		self.holidays.add(date(2026, 4, 2))
		self.assertEqual(self.balance(), 9.5)
		self.assertEqual(reservations.pending_days("EMP-1", "Casual", "2026-04-02", "2026-04-02"), 0)
		self.assertEqual(reservations.pending_days("EMP-1", "Casual", None, None), 0)

	def test_carry_forward_before_and_after_expiry(self):
		self.allocation.unused_leaves = 4
		self.allocation.new_leaves_allocated = 6
		self.expiry.return_value = date(2026, 6, 30)
		self.requests.append(self.request())
		self.assertEqual(self.balance(), 8)
		self.assertEqual(reservations.effective_balance("EMP-1", "Casual", "2026-07-01", None, True), 6)
		self.requests.append(self.request("REQ-2", "2026-08-01", "2026-08-02"))
		self.assertEqual(self.balance(), 6)
		self.assertEqual(reservations.effective_balance("EMP-1", "Casual", "2026-07-01", None, True), 4)

	def test_expiry_consumption_cap_and_self_exclusion(self):
		self.requests.append(self.request())
		result = reservations.effective_balance("EMP-1", "Casual", "2026-12-31", None, True, True)
		self.assertEqual(result.leave_balance, 8)
		self.assertEqual(result.leave_balance_for_consumption, 1)
		self.assertEqual(self.balance(exclude="REQ-1", lock=True), 10)
		self.assertTrue(self.query.call_args.args[1])

	def test_lwp_and_no_allocation_preserve_core_behavior(self):
		doc = self.request()
		doc.leave_type = "LWP"
		doc.validate_balance_leaves()
		self.assertEqual(reservations.effective_balance("EMP-1", "LWP", "2026-04-01"), 10)

	def test_employee_cannot_approve_reject_or_submit(self):
		def denied(*args, **kwargs):
			raise frappe.PermissionError
		with patch.object(reservations, "_", side_effect=lambda value: value), patch.object(frappe, "throw", side_effect=denied):
			for status, docstatus in [("Approved", 0), ("Rejected", 0), ("Open", 1)]:
				doc = self.request()
				doc.status, doc.docstatus, doc.can_approve = status, docstatus, False
				with self.assertRaises(frappe.PermissionError):
					doc.before_validate()
		self.db.get_value.assert_called_with("Employee", "EMP-1", "name", for_update=True)

	def test_existing_status_field_review_permission_is_preserved(self):
		doc = self.request()
		doc.status, doc.can_approve, doc.can_review = "Approved", False, True
		doc.before_validate()

	def test_api_hooks_and_salary_slip_keep_accounting_metrics(self):
		for method in ("get_leave_balance_on", "get_leave_details", "get_leave_metrics_and_details"):
			self.assertEqual(hooks.override_whitelisted_methods[
				f"hrms.hr.doctype.leave_application.leave_application.{method}"],
				f"dekure_custom.leave_reservations.{method}")
		self.requests.append(self.request())
		def details(*args):
			return {"leave_allocation": {"Casual": {"total_leaves": 10,
				"remaining_leaves": 10, "leaves_taken": 0, "expired_leaves": 0}}}
		with patch.object(reservations.standard, "get_leave_details", side_effect=details):
			allocation = reservations.get_leave_details("EMP-1", "2026-04-01")["leave_allocation"]["Casual"]
			self.assertEqual(allocation["remaining_leaves"], 8)
			self.assertEqual(allocation["leaves_taken"], 0)
			self.assertEqual(allocation["expired_leaves"], 0)
			self.assertEqual(allocation["leaves_reserved"], 2)
			self.assertEqual(reservations.get_leave_details("EMP-1", "2026-04-01", True)["leave_allocation"]["Casual"]["remaining_leaves"], 10)

	def test_pending_query_uses_current_reads_only_for_validation(self):
		query = MagicMock()
		query.for_update.return_value = query
		with patch.object(frappe, "get_all", return_value=query):
			PENDING_READER({"docstatus": 0}, False)
			query.for_update.assert_not_called()
			PENDING_READER({"docstatus": 0}, True)
			query.for_update.assert_called_once()
			query.run.assert_called_with(as_dict=True)

	def test_current_ledger_read_at_submission_handover(self):
		query = MagicMock()
		query.for_update.return_value = query
		query.run.return_value = [frappe._dict(
			from_date=date(2026, 4, 1), to_date=date(2026, 4, 2), leaves=-2,
			transaction_type="Leave Application", transaction_name="REQ-1", holiday_list="Test")]
		with patch.object(frappe, "get_all", return_value=query):
			self.assertEqual(LEDGER_READER("EMP-1", "Casual", date(2026, 1, 1), date(2026, 12, 31), True), -2)
			query.for_update.assert_called_once()

	def test_pwa_map_uses_effective_balance(self):
		self.requests.append(self.request())
		with patch("hrms.api.get_current_employee", return_value="EMP-1"), patch.object(
			reservations, "getdate", return_value=date(2026, 4, 1)), patch.object(
			reservations, "get_leave_details", return_value={"leave_allocation": {
				"Casual": {"total_leaves": 10, "remaining_leaves": 8,
					"leaves_taken": 2, "leaves_pending_approval": 0, "expired_leaves": 0}}}):
			self.assertEqual(reservations.get_leave_balance_map(), {
				"Casual": {"allocated_leaves": 10, "balance_leaves": 8,
					"leaves_taken": 2, "leaves_pending_approval": 0, "expired_leaves": 0}})

	def test_balance_reports_and_unrelated_report_passthrough(self):
		report = BalanceReport()
		report.name = "Employee Leave Balance"
		row = frappe._dict(employee="EMP-1", leave_type="Casual", closing_balance=10)
		report.result = ([], [row], None, {})
		self.requests.append(self.request())
		with patch.object(reservations, "_", side_effect=lambda value: value), patch(
			"hrms.hr.report.employee_leave_balance.employee_leave_balance.get_chart_data", return_value={}):
			result = report.execute_module({"to_date": "2026-04-30"})
		self.assertEqual(result[1][0].closing_balance, 8)
		self.assertEqual(result[1][0].leaves_reserved, 2)
		self.assertEqual(result[0][-1]["fieldname"], "leaves_reserved")
		report.name = "Unrelated Report"
		self.assertIs(report.execute_module({}), report.result)

	def test_balance_summary_uses_effective_balances(self):
		report = BalanceReport()
		report.name = "Employee Leave Balance Summary"
		report.result = ([], [["EMP-1", "Employee", "Department", 10]])
		with patch.object(frappe, "get_all", return_value=["Casual"]), patch.object(
			reservations, "get_leave_details", return_value={"leave_allocation": {
				"Casual": {"remaining_leaves": 8}}}):
			self.assertEqual(report.execute_module({"date": "2026-04-01"})[1][0][3], 8)

	def test_standard_date_overlap_policy_notifications_and_ledger_methods_are_inherited(self):
		from hrms.hr.doctype.leave_application.leave_application import LeaveApplication

		controller = type("ReservedLeaveApplication", (reservations.LeaveReservationMixin, LeaveApplication), {})
		for method in ("validate_dates", "validate_leave_overlap", "validate_max_days",
			"validate_applicable_after", "after_insert", "on_update", "on_submit", "on_cancel",
			"create_leave_ledger_entry", "publish_update"):
			self.assertIs(getattr(controller, method), getattr(LeaveApplication, method))

	def test_standard_holiday_and_half_day_calculation(self):
		# Run HRMS's actual day calculator with only access/calendar lookup mocked.
		self.start_patch(patch.object(reservations.standard, "is_holiday", return_value=False))
		with patch.object(reservations.standard, "get_holidays", return_value=2):
			self.db.get_value.return_value = 0  # exclude holidays
			self.assertEqual(STANDARD_DAY_CALCULATOR("EMP-1", "Casual", date(2026, 4, 1), date(2026, 4, 7), 1, date(2026, 4, 1)), 4.5)

	def ledger_summary(self, employee, date, for_salary_slip=False):
		"""Standard ledger-only response; draft classification is tested separately."""
		allocations = {}
		for kind, record in reservations.standard.get_leave_allocation_records(employee, date).items():
			used = -self.taken(employee, kind, record.from_date, record.to_date)
			allocations[kind] = {"total_leaves": record.total_leaves_allocated,
				"remaining_leaves": record.total_leaves_allocated - used,
				"leaves_taken": used, "expired_leaves": 0}
		return {"leave_allocation": allocations}

	def summary(self, kind="Casual"):
		with patch.object(reservations.standard, "get_leave_details", side_effect=self.ledger_summary):
			return reservations.get_leave_details("EMP-1", "2026-04-01")["leave_allocation"][kind]

	def assert_summary(self, used, pending, available, kind="Casual"):
		row = self.summary(kind)
		self.assertEqual((row["leaves_taken"], row["leaves_pending_approval"], row["remaining_leaves"]),
			(used, pending, available))
		return row

	def test_summary_pending_approved_saved_and_submitted_without_double_counting(self):
		self.allocation.total_leaves_allocated = self.allocation.new_leaves_allocated = 11
		historical = self.request(name="HISTORICAL", start="2026-06-01", end="2026-06-02")
		historical.status, historical.docstatus = "Approved", 1
		historical.half_day, historical.half_day_date = 1, "2026-06-01"
		original = dict(historical)
		self.ledger.append(historical)
		self.assert_summary(1.5, 0, 9.5)
		doc = self.request()
		self.requests.append(doc)
		for _ in range(2):
			doc.validate_balance_leaves()
			self.assert_summary(1.5, 2, 7.5)
		doc.status = "Approved"
		for _ in range(2):
			doc.before_validate()
			doc.validate_balance_leaves()
			self.assert_summary(3.5, 0, 7.5)
			self.assertEqual(doc.docstatus, 0)
		# Normal submission commits the application and its standard ledger usage
		# together. The same application is no longer an eligible draft.
		doc.docstatus = 1
		self.ledger.append(doc)
		self.assert_summary(3.5, 0, 7.5)
		self.assert_summary(3.5, 0, 7.5)
		self.assertEqual(dict(historical), original)
		self.db.set_value.assert_not_called()
		self.db.sql.assert_not_called()

	def test_summary_rejected_resaved_and_reopened(self):
		doc = self.request()
		self.requests.append(doc)
		self.assert_summary(0, 2, 8)
		for _ in range(2):
			doc.status = "Rejected"
			self.assert_summary(0, 0, 10)
		doc.status = "Open"
		self.assert_summary(0, 2, 8)

	def test_summary_approved_draft_can_be_rejected_without_retaining_usage(self):
		doc = self.request()
		doc.status = "Approved"
		self.requests.append(doc)
		self.assert_summary(2, 0, 8)
		doc.status = "Rejected"
		self.assert_summary(0, 0, 10)

	def test_summary_multiple_pending_and_approved_drafts(self):
		self.requests.extend([self.request(), self.request(name="APPROVED", start="2026-04-10", end="2026-04-12")])
		self.requests[1].status = "Approved"
		self.assert_summary(3, 2, 5)
		self.requests[0].status = "Approved"
		self.assert_summary(5, 0, 5)

	def test_summary_approved_edits_and_leave_type_change(self):
		doc = self.request()
		doc.status = "Approved"
		self.requests.append(doc)
		for end, expected_used, available in [("2026-04-03", 3, 7), ("2026-04-01", 1, 9)]:
			doc.to_date = end
			self.assert_summary(expected_used, 0, available)
		doc.leave_type = "Sick"
		self.assert_summary(0, 0, 10, "Casual")
		self.assert_summary(1, 0, 9, "Sick")

	def test_summary_half_day_holidays_and_allocation_period_clipping(self):
		doc = self.request(end="2026-04-03")
		doc.status, doc.half_day, doc.half_day_date = "Approved", 1, "2026-04-01"
		self.holidays.add(date(2026, 4, 2))
		self.requests.append(doc)
		self.assert_summary(1.5, 0, 8.5)
		self.holidays.clear()
		self.allocation.from_date = date(2026, 4, 1)
		doc.from_date, doc.to_date, doc.half_day_date = "2026-03-31", "2026-04-02", "2026-03-31"
		self.assert_summary(2, 0, 8)

	def test_summary_cancelled_and_deleted_approved_drafts_release_usage(self):
		doc = self.request()
		doc.status = "Approved"
		self.requests.append(doc)
		self.assert_summary(2, 0, 8)
		doc.docstatus = 2
		self.assert_summary(0, 0, 10)
		self.requests.clear()
		self.assert_summary(0, 0, 10)

	def test_summary_carry_forward_usage_is_not_also_counted_as_expired(self):
		self.allocation.unused_leaves, self.allocation.new_leaves_allocated = 4, 6
		self.expiry.return_value = date(2026, 6, 30)
		doc = self.request()
		self.requests.append(doc)

		def standard_details(*args):
			return {"leave_allocation": {"Casual": {"total_leaves": 10,
				"remaining_leaves": 6, "leaves_taken": 0, "expired_leaves": 4}}}

		with patch.object(reservations.standard, "get_leave_details", side_effect=standard_details):
			for status, used, pending, expired in [("Open", 0, 2, 2), ("Approved", 2, 0, 2), ("Rejected", 0, 0, 4)]:
				doc.status = status
				row = reservations.get_leave_details("EMP-1", "2026-07-01")["leave_allocation"]["Casual"]
				self.assertEqual((row["leaves_taken"], row["leaves_pending_approval"], row["expired_leaves"], row["remaining_leaves"]),
					(used, pending, expired, 6))

	def test_pwa_summary_matches_dashboard_across_status_and_submission(self):
		doc = self.request()
		self.requests.append(doc)
		with patch("hrms.api.get_current_employee", return_value="EMP-1"), patch.object(
			reservations.standard, "get_leave_details", side_effect=self.ledger_summary), patch(
			"frappe.utils.data.get_system_timezone", return_value="Asia/Kolkata"):
			for status, docstatus in [("Open", 0), ("Approved", 0), ("Rejected", 0), ("Approved", 1)]:
				doc.status, doc.docstatus = status, docstatus
				if docstatus == 1:
					self.ledger.append(doc)
				row = reservations.get_leave_details("EMP-1", "2026-04-01")["leave_allocation"]["Casual"]
				pwa = reservations.get_leave_balance_map()["Casual"]
				for key in ("leaves_taken", "leaves_pending_approval", "expired_leaves"):
					self.assertEqual(pwa[key], row[key])
				self.assertEqual(pwa["balance_leaves"], row["remaining_leaves"])
