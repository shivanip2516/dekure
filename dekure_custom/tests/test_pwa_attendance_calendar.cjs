const { test } = require("node:test")
const assert = require("node:assert/strict")
const path = require("node:path")

const { DRAFT_COLOR, adaptCalendarEnvelope, draftDays } = require(path.join(
	__dirname,
	"../public/js/pwa_attendance_calendar.js"
))

test("draft status has priority over every internal attendance status", () => {
	assert.equal(DRAFT_COLOR, "#F59E0B")
	const adapted = adaptCalendarEnvelope({ message: {
		"2026-10-01": { status: "Present", docstatus: 0 },
		"2026-10-02": { status: "Half Day", docstatus: 0 },
		"2026-10-03": { status: "Absent", docstatus: 0 },
	} })
	assert.deepEqual(adapted.message, {
		"2026-10-01": "Draft",
		"2026-10-02": "Draft",
		"2026-10-03": "Draft",
	})
	assert.deepEqual([...draftDays(adapted.message)], [1, 2, 3])
})

test("submitted colors and non-attendance events retain their existing status keys", () => {
	const adapted = adaptCalendarEnvelope({ message: {
		"2026-10-04": { status: "Present", docstatus: 1 },
		"2026-10-05": { status: "Half Day", docstatus: 1 },
		"2026-10-06": { status: "Absent", docstatus: 1 },
		"2026-10-07": { status: "On Leave", docstatus: 1 },
		"2026-10-08": "Holiday",
		"2026-10-09": { status: "Present", docstatus: 2 },
	} })
	assert.deepEqual(adapted.message, {
		"2026-10-04": "Present",
		"2026-10-05": "Half Day",
		"2026-10-06": "Absent",
		"2026-10-07": "On Leave",
		"2026-10-08": "Holiday",
	})
})
