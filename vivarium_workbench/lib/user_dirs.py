"""Per-user configuration and data directories.

Personal state — preferences that belong to a person rather than a workspace,
and private data such as an extension's history — must not live in the served
workspace: ``workspace.yaml`` is committed and shared, and the catch-all route
serves files from the workspace tree. It lives here instead:

* config: ``$VIVARIUM_WORKBENCH_CONFIG_DIR`` → ``$XDG_CONFIG_HOME/vivarium-workbench``
  → ``~/.config/vivarium-workbench``
* data:   ``$VIVARIUM_WORKBENCH_DATA_DIR``   → ``$XDG_DATA_HOME/vivarium-workbench``
  → ``~/.local/share/vivarium-workbench``

The same XDG-style layout is used on every platform (no extra dependency). The
legacy ``~/.config/vivarium-dashboard/`` GitHub-login hint directory
(``lib.github_auth``) is intentionally left where it is.

Helpers create directories ``0700`` and files ``0600`` on POSIX.

AI-agnostic; standard library only.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from vivarium_workbench.lib.env_compat import get_env

CONFIG_DIR_ENV_SUFFIX = "CONFIG_DIR"
DATA_DIR_ENV_SUFFIX = "DATA_DIR"
APP_DIR_NAME = "vivarium-workbench"


def _from_env_or_xdg(suffix: str, xdg_var: str, fallback: Path) -> Path:
    explicit = (get_env(suffix, "") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    xdg = (os.environ.get(xdg_var) or "").strip()
    if xdg:
        return Path(xdg).expanduser() / APP_DIR_NAME
    return fallback


def user_config_dir() -> Path:
    """The per-user configuration directory (not created)."""
    return _from_env_or_xdg(
        CONFIG_DIR_ENV_SUFFIX, "XDG_CONFIG_HOME",
        Path.home() / ".config" / APP_DIR_NAME,
    )


def user_data_dir() -> Path:
    """The per-user data directory (not created)."""
    return _from_env_or_xdg(
        DATA_DIR_ENV_SUFFIX, "XDG_DATA_HOME",
        Path.home() / ".local" / "share" / APP_DIR_NAME,
    )


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` (and parents) and restrict it to the owner (``0700``)."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    return path


def write_private_text(path: Path, text: str) -> None:
    """Atomically write ``text`` to ``path`` with owner-only (``0600``) permissions.

    Writes a sibling temporary file created ``0600`` and ``os.replace``s it into
    place, so readers never see a partial file and the content is never briefly
    world-readable.
    """
    path = Path(path)
    ensure_private_dir(path.parent)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if os.name == "posix":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def append_private_line(path: Path, line: str) -> None:
    """Append one line (a trailing newline is added) to an owner-only file."""
    path = Path(path)
    ensure_private_dir(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    fd = os.open(path, flags, 0o600)
    try:
        data = memoryview((line.rstrip("\n") + "\n").encode("utf-8"))
        while data:
            written = os.write(fd, data)
            data = data[written:]
    finally:
        os.close(fd)
    if os.name == "posix":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
