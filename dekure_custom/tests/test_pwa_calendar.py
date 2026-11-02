from types import SimpleNamespace
from unittest import TestCase

from werkzeug.wrappers import Response

from dekure_custom.pwa_calendar import PWA_CALENDAR_SCRIPT, inject_attendance_calendar_script


class TestPWACalendarAsset(TestCase):
	def test_injects_script_once_on_hrms_shell(self):
		response = Response("<html><head></head><body></body></html>", content_type="text/html")
		request = SimpleNamespace(method="GET", path="/hrms")

		inject_attendance_calendar_script(response, request)
		inject_attendance_calendar_script(response, request)

		self.assertEqual(response.get_data(as_text=True).count(PWA_CALENDAR_SCRIPT), 1)

	def test_does_not_modify_other_pages_or_api_responses(self):
		for path, content_type in (("/app", "text/html"), ("/hrms", "application/json")):
			response = Response("unchanged", content_type=content_type)
			inject_attendance_calendar_script(response, SimpleNamespace(method="GET", path=path))
			self.assertEqual(response.get_data(as_text=True), "unchanged")
