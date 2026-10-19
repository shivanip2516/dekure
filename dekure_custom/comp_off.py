import frappe
from frappe import _
from frappe.utils import cint

from hrms.hr.doctype.leave_application.leave_application import get_employee_leave_approver
from hrms.hr.utils import share_doc_with_approver


PWA_MARKER_FIELD = "custom_pwa_comp_off"
STATUS_FIELD = "custom_status"
APPROVER_FIELD = "custom_leave_approver"
DRAFT = "Draft"
PENDING_APPROVAL = "Pending Approval"
APPROVED = "Approved"
REJECTED = "Rejected"
VALID_STATUSES = {DRAFT, PENDING_APPROVAL, APPROVED, REJECTED}
SUBMISSION_ERROR = "Compensatory Off cannot be submitted until it has been approved by the authorized approver."


def prepare_pwa_comp_off(doc, method=None):
	# Do not interfere with cancellation or recovery of historical submitted rows,
	# including the legacy rows whose business status was never synchronized.
	if doc.docstatus == 2:
		return

	if doc.docstatus == 1 and doc.get(STATUS_FIELD) != APPROVED:
		frappe.throw(_(SUBMISSION_ERROR))

	stored_status = None
	stored_approver = None
	if not doc.is_new():
		stored_status, stored_approver = frappe.db.get_value(
			doc.doctype,
			doc.name,
			[STATUS_FIELD, APPROVER_FIELD],
		)
		doc.set(APPROVER_FIELD, stored_approver or get_employee_leave_approver(doc.employee))
		_validate_status_transition(doc, stored_status)
	else:
		# Neither the Desk nor an API client may choose an approval outcome at creation.
		doc.set(STATUS_FIELD, DRAFT)
		doc.set(APPROVER_FIELD, get_employee_leave_approver(doc.employee))

	requester_users = _employee_users(doc.employee)
	if doc.is_new() and _is_pwa_comp_off(doc) and frappe.session.user in requester_users:
		# The existing PWA Save action doubles as "Send for Approval".
		doc.set(PWA_MARKER_FIELD, 1)
		doc.set(STATUS_FIELD, PENDING_APPROVAL)

	if doc.get(STATUS_FIELD) == PENDING_APPROVAL and not doc.get(APPROVER_FIELD):
		frappe.throw(_("Please configure a Leave Approver for employee {0}.").format(doc.employee))


def notify_pwa_comp_off_approver(doc, method=None):
	if doc.get(STATUS_FIELD) != PENDING_APPROVAL:
		return

	share_doc_with_approver(doc, doc.get(APPROVER_FIELD))
	employee_user = frappe.db.get_value("Employee", doc.employee, "user_id", cache=True)
	if not employee_user or employee_user == doc.get(APPROVER_FIELD):
		return

	_create_notification(
		from_user=employee_user,
		to_user=doc.get(APPROVER_FIELD),
		message=f"<b>{doc.employee_name}</b> raised a new <b>{doc.doctype}</b> for approval: {doc.name}",
		doc=doc,
		action_type="Approval",
	)


def notify_pwa_comp_off_status(doc, method=None):
	if not cint(doc.get(PWA_MARKER_FIELD)):
		return

	previous = doc.get_doc_before_save()
	status = doc.get(STATUS_FIELD)
	if not previous or previous.get(STATUS_FIELD) == status or status not in (APPROVED, REJECTED):
		return

	employee_user = frappe.db.get_value("Employee", doc.employee, "user_id", cache=True)
	if not employee_user or employee_user == frappe.session.user:
		return

	_create_notification(
		from_user=frappe.session.user,
		to_user=employee_user,
		message=f"<b>Your</b> <b>{doc.doctype}</b> {doc.name} has been <b>{status}</b>",
		doc=doc,
	)


def prevent_pwa_employee_comp_off_submission(doc, method=None):
	from dekure_custom.pwa_submission import prevent_employee_pwa_submission

	prevent_employee_pwa_submission(doc)

	if doc.get(STATUS_FIELD) != APPROVED:
		frappe.throw(_(SUBMISSION_ERROR))

	if frappe.session.user != doc.get(APPROVER_FIELD):
		frappe.throw(
			_("Only the configured Leave Approver can submit this Comp-Off request."),
			frappe.PermissionError,
		)

	# A requester cannot use the PWA's generic document endpoint to submit, even
	# when that requester is also configured as their own approver. The controlled
	# review action below remains the only self-approval path.
	if (
		_is_pwa_comp_off(doc)
		and frappe.session.user in _employee_users(doc.employee)
		and not doc.flags.get("pwa_comp_off_review")
	):
		frappe.throw(
			_("Comp-Off requests created in the HRMS app cannot be submitted by an employee."),
			frappe.PermissionError,
		)


@frappe.whitelist()
def request_comp_off_approval(name: str):
	doc = frappe.get_doc("Compensatory Leave Request", name)
	if doc.docstatus != 0 or doc.get(STATUS_FIELD) != DRAFT:
		frappe.throw(_("Only a Draft Comp-Off request can be sent for approval."))
	if frappe.session.user not in _employee_users(doc.employee):
		frappe.throw(_("Only the employee can send this Comp-Off request for approval."), frappe.PermissionError)

	doc.check_permission("write")
	doc.flags.comp_off_request_approval = True
	doc.set(STATUS_FIELD, PENDING_APPROVAL)
	doc.save()
	notify_pwa_comp_off_approver(doc)

	return {"name": doc.name, "status": doc.get(STATUS_FIELD), "docstatus": doc.docstatus}


@frappe.whitelist()
def review_pwa_comp_off(name: str, action: str):
	doc = frappe.get_doc("Compensatory Leave Request", name)
	if doc.docstatus != 0 or doc.get(STATUS_FIELD) != PENDING_APPROVAL:
		frappe.throw(_("This Comp-Off request is no longer pending approval."))
	if frappe.session.user != doc.get(APPROVER_FIELD):
		frappe.throw(_("You are not the configured Leave Approver for this request."), frappe.PermissionError)

	action = (action or "").strip().title()
	if action not in (APPROVED, REJECTED):
		frappe.throw(_("Action must be Approved or Rejected."))

	doc.flags.pwa_comp_off_review = True
	doc.set(STATUS_FIELD, action)
	doc.flags.ignore_permissions = True
	if action == APPROVED:
		doc.submit()
	else:
		doc.save()

	return {"name": doc.name, "status": doc.get(STATUS_FIELD), "docstatus": doc.docstatus}


def _validate_status_transition(doc, stored_status):
	status = doc.get(STATUS_FIELD)
	if status == stored_status:
		return

	if status not in VALID_STATUSES:
		frappe.throw(_("Invalid Comp-Off status: {0}").format(status or _("Empty")))

	if doc.flags.get("comp_off_request_approval"):
		valid = (
			stored_status == DRAFT
			and status == PENDING_APPROVAL
			and frappe.session.user in _employee_users(doc.employee)
		)
	elif doc.flags.get("pwa_comp_off_review"):
		valid = (
			stored_status == PENDING_APPROVAL
			and status in (APPROVED, REJECTED)
			and frappe.session.user == doc.get(APPROVER_FIELD)
		)
	else:
		valid = False

	if not valid:
		frappe.throw(
			_("Comp-Off Status can only be changed through the authorized approval actions."),
			frappe.PermissionError,
		)


def _is_pwa_comp_off(doc):
	if cint(doc.get(PWA_MARKER_FIELD)):
		return True
	if not doc.get("name") or doc.is_new():
		return False
	return cint(frappe.db.get_value(doc.doctype, doc.name, PWA_MARKER_FIELD))


def _employee_users(employee):
	return frappe.get_all("Employee", filters={"name": employee}, pluck="user_id")


def _create_notification(from_user, to_user, message, doc, action_type=None):
	notification = frappe.new_doc("PWA Notification")
	notification.from_user = from_user
	notification.to_user = to_user
	notification.message = message
	notification.reference_document_type = doc.doctype
	notification.reference_document_name = doc.name
	notification.custom_action_type = action_type
	notification.insert(ignore_permissions=True)
