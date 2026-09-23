"""Model registry: discovery cache, manual models, metadata overrides, allowlists.

Sources, in precedence order (plan §6.4):

1. the operator allowlist (hosted deployments) — a filter;
2. discovery per instance — cached in memory and on disk for 24 h;
3. models the user added manually to the instance;
4. ``<config>/assistant/models.overrides.json`` — context windows and
   capabilities for providers whose discovery lacks them::

       {"*": {"llama3.1:8b": {"context_window": 131072, "caps": ["tools"]}},
        "ollama": {"qwen2.5-coder:7b": {"context_window": 32768}}}

No model identifiers are built in: a fresh install needs discovery or manual
entry. Unknown capabilities (``caps=None``) allow chat; tools need an override
or a model that reports them.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any, Callable

from vivarium_workbench.lib import user_dirs
from vivarium_workbench_assistant.providers.base import Cap, ModelInfo
from vivarium_workbench_assistant.secrets import get_logger

log = get_logger("vivarium_workbench_assistant.models_registry")

CACHE_TTL_S = 24 * 3600
DEFAULT_CONTEXT_WINDOW = 16_384


def _caps_from(raw: Any) -> frozenset[Cap] | None:
    if not isinstance(raw, list):
        return None
    out = set()
    for c in raw:
        try:
            out.add(Cap(str(c)))
        except ValueError:
            continue
    return frozenset(out | {Cap.STREAMING, Cap.SYSTEM_PROMPT})


def _cache_key(instance: Any) -> str:
    """``<instance id>:<hash of the endpoint>`` — a changed endpoint misses the cache."""
    ident = f"{instance.type}|{instance.base_url or ''}|{instance.project or ''}|{instance.location or ''}"
    return f"{instance.id}:" + hashlib.sha256(ident.encode("utf-8")).hexdigest()[:16]


class ModelRegistry:
    def __init__(self, *, cache_path: Callable[[], Path], overrides_path: Callable[[], Path],
                 persist_cache: Callable[[], bool] = lambda: True) -> None:
        self._cache_path = cache_path
        self._overrides_path = overrides_path
        self._persist_cache = persist_cache
        self._lock = threading.Lock()
        self._mem: dict[str, tuple[float, list[ModelInfo]]] = {}
        self._disk_loaded = False

    # -- cache ----------------------------------------------------------------------
    def _load_disk(self) -> None:
        if self._disk_loaded:
            return
        self._disk_loaded = True
        path = self._cache_path()
        if not path.is_file():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return
        if not isinstance(raw, dict):
            return
        for key, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            ts = entry.get("ts")
            models = []
            for m in entry.get("models") or []:
                if isinstance(m, dict) and isinstance(m.get("id"), str):
                    models.append(ModelInfo(
                        id=m["id"], display_name=m.get("display_name"),
                        context_window=m.get("context_window"), max_output_tokens=m.get("max_output_tokens"),
                        caps=_caps_from(m.get("caps")), source="discovered"))
            if isinstance(ts, (int, float)):
                self._mem[key] = (float(ts), models)

    def _save_disk(self) -> None:
        if not self._persist_cache():
            return
        data = {k: {"ts": ts, "models": [m.to_json() for m in models]} for k, (ts, models) in self._mem.items()}
        try:
            user_dirs.write_private_text(self._cache_path(), json.dumps(data) + "\n")
        except OSError as exc:
            log.warning("model cache write failed: %s", type(exc).__name__)

    def cached(self, instance: Any) -> tuple[list[ModelInfo], float | None]:
        with self._lock:
            self._load_disk()
            hit = self._mem.get(_cache_key(instance))
        if not hit:
            return [], None
        return list(hit[1]), hit[0]

    def store(self, instance: Any, models: list[ModelInfo]) -> None:
        with self._lock:
            self._load_disk()
            self._mem[_cache_key(instance)] = (time.time(), list(models))
            self._save_disk()

    def invalidate(self, instance_id: str) -> None:
        with self._lock:
            self._load_disk()
            for key in [k for k in self._mem if k.startswith(f"{instance_id}:")]:
                self._mem.pop(key, None)
            self._save_disk()

    def fresh(self, instance: Any) -> bool:
        _models, ts = self.cached(instance)
        return ts is not None and (time.time() - ts) < CACHE_TTL_S

    # -- overrides ------------------------------------------------------------------
    def overrides(self) -> dict[str, dict[str, dict[str, Any]]]:
        path = self._overrides_path()
        if not path.is_file():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            log.warning("ignoring unreadable %s", path.name)
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(k): v for k, v in raw.items() if isinstance(v, dict)}

    def _apply_override(self, instance: Any, m: ModelInfo, ov: dict[str, dict[str, dict[str, Any]]]) -> ModelInfo:
        entry = {**ov.get("*", {}).get(m.id, {}), **ov.get(instance.id, {}).get(m.id, {})}
        ctx = m.context_window
        if isinstance(entry.get("context_window"), int):
            ctx = entry["context_window"]
        elif ctx is None and instance.context_window_override:
            ctx = instance.context_window_override
        max_out = entry["max_output_tokens"] if isinstance(entry.get("max_output_tokens"), int) else m.max_output_tokens
        caps = _caps_from(entry.get("caps")) if "caps" in entry else m.caps
        return ModelInfo(id=m.id, display_name=m.display_name, context_window=ctx,
                         max_output_tokens=max_out, caps=caps, source=m.source)

    # -- merged view ------------------------------------------------------------------
    def merged(self, instance: Any, discovered: list[ModelInfo],
               allowlist: tuple[str, ...] | None = None) -> list[ModelInfo]:
        ov = self.overrides()
        out: dict[str, ModelInfo] = {}
        for m in discovered:
            out[m.id] = m
        for mid in instance.manual_models:
            if mid not in out:
                out[mid] = ModelInfo(id=mid, source="manual")
        if instance.default_model and instance.default_model not in out:
            out[instance.default_model] = ModelInfo(id=instance.default_model, source="configured")
        models = [self._apply_override(instance, m, ov) for m in out.values()]
        if allowlist:
            models = [m for m in models if m.id in allowlist]
        models.sort(key=lambda m: (m.source != "configured", (m.display_name or m.id).lower()))
        return models

    def resolve(self, instance: Any, model_id: str) -> ModelInfo:
        cached, _ts = self.cached(instance)
        for m in self.merged(instance, cached):
            if m.id == model_id:
                return m
        return self._apply_override(instance, ModelInfo(id=model_id, source="manual"), self.overrides())
