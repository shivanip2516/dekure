# Copyright (c) 2026, vigisolvo and contributors
# For license information, please see license.txt

from frappe.model.document import Document
from frappe.model.naming import make_autoname


class VisitCustomer(Document):
	def autoname(self):
		if not self.name:
			self.name = make_autoname("VC-.YYYY.-.#####")
