"""Versioned structural checks for process-bigraph composite documents.

A composite file is the document ``load_spec`` already reads: ``name`` plus
``state``, with optional authoring metadata. ``schemaVersion`` is optional and
defaults to 1 so existing YAML and JSON composites stay valid.

This module does not resolve ``local:`` process addresses and does not fetch
``$schema``. Callers turn the returned issue dicts into API errors.
"""
from __future__ import annotations

import re
from typing import Any

SCHEMA_VERSION = 1
SCHEMA_URL = "https://vivarium.science/schemas/composite-1.schema.json"
MAX_BYTES = 1_048_576
MAX_DEPTH = 64
MAX_NODES = 10_000
STEM_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
_PLACEHOLDER = re.compile(r"\$\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
# ``string`` is the alias ``substitute_parameters`` already casts like ``str``.
_PARAM_TYPES = {"float", "int", "str", "string", "bool"}


def issue(
    category: str,
    message: str,
    *,
    path: str = "",
    hint: str = "",
    file: str = "",
    severity: str = "error",
) -> dict[str, str]:
    out = {"category": category, "path": path, "message": message, "severity": severity}
    if hint:
        out["hint"] = hint
    if file:
        out["file"] = file
    return out


def validate_stem(stem: str) -> dict[str, str] | None:
    if not isinstance(stem, str) or not STEM_RE.fullmatch(stem):
        return issue(
            "schema",
            "stem must be 1–64 characters, start with a letter, and contain only "
            "letters, digits, underscores, or hyphens.",
            path="/stem",
            hint="Rename the file so its stem matches that pattern. "
            "Slashes, dots, and leading digits are not allowed.",
        )
    return None


def validate_composite_document(doc: Any) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Return ``(errors, warnings)``. Errors reject the document. Warnings do not."""
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    if not isinstance(doc, dict):
        errors.append(issue(
            "schema",
            "A composite document must be a JSON object.",
            path="",
            hint="The file's top level should be an object with name and state.",
        ))
        return errors, warnings

    schema_url = doc.get("$schema")
    if schema_url is not None:
        if not isinstance(schema_url, str) or not schema_url.endswith("composite-1.schema.json"):
            errors.append(issue(
                "schema",
                "$schema must point at composite-1.schema.json.",
                path="/$schema",
                hint=f"Use {SCHEMA_URL}.",
            ))

    if "schemaVersion" in doc and doc.get("schemaVersion") is not None:
        version = doc.get("schemaVersion")
        if not isinstance(version, int) or isinstance(version, bool):
            errors.append(issue(
                "unsupported_schema",
                "schemaVersion must be an integer.",
                path="/schemaVersion",
                hint="Omit schemaVersion or set it to 1.",
            ))
        elif version != SCHEMA_VERSION:
            errors.append(issue(
                "unsupported_schema",
                f"schemaVersion {version} is not supported by this workbench (max {SCHEMA_VERSION}).",
                path="/schemaVersion",
                hint="Remove schemaVersion or set it to 1. This workbench will not rewrite the file.",
            ))

    name = doc.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append(issue(
            "schema",
            "name is required.",
            path="/name",
            hint="Add a non-empty name.",
        ))
    elif len(name) > 200:
        errors.append(issue("schema", "name must be at most 200 characters.", path="/name"))

    state = doc.get("state")
    if not isinstance(state, dict):
        errors.append(issue(
            "schema",
            "state must be a JSON object.",
            path="/state",
            hint="The process-bigraph state tree is an object of stores and processes, not an array.",
        ))
    else:
        _walk_state(state, doc.get("parameters"), errors, warnings)

    _check_text(doc, "description", 8_000, errors)
    _check_text(doc, "author", 200, errors)
    _check_text(doc, "version", 64, errors)
    _check_tags(doc.get("tags"), errors)
    _check_parameters(doc.get("parameters"), errors)
    _check_requires(doc.get("requires"), errors)
    return errors, warnings


def _check_text(doc: dict, key: str, limit: int, errors: list[dict[str, str]]) -> None:
    if key not in doc or doc[key] is None:
        return
    value = doc[key]
    if not isinstance(value, str):
        errors.append(issue("schema", f"{key} must be a string.", path=f"/{key}"))
    elif len(value) > limit:
        errors.append(issue("schema", f"{key} must be at most {limit} characters.", path=f"/{key}"))


def _check_tags(tags: Any, errors: list[dict[str, str]]) -> None:
    if tags is None:
        return
    if not isinstance(tags, list):
        errors.append(issue("schema", "tags must be an array of strings.", path="/tags"))
        return
    if len(tags) > 32:
        errors.append(issue("schema", "tags must contain at most 32 entries.", path="/tags"))
    for i, tag in enumerate(tags):
        if not isinstance(tag, str):
            errors.append(issue("schema", "each tag must be a string.", path=f"/tags/{i}"))
        elif len(tag) > 64:
            errors.append(issue("schema", "each tag must be at most 64 characters.", path=f"/tags/{i}"))


def _check_parameters(params: Any, errors: list[dict[str, str]]) -> set[str] | None:
    if params is None:
        return set()
    if not isinstance(params, dict):
        errors.append(issue("schema", "parameters must be an object.", path="/parameters"))
        return None
    names: set[str] = set()
    for key, value in params.items():
        pointer = "/parameters/" + _pointer_token(str(key))
        if not isinstance(key, str):
            errors.append(issue("schema", "parameter names must be strings.", path=pointer))
            continue
        names.add(key)
        if not isinstance(value, dict):
            errors.append(issue(
                "schema",
                f"parameter {key} must be an object.",
                path=pointer,
                hint='Use {"type": "float", "default": 1}.',
            ))
            continue
        kind = value.get("type")
        if kind is not None and kind not in _PARAM_TYPES:
            errors.append(issue(
                "schema",
                f"parameter {key} type must be one of float, int, str, string, bool.",
                path=pointer + "/type",
            ))
    return names


def _check_requires(requires: Any, errors: list[dict[str, str]]) -> None:
    if requires is None:
        return
    if not isinstance(requires, dict):
        errors.append(issue("schema", "requires must be an object.", path="/requires"))
        return
    for key in ("processes", "steps"):
        if key not in requires or requires[key] is None:
            continue
        values = requires[key]
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            errors.append(issue(
                "schema",
                f"requires.{key} must be an array of strings.",
                path=f"/requires/{key}",
            ))


def _walk_state(
    state: dict,
    params: Any,
    errors: list[dict[str, str]],
    warnings: list[dict[str, str]],
) -> None:
    stack: list[tuple[Any, int, str]] = [(state, 1, "/state")]
    nodes = 0
    while stack:
        node, depth, path = stack.pop()
        nodes += 1
        if nodes > MAX_NODES:
            errors.append(issue(
                "semantic",
                f"state exceeds {MAX_NODES} nodes.",
                path=path,
                hint="Split the composite or remove unused branches.",
            ))
            return
        if depth > MAX_DEPTH:
            errors.append(issue(
                "semantic",
                f"state nesting exceeds {MAX_DEPTH} levels.",
                path=path,
                hint="Flatten the state tree.",
            ))
            return
        if isinstance(node, dict):
            if "address" in node:
                _check_address(node.get("address"), path + "/address", errors)
            for key, value in node.items():
                child = path + "/" + _pointer_token(str(key))
                if isinstance(value, (dict, list)):
                    stack.append((value, depth + 1, child))
                elif isinstance(value, str):
                    _check_placeholder_string(value, params, warnings, child)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                child = f"{path}/{index}"
                if isinstance(value, (dict, list)):
                    stack.append((value, depth + 1, child))
                elif isinstance(value, str):
                    _check_placeholder_string(value, params, warnings, child)


def _check_placeholder_string(
    value: str,
    params: Any,
    warnings: list[dict[str, str]],
    path: str,
) -> None:
    if not isinstance(params, dict):
        return
    for match in _PLACEHOLDER.finditer(value):
        pname = match.group(1)
        if pname not in params:
            warnings.append(issue(
                "semantic",
                f"${{{pname}}} does not match a parameter.",
                path=path,
                hint=f"Add parameters.{pname} or change the placeholder.",
                severity="warning",
            ))


def _check_address(value: Any, path: str, errors: list[dict[str, str]]) -> None:
    if not isinstance(value, str):
        errors.append(issue("schema", "address must be a string.", path=path))
        return
    if "\x00" in value or ".." in value:
        errors.append(issue(
            "semantic",
            "address must not contain '..' or a NUL character.",
            path=path,
            hint="Use a process address such as local:ProcessName.",
        ))
        return
    lowered = value.lower()
    if value.startswith("/") or value.startswith("\\") or lowered.startswith("file:"):
        errors.append(issue(
            "semantic",
            "address must be a process address, not a filesystem path.",
            path=path,
            hint="Use a process address such as local:ProcessName.",
        ))


def _pointer_token(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")
