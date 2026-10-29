import frappe
from frappe import _
from frappe.utils import cint, get_datetime, getdate

PWA_CHECKIN_SOURCE = "PWA Selfie App"
PROCESSING_PENDING = "Pending"
PROCESSING_COMPLETED = "Completed"
PROCESSING_EXISTING = "Existing Attendance"
PROCESSING_NOT_APPLICABLE = "Not Applicable"
PROCESSING_DRAFT = "Draft"


def process_pwa_punch_attendance(doc, method=None):
	"""Run inside the punch transaction; never acknowledge a failed attendance write."""
	if doc.get("custom_checkin_source") != PWA_CHECKIN_SOURCE:
		return
	if frappe.db.get_value("Employee", doc.employee, "user_id") != frappe.session.user:
		frappe.throw(_("You can only record PWA attendance for yourself."), frappe.PermissionError)
	if doc.log_type == "IN":
		result = _create_pwa_draft_attendance(doc)
	else:
		result = _process_pwa_checkout_attendance(doc.name)
	expected = PROCESSING_DRAFT if doc.log_type == "IN" else PROCESSING_COMPLETED
	if result["status"] != expected:
		frappe.throw(result.get("message") or _("Attendance could not be updated. Please contact HR."))
	doc.reload()
	snapshot = get_checkout_attendance_status(doc)
	if doc.log_type == "OUT" and (
		snapshot.get("attendance_processing_status") != PROCESSING_COMPLETED
		or not snapshot.get("attendance") or snapshot["attendance"].get("docstatus") != 1
	):
		frappe.throw(_("Check-out attendance was not submitted. Please retry or contact HR."))
	# frappe.client.insert includes __onload in its response. Return the confirmed
	# attendance status, hours, times and docstatus without adding database fields.
	doc.set_onload("pwa_attendance", snapshot)
	return result


def _create_pwa_draft_attendance(checkin):
	if checkin.offshift or not checkin.shift or not checkin.shift_start:
		frappe.throw(_("A valid shift is required to record PWA attendance."))
	frappe.db.get_value("Employee", checkin.employee, "name", for_update=True)
	shift = frappe.get_doc("Shift Type", checkin.shift)
	message = _get_eligibility_message(shift, checkin)
	if message:
		frappe.throw(message)
	attendance_date = getdate(checkin.shift_start)
	if not shift.should_mark_attendance(checkin.employee, attendance_date):
		frappe.throw(_("Auto Attendance is not configured for this holiday."))
	attendance = _get_existing_attendance(checkin.employee, attendance_date, checkin.shift)
	if attendance:
		logs = _get_shift_occurrence_logs(checkin, attendance.name)
		_validate_pwa_draft(attendance, logs)
		from dekure_custom.api import _validate_attendance_can_be_overridden

		_validate_attendance_can_be_overridden(attendance, logs)
		# An additional IN belongs to the same draft; retain its original IN.
		attendance.out_time = None
		attendance.save(ignore_permissions=True)
		_link_logs([checkin], attendance.name)
	else:
		attendance = frappe.new_doc("Attendance")
		attendance.update({
			"employee": checkin.employee,
			"company": frappe.db.get_value("Employee", checkin.employee, "company"),
			"attendance_date": attendance_date, "shift": checkin.shift,
			"status": "Present", "in_time": checkin.time, "out_time": None,
			"working_hours": 0,
		})
		# Only the authenticated employee's validated PWA punch reaches here.
		# Attendance validation and all lifecycle hooks still run.
		attendance.insert(ignore_permissions=True)
		_link_logs([checkin], attendance.name)
	_notify_employee(checkin.employee, attendance.name)
	return _result(PROCESSING_DRAFT, attendance=attendance.name)


def _link_logs(logs, attendance):
	from hrms.hr.doctype.employee_checkin.employee_checkin import update_attendance_in_checkins

	names = [log.name for log in logs if log.attendance != attendance]
	if names:
		update_attendance_in_checkins(names, attendance)


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

	if checkin.attendance and frappe.db.get_value("Attendance", checkin.attendance, "docstatus") == 1:
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

	# Recover punches saved by the earlier asynchronous implementation through
	# the same draft lifecycle, rather than creating a second attendance path.
	first_in = next((log for log in logs if log.log_type == "IN"
		and log.custom_checkin_source == PWA_CHECKIN_SOURCE), None)
	if not first_in:
		return _result(PROCESSING_PENDING, _("A valid PWA check-in is required."))
	_create_pwa_draft_attendance(first_in)
	existing_attendance = _get_existing_attendance(checkin.employee, attendance_date, checkin.shift)
	return _reconcile_early_attendance(checkin, shift, existing_attendance)


def get_checkout_attendance_status(checkin):
	"""Return a display-safe status without changing Attendance or check-ins."""
	if not checkin:
		return {"attendance_processing_status": None}
	if checkin.attendance:
		attendance = frappe.db.get_value(
			"Attendance",
			checkin.attendance,
			["name", "docstatus", "status", "working_hours", "in_time", "out_time"],
			as_dict=True,
		)
		return {
			"attendance_processing_status": PROCESSING_COMPLETED if attendance.docstatus == 1 else PROCESSING_DRAFT,
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
		["name", "docstatus", "status", "working_hours", "in_time", "out_time"],
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
	query = frappe.get_all(
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
			"skip_auto_attendance",
		],
		order_by="time asc",
		run=False,
	)
	logs = query.for_update().run(as_dict=True)
	return [log for log in logs if not log.attendance or log.attendance == attendance]


def _reconcile_early_attendance(checkin, shift, existing_attendance, shift_closed=False):
	logs = _get_shift_occurrence_logs(checkin, existing_attendance.name)
	from dekure_custom.attendance_request import is_approved_attendance_request_correction

	if is_approved_attendance_request_correction(existing_attendance):
		return _result(
			PROCESSING_EXISTING,
			_("Attendance is protected by an approved Attendance Request and was left unchanged."),
			attendance=existing_attendance.name,
		)
	if not _is_pwa_owned_attendance(existing_attendance.name, logs):
		frappe.throw(
			_(
				"Attendance {0} was not created from this PWA shift occurrence and was left unchanged. "
				"An authorized user must review the later punch."
			).format(existing_attendance.name)
		)
	_validate_pwa_draft(existing_attendance, logs)
	if not shift_closed and (not logs or logs[-1].name != checkin.name or logs[-1].log_type != "OUT"):
		return _result(PROCESSING_PENDING,
			_("A later punch exists for this shift. Attendance must be finalized from the latest check-out."))

	from dekure_custom.api import (
		_calculate_attendance_from_shift_logs,
		_validate_attendance_can_be_overridden,
	)

	_validate_attendance_can_be_overridden(existing_attendance, logs)
	attendance_date = getdate(checkin.shift_start)
	attendance_values = _calculate_attendance_from_shift_logs(shift, logs, attendance_date, checkin.employee)
	if not shift_closed and not _has_valid_in_out_pair(attendance_values):
		frappe.throw(
			_("Attendance {0} was not changed because the shift has no valid IN/OUT pair.").format(
				existing_attendance.name
			)
		)

	# A successful PWA checkout now finalizes immediately. The shift's calculator
	# still accounts for every eligible punch and its configured break rules.
	_update_draft_attendance(existing_attendance, logs, attendance_values, finalize=True)
	_notify_employee(checkin.employee, existing_attendance.name)
	return _result(PROCESSING_COMPLETED, attendance=existing_attendance.name)


def _validate_pwa_draft(attendance, logs):
	if (attendance.docstatus != 0 or attendance.get("attendance_request")
		or any(log.get("skip_auto_attendance") for log in logs)
		or not _is_pwa_owned_attendance(attendance.name, logs)):
		frappe.throw(_("Attendance {0} is finalized or protected and was left unchanged. An authorized user must review the punch.").format(attendance.name))


def _is_pwa_owned_attendance(attendance, logs):
	return not any(
		log.attendance == attendance and log.custom_checkin_source != PWA_CHECKIN_SOURCE for log in logs
	) and any(
		log.attendance == attendance
		and log.custom_checkin_source == PWA_CHECKIN_SOURCE
		for log in logs
	)


def _update_draft_attendance(attendance, logs, attendance_values, finalize):
	status, attendance_date, working_hours, late_entry, early_exit, in_time, out_time = attendance_values
	attendance.update(
		{
			"status": status if finalize else "Present",
			"attendance_date": attendance_date,
			"working_hours": working_hours,
			"late_entry": late_entry,
			"early_exit": early_exit,
			"in_time": in_time,
			"out_time": out_time,
			"shift": logs[0].shift,
		}
	)
	from hrms.hr.doctype.employee_checkin.employee_checkin import get_overtime_data

	if logs[0].get("overtime_type") and status == "Present":
		overtime = get_overtime_data(logs[0].shift, working_hours)
		if overtime:
			attendance.update({"overtime_type": logs[0].overtime_type, **overtime})
	attendance.save(ignore_permissions=True)
	if finalize:
		attendance.flags.ignore_permissions = True
		attendance.submit()
		if attendance.docstatus != 1:
			frappe.throw(_("Attendance {0} was not submitted. Check-out was not finalized.").format(attendance.name))
	_link_logs(logs, attendance.name)


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
	import hrms

	hrms.refetch_resource("hrms:attendance_calendar_events", user)
	hrms.refetch_resource(["dekure:attendance_summary", employee], user)


class PWADraftAttendanceShiftMixin:
	def get_employee_checkins(self):
		# Linked drafts are intentionally absent from standard auto-attendance's
		# unlinked-log query. Finalize them once the normal sync boundary passes.
		drafts = frappe.get_all("Attendance", filters={"shift": self.name, "docstatus": 0}, pluck="name")
		if not drafts:
			return super().get_employee_checkins()
		logs = frappe.get_all("Employee Checkin", filters={
			"shift": self.name, "custom_checkin_source": PWA_CHECKIN_SOURCE,
			"attendance": ["in", drafts], "log_type": "IN", "offshift": 0,
			"shift_actual_end": ["<", self.last_sync_of_checkin],
			"time": [">=", self.process_attendance_after],
		}, fields=["name", "employee", "attendance"], order_by="time asc")
		seen = set()
		for log in logs:
			if log.attendance in seen:
				continue
			seen.add(log.attendance)
			frappe.db.get_value("Employee", log.employee, "name", for_update=True)
			doc = frappe.get_doc("Attendance", log.attendance, for_update=True)
			if doc.docstatus != 0:
				continue
			checkin = frappe.get_doc("Employee Checkin", log.name)
			if self.should_mark_attendance(checkin.employee, getdate(checkin.shift_start)):
				frappe.db.savepoint("pwa_draft_finalization")
				try:
					_reconcile_early_attendance(checkin, self, doc, shift_closed=True)
				except frappe.ValidationError:
					frappe.db.rollback(save_point="pwa_draft_finalization")
					frappe.log_error(title=f"PWA draft attendance requires review: {doc.name}",
						message=frappe.get_traceback())
		return super().get_employee_checkins()


@frappe.whitelist()
def get_attendance_calendar_events(from_date, to_date):
	from hrms.api import get_attendance_calendar_events as standard_events, get_current_employee

	events = standard_events(from_date, to_date)
	employee = get_current_employee()
	logs = frappe.get_all("Employee Checkin", filters={
		"employee": employee, "custom_checkin_source": PWA_CHECKIN_SOURCE,
		"attendance": ["is", "set"],
		"shift_start": ["between", [f"{from_date} 00:00:00", f"{to_date} 23:59:59"]],
	}, pluck="attendance")
	if logs:
		for doc in frappe.get_all("Attendance", filters={"name": ["in", logs],
			"employee": employee, "docstatus": 0,
			"attendance_date": ["between", [from_date, to_date]]}, fields=["attendance_date", "status"]):
			date = str(doc.attendance_date)
			if date not in events or events[date] == "Holiday":
				events[date] = doc.status
	return events


def _result(status, message=None, attendance=None):
	return {"status": status, "message": message, "attendance": attendance}


def ensure_rollback_ready():
	"""Read-only deployment guard: old code cannot finalize linked PWA drafts."""
	drafts = frappe.get_all("Attendance", filters={"docstatus": 0}, pluck="name")
	if drafts and frappe.db.exists("Employee Checkin", {
		"custom_checkin_source": PWA_CHECKIN_SOURCE, "attendance": ["in", drafts],
	}):
		frappe.throw(_("Open PWA attendance drafts remain. Let their shift windows close and run normal auto-attendance, or ask HR to review them, before rolling back."))
	return "No open PWA attendance drafts; code rollback can proceed."
