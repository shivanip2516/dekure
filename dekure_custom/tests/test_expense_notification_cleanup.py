"""Lifecycle contract tests with in-memory logs; never connect to a site."""

from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from dekure_custom import hooks, notifications


class ParentLifecycle:
	def run_before_save_methods(self):
		self.before()

	def run_post_save_methods(self):
		return self.after()


class Claim(notifications.ExpenseClaimNotificationCleanupMixin, ParentLifecycle):
	doctype = "Expense Claim"
	name = "EXP-1"

	def __init__(self, previous, **values):
		self.previous = previous
		self.flags = frappe._dict()
		self.before = Mock()
		self.after = Mock()
		self.__dict__.update(values)

	def get(self, key):
		return getattr(self, key, None)

	def get_doc_before_save(self):
		return self.previous


class TestExpenseNotificationCleanup(TestCase):
	def setUp(self):
		self.logs = {}
		self.workflow = self.start_patch(patch.object(notifications, "get_workflow_name", return_value=None))
		self.workflow_field = self.start_patch(patch.object(notifications, "get_workflow_state_field", return_value="review_step"))
		self.query = self.start_patch(patch.object(frappe, "get_all", side_effect=self.get_all))
		self.delete = self.start_patch(patch.object(frappe, "delete_doc", side_effect=self.delete_doc))
		self.publish = self.start_patch(patch.object(frappe, "publish_realtime"))

	def start_patch(self, patcher):
		value = patcher.start()
		self.addCleanup(patcher.stop)
		return value

	def add_log(self, name, user="approver@example.com", claim="EXP-1", doctype="Expense Claim", read=0):
		self.logs[name] = frappe._dict(name=name, for_user=user, document_name=claim, document_type=doctype, read=read)

	def get_all(self, doctype, filters, fields):
		self.assertEqual(doctype, "Notification Log")
		return [frappe._dict({key: row[key] for key in fields}) for row in self.logs.values()
			if all(row.get(key) == value for key, value in filters.items())]

	def delete_doc(self, doctype, name, **kwargs):
		self.assertEqual(doctype, "Notification Log")
		self.assertEqual(kwargs, {"ignore_permissions": True, "ignore_missing": True, "delete_permanently": True})
		self.logs.pop(name, None)

	def claim(self, **values):
		previous = frappe._dict(status="Draft", approval_status="Pending Approval", docstatus=0)
		current = {**previous, "approval_status": "Approved", **values}
		return Claim(previous, **current)

	def test_new_claim_keeps_initial_notifications(self):
		doc = Claim(None, status="Draft", approval_status="Pending Approval", docstatus=0)
		doc.after.side_effect = lambda: self.add_log("INITIAL")
		doc.run_before_save_methods()
		doc.run_post_save_methods()
		self.assertIn("INITIAL", self.logs)
		self.query.assert_not_called()
		self.delete.assert_not_called()
		doc.before.assert_called_once()
		doc.after.assert_called_once()

	def test_terminal_changes_delete_read_and_unread_for_all_five_users(self):
		for status in ("Approved", "Rejected", "Cancelled"):
			with self.subTest(status=status):
				self.logs.clear()
				for index in range(5):
					self.add_log(f"OLD-{index}", user=f"user{index}@example.com", read=index % 2)
				self.add_log("OTHER-CLAIM", claim="EXP-2")
				self.add_log("OTHER-TYPE", doctype="Leave Application")
				doc = self.claim(approval_status=status)
				doc.run_before_save_methods()
				doc.run_post_save_methods()
				self.assertEqual(set(self.logs), {"OTHER-CLAIM", "OTHER-TYPE"})
				self.assertEqual(self.query.call_args.kwargs["filters"], {
					"document_type": "Expense Claim", "document_name": "EXP-1"})
		for args in self.publish.call_args_list:
			self.assertTrue(args.kwargs["after_commit"])
		self.assertEqual({args.kwargs["user"] for args in self.publish.call_args_list},
			{f"user{index}@example.com" for index in range(5)})

	def test_nonterminal_workflow_change_keeps_logs_from_all_new_stage_events(self):
		self.workflow.return_value = "Expense Approval"
		self.add_log("OLD")
		doc = self.claim(approval_status="Pending Approval", review_step="Finance Review")
		doc.previous.review_step = "Manager Review"
		doc.before.side_effect = lambda: self.add_log("NEW-BEFORE-SAVE")
		doc.after.side_effect = lambda: self.add_log("NEW-ON-CHANGE")
		doc.run_before_save_methods()
		doc.run_post_save_methods()
		# Jobs enqueued by the new stage may insert logs after commit as usual.
		self.add_log("NEW-QUEUED")
		self.assertEqual(set(self.logs), {"NEW-BEFORE-SAVE", "NEW-ON-CHANGE", "NEW-QUEUED"})
		self.workflow_field.assert_called_once_with("Expense Approval")

	def test_status_alone_and_submit_cancel_transitions_are_detected(self):
		for values in ({"approval_status": "Pending Approval", "status": "Unpaid"},
			{"approval_status": "Pending Approval", "docstatus": 1},
			{"approval_status": "Pending Approval", "docstatus": 2}):
			self.add_log("OLD")
			doc = self.claim(**values)
			doc.run_before_save_methods()
			doc.run_post_save_methods()
			self.assertNotIn("OLD", self.logs)

	def test_unrelated_edit_does_not_delete_any_logs(self):
		self.add_log("OLD")
		doc = self.claim(approval_status="Pending Approval", description="Changed description")
		doc.run_before_save_methods()
		doc.run_post_save_methods()
		self.assertIn("OLD", self.logs)
		self.delete.assert_not_called()
		self.publish.assert_not_called()

	def test_retries_and_already_deleted_logs_are_safe(self):
		self.add_log("OLD")
		doc = self.claim()
		doc.run_before_save_methods()
		self.logs.clear()  # Another cleanup already removed the captured log.
		doc.run_post_save_methods()
		doc.run_post_save_methods()
		self.assertEqual(self.delete.call_count, 1)
		self.assertEqual(self.publish.call_count, 1)

	def test_failed_validation_or_post_save_does_not_delete_logs(self):
		for phase in ("before", "after"):
			with self.subTest(phase=phase):
				self.add_log("OLD")
				doc = self.claim()
				getattr(doc, phase).side_effect = frappe.ValidationError("Save failed")
				with self.assertRaises(frappe.ValidationError):
					doc.run_before_save_methods()
					doc.run_post_save_methods()
				self.assertIn("OLD", self.logs)
		self.delete.assert_not_called()
		self.publish.assert_not_called()

	def test_same_object_next_save_snapshots_only_its_current_stage(self):
		self.add_log("OLD")
		doc = self.claim()
		doc.after.side_effect = lambda: self.add_log("CURRENT")
		doc.run_before_save_methods()
		doc.run_post_save_methods()
		doc.previous = frappe._dict(status=doc.status, approval_status=doc.approval_status, docstatus=doc.docstatus)
		doc.after.side_effect = None
		doc.run_before_save_methods()
		doc.run_post_save_methods()
		self.assertIn("CURRENT", self.logs)
		self.assertEqual(self.delete.call_count, 1)

	def test_cleanup_is_not_a_whitelisted_deletion_api_and_is_expense_only(self):
		mixin = notifications.ExpenseClaimNotificationCleanupMixin
		self.assertNotIn(mixin.run_before_save_methods, frappe.whitelisted)
		self.assertNotIn(mixin.run_post_save_methods, frappe.whitelisted)
		self.assertEqual(hooks.extend_doctype_class["Expense Claim"],
			"dekure_custom.notifications.ExpenseClaimNotificationCleanupMixin")
		self.assertEqual(hooks.doc_events["Expense Claim"]["before_submit"],
			"dekure_custom.api.prevent_pwa_employee_expense_claim_submission")

	def test_deletion_failure_propagates_without_publishing_success(self):
		self.add_log("OLD")
		doc = self.claim()
		doc.run_before_save_methods()
		self.delete.side_effect = RuntimeError("Database unavailable")
		with self.assertRaisesRegex(RuntimeError, "Database unavailable"):
			doc.run_post_save_methods()
		self.publish.assert_not_called()

	def test_desk_and_pwa_commands_use_the_same_cleanup(self):
		for command in ("frappe.desk.form.save.savedocs", "frappe.client.set_value"):
			self.add_log("OLD")
			doc = self.claim()
			with patch.object(frappe.local, "form_dict", frappe._dict(cmd=command), create=True):
				doc.run_before_save_methods()
				doc.run_post_save_methods()
			self.assertNotIn("OLD", self.logs)
