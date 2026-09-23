"""Sensitive-path policy shared by every server-side reader of workspace files.

The workbench serves files out of the workspace tree (the catch-all
``GET /{rel:path}`` route, :func:`vivarium_workbench.lib.static_serving.resolve_asset`)
and optional extensions may read workspace files on a user's behalf. Both must
refuse the same set of paths, so the policy lives here, once.

It is deliberately *path-based and conservative*:

* **Denied directories** (any path segment): repository metadata and credential
  stores such as ``.git``, ``.ssh``, ``.aws``, ``.gnupg`` and any ``secrets``
  directory.
* **Denied workspace-private state** (root-relative prefixes): the per-server
  runtime directory ``.pbg/server/``, the per-developer workstream state
  ``.pbg/state.json`` and the private ``.pbg/assistant/`` area.
* **Denied basenames** (glob, case-insensitive): environment files, private
  keys and certificates, credential JSON, ``.netrc``-style files.

Matching is case-insensitive and Unicode-NFC-normalised on every platform, so a
case-insensitive filesystem (macOS APFS default) cannot be used to sidestep a
rule with ``.ENV`` or ``.Git/config``.

This module is AI-agnostic and has no dependencies beyond the standard library.
"""
from __future__ import annotations

import fnmatch
import re
import unicodedata
from pathlib import Path, PurePosixPath

#: Directory names refused anywhere in a path (single segments).
DENIED_DIR_NAMES: frozenset[str] = frozenset({
    ".git", ".hg", ".svn",
    ".ssh", ".aws", ".azure", ".gnupg", ".kube", ".docker",
    "secrets",
})

#: Multi-segment directory paths refused anywhere in a path.
DENIED_DIR_PATHS: tuple[str, ...] = (".config/gcloud",)

#: Workspace-root-relative directory prefixes holding private workbench state.
DENIED_ROOT_PREFIXES: tuple[str, ...] = (
    ".pbg/server",
    ".pbg/assistant",
)

#: Exact workspace-root-relative files holding private workbench state.
DENIED_ROOT_FILES: frozenset[str] = frozenset({
    ".pbg/state.json",
})

#: Basename globs refused anywhere (matched lower-cased).
DENIED_BASENAME_GLOBS: tuple[str, ...] = (
    ".env", ".env.*", "*.env",
    "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore", "*.jks", "*.kdbx",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
    ".netrc", "_netrc", ".pypirc", ".npmrc", ".git-credentials", ".htpasswd",
    "credentials*.json", "*service-account*.json", "*service_account*.json",
    "*.secret", "*.secrets",
)

_DRIVE_RE = re.compile(r"^[A-Za-z]:")

# Content signatures of credential material (checked on the first bytes of a
# file). A PEM private key header or a Google service-account JSON document.
_PEM_PRIVATE_RE = re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_SA_JSON_RE = re.compile(rb'"type"\s*:\s*"service_account"')
_PRIVATE_KEY_FIELD_RE = re.compile(rb'"private_key"\s*:\s*"-----BEGIN')


class UnsafePath(ValueError):
    """A client-supplied relative path is malformed or escapes its root."""


def normalize_rel(rel: str) -> str:
    """Return a clean POSIX, NFC-normalised, root-relative form of ``rel``.

    Raises :class:`UnsafePath` for anything that is not a plain relative path:
    empty strings, NUL bytes, backslashes, absolute paths, drive letters, and
    ``.``/``..``/empty segments.
    """
    if not isinstance(rel, str) or not rel:
        raise UnsafePath("empty path")
    if "\x00" in rel:
        raise UnsafePath("NUL byte in path")
    if "\\" in rel:
        raise UnsafePath("backslash in path")
    if rel.startswith("/") or _DRIVE_RE.match(rel):
        raise UnsafePath("absolute path")
    rel = unicodedata.normalize("NFC", rel)
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise UnsafePath("traversal or empty segment")
    return "/".join(parts)


def is_sensitive_rel(rel: str) -> bool:
    """True when the root-relative POSIX path ``rel`` is on the denylist.

    ``rel`` should already be normalised (see :func:`normalize_rel`); matching
    is case-insensitive regardless.
    """
    low = unicodedata.normalize("NFC", rel).lower().strip("/")
    if not low:
        return False
    parts = PurePosixPath(low).parts
    # A denied directory name anywhere in the path (including the leaf, e.g. a
    # request for the directory itself).
    if any(part in DENIED_DIR_NAMES for part in parts):
        return True
    wrapped = f"/{low}/"
    if any(f"/{name}/" in wrapped for name in DENIED_DIR_PATHS):
        return True
    for prefix in DENIED_ROOT_PREFIXES:
        if low == prefix or low.startswith(prefix + "/"):
            return True
    if low in DENIED_ROOT_FILES:
        return True
    base = parts[-1]
    return any(fnmatch.fnmatchcase(base, pat) for pat in DENIED_BASENAME_GLOBS)


def looks_like_credential(head: bytes) -> bool:
    """True when ``head`` (the first bytes of a file) contains credential material."""
    if not head:
        return False
    return bool(
        _PEM_PRIVATE_RE.search(head)
        or _SA_JSON_RE.search(head)
        or _PRIVATE_KEY_FIELD_RE.search(head)
    )


def resolve_contained(root: Path, rel: str) -> Path | None:
    """Join ``rel`` onto ``root`` and return the real path, or ``None`` if unsafe.

    Returns ``None`` when ``rel`` is malformed (see :func:`normalize_rel`), when
    it is on the denylist (:func:`is_sensitive_rel`), or when the resolved target
    — after following symlinks — lies outside ``root``. The target need not
    exist; callers decide what a missing file means.
    """
    try:
        clean = normalize_rel(rel)
    except UnsafePath:
        return None
    if is_sensitive_rel(clean):
        return None
    try:
        real_root = Path(root).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    target = (real_root / clean).resolve(strict=False)
    if target != real_root and not target.is_relative_to(real_root):
        return None
    # A symlink inside the root may point at a sensitive path *inside* the
    # root too (e.g. ``notes/link -> ../.env``); re-check the resolved path.
    try:
        rel_real = target.relative_to(real_root).as_posix()
    except ValueError:
        return None
    if rel_real and is_sensitive_rel(rel_real):
        return None
    return target
