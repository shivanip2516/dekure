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
	if (!["Product", "Products"].includes(frm.doc.item_group)) {
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

(function install_dekure_multiple_variant_dialog() {
	if (!window.erpnext?.item) {
		frappe.after_ajax(install_dekure_multiple_variant_dialog);
		return;
	}

	if (erpnext.item.dekure_multiple_variant_dialog_installed) {
		return;
	}
	erpnext.item.dekure_multiple_variant_dialog_installed = true;

	erpnext.item.show_multiple_variants_dialog = function (frm) {
		const item_controller = this;
		const attr_val_fields = {};
		const promises = [];

		function make_fields_from_attribute_values(attr_dict) {
			const fields = [];
			const attributes = frm.doc.attributes.filter((row) => !row.disabled);
			attributes.forEach((row, i) => {
				const name = row.attribute;
				if (i % 3 === 0) {
					fields.push({ fieldtype: "Section Break" });
				}
				fields.push({ fieldtype: "Column Break" });
				fields.push({
					fieldtype: "MultiSelectPills",
					label: name,
					fieldname: frappe.scrub(name),
					placeholder: __("Search values..."),
					get_data: (txt) => get_attribute_suggestions(attr_dict[name], txt),
					onchange: update_primary_action,
				});
			});
			return fields;
		}

		function get_attribute_suggestions(spec, txt) {
			if (!spec) return [];
			return Array.isArray(spec) ? filter_list(spec, txt) : numeric_suggestions(spec, txt);
		}

		function filter_list(values, txt) {
			txt = (txt || "").toLowerCase();
			const matches = [];
			for (const value of values) {
				const label = (value.label || value.value || "").toLowerCase();
				if (!txt || label.includes(txt) || String(value.value || "").toLowerCase().includes(txt)) {
					matches.push(value);
					if (matches.length >= 50) break;
				}
			}
			return matches;
		}

		function numeric_suggestions(range, txt) {
			const { from_range: from, to_range: to, increment } = range;
			if (!(increment > 0) || from > to) return [];

			txt = (txt || "").trim();
			if (!txt) {
				const preview = [];
				for (let value = from; value <= to && preview.length < 50; value = flt(value + increment, 6)) {
					preview.push(String(value));
				}
				return preview;
			}

			return is_valid_attribute_value(range, txt) ? [String(flt(txt, 6))] : [];
		}

		function is_valid_attribute_value(spec, value) {
			if (!spec || !value) return false;
			if (Array.isArray(spec)) return spec.some((option) => option.value === value);

			const { from_range: from, to_range: to, increment } = spec;
			if (!(increment > 0)) return false;

			const text = String(value).trim();
			const num = Number(text);
			if (text === "" || !Number.isFinite(num)) return false;

			if (num < from || num > to) return false;
			const steps = (num - from) / increment;
			return Math.abs(Math.round(steps) - steps) <= 1e-6;
		}

		function validate_selected_attributes() {
			const errors = [];
			frm.doc.attributes.forEach((row) => {
				if (row.disabled) return;
				const field = item_controller.multiple_variant_dialog.get_field(frappe.scrub(row.attribute));
				if (!field) return;

				const attribute = frappe.utils.escape_html(row.attribute);
				const spec = attr_val_fields[row.attribute];
				const invalid = [
					...new Set((field.get_value() || []).filter((value) => !is_valid_attribute_value(spec, value))),
				];
				if (invalid.length) {
					const values = invalid.map(frappe.utils.escape_html).join(", ");
					errors.push(__("{0}: remove invalid value(s) {1}", [attribute, values]));
				}

				const pending = (field.$input?.val() || "").trim();
				if (pending) {
					const value = frappe.utils.escape_html(pending);
					errors.push(__("{0}: select the typed value {1} from the list or clear it", [attribute, value]));
				}
			});

			if (errors.length) {
				frappe.throw({
					title: __("Invalid Attribute Values"),
					message: errors.join("<br>"),
					indicator: "red",
				});
			}
		}

		function update_primary_action() {
			const selected_attributes = get_selected_attributes();
			const count = Object.keys(selected_attributes).reduce(
				(total, key) => total + selected_attributes[key].length,
				0
			);
			if (!count) {
				item_controller.multiple_variant_dialog.get_primary_btn().html(__("Create Variants"));
				item_controller.multiple_variant_dialog.disable_primary_action();
			} else {
				const msg = count === 1 ? __("Make {0} Variant", [count]) : __("Make {0} Variants", [count]);
				item_controller.multiple_variant_dialog.get_primary_btn().html(msg);
				item_controller.multiple_variant_dialog.enable_primary_action();
			}
		}

		function make_and_show_dialog(fields) {
			item_controller.multiple_variant_dialog = new frappe.ui.Dialog({
				title: __("Select Attribute Values"),
				fields: [
					frm.doc.image
						? {
								fieldtype: "Check",
								label: __("Create a variant with the template image."),
								fieldname: "use_template_image",
								default: 0,
						  }
						: null,
					{
						fieldtype: "HTML",
						fieldname: "help",
						options: `<label class="control-label">
							${__("Select at least one attribute value.")}
						</label>`,
					},
				]
					.concat(fields)
					.filter(Boolean),
			});

			item_controller.multiple_variant_dialog.set_primary_action(__("Create Variants"), () => {
				validate_selected_attributes();

				const selected_attributes = get_selected_attributes();
				const use_template_image = item_controller.multiple_variant_dialog.get_value("use_template_image");

				item_controller.multiple_variant_dialog.hide();
				frappe.call({
					method: "erpnext.controllers.item_variant.enqueue_multiple_variant_creation",
					args: {
						item: frm.doc.name,
						args: selected_attributes,
						use_template_image: use_template_image,
					},
					callback: function (r) {
						if (r.message === "queued") {
							frappe.show_alert({
								message: __("Variant creation has been queued."),
								indicator: "orange",
							});
						} else {
							frappe.show_alert({
								message: __("{0} variants created.", [r.message]),
								indicator: "green",
							});
						}
					},
				});
			});

			item_controller.multiple_variant_dialog.disable_primary_action();
			item_controller.multiple_variant_dialog.clear();
			item_controller.multiple_variant_dialog.show();
		}

		function get_selected_attributes() {
			const selected_attributes = {};
			frm.doc.attributes.forEach((row) => {
				if (row.disabled) return;
				const values = item_controller.multiple_variant_dialog.get_value(frappe.scrub(row.attribute));
				if (values && values.length) {
					selected_attributes[row.attribute] = values;
				}
			});
			return selected_attributes;
		}

		function get_attribute_value_label(row) {
			return row.spare_part ? `${row.attribute_value} - ${row.spare_part}` : row.attribute_value;
		}

		frm.doc.attributes.forEach((row) => {
			if (row.disabled) return;

			const promise = frappe.db
				.get_value("Item Attribute", row.attribute, [
					"numeric_values",
					"from_range",
					"to_range",
					"increment",
				])
				.then((res) => {
					const attr = res.message || {};

					if (!attr.numeric_values) {
						return frappe
							.call({
								method: "frappe.client.get_list",
								args: {
									doctype: "Item Attribute Value",
									filters: [["parent", "=", row.attribute]],
									fields: ["name", "attribute_value", "spare_part"],
									limit_page_length: 0,
									parent: "Item Attribute",
									order_by: "idx",
								},
							})
							.then((r) => {
								attr_val_fields[row.attribute] = (r.message || []).map((value_row) => ({
									value: value_row.name,
									label: get_attribute_value_label(value_row),
									description: value_row.attribute_value,
								}));
							});
					}

					attr_val_fields[row.attribute] = {
						from_range: flt(attr.from_range),
						to_range: flt(attr.to_range),
						increment: flt(attr.increment),
					};
				});

			promises.push(promise);
		});

		Promise.all(promises).then(() => {
			const fields = make_fields_from_attribute_values(attr_val_fields);
			make_and_show_dialog(fields);
		});
	};
})();
