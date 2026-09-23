"""Credential storage and secret redaction.

Where a provider credential may live (plan §10.1):

* ``keyring``  — the OS keychain (service ``vivarium-workbench-assistant``,
  username ``<instance-id>:<field>``). The local-mode default.
* ``env``      — an environment variable *named* by the instance; only the name
  is stored, the value is read at use time.
* ``session``  — server memory only, lost on restart. In hosted mode (when an
  operator allows user credentials at all) it is bound to the browser session.
* ``adc`` / ``key_file`` — Google credentials resolved by ``auth_google``; the
  workbench stores at most a *path*.

Never: the browser, cookies, URLs, ``workspace.yaml``, the workspace tree, the
assistant's config file, logs or conversation transcripts. No endpoint returns a
stored secret — status calls report ``{configured, source, hint}`` only.

Redaction removes exact known secret values first (longest first — the most
reliable method), then well-known credential shapes. It is applied to every
log record of the extension's logger tree, to provider error messages, audit
records and tool results.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:
    from vivarium_workbench_assistant.config import ProviderInstance

KEYRING_SERVICE = "vivarium-workbench-assistant"

#: Scope used for session-memory secrets on a single-user (local) server.
LOCAL_SCOPE = "local"

#: Environment variables an instance may never read, even in local mode: the
#: workbench's own credentials and common cloud root secrets are not model keys.
DENIED_ENV_VARS: frozenset[str] = frozenset({
    "VIVARIUM_WORKBENCH_GH_TOKEN", "VIVARIUM_DASHBOARD_GH_TOKEN", "GH_TOKEN", "GITHUB_TOKEN",
    "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_ACCESS_KEY_ID",
    "GOOGLE_APPLICATION_CREDENTIALS", "SSH_AUTH_SOCK",
})
ENV_VAR_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")

# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

_known_lock = threading.Lock()
_known: set[str] = set()
_MIN_KNOWN_LEN = 8

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|$)"),
     "[redacted private key]"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{8,}"), "[redacted]"),
    (re.compile(r"\bsk-or-[A-Za-z0-9_\-]{8,}"), "[redacted]"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"), "[redacted]"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"), "[redacted]"),
    (re.compile(r"\bya29\.[0-9A-Za-z_\-\.]{10,}"), "[redacted]"),
    (re.compile(r"\b(?:gh[opusr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"), "[redacted]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[redacted]"),
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9_\-\.=:+/~]{8,}"), r"\1[redacted]"),
    (re.compile(r"(?i)((?:x-api-key|x-goog-api-key|api[_-]?key|access[_-]?token|authorization|"
                r"client[_-]?secret|refresh[_-]?token|password)[\"']?\s*[:=]\s*[\"']?)"
                r"(?!\[redacted)[^\s\"',;&}]{6,}"),
     r"\1[redacted]"),
    (re.compile(r"(?i)([?&](?:key|api_key|token|access_token)=)[^&\s#\"']+"), r"\1[redacted]"),
)


def register_known_secret(value: str | None) -> None:
    """Remember a loaded credential so every later redaction removes it exactly."""
    if value and len(value) >= _MIN_KNOWN_LEN:
        with _known_lock:
            _known.add(value)


def forget_known_secret(value: str | None) -> None:
    if value:
        with _known_lock:
            _known.discard(value)


def redact(text: Any, extra: Iterable[str] = ()) -> str:
    """Return ``text`` with known secret values and credential shapes removed."""
    s = text if isinstance(text, str) else str(text)
    if not s:
        return s
    with _known_lock:
        values = sorted(set(_known) | {v for v in extra if v and len(v) >= _MIN_KNOWN_LEN},
                        key=len, reverse=True)
    for v in values:
        if v in s:
            s = s.replace(v, "[redacted]")
    for pattern, repl in _PATTERNS:
        s = pattern.sub(repl, s)
    return s


class StreamRedactor:
    """Removes known secret values from streamed text, even when split across chunks.

    Only exact known values are removed here (credential-shaped *patterns* are
    left alone in model prose, which may legitimately explain what a key looks
    like). To catch a value split across chunks, the longest trailing fragment
    that could still become a known secret is held back until more text — or
    the end of the stream — decides it.
    """

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, chunk: str) -> str:
        buf = self._buf + chunk
        with _known_lock:
            values = sorted(_known, key=len, reverse=True)
        for v in values:
            if v in buf:
                buf = buf.replace(v, "[redacted]")
        hold = 0
        for v in values:
            for k in range(min(len(v) - 1, len(buf)), hold, -1):
                if buf.endswith(v[:k]):
                    hold = k
                    break
        self._buf = buf[len(buf) - hold:] if hold else ""
        return buf[: len(buf) - hold] if hold else buf

    def flush(self) -> str:
        out, self._buf = self._buf, ""
        with _known_lock:
            values = sorted(_known, key=len, reverse=True)
        for v in values:
            out = out.replace(v, "[redacted]")
        return out


def secret_findings(text: str) -> list[str]:
    """Names of credential shapes found in ``text`` (for "this looks like a key" warnings)."""
    if not text:
        return []
    names = []
    checks = (
        ("private key", _PATTERNS[0][0]),
        ("Anthropic-style key", _PATTERNS[1][0]),
        ("OpenRouter-style key", _PATTERNS[2][0]),
        ("OpenAI-style key", _PATTERNS[3][0]),
        ("Google API key", _PATTERNS[4][0]),
        ("Google OAuth token", _PATTERNS[5][0]),
        ("GitHub token", _PATTERNS[6][0]),
        ("AWS access key id", _PATTERNS[7][0]),
    )
    for name, pat in checks:
        if pat.search(text):
            names.append(name)
    with _known_lock:
        if any(v in text for v in _known):
            names.append("a configured credential")
    return names


def redact_obj(obj: Any, extra: Iterable[str] = ()) -> Any:
    """Recursively redact every string inside a JSON-like structure."""
    extra = tuple(extra)
    if isinstance(obj, str):
        return redact(obj, extra)
    if isinstance(obj, dict):
        return {str(k): redact_obj(v, extra) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v, extra) for v in obj]
    return obj


class RedactingFilter(logging.Filter):
    """Redact secrets from every record passing through a handler/logger."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a malformed record must not escape unredacted
            message = str(record.msg)
        record.msg = redact(message)
        record.args = ()
        if record.exc_info:
            import traceback
            record.exc_text = redact("".join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        return True


def get_logger(name: str) -> logging.Logger:
    """A logger whose records are redacted before they propagate anywhere.

    (A filter on a *parent* logger is not consulted for records created by its
    children, so every module of the extension takes its logger from here.)
    """
    lg = logging.getLogger(name)
    if not any(isinstance(f, RedactingFilter) for f in lg.filters):
        lg.addFilter(RedactingFilter())
    return lg


_filter_installed = False


def install_log_redaction() -> None:
    """Redact the extension's loggers; silence HTTP-library request logging."""
    global _filter_installed
    if _filter_installed:
        return
    get_logger("vivarium_workbench_assistant")
    # httpx/httpcore log request lines (with URLs) at INFO/DEBUG; never let them
    # through, and never log bodies or headers. Child loggers inherit the level.
    for name in ("httpx", "httpcore", "hpack", "urllib3", "google.auth", "keyring"):
        lg = get_logger(name)
        if lg.level == logging.NOTSET or lg.level < logging.WARNING:
            lg.setLevel(logging.WARNING)
    _filter_installed = True


log = get_logger("vivarium_workbench_assistant.secrets")


# ---------------------------------------------------------------------------
# Secret store
# ---------------------------------------------------------------------------

class SecretStoreError(Exception):
    """A credential could not be stored or read (message is safe to show)."""


@dataclass(frozen=True)
class CredentialStatus:
    configured: bool
    source: str
    hint: str | None = None
    notice: str | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"configured": self.configured, "source": self.source}
        if self.hint:
            out["hint"] = self.hint
        if self.notice:
            out["notice"] = self.notice
        return out


def _keyring_module() -> Any | None:
    try:
        import keyring  # type: ignore[import-not-found]
    except ImportError:
        return None
    return keyring


def keyring_available() -> bool:
    kr = _keyring_module()
    if kr is None:
        return False
    try:
        backend = kr.get_keyring()
    except Exception:  # noqa: BLE001
        return False
    name = type(backend).__name__.lower()
    # keyring's "fail" / "null" backends mean no usable secret service.
    if "fail" in name or "null" in name:
        return False
    priority = getattr(backend, "priority", 1)
    try:
        return float(priority) > 0
    except (TypeError, ValueError):
        return True


def masked_hint(value: str | None) -> str | None:
    """At most the last four characters, and only for long values."""
    if not value or len(value) < 16:
        return None
    return "••••" + value[-4:]


def validate_env_var_name(name: str | None) -> str:
    n = (name or "").strip()
    if not ENV_VAR_RE.match(n):
        raise SecretStoreError("Environment variable names use A-Z, 0-9 and _, starting with a letter.")
    if n in DENIED_ENV_VARS or n.startswith(("VIVARIUM_WORKBENCH_", "VIVARIUM_DASHBOARD_", "AWS_")):
        raise SecretStoreError(f"{n} is not allowed as a model credential.")
    return n


class SecretStore:
    """Resolves and stores provider credentials. Thread-safe."""

    FIELDS = ("api_key", "access_token")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._session: dict[tuple[str, str, str], str] = {}

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _username(instance_id: str, field: str) -> str:
        return f"{instance_id}:{field}"

    def _keyring_get(self, instance_id: str, field: str) -> str | None:
        kr = _keyring_module()
        if kr is None:
            return None
        try:
            value = kr.get_password(KEYRING_SERVICE, self._username(instance_id, field))
        except Exception as exc:  # noqa: BLE001 - backend errors must not leak or crash
            log.warning("keychain read failed for %s: %s", instance_id, type(exc).__name__)
            return None
        return value or None

    # -- read ---------------------------------------------------------------
    def get(self, inst: "ProviderInstance", field: str = "api_key", *, scope: str = LOCAL_SCOPE) -> str | None:
        """The credential value for ``inst`` (``None`` when not configured)."""
        source = inst.credential.source
        value: str | None = None
        if source == "keyring":
            value = self._keyring_get(inst.id, field)
        elif source == "env":
            name = inst.credential.env_var
            if name:
                try:
                    value = os.environ.get(validate_env_var_name(name)) or None
                except SecretStoreError:
                    value = None
        elif source == "session":
            with self._lock:
                value = self._session.get((scope, inst.id, field))
        if value:
            register_known_secret(value)
        return value

    def status(self, inst: "ProviderInstance", *, scope: str = LOCAL_SCOPE,
               show_hint: bool = True) -> CredentialStatus:
        source = inst.credential.source
        if source == "none":
            return CredentialStatus(configured=True, source="none")
        if source == "adc":
            return CredentialStatus(configured=True, source="adc",
                                    notice="Google Application Default Credentials")
        if source == "key_file":
            path = inst.credential.key_file_path
            ok = bool(path) and os.path.isfile(os.path.expanduser(path or ""))
            return CredentialStatus(configured=ok, source="key_file",
                                    notice=None if ok else "Key file not found")
        field = "access_token" if inst.type == "vertex" else "api_key"
        value = self.get(inst, field, scope=scope)
        if source == "env":
            return CredentialStatus(configured=bool(value), source="env",
                                    notice=f"Environment variable {inst.credential.env_var or '(unset)'}")
        notice = {
            "keyring": "OS keychain",
            "session": "This server session only (lost on restart)",
        }.get(source)
        return CredentialStatus(configured=bool(value), source=source,
                                hint=masked_hint(value) if show_hint else None, notice=notice)

    # -- write --------------------------------------------------------------
    def set(self, inst: "ProviderInstance", value: str, *, field: str = "api_key",
            persist: bool, scope: str = LOCAL_SCOPE) -> str:
        """Store ``value``; returns the source actually used (``keyring``/``session``)."""
        value = (value or "").strip()
        if not value:
            raise SecretStoreError("The credential is empty.")
        if len(value) > 8192 or any(c in value for c in "\r\n\x00"):
            raise SecretStoreError("That does not look like a credential.")
        if field not in self.FIELDS:
            raise SecretStoreError("Unknown credential field.")
        register_known_secret(value)
        if persist:
            kr = _keyring_module()
            if kr is not None and keyring_available():
                try:
                    kr.set_password(KEYRING_SERVICE, self._username(inst.id, field), value)
                    with self._lock:
                        self._session.pop((scope, inst.id, field), None)
                    return "keyring"
                except Exception as exc:  # noqa: BLE001
                    log.warning("keychain write failed for %s: %s", inst.id, type(exc).__name__)
            # No usable keychain: fall back to memory, and say so.
        with self._lock:
            self._session[(scope, inst.id, field)] = value
        return "session"

    def delete(self, inst_id: str, *, scope: str | None = None) -> None:
        """Remove every stored credential for ``inst_id`` (keychain + memory)."""
        kr = _keyring_module()
        if kr is not None:
            for field in self.FIELDS:
                try:
                    if kr.get_password(KEYRING_SERVICE, self._username(inst_id, field)):
                        kr.delete_password(KEYRING_SERVICE, self._username(inst_id, field))
                except Exception:  # noqa: BLE001 - missing entry / backend quirk
                    pass
        with self._lock:
            for key in [k for k in self._session if k[1] == inst_id and (scope is None or k[0] == scope)]:
                # The value stays in the redaction set for the process lifetime:
                # it may still appear in in-flight output.
                self._session.pop(key)

    def clear_scope(self, scope: str) -> None:
        with self._lock:
            for key in [k for k in self._session if k[0] == scope]:
                self._session.pop(key)
