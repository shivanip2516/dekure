# Copyright (c) 2026, vigisolvo and Contributors
# See license.txt

import unittest
from unittest.mock import patch

from frappe.model.naming import NamingSeries, parse_naming_series
from dekure_custom.dekure_custom.doctype.visit_customer.visit_customer import VisitCustomer


class TestVisitCustomer(unittest.TestCase):
	def test_naming_series_preview(self):
		ns = NamingSeries("VC-.YYYY.-.#####")
		counter = 0

		def mock_counter(prefix, digits):
			nonlocal counter
			counter += 1
			return str(counter).zfill(digits)

		id_1 = parse_naming_series(ns.series, number_generator=mock_counter)
		id_2 = parse_naming_series(ns.series, number_generator=mock_counter)
		id_3 = parse_naming_series(ns.series, number_generator=mock_counter)

		self.assertEqual(id_1, "VC-2026-00001")
		self.assertEqual(id_2, "VC-2026-00002")
		self.assertEqual(id_3, "VC-2026-00003")

	def test_visit_customer_autoname(self):
		counter = 0

		def mock_counter(prefix, digits):
			nonlocal counter
			counter += 1
			return str(counter).zfill(digits)

		with patch("frappe.model.naming.getseries", side_effect=mock_counter):
			doc1 = VisitCustomer({"doctype": "Visit Customer", "customer_name": "Shivani New Customer"})
			doc1.autoname()
			self.assertEqual(doc1.name, "VC-2026-00001")

			doc2 = VisitCustomer({"doctype": "Visit Customer", "customer_name": "ABC Customer"})
			doc2.autoname()
			self.assertEqual(doc2.name, "VC-2026-00002")

			doc3 = VisitCustomer({"doctype": "Visit Customer", "customer_name": "XYZ Customer"})
			doc3.autoname()
			self.assertEqual(doc3.name, "VC-2026-00003")


if __name__ == "__main__":
	import frappe
	frappe.local.valid_columns = {}
	frappe.local.flags = frappe._dict()
	frappe.get_system_settings = lambda x: "Asia/Kolkata"
	frappe.get_hooks = lambda x, d=None: {} if d is None else d
	frappe.get_meta = lambda doctype: frappe._dict(
		autoname="VC-.YYYY.-.#####",
		naming_rule="Expression (old style)",
		get_table_fields=lambda: [],
		get_valid_columns=lambda: ["name", "customer_name", "doctype"],
		issingle=0,
		istable=0,
	)
	unittest.main()
