import frappe


class DraftQuotationStatusMixin:
	def has_opportunity(self):
		# Preserve the standard direct-Lead rule and its priority in get_status().
		direct = super().has_opportunity()
		if direct or self.status not in ("Prospect", "Opportunity"):
			return direct
		prospect = frappe.db.get_value(
			"Prospect Lead",
			{"lead": self.name, "parenttype": "Prospect", "parentfield": "leads"},
			"parent",
		)
		if prospect:
			return frappe.db.get_value(
				"Opportunity",
				{"opportunity_from": "Prospect", "party_name": prospect, "status": ["!=", "Lost"]},
			)

	def has_quotation(self):
		# Keep ERPNext's submitted-quotation rules and include saved Drafts.
		return super().has_quotation() or frappe.db.get_value(
			"Quotation",
			{
				"quotation_to": "Lead",
				"party_name": self.name,
				"docstatus": 0,
				"status": ["!=", "Lost"],
			},
		)


def ensure_prospect_status_option():
	"""Place Prospect after Replied without reordering other options or changing defaults."""
	from frappe.custom.doctype.property_setter.property_setter import make_property_setter

	options = frappe.get_meta("Lead", cached=False).get_field("status").options or ""
	values = [value for value in options.split("\n") if value != "Prospect"]
	if "Replied" in values:
		position = values.index("Replied") + 1
	elif "Opportunity" in values:
		position = values.index("Opportunity")
	else:
		position = len(values)
	values.insert(position, "Prospect")
	updated_options = "\n".join(values)
	if updated_options != options:
		make_property_setter("Lead", "status", "options", updated_options, "Text")


def update_prospect_lead_status(doc, method=None):
	"""Only new links may promote pre-sales Leads; existing links respect manual edits."""
	previous = doc.get_doc_before_save()
	previous_leads = {row.lead for row in previous.leads} if previous else set()
	for name in {row.lead for row in doc.leads if row.lead}:
		lead = frappe.get_doc("Lead", name)
		if name not in previous_leads and lead.status in ("Lead", "Open"):
			# Existing set_status checks customers/quotations/opportunities first,
			# records the status label and synchronizes the Prospect Lead row.
			lead.set_status(update=True, status="Prospect")
		elif lead.status == "Prospect":
			lead.set_status(update=True)


def update_prospect_opportunity_leads(doc, method=None):
	"""Direct-Lead Opportunities already have a standard controller callback."""
	if doc.opportunity_from != "Prospect" or not doc.party_name:
		return
	for name in frappe.get_all(
		"Prospect Lead",
		filters={"parent": doc.party_name, "parenttype": "Prospect", "parentfield": "leads"},
		pluck="lead",
	):
		lead = frappe.get_doc("Lead", name)
		if lead.status in ("Prospect", "Opportunity"):
			lead.set_status(update=True)
