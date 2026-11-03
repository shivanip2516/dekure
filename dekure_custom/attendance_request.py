import frappe
from frappe import _
from frappe.utils import format_date, get_link_to_form

from hrms.hr.doctype.attendance_request.attendance_request import AttendanceRequest

from dekure_custom.auto_half_day_leave import (
	ATTENDANCE_AUTO_LEAVE,
	PRE_REQUEST_HALF_DAY_STATUS,
	PRE_REQUEST_STATUS,
	apply_auto_half_day_adjustment,
	get_active_auto_adjustment,
	has_submitted_salary_slip,
	is_manual_approved_leave,
	reverse_auto_half_day_adjustment,
)


class DekureAttendanceRequest(AttendanceRequest):
	"""Attendance Request processing with safe correction of existing Attendance."""

	def before_submit(self):
		# Attendance Request has no separate approval state in HRMS. Its server-side
		# submit permission is therefore the existing approval boundary.
		frappe.has_permission(
			"Attendance Request",
			ptype="submit",
			doc=self,
			user=frappe.session.user,
			throw=True,
		)

	def get_attendance_doc(self, attendance_date):
		"""Find the row that Attendance duplicate validation would consider a duplicate.

		HRMS's standard implementation uses exact shift equality. That misses a
		shifted Attendance when an Attendance Request has no shift, after which the
		standard insert fails in Attendance.validate_duplicate_record.
		"""
		params = {
			"employee": self.employee,
			"attendance_date": attendance_date,
		}
		shift_condition = ""
		order_by = "modified desc"
		if self.shift:
			params["shift"] = self.shift
			shift_condition = "and (shift = %(shift)s or shift is null or shift = '')"
			order_by = "case when shift = %(shift)s then 0 else 1 end, modified desc"

		rows = frappe.db.sql(
			f"""
				select name
				from `tabAttendance`
				where employee = %(employee)s
					and attendance_date = %(attendance_date)s
					and docstatus < 2
					{shift_condition}
				order by {order_by}
				for update
			""",
			params,
			as_dict=True,
		)

		if not rows:
			return None
		if not self.shift and len(rows) > 1:
			frappe.throw(
				_(
					"Multiple Attendance records exist for {0} on {1}. Select a shift on Attendance Request {2} before submitting."
				).format(
					frappe.bold(self.employee),
					frappe.bold(format_date(attendance_date)),
					frappe.bold(self.name),
				)
			)

		return frappe.get_doc("Attendance", rows[0].name)

	def create_or_update_attendance(self, date):
		doc = self.get_attendance_doc(date)
		if not doc:
			# Preserve HRMS's standard path, including all normal Attendance
			# validation, whenever the request date has no Attendance yet.
			return super().create_or_update_attendance(date)

		status = self.get_attendance_status(date)
		auto_adjustment = get_active_auto_adjustment(doc.name, for_update=True)
		payroll_reconciliation = _validate_existing_attendance(doc, auto_adjustment)
		original_state = _get_original_state_updates(doc)

		if doc.status == status:
			if doc.attendance_request != self.name:
				doc.db_set({"attendance_request": self.name, **original_state})
				doc.add_comment(
					comment_type="Info",
					text=_("Attendance status {0} confirmed via approved Attendance Request {1}").format(
						frappe.bold(status),
						get_link_to_form("Attendance Request", self.name),
					),
				)
			return

		old_status = doc.status
		updates = {"status": status, "attendance_request": self.name, **original_state}
		if status == "Half Day":
			updates["half_day_status"] = "Absent"
		elif auto_adjustment:
			updates.update(
				{
					ATTENDANCE_AUTO_LEAVE: None,
					"leave_type": None,
					"leave_application": None,
					"half_day_status": None,
					"modify_half_day_status": 0,
				}
			)

		# db_set is the supported update mechanism used by HRMS itself for a
		# submitted Attendance correction. It is deliberately reached only after
		# permission, leave, payroll, status, and row-lock checks above.
		doc.db_set(updates)
		for field, value in updates.items():
			setattr(doc, field, value)
		if auto_adjustment and status != "Half Day":
			reverse_auto_half_day_adjustment(doc, self)
			self.add_comment(
				comment_type="Info",
				text=_("Reversed automatic Leave Application {0} after correcting Attendance {1} to {2}.").format(
					get_link_to_form("Leave Application", auto_adjustment.name),
					get_link_to_form("Attendance", doc.name),
					frappe.bold(status),
				),
			)
		if payroll_reconciliation:
			frappe.msgprint(
				_("Submitted Salary Slip {0} was not changed. Payroll reconciliation may be required.").format(
					get_link_to_form("Salary Slip", payroll_reconciliation)
				),
				title=_("Payroll Reconciliation Required"),
				indicator="orange",
			)
		doc.add_comment(
			comment_type="Info",
			text=_("Changed the status from {0} to {1} via approved Attendance Request {2}").format(
				frappe.bold(old_status),
				frappe.bold(status),
				get_link_to_form("Attendance Request", self.name),
			),
		)
		frappe.msgprint(
			_("Updated status from {0} to {1} for date {2} in Attendance {3}").format(
				frappe.bold(old_status),
				frappe.bold(status),
				frappe.bold(format_date(date)),
				get_link_to_form("Attendance", doc.name),
			),
			title=_("Attendance Updated"),
		)

	def has_leave_record(self, attendance_date):
		"""Ignore only Dekure's automatic half-day application during correction."""
		filters = {
			"employee": self.employee,
			"docstatus": 1,
			"from_date": ("<=", attendance_date),
			"to_date": (">=", attendance_date),
			"status": "Approved",
		}
		if self.half_day_date == attendance_date:
			filters["half_day"] = 0
		applications = frappe.get_all(
			"Leave Application",
			filters=filters,
			fields=["name", "custom_is_auto_half_day_adjustment"],
		)
		return next(
			(
				row.name
				for row in applications
				if not row.get("custom_is_auto_half_day_adjustment")
			),
			None,
		)

	def on_cancel(self):
		attendance_list = frappe.get_all(
			"Attendance",
			{"employee": self.employee, "attendance_request": self.name, "docstatus": 1},
			fields=["name"],
		)
		for row in attendance_list:
			attendance = frappe.get_doc("Attendance", row.name)
			previous_status = attendance.get(PRE_REQUEST_STATUS)
			if not previous_status:
				attendance.cancel()
				continue

			if has_submitted_salary_slip(attendance.employee, attendance.attendance_date):
				frappe.msgprint(
					_("A submitted Salary Slip covers {0}. It was not changed and payroll reconciliation may be required.").format(
						frappe.bold(format_date(attendance.attendance_date))
					),
					title=_("Payroll Reconciliation Required"),
					indicator="orange",
				)

			previous_half_day_status = attendance.get(PRE_REQUEST_HALF_DAY_STATUS)
			updates = {
				"status": previous_status,
				"half_day_status": previous_half_day_status,
				"attendance_request": None,
				"leave_type": None,
				"leave_application": None,
				ATTENDANCE_AUTO_LEAVE: None,
				PRE_REQUEST_STATUS: None,
				PRE_REQUEST_HALF_DAY_STATUS: None,
			}
			attendance.db_set(updates)
			for field, value in updates.items():
				setattr(attendance, field, value)
			if previous_status == "Half Day":
				apply_auto_half_day_adjustment(attendance)
			attendance.add_comment(
				comment_type="Info",
				text=_("Restored status {0} after cancellation of Attendance Request {1}.").format(
					frappe.bold(previous_status), get_link_to_form("Attendance Request", self.name)
				),
			)


def _validate_existing_attendance(attendance, auto_adjustment=None):
	if attendance.docstatus == 2:
		frappe.throw(_("Attendance {0} is cancelled and was not changed.").format(attendance.name))

	if attendance.status == "On Leave" or (
		(attendance.leave_application or attendance.leave_type) and not auto_adjustment
	):
		frappe.throw(
			_("Attendance {0} is linked to leave and was not changed.").format(
				get_link_to_form("Attendance", attendance.name)
			)
		)

	leave_application = is_manual_approved_leave(
		attendance.employee,
		attendance.attendance_date,
		exclude=auto_adjustment.name if auto_adjustment else None,
	)
	if leave_application:
		frappe.throw(
			_("Attendance {0} conflicts with approved Leave Application {1} and was not changed.").format(
				get_link_to_form("Attendance", attendance.name),
				get_link_to_form("Leave Application", leave_application),
			)
		)

	payroll_reconciliation = _has_submitted_salary_slip(
		attendance.employee, attendance.attendance_date
	)
	if payroll_reconciliation and not auto_adjustment:
		frappe.throw(
			_("Attendance {0} falls inside a submitted Salary Slip period and was not changed. Cancel or amend payroll before making this correction.").format(
				get_link_to_form("Attendance", attendance.name)
			)
		)

	if attendance.status not in ("Present", "Half Day", "Absent"):
		frappe.throw(
			_("Attendance {0} has a protected status and was not changed.").format(
				get_link_to_form("Attendance", attendance.name)
			)
		)
	return payroll_reconciliation


def _get_original_state_updates(attendance):
	if getattr(attendance, PRE_REQUEST_STATUS, None):
		return {}
	return {
		PRE_REQUEST_STATUS: attendance.status,
		PRE_REQUEST_HALF_DAY_STATUS: attendance.half_day_status,
		ATTENDANCE_AUTO_LEAVE: getattr(attendance, ATTENDANCE_AUTO_LEAVE, None),
	}


def _has_submitted_salary_slip(employee, attendance_date):
	return frappe.db.exists(
		"Salary Slip",
		{
			"employee": employee,
			"docstatus": 1,
			"start_date": ("<=", attendance_date),
			"end_date": (">=", attendance_date),
		},
	)


def is_approved_attendance_request_correction(attendance):
	"""Return whether Attendance is protected by a submitted Attendance Request."""
	return bool(
		attendance.attendance_request
		and frappe.db.exists(
			"Attendance Request",
			{"name": attendance.attendance_request, "docstatus": 1},
		)
	)
