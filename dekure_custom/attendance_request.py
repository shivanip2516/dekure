import frappe
from frappe import _
from frappe.utils import format_date, get_link_to_form

from hrms.hr.doctype.attendance_request.attendance_request import AttendanceRequest


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
		_validate_existing_attendance(doc)

		if doc.status == status:
			if doc.attendance_request != self.name:
				doc.db_set("attendance_request", self.name)
				doc.add_comment(
					comment_type="Info",
					text=_("Attendance status {0} confirmed via approved Attendance Request {1}").format(
						frappe.bold(status),
						get_link_to_form("Attendance Request", self.name),
					),
				)
			return

		old_status = doc.status
		updates = {"status": status, "attendance_request": self.name}
		if status == "Half Day":
			updates["half_day_status"] = "Absent"

		# db_set is the supported update mechanism used by HRMS itself for a
		# submitted Attendance correction. It is deliberately reached only after
		# permission, leave, payroll, status, and row-lock checks above.
		doc.db_set(updates)
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


def _validate_existing_attendance(attendance):
	if attendance.docstatus == 2:
		frappe.throw(_("Attendance {0} is cancelled and was not changed.").format(attendance.name))

	if attendance.status == "On Leave" or attendance.leave_application or attendance.leave_type:
		frappe.throw(
			_("Attendance {0} is linked to leave and was not changed.").format(
				get_link_to_form("Attendance", attendance.name)
			)
		)

	leave_application = frappe.db.exists(
		"Leave Application",
		{
			"employee": attendance.employee,
			"docstatus": 1,
			"status": "Approved",
			"from_date": ("<=", attendance.attendance_date),
			"to_date": (">=", attendance.attendance_date),
		},
	)
	if leave_application:
		frappe.throw(
			_("Attendance {0} conflicts with approved Leave Application {1} and was not changed.").format(
				get_link_to_form("Attendance", attendance.name),
				get_link_to_form("Leave Application", leave_application),
			)
		)

	if _has_submitted_salary_slip(attendance.employee, attendance.attendance_date):
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
