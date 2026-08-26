import frappe
from frappe.model.document import Document
from frappe import _
from frappe.utils import now_datetime, getdate, get_time

class AttendanceRegularizationRequest(Document):
	def validate(self):
		if self.is_new():
			# 1. Check/force employee if not set or if user is Employee
			roles = frappe.get_roles()
			if "HR Manager" not in roles and "System Manager" not in roles and "HR User" not in roles:
				user_employee = frappe.db.get_value("Employee", {"user_id": frappe.session.user}, "name")
				if not user_employee:
					frappe.throw(_("No employee is linked to your login"), frappe.PermissionError)
				if self.employee and self.employee != user_employee:
					frappe.throw(_("You can only create requests for yourself"), frappe.PermissionError)
				self.employee = user_employee

			if not self.employee:
				frappe.throw(_("Employee is required"))

			# 2. Autofill fields using existing api logic
			from dekure_custom.api import (
				_get_shift,
				_get_shift_approver,
				_get_existing_checkin_time,
				_lock_employee,
				_validate_missed_punch,
				_validate_requested_punch_time,
				_validate_coordinates,
				MISSED_CHECKOUT
			)
			
			self.shift = _get_shift(self.employee, self.attendance_date)
			self.attendance_approver = _get_shift_approver(self.employee)
			self.existing_checkin = _get_existing_checkin_time(self.employee, self.attendance_date)
			self.submitted_by = frappe.session.user
			self.submitted_on = now_datetime()
			self.status = "Pending Approval"

			# 3. Lock & run validation helpers
			_lock_employee(self.employee)
			_validate_missed_punch(self.employee, self.attendance_date, self.request_type, exclude_request=self.name)
			
			# Validate requested time format
			requested_time = get_time(self.requested_time)
			if not requested_time:
				frappe.throw(_("Invalid requested time format"))
			_validate_requested_punch_time(self.employee, self.attendance_date, self.request_type, requested_time)

			# If missed check-out, validate and coordinates are mandatory
			if self.request_type == MISSED_CHECKOUT:
				latitude, longitude = _validate_coordinates(self.latitude, self.longitude)
				self.latitude = latitude
				self.longitude = longitude
			else:
				self.latitude = None
				self.longitude = None
