"""Draft applications reserve availability; only HRMS writes accounting ledger entries.

Deliberately scoped to leave UI APIs, Leave Application validation and balance reports.
Internal payroll, allocation expiry and carry-forward accounting remain ledger based.
"""

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, getdate, nowdate

from hrms.hr.doctype.leave_application import leave_application as standard


def pending_days(employee, leave_type, from_date, to_date, exclude=None, lock=False,
	statuses=("Open", "Approved")):
	if not from_date or not to_date or getdate(from_date) > getdate(to_date):
		return 0.0
	filters = {
		"employee": employee,
		"leave_type": leave_type,
		"docstatus": 0,
		"status": ["in", list(statuses)],
		"from_date": ["<=", to_date],
		"to_date": [">=", from_date],
	}
	if exclude:
		filters["name"] = ["!=", exclude]
	requests = _pending_requests(filters, lock)
	return sum(
		standard.get_number_of_leave_days(
			employee, leave_type,
			max(getdate(from_date), getdate(request.from_date)),
			min(getdate(to_date), getdate(request.to_date)),
			request.half_day, request.half_day_date,
		)
		for request in requests
	)


def _pending_requests(filters, lock):
	query = frappe.get_all("Leave Application", filters=filters,
		fields=["from_date", "to_date", "half_day", "half_day_date"], run=False)
	if lock:
		query = query.for_update()
	return query.run(as_dict=True)


def _ledger_days(employee, leave_type, start, finish, lock):
	if not lock:
		return standard.get_leaves_for_period(employee, leave_type, start, finish)
	# Approval can replace a draft with ledger entries while another save waits
	# for the employee lock. Read both sides of that handover as current reads.
	query = frappe.get_all("Leave Ledger Entry", filters={
		"employee": employee, "leave_type": leave_type, "docstatus": 1,
		"from_date": ["<=", finish], "to_date": [">=", start], "leaves": ["<", 0],
	}, fields=["from_date", "to_date", "leaves", "transaction_type", "transaction_name", "holiday_list"],
		run=False)
	days = 0.0
	for entry in query.for_update().run(as_dict=True):
		if entry.transaction_type == "Leave Encashment":
			if entry.from_date >= getdate(start) and entry.to_date <= getdate(finish):
				days += entry.leaves
		elif entry.transaction_type == "Leave Application":
			half_day_date = None
			if entry.leaves % 1:
				half_day_date = frappe.db.get_value("Leave Application", entry.transaction_name,
					"half_day_date", for_update=True)
			days -= standard.get_number_of_leave_days(employee, leave_type,
				max(entry.from_date, getdate(start)), min(entry.to_date, getdate(finish)),
				int(bool(entry.leaves % 1)), half_day_date, holiday_list=entry.holiday_list)
	return days


def effective_balance(employee, leave_type, date, to_date=None,
	consider_all_leaves_in_the_allocation_period=False, for_consumption=False,
	exclude=None, lock=False):
	standard.validate_leave_access(employee)
	consider_all_leaves_in_the_allocation_period = bool(cint(consider_all_leaves_in_the_allocation_period))
	for_consumption = bool(cint(for_consumption))
	date, to_date = getdate(date), getdate(to_date or nowdate())
	allocation = standard.get_leave_allocation_records(employee, date, leave_type).get(leave_type)
	if not allocation or standard.is_lwp(leave_type):
		return standard.get_leave_balance_on(
			employee, leave_type, date, to_date,
			consider_all_leaves_in_the_allocation_period, for_consumption,
		)
	end = allocation.to_date if cint(consider_all_leaves_in_the_allocation_period) else date
	cf_expiry = standard.get_allocation_expiry_for_cf_leaves(
		employee, leave_type, to_date, allocation.from_date
	)

	def used(start, finish):
		return _ledger_days(employee, leave_type, start, finish, lock) - pending_days(
			employee, leave_type, start, finish, exclude, lock
		)

	taken = used(allocation.from_date, end)
	expired = flt(standard.get_manually_expired_leaves(employee, leave_type, allocation.from_date, end))
	if cf_expiry and allocation.unused_leaves:
		# Match HRMS's allocation-wide, oldest-leave-first consumption, including
		# reservations in the correct expiry segment rather than subtracting twice.
		cf_taken = used(allocation.from_date, cf_expiry)
		new_taken = used(add_days(cf_expiry, 1), allocation.to_date)
		if abs(cf_taken) > allocation.unused_leaves:
			new_taken -= abs(cf_taken) - allocation.unused_leaves
			cf_taken = -allocation.unused_leaves
		cf = flt(allocation.unused_leaves) + cf_taken if date <= getdate(cf_expiry) else 0
		balance = flt(allocation.new_leaves_allocated) + new_taken + cf + expired
		consumable = flt(allocation.new_leaves_allocated) + new_taken + _cap(cf, cf_expiry, date) + expired
	else:
		balance = consumable = flt(allocation.total_leaves_allocated) + taken + expired
	result = frappe._dict(
		leave_balance=balance,
		leave_balance_for_consumption=_cap(consumable, allocation.to_date, date),
	)
	return result if cint(for_consumption) else result.leave_balance


def _cap(balance, expiry, date):
	return min(balance, (getdate(expiry) - getdate(date)).days + 1) if balance > 0 else balance


@frappe.whitelist()
def get_leave_balance_on(employee, leave_type, date, to_date=None,
	consider_all_leaves_in_the_allocation_period=False, for_consumption=False):
	return effective_balance(employee, leave_type, date, to_date,
		consider_all_leaves_in_the_allocation_period, for_consumption)


@frappe.whitelist()
def get_leave_details(employee, date, for_salary_slip=False):
	for_salary_slip = bool(cint(for_salary_slip))
	details = standard.get_leave_details(employee, date, for_salary_slip)
	if cint(for_salary_slip):
		return details
	precision = cint(frappe.db.get_single_value("System Settings", "float_precision")) or 2
	records = standard.get_leave_allocation_records(employee, date)
	for leave_type, allocation in details["leave_allocation"].items():
		record = records[leave_type]
		approved = pending_days(employee, leave_type, record.from_date, record.to_date,
			statuses=("Approved",))
		pending = pending_days(employee, leave_type, record.from_date, record.to_date,
			statuses=("Open",))
		# Availability already includes both draft statuses. Classify them for
		# display without applying another deduction. Submitted applications leave
		# these draft-only queries and are counted by standard HRMS ledger usage.
		balance = effective_balance(employee, leave_type, date, to_date=records[leave_type].to_date,
			consider_all_leaves_in_the_allocation_period=True)
		allocation["leaves_reserved"] = flt(allocation["remaining_leaves"] - balance, precision)
		allocation["leaves_taken"] = flt(allocation["leaves_taken"] + approved, precision)
		allocation["leaves_pending_approval"] = flt(pending, precision)
		allocation["remaining_leaves"] = flt(balance, precision)
		# Apply HRMS's existing summary identity to the effective usage. Otherwise
		# carry-forward days consumed by drafts can also appear as expired days.
		allocation["expired_leaves"] = flt(max(0, allocation["total_leaves"] - balance
			- allocation["leaves_taken"] - pending), precision)
	return details


@frappe.whitelist()
def get_leave_balance_map():
	from hrms.api import get_current_employee

	details = get_leave_details(get_current_employee(), getdate())
	return {
		leave_type: {"allocated_leaves": allocation["total_leaves"],
			"balance_leaves": allocation["remaining_leaves"],
			"leaves_taken": allocation["leaves_taken"],
			"leaves_pending_approval": allocation["leaves_pending_approval"],
			"expired_leaves": allocation["expired_leaves"]}
		for leave_type, allocation in details["leave_allocation"].items()
	}


@frappe.whitelist()
def get_leave_metrics_and_details(employee, leave_type, from_date, to_date, half_day=None, half_day_date=None):
	frappe.has_permission("Employee", "read", employee, throw=True)
	return {
		"number_of_leave_days": standard.get_number_of_leave_days(
			employee, leave_type, from_date, to_date, half_day, half_day_date),
		"leave_allocation": get_leave_details(employee, from_date)["leave_allocation"],
	}


class LeaveReservationMixin:
	def before_validate(self):
		parent = getattr(super(), "before_validate", None)
		if parent:
			parent()
		# Serialize competing requests before balance validation. Current reads of
		# pending rows avoid a stale REPEATABLE READ snapshot after waiting.
		if self.employee:
			frappe.db.get_value("Employee", self.employee, "name", for_update=True)
		previous = self.get_doc_before_save()
		changed = not previous or previous.status != self.status
		if self.status in ("Approved", "Rejected") and changed:
			if not (self.has_permission("submit") or self.has_permlevel_access_to("status", permission_type="write")):
				frappe.throw(_("Only an authorized leave approver can approve, reject or submit leave."),
					frappe.PermissionError)
		if self.docstatus == 1 and not self.has_permission("submit"):
			frappe.throw(_("You do not have permission to submit leave."), frappe.PermissionError)

	def validate_balance_leaves(self):
		super().validate_balance_leaves()
		if self.status in ("Rejected", "Cancelled") or self.docstatus == 2 or standard.is_lwp(self.leave_type):
			return
		balance = effective_balance(self.employee, self.leave_type, self.from_date, self.to_date,
			True, True, exclude=self.name, lock=True)
		self.leave_balance = balance.leave_balance
		precision = cint(frappe.db.get_single_value("System Settings", "float_precision")) or 2
		if flt(balance.leave_balance_for_consumption, precision) < self.total_leave_days:
			self.show_insufficient_balance_message(balance.leave_balance_for_consumption)


class LeaveBalanceReportMixin:
	def execute_module(self, filters):
		filters = frappe._dict(filters)
		result = super().execute_module(filters)
		if self.name == "Employee Leave Balance Summary":
			columns, rows, *extra = result
			leave_types = frappe.get_all("Leave Type", pluck="name", order_by="name")
			for row in rows:
				details = get_leave_details(row[0], filters.date)["leave_allocation"]
				for index, leave_type in enumerate(leave_types, 3):
					row[index] = details.get(leave_type, {}).get("remaining_leaves", 0)
			return columns, rows, *extra
		if self.name == "Employee Leave Balance":
			from hrms.hr.report.employee_leave_balance.employee_leave_balance import get_chart_data

			columns, rows, *extra = result
			columns = [*columns, {"fieldname": "leaves_reserved", "label": _("Leave(s) Reserved"),
				"fieldtype": "Float", "width": 150}]
			for row in rows:
				if not row.get("employee"):
					continue
				args = (row.employee, row.leave_type, filters.to_date)
				reserved = standard.get_leave_balance_on(*args) - effective_balance(*args)
				row["leaves_reserved"] = reserved
				row["closing_balance"] -= reserved
			# The opening balance remains an accounting opening balance. Reserved
			# separately explains the difference in available closing balance.
			if len(extra) > 1:
				extra[1] = get_chart_data(rows, filters)
			return columns, rows, *extra
		return result
