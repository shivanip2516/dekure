(() => {
	const API_PATH = "/api/method/hrms.api.get_attendance_calendar_events"
	const DRAFT_COLOR = "#F59E0B"

	function adaptCalendarEnvelope(envelope) {
		if (!envelope?.message || typeof envelope.message !== "object") return null
		const events = {}
		for (const [date, event] of Object.entries(envelope.message)) {
			if (event && typeof event === "object" && "docstatus" in event) {
				const docstatus = Number(event.docstatus)
				if (docstatus === 2) continue
				events[date] = docstatus === 0 ? "Draft" : event.status
			} else {
				events[date] = event
			}
		}
		return { ...envelope, message: events }
	}

	function draftDays(events) {
		return new Set(
			Object.entries(events || {})
				.filter(([, status]) => status === "Draft")
				.map(([date]) => Number(date.slice(-2)))
		)
	}

	function findCalendarGrid(doc) {
		return Array.from(doc.querySelectorAll(".grid.grid-cols-7")).find((grid) =>
			Array.from(grid.children).slice(7).some((cell) => /^\d+$/.test(cell.textContent.trim()))
		)
	}

	function renderDraftPresentation(doc, events) {
		const calendar = findCalendarGrid(doc)
		if (!calendar) return

		const days = draftDays(events)
		for (const marked of calendar.querySelectorAll("[data-dekure-draft]")) {
			marked.style.removeProperty("background-color")
			delete marked.dataset.dekureDraft
		}
		for (const cell of Array.from(calendar.children).slice(7)) {
			const day = Number(cell.textContent.trim())
			const circle = cell.firstElementChild
			if (!circle || !days.has(day)) continue
			circle.style.backgroundColor = DRAFT_COLOR
			circle.dataset.dekureDraft = "true"
		}

		const card = calendar.parentElement
		const summary = card?.querySelector(".grid.grid-cols-4.mx-2")
		if (!summary) return
		summary.style.gridTemplateColumns = "repeat(5, minmax(0, 1fr))"
		let item = summary.querySelector("[data-dekure-draft-summary]")
		if (!item) {
			item = doc.createElement("div")
			item.className = "flex flex-col gap-1"
			item.dataset.dekureDraftSummary = "true"
			item.innerHTML = `
				<div class="flex flex-row gap-1 items-center">
					<span class="rounded full h-3 w-3" style="background-color: ${DRAFT_COLOR}"></span>
					<span class="text-gray-600 text-sm font-medium leading-5">Draft</span>
				</div>
				<span class="text-gray-800 text-base font-semibold leading-6 mx-auto" data-dekure-draft-count></span>`
			summary.appendChild(item)
		}
		item.querySelector("[data-dekure-draft-count]").textContent = String(days.size)
	}

	const helpers = { DRAFT_COLOR, adaptCalendarEnvelope, draftDays, renderDraftPresentation }
	if (typeof module !== "undefined" && module.exports) module.exports = helpers
	if (typeof window === "undefined" || typeof document === "undefined" || typeof fetch === "undefined") return
	window.dekureAttendanceCalendar = helpers

	let currentEvents = {}
	let renderQueued = false
	const queueRender = () => {
		if (renderQueued) return
		renderQueued = true
		setTimeout(() => {
			renderQueued = false
			renderDraftPresentation(document, currentEvents)
		}, 0)
	}

	const originalFetch = window.fetch.bind(window)
	window.fetch = async (...args) => {
		const response = await originalFetch(...args)
		const requestUrl = typeof args[0] === "string" ? args[0] : args[0]?.url
		if (!requestUrl?.includes(API_PATH)) return response
		try {
			const envelope = await response.clone().json()
			const adapted = adaptCalendarEnvelope(envelope)
			if (!adapted) return response
			currentEvents = adapted.message
			queueRender()
			const headers = new Headers(response.headers)
			headers.delete("content-length")
			headers.delete("content-encoding")
			return new Response(JSON.stringify(adapted), {
				status: response.status,
				statusText: response.statusText,
				headers,
			})
		} catch {
			return response
		}
	}

	new MutationObserver(queueRender).observe(document.documentElement, { childList: true, subtree: true })
})()
