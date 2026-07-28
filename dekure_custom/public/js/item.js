frappe.ui.form.on("Item", {
	setup(frm) {
		configure_item_code_field(frm);
	},

	onload(frm) {
		configure_item_code_field(frm);
	},

	refresh(frm) {
		configure_item_code_field(frm);
		set_item_group_fields(frm);
	},

	item_group(frm) {
		configure_item_code_field(frm);
		set_item_group_fields(frm);
	},
});

function configure_item_code_field(frm) {
	frm.set_df_property("item_code", "reqd", 0);
	frm.set_df_property("item_code", "read_only", 1);
	frm.refresh_field("item_code");
}

function set_item_group_fields(frm) {
	const item_group = frm.doc.item_group;
	const is_product = ["Product", "Products"].includes(item_group);
	const is_service = item_group === "Services";

	frm.toggle_display("spare_part", is_product);
	frm.toggle_display("services", is_service);

	if (!is_product && frm.doc.spare_part) {
		frm.set_value("spare_part", "");
	}

	if (!is_service && frm.doc.services) {
		frm.set_value("services", "");
	}
}
