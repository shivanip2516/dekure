import frappe


class DraftQuotationStatusMixin:
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
