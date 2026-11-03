import frappe
from frappe import _
from frappe.utils import cint, flt, get_link_to_form, getdate


AUTO_FLAG = "custom_is_auto_half_day_adjustment"
AUTO_ATTENDANCE = "custom_auto_half_day_attendance"
REVERSED_BY_REQUEST = "custom_reversed_by_attendance_request"
ATTENDANCE_AUTO_LEAVE = "custom_auto_half_day_leave_application"
PRE_REQUEST_STATUS = "custom_pre_request_status"
PRE_REQUEST_HALF_DAY_STATUS = "custom_pre_request_half_day_status"

PRIVILEGE_LEAVE = "Privilege Leave"


class AutoHalfDayLeaveMixin:
	def before_cancel(self):
		if self.get(AUTO_FLAG) and not self.flags.get("dekure_auto_half_day_adjustment"):
			frappe.throw(
				_("Cancel or correct the linked Attendance instead of cancelling its automatic half-day adjustment directly.")
			)
		return super().before_cancel()

	def on_cancel(self):
		if not self.get(AUTO_FLAG):
			return super().on_cancel()

		# Retain HRMS's standard Leave Ledger reversal, but do not cancel the
		# Attendance that the approved Attendance Request has just corrected.
		self.create_leave_ledger_entry(submit=False)
		if frappe.db.get_single_value("HR Settings", "send_leave_notification"):
			self.notify_employee()
		self.publish_update()


def apply_calculated_half_day_on_submit(attendance, method=None):
	"""Cover standard auto-attendance while excluding manual/request/leave Half Days."""
	if (
		attendance.status == "Half Day"
		and attendance.shift
		and attendance.in_time
		and attendance.out_time
		and attendance.working_hours is not None
		and not attendance.attendance_request
		and not attendance.leave_application
	):
		apply_auto_half_day_adjustment(attendance)


def apply_auto_half_day_adjustment(attendance):
	"""Create one submitted half-day PL/LWP application for calculated Half Day."""
	if attendance.docstatus != 1 or attendance.status != "Half Day":
		return None

	frappe.db.get_value("Employee", attendance.employee, "name", for_update=True)
	existing = get_active_auto_adjustment(attendance.name, for_update=True)
	if existing:
		_link_adjustment(attendance, existing)
		return existing

	if attendance.leave_application:
		frappe.throw(
			_("Attendance {0} is already linked to Leave Application {1}.").format(
				get_link_to_form("Attendance", attendance.name),
				get_link_to_form("Leave Application", attendance.leave_application),
			)
		)

	leave_type = _get_automatic_leave_type(attendance.employee, attendance.attendance_date)
	from hrms.hr.doctype.leave_application.leave_application import get_employee_leave_approver

	leave = frappe.new_doc("Leave Application")
	leave.update(
		{
			"employee": attendance.employee,
			"company": attendance.company
			or frappe.db.get_value("Employee", attendance.employee, "company"),
			"leave_type": leave_type,
			"from_date": attendance.attendance_date,
			"to_date": attendance.attendance_date,
			"half_day": 1,
			"half_day_date": attendance.attendance_date,
			"status": "Approved",
			"leave_approver": get_employee_leave_approver(attendance.employee),
			"description": _("Automatic half-day adjustment for calculated Attendance {0}").format(
				attendance.name
			),
			AUTO_FLAG: 1,
			AUTO_ATTENDANCE: attendance.name,
		}
	)
	leave.flags.dekure_auto_half_day_adjustment = True
	leave.insert(ignore_permissions=True)
	leave.flags.dekure_auto_half_day_adjustment = True
	leave.flags.ignore_permissions = True
	leave.submit()
	_link_adjustment(attendance, leave)

	attendance.add_comment(
		comment_type="Info",
		text=_("Applied automatic 0.5 day {0} through Leave Application {1}.").format(
			frappe.bold(leave_type), get_link_to_form("Leave Application", leave.name)
		),
	)
	leave.add_comment(
		comment_type="Info",
		text=_("Created automatically for calculated Half Day Attendance {0}.").format(
			get_link_to_form("Attendance", attendance.name)
		),
	)
	return leave


def get_active_auto_adjustment(attendance_name, for_update=False):
	query = frappe.get_all(
		"Leave Application",
		filters={AUTO_FLAG: 1, AUTO_ATTENDANCE: attendance_name, "docstatus": 1, "status": "Approved"},
		fields=["name"],
		order_by="creation desc",
		run=False,
	)
	rows = query.for_update().run(as_dict=True) if for_update else query.run(as_dict=True)
	return frappe.get_doc("Leave Application", rows[0].name) if rows else None


def reverse_auto_half_day_adjustment(attendance, attendance_request=None):
	"""Cancel only the marked automatic application, reversing its ledger entry."""
	leave = get_active_auto_adjustment(attendance.name, for_update=True)
	if not leave:
		return None

	if attendance_request:
		leave.db_set(REVERSED_BY_REQUEST, attendance_request.name, update_modified=True)
	link_updates = {}
	if attendance.leave_application == leave.name:
		link_updates.update({"leave_application": None, "leave_type": None})
	if attendance.get(ATTENDANCE_AUTO_LEAVE) == leave.name:
		link_updates[ATTENDANCE_AUTO_LEAVE] = None
	if link_updates:
		attendance.db_set(link_updates, update_modified=True)
		for field, value in link_updates.items():
			setattr(attendance, field, value)
	leave.flags.dekure_auto_half_day_adjustment = True
	leave.flags.ignore_permissions = True
	leave.cancel()

	request_link = (
		get_link_to_form("Attendance Request", attendance_request.name)
		if attendance_request
		else _("an attendance recalculation")
	)
	leave.add_comment(
		comment_type="Info",
		text=_("Automatic half-day adjustment reversed after {0} changed Attendance {1}.").format(
			request_link, get_link_to_form("Attendance", attendance.name)
		),
	)
	return leave


def reconcile_auto_half_day_adjustment(attendance, calculated_status):
	"""Keep the automatic adjustment aligned with a recalculated Attendance."""
	if calculated_status == "Half Day":
		return apply_auto_half_day_adjustment(attendance)
	return reverse_auto_half_day_adjustment(attendance)


def reverse_auto_adjustment_before_attendance_cancel(attendance, method=None):
	"""Prevent an automatic ledger deduction from surviving Attendance cancellation."""
	leave = get_active_auto_adjustment(attendance.name, for_update=True)
	if not leave:
		return
	if has_submitted_salary_slip(attendance.employee, attendance.attendance_date):
		frappe.msgprint(
			_("A submitted Salary Slip covers this Attendance. It was not changed and payroll reconciliation may be required."),
			title=_("Payroll Reconciliation Required"),
			indicator="orange",
		)
	reverse_auto_half_day_adjustment(attendance)


def is_manual_approved_leave(employee, attendance_date, exclude=None):
	filters = {
		"employee": employee,
		"docstatus": 1,
		"status": "Approved",
		"from_date": ("<=", attendance_date),
		"to_date": (">=", attendance_date),
	}
	if exclude:
		filters["name"] = ("!=", exclude)
	applications = frappe.get_all(
		"Leave Application", filters=filters, fields=["name", AUTO_FLAG]
	)
	return next((row.name for row in applications if not cint(row.get(AUTO_FLAG))), None)


def _get_automatic_leave_type(employee, attendance_date):
	privilege_leave = frappe.db.get_value(
		"Leave Type", PRIVILEGE_LEAVE, ["is_lwp", "is_ppl"], as_dict=True
	)
	if privilege_leave and not cint(privilege_leave.is_lwp) and not cint(privilege_leave.is_ppl):
		from dekure_custom.leave_reservations import effective_balance

		balance = effective_balance(
			employee,
			PRIVILEGE_LEAVE,
			getdate(attendance_date),
			to_date=getdate(attendance_date),
			consider_all_leaves_in_the_allocation_period=True,
			for_consumption=True,
			lock=True,
		)
		available = balance.get("leave_balance_for_consumption", balance) if hasattr(balance, "get") else balance
		if flt(available) >= 0.5:
			return PRIVILEGE_LEAVE

	lwp_types = frappe.get_all("Leave Type", filters={"is_lwp": 1}, pluck="name", order_by="name")
	for preferred in ("Leave Without Pay", "LWP", "Loss Of Pay"):
		if preferred in lwp_types:
			return preferred
	if len(lwp_types) == 1:
		return lwp_types[0]
	if not lwp_types:
		frappe.throw(_("Configure a Leave Type marked Leave Without Pay for automatic half-day adjustments."))
	frappe.throw(
		_("Multiple Leave Without Pay types are configured. Name the intended type Leave Without Pay or LWP.")
	)


def _link_adjustment(attendance, leave):
	updates = {ATTENDANCE_AUTO_LEAVE: leave.name}
	if attendance.leave_application != leave.name or attendance.leave_type != leave.leave_type:
		updates.update({"leave_application": leave.name, "leave_type": leave.leave_type})
	attendance.db_set(updates, update_modified=True)
	attendance.leave_application = leave.name
	attendance.leave_type = leave.leave_type
	setattr(attendance, ATTENDANCE_AUTO_LEAVE, leave.name)


def has_submitted_salary_slip(employee, attendance_date):
	return frappe.db.exists(
		"Salary Slip",
		{
			"employee": employee,
			"docstatus": 1,
			"start_date": ("<=", attendance_date),
			"end_date": (">=", attendance_date),
		},
	)
