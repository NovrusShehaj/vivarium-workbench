"""Google OAuth access tokens for Vertex AI (optional ``google-auth`` dependency).

Credential sources for a Vertex instance:

* ``adc``      — Application Default Credentials (``gcloud auth
  application-default login`` or ``GOOGLE_APPLICATION_CREDENTIALS``);
* ``key_file`` — a service-account key file *path* (local mode only); the
  workbench never reads the key material into its own storage;
* ``session``  — a pasted short-lived access token, kept in server memory.

Access tokens are minted and refreshed with ``google-auth`` (in a worker
thread, since it is synchronous), refreshed five minutes before expiry, and
never written anywhere. Install with ``pip install 'vivarium-workbench[assistant-google]'``.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import os
import threading
from typing import Any

from vivarium_workbench_assistant.providers.base import ProviderError
from vivarium_workbench_assistant.secrets import LOCAL_SCOPE, SecretStore, get_logger, register_known_secret

log = get_logger("vivarium_workbench_assistant.providers.auth_google")

SCOPE = "https://www.googleapis.com/auth/cloud-platform"
REFRESH_MARGIN_S = 300
INSTALL_HINT = ("Vertex AI needs the optional Google auth library: "
                "pip install 'vivarium-workbench[assistant-google]'.")


def _google_modules() -> tuple[Any, Any, Any]:
    try:
        import google.auth  # type: ignore[import-not-found]
        import google.auth.transport.requests  # type: ignore[import-not-found]
        from google.oauth2 import service_account  # type: ignore[import-not-found]
    except ImportError:
        raise ProviderError("config", INSTALL_HINT, retryable=False) from None
    return google.auth, google.auth.transport.requests, service_account


class GoogleTokenProvider:
    """Caches one ``google.auth`` credentials object per (instance, source, path)."""

    def __init__(self, secrets: SecretStore) -> None:
        self._secrets = secrets
        self._lock = threading.Lock()
        self._creds: dict[tuple[str, str, str], Any] = {}

    def forget(self, instance_id: str) -> None:
        with self._lock:
            for key in [k for k in self._creds if k[0] == instance_id]:
                self._creds.pop(key, None)

    async def token(self, inst: Any, *, scope: str = LOCAL_SCOPE) -> str:
        source = inst.credential.source
        if source == "session":
            tok = self._secrets.get(inst, "access_token", scope=scope)
            if not tok:
                raise ProviderError("config", "Paste a Google access token for this session in Settings.",
                                    retryable=False)
            return tok
        if source not in ("adc", "key_file"):
            raise ProviderError("config", "Vertex AI uses Google credentials (ADC, a key file, or a session token).",
                                retryable=False)
        path = ""
        if source == "key_file":
            path = os.path.expanduser(inst.credential.key_file_path or "")
            if not path or not os.path.isfile(path):
                raise ProviderError("config", "The service-account key file was not found.", retryable=False)
        return await asyncio.to_thread(self._token_sync, inst.id, source, path)

    def _token_sync(self, instance_id: str, source: str, path: str) -> str:
        google_auth, transport, service_account = _google_modules()
        key = (instance_id, source, path)
        with self._lock:
            creds = self._creds.get(key)
        try:
            if creds is None:
                if source == "adc":
                    creds, _project = google_auth.default(scopes=[SCOPE])
                else:
                    creds = service_account.Credentials.from_service_account_file(path, scopes=[SCOPE])
                with self._lock:
                    self._creds[key] = creds
            if not _fresh(creds):
                creds.refresh(transport.Request())
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - map google-auth's error types by name
            name = type(exc).__name__
            if name == "DefaultCredentialsError":
                raise ProviderError("config", "No Google Application Default Credentials were found. "
                                    "Run: gcloud auth application-default login", retryable=False) from None
            if name == "RefreshError":
                raise ProviderError("auth", "Google rejected the credentials while refreshing the access "
                                    "token. Re-authenticate and try again.", retryable=False) from None
            if name == "TransportError":
                raise ProviderError("network", "Could not reach Google to refresh the access token.") from None
            raise ProviderError("auth", f"Could not obtain a Google access token ({name}).",
                                retryable=False) from None
        token = getattr(creds, "token", None)
        if not token:
            raise ProviderError("auth", "Google did not return an access token.", retryable=False)
        register_known_secret(token)
        return str(token)


def _fresh(creds: Any) -> bool:
    if not getattr(creds, "token", None):
        return False
    expiry = getattr(creds, "expiry", None)
    if expiry is None:
        return bool(getattr(creds, "valid", False))
    now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None) if expiry.tzinfo is None \
        else _dt.datetime.now(_dt.timezone.utc)
    return (expiry - now).total_seconds() > REFRESH_MARGIN_S
