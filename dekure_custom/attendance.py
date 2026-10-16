import frappe
from frappe import _
from frappe.utils import cint, get_datetime, getdate

PWA_CHECKIN_SOURCE = "PWA Selfie App"
PROCESSING_PENDING = "Pending"
PROCESSING_COMPLETED = "Completed"
PROCESSING_EXISTING = "Existing Attendance"
PROCESSING_NOT_APPLICABLE = "Not Applicable"


def enqueue_pwa_checkout_attendance(doc, method=None):
	"""Queue attendance for a saved PWA OUT punch without delaying its response."""
	if doc.log_type != "OUT" or doc.get("custom_checkin_source") != PWA_CHECKIN_SOURCE:
		return

	frappe.enqueue(
		"dekure_custom.attendance.process_pwa_checkout_attendance",
		queue="short",
		enqueue_after_commit=True,
		job_id=f"pwa_checkout_attendance_{doc.name}",
		deduplicate=True,
		checkin_name=doc.name,
	)


def process_pwa_checkout_attendance(checkin_name):
	"""Finalize only the employee and shift occurrence identified by an OUT punch."""
	try:
		return _process_pwa_checkout_attendance(checkin_name)
	except Exception:
		frappe.log_error(
			title=f"PWA attendance processing failed for {checkin_name}",
			message=frappe.get_traceback(),
		)
		raise


def _process_pwa_checkout_attendance(checkin_name):
	checkin = frappe.get_doc("Employee Checkin", checkin_name)
	if checkin.log_type != "OUT" or checkin.get("custom_checkin_source") != PWA_CHECKIN_SOURCE:
		return _result(PROCESSING_NOT_APPLICABLE, _("This is not a PWA check-out."))

	# The employee row lock serializes jobs for the same employee while allowing
	# unrelated employees to be processed independently.
	frappe.db.sql("select name from `tabEmployee` where name=%s for update", checkin.employee)
	frappe.db.sql("select name from `tabEmployee Checkin` where name=%s for update", checkin.name)
	checkin.reload()

	if checkin.attendance:
		return _result(PROCESSING_COMPLETED, attendance=checkin.attendance)
	if cint(checkin.offshift) or not checkin.shift or not checkin.shift_start or not checkin.shift_actual_end:
		return _result(
			PROCESSING_NOT_APPLICABLE, _("The check-out is not linked to a valid shift occurrence.")
		)

	shift = frappe.get_doc("Shift Type", checkin.shift)
	eligibility = _get_eligibility_message(shift, checkin)
	if eligibility:
		return _result(PROCESSING_PENDING, eligibility)

	attendance_date = getdate(checkin.shift_start)
	existing_attendance = _get_existing_attendance(checkin.employee, attendance_date, checkin.shift)
	if existing_attendance:
		return _reconcile_early_attendance(checkin, shift, existing_attendance)

	logs = _get_shift_occurrence_logs(checkin)
	if not logs:
		return _result(PROCESSING_PENDING, _("No eligible check-in logs were found for this shift."))
	if not shift.should_mark_attendance(checkin.employee, attendance_date):
		return _result(PROCESSING_NOT_APPLICABLE, _("Auto Attendance is not configured for this holiday."))

	from dekure_custom.api import _calculate_attendance_from_shift_logs

	attendance_values = _calculate_attendance_from_shift_logs(shift, logs, attendance_date, checkin.employee)
	if not _has_valid_in_out_pair(attendance_values):
		return _result(PROCESSING_PENDING, _("A valid IN and OUT pair is required."))

	# Recheck under the employee lock immediately before creating Attendance.
	existing_attendance = _get_existing_attendance(checkin.employee, attendance_date, checkin.shift)
	if existing_attendance:
		return _reconcile_early_attendance(checkin, shift, existing_attendance)

	attendance = _create_attendance_and_link_logs(logs, attendance_values, shift)
	if not attendance:
		# A concurrent standard scheduler run can win after the preflight check.
		existing_attendance = _get_existing_attendance(checkin.employee, attendance_date, checkin.shift)
		if existing_attendance:
			return _result(PROCESSING_EXISTING, attendance=existing_attendance.name)
		return _result(PROCESSING_PENDING, _("Attendance could not be finalized and will be retried."))

	_notify_employee(checkin.employee, attendance.name)
	return _result(PROCESSING_COMPLETED, attendance=attendance.name)


def get_checkout_attendance_status(checkin):
	"""Return a display-safe status without changing Attendance or check-ins."""
	if not checkin:
		return {"attendance_processing_status": None}
	if checkin.attendance:
		attendance = frappe.db.get_value(
			"Attendance",
			checkin.attendance,
			["name", "status", "working_hours", "in_time", "out_time"],
			as_dict=True,
		)
		return {
			"attendance_processing_status": PROCESSING_COMPLETED,
			"attendance": attendance,
		}
	if checkin.offshift or not checkin.shift or not checkin.shift_start:
		return {
			"attendance_processing_status": PROCESSING_NOT_APPLICABLE,
			"attendance_processing_message": _("The check-out is not linked to a valid shift occurrence."),
		}

	attendance_date = getdate(checkin.shift_start)
	existing = frappe.db.get_value(
		"Attendance",
		{"employee": checkin.employee, "attendance_date": attendance_date, "docstatus": ["<", 2]},
		["name", "status", "working_hours", "in_time", "out_time"],
		as_dict=True,
	)
	if existing:
		return {
			"attendance_processing_status": PROCESSING_EXISTING,
			"attendance_processing_message": _("Attendance already exists and was left unchanged."),
			"attendance": existing,
		}
	return {
		"attendance_processing_status": PROCESSING_PENDING,
		"attendance_processing_message": _("Attendance processing is pending."),
	}


def _get_eligibility_message(shift, checkin):
	if not cint(shift.enable_auto_attendance) or not shift.process_attendance_after:
		return _("Auto Attendance settings are incomplete for shift {0}.").format(shift.name)
	if get_datetime(checkin.time) < get_datetime(shift.process_attendance_after):
		return _("The check-out is before the shift's Process Attendance After date.")
	return None


def _get_shift_occurrence_logs(checkin, attendance=None):
	logs = frappe.get_all(
		"Employee Checkin",
		filters={
			"employee": checkin.employee,
			"shift": checkin.shift,
			"shift_start": checkin.shift_start,
			"offshift": 0,
			"time": [">=", frappe.db.get_value("Shift Type", checkin.shift, "process_attendance_after")],
		},
		fields=[
			"name",
			"employee",
			"log_type",
			"time",
			"shift",
			"shift_start",
			"shift_end",
			"shift_actual_start",
			"shift_actual_end",
			"device_id",
			"overtime_type",
			"attendance",
			"custom_checkin_source",
		],
		order_by="time asc",
	)
	return [log for log in logs if not log.attendance or log.attendance == attendance]


def _reconcile_early_attendance(checkin, shift, existing_attendance):
	logs = _get_shift_occurrence_logs(checkin, existing_attendance.name)
	if not _is_pwa_owned_attendance(existing_attendance.name, logs):
		frappe.throw(
			_(
				"Attendance {0} was not created from this PWA shift occurrence and was left unchanged. "
				"An authorized user must review the later punch."
			).format(existing_attendance.name)
		)

	from dekure_custom.api import (
		_calculate_attendance_from_shift_logs,
		_validate_attendance_can_be_overridden,
	)

	_validate_attendance_can_be_overridden(existing_attendance, logs)
	attendance_date = getdate(checkin.shift_start)
	attendance_values = _calculate_attendance_from_shift_logs(shift, logs, attendance_date, checkin.employee)
	if not _has_valid_in_out_pair(attendance_values):
		frappe.throw(
			_("Attendance {0} was not changed because the shift has no valid IN/OUT pair.").format(
				existing_attendance.name
			)
		)

	_update_submitted_attendance(existing_attendance, logs, attendance_values)
	_notify_employee(checkin.employee, existing_attendance.name)
	return _result(PROCESSING_COMPLETED, attendance=existing_attendance.name)


def _is_pwa_owned_attendance(attendance, logs):
	return any(
		log.attendance == attendance
		and log.log_type == "OUT"
		and log.custom_checkin_source == PWA_CHECKIN_SOURCE
		for log in logs
	)


def _update_submitted_attendance(attendance, logs, attendance_values):
	status, attendance_date, working_hours, late_entry, early_exit, in_time, out_time = attendance_values
	attendance.update(
		{
			"status": status,
			"attendance_date": attendance_date,
			"working_hours": working_hours,
			"late_entry": late_entry,
			"early_exit": early_exit,
			"in_time": in_time,
			"out_time": out_time,
			"shift": logs[0].shift,
		}
	)
	# These calculated fields are not allow_on_submit. This exemption is scoped to
	# the validated PWA-owned Attendance document and still runs Attendance.validate.
	attendance.flags.ignore_validate_update_after_submit = True
	attendance.save(ignore_permissions=True)

	from hrms.hr.doctype.employee_checkin.employee_checkin import update_attendance_in_checkins

	unlinked_logs = [log.name for log in logs if log.attendance != attendance.name]
	if unlinked_logs:
		update_attendance_in_checkins(unlinked_logs, attendance.name)
	attendance.add_comment(
		text=_("Attendance recalculated after an additional PWA check-out in the same shift.")
	)


def _has_valid_in_out_pair(attendance_values):
	_in_time = attendance_values[-2]
	_out_time = attendance_values[-1]
	return bool(_in_time and _out_time and _out_time >= _in_time)


def _create_attendance_and_link_logs(logs, attendance_values, shift):
	from hrms.hr.doctype.employee_checkin.employee_checkin import (
		create_or_update_attendance,
		update_attendance_in_checkins,
	)

	status, attendance_date, working_hours, late_entry, early_exit, in_time, out_time = attendance_values
	frappe.db.savepoint("pwa_attendance_creation")
	try:
		attendance = create_or_update_attendance(
			employee=logs[0].employee,
			attendance_date=attendance_date,
			attendance_status=status,
			working_hours=working_hours,
			shift=shift.name,
			late_entry=late_entry,
			early_exit=early_exit,
			in_time=in_time,
			out_time=out_time,
			overtime_type=logs[0].get("overtime_type"),
		)
		update_attendance_in_checkins([log.name for log in logs], attendance.name)
		if status == "Absent":
			attendance.add_comment(
				text=_("Employee was marked Absent for not meeting the working hours threshold.")
			)
		return attendance
	except frappe.ValidationError:
		# Unlike HRMS's batch wrapper, do not mark this employee's logs as skipped.
		# A retry can safely resolve a concurrent Attendance created by the scheduler.
		frappe.db.rollback(save_point="pwa_attendance_creation")
		if _get_existing_attendance(logs[0].employee, attendance_date, shift.name):
			return None
		raise


def _get_existing_attendance(employee, attendance_date, shift):
	rows = frappe.db.sql(
		"""
		select name
		from `tabAttendance`
		where employee = %s
			and attendance_date = %s
			and docstatus < 2
		order by
			case when shift = %s then 0 when shift is null or shift = '' then 1 else 2 end,
			modified desc
		limit 1
		for update
		""",
		(employee, attendance_date, shift),
	)
	return frappe.get_doc("Attendance", rows[0][0]) if rows else None


def _notify_employee(employee, attendance):
	user = frappe.db.get_value("Employee", employee, "user_id")
	if not user:
		return
	frappe.publish_realtime(
		"dekure_attendance_processed",
		{"attendance": attendance},
		user=user,
		after_commit=True,
	)


def _result(status, message=None, attendance=None):
	return {"status": status, "message": message, "attendance": attendance}
