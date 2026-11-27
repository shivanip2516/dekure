"""CRM activity adjustments kept within dekure_custom."""

from crm.api.activities import get_activities as _get_activities
from erpnext.crm.utils import get_open_activities as _get_open_activities

import frappe
from frappe.utils import get_datetime, now_datetime


def _sort_tasks_by_creation(tasks, creation_by_name, update_modified=False):
	for task in tasks:
		creation = task.get("creation") or creation_by_name.get(task.get("name"))
		if creation:
			task["creation"] = creation
			if update_modified:
				# CRM's current TaskArea sorts the received task list by modified.
				# Use creation only as its compatibility ordering key.
				task["modified"] = creation

	tasks.sort(key=_creation_sort_key, reverse=True)
	return tasks


def _creation_sort_key(task):
	return get_datetime(task.get("creation") or "1900-01-01")


def set_todo_completion_timestamp(doc, method=None):
	"""Track only current completion transitions; do not infer historical times."""
	previous_status = doc.get_db_value("status") if not doc.is_new() else None

	if doc.status == "Closed" and previous_status != "Closed":
		doc.custom_completed_on = now_datetime()
	elif doc.status != "Closed" and previous_status == "Closed":
		doc.custom_completed_on = None


@frappe.whitelist()
def get_activities(name: str):
	"""Return CRM activities with linked tasks ordered by creation time.

	The CRM activity component currently applies its final task ordering using
	``modified``.  Reuse the standard endpoint and provide the task creation
	timestamp as that ordering key; the task card does not display modified, so
	this changes ordering only and leaves the standard task data and filters
	unchanged.
	"""
	result = _get_activities(name)
	if not result:
		return result

	activities, calls, notes, tasks, attachments = result
	if not tasks:
		return result

	task_names = [task.get("name") for task in tasks if task.get("name")]
	creation_by_name = {
		row.name: row.creation
		for row in frappe.db.get_all(
			"CRM Task",
			filters={"name": ("in", task_names)},
			fields=["name", "creation"],
			order_by="creation DESC",
		)
	}

	_sort_tasks_by_creation(tasks, creation_by_name, update_modified=True)
	return activities, calls, notes, tasks, attachments


@frappe.whitelist()
def get_open_activities(ref_doctype: str, ref_docname: str):
	"""Return ERPNext CRM widget activities with newest-created tasks first."""
	result = _get_open_activities(ref_doctype, ref_docname)

	all_tasks = (result.get("tasks") or []) + (result.get("tasks_history") or [])
	task_names = [task.get("name") for task in all_tasks if task.get("name")]
	if not task_names:
		return result

	todo_info_by_name = {
		row.name: row
		for row in frappe.db.get_all(
			"ToDo",
			filters={"name": ("in", task_names)},
			fields=["name", "creation", "custom_completed_on"],
		)
	}

	open_tasks = result.get("tasks") or []
	_sort_tasks_by_creation(
		open_tasks,
		{name: row.creation for name, row in todo_info_by_name.items()},
	)

	completed_tasks = result.get("tasks_history") or []
	for task in completed_tasks:
		todo = todo_info_by_name.get(task.get("name"))
		task["custom_completed_on"] = todo.custom_completed_on if todo else None
	completed_tasks.sort(
		key=lambda task: get_datetime(task["custom_completed_on"])
		if task.get("custom_completed_on")
		else get_datetime("1900-01-01"),
		reverse=True,
	)

	return result
