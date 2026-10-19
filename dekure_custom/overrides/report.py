import frappe
from frappe import _
from frappe.core.utils import html_to_plain_text
from frappe.desk import reportview
from frappe.model import get_permitted_fields
from frappe.utils import get_datetime


@frappe.whitelist()
@frappe.read_only()
def get_lead_reportview():
	"""Enrich the existing Report Builder response without joining its Lead rows."""
	if frappe.form_dict.get("doctype") != "Lead":
		frappe.throw(_("This report is only available for Leads."), frappe.PermissionError)

	result = reportview.get()
	if not result or "name" not in result["keys"]:
		return result

	name_index = result["keys"].index("name")
	notes = get_lead_notes({row[name_index] for row in result["values"] if row[name_index]})
	result["keys"].append("_lead_notes")
	for row in result["values"]:
		row.append(notes.get(row[name_index], ""))
	return result


class LeadNotesReportMixin:
	def execute_module(self, filters):
		result = super().execute_module(filters)
		if self.name != "Lead Details":
			return result

		columns, rows, *extra = result
		notes = get_lead_notes({row["name"] for row in rows})
		columns = [
			*columns,
			{"label": _("Notes"), "fieldname": "notes", "fieldtype": "Small Text", "width": 350},
		]
		return columns, [dict(row, notes=notes.get(row["name"], "")) for row in rows], *extra


def get_lead_notes(lead_names):
	"""Read child notes in bulk, restricted to Leads and fields the user can read."""
	if not lead_names or not frappe.has_permission("Lead", "read"):
		return {}

	meta = frappe.get_meta("Lead")
	if meta.get_field("notes").permlevel not in meta.get_permlevel_access("read"):
		return {}
	if "note" not in get_permitted_fields("CRM Note", parenttype="Lead", permission_type="read"):
		return {}

	readable_leads = frappe.get_list(
		"Lead", filters={"name": ["in", sorted(lead_names)]}, pluck="name", limit_page_length=0
	)
	if not readable_leads:
		return {}

	# CRM Note inherits access from its parent; only query permission-filtered Leads.
	rows = frappe.get_all(
		"CRM Note",
		filters={"parenttype": "Lead", "parentfield": "notes", "parent": ["in", readable_leads]},
		fields=["parent", "note", "added_on", "creation", "idx", "name"],
		order_by="parent asc, idx desc",
	)
	# added_on is the standard CRM note timestamp; legacy rows may only have creation.
	latest = {}

	def sequence(row):
		return (get_datetime(row.added_on or row.creation or "0001-01-01"), row.idx or 0, row.name or "")

	for row in rows:
		if row.parent not in latest or sequence(row) > sequence(latest[row.parent]):
			latest[row.parent] = row
	return {lead: html_to_plain_text(row.note).strip() for lead, row in latest.items()}
