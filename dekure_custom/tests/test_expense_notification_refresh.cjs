const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function setup(respond) {
	let ready, listener, event;
	const calls = [];
	const view = {
		max_length: 20,
		dropdown_items: [{ name: "OLD" }],
		container: { empty() { view.emptied = true; } },
		render_notifications_dropdown() { view.rendered = true; },
		update_count_badge(count) { view.count = count; },
		toggle_notification_icon(seen) { view.seen = seen; },
	};
	const frappe = {
		app: { sidebar: { notifications: { tabs: { notifications: view } } } },
		session: { user: "recipient@example.com" },
		boot: {},
		realtime: { on(name, handler) { event = name; listener = handler; } },
		update_user_info() {},
		call(options) { calls.push(options); return respond(options); },
	};
	vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../public/js/expense_claim_notifications.js"), "utf8"), {
		frappe, document: {}, $: () => ({ on(name, callback) { ready = callback; } }),
	});
	ready();
	assert.equal(event, "dekure_expense_notifications_cleared");
	return { refresh: () => listener(), view, calls, frappe };
}

test("refreshes the real list and unread count without incrementing for a deletion", async () => {
	const state = setup((options) => Promise.resolve({ message: options.method.endsWith("get_count") ? 3 : {
		notification_logs: [{ name: "NEW-STAGE" }], user_info: {},
	} }));
	await state.refresh();
	assert.equal(state.view.dropdown_items[0].name, "NEW-STAGE");
	assert.equal(state.view.count, 3);
	assert.equal(state.frappe.boot.notification_unread_count, 3);
	assert.ok(state.view.emptied && state.view.rendered);
	assert.ok(state.calls.every((call) => call.type === "POST"));
	assert.equal(state.calls[1].args.filters.for_user, "recipient@example.com");
	assert.equal(state.calls[1].args.filters.read, 0);
});

test("clears stale rendered rows and hides the indicator when no unread logs remain", async () => {
	const state = setup((options) => Promise.resolve({ message: options.method.endsWith("get_count") ? 0 : {
		notification_logs: [], user_info: {},
	} }));
	await state.refresh();
	assert.equal(state.view.dropdown_items.length, 0);
	assert.equal(state.view.count, 0);
	assert.equal(state.view.seen, true);
	assert.ok(state.view.emptied);
});

test("an older response cannot overwrite a newer cleanup refresh", async () => {
	const pending = [];
	let requests = 0;
	const state = setup((options) => {
		if (++requests <= 2) return new Promise((resolve) => pending.push(resolve));
		return Promise.resolve({ message: options.method.endsWith("get_count") ? 1 : {
			notification_logs: [{ name: "LATEST" }], user_info: {},
		} });
	});
	const first = state.refresh();
	await state.refresh();
	pending[0]({ message: { notification_logs: [{ name: "STALE" }], user_info: {} } });
	pending[1]({ message: 9 });
	await first;
	assert.equal(state.view.dropdown_items[0].name, "LATEST");
	assert.equal(state.view.count, 1);
});
