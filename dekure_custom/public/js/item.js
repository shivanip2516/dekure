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

	spare_part(frm) {
		update_variant_item_code_from_spare_part(frm);
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

async function update_variant_item_code_from_spare_part(frm) {
	if (!["Product", "Products"].includes(frm.doc.item_group) || !frm.doc.variant_of) {
		return;
	}

	const response = await frappe.call({
		method: "dekure_custom.overrides.item.preview_item_code",
		args: {
			doc: frm.doc,
		},
	});

	if (!response.message) {
		return;
	}

	if (response.message.item_abbreviation !== undefined) {
		await frm.set_value("item_abbreviation", response.message.item_abbreviation);
	}

	if (response.message.item_code !== undefined) {
		await frm.set_value("item_code", response.message.item_code || "");
	}
}
