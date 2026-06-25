"""Config-file discovery and loading for the pip-installed wizard.

When the project lived as loose scripts the config sat next to ``vt_ir.py``.
Installed via pip the package lives in a read-only ``site-packages`` directory,
so the editable ``vt_ir_config.ini`` must live somewhere the user owns.

Resolution order (``resolve_config_path``):

    1. an explicit ``--config PATH``                (always wins)
    2. ``./vt_ir_config.ini`` in the current folder (per-experiment configs)
    3. ``%APPDATA%/vtir-wizard/vt_ir_config.ini``   (the per-user copy)

``vtir-wizard --init-config`` writes the packaged template to the per-user
location so a fresh install has something to edit.
"""
from __future__ import annotations

import configparser
import os
from pathlib import Path
from typing import Optional

APP_DIR_NAME = "vtir-wizard"
CONFIG_NAME = "vt_ir_config.ini"


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------
def user_config_dir() -> Path:
    """Per-user config directory.  ``%APPDATA%/vtir-wizard`` on Windows (the
    documented target); falls back to ``~/.config/vtir-wizard`` elsewhere so the
    package still imports/tests on non-Windows CI."""
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / APP_DIR_NAME
    return Path.home() / ".config" / APP_DIR_NAME


def user_config_path() -> Path:
    return user_config_dir() / CONFIG_NAME


def packaged_template_text() -> str:
    """The bundled template ``vt_ir_config.ini`` shipped under the package's
    ``data`` directory.  Read through importlib.resources so it works from a
    wheel, an editable install, or a source checkout."""
    from importlib import resources

    return (resources.files("vtir_wizard.data")
            .joinpath(CONFIG_NAME)
            .read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Resolution + loading
# ---------------------------------------------------------------------------
def resolve_config_path(explicit: Optional[str]) -> Optional[Path]:
    """Return the config file to use, or ``None`` if nothing was found.

    An explicit path that does not exist is a hard error (the user clearly meant
    that file); the implicit search just falls through to the next candidate."""
    if explicit:
        p = Path(explicit).expanduser()
        if not p.exists():
            raise SystemExit(f"--config path does not exist: {p}")
        return p
    cwd = Path.cwd() / CONFIG_NAME
    if cwd.exists():
        return cwd
    user = user_config_path()
    if user.exists():
        return user
    return None


def load_config(path: Path) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    cfg.read(path, encoding="utf-8")
    return cfg


def load_config_optional(path: Optional[Path]) -> configparser.ConfigParser:
    """Tolerant load used by the plot helpers: returns an empty parser if the
    path is missing, so a standalone plot window still starts."""
    cfg = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    if path and Path(path).exists():
        cfg.read(path, encoding="utf-8")
    return cfg


def init_user_config(force: bool = False) -> tuple[Path, bool]:
    """Write the packaged template to the per-user config location.

    Returns ``(path, written)`` -- ``written`` is False if the file already
    existed and ``force`` was not given (so we never clobber a tuned config by
    accident)."""
    dest = user_config_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and not force:
        return dest, False
    dest.write_text(packaged_template_text(), encoding="utf-8")
    return dest, True


def missing_config_message() -> str:
    """Actionable message when no config is found anywhere."""
    return (
        "No vt_ir_config.ini found.\n"
        f"Looked for: ./{CONFIG_NAME} (current folder) and {user_config_path()}.\n"
        "Create the per-user copy with:\n"
        "    vtir-wizard --init-config\n"
        "then edit it (the path is printed) and re-run.  Or pass an explicit\n"
        "file with  --config <path>."
    )
