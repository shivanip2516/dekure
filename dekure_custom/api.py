import frappe
from frappe.utils import now_datetime


@frappe.whitelist()
def create_checkin(log_type, selfie, latitude=None, longitude=None):
    """Called from the /checkin page when employee taps Check In / Check Out"""
    if not selfie:
        return {"ok": False, "error": "Selfie required"}

    employee = frappe.db.get_value("Employee", {"user_id": frappe.session.user}, "name")
    if not employee:
        return {"ok": False, "error": "No employee linked to your login"}

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

def validate_checkin(doc, method):
    """Hooked into Employee Checkin's validate event via hooks.py"""
    if "HR Manager" in frappe.get_roles(frappe.session.user):
        return
    if not doc.custom_selfie:
        frappe.throw("A selfie is required to check in or check out")