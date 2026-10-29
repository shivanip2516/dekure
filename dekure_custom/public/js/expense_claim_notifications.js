// Refresh the Desk bell after deletion without firing the "notification" event,
// which Frappe treats as a new notification and increments the unread count.
$(document).on("app_ready", () => {
	let revision = 0;
	frappe.realtime.on("dekure_expense_notifications_cleared", async () => {
		const current = ++revision;
		const view = frappe.app?.sidebar?.notifications?.tabs?.notifications;
		if (!view) return;
		const [list, count] = await Promise.all([
			frappe.call({
				method: "frappe.desk.doctype.notification_log.notification_log.get_notification_logs",
				args: { limit: view.max_length },
				type: "POST",
			}),
			frappe.call({
				method: "frappe.client.get_count",
				args: {
					doctype: "Notification Log",
					filters: { for_user: frappe.session.user, read: 0 },
				},
				type: "POST",
			}),
		]);
		if (current !== revision || !list.message) return;
		frappe.update_user_info(list.message.user_info);
		view.dropdown_items = list.message.notification_logs;
		view.container.empty();
		view.render_notifications_dropdown();
		view.update_count_badge(count.message);
		frappe.boot.notification_unread_count = count.message;
		if (!count.message) view.toggle_notification_icon(true);
	});
});
