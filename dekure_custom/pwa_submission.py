import frappe
from frappe import _


EMPLOYEE_REQUEST_DOCTYPES = {
	"Attendance Request",
	"Compensatory Leave Request",
	"Employee Advance",
	"Expense Claim",
	"Leave Application",
	"Shift Assignment",
	"Shift Request",
}

PWA_SUBMISSION_COMMANDS = {
	"frappe.client.insert",
	"frappe.client.save",
	"frappe.client.set_value",
	"frappe.client.submit",
	"frappe.model.workflow.apply_workflow",
}

SUBMISSION_ERROR = "Employee requests can only be saved from the HRMS app. Submit them through ERPNext Desk."


def prevent_employee_pwa_submission(doc, method=None):
	"""Reject submission of the current employee's request through PWA-facing APIs."""
	if doc.doctype not in EMPLOYEE_REQUEST_DOCTYPES or not _is_pwa_submission_request():
		return

	employee = doc.get("employee")
	if not employee:
		return

	current_user_employees = frappe.get_all(
		"Employee",
		filters={"user_id": frappe.session.user},
		pluck="name",
	)
	if employee in current_user_employees:
		frappe.throw(_(SUBMISSION_ERROR), frappe.PermissionError)


def _is_pwa_submission_request():
	form_dict = getattr(frappe.local, "form_dict", None) or {}
	if form_dict.get("cmd") in PWA_SUBMISSION_COMMANDS:
		return True

	request = getattr(frappe.local, "request", None)
	return bool(request and str(getattr(request, "path", "")).startswith("/api/resource/"))
