"""Top-level allOf/anyOf/oneOf must be flattened before the schema reaches Claude."""
from food_console.mcp_bridge import normalize_schema


def test_plain_schema_unchanged():
    raw = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}
    schema, note = normalize_schema(raw)
    assert schema == raw and note == ""


def test_oneof_flattened_with_note():
    raw = {
        "type": "object",
        "properties": {"limit": {"type": "integer"}},
        "oneOf": [
            {"properties": {"address_id": {"type": "string"}}, "required": ["address_id"]},
            {"properties": {"lat": {"type": "number"}, "lng": {"type": "number"}}, "required": ["lat", "lng"]},
        ],
    }
    schema, note = normalize_schema(raw)
    assert not any(k in schema for k in ("oneOf", "anyOf", "allOf"))
    assert set(schema["properties"]) == {"limit", "address_id", "lat", "lng"}
    assert "required" not in schema  # nothing is required by every branch
    assert note == "Input rule: provide exactly one of these field sets: {address_id} | {lat, lng}."
    assert "oneOf" in raw  # input not mutated


def test_allof_and_refs_and_missing_type():
    raw = {
        "$defs": {"Loc": {"properties": {"city": {"type": "string"}}, "required": ["city"]}},
        "allOf": [{"$ref": "#/$defs/Loc"}, {"properties": {"n": {"type": "integer"}}}],
    }
    schema, note = normalize_schema(raw)
    assert schema["type"] == "object"
    assert set(schema["properties"]) == {"city", "n"}
    assert schema["required"] == ["city"]
    assert note == ""


def test_anyof_common_required_kept():
    raw = {"anyOf": [{"required": ["a", "b"]}, {"required": ["a", "c"]}], "properties": {}}
    schema, note = normalize_schema(raw)
    assert schema["required"] == ["a"]
    assert note == "Input rule: provide at least one of these field sets: {b} | {c}."
