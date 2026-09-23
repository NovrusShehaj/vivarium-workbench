"""Outbound HTTP for provider calls: SSRF policy, DNS pinning, hard limits.

Every provider request goes through :class:`OutboundHTTP`. The policy
(plan §10.2) is enforced on each request, not only when a URL is saved:

* **Schemes** — ``https``; plain ``http`` only to loopback/private addresses of
  an explicitly configured local endpoint (Ollama, LM Studio, a LAN box) in
  local mode, or an operator-allowlisted internal service in hosted mode.
* **No credential-bearing URLs** — userinfo (``user:pass@``), query strings and
  fragments are refused in base URLs; secrets only ever travel in headers.
* **Host pinning** — cloud profiles talk to their official hosts unless the
  instance sets an explicit base URL (local mode) or the operator allowlists
  one (hosted mode).
* **Address checks after DNS** — link-local and cloud-metadata ranges
  (``169.254.0.0/16``, ``fe80::/10``, ``fd00:ec2::254``, ``100.100.100.200``),
  unspecified, multicast and reserved addresses are always refused; loopback
  and private ranges only for a configured local endpoint. The connection is
  then **pinned to the checked IP** (TLS still verifies the hostname through
  SNI), so a second DNS answer cannot redirect it (DNS rebinding).
* **No redirects** — a 3xx is an error, never followed (credentials could leak
  to, or the host policy be bypassed by, the redirect target).
* **Limits** — connect/read timeouts, a total-duration cap enforced by the
  caller, a maximum JSON body and a maximum SSE line length.
* **Hygiene** — TLS verification always on (``certifi`` roots plus an optional
  per-instance CA bundle); proxies and ``.netrc`` from the environment are not
  used (``trust_env=False``); nothing here logs URLs, headers or bodies.
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
import ssl
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, AsyncIterator, Mapping

import httpx

from vivarium_workbench_assistant.policy import HostedPolicy, Mode
from vivarium_workbench_assistant.providers.base import ProviderError
from vivarium_workbench_assistant.providers.profiles import (
    ProviderProfile,
    get_profile,
    host_is_official,
    vertex_base_url,
)
from vivarium_workbench_assistant.secrets import get_logger, redact

log = get_logger("vivarium_workbench_assistant.providers.http")

MAX_SSE_LINE = 1_048_576          # bytes in one SSE line
MAX_JSON_BODY = 8 * 1_048_576     # bytes in one non-streamed response
MAX_ERROR_BODY = 65_536           # bytes read from an error response
CONNECT_TIMEOUT_S = 10.0
DEFAULT_READ_TIMEOUT_S = 120.0

_ALWAYS_BLOCKED = tuple(ipaddress.ip_network(n) for n in (
    "169.254.0.0/16",        # IPv4 link-local, incl. 169.254.169.254 metadata
    "fe80::/10",             # IPv6 link-local
    "fd00:ec2::254/128",     # AWS IMDS over IPv6
    "100.100.100.200/32",    # Alibaba Cloud metadata
    "0.0.0.0/8",             # "this network"
    "::/128",                # unspecified
    "224.0.0.0/4", "ff00::/8",   # multicast
    "240.0.0.0/4",           # reserved (incl. 255.255.255.255)
))
_PRIVATE_EXTRA = tuple(ipaddress.ip_network(n) for n in ("100.64.0.0/10", "fc00::/7"))


def ip_is_always_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return any(ip in net for net in _ALWAYS_BLOCKED if net.version == ip.version)


def ip_is_private(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_loopback or ip.is_private:
        return True
    return any(ip in net for net in _PRIVATE_EXTRA if net.version == ip.version)


def _blocked(message: str) -> ProviderError:
    return ProviderError("blocked_by_policy", message, retryable=False)


@dataclass(frozen=True)
class Destination:
    """Where an instance may connect, as decided by the static policy."""

    profile: ProviderProfile
    base_url: str
    scheme: str
    host: str
    port: int
    #: Loopback/private addresses permitted (a configured local endpoint).
    allow_private: bool
    #: Plain http permitted (still only to loopback/private addresses).
    allow_http: bool
    ca_bundle_path: str | None = None


def _normalize_base(url: str) -> str:
    return url.strip().rstrip("/")


def destination_for(
    instance: Any,
    *,
    mode: Mode,
    hosted: HostedPolicy | None = None,
) -> Destination:
    """Apply the static outbound policy to a provider instance's endpoint.

    Raises :class:`ProviderError` (``blocked_by_policy`` / ``config``).
    """
    profile = get_profile(instance.type)
    custom = bool(instance.base_url)
    if instance.type == "vertex" and not custom:
        try:
            base = vertex_base_url(instance.project or "", instance.location or "")
        except ValueError as exc:
            raise ProviderError("config", str(exc), retryable=False) from None
    else:
        base = instance.base_url or profile.base_url or ""
    if not base:
        raise ProviderError("config", f"{profile.display_name} needs a base URL.", retryable=False)
    if len(base) > 2048:
        raise _blocked("The base URL is too long.")
    try:
        url = httpx.URL(_normalize_base(base))
    except Exception:  # noqa: BLE001 - httpx raises several types for bad URLs
        raise _blocked("The base URL is not a valid URL.") from None
    scheme = url.scheme.lower()
    if scheme not in ("http", "https"):
        raise _blocked("Only https (or http to a local endpoint) is allowed.")
    if url.userinfo:
        raise _blocked("URLs with embedded credentials are not allowed; use the credential field.")
    if url.query or url.fragment:
        raise _blocked("Base URLs may not contain a query string or fragment.")
    host = (url.host or "").lower().rstrip(".")
    if not host:
        raise _blocked("The base URL has no host.")
    port = url.port or (443 if scheme == "https" else 80)

    allow_private = False
    allow_http = False
    if mode == "local":
        if profile.locality == "local" or custom:
            allow_private = True
            allow_http = True
        elif not host_is_official(profile, host):
            raise _blocked(f"{profile.display_name} requests must go to its official host.")
    else:
        hp = hosted or HostedPolicy()
        if profile.type not in hp.providers_allowed:
            raise _blocked(f"{profile.display_name} is not enabled on this deployment.")
        if custom or profile.locality == "local":
            if _normalize_base(base) not in hp.base_url_allowlist:
                raise _blocked("This endpoint is not on the operator's allowlist.")
            allow_private = True
            allow_http = scheme == "http"
        elif not host_is_official(profile, host):
            raise _blocked(f"{profile.display_name} requests must go to its official host.")
    if scheme == "http" and not allow_http:
        raise _blocked("Plain http is only allowed for local endpoints.")
    ca = getattr(instance, "ca_bundle_path", None) or None
    if ca and mode != "local":
        raise _blocked("Custom CA bundles are only allowed in local mode.")
    return Destination(profile=profile, base_url=_normalize_base(base), scheme=scheme, host=host,
                       port=port, allow_private=allow_private, allow_http=allow_http,
                       ca_bundle_path=ca)


async def _resolve(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ProviderError("network", f"DNS lookup failed for {host} ({exc.strerror or 'no address'}).") from None
    out: list[str] = []
    for info in infos:
        addr = info[4][0]
        if isinstance(addr, str) and addr not in out:
            out.append(addr.split("%", 1)[0])
    if not out:
        raise ProviderError("network", f"DNS lookup failed for {host} (no address).")
    return out


async def check_addresses(dest: Destination, url: httpx.URL) -> list[str]:
    """Resolve ``url``'s host and apply the address policy to EVERY answer.

    Returns the addresses in resolver order (all of them passed the policy);
    the caller connects to them in turn, pinned, never re-resolving.
    """
    host = (url.host or "").lower().rstrip(".")
    if host != dest.host or (url.port or (443 if url.scheme == "https" else 80)) != dest.port \
            or url.scheme != dest.scheme:
        raise _blocked("Request left the configured endpoint.")
    try:
        literal = ipaddress.ip_address(host)
        addrs = [str(literal)]
    except ValueError:
        addrs = await _resolve(host, dest.port)
    for a in addrs:
        ip = ipaddress.ip_address(a)
        if ip_is_always_blocked(ip):
            raise _blocked(f"{host} resolves to a link-local, metadata or reserved address; refused.")
        private = ip_is_private(ip)
        if private and not dest.allow_private:
            raise _blocked(f"{host} resolves to a private or loopback address; refused for a cloud provider.")
        if dest.scheme == "http" and not private:
            raise _blocked("Plain http is only allowed to local or private addresses.")
    return addrs


async def check_and_pin(dest: Destination, url: httpx.URL) -> str:
    """The first policy-checked address for ``url`` (see :func:`check_addresses`)."""
    return (await check_addresses(dest, url))[0]


def _ssl_context(ca_bundle_path: str | None) -> ssl.SSLContext:
    import certifi
    ctx = ssl.create_default_context(cafile=certifi.where())
    if ca_bundle_path:
        path = os.path.expanduser(ca_bundle_path)
        if not os.path.isfile(path):
            raise ProviderError("config", "The CA bundle file does not exist.", retryable=False)
        ctx.load_verify_locations(cafile=path)
    return ctx


def parse_retry_after(headers: Mapping[str, str]) -> float | None:
    ms = headers.get("retry-after-ms")
    if ms:
        try:
            return max(0.0, float(ms) / 1000.0)
        except ValueError:
            pass
    ra = headers.get("retry-after")
    if not ra:
        return None
    try:
        return max(0.0, float(ra))
    except ValueError:
        try:
            dt = parsedate_to_datetime(ra)
            return max(0.0, dt.timestamp() - time.time())
        except (TypeError, ValueError):
            return None


_CONTEXT_HINTS = ("context length", "context window", "too long", "maximum context", "too many tokens",
                  "prompt is too long", "exceeds the maximum", "max_tokens", "input length")


def _extract_message(body: str) -> str:
    import json
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return body.strip()[:300]
    if isinstance(data, list) and data:
        data = data[0]
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("type") or ""
            return str(msg)
        if isinstance(err, str):
            return err
        if "message" in data:
            return str(data["message"])
    return ""


def map_http_error(status: int, body: str, headers: Mapping[str, str], provider: str) -> ProviderError:
    """Map an HTTP error to a redacted :class:`ProviderError` (never the raw body)."""
    detail = redact(_extract_message(body or ""))[:300].strip()
    suffix = f" ({detail})" if detail else ""
    retry_after = parse_retry_after(headers)
    if status == 401:
        return ProviderError("auth", f"{provider} rejected the credential (401). Update it in Settings → AI Assistant.",
                             status=status)
    if status == 403:
        return ProviderError("permission", f"The credential lacks access to this model or endpoint (403){suffix}.",
                             status=status)
    if status == 404:
        return ProviderError("not_found", f"{provider} could not find that model or endpoint (404){suffix}.",
                             status=status)
    if status == 408:
        return ProviderError("timeout", f"{provider} timed out (408).", status=status)
    if status == 413:
        return ProviderError("context_length", f"The request is too large for {provider} (413).", status=status)
    if status == 429:
        wait = f" Retry in {int(retry_after)} s." if retry_after else ""
        return ProviderError("rate_limited", f"{provider} rate-limited the request (429).{wait}",
                             status=status, retry_after_s=retry_after)
    if status == 529:
        return ProviderError("overloaded", f"{provider} is overloaded; try again shortly (529).",
                             status=status, retry_after_s=retry_after)
    if status >= 500:
        return ProviderError("server", f"{provider} returned a server error ({status}){suffix}.",
                             status=status, retry_after_s=retry_after)
    low = detail.lower()
    if status in (400, 422) and any(h in low for h in _CONTEXT_HINTS):
        return ProviderError("context_length", f"Too much context for this model{suffix}.", status=status)
    if 300 <= status < 400:
        return _blocked(f"{provider} answered with a redirect ({status}); redirects are not followed.")
    return ProviderError("invalid_request", f"{provider} rejected the request ({status}){suffix}.", status=status)


def _causes(exc: BaseException) -> list[BaseException]:
    out: list[BaseException] = []
    cur: BaseException | None = exc
    while cur is not None and cur not in out and len(out) < 10:
        out.append(cur)
        cur = cur.__cause__ or cur.__context__
    return out


def _transport_error(exc: Exception, host: str, provider: str) -> ProviderError:
    if isinstance(exc, httpx.TimeoutException):
        return ProviderError("timeout", f"{provider} did not respond in time ({host}).")
    chain = _causes(exc)
    if any(isinstance(c, ssl.SSLError) for c in chain):
        detail = redact(str(next(c for c in chain if isinstance(c, ssl.SSLError))))[:200]
        return ProviderError("network", f"TLS error talking to {host}: {detail}", retryable=False)
    if any(isinstance(c, ConnectionRefusedError) for c in chain):
        return ProviderError("network", f"Couldn't connect to {host} (connection refused). Is the server running?")
    if any(isinstance(c, socket.gaierror) for c in chain):
        return ProviderError("network", f"DNS lookup failed for {host}.")
    text = redact(str(exc) or type(exc).__name__)
    low = text.lower()
    if "certificate" in low or "ssl" in low or "tls" in low:
        return ProviderError("network", f"TLS error talking to {host}: {text[:200]}", retryable=False)
    if "refused" in low or "errno 61" in low or "errno 111" in low:
        return ProviderError("network", f"Couldn't connect to {host} (connection refused). Is the server running?")
    return ProviderError("network", f"Couldn't reach {host}: {text[:200]}")


class OutboundHTTP:
    """A policy-checked, pinned, redirect-free HTTP client for one destination."""

    def __init__(self, dest: Destination, *, read_timeout_s: float | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.dest = dest
        self.read_timeout_s = read_timeout_s or DEFAULT_READ_TIMEOUT_S
        self._transport = transport

    def url(self, path: str, *, from_origin: bool = False) -> httpx.URL:
        """``dest.base_url`` + ``path`` (a relative path; never a full URL).

        ``from_origin=True`` resolves ``path`` against the endpoint's origin
        (scheme://host:port) instead — still the same, policy-checked host.
        """
        if "://" in path or path.startswith("//"):
            raise _blocked("Absolute URLs are not accepted here.")
        if path and not path.startswith(("/", "?")):
            path = "/" + path
        if from_origin:
            base_url = httpx.URL(self.dest.base_url)
            origin = f"{base_url.scheme}://{base_url.netloc.decode('ascii')}"
            return httpx.URL(origin + path)
        return httpx.URL(self.dest.base_url + path)

    def _client(self) -> httpx.AsyncClient:
        timeout = httpx.Timeout(connect=CONNECT_TIMEOUT_S, read=self.read_timeout_s, write=30.0, pool=10.0)
        kwargs: dict[str, Any] = dict(
            timeout=timeout, follow_redirects=False, trust_env=False,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=2),
        )
        if self._transport is not None:
            kwargs["transport"] = self._transport
        else:
            kwargs["verify"] = _ssl_context(self.dest.ca_bundle_path)
        return httpx.AsyncClient(**kwargs)

    def _pinned_request(self, client: httpx.AsyncClient, method: str, url: httpx.URL, ip: str,
                        headers: Mapping[str, str], json_body: Any) -> httpx.Request:
        """A request to ``ip`` that still names (Host header) and verifies (SNI) the host."""
        host = url.host
        default_port = 443 if url.scheme == "https" else 80
        host_header = host if (url.port in (None, default_port)) else f"{host}:{url.port}"
        out_headers = dict(headers)
        out_headers["Host"] = host_header
        extensions: dict[str, Any] = {}
        if url.scheme == "https":
            extensions["sni_hostname"] = host
        return client.build_request(method, url.copy_with(host=ip), headers=out_headers, json=json_body,
                                    extensions=extensions)

    async def _send(self, client: httpx.AsyncClient, method: str, url: httpx.URL,
                    headers: Mapping[str, str], json_body: Any) -> httpx.Response:
        """Send to each policy-checked address in turn (only connect errors fall through)."""
        provider = self.dest.profile.display_name
        last: Exception | None = None
        for ip in await check_addresses(self.dest, url):
            req = self._pinned_request(client, method, url, ip, headers, json_body)
            try:
                return await client.send(req, stream=True)
            except httpx.ConnectError as exc:
                last = exc
                continue
            except (httpx.TransportError, ssl.SSLError, OSError) as exc:
                raise _transport_error(exc, self.dest.host, provider) from None
        assert last is not None
        raise _transport_error(last, self.dest.host, provider) from None

    @asynccontextmanager
    async def stream(self, method: str, path: str, *, headers: Mapping[str, str],
                     json_body: Any = None) -> AsyncIterator[httpx.Response]:
        """Open a streamed request; a non-2xx status is raised as ProviderError."""
        url = self.url(path)
        provider = self.dest.profile.display_name
        async with self._client() as client:
            resp = await self._send(client, method, url, headers, json_body)
            try:
                if resp.status_code >= 300:
                    body = await _read_capped(resp, MAX_ERROR_BODY, tolerate_overflow=True)
                    raise map_http_error(resp.status_code, body.decode("utf-8", "replace"),
                                         resp.headers, provider)
                yield resp
            finally:
                await resp.aclose()

    async def request_json(self, method: str, path: str, *, headers: Mapping[str, str],
                           json_body: Any = None, from_origin: bool = False) -> Any:
        """A non-streamed JSON request with a response-size cap."""
        import json
        url = self.url(path, from_origin=from_origin)
        provider = self.dest.profile.display_name
        async with self._client() as client:
            resp = await self._send(client, method, url, headers, json_body)
            try:
                if resp.status_code >= 300:
                    body = await _read_capped(resp, MAX_ERROR_BODY, tolerate_overflow=True)
                    raise map_http_error(resp.status_code, body.decode("utf-8", "replace"),
                                         resp.headers, provider)
                raw = await _read_capped(resp, MAX_JSON_BODY)
            except httpx.TransportError as exc:
                raise _transport_error(exc, self.dest.host, provider) from None
            finally:
                await resp.aclose()
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ProviderError("server", f"{provider} returned a response that is not JSON.") from None


async def _read_capped(resp: httpx.Response, limit: int, *, tolerate_overflow: bool = False) -> bytes:
    buf = bytearray()
    async for chunk in resp.aiter_bytes():
        buf.extend(chunk)
        if len(buf) > limit:
            if tolerate_overflow:
                return bytes(buf[:limit])
            raise ProviderError("server", "The provider response exceeded the size limit.")
    return bytes(buf)


async def iter_sse_lines(resp: httpx.Response) -> AsyncIterator[str]:
    """Decoded lines of an SSE body (``\\r\\n``/``\\n``), with a line-length cap."""
    buf = bytearray()
    try:
        async for chunk in resp.aiter_bytes():
            buf.extend(chunk)
            while True:
                nl = buf.find(b"\n")
                if nl == -1:
                    break
                line = bytes(buf[:nl])
                del buf[: nl + 1]
                if line.endswith(b"\r"):
                    line = line[:-1]
                yield line.decode("utf-8", "replace")
            if len(buf) > MAX_SSE_LINE:
                raise ProviderError("server", "The provider sent an oversized stream event.")
    except httpx.TransportError as exc:
        if isinstance(exc, httpx.TimeoutException):
            raise ProviderError("timeout", "The provider stopped sending data (read timeout).") from None
        raise ProviderError("network", "The connection to the provider was interrupted.") from None
    if buf:
        tail = bytes(buf)
        yield (tail[:-1] if tail.endswith(b"\r") else tail).decode("utf-8", "replace")


async def iter_sse_events(lines: AsyncIterator[str]) -> AsyncIterator[tuple[str, str]]:
    """``(event, data)`` pairs from SSE lines; comments and ids are ignored."""
    event = ""
    data: list[str] = []
    async for line in lines:
        if line == "":
            if data:
                yield event or "message", "\n".join(data)
            event, data = "", []
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if name == "event":
            event = value
        elif name == "data":
            data.append(value)
    if data:
        yield event or "message", "\n".join(data)
