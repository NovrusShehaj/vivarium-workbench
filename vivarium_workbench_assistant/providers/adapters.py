"""One adapter for every provider: profile (data) + wire codec + auth + HTTP.

``ProviderAdapter`` validates a configured instance, discovers its models and
streams chat completions as provider-neutral ``StreamEvent``s. The outbound
policy is applied when the adapter is built (``destination_for``) and again on
every request (``OutboundHTTP``), so a stored URL can never bypass it.
"""
from __future__ import annotations

import time
from importlib import metadata
from typing import Any, AsyncIterator

from vivarium_workbench_assistant.policy import HostedPolicy, Mode
from vivarium_workbench_assistant.providers import wire_anthropic, wire_openai_chat
from vivarium_workbench_assistant.providers.auth_google import GoogleTokenProvider
from vivarium_workbench_assistant.providers.base import (
    ChatMessage,
    ChatRequest,
    ModelInfo,
    ProviderError,
    StreamEvent,
    TextDelta,
    TextPart,
    ValidationResult,
)
from vivarium_workbench_assistant.providers.http import (
    DEFAULT_READ_TIMEOUT_S,
    OutboundHTTP,
    destination_for,
    iter_sse_events,
    iter_sse_lines,
)
from vivarium_workbench_assistant.providers.profiles import get_profile
from vivarium_workbench_assistant.secrets import LOCAL_SCOPE, SecretStore

MAX_DISCOVERY_PAGES = 10


def _user_agent() -> str:
    try:
        version = metadata.version("vivarium-workbench")
    except metadata.PackageNotFoundError:
        version = "0"
    return f"vivarium-workbench-assistant/{version}"


class ProviderAdapter:
    def __init__(
        self,
        instance: Any,
        *,
        mode: Mode,
        secrets: SecretStore,
        google: GoogleTokenProvider,
        hosted: HostedPolicy | None = None,
        scope: str = LOCAL_SCOPE,
    ) -> None:
        self.instance = instance
        self.profile = get_profile(instance.type)
        self.mode = mode
        self.hosted = hosted
        self.dest = destination_for(instance, mode=mode, hosted=hosted)
        self._secrets = secrets
        self._google = google
        self._scope = scope
        read_timeout = instance.first_token_timeout_s or DEFAULT_READ_TIMEOUT_S
        self.http = OutboundHTTP(self.dest, read_timeout_s=read_timeout)

    # -- credentials ----------------------------------------------------------
    def _api_key(self) -> str | None:
        inst = self.instance
        src = inst.credential.source
        if src == "none":
            return None
        if src == "env" and self.mode != "local":
            allowed = (self.hosted or HostedPolicy()).credential_env.get(inst.type)
            if inst.credential.env_var != allowed:
                raise ProviderError("blocked_by_policy", "This credential source is not allowed here.",
                                    retryable=False)
        return self._secrets.get(inst, "api_key", scope=self._scope)

    async def auth_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"User-Agent": _user_agent()}
        headers.update(self.profile.default_headers)
        auth = self.profile.auth
        if auth == "bearer-google-oauth":
            token = await self._google.token(self.instance, scope=self._scope)
            headers["Authorization"] = f"Bearer {token}"
        else:
            key = self._api_key()
            if auth == "x-api-key":
                if not key:
                    raise ProviderError("config", f"Add an API key for {self.profile.display_name} in Settings.",
                                        retryable=False)
                headers["x-api-key"] = key
            elif auth == "bearer-static":
                if not key:
                    raise ProviderError("config", f"Add an API key for {self.profile.display_name} in Settings.",
                                        retryable=False)
                headers["Authorization"] = f"Bearer {key}"
            elif auth == "optional-bearer" and key:
                headers["Authorization"] = f"Bearer {key}"
        if self.instance.type == "openrouter" and self.instance.send_attribution_headers:
            headers["X-OpenRouter-Title"] = "Vivarium Workbench"
            headers["X-Title"] = "Vivarium Workbench"
        return headers

    # -- discovery ------------------------------------------------------------
    async def list_models(self) -> list[ModelInfo]:
        discovery = self.profile.discovery
        if discovery == "none":
            return []
        headers = await self.auth_headers()
        headers["Accept"] = "application/json"
        if discovery == "openai":
            data = await self.http.request_json("GET", wire_openai_chat.MODELS_PATH, headers=headers)
            return wire_openai_chat.parse_models(data)
        if discovery == "anthropic":
            models: list[ModelInfo] = []
            after: str | None = None
            for _ in range(MAX_DISCOVERY_PAGES):
                path = f"{wire_anthropic.MODELS_PATH}?limit=1000"
                if after:
                    path += "&after_id=" + _q(after)
                page, after = wire_anthropic.parse_models(
                    await self.http.request_json("GET", path, headers=headers))
                models.extend(page)
                if not after:
                    break
            return models
        if discovery == "gemini-native":
            key = headers.pop("Authorization", "").removeprefix("Bearer ").strip()
            headers["x-goog-api-key"] = key
            out: list[ModelInfo] = []
            token: str | None = None
            for _ in range(MAX_DISCOVERY_PAGES):
                path = "/v1beta/models?pageSize=1000"
                if token:
                    path += "&pageToken=" + _q(token)
                page, token = wire_openai_chat.parse_gemini_native_models(
                    await self.http.request_json("GET", path, headers=headers, from_origin=True))
                out.extend(page)
                if not token:
                    break
            return out
        return []

    async def validate(self, *, probe_model: str | None = None) -> ValidationResult:
        """Check the credential and endpoint; discovery or a 1-token request."""
        t0 = time.monotonic()
        try:
            if self.profile.discovery != "none":
                models = await self.list_models()
                notes = [] if models else ["The server listed no models — pull or load one first."]
                return ValidationResult(ok=True, latency_ms=int((time.monotonic() - t0) * 1000),
                                        models_count=len(models), notes=notes)
            await self.auth_headers()          # e.g. mints a Google token
            model = probe_model or self.instance.default_model or (self.instance.manual_models or [None])[0]
            if not model:
                return ValidationResult(ok=True, latency_ms=int((time.monotonic() - t0) * 1000),
                                        notes=["Credentials look good. Add a model id to test a request."])
            req = ChatRequest(model=model, system=None,
                              messages=[ChatMessage("user", [TextPart("Reply with the word OK.")])],
                              max_output_tokens=8)
            async for ev in self.stream_chat(req):
                if isinstance(ev, TextDelta):
                    break
            return ValidationResult(ok=True, latency_ms=int((time.monotonic() - t0) * 1000))
        except ProviderError as exc:
            return ValidationResult(ok=False, latency_ms=int((time.monotonic() - t0) * 1000),
                                    error=exc.to_client())

    # -- chat -----------------------------------------------------------------
    async def stream_chat(self, req: ChatRequest) -> AsyncIterator[StreamEvent]:
        headers = await self.auth_headers()
        headers["Accept"] = "text/event-stream"
        if self.profile.wire == "anthropic-messages":
            path = wire_anthropic.MESSAGES_PATH
            body = wire_anthropic.build_body(req)
            parser = wire_anthropic.parse_stream
        else:
            path = wire_openai_chat.CHAT_PATH
            body = wire_openai_chat.build_body(req, stream_usage=self.profile.stream_usage)
            parser = wire_openai_chat.parse_stream
        async with self.http.stream("POST", path, headers=headers, json_body=body) as resp:
            ctype = resp.headers.get("content-type", "")
            if "text/event-stream" not in ctype:
                raise ProviderError("server", f"{self.profile.display_name} did not return a stream "
                                    f"(content-type {ctype.split(';')[0] or 'missing'}). Is the base URL right?",
                                    retryable=False)
            async for ev in parser(iter_sse_events(iter_sse_lines(resp))):
                yield ev


def _q(value: str) -> str:
    from urllib.parse import quote
    return quote(value, safe="")
