from datetime import date
from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from dekure_custom.api import get_expense_claims_with_dates, validate_pwa_expense_claim_dates


def _expense_row(name, expense_date):
	return frappe._dict(name=name, expense_date=expense_date)


def _expense_claim(rows, previous=None):
	doc = frappe._dict(expenses=rows)
	doc.is_new = lambda: previous is None
	doc.get_doc_before_save = lambda: previous
	return doc


class TestPWAExpenseClaimDates(UnitTestCase):
	@patch("dekure_custom.api.nowdate", return_value="2026-10-14")
	def test_new_claim_allows_today_yesterday_and_two_days_ago(self, nowdate):
		for day in (14, 13, 12):
			validate_pwa_expense_claim_dates(_expense_claim([_expense_row("NEW-1", date(2026, 10, day))]))

	@patch("dekure_custom.api.nowdate", return_value="2026-10-14")
	def test_new_claim_blocks_older_date_even_without_pwa_marker(self, nowdate):
		for invalid_date in (date(2026, 10, 11), date(2026, 10, 15)):
			with self.assertRaisesRegex(
				frappe.ValidationError, "Row 1: Expense Date can only be within the last 3 days"
			):
				validate_pwa_expense_claim_dates(_expense_claim([_expense_row("NEW-1", invalid_date)]))

	@patch("dekure_custom.api.nowdate", return_value="2026-10-14")
	def test_approval_or_rejection_keeps_unchanged_old_date(self, nowdate):
		previous = frappe._dict(expenses=[_expense_row("ROW-1", date(2026, 10, 7))])
		current = _expense_claim([_expense_row("ROW-1", date(2026, 10, 7))], previous)

		validate_pwa_expense_claim_dates(current)

	@patch("dekure_custom.api.nowdate", return_value="2026-10-14")
	def test_changed_or_new_old_expense_date_is_blocked(self, nowdate):
		previous = frappe._dict(expenses=[_expense_row("ROW-1", date(2026, 10, 7))])
		for rows, expected_row in (
			([_expense_row("ROW-1", date(2026, 10, 8))], 1),
			(
				[
					_expense_row("ROW-1", date(2026, 10, 7)),
					_expense_row("ROW-2", date(2026, 10, 8)),
				],
				2,
			),
		):
			with self.assertRaisesRegex(frappe.ValidationError, f"Row {expected_row}: Expense Date"):
				validate_pwa_expense_claim_dates(_expense_claim(rows, previous))

	@patch("dekure_custom.api.nowdate", return_value="2026-10-14")
	def test_changed_expense_date_within_window_is_allowed(self, nowdate):
		previous = frappe._dict(expenses=[_expense_row("ROW-1", date(2026, 10, 7))])
		current = _expense_claim([_expense_row("ROW-1", date(2026, 10, 12))], previous)

		validate_pwa_expense_claim_dates(current)

	@patch("dekure_custom.api.frappe.get_all")
	@patch("hrms.api.get_expense_claims")
	def test_claims_receive_single_range_and_missing_expense_dates(self, get_claims, get_all):
		get_claims.return_value = [
			frappe._dict(name="EXP-1", posting_date=date(2026, 10, 13)),
			frappe._dict(name="EXP-2", posting_date=date(2026, 10, 13)),
			frappe._dict(name="EXP-3", posting_date=date(2026, 10, 13)),
		]
		get_all.return_value = [
			frappe._dict(parent="EXP-1", expense_date=date(2026, 10, 12)),
			frappe._dict(parent="EXP-1", expense_date=date(2026, 10, 12)),
			frappe._dict(parent="EXP-2", expense_date=date(2026, 10, 11)),
			frappe._dict(parent="EXP-2", expense_date=date(2026, 10, 13)),
		]

		claims = get_expense_claims_with_dates("EMP-1", limit=10)

		self.assertEqual(claims[0].expense_date_from, date(2026, 10, 12))
		self.assertEqual(claims[0].expense_date_to, date(2026, 10, 12))
		self.assertEqual(claims[1].expense_date_from, date(2026, 10, 11))
		self.assertEqual(claims[1].expense_date_to, date(2026, 10, 13))
		self.assertIsNone(claims[2].expense_date_from)
		self.assertIsNone(claims[2].expense_date_to)
		get_all.assert_called_once()

	@patch("dekure_custom.api.frappe.get_all")
	@patch("hrms.api.get_expense_claims", return_value=[])
	def test_empty_claim_list_does_not_query_child_rows(self, get_claims, get_all):
		self.assertEqual(get_expense_claims_with_dates("EMP-1"), [])
		get_all.assert_not_called()
