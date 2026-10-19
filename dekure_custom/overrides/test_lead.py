import unittest
from unittest.mock import Mock, patch

from dekure_custom.overrides.lead import DraftQuotationStatusMixin


class BaseLead:
	def has_quotation(self):
		return self.submitted_quotation


class ExtendedLead(DraftQuotationStatusMixin, BaseLead):
	name = "LEAD-1"


class TestDraftQuotationStatus(unittest.TestCase):
	def test_submitted_rule_is_preserved(self):
		lead = ExtendedLead()
		lead.submitted_quotation = "SUBMITTED-QUOTATION"
		with patch("dekure_custom.overrides.lead.frappe.db", new=Mock()) as db:
			self.assertEqual(lead.has_quotation(), "SUBMITTED-QUOTATION")
		db.get_value.assert_not_called()

	def test_draft_lookup_is_restricted_to_linked_lead(self):
		lead = ExtendedLead()
		lead.submitted_quotation = None
		with patch("dekure_custom.overrides.lead.frappe.db", new=Mock()) as db:
			db.get_value.return_value = "DRAFT-QUOTATION"
			self.assertEqual(lead.has_quotation(), "DRAFT-QUOTATION")
		db.get_value.assert_called_once_with(
			"Quotation",
			{"quotation_to": "Lead", "party_name": "LEAD-1", "docstatus": 0, "status": ["!=", "Lost"]},
		)
