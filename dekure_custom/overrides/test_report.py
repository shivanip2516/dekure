import unittest
from unittest.mock import Mock, patch

import frappe

from dekure_custom.overrides.report import LeadNotesReportMixin, get_lead_notes, get_lead_reportview


class BaseReport:
	def execute_module(self, filters):
		return self.result


class ExtendedReport(LeadNotesReportMixin, BaseReport):
	pass


class TestLeadNotesReport(unittest.TestCase):
	def test_report_builder_enriches_rows_without_changing_existing_values(self):
		response = {"keys": ["name", "status"], "values": [["LEAD-1", "Open"], ["LEAD-2", "Lead"]]}
		with (
			patch("dekure_custom.overrides.report.frappe.conf", frappe._dict(read_from_replica=False)),
			patch("dekure_custom.overrides.report.frappe.form_dict", {"doctype": "Lead"}),
			patch("dekure_custom.overrides.report.reportview.get", return_value=response),
			patch("dekure_custom.overrides.report.get_lead_notes", return_value={"LEAD-1": "First\n\nSecond"}),
		):
			result = get_lead_reportview()
		self.assertEqual(result["keys"], ["name", "status", "_lead_notes"])
		self.assertEqual(result["values"], [["LEAD-1", "Open", "First\n\nSecond"], ["LEAD-2", "Lead", ""]])

	def test_report_builder_empty_or_grouped_response_is_unchanged(self):
		for response in ([], {"keys": ["status", "count"], "values": [["Open", 3]]}):
			with (
				patch("dekure_custom.overrides.report.frappe.conf", frappe._dict(read_from_replica=False)),
				patch("dekure_custom.overrides.report.frappe.form_dict", {"doctype": "Lead"}),
				patch("dekure_custom.overrides.report.reportview.get", return_value=response),
				patch("dekure_custom.overrides.report.get_lead_notes") as fetch,
			):
				self.assertIs(get_lead_reportview(), response)
				fetch.assert_not_called()

	def test_report_preserves_rows_columns_and_extra_results(self):
		report = ExtendedReport()
		report.name = "Lead Details"
		columns = [{"fieldname": "name"}]
		rows = [{"name": "LEAD-1", "status": "Open"}, {"name": "LEAD-2", "status": "Lead"}]
		report.result = (columns, rows, "message", {"chart": "unchanged"})
		with (
			patch("dekure_custom.overrides.report.get_lead_notes", return_value={"LEAD-1": "First\n\nSecond"}),
			patch("dekure_custom.overrides.report._", side_effect=lambda value: value),
		):
			result = report.execute_module({"status": "Open"})
		self.assertEqual(result[0][:-1], columns)
		self.assertEqual(result[0][-1]["fieldname"], "notes")
		self.assertEqual([row["name"] for row in result[1]], ["LEAD-1", "LEAD-2"])
		self.assertEqual([row["notes"] for row in result[1]], ["First\n\nSecond", ""])
		self.assertEqual(result[2:], report.result[2:])
		self.assertNotIn("notes", rows[0])

	def test_other_reports_are_unchanged(self):
		report = ExtendedReport()
		report.name = "Lead Owner Efficiency"
		report.result = ([], [])
		with patch("dekure_custom.overrides.report.get_lead_notes") as fetch:
			self.assertIs(report.execute_module({}), report.result)
		fetch.assert_not_called()

	def test_latest_note_is_selected_by_timestamp_with_two_bulk_queries(self):
		meta = Mock()
		meta.get_field.return_value.permlevel = 0
		meta.get_permlevel_access.return_value = [0]
		with (
			patch("dekure_custom.overrides.report.frappe.has_permission", return_value=True),
			patch("dekure_custom.overrides.report.frappe.get_meta", return_value=meta),
			patch("dekure_custom.overrides.report.get_permitted_fields", return_value=["note"]),
			patch("dekure_custom.overrides.report.frappe.get_list", return_value=["LEAD-1"]) as leads,
			patch(
				"dekure_custom.overrides.report.frappe.get_all",
				return_value=[
					frappe._dict(parent="LEAD-1", note="<p>Second<br>line</p>", added_on="2026-10-19 12:00:00", idx=1),
					frappe._dict(parent="LEAD-1", note="<p>First</p>", added_on="2026-10-18 12:00:00", idx=3),
					frappe._dict(parent="LEAD-1", note="<p><br></p>", creation="2026-10-17 12:00:00", idx=2),
				],
			) as notes,
		):
			result = get_lead_notes({"LEAD-1", "RESTRICTED"})
		self.assertEqual(result, {"LEAD-1": "Second\nline"})
		leads.assert_called_once()
		notes.assert_called_once()
		self.assertEqual(
			notes.call_args.kwargs["filters"],
			{"parenttype": "Lead", "parentfield": "notes", "parent": ["in", ["LEAD-1"]]},
		)

	def test_empty_result_or_denied_lead_access_does_not_fetch_notes(self):
		with (
			patch("dekure_custom.overrides.report.frappe.has_permission", return_value=False),
			patch("dekure_custom.overrides.report.frappe.get_all") as notes,
		):
			self.assertEqual(get_lead_notes(set()), {})
			self.assertEqual(get_lead_notes({"LEAD-1"}), {})
		notes.assert_not_called()

	def test_denied_notes_field_access_does_not_fetch_notes(self):
		meta = Mock()
		meta.get_field.return_value.permlevel = 1
		meta.get_permlevel_access.return_value = [0]
		with (
			patch("dekure_custom.overrides.report.frappe.has_permission", return_value=True),
			patch("dekure_custom.overrides.report.frappe.get_meta", return_value=meta),
			patch("dekure_custom.overrides.report.frappe.get_all") as notes,
		):
			self.assertEqual(get_lead_notes({"LEAD-1"}), {})
		notes.assert_not_called()
