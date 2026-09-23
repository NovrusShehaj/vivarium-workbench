"""Install and remove workspace-local composite files.

Destination paths are derived from a sanitized stem under
``<package_path>/composites/``. Client-supplied paths are ignored.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from vivarium_workbench.lib import composite_lookup
from vivarium_workbench.lib.atomic_io import atomic_write_text
from vivarium_workbench.lib.composite_schema import (
    MAX_BYTES,
    SCHEMA_VERSION,
    issue,
    validate_composite_document,
    validate_stem,
)

_SUFFIXES = (".composite.json", ".composite.yaml", ".composite.yml")


def catalog_dir(ws_root: Path) -> tuple[Path, str] | tuple[None, dict]:
    """Return ``(directory, package)`` or ``(None, error_body)``."""
    ws_root = Path(ws_root)
    manifest = ws_root / "workspace.yaml"
    try:
        data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        return None, {
            "category": "io",
            "message": f"failed to read workspace.yaml: {exc}",
            "errors": [issue("io", f"failed to read workspace.yaml: {exc}", file="workspace.yaml")],
        }
    if not isinstance(data, dict):
        return None, {
            "category": "io",
            "message": "workspace.yaml must be a mapping.",
            "errors": [issue("io", "workspace.yaml must be a mapping.", file="workspace.yaml")],
        }
    pkg = data.get("package_path") or ("pbg_" + str(data.get("name") or "").replace("-", "_"))
    if not isinstance(pkg, str) or not pkg or "/" in pkg or "\\" in pkg or ".." in pkg:
        return None, {
            "category": "io",
            "message": "workspace.yaml package_path is not a single path segment.",
            "errors": [issue("io", "package_path must be a single directory name.", file="workspace.yaml")],
        }
    directory = (ws_root / pkg / "composites")
    if not _confined(directory, ws_root):
        return None, {
            "category": "io",
            "message": "composite catalog directory escapes the workspace.",
            "errors": [issue("io", "composite catalog directory escapes the workspace.")],
        }
    return directory, pkg


def install_document(
    ws_root: Path,
    stem: str,
    document: Any,
    *,
    replace: bool = False,
) -> tuple[dict, int]:
    stem_error = validate_stem(stem)
    if stem_error:
        return {"error": stem_error["message"], "errors": [stem_error]}, 400
    errors, warnings = validate_composite_document(document)
    if errors:
        return {"error": errors[0]["message"], "errors": errors, "warnings": warnings}, 400
    try:
        payload = json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    except (TypeError, ValueError) as exc:
        err = issue("syntax", f"document is not strict JSON: {exc}")
        return {"error": err["message"], "errors": [err]}, 400
    if len(payload.encode("utf-8")) > MAX_BYTES:
        err = issue("schema", f"document exceeds {MAX_BYTES} bytes.", hint="Split the composite.")
        return {"error": err["message"], "errors": [err]}, 400

    located = catalog_dir(ws_root)
    if located[0] is None:
        body = located[1]
        assert isinstance(body, dict)
        return {"error": body["message"], "errors": body.get("errors") or []}, 500
    directory, pkg = located
    assert isinstance(directory, Path)

    existing = _existing(directory, stem)
    if existing and not replace:
        rel = _rel(ws_root, existing[0])
        err = issue(
            "conflict",
            f"{existing[0].name} already exists. Choose another stem or import with replace.",
            file=rel,
            hint="Set replace to true to overwrite a workspace composite with this stem.",
        )
        return {"error": err["message"], "errors": [err]}, 409

    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{stem}.composite.json"
    if not _confined(target, ws_root) or _escapes(target, directory):
        err = issue("io", "refusing to write outside the workspace catalog.")
        return {"error": err["message"], "errors": [err]}, 403
    try:
        atomic_write_text(target, payload)
    except Exception as exc:  # noqa: BLE001
        err = issue("io", f"Could not write { _rel(ws_root, directory) } ({exc}).", file=_rel(ws_root, target))
        return {"error": err["message"], "errors": [err]}, 500

    removed_twins: list[str] = []
    if replace:
        for twin in existing:
            if twin == target:
                continue
            if twin.is_file() and not _escapes(twin, directory):
                twin.unlink()
                removed_twins.append(_rel(ws_root, twin))
    if removed_twins:
        warnings = list(warnings) + [issue(
            "conflict",
            "Removed the YAML twin so this stem has a single catalog file.",
            file=removed_twins[0],
            severity="warning",
            hint="The JSON file is now the catalog entry.",
        )]
    return {
        "ok": True,
        "id": f"{pkg}.composites.{stem}",
        "name": document.get("name"),
        "path": _rel(ws_root, target),
        "origin": "workspace",
        "schemaVersion": document.get("schemaVersion") if isinstance(document.get("schemaVersion"), int) else SCHEMA_VERSION,
        "warnings": warnings,
    }, 200


def remove_stem(ws_root: Path, stem: str) -> tuple[dict, int]:
    stem_error = validate_stem(stem)
    if stem_error:
        return {"error": stem_error["message"], "errors": [stem_error]}, 400
    located = catalog_dir(ws_root)
    if located[0] is None:
        body = located[1]
        assert isinstance(body, dict)
        return {"error": body["message"], "errors": body.get("errors") or []}, 500
    directory, _pkg = located
    assert isinstance(directory, Path)
    existing = [p for p in _existing(directory, stem) if not _escapes(p, directory)]
    escaped = [p for p in _existing(directory, stem) if _escapes(p, directory)]
    if escaped:
        err = issue("io", "refusing to delete a file that escapes the catalog directory.")
        return {"error": err["message"], "errors": [err]}, 403
    if not existing:
        foreign = _foreign_origin(ws_root, stem)
        if foreign:
            err = issue(
                "conflict",
                f"Cannot remove {foreign} composite {stem!r}. Only workspace files can be deleted.",
                hint="Installed, federated, and generator composites stay in their own source.",
            )
            return {"error": err["message"], "errors": [err], "origin": foreign}, 403
        err = issue("io", f"no workspace composite named {stem!r}.")
        return {"error": err["message"], "errors": [err]}, 404
    removed = []
    for path in existing:
        path.unlink()
        removed.append(_rel(ws_root, path))
    return {"ok": True, "removed": removed, "stem": stem}, 200


def _existing(directory: Path, stem: str) -> list[Path]:
    if not directory.is_dir():
        return []
    found = []
    for suffix in _SUFFIXES:
        candidate = directory / f"{stem}{suffix}"
        if candidate.is_file() or candidate.is_symlink():
            found.append(candidate)
    return found


def _foreign_origin(ws_root: Path, stem: str) -> str | None:
    """Origin of a non-workspace composite whose id ends with this stem, if any."""
    located = catalog_dir(ws_root)
    if located[0] is None:
        return None
    _directory, pkg = located
    try:
        specs = composite_lookup.discover_all_composites(Path(ws_root), pkg)
    except Exception:  # noqa: BLE001
        return None
    suffix = f".composites.{stem}"
    for spec_id, rec in specs.items():
        if not spec_id.endswith(suffix):
            continue
        origin = rec.get("origin") or ""
        if origin and origin != "workspace":
            return str(origin)
        if rec.get("read_only") or rec.get("kind") == "generator":
            return str(origin or rec.get("kind") or "installed")
    return None


def _confined(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _escapes(path: Path, directory: Path) -> bool:
    """True when a symlink (or resolved path) leaves the catalog directory."""
    try:
        if path.is_symlink():
            path.resolve().relative_to(directory.resolve())
            return False
        path.resolve().relative_to(directory.resolve())
    except ValueError:
        return True
    return False


def _rel(ws_root: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ws_root.resolve()))
    except ValueError:
        return str(path)
