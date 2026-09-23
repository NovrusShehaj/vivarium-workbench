"""Type-specific validation of proposed file content (never executes it).

* ``workspace.yaml`` / ``study.yaml`` / ``investigation.yaml`` — YAML parse,
  then JSON-Schema validation against the workspace's own
  ``.pbg/schemas/<kind>.schema.json`` (Draft 7, as the core validates).
* other ``.yaml``/``.yml`` — YAML parse; ``.json`` — JSON parse;
  ``.toml`` — TOML parse; ``.py`` — ``ast.parse`` (syntax only).

``executable_code`` flags content that runs when simulations execute (the
workspace package, ``.py`` files, packaging files) so the review UI can warn.
"""
from __future__ import annotations

import ast
import json
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

SCHEMA_FILES = {"workspace.yaml": "workspace", "study.yaml": "study", "investigation.yaml": "investigation"}
PACKAGING_FILES = {"pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "uv.lock", "environment.yml"}


def _schema_for(ws_root: Path, kind: str) -> dict[str, Any] | None:
    from vivarium_workbench.lib.workspace_paths import WorkspacePaths
    try:
        path = WorkspacePaths.load(ws_root).pbg / "schemas" / f"{kind}.schema.json"
    except Exception:  # noqa: BLE001 - a broken workspace.yaml still validates syntax
        path = Path(ws_root) / ".pbg" / "schemas" / f"{kind}.schema.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    return data if isinstance(data, dict) else None


def validate(ws_root: Path, rel: str, content: str | None) -> list[dict[str, Any]]:
    """Validation results for ``rel`` with ``content`` (``None`` = deletion)."""
    if content is None:
        return [{"kind": "delete", "ok": True, "message": "file will be deleted"}]
    name = PurePosixPath(rel).name
    suffix = PurePosixPath(rel).suffix.lower()
    results: list[dict[str, Any]] = []
    if suffix in (".yaml", ".yml"):
        try:
            data = yaml.safe_load(content)
            results.append({"kind": "yaml", "ok": True, "message": "valid YAML"})
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            where = f" (line {mark.line + 1})" if mark is not None else ""
            return [{"kind": "yaml", "ok": False, "message": f"invalid YAML{where}: {getattr(exc, 'problem', exc)}"}]
        kind = SCHEMA_FILES.get(name)
        if kind:
            schema = _schema_for(ws_root, kind)
            if schema is None:
                results.append({"kind": "schema", "ok": True,
                                "message": f"no {kind}.schema.json in .pbg/schemas; schema not checked"})
            else:
                from jsonschema import Draft7Validator, FormatChecker
                errors = sorted(Draft7Validator(schema, format_checker=FormatChecker()).iter_errors(data),
                                key=lambda e: list(e.absolute_path))
                if errors:
                    for e in errors[:5]:
                        loc = "/".join(str(p) for p in e.absolute_path) or "(root)"
                        results.append({"kind": "schema", "ok": False, "message": f"{loc}: {e.message}"[:300]})
                else:
                    results.append({"kind": "schema", "ok": True, "message": f"matches {kind}.schema.json"})
    elif suffix == ".json":
        try:
            json.loads(content)
            results.append({"kind": "json", "ok": True, "message": "valid JSON"})
        except ValueError as exc:
            results.append({"kind": "json", "ok": False, "message": f"invalid JSON: {exc}"[:300]})
    elif suffix == ".toml":
        try:
            tomllib.loads(content)
            results.append({"kind": "toml", "ok": True, "message": "valid TOML"})
        except tomllib.TOMLDecodeError as exc:
            results.append({"kind": "toml", "ok": False, "message": f"invalid TOML: {exc}"[:300]})
    elif suffix == ".py":
        try:
            ast.parse(content, filename=rel)
            results.append({"kind": "python", "ok": True, "message": "Python syntax OK (not executed)"})
        except SyntaxError as exc:
            results.append({"kind": "python", "ok": False,
                            "message": f"syntax error at line {exc.lineno}: {exc.msg}"[:300]})
    return results


def package_path(ws_root: Path) -> str | None:
    try:
        data = yaml.safe_load((Path(ws_root) / "workspace.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    pkg = data.get("package_path") or ("pbg_" + str(data.get("name", "")).replace("-", "_"))
    return str(pkg) if pkg and pkg != "pbg_" else None


def executable_code(ws_root: Path, rel: str) -> bool:
    p = PurePosixPath(rel)
    if p.suffix.lower() in (".py", ".pyx", ".so", ".sh"):
        return True
    if p.name in PACKAGING_FILES or p.name.startswith("requirements"):
        return True
    pkg = package_path(ws_root)
    if not pkg:
        return False
    return rel == pkg or rel.startswith(pkg + "/")
