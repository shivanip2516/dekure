import frappe
from frappe.model.document import Document


class Services(Document):
	def before_insert(self):
		if not self.name:
			self.name = frappe.generate_hash(length=10)

