PWA_CALENDAR_SCRIPT = "/assets/dekure_custom/js/pwa_attendance_calendar.js"


def inject_attendance_calendar_script(response, request):
	"""Load the custom calendar adapter only on the HRMS PWA shell."""
	if request.method != "GET" or not (request.path == "/hrms" or request.path.startswith("/hrms/")):
		return
	if response.mimetype != "text/html" or response.direct_passthrough:
		return

	body = response.get_data(as_text=True)
	if PWA_CALENDAR_SCRIPT in body or "</head>" not in body:
		return

	tag = f'<script src="{PWA_CALENDAR_SCRIPT}"></script>\n'
	response.set_data(body.replace("</head>", f"{tag}</head>", 1))
