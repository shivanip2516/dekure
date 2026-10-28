// Copyright (c) 2026, vigisolvo and contributors
// For license information, please see license.txt

(() => {
	const MAP_FIELD = "visit_location_map";
	const MAP_HEIGHT = 360;
	const CHECK_IN_COLOR = "#198754";
	const CHECK_OUT_COLOR = "#dc3545";

	frappe.ui.form.on("Visit", {
		refresh(frm) {
			render_visit_location_map(frm);
		},
	});

	function render_visit_location_map(frm) {
		const field = frm.fields_dict[MAP_FIELD];
		if (!field?.$wrapper) return;

		destroy_visit_location_map(frm);

		const locations = get_visit_locations(frm.doc);
		if (!locations.length) {
			field.$wrapper.html(
				`<div class="text-muted visit-location-unavailable">${__(
					"Visit location is not available."
				)}</div>`
			);
			return;
		}

		if (typeof L === "undefined" || !frappe.utils?.map_defaults) {
			field.$wrapper.html(
				`<div class="text-muted visit-location-unavailable">${__(
					"Visit location map could not be loaded."
				)}</div>`
			);
			return;
		}

		const map_id = frappe.dom.get_unique_id();
		field.$wrapper.html(`
			<div class="visit-location-map-wrapper border rounded">
				<div id="${map_id}" style="height: ${MAP_HEIGHT}px; min-height: 300px; width: 100%; z-index: 1;"></div>
			</div>
			<div class="visit-location-map-legend small text-muted mt-2">
				${locations.map(location_legend).join("")}
			</div>
		`);

		try {
			const map = L.map(map_id, { scrollWheelZoom: false });
			frm.visit_location_map = map;

			L.Icon.Default.imagePath = frappe.utils.map_defaults.image_path;
			L.tileLayer(
				frappe.utils.map_defaults.tiles.default_tile.url,
				frappe.utils.map_defaults.tiles.default_tile.options
			).addTo(map);

			if (locations.length === 2 && locations_are_identical(locations[0], locations[1])) {
				add_combined_marker(map, locations);
				map.setView([locations[0].latitude, locations[0].longitude], 16);
			} else {
				locations.forEach((location) => add_location_marker(map, location));
				if (locations.length === 1) {
					map.setView([locations[0].latitude, locations[0].longitude], 16);
				} else {
					map.fitBounds(
						locations.map((location) => [location.latitude, location.longitude]),
						{ padding: [35, 35], maxZoom: 16 }
					);
				}
			}

			window.requestAnimationFrame(() => map.invalidateSize());
		} catch (error) {
			destroy_visit_location_map(frm);
			field.$wrapper.html(
				`<div class="text-muted visit-location-unavailable">${__(
					"Visit location map could not be loaded."
				)}</div>`
			);
			console.error("Unable to render Visit location map", error);
		}
	}

	function destroy_visit_location_map(frm) {
		if (!frm.visit_location_map) return;
		frm.visit_location_map.remove();
		frm.visit_location_map = null;
	}

	function get_visit_locations(doc) {
		return [
			make_location(
				"checkin",
				__("Visit Check-In"),
				doc.checkin_latitude,
				doc.checkin_longitude,
				doc.checkin_time,
				CHECK_IN_COLOR
			),
			make_location(
				"checkout",
				__("Visit Check-Out"),
				doc.checkout_latitude,
				doc.checkout_longitude,
				doc.checkout_time,
				CHECK_OUT_COLOR
			),
		].filter(Boolean);
	}

	function make_location(type, label, latitude_value, longitude_value, time, color) {
		const latitude = parse_coordinate(latitude_value, -90, 90);
		const longitude = parse_coordinate(longitude_value, -180, 180);
		if (latitude === null || longitude === null) {
			return null;
		}

		return { type, label, latitude, longitude, time, color };
	}

	function parse_coordinate(value, minimum, maximum) {
		if (
			value === null ||
			value === undefined ||
			typeof value === "boolean" ||
			(typeof value === "string" && !value.trim())
		) {
			return null;
		}

		const coordinate = Number(value);
		return Number.isFinite(coordinate) && coordinate >= minimum && coordinate <= maximum
			? coordinate
			: null;
	}

	function location_legend(location, index) {
		const spacing = index ? "ml-3" : "";
		return `<span class="${spacing}"><span aria-hidden="true" style="color: ${
			location.color
		};">&#9679;</span> ${escape_html(location.label)}</span>`;
	}

	function add_location_marker(map, location) {
		const marker = L.circleMarker([location.latitude, location.longitude], {
			radius: 9,
			color: "#ffffff",
			weight: 2,
			fillColor: location.color,
			fillOpacity: 1,
		}).addTo(map);

		marker.bindTooltip(location.label, { direction: "top" });
		marker.bindPopup(location_popup(location));
	}

	function add_combined_marker(map, locations) {
		const label = locations.map((location) => location.label).join(" / ");
		const icon = L.divIcon({
			className: "visit-location-combined-marker",
			html: `<span aria-label="${escape_html(label)}" style="
				display: block;
				width: 22px;
				height: 22px;
				border: 2px solid #fff;
				border-radius: 50%;
				background: linear-gradient(90deg, ${CHECK_IN_COLOR} 0 50%, ${CHECK_OUT_COLOR} 50% 100%);
				box-shadow: 0 1px 4px rgba(0, 0, 0, 0.45);
			"></span>`,
			iconSize: [22, 22],
			iconAnchor: [11, 11],
			popupAnchor: [0, -13],
		});
		const marker = L.marker([locations[0].latitude, locations[0].longitude], {
			icon,
			draggable: false,
			keyboard: true,
		}).addTo(map);

		marker.bindTooltip(label, { direction: "top" });
		marker.bindPopup(locations.map(location_popup).join('<hr class="my-2">'));
	}

	function location_popup(location) {
		const time = format_time(location.time);
		return `<div class="visit-location-popup">
			<strong style="color: ${location.color};">${escape_html(location.label)}</strong><br>
			${__("Latitude")}: ${escape_html(format_coordinate(location.latitude))}<br>
			${__("Longitude")}: ${escape_html(format_coordinate(location.longitude))}
			${time ? `<br>${__("Time")}: ${escape_html(time)}` : ""}
		</div>`;
	}

	function format_coordinate(value) {
		return Number(value).toFixed(7);
	}

	function format_time(value) {
		if (!value) return "";
		try {
			return frappe.datetime.str_to_user(value);
		} catch (error) {
			return String(value);
		}
	}

	function locations_are_identical(first, second) {
		const precision = 1e-7;
		return (
			Math.abs(first.latitude - second.latitude) < precision &&
			Math.abs(first.longitude - second.longitude) < precision
		);
	}

	function escape_html(value) {
		return frappe.utils.escape_html(String(value));
	}
})();
