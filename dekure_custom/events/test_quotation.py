import unittest
from unittest.mock import Mock, patch

import frappe

from dekure_custom.events.quotation import update_lead_on_creation
from dekure_custom.hooks import doc_events


class TestQuotationLeadStatus(unittest.TestCase):
	def setUp(self):
		self.doc = frappe._dict(docstatus=0, quotation_to="Lead", party_name="TEST-LEAD")
		self.lead = Mock(status="Opportunity")
		self.lead.has_customer.return_value = False
		self.lookup = patch("dekure_custom.events.quotation.frappe.get_doc", return_value=self.lead)
		self.get_doc = self.lookup.start()
		self.addCleanup(self.lookup.stop)

	def test_first_draft_save_updates_only_linked_lead(self):
		with patch("dekure_custom.events.quotation._", side_effect=lambda value: value):
			update_lead_on_creation(self.doc)
		self.get_doc.assert_called_once_with("Lead", "TEST-LEAD")
		self.lead.db_set.assert_called_once_with("status", "Quotation")
		self.lead.add_comment.assert_called_once_with("Label", "Quotation")

	def test_already_quoted_lead_is_not_written_again(self):
		self.lead.status = "Quotation"
		update_lead_on_creation(self.doc)
		self.lead.db_set.assert_not_called()
		self.lead.add_comment.assert_not_called()

	def test_customer_conversion_keeps_standard_precedence(self):
		self.lead.has_customer.return_value = True
		update_lead_on_creation(self.doc)
		self.lead.set_status.assert_called_once_with(update=True)
		self.lead.db_set.assert_not_called()

	def test_customer_or_missing_lead_is_ignored(self):
		for quotation_to, party_name in [("Customer", "TEST-LEAD"), ("Lead", None), ("Prospect", "X")]:
			with self.subTest(quotation_to=quotation_to, party_name=party_name):
				self.doc.update(quotation_to=quotation_to, party_name=party_name)
				update_lead_on_creation(self.doc)
		self.get_doc.assert_not_called()

	def test_submitted_and_cancelled_documents_are_ignored(self):
		for docstatus in (1, 2):
			self.doc.docstatus = docstatus
			update_lead_on_creation(self.doc)
		self.get_doc.assert_not_called()

	def test_one_hook_covers_creation_and_existing_draft_saves(self):
		self.assertEqual(
			doc_events["Quotation"],
			{"on_update": "dekure_custom.events.quotation.update_lead_on_creation"},
		)
