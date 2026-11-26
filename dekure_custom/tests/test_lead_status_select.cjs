// Exercise the installed Select control with metadata produced by our backend hook.
// The DOM adapter keeps this check independent of a running site or browser.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { execFileSync } = require("node:child_process");
const bench = path.resolve(__dirname, "../../../..");
const field = JSON.parse(execFileSync(path.join(bench, "env/bin/python"), ["-c", `
import json
from pathlib import Path
from unittest.mock import Mock, patch
import erpnext
from dekure_custom.overrides.lead import ensure_prospect_status_option
source = Path(erpnext.__file__).parent / 'crm/doctype/lead/lead.json'
field = next(f for f in json.loads(source.read_text())['fields'] if f['fieldname'] == 'status')
def apply_setter(doctype, fieldname, property, value, property_type):
    field[property] = value
with patch('frappe.get_meta', return_value=Mock(get_field=Mock(return_value=Mock(options=field['options'])))), patch('frappe.custom.doctype.property_setter.property_setter.make_property_setter', side_effect=apply_setter):
    ensure_prospect_status_option()
print(json.dumps(field))
`], { cwd: bench, encoding: "utf8" }));

class SelectElement {
	constructor() { this.options = []; this.selected = null; }
	val(value) {
		if (arguments.length) {
			this.selected = this.options.some((option) => option.value === value) ? value : null;
			return this;
		}
		return this.selected;
	}
	find() { return { val: () => this.selected }; }
	empty() { this.options = []; this.selected = null; return this; }
	get() { return this; }
	trigger() { return this; }
	set selectedIndex(index) { this.selected = this.options[index]?.value ?? null; }
}
class OptionElement {
	html(label) { this.label = label; return this; }
	attr(key, value) { this[key] = value; return this; }
	prop(key, value) { this[key] = value; return this; }
	appendTo(select) { select.options.push(this); }
}
const $ = (element) => element === "<option>" ? new OptionElement() : element;
$.fn = { val() {} };
class ControlData {
	set_formatted_input(value) { this.$input.val(value); }
}
const frappe = { ui: { form: { ControlData } }, listview_settings: {},
	utils: { guess_colour: (value) => `standard-colour:${value}` } };
const context = { frappe, $, jQuery: $, __: (value) => value,
	cstr: (value) => String(value ?? ""), is_null: (value) => value == null };
vm.runInNewContext(fs.readFileSync(path.join(bench,
	"apps/frappe/frappe/public/js/frappe/form/controls/select.js"), "utf8"), context);
vm.runInNewContext(fs.readFileSync(path.join(bench,
	"apps/erpnext/erpnext/crm/doctype/lead/lead_list.js"), "utf8"), context);

function control() {
	const input = new frappe.ui.form.ControlSelect();
	input.df = field;
	input.$input = new SelectElement();
	input.set_model_value = () => assert.fail("Dropdown must not overwrite the saved status");
	return input;
}

test("backend metadata supplies the real field and ordered frontend options", () => {
	assert.equal(field.fieldname, "status");
	assert.equal(field.fieldtype, "Select");
	assert.equal(field.in_list_view, 1);
	assert.equal(field.in_standard_filter, 1);
	const input = control();
	input.set_options();
	const values = input.$input.options.map((option) => option.value);
	assert.equal(values[values.indexOf("Replied") + 1], "Prospect");
	assert.equal(values[values.indexOf("Prospect") + 1], "Opportunity");
});

test("Prospect remains selected on initial load, refresh and reopening", () => {
	const input = control();
	for (let refresh = 0; refresh < 3; refresh++) {
		input.set_formatted_input("Prospect");
		assert.equal(input.$input.val(), "Prospect");
	}
	const reopened = control();
	reopened.set_formatted_input("Prospect");
	assert.equal(reopened.$input.val(), "Prospect");
	assert.equal(reopened.$input.options.find((option) => option.value === "Prospect").label, "Prospect");
});

test("standard list indicator displays Prospect with its status filter", () => {
	const indicator = frappe.listview_settings.Lead.get_indicator({ status: "Prospect" });
	assert.deepEqual(Array.from(indicator), ["Prospect", "standard-colour:Prospect", "status,=,Prospect"]);
});
