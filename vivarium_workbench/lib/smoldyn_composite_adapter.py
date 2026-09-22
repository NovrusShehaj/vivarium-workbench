"""Composite → viva-smoldyn request adapter with contract pre-flight.

Phase 2 of the workbench SMS-retirement plan
(docs/superpowers/plans/2026-09-21-workbench-sms-retirement-and-smoldyn-backend-plan.md,
§5.2): translates a resolved Smoldyn composite payload (the same shape
``GET /api/composite-resolve`` returns — ``state`` wiring plus declared
``parameters``) into a ``POST /smoldyn/v1/simulations`` request document, and
pre-flights it against the endpoint's published contract and operator limits.

Why pre-flight: the endpoint accepts a documented subset (unimolecular
reactions only, ≤64 species, ≤128 reactions, four colors, counts-only) and
operator limits (MAX_SECONDS/MAX_PARTICLES/MAX_STEPS/MAX_SAMPLES/
MAX_BODY_BYTES). A composite outside the subset is *not remotely runnable*;
the adapter rejects it locally with the specific violated constraint named —
never a round-trip 422 the user cannot act on. Local execution remains
available for everything rejected here.

Pure functions, no HTTP and no workspace access — the caller resolves the
composite (``lib.composite_resolve.resolve_composite_for_request``) and sends
the built request (``lib.smoldyn_api_client.SmoldynApiClient``).
"""

from __future__ import annotations

import json
import math
from typing import Any

#: Operator limits published by viva-smoldyn (docs/api.md). Client-side copies:
#: the service enforces them authoritatively; mirroring them here only lets the
#: adapter reject early with a *named* constraint instead of a server error.
MAX_SECONDS = 15.0
MAX_PARTICLES = 10000
MAX_STEPS = 10000
MAX_SAMPLES = 101
MAX_BODY_BYTES = 65536

VALID_COLORS = ("black", "red", "green", "blue")
VALID_BOUNDARY = ("r", "p", "a")
_NAME_PATTERN_ERRORS = (
    "species/reaction names must start with a letter and use only ASCII "
    "letters, digits or underscores (max 64 chars)"
)


def extract_smoldyn_config(
    resolved: dict,
) -> "tuple[dict | None, Any, str | None]":
    """Find the Smoldyn process ``config`` in a resolved composite payload.

    Returns ``(config, node_interval, None)`` on success. ``node_interval`` is
    the Smoldyn node's step ``interval`` when declared at the STATE-NODE level
    (a sibling of ``config``) — how packaged viva-smoldyn composites declare
    it, since process-bigraph drives each process update with it; ``None``
    when the node declares none. Returns ``(None, None, reason)`` when the
    payload has no single Smoldyn process to translate (the reason names what
    was found, so a wiring mistake is diagnosable from the message alone).
    """
    state = (resolved or {}).get("state")
    if not isinstance(state, dict) or not state:
        return None, None, "composite has no state wiring (no processes to translate)"
    configs: list[tuple[dict, Any]] = []
    for _key, node in state.items():
        if not isinstance(node, dict):
            continue
        address = str(node.get("address") or "")
        if address.endswith("SmoldynProcess"):
            cfg = node.get("config")
            if isinstance(cfg, dict):
                configs.append((cfg, node.get("interval")))
    if not configs:
        return None, None, "no SmoldynProcess in the composite state (remote Smoldyn runs need one)"
    if len(configs) > 1:
        return None, None, (
            f"composite wires {len(configs)} SmoldynProcess nodes; the remote "
            "endpoint simulates one configuration per request"
        )
    cfg, node_interval = configs[0]
    return cfg, node_interval, None


def _substitute(value: Any, params: dict[str, Any]) -> Any:
    """Resolve ``${name}`` placeholders against declared parameter values.

    A whole-string ``"${name}"`` is replaced by the parameter value itself
    (preserving its type); ``${name}`` embedded in a longer string is replaced
    textually. Undeclared placeholders are left as-is — the pre-flight will
    name them. Matches the composite YAML substitution dialect.
    """
    if isinstance(value, str) and "${" in value:
        stripped = value.strip()
        if stripped.startswith("${") and stripped.endswith("}") and stripped.count("${") == 1:
            name = stripped[2:-1]
            if name in params:
                return params[name]
            return value  # unresolved — pre-flight names it
        for name, replacement in params.items():
            value = value.replace("${" + name + "}", str(replacement))
    return value


def _deep_substitute(node: Any, params: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        return {k: _deep_substitute(v, params) for k, v in node.items()}
    if isinstance(node, list):
        return [_deep_substitute(v, params) for v in node]
    return _substitute(node, params)


def _coerce(value: Any, name: str) -> float:
    """Coerce a (possibly numeric-string) scalar to float, naming failures."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"parameter {name!r} must be numeric, got {value!r}") from None
    if not math.isfinite(out):
        raise ValueError(f"parameter {name!r} must be finite, got {value!r}")
    return out


def build_request(
    resolved: dict,
    *,
    duration: float,
    interval: float | None = None,
    dt: float | None = None,
    seed: int | None = None,
    overrides: dict | None = None,
) -> dict:
    """Build a ``POST /smoldyn/v1/simulations`` request from a resolved payload.

    ``duration`` (simulation time) comes from the caller — the quick-run panel
    supplies it; a composite declares a step ``interval`` but no total run
    length. ``interval``/``dt``/``seed`` default to the composite's declared
    values (falling back to the endpoint's defaults when undeclared).
    ``overrides`` are user-supplied values for the composite's declared
    parameters (merged over parameter defaults, pre-substitution).

    Raises ``ValueError`` naming the first structural problem (no Smoldyn
    process, undeclared ``${param}`` left in a required field, non-numeric
    parameter). Use :func:`preflight` on the result for contract/limit
    violations before sending.
    """
    config, node_interval, reason = extract_smoldyn_config(resolved)
    if config is None:
        raise ValueError(reason or "composite has no SmoldynProcess config")

    # Declared parameter values: defaults overlaid with user overrides.
    params: dict[str, Any] = {}
    for name, spec in ((resolved or {}).get("parameters") or {}).items():
        if isinstance(spec, dict) and "default" in spec:
            params[name] = spec["default"]
    params.update(overrides or {})

    cfg = _deep_substitute(config, params)

    request: dict[str, Any] = {"duration": _coerce(duration, "duration")}

    # Species: {name: {count, difc, color, display_size}} — required by the
    # endpoint (min 1 species); the composite's config carries them as-is.
    species = cfg.get("species")
    if not isinstance(species, dict) or not species:
        raise ValueError("composite config has no species to simulate")
    cleaned_species: dict[str, dict[str, Any]] = {}
    for name, spec in species.items():
        if not isinstance(spec, dict):
            spec = {"count": spec}
        entry: dict[str, Any] = {}
        if "count" in spec:
            entry["count"] = int(_coerce(spec["count"], f"species {name}.count"))
        if "difc" in spec:
            entry["difc"] = _coerce(spec["difc"], f"species {name}.difc")
        if "color" in spec:
            entry["color"] = str(spec["color"])
        if "display_size" in spec:
            entry["display_size"] = _coerce(spec["display_size"], f"species {name}.display_size")
        cleaned_species[str(name)] = entry
    request["species"] = cleaned_species

    # Reactions (optional endpoint-side; composite configs list them inline).
    reactions = cfg.get("reactions")
    if isinstance(reactions, list) and reactions:
        cleaned_reactions = []
        for rxn in reactions:
            if not isinstance(rxn, dict):
                raise ValueError(f"reaction entry is not an object: {rxn!r}")
            cleaned_reactions.append({
                "name": str(rxn.get("name", "")),
                "subs": [str(s) for s in (rxn.get("subs") or [])],
                "prds": [str(p) for p in (rxn.get("prds") or [])],
                **({"rate": _coerce(rxn["rate"], f"reaction {rxn.get('name')}.rate")}
                   if "rate" in rxn else {}),
                **({"kb": _coerce(rxn["kb"], f"reaction {rxn.get('name')}.kb")}
                   if rxn.get("kb") is not None else {}),
            })
        request["reactions"] = cleaned_reactions

    if "dimensions" in cfg:
        request["dimensions"] = int(_coerce(cfg["dimensions"], "dimensions"))
    if cfg.get("bounds") is not None:
        bounds = cfg["bounds"]
        if not isinstance(bounds, list):
            raise ValueError("bounds must be a list of [low, high] pairs")
        request["bounds"] = [[_coerce(b[0], "bounds low"), _coerce(b[1], "bounds high")]
                             for b in bounds]
    if cfg.get("boundary_type") is not None:
        request["boundary_type"] = str(cfg["boundary_type"])
    if dt is not None:
        request["dt"] = _coerce(dt, "dt")
    elif cfg.get("dt") is not None:
        request["dt"] = _coerce(cfg["dt"], "dt")
    if interval is not None:
        request["interval"] = _coerce(interval, "interval")
    elif cfg.get("interval") is not None:
        request["interval"] = _coerce(cfg["interval"], "interval")
    elif node_interval is not None:
        # Packaged viva-smoldyn composites declare the step interval at the
        # STATE-NODE level (a sibling of ``config`` — process-bigraph drives
        # each process update with it), not inside the config dict.
        request["interval"] = _coerce(node_interval, "interval")
    elif "interval" in params:
        # The packaged composites declare the step interval as a composite
        # parameter (it drives the process update loop), not inside the
        # process config — fall back to the declared/default value.
        request["interval"] = _coerce(params["interval"], "interval")
    if seed is not None:
        request["seed"] = int(_coerce(seed, "seed"))
    elif cfg.get("seed") is not None:
        request["seed"] = int(_coerce(cfg["seed"], "seed"))
    return request


def preflight(request: dict) -> list[str]:
    """Validate a built request against the endpoint's published contract.

    Returns a list of human-readable violations — **empty means runnable**.
    Mirrors viva-smoldyn's request model and operator limits (the service
    remains authoritative; this exists so the UI can reject before a
    round-trip and name the constraint).
    """
    problems: list[str] = []

    duration = request.get("duration")
    if not isinstance(duration, (int, float)) or isinstance(duration, bool) \
            or not math.isfinite(duration) or duration <= 0:
        problems.append("duration must be a positive finite number")
        duration = None

    dt = request.get("dt", 0.01)
    if not isinstance(dt, (int, float)) or isinstance(dt, bool) \
            or not math.isfinite(dt) or dt <= 0:
        problems.append("dt must be a positive finite number")
        dt = None

    interval = request.get("interval", 1.0)
    if not isinstance(interval, (int, float)) or isinstance(interval, bool) \
            or not math.isfinite(interval) or interval <= 0:
        problems.append("interval must be a positive finite number")
        interval = None

    # Integer-multiple rule (service tolerance: 1e-9 relative/absolute).
    if duration and dt:
        ratio = duration / dt
        if ratio < 1 or not math.isclose(ratio, round(ratio), rel_tol=1e-9, abs_tol=1e-9):
            problems.append("duration must be an integer multiple of dt (tolerance 1e-9)")
        elif round(ratio) > MAX_STEPS:
            problems.append(
                f"run needs {round(ratio)} steps but the service cap is {MAX_STEPS} "
                "(MAX_STEPS) — raise dt or shorten duration"
            )
    if interval and dt:
        ratio = interval / dt
        if ratio < 1 or not math.isclose(ratio, round(ratio), rel_tol=1e-9, abs_tol=1e-9):
            problems.append("interval must be an integer multiple of dt (tolerance 1e-9)")
        elif duration and dt:
            steps = round(duration / dt)
            stride = round(interval / dt)
            samples = len({0, *range(stride, steps, stride), steps})
            if samples > MAX_SAMPLES:
                problems.append(
                    f"run would sample {samples} steps but the service cap is "
                    f"{MAX_SAMPLES} (MAX_SAMPLES) — raise interval"
                )

    # Species.
    species = request.get("species")
    if not isinstance(species, dict) or not species:
        problems.append("species must be a non-empty object")
        species = {}
    elif len(species) > 64:
        problems.append(f"{len(species)} species exceeds the service cap of 64")
    total_particles = 0
    for name, spec in species.items():
        if not isinstance(name, str) or not name.isidentifier() or len(name) > 64 \
                or not name[0:1].isalpha():
            problems.append(f"species name {name!r} invalid: {_NAME_PATTERN_ERRORS}")
        if not isinstance(spec, dict):
            spec = {"count": spec}
        count = spec.get("count", 0)
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            problems.append(f"species {name!r} count must be a nonnegative integer")
        else:
            total_particles += count
        difc = spec.get("difc", 1.0)
        if not isinstance(difc, (int, float)) or isinstance(difc, bool) \
                or not math.isfinite(difc) or difc < 0:
            problems.append(f"species {name!r} difc must be finite and nonnegative")
        color = spec.get("color", "black")
        if color not in VALID_COLORS:
            problems.append(
                f"species {name!r} color {color!r} not supported (valid: "
                f"{', '.join(VALID_COLORS)})"
            )
        display = spec.get("display_size", 3.0)
        if not isinstance(display, (int, float)) or isinstance(display, bool) \
                or not math.isfinite(display) or display <= 0:
            problems.append(f"species {name!r} display_size must be finite and positive")
    if species and total_particles > MAX_PARTICLES:
        problems.append(
            f"total initial particles ({total_particles}) exceeds the service cap "
            f"of {MAX_PARTICLES} (MAX_PARTICLES)"
        )

    # Reactions: V1 supports unimolecular conversion only.
    reactions = request.get("reactions") or []
    if len(reactions) > 128:
        problems.append(f"{len(reactions)} reactions exceeds the service cap of 128")
    names: set[str] = set()
    for rxn in reactions:
        label = str(rxn.get("name") or "<unnamed>")
        if label in names:
            problems.append(f"reaction name {label!r} is duplicated")
        names.add(label)
        subs, prds = rxn.get("subs") or [], rxn.get("prds") or []
        if len(subs) != 1 or len(prds) != 1:
            problems.append(
                f"reaction {label!r} is not unimolecular — the endpoint supports "
                "one-substrate/one-product conversions only"
            )
        for ref in (*subs, *prds):
            if ref not in species:
                problems.append(f"reaction {label!r} references undefined species {ref!r}")
        rate = rxn.get("rate", 0.0)
        if not isinstance(rate, (int, float)) or isinstance(rate, bool) \
                or not math.isfinite(rate) or rate < 0:
            problems.append(f"reaction {label!r} rate must be finite and nonnegative")
        kb = rxn.get("kb")
        if kb is not None and (not isinstance(kb, (int, float)) or isinstance(kb, bool)
                               or not math.isfinite(kb) or kb < 0):
            problems.append(f"reaction {label!r} kb must be finite and nonnegative")
        if label and (not label.isidentifier() or len(label) > 64 or not label[0:1].isalpha()):
            problems.append(f"reaction name {label!r} invalid: {_NAME_PATTERN_ERRORS}")

    # Geometry and seed.
    dimensions = request.get("dimensions", 2)
    if dimensions not in (2, 3):
        problems.append("dimensions must be 2 or 3")
    bounds = request.get("bounds")
    if bounds is not None:
        if not isinstance(bounds, list) or len(bounds) != dimensions:
            problems.append("bounds cardinality must match dimensions")
        else:
            for pair in bounds:
                if (not isinstance(pair, (list, tuple)) or len(pair) != 2
                        or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                   and math.isfinite(v) for v in pair)
                        or pair[0] >= pair[1]):
                    problems.append(
                        "each bounds pair must be two finite numbers with low < high"
                    )
                    break
    if request.get("boundary_type", "r") not in VALID_BOUNDARY:
        problems.append(
            f"boundary_type {request.get('boundary_type')!r} not supported "
            f"(valid: {', '.join(VALID_BOUNDARY)})"
        )
    seed = request.get("seed", -1)
    if not isinstance(seed, int) or isinstance(seed, bool) or not (-1 <= seed <= 2147483647):
        problems.append("seed must be an integer in [-1, 2147483647]")

    # Body size (the service rejects >MAX_BODY_BYTES before parsing).
    try:
        body_bytes = len(json.dumps(request).encode())
    except (TypeError, ValueError):
        problems.append("request is not JSON-serializable")
    else:
        if body_bytes > MAX_BODY_BYTES:
            problems.append(
                f"request body ({body_bytes} bytes) exceeds the service cap of "
                f"{MAX_BODY_BYTES} (MAX_BODY_BYTES)"
            )

    return problems
