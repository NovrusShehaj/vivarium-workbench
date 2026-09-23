"""Install, replace, and remove workspace composite JSON files."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from vivarium_workbench.lib import composite_files as files
from vivarium_workbench.lib import composite_mutations as cm
from vivarium_workbench.lib.composite_lookup import composites_data
from vivarium_workbench.lib.composite_schema import MAX_BYTES
from vivarium_workbench.lib.workspace_manifest_views import filter_composites
from process_bigraph.composite_spec import CompositeSpec


def _ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "workspace.yaml").write_text(
        "schema_version: 3\nname: testws\npackage_path: pbg_testws\n",
        encoding="utf-8",
    )
    return ws


def _doc(name: str = "custom-demo") -> dict:
    return {
        "schemaVersion": 1,
        "name": name,
        "state": {
            "increase": {
                "_type": "process",
                "address": "local:IncreaseProcess",
                "config": {"rate": "${rate}"},
            }
        },
        "parameters": {"rate": {"type": "float", "default": 1.0}},
    }


def test_import_lists_beside_existing_yaml_and_persists(tmp_path: Path):
    ws = _ws(tmp_path)
    catalog = ws / "pbg_testws" / "composites"
    catalog.mkdir(parents=True)
    (catalog / "existing.composite.yaml").write_text(
        yaml.safe_dump({"name": "existing", "state": {"stores": {"level": 0}}}),
        encoding="utf-8",
    )
    body, status = cm.import_composite(ws, {"stem": "custom-demo", "document": _doc()})
    assert status == 200, body
    assert body["id"] == "pbg_testws.composites.custom-demo"
    assert body["origin"] == "workspace"
    target = catalog / "custom-demo.composite.json"
    assert target.is_file()
    listed = composites_data(ws)
    ids = {row["id"]: row for row in listed["composites"]}
    assert "pbg_testws.composites.custom-demo" in ids
    assert ids["pbg_testws.composites.custom-demo"]["origin"] == "workspace"
    assert "pbg_testws.composites.existing" in ids
    again = composites_data(ws)
    assert any(row["id"] == body["id"] for row in again["composites"])
    spec = CompositeSpec.from_file(target)
    assert spec is not None


def test_second_import_without_replace_keeps_bytes(tmp_path: Path):
    ws = _ws(tmp_path)
    first, status = cm.import_composite(ws, {"stem": "custom-demo", "document": _doc("first")})
    assert status == 200, first
    path = ws / first["path"]
    before = path.read_bytes()
    body, status = cm.import_composite(ws, {"stem": "custom-demo", "document": _doc("second")})
    assert status == 409
    assert path.read_bytes() == before


def test_replace_updates_and_failed_validation_does_not_truncate(tmp_path: Path):
    ws = _ws(tmp_path)
    cm.import_composite(ws, {"stem": "custom-demo", "document": _doc("first")})
    path = ws / "pbg_testws" / "composites" / "custom-demo.composite.json"
    body, status = cm.import_composite(
        ws, {"stem": "custom-demo", "document": _doc("second"), "replace": True},
    )
    assert status == 200, body
    assert json.loads(path.read_text(encoding="utf-8"))["name"] == "second"
    before = path.read_bytes()
    rejected, status = cm.import_composite(
        ws, {"stem": "custom-demo", "document": {"name": "no-state"}, "replace": True},
    )
    assert status == 400
    assert path.read_bytes() == before
    assert rejected["errors"]


def test_remove_only_the_workspace_file(tmp_path: Path):
    ws = _ws(tmp_path)
    cm.import_composite(ws, {"stem": "custom-demo", "document": _doc()})
    other = ws / "pbg_testws" / "composites" / "keep.composite.yaml"
    other.write_text(yaml.safe_dump({"name": "keep", "state": {}}), encoding="utf-8")
    body, status = cm.remove_composite(ws, "custom-demo")
    assert status == 200, body
    assert not (ws / "pbg_testws" / "composites" / "custom-demo.composite.json").exists()
    assert other.is_file()


@pytest.mark.parametrize("origin", ["installed", "federated", "generator"])
def test_remove_non_workspace_origin_is_forbidden(tmp_path: Path, monkeypatch, origin: str):
    ws = _ws(tmp_path)

    def _fake_discover(root, package, errors=None):
        return {"other.composites.foreign": {"origin": origin, "kind": "spec", "read_only": True}}

    monkeypatch.setattr(
        "vivarium_workbench.lib.composite_lookup.discover_all_composites",
        _fake_discover,
    )
    body, status = cm.remove_composite(ws, "foreign")
    assert status == 403, body
    assert body["origin"] == origin


def test_missing_directory_is_created(tmp_path: Path):
    ws = _ws(tmp_path)
    body, status = cm.import_composite(ws, {"stem": "custom-demo", "document": _doc()})
    assert status == 200, body
    assert (ws / "pbg_testws" / "composites" / "custom-demo.composite.json").is_file()


def test_broken_json_does_not_drop_valid_files(tmp_path: Path):
    ws = _ws(tmp_path)
    catalog = ws / "pbg_testws" / "composites"
    catalog.mkdir(parents=True)
    (catalog / "good.composite.yaml").write_text(
        yaml.safe_dump({"name": "good", "state": {"level": 1}}),
        encoding="utf-8",
    )
    (catalog / "also.composite.json").write_text(
        json.dumps({"name": "also", "state": {"level": 2}}),
        encoding="utf-8",
    )
    (catalog / "broken.composite.json").write_text("{", encoding="utf-8")
    listed = composites_data(ws)
    ids = {row["id"] for row in listed["composites"]}
    assert "pbg_testws.composites.good" in ids
    assert "pbg_testws.composites.also" in ids
    assert "pbg_testws.composites.broken" not in ids
    assert any(err["category"] == "syntax" and "broken" in err["file"] for err in listed["composite_errors"])


def test_json_shadows_yaml_twin(tmp_path: Path):
    ws = _ws(tmp_path)
    catalog = ws / "pbg_testws" / "composites"
    catalog.mkdir(parents=True)
    (catalog / "twin.composite.yaml").write_text(
        yaml.safe_dump({"name": "from-yaml", "state": {}}),
        encoding="utf-8",
    )
    (catalog / "twin.composite.json").write_text(
        json.dumps({"name": "from-json", "state": {}}),
        encoding="utf-8",
    )
    listed = composites_data(ws)
    match = [row for row in listed["composites"] if row["id"] == "pbg_testws.composites.twin"]
    assert len(match) == 1
    assert match[0]["name"] == "from-json"
    assert match[0]["format"] == "json"
    assert any(err.get("severity") == "warning" and "shadowed" in err["message"] for err in listed["composite_errors"])


def test_symlink_outside_workspace_is_refused(tmp_path: Path):
    ws = _ws(tmp_path)
    catalog = ws / "pbg_testws" / "composites"
    catalog.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"name": "escaped", "state": {}}), encoding="utf-8")
    (catalog / "escaped.composite.json").symlink_to(outside)
    listed = composites_data(ws)
    assert all(row["id"] != "pbg_testws.composites.escaped" for row in listed["composites"])
    assert any(err["category"] == "io" for err in listed["composite_errors"])


def test_oversized_file_is_not_loaded(tmp_path: Path):
    ws = _ws(tmp_path)
    catalog = ws / "pbg_testws" / "composites"
    catalog.mkdir(parents=True)
    blob = catalog / "huge.composite.json"
    blob.write_bytes(b"{" + b" " * (MAX_BYTES + 10))
    listed = composites_data(ws)
    assert all("huge" not in row["id"] for row in listed["composites"])
    assert any("exceeds" in err["message"] for err in listed["composite_errors"])


def test_unwritable_directory_reports_io(tmp_path: Path, monkeypatch):
    ws = _ws(tmp_path)

    def _boom(path, text):
        raise PermissionError("read-only")

    monkeypatch.setattr("vivarium_workbench.lib.composite_files.atomic_write_text", _boom)
    body, status = cm.import_composite(ws, {"stem": "custom-demo", "document": _doc()})
    assert status == 500
    assert body["errors"][0]["category"] == "io"
    assert not (ws / "pbg_testws" / "composites" / "custom-demo.composite.json").exists()


def test_workspace_origin_survives_allow_list():
    records = [
        {"id": "local", "origin": "workspace", "workspace_local": False, "module": "not_listed"},
        {"id": "hidden", "origin": "installed", "module": "other_pkg.composites.y"},
        {"id": "kept", "origin": "installed", "module": "pbg_testws.composites.z"},
    ]
    ws_data = {"dashboard": {"registry": {"include": ["pbg_testws"]}}}
    kept = {row["id"] for row in filter_composites(records, ws_data)}
    assert "local" in kept
    assert "kept" in kept
    assert "hidden" not in kept
