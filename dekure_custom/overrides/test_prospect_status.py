"""Exercise the installed ERPNext status calculator without writing site data."""

import itertools
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import frappe
import erpnext
from erpnext.crm.doctype.lead.lead import Lead
from erpnext.crm.doctype.prospect.prospect import Prospect
from erpnext.crm.utils import STATUSES_REOPENED_BY_EVENT

from dekure_custom.hooks import after_install, after_migrate, doc_events
from dekure_custom.overrides.lead import (
	DraftQuotationStatusMixin,
	ensure_prospect_status_option,
	update_prospect_lead_status,
	update_prospect_opportunity_leads,
)


class ExtendedLead(DraftQuotationStatusMixin, Lead):
	pass


class TestProspectStatus(unittest.TestCase):
	def setUp(self):
		self.customer = self.quotation = self.lost_quotation = self.direct = self.indirect = False
		self.linked = True
		self.leads = {}
		self.db = Mock()
		self.db.get_value.side_effect = self.get_value
		self.start_patch("frappe.db", new=self.db)
		self.start_patch("frappe.get_doc", side_effect=lambda doctype, name: self.leads[name])
		self.start_patch("erpnext.controllers.status_updater._", side_effect=lambda value: value)

	def start_patch(self, target, **kwargs):
		patcher = patch(target, **kwargs)
		self.addCleanup(patcher.stop)
		return patcher.start()

	def get_value(self, doctype, filters, *args, **kwargs):
		if doctype == "Customer":
			return "CUSTOMER" if self.customer else None
		if doctype == "Quotation":
			active = self.lost_quotation if filters.get("status") == "Lost" else self.quotation
			return "QUOTATION" if active else None
		if doctype == "Prospect Lead":
			self.assertEqual(filters, {"lead": "L1", "parenttype": "Prospect", "parentfield": "leads"})
			return "P1" if self.linked else None
		if doctype == "Opportunity":
			if filters.get("opportunity_from") == "Prospect":
				self.assertEqual(filters, {
					"opportunity_from": "Prospect", "party_name": "P1", "status": ["!=", "Lost"]
				})
				return "INDIRECT" if self.indirect else None
			return "DIRECT" if self.direct else None
		self.fail(f"Unexpected lookup: {doctype}")

	def lead(self, status="Lead", cls=ExtendedLead, name="L1", is_new=False):
		# Bypass metadata initialization only; execute actual ERPNext Lead methods.
		lead = object.__new__(cls)
		lead.__dict__.update(doctype="Lead", name=name, status=status, flags=frappe._dict())
		lead.__dict__["__islocal"] = is_new
		lead.as_dict = lambda: frappe._dict(status=lead.status)
		lead.add_comment = Mock()
		lead.db_set = Mock()
		lead.update_prospect = Mock()
		self.leads[name] = lead
		return lead

	def prospect(self, leads=("L1",), previous=None):
		return Mock(
			leads=[frappe._dict(lead=name) for name in leads],
			get_doc_before_save=Mock(return_value=previous),
		)

	def test_new_lead_keeps_default_and_does_not_query_relationship(self):
		path = Path(erpnext.__file__).parent / "crm/doctype/lead/lead.json"
		field = next(field for field in json.loads(path.read_text())["fields"] if field["fieldname"] == "status")
		default = field.get("default") or field["options"].splitlines()[0]
		lead = self.lead(default, is_new=True)
		lead.set_status()
		self.assertEqual(lead.status, default)
		self.db.get_value.assert_not_called()
		lead.db_set.assert_not_called()

	def test_no_prospect_matches_existing_calculator_for_every_standard_status(self):
		self.linked = False
		for status in self.standard_statuses():
			with self.subTest(status=status):
				baseline = self.lead(status, cls=Lead)
				lead = self.lead(status)
				baseline.set_status()
				lead.set_status()
				self.assertEqual(lead.status, baseline.status)

	@staticmethod
	def standard_statuses():
		path = Path(erpnext.__file__).parent / "crm/doctype/lead/lead.json"
		return next(f["options"].splitlines() for f in json.loads(path.read_text())["fields"] if f["fieldname"] == "status")

	def test_new_prospect_promotes_only_eligible_leads_and_deduplicates(self):
		lead = self.lead()
		other = self.lead("Interested", name="L2")
		unrelated = self.lead("Lead", name="L3")
		update_prospect_lead_status(self.prospect(("L1", "L1", "L2")))
		self.assertEqual(lead.status, "Prospect")
		lead.db_set.assert_called_once_with("status", "Prospect", update_modified=True)
		lead.update_prospect.assert_called_once()
		self.assertEqual(other.status, "Interested")
		other.db_set.assert_not_called()
		unrelated.db_set.assert_not_called()

	def test_open_lead_is_eligible(self):
		lead = self.lead("Open")
		update_prospect_lead_status(self.prospect())
		self.assertEqual(lead.status, "Prospect")

	def test_other_manual_or_advanced_stages_are_not_downgraded(self):
		for status in [*self.standard_statuses(), "Custom Stage"]:
			if status in ("Lead", "Open"):
				continue
			with self.subTest(status=status):
				lead = self.lead(status)
				update_prospect_lead_status(self.prospect())
				self.assertEqual(lead.status, status)
				lead.db_set.assert_not_called()

	def test_existing_customer_quotation_and_opportunity_take_precedence(self):
		for attribute, expected in (("customer", "Converted"), ("quotation", "Quotation"),
			("lost_quotation", "Lost Quotation"), ("direct", "Opportunity"), ("indirect", "Opportunity")):
			with self.subTest(attribute=attribute):
				setattr(self, attribute, True)
				lead = self.lead()
				update_prospect_lead_status(self.prospect())
				self.assertEqual(lead.status, expected)
				setattr(self, attribute, False)

	def test_repeated_lead_and_prospect_saves_retain_status_without_duplicate_writes(self):
		lead = self.lead()
		doc = self.prospect()
		update_prospect_lead_status(doc)
		doc.get_doc_before_save.return_value = self.prospect()
		for _ in range(3):
			lead.set_status(update=True)
			update_prospect_lead_status(doc)
		self.assertEqual(lead.status, "Prospect")
		lead.db_set.assert_called_once()
		lead.add_comment.assert_called_once()

	def test_existing_link_does_not_reapply_prospect_after_manual_status_change(self):
		for status in self.standard_statuses():
			lead = self.lead(status)
			update_prospect_lead_status(self.prospect(previous=self.prospect()))
			self.assertEqual(lead.status, status)
			lead.db_set.assert_not_called()

	def test_direct_opportunity_uses_existing_status_transition(self):
		lead = self.lead("Prospect")
		self.direct = True
		lead.set_status(update=True)
		self.assertEqual(lead.status, "Opportunity")

	def test_unsaved_opportunity_does_not_change_lead(self):
		lead = self.lead("Prospect")
		# No database Opportunity exists and no on_update event has run.
		lead.set_status(update=True)
		self.assertEqual(lead.status, "Prospect")
		lead.db_set.assert_not_called()

	def test_lead_validation_and_followup_rules_preserve_prospect(self):
		lead = self.lead("Prospect")
		lead.lead_owner = None
		for method in ("set_full_name", "set_lead_name", "set_title",
			"check_email_id_is_unique", "validate_email_id"):
			setattr(lead, method, Mock())
		lead.validate()
		self.assertEqual(lead.status, "Prospect")
		self.assertNotIn("Prospect", STATUSES_REOPENED_BY_EVENT["Lead"])
		lead.db_set.assert_not_called()

	def test_failed_prospect_validation_never_updates_lead(self):
		lead = self.lead()
		prospect = object.__new__(Prospect)
		prospect.__dict__.update(name="P1", leads=[frappe._dict(lead="L1"), frappe._dict(lead="L1")])
		with patch("frappe.throw", side_effect=frappe.ValidationError), patch(
			"erpnext.crm.doctype.prospect.prospect._", side_effect=lambda value: value
		):
			with self.assertRaises(frappe.ValidationError):
				prospect.validate()
		self.assertEqual(lead.status, "Lead")
		lead.db_set.assert_not_called()

	def test_prospect_opportunity_updates_linked_lead_and_survives_routine_save(self):
		lead = self.lead("Prospect")
		self.indirect = True
		with patch("frappe.get_all", return_value=["L1"]) as lookup:
			update_prospect_opportunity_leads(frappe._dict(opportunity_from="Prospect", party_name="P1"))
		lookup.assert_called_once_with("Prospect Lead", filters={
			"parent": "P1", "parenttype": "Prospect", "parentfield": "leads"
		}, pluck="lead")
		lead.set_status(update=True)
		self.assertEqual(lead.status, "Opportunity")
		lead.db_set.assert_called_once()

	def test_indirect_transition_does_not_affect_other_stages_or_unrelated_leads(self):
		for status in ("Lead", "Open", "Replied", "Interested", "Converted", "Do Not Contact", "Custom"):
			lead = self.lead(status)
			with patch("frappe.get_all", return_value=["L1"]):
				update_prospect_opportunity_leads(frappe._dict(opportunity_from="Prospect", party_name="P1"))
			self.assertEqual(lead.status, status)
			lead.db_set.assert_not_called()

	def test_non_prospect_opportunity_hook_is_noop(self):
		with patch("frappe.get_all") as lookup:
			for source in ("Lead", "Customer"):
				update_prospect_opportunity_leads(frappe._dict(opportunity_from=source, party_name="L1"))
		lookup.assert_not_called()

	def test_standard_priority_matrix_is_unchanged_with_linked_prospect(self):
		# Exercise real ERPNext get_status/set_status across all standard statuses
		# and every combination of existing opportunity, quotation and customer rules.
		for flags in itertools.product((False, True), repeat=4):
			self.direct, self.quotation, self.lost_quotation, self.customer = flags
			for status in [*self.standard_statuses(), "Prospect", "Custom Stage"]:
				with self.subTest(flags=flags, status=status):
					baseline = self.lead(status, cls=Lead)
					lead = self.lead(status)
					baseline.set_status()
					lead.set_status()
					self.assertEqual(lead.status, baseline.status)

	def test_indirect_opportunity_keeps_quotation_and_customer_priorities(self):
		self.indirect = True
		for attribute, expected in (("lost_quotation", "Opportunity"),
			("quotation", "Quotation"), ("customer", "Converted")):
			setattr(self, attribute, True)
			lead = self.lead("Prospect")
			lead.set_status(update=True)
			self.assertEqual(lead.status, expected)
			setattr(self, attribute, False)

	def test_no_validation_or_deletion_hooks_and_no_global_calculator_override(self):
		self.assertEqual(doc_events["Prospect"], {"on_update": "dekure_custom.overrides.lead.update_prospect_lead_status"})
		self.assertEqual(doc_events["Opportunity"], {"on_update": "dekure_custom.overrides.lead.update_prospect_opportunity_leads"})
		self.assertNotIn("get_status", DraftQuotationStatusMixin.__dict__)
		self.assertNotIn("set_status", DraftQuotationStatusMixin.__dict__)

	def test_failed_status_update_propagates_for_transaction_rollback(self):
		lead = self.lead()
		lead.db_set.side_effect = RuntimeError("write failed")
		with self.assertRaisesRegex(RuntimeError, "write failed"):
			update_prospect_lead_status(self.prospect())
		self.db.commit.assert_not_called()


class TestProspectStatusOption(unittest.TestCase):
	def test_inserts_only_prospect_and_is_idempotent(self):
		options = "Lead\nOpen\nReplied\nOpportunity\nCustom Stage\nConverted\nDo Not Contact"
		expected = options.replace("Replied\nOpportunity", "Replied\nProspect\nOpportunity")
		with patch("frappe.get_meta") as meta, patch(
			"frappe.custom.doctype.property_setter.property_setter.make_property_setter"
		) as setter:
			meta.return_value.get_field.return_value.options = options
			ensure_prospect_status_option()
			setter.assert_called_once_with("Lead", "status", "options", expected, "Text")
			meta.assert_called_once_with("Lead", cached=False)
			meta.return_value.get_field.return_value.options = expected
			ensure_prospect_status_option()
			setter.assert_called_once()

	def test_existing_prospect_option_is_preserved_in_place(self):
		with patch("frappe.get_meta") as meta, patch(
			"frappe.custom.doctype.property_setter.property_setter.make_property_setter"
		) as setter:
			meta.return_value.get_field.return_value.options = "Lead\nReplied\nProspect\nOpportunity"
			ensure_prospect_status_option()
			setter.assert_not_called()

	def test_previously_appended_or_duplicate_prospect_is_repositioned(self):
		for options in ("Lead\nReplied\nOpportunity\nProspect",
			"Lead\nProspect\nReplied\nOpportunity\nProspect"):
			with patch("frappe.get_meta") as meta, patch(
				"frappe.custom.doctype.property_setter.property_setter.make_property_setter"
			) as setter:
				meta.return_value.get_field.return_value.options = options
				ensure_prospect_status_option()
				setter.assert_called_once_with(
					"Lead", "status", "options", "Lead\nReplied\nProspect\nOpportunity", "Text"
				)

	def test_install_and_migrate_restore_the_customization(self):
		self.assertEqual(after_install, "dekure_custom.overrides.lead.ensure_prospect_status_option")
		self.assertEqual(after_migrate, after_install)
