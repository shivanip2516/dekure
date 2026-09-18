import frappe
from frappe.desk.query_report import run as run_query_report

STOCK_LEDGER_CUSTOM_COLUMNS = {
    "supplier": {
        "fieldname": "supplier",
        "fieldtype": "Link",
        "label": "Supplier",
        "insert_after_index": 0,
        "link_field": {"fieldname": "voucher_no", "names": []},
        "doctype": "Purchase Receipt",
        "options": "Supplier",
        "width": 100,
    },
    "customer": {
        "fieldname": "customer",
        "fieldtype": "Link",
        "label": "Customer",
        "insert_after_index": 1,
        "link_field": {"fieldname": "voucher_no", "names": []},
        "doctype": "Delivery Note",
        "options": "Customer",
        "width": 100,
    },
}


@frappe.whitelist()
def run(*args, **kwargs):
    report_name = kwargs.get("report_name") or (args[0] if args else None)

    if report_name == "Stock Ledger":
        kwargs["custom_columns"] = get_stock_ledger_custom_columns(kwargs.get("custom_columns"))

    result = run_query_report(*args, **kwargs)

    if report_name == "Stock Ledger" and isinstance(result, dict):
        result["columns"] = get_stock_ledger_columns(result.get("columns") or [])

    return result


def get_stock_ledger_custom_columns(custom_columns=None):
    custom_columns = frappe.parse_json(custom_columns or [])
    custom_columns_by_fieldname = {
        column.get("fieldname"): column for column in custom_columns if isinstance(column, dict)
    }

    for fieldname, column in STOCK_LEDGER_CUSTOM_COLUMNS.items():
        custom_columns_by_fieldname.setdefault(fieldname, column.copy())

    return list(custom_columns_by_fieldname.values())


def get_stock_ledger_columns(columns):
    remaining = []
    by_fieldname = {}

    for column in columns:
        fieldname = column.get("fieldname") if isinstance(column, dict) else None
        if fieldname in {"supplier", "customer", "serial_no"}:
            by_fieldname.setdefault(fieldname, column)
        else:
            remaining.append(column)

    supplier = by_fieldname.get("supplier")
    customer = by_fieldname.get("customer")
    serial_no = by_fieldname.get("serial_no") or {
        "label": "Serial No",
        "fieldname": "serial_no",
        "fieldtype": "Link",
        "options": "Serial No",
        "width": 100,
    }
    serial_no.pop("hidden", None)

    if supplier and customer:
        insert_after(remaining, "date", [supplier, customer])
    insert_after(remaining, "item_name", [serial_no])

    return remaining


def insert_after(columns, fieldname, new_columns):
    index = next(
        (
            i
            for i, column in enumerate(columns)
            if isinstance(column, dict) and column.get("fieldname") == fieldname
        ),
        None,
    )

    if index is None:
        columns.extend(new_columns)
    else:
        columns[index + 1 : index + 1] = new_columns
