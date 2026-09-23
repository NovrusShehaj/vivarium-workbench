"""Structural validation for user-authored composite documents."""
from __future__ import annotations

from vivarium_workbench.lib.composite_schema import (
    validate_composite_document,
    validate_stem,
)


def _demo() -> dict:
    return {
        "name": "increase-demo",
        "description": "Trivial linear-growth composite.",
        "parameters": {"rate": {"type": "float", "default": 2.0}},
        "requires": {"processes": ["IncreaseProcess"]},
        "state": {
            "increase": {
                "_type": "process",
                "address": "local:IncreaseProcess",
                "config": {"rate": "${rate}"},
            },
            "stores": {"level": 0},
        },
    }


def test_valid_document_without_schema_version():
    errors, warnings = validate_composite_document(_demo())
    assert errors == []
    assert warnings == []


def test_unknown_field_is_accepted():
    doc = _demo()
    doc["author"] = "workspace"
    doc["extraNote"] = {"kept": True}
    errors, _warnings = validate_composite_document(doc)
    assert errors == []


def test_missing_name():
    doc = _demo()
    del doc["name"]
    errors, _warnings = validate_composite_document(doc)
    assert errors[0]["path"] == "/name"
    assert errors[0]["category"] == "schema"


def test_state_must_be_object():
    doc = _demo()
    doc["state"] = []
    errors, _warnings = validate_composite_document(doc)
    assert errors[0]["path"] == "/state"
    assert "object" in errors[0]["message"]


def test_bad_tag_type():
    doc = _demo()
    doc["tags"] = [1]
    errors, _warnings = validate_composite_document(doc)
    assert errors[0]["path"] == "/tags/0"


def test_unsupported_schema_version():
    doc = _demo()
    doc["schemaVersion"] = 2
    errors, _warnings = validate_composite_document(doc)
    assert errors[0]["category"] == "unsupported_schema"
    assert "max 1" in errors[0]["message"]


def test_string_parameter_type_is_accepted():
    doc = _demo()
    doc["parameters"]["title"] = {"type": "string", "default": "Smoldyn diffusion-reaction"}
    errors, _warnings = validate_composite_document(doc)
    assert errors == []


def test_missing_placeholder_is_a_warning():
    doc = _demo()
    doc["state"]["increase"]["config"]["rate"] = "${missing}"
    errors, warnings = validate_composite_document(doc)
    assert errors == []
    assert warnings
    assert warnings[0]["severity"] == "warning"
    assert "missing" in warnings[0]["message"]


def test_filesystem_address_is_rejected():
    doc = _demo()
    doc["state"]["increase"]["address"] = "../secrets"
    errors, _warnings = validate_composite_document(doc)
    assert errors[0]["category"] == "semantic"


def test_state_depth_is_capped():
    node: dict = {"level": 0}
    for _ in range(70):
        node = {"child": node}
    errors, _warnings = validate_composite_document({"name": "deep", "state": node})
    assert any("nesting exceeds" in err["message"] for err in errors)


def test_stem_rules():
    assert validate_stem("increase-demo") is None
    assert validate_stem("../x") is not None
    assert validate_stem("a/b") is not None
    assert validate_stem("") is not None
    assert validate_stem("1starts-with-digit") is not None
