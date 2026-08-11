import frappe

def get_context(context):
    if frappe.session.user == "Guest":
        frappe.throw("Please login to check in", frappe.PermissionError)
    context.no_cache = 1