(() => {
	const settings = frappe.listview_settings["Lead"];
	const original_onload = settings.onload;

	settings.onload = function (listview) {
		original_onload?.call(this, listview);
		if (listview.view_name !== "Report" || listview.dekure_notes_configured) return;
		listview.dekure_notes_configured = true;
		listview.method = "dekure_custom.overrides.report.get_lead_reportview";

		const switch_column = listview.switch_column;
		listview.switch_column = function (first, second) {
			if ([first, second].some((column) => column.id === "_lead_notes")) {
				// A computed column has no SQL field to reorder in saved settings.
				this.render(true);
				return;
			}
			return switch_column.call(this, first, second);
		};

		// The Report Builder uses its own columns, separate from Lead Details.
		// Keep the computed value out of SQL fields and saved report settings.
		const setup_columns = listview.setup_columns;
		listview.setup_columns = function () {
			setup_columns.call(this);
			if (this.group_by || this.columns_map._lead_notes) return;

			const column = {
				id: "_lead_notes",
				field: "_lead_notes",
				name: __("Notes"),
				content: __("Notes"),
				width: 350,
				editable: false,
				sortable: false,
				dropdown: false,
				docfield: {
					fieldname: "_lead_notes",
					fieldtype: "Small Text",
					label: "Notes",
					parent: "Lead",
					read_only: 1,
				},
				format: (value) =>
					`<div style="white-space: pre-wrap">${frappe.utils.escape_html(value || "")}</div>`,
			};
			this.columns.push(column);
			this.columns_map[column.id] = column;
		};
	};
})();
