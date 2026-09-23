"""HTTP routes for the assistant, mounted by the core seam at ``/api/ext/assistant``.

Security properties enforced here (on top of the core's Host guard and CSRF
middleware):

* **Browser-only mutations** — every state-changing route and every run needs
  an ``Origin`` header that passes the same-origin check
  (``lib.csrf.is_browser_request_allowed``); Origin-less requests (curl, a
  DNS-rebound page without Origin, a stray tool) are refused.
* **No CORS headers** are ever added; every response is ``no-store``.
* **Availability** — every route except ``/status`` refuses with 403 when the
  assistant is unavailable (hosted without operator opt-in, or disabled).
* **Scope** — on a hosted deployment everything is keyed by the browser
  session; runs, approvals, proposals and conversations of another session
  answer 404.
* **Secrets** are write-only: credential routes accept a key and answer
  ``{configured, source, hint?}``; no route returns a stored secret.
"""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from vivarium_workbench.lib import csrf
from vivarium_workbench_assistant import policy, sandbox
from vivarium_workbench_assistant.api_models import (
    ApprovalDecision,
    ContextPreviewRequest,
    ConversationCreate,
    ConversationPatch,
    CredentialSet,
    DeleteAllRequest,
    InstanceCreate,
    InstancePatch,
    ModelCapabilitiesBody,
    PreferencesPatch,
    ProposalAction,
    RunRequest,
    SuggestBody,
)
from vivarium_workbench_assistant.chat_service import ChatService, RunParams, RunRefused
from vivarium_workbench_assistant.config import AssistantConfig, CredentialRef, ProviderInstance
from vivarium_workbench_assistant.context import builder as ctx_builder
from vivarium_workbench_assistant.conversations import CID_RE, ConversationNotFound
from vivarium_workbench_assistant.edits import apply as edits_apply
from vivarium_workbench_assistant.edits.proposals import PID_RE
from vivarium_workbench_assistant.models_registry import DEFAULT_CONTEXT_WINDOW
from vivarium_workbench_assistant.providers.base import ProviderError
from vivarium_workbench_assistant.providers.profiles import PRESETS, PROFILES
from vivarium_workbench_assistant.sandbox import SandboxError
from vivarium_workbench_assistant.secrets import (
    SecretStoreError,
    get_logger,
    keyring_available,
    validate_env_var_name,
)
from vivarium_workbench_assistant.services import AssistantServices, public_instance
from vivarium_workbench_assistant.streaming import RunLimitError, consume

log = get_logger("vivarium_workbench_assistant.routes")

IID_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
RID_RE = re.compile(r"^r_[0-9a-f]{16}$")
AID_RE = re.compile(r"^a_[0-9a-f]{16}$")
_NO_STORE = {"Cache-Control": "no-store"}


def _json(data: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status, headers=_NO_STORE)


def _error(message: str, status: int, **extra: Any) -> JSONResponse:
    return _json({"error": message, **extra}, status)


def _browser_ok(request: Request) -> bool:
    return csrf.is_browser_request_allowed(
        request.headers.get("origin"), request.headers.get("host"),
        disabled=csrf.is_disabled_via_env(os.environ),
        forwarded_host=request.headers.get("x-forwarded-host"),
        trust_forwarded=csrf.is_trust_proxy_via_env(os.environ),
        allowed_origins=csrf.allowed_origins_via_env(os.environ),
    )


def register(router: APIRouter, ctx: Any, services: AssistantServices, chat: ChatService) -> None:
    get_workspace: Callable[..., Path] = ctx.get_workspace

    def scope_of(request: Request) -> str | None:
        key = ctx.session_key_of(request)
        if services.mode() != "local" and not key:
            return None
        return services.scope_for(key)

    def guard(request: Request, *, mutate: bool) -> tuple[JSONResponse | None, str]:
        ok, reason = policy.availability()
        if not ok:
            return _error(reason, 403, code="assistant_unavailable"), ""
        if mutate and not _browser_ok(request):
            return _error("This action is only available from the workbench page (same-origin browser "
                          "request with an Origin header).", 403, code="origin_required"), ""
        scope = scope_of(request)
        if scope is None:
            return _error("A browser session is required.", 400, code="session_required"), ""
        return None, scope

    def local_only() -> JSONResponse | None:
        if not services.capabilities().config_editable:
            return _error("Provider configuration is managed by the operator on this deployment.", 403,
                          code="operator_managed")
        return None

    def instance_or_404(iid: str) -> ProviderInstance | JSONResponse:
        if not IID_RE.match(iid):
            return _error("unknown provider", 404)
        inst = services.instance(iid)
        if inst is None:
            return _error("unknown provider", 404)
        return inst

    # ------------------------------------------------------------------ status
    @router.get("/status")
    def status(request: Request) -> JSONResponse:
        ok, reason = policy.availability()
        scope = scope_of(request) or "anonymous"
        caps = services.capabilities()
        cfg = services.load_config() if ok else AssistantConfig()
        prefs = services.preferences(scope) if ok else None
        try:
            import google.auth  # type: ignore[import-not-found]  # noqa: F401
            google_ok = True
        except ImportError:
            google_ok = False
        storage: dict[str, str] = {}
        if services.mode() == "local":
            from vivarium_workbench_assistant.conversations import assistant_data_dir
            storage = {"config": str(services.config.path), "data": str(assistant_data_dir())}
        return _json({
            "enabled": True, "available": ok, "reason": reason, "mode": services.mode(),
            "capabilities": caps.to_json(), "providers_configured": len(cfg.instances),
            "default_instance": prefs.default_instance if prefs else None,
            "persist_conversations": services.persist(scope) if ok else False,
            "keyring_available": keyring_available() if services.mode() == "local" else False,
            "google_auth_installed": google_ok,
            "config_read_only": services.config.read_only_reason if services.mode() == "local" else None,
            "storage": storage,
        })

    @router.get("/provider-types")
    def provider_types(request: Request) -> JSONResponse:
        err, _scope = guard(request, mutate=False)
        if err:
            return err
        allowed = set(services.capabilities().provider_types)
        return _json({
            "types": [p.to_json() for t, p in PROFILES.items() if t in allowed],
            "presets": [{"id": pr.id, "display_name": pr.display_name, "base_url": pr.base_url,
                         "notes": pr.notes} for pr in PRESETS.values()],
        })

    # --------------------------------------------------------------- providers
    @router.get("/providers")
    def list_providers(request: Request) -> JSONResponse:
        err, scope = guard(request, mutate=False)
        if err:
            return err
        cfg = services.load_config()
        return _json({"providers": [public_instance(i, services, scope) for i in cfg.instances],
                      "default_instance": services.preferences(scope).default_instance})

    def _unique_id(cfg: AssistantConfig, base: str) -> str:
        base = re.sub(r"[^a-z0-9-]+", "-", base.lower()).strip("-")[:30] or "provider"
        if not base[0].isalpha():
            base = "p-" + base
        cand, n = base, 2
        while cfg.instance(cand) is not None:
            cand = f"{base}-{n}"
            n += 1
        return cand

    def _validate_credential_ref(ref: dict[str, Any] | None) -> None:
        if ref and ref.get("source") == "env":
            validate_env_var_name(ref.get("env_var"))

    @router.post("/providers")
    def create_provider(body: InstanceCreate, request: Request) -> JSONResponse:
        err, _scope = guard(request, mutate=True)
        if err or (err := local_only()):
            return err
        data = body.model_dump(exclude_none=True)
        profile = PROFILES[body.type]
        if "credential" not in data:
            data["credential"] = {"source": profile.credential_sources[0]}
        try:
            _validate_credential_ref(data.get("credential"))
        except SecretStoreError as exc:
            return _error(str(exc), 400)
        if body.type == "openai_compatible" and not data.get("preset") and not data.get("base_url"):
            data["preset"] = "custom"
        data.setdefault("display_name", profile.display_name if body.type != "openai_compatible"
                        else PRESETS.get(data.get("preset") or "custom", PRESETS["custom"]).display_name)

        def upd(cfg: AssistantConfig) -> AssistantConfig:
            data["id"] = data.get("id") if data.get("id") and cfg.instance(data["id"]) is None \
                else _unique_id(cfg, data.get("id") or data.get("preset") or body.type.replace("_", "-"))
            inst = ProviderInstance.model_validate(data)
            cfg.instances.append(inst)
            if cfg.preferences.default_instance is None:
                cfg.preferences.default_instance = inst.id
            return cfg
        try:
            cfg = services.config.update(upd)
        except (ValueError, PermissionError) as exc:
            return _error(_clean_validation(exc), 400)
        inst = cfg.instances[-1]
        return _json({"provider": public_instance(inst, services, "local")}, 201)

    @router.patch("/providers/{iid}")
    def patch_provider(iid: str, body: InstancePatch, request: Request) -> JSONResponse:
        err, _scope = guard(request, mutate=True)
        if err or (err := local_only()):
            return err
        found = instance_or_404(iid)
        if isinstance(found, JSONResponse):
            return found
        changes = body.model_dump(exclude_unset=True)
        make_default = changes.pop("make_default", None)
        try:
            _validate_credential_ref(changes.get("credential"))
        except SecretStoreError as exc:
            return _error(str(exc), 400)

        def upd(cfg: AssistantConfig) -> AssistantConfig:
            for i, inst in enumerate(cfg.instances):
                if inst.id == iid:
                    merged = inst.model_dump(mode="json")
                    merged.update({k: v for k, v in changes.items()})
                    cfg.instances[i] = ProviderInstance.model_validate(merged)
            if make_default:
                cfg.preferences.default_instance = iid
            return cfg
        try:
            cfg = services.config.update(upd)
        except (ValueError, PermissionError) as exc:
            return _error(_clean_validation(exc), 400)
        services.models.invalidate(iid)
        services.google.forget(iid)
        inst = cfg.instance(iid)
        assert inst is not None
        return _json({"provider": public_instance(inst, services, "local"),
                      "default_instance": cfg.preferences.default_instance})

    @router.delete("/providers/{iid}")
    def delete_provider(iid: str, request: Request) -> JSONResponse:
        err, _scope = guard(request, mutate=True)
        if err or (err := local_only()):
            return err
        found = instance_or_404(iid)
        if isinstance(found, JSONResponse):
            return found

        def upd(cfg: AssistantConfig) -> AssistantConfig:
            cfg.instances = [i for i in cfg.instances if i.id != iid]
            if cfg.preferences.default_instance == iid:
                cfg.preferences.default_instance = cfg.instances[0].id if cfg.instances else None
            return cfg
        services.config.update(upd)
        services.secrets.delete(iid)          # removes the keychain entry too
        services.models.invalidate(iid)
        services.google.forget(iid)
        services.audit.record("credential_removed", session_key=ctx.session_key_of(request), provider_instance=iid)
        return _json({"deleted": iid})

    @router.post("/providers/{iid}/credential")
    async def set_credential(iid: str, request: Request) -> JSONResponse:
        """Store a credential. Parsed by hand so no validation error can echo it."""
        err, scope = guard(request, mutate=True)
        if err:
            return err
        found = instance_or_404(iid)
        if isinstance(found, JSONResponse):
            return found
        try:
            raw = await request.json()
            body = CredentialSet.model_validate(raw)
        except Exception:  # noqa: BLE001 - never echo the submitted value back
            return _error("Send a JSON object: {\"api_key\": \"…\"} (or access_token), optionally "
                          "\"session_only\": true.", 400)
        finally:
            raw = None
        inst = found
        caps = services.capabilities()
        local = services.mode() == "local"
        if not local and (not caps.user_credentials or inst.credential.source != "session"):
            return _error("Credentials are managed by the operator on this deployment.", 403, code="operator_managed")
        field = "access_token" if inst.type == "vertex" else "api_key"
        secret = body.access_token if field == "access_token" else body.api_key
        if secret is None:
            return _error(f"Send {field}.", 400)
        session_only = body.session_only or inst.type == "vertex"     # access tokens are short-lived
        try:
            used = services.secrets.set(inst, secret.get_secret_value(), field=field,
                                        persist=local and not session_only, scope=scope)
        except SecretStoreError as exc:
            return _error(str(exc), 400)
        if local and inst.credential.source != used:
            def upd(cfg: AssistantConfig) -> AssistantConfig:
                for i, cur in enumerate(cfg.instances):
                    if cur.id == iid:
                        merged = cur.model_dump(mode="json")
                        merged["credential"] = {"source": used}
                        cfg.instances[i] = ProviderInstance.model_validate(merged)
                return cfg
            services.config.update(upd)
            inst = services.instance(iid) or inst
        services.models.invalidate(iid)
        services.audit.record("credential_set", session_key=ctx.session_key_of(request), provider_instance=iid,
                              source=used)
        st = services.secrets.status(inst, scope=scope, show_hint=local).to_json()
        if local and not session_only and used == "session":
            st["notice"] = ("No usable OS keychain was found, so the key is kept in this server session only "
                            "(lost on restart).")
        return _json(st)

    @router.delete("/providers/{iid}/credential")
    def delete_credential(iid: str, request: Request) -> JSONResponse:
        err, scope = guard(request, mutate=True)
        if err:
            return err
        found = instance_or_404(iid)
        if isinstance(found, JSONResponse):
            return found
        local = services.mode() == "local"
        services.secrets.delete(iid, scope=None if local else scope)
        services.google.forget(iid)
        services.audit.record("credential_removed", session_key=ctx.session_key_of(request), provider_instance=iid)
        return _json(services.secrets.status(found, scope=scope, show_hint=local).to_json())

    @router.post("/providers/{iid}/test")
    async def test_provider(iid: str, request: Request) -> JSONResponse:
        err, scope = guard(request, mutate=True)
        if err:
            return err
        found = instance_or_404(iid)
        if isinstance(found, JSONResponse):
            return found
        try:
            adapter = services.adapter(found, scope)
        except ProviderError as exc:
            return _json({"ok": False, "error": exc.to_client()})
        result = await adapter.validate()
        if result.ok and adapter.profile.discovery != "none":
            try:
                services.models.store(found, await adapter.list_models())
            except ProviderError:
                pass
        return _json(result.to_json())

    @router.get("/providers/{iid}/models")
    async def list_models(iid: str, request: Request, refresh: int = 0) -> JSONResponse:
        err, scope = guard(request, mutate=False)
        if err:
            return err
        found = instance_or_404(iid)
        if isinstance(found, JSONResponse):
            return found
        error = None
        discovered, ts = services.models.cached(found)
        if refresh or not services.models.fresh(found):
            try:
                adapter = services.adapter(found, scope)
                if adapter.profile.discovery != "none":
                    discovered = await adapter.list_models()
                    services.models.store(found, discovered)
                    ts = None
            except ProviderError as exc:
                error = exc.to_client()
        models = services.models.merged(found, discovered, services.model_allowlist(found))
        return _json({"models": [m.to_json() for m in models], "error": error,
                      "default_model": found.default_model, "cached_at": ts})

    @router.post("/providers/{iid}/models/capabilities")
    def set_model_caps(iid: str, body: ModelCapabilitiesBody, request: Request) -> JSONResponse:
        err, _scope = guard(request, mutate=True)
        if err or (err := local_only()):
            return err
        found = instance_or_404(iid)
        if isinstance(found, JSONResponse):
            return found
        import json as _json_mod

        from vivarium_workbench.lib import user_dirs
        path = user_dirs.user_config_dir() / "assistant" / "models.overrides.json"
        data = services.models.overrides()
        entry = dict(data.get(iid, {}).get(body.model, {}))
        if body.tools is not None:
            caps = set(entry.get("caps") or [])
            caps.discard("tools")
            if body.tools:
                caps.add("tools")
            entry["caps"] = sorted(caps)
        if body.context_window is not None:
            entry["context_window"] = body.context_window
        if body.max_output_tokens is not None:
            entry["max_output_tokens"] = body.max_output_tokens
        data.setdefault(iid, {})[body.model] = entry
        user_dirs.write_private_text(path, _json_mod.dumps(data, indent=2, sort_keys=True) + "\n")
        return _json({"model": services.models.resolve(found, body.model).to_json()})

    # ------------------------------------------------------------- preferences
    @router.get("/preferences")
    def get_prefs(request: Request) -> JSONResponse:
        err, scope = guard(request, mutate=False)
        if err:
            return err
        return _json(services.preferences(scope).model_dump(mode="json"))

    @router.patch("/preferences")
    def patch_prefs(body: PreferencesPatch, request: Request) -> JSONResponse:
        err, scope = guard(request, mutate=True)
        if err:
            return err
        changes = body.model_dump(exclude_unset=True)
        if changes.get("retention_days") == 0:
            changes["retention_days"] = None
        if "default_instance" in changes and changes["default_instance"] and \
                services.instance(changes["default_instance"]) is None:
            return _error("unknown provider", 400)
        caps = services.capabilities()
        tools = changes.get("tools")
        if tools and tools.get("shell") not in (None, "disabled") and not caps.tools_shell:
            return _error("The shell tool is not available on this deployment.", 403)
        if tools and tools.get("shell") == "auto":
            return _error("The shell tool can never run without approval.", 400)
        merged = services.preferences(scope).model_copy(update=changes)
        try:
            prefs = services.set_preferences(scope, type(merged).model_validate(merged.model_dump()))
        except (ValueError, PermissionError) as exc:
            return _error(_clean_validation(exc), 400)
        return _json(prefs.model_dump(mode="json"))

    # ----------------------------------------------------------- conversations
    def _conv_args(scope: str) -> dict[str, Any]:
        return {"scope": scope, "persist": services.persist(scope)}

    @router.get("/conversations")
    def list_conversations(request: Request, ws: Path = Depends(get_workspace)) -> JSONResponse:
        err, scope = guard(request, mutate=False)
        if err:
            return err
        prefs = services.preferences(scope)
        convs = services.conversations.list(ws, retention_days=prefs.retention_days, **_conv_args(scope))
        return _json({"conversations": convs, "workspace": Path(ws).name})

    @router.post("/conversations")
    def create_conversation(body: ConversationCreate, request: Request, ws: Path = Depends(get_workspace)) -> JSONResponse:
        err, scope = guard(request, mutate=True)
        if err:
            return err
        return _json(services.conversations.create(ws, title=body.title, **_conv_args(scope)), 201)

    @router.get("/conversations/{cid}")
    def get_conversation(cid: str, request: Request, ws: Path = Depends(get_workspace)) -> JSONResponse:
        err, scope = guard(request, mutate=False)
        if err:
            return err
        if not CID_RE.match(cid):
            return _error("conversation not found", 404)
        try:
            return _json(services.conversations.get(ws, cid, **_conv_args(scope)))
        except ConversationNotFound:
            return _error("conversation not found", 404)

    @router.patch("/conversations/{cid}")
    def rename_conversation(cid: str, body: ConversationPatch, request: Request,
                            ws: Path = Depends(get_workspace)) -> JSONResponse:
        err, scope = guard(request, mutate=True)
        if err:
            return err
        try:
            conv = services.conversations.rename(ws, cid, body.title, **_conv_args(scope))
        except ConversationNotFound:
            return _error("conversation not found", 404)
        return _json({k: v for k, v in conv.items() if k != "messages"})

    @router.delete("/conversations/{cid}")
    def delete_conversation(cid: str, request: Request, ws: Path = Depends(get_workspace)) -> JSONResponse:
        err, scope = guard(request, mutate=True)
        if err:
            return err
        try:
            services.conversations.delete(ws, cid, **_conv_args(scope))
        except ConversationNotFound:
            return _error("conversation not found", 404)
        services.grants.pop((scope, cid), None)
        return _json({"deleted": cid})

    @router.post("/conversations/delete-all")
    def delete_all(body: DeleteAllRequest, request: Request, ws: Path = Depends(get_workspace)) -> JSONResponse:
        err, scope = guard(request, mutate=True)
        if err:
            return err
        if body.confirm != Path(ws).name:
            return _error(f"Type the workspace name ({Path(ws).name}) to confirm.", 400)
        n = services.conversations.delete_all(ws, **_conv_args(scope))
        return _json({"deleted": n})

    # ----------------------------------------------------------------- context
    @router.post("/context/preview")
    async def context_preview(body: ContextPreviewRequest, request: Request,
                              ws: Path = Depends(get_workspace)) -> JSONResponse:
        err, scope = guard(request, mutate=True)
        if err:
            return err
        window = DEFAULT_CONTEXT_WINDOW
        locality = provider = None
        inst = services.instance(body.provider_instance) if body.provider_instance else None
        if inst is not None:
            provider = inst.display_name
            locality = PROFILES[inst.type].locality
            if body.model:
                info = chat.model_info(inst, body.model)
                window = info.context_window or inst.context_window_override or window
        built = await asyncio.to_thread(ctx_builder.build, ws, [c.spec() for c in body.context],
                                        mode=services.mode(), budget_tokens=int(window * 0.6),
                                        instance_id=inst.id if inst else None)
        return _json({"items": built.manifest(), "total_tokens": built.total_tokens,
                      "budget_tokens": built.budget_tokens, "warnings": built.warnings,
                      "locality": locality, "provider": provider, "model": body.model,
                      "message_secret_findings": _findings(body.message)})

    # -------------------------------------------------------------------- runs
    @router.post("/conversations/{cid}/runs")
    async def start_run(cid: str, body: RunRequest, request: Request,
                        ws: Path = Depends(get_workspace)) -> Any:
        err, scope = guard(request, mutate=True)
        if err:
            return err
        if not CID_RE.match(cid):
            return _error("conversation not found", 404)
        session_key = ctx.session_key_of(request)          # captured eagerly, before streaming
        ws_root = Path(ws)                                   # captured eagerly (ContextVar is reset later)
        try:
            run = services.runs.start(scope=scope, conversation_id=cid)
        except RunLimitError as exc:
            return _error(str(exc), exc.status)
        params = RunParams(action=body.action, conversation_id=cid, instance_id=body.provider_instance,
                           model=body.model, context=[c.spec() for c in body.context] if body.context is not None
                           else None, message=body.message, parent_id=body.parent_id, agent=body.agent,
                           ws_root=ws_root, scope=scope, session_key=session_key)
        try:
            prep = await asyncio.to_thread(chat.prepare, params)
        except RunRefused as exc:
            services.runs.discard(run)
            return _error(str(exc), exc.status)
        except ProviderError as exc:
            services.runs.discard(run)
            return _json({"error": exc.safe_message, "provider_error": exc.to_client()}, 400)
        except Exception as exc:  # noqa: BLE001
            services.runs.discard(run)
            log.warning("run preparation failed: %s", type(exc).__name__)
            return _error("The assistant could not prepare this request.", 500)
        run.task = asyncio.create_task(chat.execute(run, prep), name=f"assistant-run-{run.id}")

        def _done(t: "asyncio.Task[None]") -> None:
            if not t.cancelled() and t.exception() is not None:
                log.warning("assistant run task ended with %s", type(t.exception()).__name__)
            services.runs.finish(run)
        run.task.add_done_callback(_done)
        return StreamingResponse(consume(run), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    def _run_for(rid: str, scope: str) -> Any:
        if not RID_RE.match(rid):
            return None
        run = services.runs.get(rid)
        if run is None or run.scope != scope:
            return None
        return run

    @router.post("/runs/{rid}/cancel")
    def cancel_run(rid: str, request: Request) -> JSONResponse:
        err, scope = guard(request, mutate=True)
        if err:
            return err
        run = _run_for(rid, scope)
        if run is None:
            return _error("run not found", 404)
        return _json({"cancelled": run.cancel("user"), "status": run.status})

    @router.post("/runs/{rid}/approvals/{aid}")
    def decide_approval(rid: str, aid: str, body: ApprovalDecision, request: Request) -> JSONResponse:
        err, scope = guard(request, mutate=True)
        if err:
            return err
        run = _run_for(rid, scope)
        if run is None or not AID_RE.match(aid):
            return _error("approval not found", 404)
        approval = run.approvals.get(aid)
        if approval is None:
            return _error("This approval has expired or was already decided.", 404)
        if approval.decision is not None:
            return _error("already decided", 409)
        if body.args_hash != approval.args_hash:
            return _error("The tool arguments do not match the pending request.", 409)
        approval.scope = body.scope if approval.category != "shell" else "once"
        approval.decision = body.decision
        approval.event.set()
        return _json({"decision": body.decision, "scope": approval.scope})

    # ------------------------------------------------------------------- files
    @router.get("/files")
    async def read_file(request: Request, path: str = "", ws: Path = Depends(get_workspace)) -> JSONResponse:
        err, scope = guard(request, mutate=False)
        if err:
            return err
        try:
            fr = await asyncio.to_thread(sandbox.read_text_file, ws, path)
        except SandboxError as exc:
            return _error(str(exc), 404)
        ignored = fr.path in await asyncio.to_thread(sandbox.gitignored, ws, [fr.path])
        if ignored and services.mode() != "local":
            return _error("gitignored files are not shown on shared deployments", 404)
        return _json({"path": fr.path, "text": fr.text, "size": fr.size, "sha256": fr.sha256,
                      "truncated": fr.truncated, "gitignored": ignored})

    @router.get("/files/list")
    async def list_files(request: Request, path: str = "", ws: Path = Depends(get_workspace)) -> JSONResponse:
        err, _scope = guard(request, mutate=False)
        if err:
            return err
        try:
            entries = await asyncio.to_thread(sandbox.list_dir, ws, path)
        except SandboxError as exc:
            return _error(str(exc), 404)
        return _json({"path": path, "entries": entries})

    @router.get("/search")
    async def search(request: Request, q: str = "", glob: str | None = None,
                     ws: Path = Depends(get_workspace)) -> JSONResponse:
        err, _scope = guard(request, mutate=False)
        if err:
            return err
        if not q.strip() or len(q) > 200:
            return _error("query must be 1-200 characters", 400)
        if glob is not None and (len(glob) > 100 or ".." in glob or glob.startswith("/")):
            return _error("invalid glob", 400)
        from vivarium_workbench_assistant.context.sources import search_workspace
        hits = await asyncio.to_thread(search_workspace, ws, q, glob=glob, max_results=100)
        return _json({"results": hits})

    # --------------------------------------------------------------- proposals
    def _proposal(ws: Path, pid: str, scope: str) -> Any:
        if not PID_RE.match(pid):
            return None
        try:
            return services.proposals.get(ws, pid, scope=scope, persist=services.persist(scope))
        except KeyError:
            return None

    @router.get("/proposals/{pid}")
    def get_proposal(pid: str, request: Request, ws: Path = Depends(get_workspace)) -> JSONResponse:
        err, scope = guard(request, mutate=False)
        if err:
            return err
        prop = _proposal(ws, pid, scope)
        if prop is None:
            return _error("proposal not found", 404)
        return _json(prop.public())

    @router.post("/proposals/{pid}/apply")
    async def apply_proposal(pid: str, body: ProposalAction, request: Request,
                             ws: Path = Depends(get_workspace)) -> JSONResponse:
        err, scope = guard(request, mutate=True)
        if err:
            return err
        if not services.capabilities().apply_edits:
            return _error("Applying edits is disabled on this deployment.", 403)
        prop = _proposal(ws, pid, scope)
        if prop is None:
            return _error("proposal not found", 404)
        session_key = ctx.session_key_of(request)
        res = await asyncio.to_thread(edits_apply.apply_files, ws, prop, body.paths)
        commit = None
        if body.commit:
            commit = await asyncio.to_thread(edits_apply.commit_applied, ws, prop, res["results"],
                                             conversation_id=prop.conversation_id, model_label=prop.model_label)
        services.proposals.save(ws, prop, scope=scope, persist=services.persist(scope))
        for r in res["results"]:
            if r.get("status") == "applied":
                services.audit.record("edit_applied", session_key=session_key, conversation=prop.conversation_id,
                                      run=prop.run_id, proposal=prop.id,
                                      paths=[{"path": r["path"], "before": r.get("before"), "after": r.get("after")}],
                                      commit_sha=(commit or {}).get("commit_sha"))
        if commit and commit.get("committed"):
            services.audit.record("commit", session_key=session_key, conversation=prop.conversation_id,
                                  proposal=prop.id, commit_sha=commit.get("commit_sha"), paths=commit.get("paths"))
        return _json({"proposal": prop.public(), "results": res["results"], "commit": commit})

    @router.post("/proposals/{pid}/reject")
    def reject_proposal(pid: str, body: ProposalAction, request: Request,
                        ws: Path = Depends(get_workspace)) -> JSONResponse:
        err, scope = guard(request, mutate=True)
        if err:
            return err
        prop = _proposal(ws, pid, scope)
        if prop is None:
            return _error("proposal not found", 404)
        for f in prop.files:
            if f.status == "pending" and (body.paths is None or f.path in body.paths):
                f.status = "rejected"
        prop.refresh_status()
        services.proposals.save(ws, prop, scope=scope, persist=services.persist(scope))
        return _json({"proposal": prop.public()})

    @router.post("/proposals/{pid}/undo")
    async def undo_proposal(pid: str, body: ProposalAction, request: Request,
                            ws: Path = Depends(get_workspace)) -> JSONResponse:
        err, scope = guard(request, mutate=True)
        if err:
            return err
        prop = _proposal(ws, pid, scope)
        if prop is None:
            return _error("proposal not found", 404)
        res = await asyncio.to_thread(edits_apply.undo_files, ws, prop, body.paths)
        services.proposals.save(ws, prop, scope=scope, persist=services.persist(scope))
        services.audit.record("edit_undone", session_key=ctx.session_key_of(request),
                              conversation=prop.conversation_id, proposal=prop.id,
                              paths=[r["path"] for r in res["results"] if r.get("status") == "undone"])
        return _json({"proposal": prop.public(), **res})

    @router.post("/proposals/{pid}/revert")
    async def revert_proposal(pid: str, request: Request, ws: Path = Depends(get_workspace)) -> JSONResponse:
        err, scope = guard(request, mutate=True)
        if err:
            return err
        prop = _proposal(ws, pid, scope)
        if prop is None:
            return _error("proposal not found", 404)
        res = await asyncio.to_thread(edits_apply.revert_commit, ws, prop)
        services.proposals.save(ws, prop, scope=scope, persist=services.persist(scope))
        if res.get("reverted"):
            services.audit.record("edit_undone", session_key=ctx.session_key_of(request),
                                  conversation=prop.conversation_id, proposal=prop.id,
                                  commit_sha=res.get("revert_sha"))
        return _json({"proposal": prop.public(), **res})

    # ----------------------------------------------------------------- suggest
    @router.post("/suggest")
    async def suggest(body: SuggestBody, request: Request, ws: Path = Depends(get_workspace)) -> JSONResponse:
        err, scope = guard(request, mutate=True)
        if err:
            return err
        try:
            out = await chat.suggest(kind=body.kind, ws_root=Path(ws), instance_id=body.provider_instance,
                                     model=body.model, scope=scope)
        except RunRefused as exc:
            return _error(str(exc), exc.status)
        except ProviderError as exc:
            return _json({"error": exc.safe_message, "provider_error": exc.to_client()}, 502)
        except TimeoutError:
            return _error("The provider did not answer in time.", 504)
        return _json(out)

    # ------------------------------------------------------------------- audit
    @router.get("/audit")
    def audit_tail(request: Request, limit: int = 100) -> JSONResponse:
        err, _scope = guard(request, mutate=False)
        if err or (err := local_only()):
            return err
        return _json({"events": services.audit.tail(max(1, min(limit, 500)))})


def _findings(text: str | None) -> list[str]:
    from vivarium_workbench_assistant.secrets import secret_findings
    return secret_findings(text or "")


def _clean_validation(exc: Exception) -> str:
    from vivarium_workbench_assistant.secrets import redact
    text = redact(str(exc))
    if "validation error" in text:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("For further")]
        return "; ".join(lines[1:4])[:300] or "invalid settings"
    return text[:300]


__all__ = ["register", "CredentialRef"]
