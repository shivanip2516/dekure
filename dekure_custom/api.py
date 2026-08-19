import frappe
from frappe import _
from frappe.utils import get_datetime, getdate, get_time, now_datetime, nowdate


MISSED_CHECKIN = "Missed Check-in"
MISSED_CHECKOUT = "Missed Check-out"
PENDING = "Pending Approval"
VISIT_PLANNED = "Planned"
VISIT_IN_PROGRESS = "In Progress"
VISIT_COMPLETED = "Completed"
VISIT_CANCELLED = "Cancelled"


@frappe.whitelist()
def create_checkin(log_type, selfie, latitude=None, longitude=None):
    """Called from the /checkin page when employee taps Check In / Check Out"""
    if not selfie:
        return {"ok": False, "error": "Selfie required"}

    employee = frappe.db.get_value("Employee", {"user_id": frappe.session.user}, "name")
    if not employee:
        return {"ok": False, "error": "No employee linked to your login"}

    if log_type not in ("IN", "OUT"):
        return {"ok": False, "error": "Invalid check-in type"}

    try:
        _lock_employee(employee)
        _validate_normal_punch(employee, log_type, nowdate())
    except frappe.ValidationError as error:
        return {"ok": False, "error": str(error)}

    # Save the base64 selfie image as a File record
    photo = frappe.get_doc({
        "doctype": "File",
        "file_name": f"selfie_{employee}_{now_datetime().strftime('%Y%m%d%H%M%S')}.jpg",
        "content": selfie.split(",")[1],
        "decode": True,
        "is_private": 1
    })
    photo.insert(ignore_permissions=True)

    # Create the Employee Checkin record with selfie + location
    checkin = frappe.get_doc({
        "doctype": "Employee Checkin",
        "employee": employee,
        "log_type": log_type,
        "time": now_datetime(),
        "custom_selfie": photo.file_url,
        "latitude": latitude,
        "longitude": longitude
    })
    checkin.insert(ignore_permissions=True)

    return {"ok": True}


@frappe.whitelist()
def get_missed_punch_status():
    """Return the actions the logged-in employee can take from the kiosk."""
    employee = _current_employee()
    today = nowdate()
    today_logs = _day_logs(employee, today)
    previous_open_checkout = _open_checkout_date(employee, before_date=today)

    checkout_request_date = previous_open_checkout or today
    return {
        "employee": employee,
        "attendance_date": today,
        "can_request_checkin": not previous_open_checkout
        and not _has_log(today_logs, "IN")
        and not _has_pending_request(employee, today, MISSED_CHECKIN),
        "can_request_checkout": (
            previous_open_checkout
            or (_has_log(today_logs, "IN") and not _has_log(today_logs, "OUT"))
        )
        and not _has_pending_request(employee, checkout_request_date, MISSED_CHECKOUT),
        "missed_checkout_date": checkout_request_date if previous_open_checkout else None,
        "blocked_by_checkout_date": previous_open_checkout,
    }


@frappe.whitelist()
def create_missed_punch_request(request_type, attendance_date, requested_time, reason):
    """Create a pending request; the employee and approver never come from the browser."""
    if request_type not in (MISSED_CHECKIN, MISSED_CHECKOUT):
        frappe.throw(_("Invalid request type"))
    if not reason or not reason.strip():
        frappe.throw(_("A reason is required"))

    employee = _current_employee()
    attendance_date = getdate(attendance_date)
    if attendance_date > getdate(nowdate()):
        frappe.throw(_("Attendance date cannot be in the future"))
    try:
        requested_time = get_time(requested_time)
    except (TypeError, ValueError):
        frappe.throw(_("Enter a valid requested time"))

    _lock_employee(employee)
    _validate_missed_punch(employee, attendance_date, request_type)
    _validate_requested_punch_time(employee, attendance_date, request_type, requested_time)

    request = frappe.get_doc(
        {
            "doctype": "Attendance Regularization Request",
            "employee": employee,
            "attendance_date": attendance_date,
            "request_type": request_type,
            "requested_time": requested_time,
            "shift": _get_shift(employee, attendance_date),
            "attendance_approver": _get_attendance_approver(employee),
            "reason": reason.strip(),
            "status": PENDING,
        }
    )
    request.insert(ignore_permissions=True)
    frappe.share.add("Attendance Regularization Request", request.name, frappe.session.user, read=1, write=1)
    frappe.share.add("Attendance Regularization Request", request.name, request.attendance_approver, read=1, write=1)
    return {"ok": True, "name": request.name, "status": request.status}


@frappe.whitelist()
def review_missed_punch_request(name, action, rejection_reason=None):
    """Approve or reject exactly once, and create the requested timestamp only on approval."""
    if action not in ("Approve", "Reject"):
        frappe.throw(_("Invalid review action"))

    request = frappe.get_doc("Attendance Regularization Request", name)
    if frappe.session.user != request.attendance_approver and "System Manager" not in frappe.get_roles():
        frappe.throw(_("Only the assigned Attendance Approver can review this request"), frappe.PermissionError)
    if request.status != PENDING:
        frappe.throw(_("This request has already been reviewed"))
    if action == "Reject" and not (rejection_reason or "").strip():
        frappe.throw(_("A rejection reason is required"))

    _lock_employee(request.employee)
    if action == "Approve":
        _validate_missed_punch(
            request.employee, getdate(request.attendance_date), request.request_type, exclude_request=request.name
        )
        _validate_requested_punch_time(
            request.employee, getdate(request.attendance_date), request.request_type, get_time(request.requested_time)
        )
        log_type = "IN" if request.request_type == MISSED_CHECKIN else "OUT"
        checkin = frappe.get_doc(
            {
                "doctype": "Employee Checkin",
                "employee": request.employee,
                "log_type": log_type,
                "time": get_datetime(f"{request.attendance_date} {request.requested_time}"),
            }
        )
        checkin.insert(ignore_permissions=True)
        request.db_set({"status": "Approved", "reviewed_by": frappe.session.user, "reviewed_on": now_datetime()})
    else:
        request.db_set(
            {
                "status": "Rejected",
                "rejection_reason": rejection_reason.strip(),
                "reviewed_by": frappe.session.user,
                "reviewed_on": now_datetime(),
            }
        )
    return {"ok": True, "status": request.status}


def _current_employee():
    employee = frappe.db.get_value("Employee", {"user_id": frappe.session.user}, "name")
    if not employee:
        frappe.throw(_("No employee is linked to your login"), frappe.PermissionError)
    return employee


def _lock_employee(employee):
    # Serialises normal punch and regularisation requests for the same employee.
    frappe.db.sql("select name from `tabEmployee` where name=%s for update", employee)


def _day_logs(employee, attendance_date):
    return frappe.get_all(
        "Employee Checkin",
        filters={"employee": employee, "time": ["between", [f"{attendance_date} 00:00:00", f"{attendance_date} 23:59:59"]]},
        fields=["name", "log_type", "time"],
    )


def _has_log(logs, log_type):
    return any(log.log_type == log_type for log in logs)


def _has_pending_request(employee, attendance_date, request_type, exclude_request=None):
    filters = {"employee": employee, "attendance_date": attendance_date, "request_type": request_type, "status": PENDING}
    if exclude_request:
        filters["name"] = ["!=", exclude_request]
    return frappe.db.exists("Attendance Regularization Request", filters)


def _open_checkout_date(employee, before_date=None):
    filters = {"employee": employee}
    if before_date:
        filters["time"] = ["<", f"{before_date} 00:00:00"]
    logs = frappe.get_all("Employee Checkin", filters=filters, fields=["log_type", "time"], order_by="time asc")
    open_dates = {}
    for log in logs:
        log_date = getdate(log.time)
        open_dates.setdefault(log_date, {"IN": False, "OUT": False})[log.log_type] = True
    incomplete = [date for date, types in open_dates.items() if types["IN"] and not types["OUT"]]
    return max(incomplete).isoformat() if incomplete else None


def _validate_normal_punch(employee, log_type, attendance_date):
    previous_open_checkout = _open_checkout_date(employee, before_date=attendance_date)
    if log_type == "IN" and previous_open_checkout:
        frappe.throw(_("Complete the missed check-out for {0} before checking in.").format(previous_open_checkout))

    logs = _day_logs(employee, attendance_date)
    if log_type == "IN" and _has_log(logs, "IN"):
        frappe.throw(_("A check-in already exists for this attendance date."))
    if log_type == "IN" and _has_pending_request(employee, attendance_date, MISSED_CHECKIN):
        frappe.throw(_("A missed check-in request for this attendance date is awaiting approval."))
    if log_type == "OUT":
        if not _has_log(logs, "IN"):
            frappe.throw(_("Check in before checking out."))
        if _has_log(logs, "OUT"):
            frappe.throw(_("A check-out already exists for this attendance date."))
        if _has_pending_request(employee, attendance_date, MISSED_CHECKOUT):
            frappe.throw(_("A missed check-out request for this attendance date is awaiting approval."))


def _validate_missed_punch(employee, attendance_date, request_type, exclude_request=None):
    logs = _day_logs(employee, attendance_date)
    log_type = "IN" if request_type == MISSED_CHECKIN else "OUT"
    if _has_log(logs, log_type):
        frappe.throw(_("A {0} already exists for this attendance date.").format("check-in" if log_type == "IN" else "check-out"))
    if request_type == MISSED_CHECKOUT and not _has_log(logs, "IN"):
        frappe.throw(_("A missed check-out can only be requested after a check-in exists."))
    if _has_pending_request(employee, attendance_date, request_type, exclude_request):
        frappe.throw(_("A pending request of this type already exists for this attendance date."))


def _validate_requested_punch_time(employee, attendance_date, request_type, requested_time):
    requested_datetime = get_datetime(f"{attendance_date} {requested_time}")
    logs = _day_logs(employee, attendance_date)
    if request_type == MISSED_CHECKOUT:
        checkin_time = next((log.time for log in logs if log.log_type == "IN"), None)
        if checkin_time and requested_datetime <= get_datetime(checkin_time):
            frappe.throw(_("Requested check-out time must be after the existing check-in."))
    else:
        checkout_time = next((log.time for log in logs if log.log_type == "OUT"), None)
        if checkout_time and requested_datetime >= get_datetime(checkout_time):
            frappe.throw(_("Requested check-in time must be before the existing check-out."))


def _get_shift(employee, attendance_date):
    assignments = frappe.get_all(
        "Shift Assignment",
        filters={"employee": employee, "start_date": ["<=", attendance_date], "docstatus": 1},
        fields=["shift_type", "end_date"],
        order_by="start_date desc",
    )
    for assignment in assignments:
        if not assignment.end_date or getdate(assignment.end_date) >= attendance_date:
            return assignment.shift_type
    return frappe.db.get_value("Employee", employee, "default_shift")


def _get_attendance_approver(employee):
    approver = frappe.db.get_value("Employee", employee, "attendance_approver")
    if not approver:
        frappe.throw(_("No Attendance Approver is configured for this employee."))
    return approver


@frappe.whitelist()
def get_today_visits():
    """Return only the logged-in employee's visits scheduled for today."""
    employee = _current_employee()
    return frappe.get_all(
        "Visit",
        filters={"employee": employee, "visit_date": nowdate()},
        fields=_visit_fields(),
        order_by="creation asc",
    )


@frappe.whitelist()
def get_visit(name):
    visit = _get_employee_visit(name)
    return {field: visit.get(field) for field in _visit_fields()}


@frappe.whitelist()
def create_visit(customer, visit_date=None, visit_type=None, contact_person=None, address=None, visit_purpose=None, remarks=None):
    """Create a planned visit for the current PWA employee only."""
    if not customer:
        frappe.throw(_("Customer is required"))
    visit_date = getdate(visit_date or nowdate())
    if visit_date < getdate(nowdate()):
        frappe.throw(_("A visit cannot be planned for a past date"))

    visit = frappe.get_doc(
        {
            "doctype": "Visit",
            "employee": _current_employee(),
            "customer": customer,
            "visit_date": visit_date,
            "visit_type": visit_type,
            "contact_person": contact_person,
            "address": address,
            "visit_purpose": visit_purpose,
            "remarks": remarks,
            "status": VISIT_PLANNED,
        }
    )
    visit.insert(ignore_permissions=True)
    frappe.share.add("Visit", visit.name, frappe.session.user, read=1)
    return get_visit(visit.name)


@frappe.whitelist()
def punch_in_visit(name, latitude, longitude):
    visit = _get_employee_visit(name)
    _validate_visit_transition(visit, VISIT_PLANNED, VISIT_IN_PROGRESS)
    latitude, longitude = _validate_coordinates(latitude, longitude)
    visit.db_set(
        {
            "checkin_latitude": latitude,
            "checkin_longitude": longitude,
            "checkin_time": now_datetime(),
            "status": VISIT_IN_PROGRESS,
        }
    )
    return get_visit(visit.name)


@frappe.whitelist()
def punch_out_visit(name, latitude, longitude):
    visit = _get_employee_visit(name)
    _validate_visit_transition(visit, VISIT_IN_PROGRESS, VISIT_COMPLETED)
    latitude, longitude = _validate_coordinates(latitude, longitude)
    visit.db_set(
        {
            "checkout_latitude": latitude,
            "checkout_longitude": longitude,
            "checkout_time": now_datetime(),
            "status": VISIT_COMPLETED,
        }
    )
    return get_visit(visit.name)


@frappe.whitelist()
def cancel_visit(name, cancellation_reason=None):
    visit = _get_employee_visit(name)
    _validate_visit_transition(visit, VISIT_PLANNED, VISIT_CANCELLED)
    visit.db_set(
        {
            "status": VISIT_CANCELLED,
            "cancelled_by": frappe.session.user,
            "cancelled_time": now_datetime(),
            "cancellation_reason": (cancellation_reason or "").strip(),
        }
    )
    return get_visit(visit.name)


def _get_employee_visit(name):
    visit = frappe.get_doc("Visit", name)
    if visit.employee != _current_employee():
        frappe.throw(_("You are not permitted to access this visit."), frappe.PermissionError)
    return visit


def _validate_visit_transition(visit, expected_status, next_status):
    if visit.status != expected_status:
        frappe.throw(_("This visit cannot transition from {0} to {1}.").format(visit.status, next_status))


def _validate_coordinates(latitude, longitude):
    try:
        latitude, longitude = float(latitude), float(longitude)
    except (TypeError, ValueError):
        frappe.throw(_("A valid current location is required."))
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180) or latitude == 0 or longitude == 0:
        frappe.throw(_("A valid current location is required."))
    return latitude, longitude


def _visit_fields():
    return [
        "name",
        "employee",
        "employee_name",
        "visit_date",
        "visit_type",
        "customer",
        "contact_person",
        "address",
        "visit_purpose",
        "remarks",
        "status",
        "planned_latitude",
        "planned_longitude",
        "checkin_latitude",
        "checkin_longitude",
        "checkin_time",
        "checkout_latitude",
        "checkout_longitude",
        "checkout_time",
        "cancelled_by",
        "cancelled_time",
        "cancellation_reason",
    ]

def validate_checkin(doc, method):
    """Hooked into Employee Checkin's validate event via hooks.py
    This validation is now handled by HRMS selfie_validation.py for PWA check-ins.
    Keep this for backward compatibility with dekure's create_checkin API."""
    pass
