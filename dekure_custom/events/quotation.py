import frappe
from frappe import _


def update_lead_on_creation(doc, method=None):
	"""Count the first Draft save as a quotation without changing later lifecycle events."""
	if doc.docstatus != 0 or doc.quotation_to != "Lead" or not doc.party_name:
		return

	lead = frappe.get_doc("Lead", doc.party_name)
	# ERPNext gives customer conversion precedence over a quotation.
	if lead.has_customer():
		lead.set_status(update=True)
		return

	if lead.status != "Quotation":
		# Match ERPNext's status updater, including its status-history label.
		lead.add_comment("Label", _("Quotation"))
		lead.db_set("status", "Quotation")
