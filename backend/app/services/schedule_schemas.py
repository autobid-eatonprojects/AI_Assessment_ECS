"""Per-schedule-type Pydantic schemas (P2, W6 mitigation).

Each schedule type (door, window, finish, room, fixture, panel,
equipment) has its own Pydantic model + JSON schema. The typed extractor
calls Sonnet with the type-specific schema as its tool input_schema, so
the model is forced to return rows shaped like the AEC convention for
that type rather than dumping random columns into a generic table.

This is the W6 mitigation: schedule extraction errors that happen with a
generic schema (column count mismatches, header/row misalignment) are
caught at the schema-validation layer because the model literally can't
emit a malformed row.

Schemas allow `additionalProperties` so non-standard columns the design
team added (e.g. a "By Owner" column on a fixture schedule) survive
extraction — they land in `extra_fields` rather than being dropped.

Per AEC convention (CSI MasterFormat / standard architectural practice):
"""

from __future__ import annotations


# -----------------------------------------------------------------------------
# Per-type column expectations
# -----------------------------------------------------------------------------


def _row_schema(*required_fields: str, **optional_fields: str) -> dict:
    """Build a JSON schema for one schedule row.

    `required_fields`: field names that MUST appear (typically just the
    mark/tag/ID column).
    `optional_fields`: name → human description. Missing values land as null.

    Extra columns the schedule has but our schema doesn't name go into
    `extra_fields` (a JSON object) so we don't lose project-specific data.
    """
    properties: dict = {}
    for name in required_fields:
        properties[name] = {"type": "string"}
    for name, desc in optional_fields.items():
        properties[name] = {
            "type": ["string", "null"],
            "description": desc,
        }
    properties["extra_fields"] = {
        "type": "object",
        "description": (
            "Non-standard columns this row has. Key = column header as printed; "
            "value = cell content. Use this for any column the schema didn't name."
        ),
        "additionalProperties": {"type": ["string", "number", "null"]},
    }
    return {
        "type": "object",
        "properties": properties,
        "required": list(required_fields),
    }


def _wrap_rows(row_schema: dict, schedule_label: str) -> dict:
    """Wrap a row schema in the outer extract_schedule tool input."""
    return {
        "type": "object",
        "properties": {
            "rows": {
                "type": "array",
                "items": row_schema,
                "description": (
                    f"Every row from the {schedule_label}. Each row = one "
                    "biddable item. Don't summarise — emit one row per "
                    "row visible in the schedule."
                ),
            },
            "schedule_name_as_printed": {
                "type": "string",
                "description": "The schedule's title as visible on the page",
            },
            "row_count_visible": {
                "type": "integer",
                "description": (
                    "Total row count you actually see (sanity check vs len(rows))"
                ),
            },
        },
        "required": ["rows", "schedule_name_as_printed"],
    }


# -----------------------------------------------------------------------------
# Door
# -----------------------------------------------------------------------------

DOOR_SCHEMA = _wrap_rows(
    _row_schema(
        "mark",
        width="Width (e.g. '3'-0\"', '36 in')",
        height="Height (e.g. '7'-0\"', '84 in')",
        thickness="Thickness if listed (e.g. '1-3/4\"')",
        material="Door material (HM, WD, AL, GL, etc.)",
        frame="Frame type / material",
        hardware="Hardware group / set (e.g. 'HW-1')",
        fire_rating="Fire rating (e.g. '20 min', '60 min', 'NR')",
        glazing="Glazing if any (e.g. 'IG vision lite')",
        finish="Door finish (paint, stain color, etc.)",
        louver="Louver if present (e.g. '24x12 LV')",
        remarks="Remarks column free text",
    ),
    "door schedule",
)


# -----------------------------------------------------------------------------
# Window
# -----------------------------------------------------------------------------

WINDOW_SCHEMA = _wrap_rows(
    _row_schema(
        "mark",
        type="Window type / configuration (fixed, casement, awning, etc.)",
        width="Rough opening width",
        height="Rough opening height",
        frame_material="Frame material (alum, vinyl, wood)",
        glazing="Glazing spec (1\" IG, low-E, tempered, etc.)",
        manufacturer="Manufacturer + model number",
        u_value="U-value if listed",
        shgc="SHGC if listed",
        operation="Operation (FX/CSMT/AWN/SH/DH)",
        head_height="Head height AFF",
        sill_height="Sill height AFF",
        remarks="Remarks column",
    ),
    "window schedule",
)


# -----------------------------------------------------------------------------
# Finish
# -----------------------------------------------------------------------------

FINISH_SCHEMA = _wrap_rows(
    _row_schema(
        "room",
        room_number="Room number if separate from name",
        floor="Floor finish (CPT, VCT, EPOXY, etc. + manufacturer / pattern if listed)",
        base="Base finish (RBR base, CT base, etc.)",
        north_wall="N wall finish",
        south_wall="S wall finish",
        east_wall="E wall finish",
        west_wall="W wall finish",
        wall_finish="Wall finish (when not split by direction)",
        ceiling="Ceiling finish (ACT, GYP, EXP, etc.) + height if listed",
        ceiling_height="Ceiling height AFF",
        remarks="Remarks column",
    ),
    "finish schedule",
)


# -----------------------------------------------------------------------------
# Room
# -----------------------------------------------------------------------------

ROOM_SCHEMA = _wrap_rows(
    _row_schema(
        "room_number",
        name="Room name (e.g. 'CONFERENCE 201', 'CORRIDOR')",
        area_sf="Area in SF if listed",
        occupancy="Occupancy classification per IBC if shown",
        occupant_load="Occupant load count if shown",
        function="Function description",
        remarks="Remarks column",
    ),
    "room schedule",
)


# -----------------------------------------------------------------------------
# Fixture (plumbing fixture schedule)
# -----------------------------------------------------------------------------

FIXTURE_SCHEMA = _wrap_rows(
    _row_schema(
        "tag",
        description="Fixture description (water closet, lavatory, urinal, drinking fountain, etc.)",
        manufacturer="Manufacturer + model number",
        mounting="Mounting (wall-hung, floor-mount, etc.)",
        cw_size="Cold water connection size",
        hw_size="Hot water connection size",
        waste_size="Waste / drain connection size",
        vent_size="Vent connection size",
        flush_type="Flush valve / flushometer / tank-type",
        ada="ADA-compliant flag (Y/N)",
        remarks="Remarks column",
    ),
    "fixture (plumbing) schedule",
)


# -----------------------------------------------------------------------------
# Panel (electrical panel schedule)
# -----------------------------------------------------------------------------

PANEL_SCHEMA = _wrap_rows(
    _row_schema(
        "panel_id",
        location="Panel location (e.g. 'STORAGE 115')",
        voltage="System voltage (e.g. '208/120V')",
        phase="Phase (1 or 3)",
        wires="Number of wires (3W, 4W)",
        mains_amps="Main breaker / lugs amps",
        bus_amps="Bus rating amps",
        aic_rating="AIC interrupt rating",
        mounting="Surface / flush",
        feed_from="Upstream feeder source",
        circuit_count="Total circuits / spaces",
        remarks="Remarks column",
    ),
    "electrical panel schedule",
)


# -----------------------------------------------------------------------------
# Equipment (HVAC / mechanical equipment schedule, also catches misc equipment)
# -----------------------------------------------------------------------------

EQUIPMENT_SCHEMA = _wrap_rows(
    _row_schema(
        "tag",
        description="Equipment description (RTU, AHU, PUMP, EF, etc.)",
        make="Manufacturer",
        model="Model number",
        capacity="Capacity (CFM, BTU, GPM, HP, etc. — include units)",
        voltage="Voltage",
        phase="Phase",
        amps="Amp rating (FLA / MCA)",
        weight="Operating weight if listed",
        location="Location served / served by",
        connection="Pipe / duct / electrical connections summary",
        remarks="Remarks column",
    ),
    "equipment schedule",
)


# -----------------------------------------------------------------------------
# Type → schema dispatch
# -----------------------------------------------------------------------------


SCHEMA_BY_TYPE: dict[str, dict] = {
    "door": DOOR_SCHEMA,
    "window": WINDOW_SCHEMA,
    "finish": FINISH_SCHEMA,
    "room": ROOM_SCHEMA,
    "fixture": FIXTURE_SCHEMA,
    "panel": PANEL_SCHEMA,
    "equipment": EQUIPMENT_SCHEMA,
}


def schema_for(schedule_type: str) -> dict | None:
    """Return the per-type input_schema, or None for 'other' / unknown."""
    return SCHEMA_BY_TYPE.get(schedule_type)
