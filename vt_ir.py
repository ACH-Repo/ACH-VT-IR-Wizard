#!/usr/bin/env python3
"""
vt_ir.py  --  Variable-temperature IR orchestrator
            for Thermo Nicolet iS5  +  Specac heated Golden Gate ATR
                                   +  Specac USB temperature controller.

The two instruments come from different manufacturers and cannot talk to
each other directly.  This script drives both from one process:

    *  the heater  -- through the Specac CLI  ( specac.cmd sp <T> w )
    *  the OMNIC software -- through DDE  ( app="OMNIC", topic="Spectra" )

Workflow (always two passes per experiment):

    Pass 1  --  mode 0  (Background)
        Empty ATR.  At each temperature, collect a single-beam BG and
        save it as  BG_<sample>_<T>C.SPA  in  <output_root>/<sample>/.

    Pass 2  --  mode 1  (Sample)
        Sample loaded.  At each temperature, tell OMNIC to use the
        matching BG_<sample>_<T>C.SPA  as the background for the next
        ratio (Collect BackgroundHandling = ThisBkg,
               Collect BackgroundFileName = <path> ),
        then collect the sample and save it as  <sample>_<T>C.SPA.

    The two passes are separated by the cooldown of the heated cell --
    no manual sample swap at temperature, no parallel-process timing.

Lab-specific paths live in  vt_ir_config.ini  next to this script.

Requires:  Python 3.10+, pywin32 (for DDE).  Tested against OMNIC's 2002
DDE manual; should work on OMNIC 6.0 and later.
"""
from __future__ import annotations

import argparse
import configparser
import datetime as dt
import logging
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple


CONFIG_FILE = Path(__file__).with_name("vt_ir_config.ini")
LOG_FORMAT = "%(asctime)s  %(levelname)-7s  %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"


# ---------------------------------------------------------------------------
# OMNIC DDE wrapper
# ---------------------------------------------------------------------------

class OmnicDDE:
    """Thin wrapper around pywin32's DDE client for OMNIC.

    Usage:
        with OmnicDDE() as omnic:
            omnic.exec('LoadParameters "C:\\\\path\\\\my.exp"')
            omnic.set_param("Collect", "BackgroundHandling", "ThisBkg")
            omnic.exec('CollectSample "title" Auto')

    A bracket-less command is auto-bracketed.  Quoting of paths with spaces
    is the caller's responsibility (use shlex.quote-style double quotes).
    """

    APP = "OMNIC"
    TOPIC = "Spectra"

    def __init__(self,
                 dry_run: bool = False,
                 poll_interval_s: float = 2.0,
                 collect_timeout_s: float = 1800.0):
        """`poll_interval_s` and `collect_timeout_s` govern how long we wait
        for OMNIC to finish a CollectBackground / CollectSample.  Increase
        `collect_timeout_s` if you run experiments that take longer than
        30 minutes per spectrum."""
        self.dry_run = dry_run
        self.poll_interval_s = poll_interval_s
        self.collect_timeout_s = collect_timeout_s
        self._server = None
        self._conv = None

    def __enter__(self):
        if self.dry_run:
            logging.info("[dry-run]  Would connect to OMNIC DDE  (app=%s, topic=%s)", self.APP, self.TOPIC)
            return self
        try:
            import win32ui  # noqa: F401  (required for the dde module to load)
            import dde
        except ImportError as e:
            raise SystemExit(
                "pywin32 is required for OMNIC DDE access.\n"
                "Install it with:  pip install pywin32\n"
                f"(import error: {e})"
            )
        self._server = dde.CreateServer()
        self._server.Create("VT_IR_Client")
        self._conv = dde.CreateConversation(self._server)
        try:
            self._conv.ConnectTo(self.APP, self.TOPIC)
        except Exception as e:
            raise SystemExit(
                f"Could not open DDE conversation with OMNIC ({e}).\n"
                "Is OMNIC running and a spectrum/experiment window open?"
            )
        logging.info("Connected to OMNIC via DDE.")
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.dry_run:
            return
        try:
            self._conv = None
            if self._server is not None:
                self._server.Shutdown()
        except Exception:
            pass

    # -- low-level ---------------------------------------------------------
    def exec(self, cmd: str) -> None:
        """ExecuteOMNIC.  Blocks until OMNIC finishes processing the command.

        Note: pywin32's DDE transactions have a hard 60-second timeout, so
        long-running commands (e.g. a 128-scan CollectBackground) MUST be
        issued with the ``Polling`` keyword and waited on via
        :meth:`_wait_until_idle` rather than via this method directly.
        """
        bracketed = cmd if cmd.startswith("[") else f"[{cmd}]"
        logging.info("DDE exec: %s", bracketed)
        if self.dry_run:
            return
        self._conv.Exec(bracketed)

    def request(self, item: str) -> str:
        """DDERequest -- read a parameter or Result item.  Returns a stripped,
        UTF-8 decoded string (pywin32 sometimes returns bytes, sometimes str)."""
        if self.dry_run:
            return ""
        raw = self._conv.Request(item)
        if isinstance(raw, (bytes, bytearray)):
            raw = bytes(raw).decode("latin-1", errors="replace")
        # Trailing NUL is common from OMNIC; strip it along with whitespace.
        return raw.replace("\x00", "").strip()

    def _wait_until_idle(self, menu_item: str,
                         settle_s: float = 2.0) -> None:
        """Poll ``MenuStatus <menu_item>`` until OMNIC reports it is no longer
        Disabled.  Used after a ``Polling`` Exec to wait for the collection
        to actually finish without tripping the 60-second DDE timeout.

        The DDE manual (page 124, Polling) recommends this exact pattern:
            ExecuteOMNIC "CollectSample Auto Polling"
            While GetOMNIC("MenuStatus CollectSample") = "Disabled"
                DoEvents
            Wend
        """
        if self.dry_run:
            return
        # Give OMNIC a moment to actually enter the busy state, otherwise the
        # first MenuStatus query may still see the menu enabled.
        time.sleep(settle_s)
        deadline = time.time() + self.collect_timeout_s
        item = f"MenuStatus {menu_item}"
        last_log = 0.0
        while time.time() < deadline:
            try:
                status = self.request(item).lower()
            except Exception as e:
                logging.debug("Transient DDE request error (%s); will retry.", e)
                status = "disabled"
            if status and "disabled" not in status:
                logging.info("OMNIC ready  (MenuStatus %s = %s).",
                             menu_item, status or "enabled")
                return
            # Heartbeat so the user can see we are still waiting.
            now = time.time()
            if now - last_log > 30.0:
                logging.info("Still waiting for %s ... (status=%s)",
                             menu_item, status or "?")
                last_log = now
            time.sleep(self.poll_interval_s)
        raise TimeoutError(
            f"OMNIC stayed busy for more than {self.collect_timeout_s:.0f}s "
            f"on {menu_item}.  Increase [omnic] collect_timeout_s in the "
            f"config if your scans are genuinely this long."
        )

    # -- high-level helpers -----------------------------------------------
    def poke(self, item: str, value: str) -> None:
        """DDEPoke a named item.  Matches the introductory example in the
        OMNIC DDE manual (page 11).  Unlike '[Set ...]' via Exec, Poke does
        not go through OMNIC's command parser, so values containing
        backslashes, spaces, or other characters that the parser might
        mis-handle (e.g. Windows paths) come through verbatim.
        """
        logging.info("DDE poke: %s = %r", item, value)
        if self.dry_run:
            return
        # pywin32 expects bytes for the Data argument.
        data = value.encode("latin-1") if isinstance(value, str) else value
        self._conv.Poke(item, data)

    def set_param(self, group: str, name: str, value: str) -> None:
        """Write a parameter value.  Tries DDEPoke first; on failure, falls
        back to '[Set <group> <name> "<value>"]' Exec.  Poke is the more
        reliable path for path-like values -- the previous Set-only code
        crashed with 'dde.error: Exec failed' on
        '[Set Collect BackgroundFileName "<path with backslashes>"]'.
        """
        item = f"{group} {name}"
        try:
            self.poke(item, value)
            return
        except Exception as e:
            logging.warning(
                "DDE Poke failed for %r (%s); retrying via [Set ...] Exec.",
                item, e,
            )
        self.exec(f'Set {group} {name} "{value}"')

    def load_parameters(self, exp_file: str, keep_bkg: bool = True) -> None:
        """LoadParameters -- load an OMNIC .exp file.  KeepBkg preserves any
        currently-set background so the BackgroundFileName poke we do later
        in sample mode is not blown away on the next iteration."""
        flags = " KeepBkg" if keep_bkg else ""
        self.exec(f'LoadParameters "{exp_file}"{flags}')

    # The collect_* helpers below use the Polling keyword so the pywin32 DDE
    # Exec call returns immediately and Python (not Windows DDE) controls the
    # wait via _wait_until_idle.  This is the only reliable way to handle
    # collections that take longer than the 60-second DDE transaction window.
    def collect_background(self, title: str) -> None:
        self.exec(f'CollectBackground "{title}" Auto Polling')
        self._wait_until_idle("CollectBackground")

    def collect_sample(self, title: str) -> None:
        self.exec(f'CollectSample "{title}" Auto Polling')
        self._wait_until_idle("CollectSample")

    def display(self) -> None:
        """Move newly-collected spectra from the invisible DDE window into
        the active spectral window.  Required between a non-Invoke collect
        and Export so the spectrum is the selected one."""
        self.exec("Display")

    def import_spectrum(self, path: str) -> None:
        """Open a saved .SPA on disk.  The imported spectrum becomes the
        active/selected one and lives in the (then-invisible) DDE window
        unless a Display follows."""
        self.exec(f'Import "{path}"')

    def set_as_background(self) -> None:
        """Promote the currently selected single-beam spectrum to be the
        background that subsequent ratios use.  This is the GUI's
        right-click -> 'Set as Background' -- equivalent to the parameter
        pair Collect/BackgroundHandling=ThisBkg + Collect/BackgroundFileName,
        which OMNIC's parameter API rejected on this build."""
        self.exec("SetAsBackground")

    def export(self, path: str) -> None:
        self.exec(f'Export "{path}"')

    def clear(self) -> None:
        self.exec("DeleteSelectedSpectra")


# ---------------------------------------------------------------------------
# Specac USB controller wrapper
# ---------------------------------------------------------------------------

class Specac:
    """Wrapper around the specac.cmd CLI (controller software v1.0.23.0+)."""

    def __init__(self, exe: str, dry_run: bool = False):
        self.exe = exe
        self.dry_run = dry_run

    def _run(self, *args: str, timeout: Optional[float] = None) -> str:
        cmd = [self.exe, *args]
        logging.info("Specac:  %s", " ".join(shlex.quote(c) for c in cmd))
        if self.dry_run:
            return ""
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
            )
        except FileNotFoundError:
            raise SystemExit(
                f"Could not find specac.cmd at:\n    {self.exe}\n"
                "Check the [specac] exe path in vt_ir_config.ini."
            )
        # Promote stdout/stderr logging to INFO whenever they have content -- we
        # need to see what query commands like 'temp?' actually print, since
        # the manual doesn't document the format.  Empty calls (e.g. 'on') stay
        # quiet because the conditional skips them.
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        if out:
            logging.info("Specac stdout: %r", out)
        if err:
            logging.info("Specac stderr: %r", err)
        if r.returncode != 0:
            logging.warning("Specac returned %d.", r.returncode)
        # Many Specac.Cmd queries print to stderr rather than stdout; return
        # whichever is non-empty so the caller can parse it.
        return out or err

    # -- commands ----------------------------------------------------------
    def on(self):  self._run("on")
    def off(self): self._run("off")

    def set_units_c(self):           self._run("c")
    def set_tolerance(self, deg):    self._run("tol", str(deg))
    def set_ramp(self, rate):        self._run("ramp", str(rate))

    def goto_and_wait(self, T: float, timeout: float = 3600.0) -> None:
        """Set setpoint and block until reached (within tolerance)."""
        self._run("sp", str(T), "w", timeout=timeout)

    # Pattern matches the first signed decimal number in a string, with
    # either '.' or ',' as the decimal separator -- the Specac CLI on the
    # lab PC reports temperatures using a European locale (e.g. '31,37').
    _NUM_RE = re.compile(r"[-+]?\d+(?:[.,]\d+)?")

    def get_temp(self) -> Optional[float]:
        out = self._run("temp?")
        if not out:
            return None
        m = self._NUM_RE.search(out)
        if not m:
            logging.warning("Could not extract a number from temp? output: %r", out)
            return None
        try:
            # float() needs '.' even if Specac printed ','.
            return float(m.group().replace(",", "."))
        except ValueError:
            return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_temps(s: str, tmin: float, tmax: float) -> List[int]:
    """Parse '(50,200,20)' range syntax or whitespace-separated list."""
    s = s.strip()
    m = re.match(r"^\(\s*(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)\s*\)$", s)
    if m:
        a, b, c = (float(x) for x in m.groups())
        step = int(c) if c > 0 else 1
        Ts = list(range(int(a), int(b) + 1, step))
        if not Ts or Ts[-1] != int(b):
            Ts.append(int(b))
    else:
        Ts = [int(round(float(x))) for x in s.replace(",", " ").split()]
    if not Ts:
        raise ValueError("no temperatures parsed from input")
    lo, hi = min(Ts), max(Ts)
    if lo < tmin:
        raise ValueError(f"minimum temperature {lo} below allowed {tmin}")
    if hi > tmax:
        raise ValueError(f"maximum temperature {hi} above allowed {tmax}")
    return Ts


def ask(text: str, default: Optional[str] = None) -> str:
    """Plain input() with default-value handling."""
    suffix = f" [{default}]" if default is not None else ""
    while True:
        ans = input(f"{text}{suffix}: ").strip()
        if ans:
            return ans
        if default is not None:
            return default
        print("  (a value is required)")


def ask_int(text: str, default: Optional[int] = None) -> int:
    while True:
        raw = ask(text, str(default) if default is not None else None)
        try:
            return int(raw)
        except ValueError:
            print("  (please enter a whole number)")


def ask_choice(text: str, choices: List[str], default: Optional[str] = None) -> str:
    pretty = "/".join(choices)
    while True:
        ans = ask(f"{text} ({pretty})", default).lower()
        if ans in choices:
            return ans
        print(f"  (please type one of: {pretty})")


def sanitize_name(s: str) -> str:
    """Make a string safe to use in filenames."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_") or "sample"


def index_width(n_steps: int) -> int:
    """Number of digits to zero-pad the per-step index with so that the
    filenames sort chronologically in Explorer.  Minimum 2 (so single-digit
    runs still produce '00, 01' rather than '0, 1', which looks cleaner)."""
    return max(2, len(str(max(n_steps - 1, 0))))


def bg_path(folder: Path, sample: str, T: int, prefix: str = "") -> Path:
    # Backgrounds carry no direction suffix -- one BG per temperature is
    # shared between up- and down-scan samples (see step_sample docstring).
    # The index prefix (e.g. "00_") makes Explorer sort chronologically.
    return folder / f"{prefix}BG_{sample}_{T}C.SPA"


def sample_path(folder: Path, sample: str, T: int,
                suffix: str = "", prefix: str = "") -> Path:
    return folder / f"{prefix}{sample}_{T}C{suffix}.SPA"


def find_bg_file(bg_dir: Path, sample: str, T: int) -> Optional[Path]:
    """Locate the BG file for (sample, T) regardless of whether it carries
    an index prefix.  Matches both the indexed style written by current runs
    (``NN_BG_<sample>_<T>C.SPA``) and the legacy unindexed style from earlier
    runs (``BG_<sample>_<T>C.SPA``).  Returns ``None`` if no candidate exists."""
    if not bg_dir.exists():
        return None
    candidates = sorted(bg_dir.glob(f"*BG_{sample}_{T}C.SPA"))
    return candidates[0] if candidates else None


# Single source of truth for per-direction metadata used across the wizard
# summary (glyph), the per-step log line (arrow), and analyse_live.py's
# overlay shading (color, label).  Adding a new schedule kind means adding
# one row here -- not editing four sites.  Background ("bg") is also listed
# even though it isn't a step direction, because analyse_live shades BG
# scans too.
SCHEDULE_KINDS = {
    "up":     {"glyph": "^", "arrow": "Heating up to",
               "color": (1.0, 0.2, 0.2, 0.22), "label": "Sample (up)"},
    "down":   {"glyph": "v", "arrow": "Cooling down to",
               "color": (1.0, 0.6, 0.0, 0.25), "label": "Sample (down)"},
    "return": {"glyph": "*", "arrow": "Cooling back to starting temperature",
               "color": (0.2, 0.7, 0.2, 0.28), "label": "Sample (return)"},
    "bg":     {"glyph": "B", "arrow": "(background, not a step)",
               "color": (0.2, 0.2, 1.0, 0.18), "label": "Background"},
}


def build_schedule(temps: List[int], down_scan: bool,
                   return_to_start: bool = False):
    """Build the ordered list of (temperature, direction, filename-suffix)
    steps for a run.

    * neither flag           ->  [(T1,'up',''), ...]               (plain names)
    * down_scan on           ->  ascending '_up' suffix, then descending
                                 (excluding the peak) '_down' suffix
    * return_to_start on     ->  one final '_return' step at temps[0] after
                                 everything else, to check if the sample reverts
                                 to its starting state.  Useful as a lightweight
                                 alternative to a full down-scan.
    """
    up_suffix = "_up" if down_scan else ""
    sched = [(T, "up", up_suffix) for T in temps]
    if down_scan:
        for T in reversed(temps[:-1]):
            sched.append((T, "down", "_down"))
    if return_to_start:
        sched.append((temps[0], "return", "_return"))
    return sched


def export_all(omnic: "OmnicDDE", spa_path: Path, extra_formats: List[str]) -> List[Path]:
    """Export the currently-selected spectrum to its native .SPA plus every
    extra format requested (OMNIC picks the format from the extension).
    Returns the list of files written."""
    written = [spa_path]
    omnic.export(str(spa_path))
    base = spa_path.with_suffix("")  # strip the trailing '.SPA'
    for fmt in extra_formats:
        alt = Path(f"{base}.{fmt}")
        omnic.export(str(alt))
        written.append(alt)
    return written


# ---------------------------------------------------------------------------
# Per-temperature loop bodies
# ---------------------------------------------------------------------------

def step_background(omnic: OmnicDDE, sample: str, T: int, out_dir: Path,
                    extra_formats: List[str], prefix: str = "") -> Path:
    title = f"{prefix}BG_{sample}_{T}C"
    out = bg_path(out_dir, sample, T, prefix=prefix)
    omnic.collect_background(title)   # blocks until done (via MenuStatus polling)
    omnic.display()                   # invisible DDE window -> active spectral window
    export_all(omnic, out, extra_formats)
    omnic.clear()
    return out


def step_sample(
    omnic: OmnicDDE, sample: str, T: int, out_dir: Path, bg_dir: Path,
    suffix: str = "", extra_formats: Optional[List[str]] = None,
    prefix: str = "",
) -> Tuple[Path, Path]:
    """Per-T sample step.

    OMNIC's Collect/BackgroundHandling + Collect/BackgroundFileName parameter
    pair would be the documented way to bind a saved BG file to the next
    sample collection -- but on the lab iS5 build both DDEPoke and
    [Set Collect BackgroundFileName "<path>"] Exec are rejected.  Instead we
    use the GUI-equivalent flow:

        Import the BG file  ->  Display (so it becomes the selected spectrum)
        ->  SetAsBackground (binds it as the current ratio reference)
        ->  DeleteSelectedSpectra (clean up; the BG reference persists in
            OMNIC's background slot, separate from the visible workspace)
        ->  CollectSample (ratios against the just-set background)
        ->  Display + Export + DeleteSelectedSpectra
    """
    bg = find_bg_file(bg_dir, sample, T)
    if bg is None:
        raise FileNotFoundError(
            f"Background file for {T} C is missing in {bg_dir}\n"
            "Run mode 0 (Background) first with the same sample name and temperatures."
        )

    # 1) Bind the matching BG file as the current background.
    omnic.import_spectrum(str(bg))
    omnic.display()              # invisible DDE window -> active window, becomes selected
    omnic.set_as_background()    # current BG <- selected spectrum
    omnic.clear()                # remove the BG visualization (BG reference persists)

    # 2) Collect the sample; it will ratio against the BG we just set.
    title = f"{prefix}{sample}_{T}C{suffix}"
    out = sample_path(out_dir, sample, T, suffix=suffix, prefix=prefix)
    omnic.collect_sample(title)  # blocks until done (via MenuStatus polling)
    omnic.display()              # invisible DDE window -> active spectral window
    export_all(omnic, out, extra_formats or [])
    omnic.clear()
    return out, bg


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

# Matches BG filenames with or without the chronological index prefix
# (e.g. "BG_foo_50C.SPA" and "07_BG_foo_50C.SPA").  Used by survey_bg_folders
# to enumerate which sample folders have backgrounds, and tolerant of legacy
# unindexed files from earlier runs.
_BG_FILE_RE = re.compile(
    r"^(?:\d+_)?BG_(?P<sample>.+)_(?P<T>-?\d+)C\.SPA$",
    re.IGNORECASE,
)


def survey_bg_folders(bg_root: Path) -> Dict[str, List[int]]:
    """Walk every immediate sub-folder of ``bg_root`` and report which
    temperatures (in C) it has BG files for.  Used to give the user a picker
    hint when the sample-mode preflight finds no matching BG files for the
    name they typed -- they almost always want a name that's already on disk.
    """
    out: Dict[str, List[int]] = {}
    if not bg_root.exists():
        return out
    for folder in sorted(bg_root.iterdir()):
        if not folder.is_dir():
            continue
        temps: List[int] = []
        for f in folder.iterdir():
            m = _BG_FILE_RE.match(f.name)
            if m:
                # Match the *folder-level* sample (so e.g. a stray file copied
                # in from another sample isn't counted here).
                if m.group("sample") == folder.name:
                    try:
                        temps.append(int(m.group("T")))
                    except ValueError:
                        pass
        if temps:
            out[folder.name] = sorted(set(temps))
    return out


def spawn_live_plot(sample: str,
                    config_path: Path,
                    live_cfg,
                    dry_run: bool) -> Optional[subprocess.Popen]:
    """Spawn analyse_live.py in a fresh console window, locked onto this
    run's sample folder.  Returns the Popen handle, or None if spawning was
    skipped or failed.  The orchestrator does NOT terminate the plotter on
    exit -- the user closes the plot window when they're done with it."""
    if dry_run:
        logging.info("[dry-run] would spawn analyse_live.py for sample %r.", sample)
        return None
    script = Path(__file__).with_name("analyse_live.py")
    if not script.exists():
        logging.warning("Live plot script not found at %s -- skipping auto-launch.",
                        script)
        return None
    cmd = [
        sys.executable, str(script),
        "--config", str(config_path),
        "--sample", sample,
        "--interval", str(live_cfg.get("interval_s", "5")),
        "--scan-duration", str(live_cfg.get("scan_duration_s", "210")),
    ]
    kwargs = {}
    if sys.platform == "win32":
        # CREATE_NEW_CONSOLE = 0x00000010 -- give the plotter its own console
        # window so its prints don't tangle with the orchestrator's prompts.
        kwargs["creationflags"] = 0x00000010
    try:
        proc = subprocess.Popen(cmd, **kwargs)
        logging.info("Spawned live overlay plot  (pid=%d, sample=%s).",
                     proc.pid, sample)
        return proc
    except Exception as e:
        logging.warning("Could not spawn live overlay plot: %s", e)
        return None


def build_session_dir(out_root: Path, sample: str) -> Path:
    """One folder per sample.  Re-using the same name across BG and sample
    runs is required for filename-based pairing."""
    d = out_root / sample
    d.mkdir(parents=True, exist_ok=True)
    return d


def configure_logging(log_dir: Path, sample: str, mode_label: str) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"{stamp}_{sample}_{mode_label}.log"
    handlers = [logging.FileHandler(log_file, encoding="utf-8"),
                logging.StreamHandler(sys.stdout)]
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt=LOG_DATEFMT,
                        handlers=handlers, force=True)
    logging.info("Log file: %s", log_file)
    return log_file


def load_config(path: Path) -> configparser.ConfigParser:
    if not path.exists():
        raise SystemExit(
            f"Missing config file: {path}\n"
            "Copy vt_ir_config.ini next to vt_ir.py and fill in the paths."
        )
    cfg = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    cfg.read(path, encoding="utf-8")
    return cfg


def banner() -> None:
    print("=" * 66)
    print("  VT-IR Orchestrator   --   iS5  +  Specac heated Golden Gate")
    print("=" * 66)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="VT-IR measurement orchestrator.")
    p.add_argument("--config", default=str(CONFIG_FILE),
                   help="Path to vt_ir_config.ini (default: next to this script).")
    p.add_argument("--dry-run", action="store_true",
                   help="Print every DDE / Specac call without executing them.")
    p.add_argument("--no-live-plot", action="store_true",
                   help="Skip auto-launching the analyse_live.py overlay plot, "
                        "regardless of the [live_plot] config setting.")
    args = p.parse_args(argv)

    cfg = load_config(Path(args.config))
    paths = cfg["paths"]
    defaults = cfg["defaults"]
    heater = cfg["heater"]
    # [omnic], [live_plot], [export] sections are optional -- fall back to defaults.
    omnic_cfg = cfg["omnic"] if cfg.has_section("omnic") else {}
    live_cfg = cfg["live_plot"] if cfg.has_section("live_plot") else {}
    export_cfg = cfg["export"] if cfg.has_section("export") else {}

    # Extra (non-.SPA) formats to write for every spectrum.  Parsed from a
    # comma-separated list of extensions; '.SPA' is always written regardless.
    extra_formats = [
        f.strip().lstrip(".").lower()
        for f in str(export_cfg.get("additional_formats", "csv")).split(",")
        if f.strip()
    ]

    banner()

    if args.dry_run:
        print("\n*** DRY RUN -- no DDE calls, no heater commands ***\n")

    # -- prompts ----------------------------------------------------------
    sample_raw = ask("Sample / experiment name")
    sample = sanitize_name(sample_raw)
    if sample != sample_raw:
        print(f"  (using sanitized name: {sample})")

    mode = ask_choice("Mode -- 0 = Background  (empty ATR)  /  1 = Sample",
                      ["0", "1"], default="0")
    mode_label = "BG" if mode == "0" else "SAMPLE"

    temps_raw = ask(
        "Temperatures -- list \"50 100 150\" or range \"(50,200,20)\"",
        defaults.get("temps_default", "(50,200,20)"),
    )
    tmin = float(defaults.get("tmin", "30"))
    tmax = float(defaults.get("tmax", "250"))
    try:
        Ts = parse_temps(temps_raw, tmin, tmax)
    except ValueError as e:
        sys.exit(f"Bad temperature input: {e}")

    # Down-scan and return-to-start only make sense for samples (backgrounds
    # are reused for both directions), so we only offer them in sample mode.
    # The two are mutually exclusive in the wizard -- down-scan already ends
    # at the starting temperature, so asking both would be redundant.
    down_scan = False
    return_to_start = False
    if mode == "1":
        down_scan = ask_choice(
            "Add a cool-down (down-scan) after heating, to check reversibility?",
            ["y", "n"], default="n",
        ) == "y"
        if not down_scan:
            return_to_start = ask_choice(
                "Add ONE final measurement at the starting temperature after "
                "cooling? (quick reversibility check, no full down-scan)",
                ["y", "n"], default="n",
            ) == "y"

    exp_file = ask("OMNIC experiment file (.exp)",
                   defaults.get("omnic_exp_file", ""))

    equilibration = ask_int(
        "Extra equilibration seconds at each set point (after Specac says ready)",
        int(defaults.get("equilibration_s", "30")),
    )

    # Build the ordered (T, direction, suffix) schedule.  down_scan and
    # return_to_start are guaranteed False outside sample mode by the prompt
    # block above, so no extra guard is needed here.
    schedule = build_schedule(Ts, down_scan=down_scan,
                              return_to_start=return_to_start)

    out_root = Path(paths["output_root"])
    bg_root = Path(paths.get("bg_root", paths["output_root"]))
    session_dir = build_session_dir(out_root, sample)
    bg_dir = build_session_dir(bg_root, sample)
    log_dir = Path(paths["log_dir"])
    log_file = configure_logging(log_dir, sample, mode_label)

    # -- pre-flight summary ----------------------------------------------
    logging.info("Sample:                %s", sample)
    logging.info("Mode:                  %s", "Background" if mode == "0" else "Sample")
    logging.info("Temperatures (C):      %s", ", ".join(str(T) for T in Ts))
    if down_scan or return_to_start:
        extra = ("up then cool-down" if down_scan
                 else "up then one final point at starting T")
        logging.info("Extra steps:           yes  (%d total: %s)", len(schedule), extra)
        logging.info(
            "Schedule:              %s",
            "  ".join(f"{T}{SCHEDULE_KINDS[d]['glyph']}" for T, d, _ in schedule),
        )
    logging.info("OMNIC experiment file: %s", exp_file)
    logging.info("Extra export formats:  %s", ", ".join(extra_formats) or "(none, SPA only)")
    logging.info("Equilibration (s):     %d", equilibration)
    logging.info("Output folder:         %s", session_dir)
    if mode == "1":
        logging.info("Background folder:     %s", bg_dir)
        # Backgrounds are keyed by unique temperature regardless of direction.
        # find_bg_file matches both indexed and unindexed naming so it accepts
        # BG sets collected before the index feature was added.
        missing = [T for T in Ts if find_bg_file(bg_dir, sample, T) is None]
        if missing:
            msg = [
                f"Missing BG files for sample {sample!r}: "
                + ", ".join(f"{T} C" for T in missing),
                f"Looked in:  {bg_dir}",
            ]
            available = survey_bg_folders(bg_root)
            # Strip out the sample we just tried so we don't suggest it back.
            available.pop(sample, None)
            if available:
                msg.append("")
                msg.append("Sample folders that DO have BG files:")
                for name, temps in available.items():
                    t_str = ", ".join(str(T) for T in temps)
                    msg.append(f"  - {name}   ({t_str} C)")
                msg.append("")
                msg.append(f"Either rerun BG mode first under name {sample!r}, "
                           f"or restart and pick one of the names above.")
            else:
                msg.append("")
                msg.append("No other sample folders have BG files either -- "
                           "you need to run BG mode first.")
            sys.exit("\n".join(msg))
    logging.info("Log file:              %s", log_file)
    logging.info("Specac exe:            %s", paths["specac_exe"])
    print()

    if ask_choice("Ready? (y = start, n = abort)", ["y", "n"], "y") != "y":
        logging.info("Aborted by user.")
        return 1

    # -- spawn live overlay plot (in its own window) ----------------------
    # Done AFTER the Ready prompt so a plot window doesn't pop up while the
    # user is still typing answers, and locked onto the just-confirmed sample.
    live_enabled = str(live_cfg.get("enabled", "true")).strip().lower() in (
        "1", "true", "yes", "y", "on"
    )
    if args.no_live_plot:
        logging.info("Live overlay plot disabled by --no-live-plot.")
    elif not live_enabled:
        logging.info("Live overlay plot disabled in config ([live_plot] enabled).")
    else:
        spawn_live_plot(sample, Path(args.config), live_cfg, args.dry_run)

    # -- run --------------------------------------------------------------
    specac = Specac(paths["specac_exe"], dry_run=args.dry_run)
    started = time.time()
    produced: List[Tuple[int, Path]] = []

    # Sanity-check margin for the "did we actually reach the setpoint?" probe.
    tol_c = float(heater.get("tolerance_c", "1"))
    # We allow tolerance + 5 C: a generous bound that still catches the
    # "Specac said done but cell was at 37 C while target was 50 C" pathology.
    sp_check_margin = tol_c + 5.0

    try:
        # Heater preamble  ---------------------------------------------
        # An explicit OFF before ON flushes any stuck "Wait" state left over
        # from a previous .prog session or an aborted run -- without this,
        # a fresh "sp T w" can return immediately even though the cell is
        # nowhere near T (observed in pyerror.txt run history).
        specac.off()
        specac.set_units_c()
        specac.set_tolerance(heater.get("tolerance_c", "1"))
        specac.set_ramp(heater.get("ramp_c_per_min", "50"))
        specac.on()

        # Log the cell temperature before we start ramping -- gives the user
        # ground-truth context in the .log file.
        start_T = specac.get_temp()
        if start_T is not None:
            logging.info("Cell temperature before ramping: %.1f C.", start_T)

        # OMNIC preamble  ----------------------------------------------
        with OmnicDDE(
            dry_run=args.dry_run,
            poll_interval_s=float(omnic_cfg.get("poll_interval_s", "2")),
            collect_timeout_s=float(omnic_cfg.get("collect_timeout_s", "1800")),
        ) as omnic:
            omnic.load_parameters(exp_file, keep_bkg=True)

            # Per-step loop  -------------------------------------------
            # Filename index: a zero-based, zero-padded counter prepended to
            # every spectrum so Explorer's name sort is also chronological.
            # Padding adapts to the schedule length (>=2 digits for tidy
            # display of small runs).
            pad = index_width(len(schedule))
            for i, (T, direction, suffix) in enumerate(schedule, 1):
                prefix = f"{i - 1:0{pad}d}_"
                logging.info("--- Step %d/%d:  %d C  (%s)  ---",
                             i, len(schedule), T, direction)
                step_t0 = time.time()

                logging.info("%s %d C ...", SCHEDULE_KINDS[direction]["arrow"], T)
                specac.goto_and_wait(
                    T, timeout=float(heater.get("ramp_timeout_s", "3600"))
                )

                # Sanity check: Specac.Cmd has been observed returning from
                # "sp T w" while the cell was still far from T (stuck "Wait"
                # state from a previous program).  Query temp? and abort
                # before we waste a scan on the wrong temperature.
                actual_T = specac.get_temp()
                if actual_T is None:
                    logging.warning(
                        "Could not read cell temperature via 'temp?' after "
                        "setpoint wait -- proceeding without sanity check."
                    )
                else:
                    delta = abs(actual_T - T)
                    logging.info("Cell temperature: %.1f C (target %d C, delta %.1f C).",
                                 actual_T, T, delta)
                    if delta > sp_check_margin:
                        raise RuntimeError(
                            f"Specac reported 'setpoint reached' but cell is at "
                            f"{actual_T:.1f} C (target {T} C, allowed delta "
                            f"{sp_check_margin:.1f} C).  This is the false-wait "
                            f"bug seen before -- usually fixed by closing and "
                            f"reopening the Specac controller GUI to clear any "
                            f"stuck program state.  Aborting before OMNIC starts "
                            f"a collection at the wrong temperature."
                        )

                logging.info("Setpoint reached.  Equilibrating for %d s.", equilibration)
                if not args.dry_run:
                    time.sleep(equilibration)

                if mode == "0":
                    out = step_background(omnic, sample, T, session_dir,
                                          extra_formats, prefix=prefix)
                    logging.info("Saved BG:  %s", out)
                    produced.append((T, out))
                else:
                    out, used_bg = step_sample(omnic, sample, T, session_dir, bg_dir,
                                               suffix=suffix, extra_formats=extra_formats,
                                               prefix=prefix)
                    logging.info("Saved sample:  %s  (BG = %s)", out, used_bg)
                    produced.append((T, out))

                logging.info("Step %d done in %.1f s.", i, time.time() - step_t0)

    finally:
        logging.info("Switching heater off.")
        try:
            specac.off()
        except Exception as e:
            logging.warning("Could not switch heater off: %s", e)

    # -- summary ----------------------------------------------------------
    logging.info("=" * 60)
    logging.info("Done.  Total time: %.1f min.  %d spectra produced.",
                 (time.time() - started) / 60.0, len(produced))
    for T, path in produced:
        logging.info("  %3d C  ->  %s", T, path)
    logging.info("Log file kept at: %s", log_file)
    return 0


def _entry() -> int:
    """Wrapper that routes any uncaught exception into the run log file.

    Tracebacks would otherwise only land on stderr, where they vanish when the
    user closes the .bat console window.  By the time main() raises, the log
    file is already configured (configure_logging runs early), so logging the
    exception captures it in the same .log the rest of the run wrote to.
    """
    try:
        return main()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        logging.warning("Interrupted by user (Ctrl-C).")
        return 130
    except Exception:
        # logging.exception writes the message + full traceback to all handlers
        # (file + console).
        logging.exception("Unhandled exception -- aborting.")
        return 1


if __name__ == "__main__":
    raise SystemExit(_entry())
