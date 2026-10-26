"""Exercise Item save hooks without changing site data."""

from importlib import import_module
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import frappe
from frappe.model.document import Document

from erpnext.stock.doctype.item.item import Item

from dekure_custom import hooks


class TestItemHooks(TestCase):
	def dispatch(self, event, controller, doc):
		registered = {
			doctype: {method: [handler] if isinstance(handler, str) else handler
				for method, handler in events.items()}
			for doctype, events in hooks.doc_events.items()
		}
		other_handlers = []

		def existing_handler(doc, method=None):
			other_handlers.append(method)

		# Include an independently registered Item hook, as installed apps do.
		registered.setdefault("Item", {}).setdefault(event, []).append("other_app.item_handler")

		def resolve(path):
			if path in ("other_app.item_handler", hooks.doc_events["*"]["on_update"]):
				return existing_handler
			module, name = path.rsplit(".", 1)
			return getattr(import_module(module), name)

		controller.__name__ = event
		with patch.object(frappe, "get_doc_hooks", return_value=registered), patch.object(
			frappe, "get_attr", side_effect=resolve), patch.object(
			frappe.local, "db", SimpleNamespace(_disable_transaction_control=0), create=True):
			Document.hook(controller)(doc)
		return other_handlers

	@staticmethod
	def item(**values):
		doc = SimpleNamespace(doctype="Item", item_code="ITEM-UNCHANGED", has_serial_no=0,
			is_stock_item=1, is_fixed_asset=0, serial_no_series="OLD-SERIES")
		doc.__dict__.update(values)
		doc.get = lambda key, default=None: getattr(doc, key, default)
		return doc

	def test_item_lifecycle_resolves_without_obsolete_module_and_keeps_other_hooks(self):
		for event in ("before_insert", "before_naming", "validate", "on_update"):
			with self.subTest(event=event):
				called = []
				doc = self.item()
				others = self.dispatch(event, lambda doc: called.append(event), doc)
				self.assertEqual(called, [event])
				self.assertEqual(others, [event, event] if event == "on_update" else [event])
				self.assertEqual(doc.item_code, "ITEM-UNCHANGED")

	def test_standard_item_validation_still_runs(self):
		doc = self.item()
		self.dispatch("validate", lambda doc: Item.validate_item_type(doc), doc)
		self.assertIsNone(doc.serial_no_series)

	def test_standard_item_validation_still_rejects_invalid_non_stock_serial_item(self):
		doc = self.item(has_serial_no=1, is_stock_item=0)

		def reject(message):
			raise frappe.ValidationError(message)

		with patch("erpnext.stock.doctype.item.item._", side_effect=lambda value: value), patch.object(
			frappe, "throw", side_effect=reject):
			with self.assertRaisesRegex(frappe.ValidationError, "non-stock item"):
				self.dispatch("validate", lambda doc: Item.validate_item_type(doc), doc)
