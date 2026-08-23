from datetime import datetime

import math
import requests

import frappe
from frappe import _
from frappe.query_builder import Order
from frappe.utils import get_datetime, get_url, getdate, get_time, now_datetime, nowdate


MISSED_CHECKIN = "Missed Check-in"
MISSED_CHECKOUT = "Missed Check-out"
PENDING = "Pending Approval"
VISIT_PLANNED = "Planned"
VISIT_IN_PROGRESS = "In Progress"
VISIT_COMPLETED = "Completed"
VISIT_CANCELLED = "Cancelled"
VISIT_DOCTYPE = "Visit"
VISIT_REVERSE_GEOCODE_URL = "https://nominatim.openstreetmap.org/reverse"
VISIT_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
VISIT_REVERSE_GEOCODE_TIMEOUT = 3
VISIT_NEARBY_ADDRESS_RADIUS_METERS = 800


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
    """Return the attendance state and missed-punch actions for the logged-in employee."""
    employee = _current_employee()
    today = nowdate()
    today_logs = _day_logs(employee, today)
    previous_open_checkout = _open_checkout_date(employee, before_date=today)
    unresolved_open_checkout = _unresolved_open_checkout_date(employee, before_date=today)

    checkout_request_date = previous_open_checkout or today
    today_checkin = _first_log(today_logs, "IN")
    today_checkout = _first_log(today_logs, "OUT")
    pending_missed_checkout = (
        _has_pending_request(employee, previous_open_checkout, MISSED_CHECKOUT)
        if previous_open_checkout
        else None
    )

    return {
        "employee": employee,
        "attendance_date": today,
        "today_checkin_time": today_checkin.time if today_checkin else None,
        "today_checkout_time": today_checkout.time if today_checkout else None,
        "attendance_completed": bool(today_checkin and today_checkout),
        "can_checkin": not unresolved_open_checkout and not today_checkin,
        "can_checkout": bool(today_checkin and not today_checkout),
        "can_request_checkin": not unresolved_open_checkout
        and not _has_log(today_logs, "IN")
        and not _has_pending_request(employee, today, MISSED_CHECKIN),
        "can_request_checkout": (
            previous_open_checkout
            or (_has_log(today_logs, "IN") and not _has_log(today_logs, "OUT"))
        )
        and not _has_pending_request(employee, checkout_request_date, MISSED_CHECKOUT),
        "missed_checkout_date": checkout_request_date if previous_open_checkout else None,
        "blocked_by_checkout_date": unresolved_open_checkout,
        "pending_missed_checkout": bool(pending_missed_checkout),
        "pending_missed_checkout_name": pending_missed_checkout,
        "default_checkout_time": _get_shift_end_time(employee, previous_open_checkout, raise_if_missing=False)
        if previous_open_checkout
        else None,
    }


@frappe.whitelist()
def create_missed_punch_request(request_type, attendance_date, requested_time, reason, latitude=None, longitude=None):
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
    if request_type == MISSED_CHECKOUT:
        latitude, longitude = _validate_coordinates(latitude, longitude)
    existing_checkin = _get_existing_checkin_time(employee, attendance_date)
    submitted_on = now_datetime()

    request = frappe.get_doc(
        {
            "doctype": "Attendance Regularization Request",
            "employee": employee,
            "attendance_date": attendance_date,
            "existing_checkin": existing_checkin,
            "request_type": request_type,
            "requested_time": requested_time,
            "latitude": latitude if request_type == MISSED_CHECKOUT else None,
            "longitude": longitude if request_type == MISSED_CHECKOUT else None,
            "shift": _get_shift(employee, attendance_date),
            "attendance_approver": _get_shift_approver(employee),
            "reason": reason.strip(),
            "status": PENDING,
            "submitted_by": frappe.session.user,
            "submitted_on": submitted_on,
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
    employee_user = frappe.db.get_value("Employee", request.employee, "user_id")
    if frappe.session.user == employee_user:
        frappe.throw(_("You cannot approve or reject your own missed check-out request"), frappe.PermissionError)
    if frappe.session.user != request.attendance_approver:
        frappe.throw(_("Only the assigned Shift Approver can review this request"), frappe.PermissionError)
    if request.status != PENDING:
        frappe.throw(_("This request has already been reviewed"))
    if action == "Reject" and not (rejection_reason or "").strip():
        frappe.throw(_("A rejection reason is required"))

    _lock_employee(request.employee)
    if action == "Approve":
        attendance_date = getdate(request.attendance_date)
        requested_time = get_time(request.requested_time)
        if not requested_time:
            frappe.throw(_("No requested punch time is configured for this request."))
        _validate_missed_punch(
            request.employee, attendance_date, request.request_type, exclude_request=request.name
        )
        _validate_requested_punch_time(
            request.employee, attendance_date, request.request_type, requested_time
        )
        log_type = "IN" if request.request_type == MISSED_CHECKIN else "OUT"
        latitude = longitude = None
        if request.request_type == MISSED_CHECKOUT:
            latitude, longitude = _validate_coordinates(request.latitude, request.longitude)
        checkin = frappe.get_doc(
            {
                "doctype": "Employee Checkin",
                "employee": request.employee,
                "log_type": log_type,
                "time": get_datetime(f"{attendance_date} {requested_time}"),
                "latitude": latitude,
                "longitude": longitude,
            }
        )
        checkin.flags.attendance_regularization_request = request.name
        checkin.insert(ignore_permissions=True)
        request.db_set(
            {
                "status": "Approved",
                "requested_time": requested_time,
                "reviewed_by": frappe.session.user,
                "reviewed_on": now_datetime(),
            }
        )
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


def _day_logs(employee, attendance_date, exclude_checkin=None):
    filters = {
        "employee": employee,
        "time": ["between", [f"{attendance_date} 00:00:00", f"{attendance_date} 23:59:59"]],
    }
    if exclude_checkin:
        filters["name"] = ["!=", exclude_checkin]
    return frappe.get_all(
        "Employee Checkin",
        filters=filters,
        fields=["name", "log_type", "time"],
        order_by="time asc",
    )


def _has_log(logs, log_type):
    return any(log.log_type == log_type for log in logs)


def _first_log(logs, log_type):
    return next((log for log in logs if log.log_type == log_type), None)


def _get_existing_checkin_time(employee, attendance_date):
    checkin = _first_log(_day_logs(employee, attendance_date), "IN")
    return checkin.time if checkin else None


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


def _unresolved_open_checkout_date(employee, before_date=None, exclude_request=None):
    filters = {"employee": employee}
    if before_date:
        filters["time"] = ["<", f"{before_date} 00:00:00"]
    logs = frappe.get_all("Employee Checkin", filters=filters, fields=["log_type", "time"], order_by="time asc")
    open_dates = {}
    for log in logs:
        log_date = getdate(log.time)
        open_dates.setdefault(log_date, {"IN": False, "OUT": False})[log.log_type] = True
    incomplete = [
        date
        for date, types in open_dates.items()
        if types["IN"]
        and not types["OUT"]
        and not _has_pending_request(employee, date, MISSED_CHECKOUT, exclude_request)
    ]
    return max(incomplete).isoformat() if incomplete else None


def _validate_normal_punch(employee, log_type, attendance_date, exclude_checkin=None, exclude_request=None):
    unresolved_open_checkout = _unresolved_open_checkout_date(
        employee, before_date=attendance_date, exclude_request=exclude_request
    )
    if log_type == "IN" and unresolved_open_checkout:
        frappe.throw(_("Complete the missed check-out for {0} before checking in.").format(unresolved_open_checkout))

    logs = _day_logs(employee, attendance_date, exclude_checkin=exclude_checkin)
    if log_type == "IN" and _has_log(logs, "IN"):
        frappe.throw(_("A check-in already exists for this attendance date."))
    if log_type == "IN" and _has_pending_request(employee, attendance_date, MISSED_CHECKIN, exclude_request):
        frappe.throw(_("A missed check-in request for this attendance date is awaiting approval."))
    if log_type == "OUT":
        if not _has_log(logs, "IN"):
            frappe.throw(_("Check in before checking out."))
        if _has_log(logs, "OUT"):
            frappe.throw(_("A check-out already exists for this attendance date."))
        if _has_pending_request(employee, attendance_date, MISSED_CHECKOUT, exclude_request):
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
    requested_datetime = datetime.combine(getdate(attendance_date), get_time(requested_time))
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


def _get_shift_end_time(employee, attendance_date, raise_if_missing=True):
    shift = _get_shift(employee, attendance_date)
    if not shift:
        if not raise_if_missing:
            return None
        frappe.throw(_("No shift is configured for this employee on {0}.").format(attendance_date))
    end_time = frappe.db.get_value("Shift Type", shift, "end_time")
    if not end_time:
        if not raise_if_missing:
            return None
        frappe.throw(_("No end time is configured for shift {0}.").format(shift))
    return get_time(end_time)


def _get_shift_approver(employee):
    employee_details = frappe.db.get_value(
        "Employee",
        employee,
        ["employee_name", "department", "shift_request_approver"],
        as_dict=True,
    )
    approver = employee_details.shift_request_approver
    if approver and frappe.db.get_value("User", {"name": approver, "enabled": 1}, "name"):
        return approver

    if employee_details.department:
        approver = _get_department_shift_approver(employee_details.department)
    if not approver:
        frappe.throw(_("No Shift Request Approver is configured for this employee."))
    return approver


def _get_department_shift_approver(department):
    department_details = frappe.db.get_value("Department", department, ["lft", "rgt"], as_dict=True)
    if not department_details:
        return None

    Department = frappe.qb.DocType("Department")
    DepartmentApprover = frappe.qb.DocType("Department Approver")
    User = frappe.qb.DocType("User")
    approvers = (
        frappe.qb.from_(Department)
        .join(DepartmentApprover)
        .on(DepartmentApprover.parent == Department.name)
        .join(User)
        .on(User.name == DepartmentApprover.approver)
        .select(User.name)
        .where(
            (Department.lft <= department_details.lft)
            & (Department.rgt >= department_details.rgt)
            & (Department.disabled == 0)
            & (DepartmentApprover.parentfield == "shift_request_approver")
            & (User.enabled == 1)
        )
        .orderby(Department.lft, order=Order.desc)
        .limit(1)
    ).run()
    return approvers[0][0] if approvers else None


@frappe.whitelist()
def get_today_visits():
    """Return only the logged-in employee's visits scheduled for today."""
    _ensure_visit_doctype()
    employee = _current_employee()
    return frappe.get_all(
        VISIT_DOCTYPE,
        filters={"employee": employee, "visit_date": nowdate()},
        fields=_visit_fields(),
        order_by="creation asc",
    )


@frappe.whitelist()
def get_visit(name):
    _ensure_visit_doctype()
    visit = _get_employee_visit(name)
    return {field: visit.get(field) for field in _visit_fields()}


@frappe.whitelist()
def create_visit(
    customer=None,
    new_customer=None,
    customer_type=None,
    visit_date=None,
    visit_type=None,
    contact_person=None,
    address=None,
    visit_purpose=None,
    remarks=None,
    **_kwargs,
):
    """Create a planned visit for the current PWA employee only."""
    _ensure_visit_doctype()
    employee = _current_employee()

    if not customer_type:
        customer_type = "New Customer" if (new_customer and not customer) else "Existing Customer"

    customer_val = None
    new_customer_val = None
    display_customer_name = None

    if customer_type == "New Customer":
        if not new_customer:
            frappe.throw(_("New Customer is required"))
        new_customer_val = new_customer
        display_customer_name = (
            frappe.db.get_value("Visit Customer", new_customer, "customer_name") or new_customer
        )
    else:
        customer_type = "Existing Customer"
        if not customer:
            frappe.throw(_("Existing Customer is required"))
        customer_val = customer
        display_customer_name = (
            frappe.db.get_value("Customer", customer, "customer_name") or customer
        )

    visit_date = getdate(visit_date or nowdate())
    if visit_date < getdate(nowdate()):
        frappe.throw(_("A visit cannot be planned for a past date"))

    visit = frappe.get_doc(
        {
            "doctype": VISIT_DOCTYPE,
            "employee": employee,
            "customer_type": customer_type,
            "customer": customer_val,
            "new_customer": new_customer_val,
            "customer_name": display_customer_name,
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
    frappe.share.add(VISIT_DOCTYPE, visit.name, frappe.session.user, read=1)
    return get_visit(visit.name)


@frappe.whitelist()
def create_visit_customer(customer_name, contact_person=None, mobile_no=None, email=None, address=None):
    """Create a new Visit Customer record specifically for PWA visits."""
    if not customer_name or not customer_name.strip():
        frappe.throw(_("Customer Name is required"))

    employee = _current_employee()
    customer_doc = frappe.get_doc(
        {
            "doctype": "Visit Customer",
            "customer_name": customer_name.strip(),
            "contact_person": (contact_person or "").strip() or None,
            "mobile_no": (mobile_no or "").strip() or None,
            "email": (email or "").strip() or None,
            "address": (address or "").strip() or None,
            "employee": employee,
        }
    )
    customer_doc.insert(ignore_permissions=True)
    frappe.share.add("Visit Customer", customer_doc.name, frappe.session.user, read=1, write=1)
    return {
        "ok": True,
        "name": customer_doc.name,
        "customer_name": customer_doc.customer_name,
        "contact_person": customer_doc.contact_person,
        "mobile_no": customer_doc.mobile_no,
        "email": customer_doc.email,
        "address": customer_doc.address,
    }


@frappe.whitelist()
def get_visit_customers(txt=None):
    """Return all PWA Visit Customers for selection."""
    filters = {}
    if txt and txt.strip():
        filters["customer_name"] = ["like", f"%{txt.strip()}%"]

    records = frappe.get_all(
        "Visit Customer",
        filters=filters,
        fields=["name", "customer_name", "contact_person", "mobile_no", "email", "address"],
        order_by="creation desc",
    )
    return [
        {
            "name": r.name,
            "value": r.name,
            "label": r.customer_name or r.name,
            "customer_name": r.customer_name or r.name,
            "contact_person": r.contact_person or "",
            "mobile_no": r.mobile_no or "",
            "email": r.email or "",
            "address": r.address or "",
        }
        for r in records
    ]


@frappe.whitelist()
def punch_in_visit(name, latitude, longitude):
    _ensure_visit_doctype()
    visit = _get_employee_visit(name)
    _validate_visit_transition(visit, VISIT_PLANNED, VISIT_IN_PROGRESS)
    latitude, longitude = _validate_coordinates(latitude, longitude)
    checkin_address = _reverse_geocode_address(latitude, longitude)
    visit.db_set(
        {
            "checkin_latitude": latitude,
            "checkin_longitude": longitude,
            "checkin_address": checkin_address,
            "checkin_time": now_datetime(),
            "status": VISIT_IN_PROGRESS,
        }
    )
    return get_visit(visit.name)


@frappe.whitelist()
def punch_out_visit(name, latitude, longitude):
    _ensure_visit_doctype()
    visit = _get_employee_visit(name)
    _validate_visit_transition(visit, VISIT_IN_PROGRESS, VISIT_COMPLETED)
    latitude, longitude = _validate_coordinates(latitude, longitude)
    checkout_address = _reverse_geocode_address(latitude, longitude)
    visit.db_set(
        {
            "checkout_latitude": latitude,
            "checkout_longitude": longitude,
            "checkout_address": checkout_address,
            "checkout_time": now_datetime(),
            "status": VISIT_COMPLETED,
        }
    )
    return get_visit(visit.name)


@frappe.whitelist()
def cancel_visit(name, cancellation_reason=None):
    _ensure_visit_doctype()
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
    if not name:
        frappe.throw(_("Visit is required"))
    visit = frappe.get_doc(VISIT_DOCTYPE, name)
    if visit.employee != _current_employee():
        frappe.throw(_("You are not permitted to access this visit."), frappe.PermissionError)
    return visit


def _ensure_visit_doctype():
    if not frappe.db.exists("DocType", VISIT_DOCTYPE):
        frappe.clear_cache(doctype=VISIT_DOCTYPE)
        if not frappe.db.exists("DocType", VISIT_DOCTYPE):
            frappe.throw(
                _("Visit DocType is not installed. Run migration for the dekure_custom app and clear cache.")
            )


def _validate_visit_transition(visit, expected_status, next_status):
    if visit.status != expected_status:
        frappe.throw(_("This visit cannot transition from {0} to {1}.").format(visit.status, next_status))


def _validate_coordinates(latitude, longitude):
    try:
        latitude, longitude = float(latitude), float(longitude)
    except (TypeError, ValueError):
        frappe.throw(_("A valid current location is required."))
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180) or (latitude == 0 and longitude == 0):
        frappe.throw(_("A valid current location is required."))
    return latitude, longitude


def _reverse_geocode_address(latitude, longitude):
    try:
        data = _fetch_nominatim_reverse_geocode(latitude, longitude)
        address = _format_nominatim_address(data)
        if _is_specific_visit_address(address, data.get("address")):
            return address

        nearby_address = _get_nearby_osm_address(latitude, longitude, data.get("address") or {})
        return nearby_address or address or (data.get("display_name") or "").strip()
    except Exception:
        try:
            frappe.log_error(
                title="Visit reverse geocoding failed",
                message=frappe.get_traceback(),
            )
        except Exception:
            pass
        return None


def _fetch_nominatim_reverse_geocode(latitude, longitude):
    response = requests.get(
        VISIT_REVERSE_GEOCODE_URL,
        params={
            "lat": latitude,
            "lon": longitude,
            "format": "jsonv2",
            "addressdetails": 1,
            "zoom": 18,
            "layer": "address",
        },
        headers=_visit_geocode_headers(),
        timeout=VISIT_REVERSE_GEOCODE_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def _visit_geocode_headers():
    return {
        "Accept": "application/json",
        "Referer": get_url(),
        "User-Agent": "dekure_custom_visit_reverse_geocoder",
    }


def _format_nominatim_address(data):
    address = data.get("address") or {}
    building = _first_value(address, ["house_number", "building", "amenity", "shop", "office", "tourism", "leisure"])
    road = _first_value(address, ["road", "pedestrian", "footway", "path", "residential", "street"])
    locality = _first_value(
        address,
        [
            "neighbourhood",
            "suburb",
            "quarter",
            "hamlet",
            "village",
            "locality",
            "town",
        ],
    )
    city = _first_value(address, ["city", "town", "municipality"])
    state = address.get("state")
    postcode = address.get("postcode")
    country = address.get("country")

    parts = []
    if building and road:
        parts.append(f"{building}, {road}")
    else:
        _append_unique(parts, building)
        _append_unique(parts, road)
    for value in [locality, _city_postcode(city, postcode) or city, state, country]:
        _append_unique(parts, value)
    return ", ".join(parts)


def _is_specific_visit_address(formatted_address, address):
    if not formatted_address or not address:
        return False
    return bool(
        _first_value(address, ["house_number", "building", "road", "pedestrian", "footway", "path", "residential", "street"])
        or _first_value(address, ["neighbourhood", "suburb", "quarter", "hamlet", "village", "locality"])
    )


def _get_nearby_osm_address(latitude, longitude, base_address):
    try:
        elements = _fetch_nearby_osm_address_elements(latitude, longitude)
        candidate = _select_nearby_osm_address(latitude, longitude, elements)
        if not candidate:
            return None
        return _format_osm_address_tags(candidate["tags"], base_address, _nearest_postcode(latitude, longitude, elements))
    except Exception:
        return None


def _fetch_nearby_osm_address_elements(latitude, longitude):
    radius = VISIT_NEARBY_ADDRESS_RADIUS_METERS
    query = f"""
[out:json][timeout:5];
(
  node(around:{radius},{latitude},{longitude})["addr:full"];
  way(around:{radius},{latitude},{longitude})["addr:full"];
  node(around:{radius},{latitude},{longitude})["addr:street"];
  way(around:{radius},{latitude},{longitude})["addr:street"];
  node(around:{radius},{latitude},{longitude})["addr:place"];
  way(around:{radius},{latitude},{longitude})["addr:place"];
  node(around:{radius},{latitude},{longitude})["addr:postcode"];
  way(around:{radius},{latitude},{longitude})["addr:postcode"];
);
out center tags;
"""
    response = requests.post(
        VISIT_OVERPASS_URL,
        data={"data": query},
        headers=_visit_geocode_headers(),
        timeout=VISIT_REVERSE_GEOCODE_TIMEOUT,
    )
    response.raise_for_status()
    return response.json().get("elements") or []


def _select_nearby_osm_address(latitude, longitude, elements):
    candidates = []
    for element in elements:
        tags = element.get("tags") or {}
        if not _has_useful_osm_address_tags(tags):
            continue
        distance = _element_distance(latitude, longitude, element)
        if distance is None:
            continue
        candidates.append(
            {
                "tags": tags,
                "distance": distance,
                "score": _osm_address_score(tags, distance),
            }
        )
    if not candidates:
        return None
    return sorted(candidates, key=lambda candidate: (-candidate["score"], candidate["distance"]))[0]


def _has_useful_osm_address_tags(tags):
    return bool(tags.get("addr:full") or tags.get("addr:street") or tags.get("addr:place"))


def _osm_address_score(tags, distance):
    score = 0
    if tags.get("addr:full"):
        score += 8
    if tags.get("addr:housenumber"):
        score += 5
    if tags.get("addr:street"):
        score += 4
    if tags.get("addr:place"):
        score += 3
    if tags.get("addr:postcode"):
        score += 2
    return score - (distance / 250)


def _nearest_postcode(latitude, longitude, elements):
    candidates = []
    for element in elements:
        postcode = (element.get("tags") or {}).get("addr:postcode")
        if not postcode:
            continue
        distance = _element_distance(latitude, longitude, element)
        if distance is not None:
            candidates.append((distance, postcode))
    return sorted(candidates)[0][1] if candidates else None


def _format_osm_address_tags(tags, base_address, nearby_postcode=None):
    if tags.get("addr:full"):
        specific_parts = _split_address_text(tags.get("addr:full"))
    else:
        specific_parts = []
        if tags.get("addr:housenumber") and tags.get("addr:street"):
            specific_parts.append(f"{tags.get('addr:housenumber')}, {tags.get('addr:street')}")
        else:
            _append_unique(specific_parts, tags.get("addr:housenumber"))
            _append_unique(specific_parts, tags.get("addr:street"))
        for part in _split_address_text(tags.get("addr:place")):
            _append_unique(specific_parts, part)

    city = tags.get("addr:city") or _first_value(base_address, ["city", "town", "municipality"])
    state = tags.get("addr:state") or base_address.get("state")
    postcode = tags.get("addr:postcode") or nearby_postcode
    country = tags.get("addr:country") or base_address.get("country")
    parts = []
    for value in specific_parts:
        _append_unique(parts, value)
    for value in [_city_postcode(city, postcode) or city, state, country]:
        _append_unique(parts, value)
    return ", ".join(parts)


def _split_address_text(value):
    if not value:
        return []
    return [part.strip() for part in str(value).split(",") if part and part.strip()]


def _city_postcode(city, postcode):
    return f"{city} {postcode}" if city and postcode else None


def _first_value(mapping, keys):
    for key in keys:
        value = mapping.get(key)
        if value:
            return str(value).strip()
    return None


def _append_unique(parts, value):
    value = str(value).strip() if value is not None else ""
    if not value:
        return
    normalized = _normalize_address_part(value)
    if normalized and normalized not in {_normalize_address_part(part) for part in parts}:
        parts.append(value)


def _normalize_address_part(value):
    return " ".join(str(value).lower().replace(".", "").split())


def _element_distance(latitude, longitude, element):
    element_latitude = element.get("lat") or (element.get("center") or {}).get("lat")
    element_longitude = element.get("lon") or (element.get("center") or {}).get("lon")
    if element_latitude is None or element_longitude is None:
        return None
    return _haversine_distance(latitude, longitude, float(element_latitude), float(element_longitude))


def _haversine_distance(latitude, longitude, other_latitude, other_longitude):
    radius = 6371000
    lat_delta = math.radians(other_latitude - latitude)
    lon_delta = math.radians(other_longitude - longitude)
    a = (
        math.sin(lat_delta / 2) ** 2
        + math.cos(math.radians(latitude))
        * math.cos(math.radians(other_latitude))
        * math.sin(lon_delta / 2) ** 2
    )
    return radius * 2 * math.asin(math.sqrt(a))


def _visit_fields():
    return [
        "name",
        "employee",
        "employee_name",
        "visit_date",
        "customer_type",
        "customer",
        "new_customer",
        "customer_name",
        "contact_person",
        "address",
        "visit_type",
        "visit_purpose",
        "remarks",
        "status",
        "planned_latitude",
        "planned_longitude",
        "checkin_latitude",
        "checkin_longitude",
        "checkin_address",
        "checkin_time",
        "checkout_latitude",
        "checkout_longitude",
        "checkout_address",
        "checkout_time",
        "cancelled_by",
        "cancelled_time",
        "cancellation_reason",
    ]

def validate_checkin(doc, method):
    """Hooked into Employee Checkin's validate event via hooks.py
    This adds duplicate/state protection without changing selfie/GPS capture."""
    if not doc.employee or doc.log_type not in ("IN", "OUT"):
        return

    attendance_date = getdate(doc.time or now_datetime())
    _lock_employee(doc.employee)
    _validate_normal_punch(
        doc.employee,
        doc.log_type,
        attendance_date,
        exclude_checkin=doc.name if not doc.is_new() else None,
        exclude_request=doc.flags.get("attendance_regularization_request"),
    )
