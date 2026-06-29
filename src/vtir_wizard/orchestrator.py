#!/usr/bin/env python3
"""
vtir_wizard.orchestrator  --  Variable-temperature IR orchestrator
            for Thermo Nicolet iS5  +  Specac heated Golden Gate ATR
                                   +  Specac USB temperature controller.

Installed as the ``vtir-wizard`` console command (see pyproject.toml).

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

Lab-specific paths live in  vt_ir_config.ini, resolved (in order) from
--config, the current folder, then %APPDATA%/vtir-wizard/  (see config.py).
Run  vtir-wizard --init-config  to drop an editable template in place.

Requires:  Python 3.9+, pywin32 (for DDE).  Tested against OMNIC's 2002
DDE manual; should work on OMNIC 6.0 and later.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import SCHEDULE_KINDS, __version__
from . import config as appconfig


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
                 collect_timeout_s: float = 1800.0,
                 collect_retries: int = 2,
                 retry_delay_s: float = 5.0):
        """`poll_interval_s` and `collect_timeout_s` govern how long we wait
        for OMNIC to finish a CollectBackground / CollectSample.  Increase
        `collect_timeout_s` if you run experiments that take longer than
        30 minutes per spectrum.

        `collect_retries` / `retry_delay_s` control recovery when OMNIC
        refuses to *start* a collection (a Polling Exec that blocks ~60 s and
        raises dde.error -- see _collect)."""
        self.dry_run = dry_run
        self.poll_interval_s = poll_interval_s
        self.collect_timeout_s = collect_timeout_s
        self.collect_retries = collect_retries
        self.retry_delay_s = retry_delay_s
        self._server = None
        self._conv = None
        self._conn_seq = 0  # unique DDE server name per (re)connect

    def _connect(self) -> None:
        """Create a DDE server + conversation and connect to OMNIC.  Each call
        uses a fresh server name so a reconnect doesn't collide with the
        previous (shut-down) server."""
        import win32ui  # noqa: F401  (required for the dde module to load)
        import dde
        self._conn_seq += 1
        self._server = dde.CreateServer()
        self._server.Create(f"VT_IR_Client_{self._conn_seq}")
        self._conv = dde.CreateConversation(self._server)
        self._conv.ConnectTo(self.APP, self.TOPIC)

    def _teardown(self) -> None:
        try:
            self._conv = None
            if self._server is not None:
                self._server.Shutdown()
        except Exception:
            pass
        self._server = None

    def reconnect(self) -> None:
        """Drop the current DDE channel and open a fresh one.  Used to clear a
        stuck conversation after a failed collect Exec."""
        if self.dry_run:
            return
        self._teardown()
        time.sleep(0.5)
        self._connect()
        logging.info("Reconnected to OMNIC via DDE.")

    def __enter__(self):
        if self.dry_run:
            logging.info("[dry-run]  Would connect to OMNIC DDE  (app=%s, topic=%s)", self.APP, self.TOPIC)
            return self
        try:
            import win32ui  # noqa: F401  (required for the dde module to load)
            import dde  # noqa: F401
        except ImportError as e:
            raise SystemExit(
                "pywin32 is required for OMNIC DDE access.\n"
                "Install it with:  pip install pywin32\n"
                f"(import error: {e})"
            )
        try:
            self._connect()
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
        self._teardown()

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

    def exec_resilient(self, cmd: str, what: str) -> None:
        """Like :meth:`exec` but for short preamble commands (e.g.
        LoadParameters) that should survive a lingering DDE wedge left over
        from a previous run.  On a failed Exec it reconnects the channel and
        retries (using the same collect_retries / retry_delay_s knobs).  A
        run once hard-crashed on LoadParameters because OMNIC was still wedged
        from the prior aborted run; this recovers that case."""
        attempts = self.collect_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                self.exec(cmd)
                return
            except Exception as e:
                if attempt >= attempts:
                    raise RuntimeError(
                        f"OMNIC did not accept '{what}' after {attempts} "
                        f"attempt(s) ({e}).\n"
                        f"OMNIC may still be wedged from a previous run.  Bring "
                        f"the OMNIC window to the front, dismiss any dialog, and "
                        f"if it persists RESTART OMNIC, then re-run."
                    ) from e
                logging.warning("'%s' failed (%s).  Reconnecting and retrying "
                                "(attempt %d of %d) ...", what, e, attempt, attempts)
                time.sleep(self.retry_delay_s)
                try:
                    self.reconnect()
                except Exception as re_err:
                    logging.warning("DDE reconnect failed: %s", re_err)

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
        self.exec_resilient(f'LoadParameters "{exp_file}"{flags}',
                            "LoadParameters")

    def set_background_aging(self, max_age_min: str) -> None:
        """Force OMNIC to reuse the currently-set background instead of prompting
        to collect a new one because it is "old".

        The lab .exp historically shipped with ``BackgroundHandling = BeforeCol``
        ("collect a background before every measurement"), which makes OMNIC stop
        and ask for confirmation before each DDE ``CollectSample`` once its
        session timer elapses.  That modal blocks the DDE ``Exec`` (pywin32's
        60 s timeout), so the wizard never gets its ack and the run hangs in
        "Still waiting for CollectSample".

        Setting ``BackgroundHandling = AfterTime`` with a very large
        ``MaxBackgroundAge`` (in minutes) tells OMNIC "the current background is
        young enough -- just use it", so no prompt ever appears.  (OMNIC DDE
        manual, Collect group: ``BackgroundHandling`` is one of
        ``BeforeCol | AfterCol | AfterTime | ThisBkg``; ``MaxBackgroundAge`` is an
        integer number of minutes, consulted only in ``AfterTime`` mode.)

        Best-effort and non-fatal: the experiment's *saved* .exp is the primary
        fix; if this build rejects the DDE write we log a warning and rely on the
        .exp.  Must run while no collection is in progress (the manual notes it is
        illegal to set Collect parameters mid-collection), i.e. right after
        :meth:`load_parameters`.
        """
        try:
            self.set_param("Collect", "BackgroundHandling", "AfterTime")
            self.set_param("Collect", "MaxBackgroundAge", str(max_age_min))
        except Exception as e:
            logging.warning(
                "Could not set OMNIC background-aging parameters over DDE (%s).  "
                "Relying on the saved .exp instead -- make sure the experiment "
                "file has 'Background after %s minutes' (AfterTime) selected and "
                "saved.", e, max_age_min)
            return
        # Read back for the audit trail (non-fatal if the build doesn't echo them).
        try:
            handling = self.request("Collect BackgroundHandling")
            age = self.request("Collect MaxBackgroundAge")
            if handling or age:
                logging.info("OMNIC background handling: %s  (MaxBackgroundAge=%s min).",
                             handling or "?", age or "?")
        except Exception as e:
            logging.debug("Could not read back background-aging params: %s", e)

    # The collect_* helpers below use the Polling keyword so the pywin32 DDE
    # Exec call returns immediately and Python (not Windows DDE) controls the
    # wait via _wait_until_idle.  This is the only reliable way to handle
    # collections that take longer than the 60-second DDE transaction window.
    def _collect(self, command: str, menu_item: str, what: str) -> None:
        """Issue a Polling collect Exec and wait for it to finish, recovering
        from a stuck DDE channel.

        With the Polling keyword the Exec normally returns in <2 s.  If OMNIC
        cannot start the collection -- because it is showing a dialog, is
        already mid-scan, or (most often seen on the lab iS5) the spectrometer
        bench has gone offline -- the Exec instead blocks ~60 s and raises
        dde.error.  Rather than killing a multi-hour run on a single hiccup we
        reconnect the DDE channel and retry a few times.  Before re-issuing we
        check MenuStatus on the fresh channel so we never start a second
        collection on top of one that did manage to begin.
        """
        attempts = self.collect_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                self.exec(command)
                self._wait_until_idle(menu_item)
                return
            except TimeoutError:
                # Collection started but never finished within collect_timeout_s.
                # That is a different failure; don't retry-spam it.
                raise
            except Exception as e:
                if attempt >= attempts:
                    raise RuntimeError(
                        f"OMNIC did not accept '{what}' after {attempts} "
                        f"attempt(s) ({e}).\n"
                        f"OMNIC could not START the collection.  The DDE channel "
                        f"itself works (file/display commands succeed) -- this is "
                        f"almost always the iS5 bench being wedged: a dialog is "
                        f"open in OMNIC, a scan is already running, or the bench "
                        f"has dropped offline.\n"
                        f"What to do: bring the OMNIC window to the front, dismiss "
                        f"any dialog, confirm no collection is in progress -- and "
                        f"if that doesn't help, RESTART OMNIC.  The bench can stay "
                        f"wedged across runs until OMNIC is restarted, which is why "
                        f"repeated re-runs keep failing on the first collection."
                    ) from e
                logging.warning(
                    "'%s' did not start (%s).  Reconnecting and retrying "
                    "(attempt %d of %d) ...", what, e, attempt, attempts,
                )
                time.sleep(self.retry_delay_s)
                try:
                    self.reconnect()
                except Exception as re_err:
                    logging.warning("DDE reconnect failed: %s", re_err)
                    continue
                # If a collection actually did start (despite the failed ack),
                # MenuStatus will be Disabled -- wait for it, don't re-issue.
                try:
                    status = self.request(f"MenuStatus {menu_item}").lower()
                except Exception:
                    status = ""
                if "disabled" in status:
                    logging.info("A collection appears to be running already; "
                                 "waiting for it instead of re-issuing.")
                    self._wait_until_idle(menu_item)
                    return

    def collect_background(self, title: str) -> None:
        self._collect(f'CollectBackground "{title}" Auto Polling',
                      "CollectBackground", f"background '{title}'")

    def collect_sample(self, title: str) -> None:
        self._collect(f'CollectSample "{title}" Auto Polling',
                      "CollectSample", f"sample '{title}'")

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
        # Export is a plain Exec, but OMNIC can momentarily wedge right after a
        # collection (a dialog pops, or the bench/DDE channel hiccups), making
        # this Exec block ~60 s and raise dde.error -- which would otherwise lose
        # the spectrum that was just collected.  Losing real data to a transient
        # export failure is worse than for a collection, so reconnect-and-retry
        # here too (same collect_retries / retry_delay_s knobs as the collects).
        self.exec_resilient(f'Export "{path}"', "Export")

    def clear(self) -> None:
        self.exec("DeleteSelectedSpectra")


# ---------------------------------------------------------------------------
# Specac USB controller wrapper
# ---------------------------------------------------------------------------

class Specac:
    """Wrapper around the specac.cmd CLI (controller software v1.0.23.0+)."""

    # Substrings the Specac CLI prints when the controller refuses to do
    # anything.  When any of these are observed in stdout/stderr, the CLI is
    # NOT actually executing commands -- proceeding produces silently bad data
    # (e.g. backgrounds collected at room temperature labelled as if they were
    # at the requested setpoint).  Each maps to a human-actionable hint.
    _FATAL_PATTERNS = [
        ("not running in manual mode",
         "Open the Specac Temperature Controller GUI and switch it to "
         "Manual Mode (not Program Mode).  The CLI only works in Manual "
         "Mode -- in Program Mode it returns this error for every command "
         "without doing anything, which means goto_and_wait returns "
         "instantly while the cell stays at room temperature."),
        ("received invalid value",
         "Specac rejected a numeric argument.  Common cause: a leading "
         "decimal point like '.5' in vt_ir_config.ini ([heater] "
         "tolerance_c).  Use '0.5' instead.  See the most recent Specac "
         "command in the log."),
    ]

    def __init__(self, exe: str, dry_run: bool = False):
        self.exe = exe
        self.dry_run = dry_run
        # Latest stdout-or-stderr; preserved so error messages can include
        # what Specac literally said, in addition to the orchestrator's hint.
        self.last_output: str = ""
        # In dry-run we have no real controller, so remember the last requested
        # setpoint and report it back from get_temp() -- otherwise the preflight
        # "did we reach the setpoint?" probe sees None and aborts, and --dry-run
        # never gets to exercise the OMNIC half of the run.
        self._dry_last_sp: Optional[float] = None

    def _check_fatal(self, blob: str) -> None:
        """Raise RuntimeError if the Specac response contains any pattern
        from ``_FATAL_PATTERNS``.  See class docstring for why we abort
        rather than warn."""
        low = blob.lower()
        for pattern, hint in self._FATAL_PATTERNS:
            if pattern in low:
                raise RuntimeError(
                    f"Specac CLI refused the command.\n"
                    f"Specac said: {blob}\n\n"
                    f"What to do:\n    {hint}"
                )

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
        # Promote stdout/stderr logging to INFO whenever they have content --
        # we need to see what query commands like 'temp?' actually print,
        # since the manual doesn't document the format.  Empty calls (e.g.
        # 'on') stay quiet because the conditional skips them.
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        if out:
            logging.info("Specac stdout: %r", out)
        if err:
            logging.info("Specac stderr: %r", err)
        if r.returncode != 0:
            logging.warning("Specac returned %d.", r.returncode)
        self.last_output = out or err
        # ABORT loudly on the known fatal patterns -- previous behaviour was
        # to log a WARNING and proceed, which produced 12 mislabelled
        # room-temperature backgrounds before anyone noticed.
        self._check_fatal(self.last_output)
        # Many Specac.Cmd queries print to stderr rather than stdout; return
        # whichever is non-empty so the caller can parse it.
        return self.last_output

    @staticmethod
    def _fmt(value) -> str:
        """Normalise a numeric argument for Specac.Cmd.  The CLI rejects
        leading-dot floats like ``.5`` (it expects ``0.5``), so we route
        every numeric input through ``float()`` and then ``:g`` formatting.
        Integers stay compact (``50`` not ``50.0``)."""
        try:
            return f"{float(value):g}"
        except (TypeError, ValueError):
            # If the caller passed a non-numeric string, pass it through and
            # let Specac complain so the user sees what they wrote.
            return str(value)

    # -- commands ----------------------------------------------------------
    def on(self):  self._run("on")
    def off(self): self._run("off")

    def set_units_c(self):           self._run("c")
    def set_tolerance(self, deg):    self._run("tol", self._fmt(deg))
    def set_ramp(self, rate):        self._run("ramp", self._fmt(rate))

    def goto_and_wait(self, T: float, timeout: float = 3600.0) -> None:
        """Set setpoint and block until reached (within tolerance)."""
        self._dry_last_sp = float(T)
        self._run("sp", self._fmt(T), "w", timeout=timeout)

    # Pattern matches the first signed decimal number in a string, with
    # either '.' or ',' as the decimal separator -- the Specac CLI on the
    # lab PC reports temperatures using a European locale (e.g. '31,37').
    _NUM_RE = re.compile(r"[-+]?\d+(?:[.,]\d+)?")

    def get_temp(self) -> Optional[float]:
        if self.dry_run:
            # No real controller in dry-run: report the last requested setpoint
            # (or a plausible room temperature before the first ramp) so the
            # preflight sanity checks pass and the whole flow is exercised.
            return self._dry_last_sp if self._dry_last_sp is not None else 25.0
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


def bg_age_days(path: Path) -> float:
    """Age of a background file in days, from its last-modified time."""
    try:
        return max(0.0, (time.time() - path.stat().st_mtime) / 86400.0)
    except OSError:
        return 0.0


def survey_sample_bgs(bg_dir: Path, sample: str) -> List[Tuple[int, Path]]:
    """Every background for this sample as (temperature_C, path), one entry per
    temperature (lowest-index file kept if duplicates), sorted by temperature.
    Matches both indexed (NN_BG_...) and unindexed names."""
    found: List[Tuple[int, Path]] = []
    if bg_dir.exists():
        for f in bg_dir.glob(f"*BG_{sample}_*C.SPA"):
            m = _BG_FILE_RE.match(f.name)
            if m and m.group("sample") == sample:
                try:
                    found.append((int(m.group("T")), f))
                except ValueError:
                    pass
    found.sort(key=lambda tp: (tp[0], tp[1].name))
    seen, uniq = set(), []
    for t, p in found:
        if t not in seen:
            seen.add(t)
            uniq.append((t, p))
    return uniq


def resolve_bg_plan(sample_temps: List[int],
                    available: List[Tuple[int, Path]],
                    mode: str,
                    fixed_temp: Optional[int] = None
                    ) -> Dict[int, Optional[Tuple[int, Path]]]:
    """Decide which background each sample temperature will use.

    Returns ``{T: (bg_temp, bg_path)}``.  In ``exact`` mode a temperature with
    no exact-match BG maps to ``None`` (caller treats it as missing).
    ``closest`` and ``fixed`` always resolve as long as ``available`` is
    non-empty.  ``closest`` breaks ties toward the lower temperature."""
    by_temp = {t: p for t, p in available}
    avail_temps = [t for t, _ in available]
    plan: Dict[int, Optional[Tuple[int, Path]]] = {}
    for T in sample_temps:
        if mode == "exact":
            plan[T] = (T, by_temp[T]) if T in by_temp else None
        elif mode == "fixed":
            plan[T] = (fixed_temp, by_temp[fixed_temp]) if fixed_temp in by_temp else None
        else:  # closest
            bt = min(avail_temps, key=lambda a: (abs(a - T), a)) if avail_temps else None
            plan[T] = (bt, by_temp[bt]) if bt is not None else None
    return plan


# SCHEDULE_KINDS (per-direction glyph/arrow/color/label) is the single source of
# truth shared with the live plots; it lives in vtir_wizard/__init__.py and is
# imported at the top of this module so the plot subprocesses can pull it in
# without importing the orchestrator's pywin32 dependency.


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
    omnic: OmnicDDE, sample: str, T: int, out_dir: Path, bg: Path,
    suffix: str = "", extra_formats: Optional[List[str]] = None,
    prefix: str = "",
) -> Tuple[Path, Path]:
    """Per-T sample step.  ``bg`` is the pre-resolved background file to use
    (chosen in the preflight according to the matching mode).

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
    # 1) Bind the chosen BG file as the current background.
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


def _spawn_console(cmd: List[str], what: str, sample: str) -> Optional[subprocess.Popen]:
    """Popen ``cmd`` in its own console window (Windows) so the child's prints
    don't tangle with the orchestrator's prompts.  Returns the handle, or None on
    failure.  The orchestrator never terminates these -- the user closes the plot
    windows when they're done with them."""
    kwargs = {}
    if sys.platform == "win32":
        # CREATE_NEW_CONSOLE = 0x00000010
        kwargs["creationflags"] = 0x00000010
    try:
        proc = subprocess.Popen(cmd, **kwargs)
        logging.info("Spawned %s  (pid=%d, sample=%s).", what, proc.pid, sample)
        return proc
    except Exception as e:
        logging.warning("Could not spawn %s: %s", what, e)
        return None


def spawn_live_plot(sample: str,
                    config_path: Path,
                    live_cfg,
                    dry_run: bool,
                    since_iso: str,
                    mode_label: str,
                    done_file: Path,
                    plot_dir: Path,
                    save_delay_s: str) -> Optional[subprocess.Popen]:
    """Spawn the temperature/setpoint overlay (``vtir_wizard.temp_plot``) in a
    fresh console window, locked onto this run's sample folder.

    The extra arguments let the plotter (a) clip its view to just this run
    (``--since``), (b) auto-save an SVG once the run finishes -- detected via the
    ``--done-file`` the orchestrator writes in its finally block -- and name it by
    mode (``--label``)."""
    if dry_run:
        logging.info("[dry-run] would spawn temperature overlay plot for sample %r.", sample)
        return None
    cmd = [
        sys.executable, "-m", "vtir_wizard.temp_plot",
        "--config", str(config_path),
        "--sample", sample,
        "--interval", str(live_cfg.get("interval_s", "5")),
        "--scan-duration", str(live_cfg.get("scan_duration_s", "210")),
        "--since", since_iso,
        "--label", mode_label,
        "--done-file", str(done_file),
        "--plot-dir", str(plot_dir),
        "--save-delay", str(save_delay_s),
    ]
    return _spawn_console(cmd, "temperature overlay plot", sample)


def spawn_ir_plot(sample: str,
                  config_path: Path,
                  ir_cfg,
                  dry_run: bool,
                  since_iso: str,
                  mode_label: str,
                  done_file: Path,
                  plot_dir: Path) -> Optional[subprocess.Popen]:
    """Spawn the live stacked-IR-spectrum window (``vtir_wizard.ir_plot``) in a
    fresh console window, locked onto this run's sample folder.  It re-reads the
    session's ``.SPA`` files as they land, stacks them by temperature, and
    overwrites a single SVG in ``plot_dir`` so only the final stacked spectrum
    persists (parallel to the temperature overlay's snapshot)."""
    if dry_run:
        logging.info("[dry-run] would spawn IR stack plot for sample %r.", sample)
        return None
    cmd = [
        sys.executable, "-m", "vtir_wizard.ir_plot",
        "--config", str(config_path),
        "--sample", sample,
        "--interval", str(ir_cfg.get("interval_s", "5")),
        "--mode", str(ir_cfg.get("mode", "stack")),
        "--unit", str(ir_cfg.get("unit", "A")),
        "--since", since_iso,
        "--label", mode_label,
        "--done-file", str(done_file),
        "--plot-dir", str(plot_dir),
    ]
    offset = str(ir_cfg.get("offset", "")).strip()
    if offset:
        cmd += ["--offset", offset]
    return _spawn_console(cmd, "IR stack plot", sample)


def build_session_dir(out_root: Path, sample: str) -> Path:
    """One folder per sample.  Re-using the same name across BG and sample
    runs is required for filename-based pairing."""
    d = out_root / sample
    d.mkdir(parents=True, exist_ok=True)
    return d


def configure_logging(log_dir: Path, sample: str, mode_label: str,
                      stamp: str) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{stamp}_{sample}_{mode_label}.log"
    handlers = [logging.FileHandler(log_file, encoding="utf-8"),
                logging.StreamHandler(sys.stdout)]
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt=LOG_DATEFMT,
                        handlers=handlers, force=True)
    logging.info("Log file: %s", log_file)
    return log_file


def banner() -> None:
    print("=" * 66)
    print(f"  VT-IR Orchestrator  v{__version__}   --   iS5  +  Specac heated Golden Gate")
    print("=" * 66)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="vtir-wizard",
        description="VT-IR measurement orchestrator (iS5 + Specac heated Golden Gate).",
    )
    p.add_argument("--config", default=None,
                   help="Path to vt_ir_config.ini.  Default search order: this "
                        "flag, then ./vt_ir_config.ini, then the per-user copy "
                        "under %%APPDATA%%/vtir-wizard.")
    p.add_argument("--init-config", action="store_true",
                   help="Write a template vt_ir_config.ini to the per-user config "
                        "folder (%%APPDATA%%/vtir-wizard) and exit.")
    p.add_argument("--force", action="store_true",
                   help="With --init-config, overwrite an existing per-user config.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print every DDE / Specac call without executing them.")
    p.add_argument("--no-live-plot", action="store_true",
                   help="Skip auto-launching BOTH live plot windows (temperature "
                        "overlay and IR stack), regardless of config.")
    p.add_argument("--no-ir-plot", action="store_true",
                   help="Skip only the live IR stack window (keep the temperature "
                        "overlay).")
    p.add_argument("--version", action="version",
                   version=f"vtir-wizard {__version__}")
    args = p.parse_args(argv)

    # --init-config short-circuits before any config is required.
    if args.init_config:
        dest, written = appconfig.init_user_config(force=args.force)
        if written:
            print(f"Wrote config template to:\n    {dest}\n"
                  "Edit it (paths, defaults) and re-run  vtir-wizard.")
        else:
            print(f"Config already exists:\n    {dest}\n"
                  "Edit it, or pass --force to overwrite with a fresh template.")
        return 0

    cfg_path = appconfig.resolve_config_path(args.config)
    if cfg_path is None:
        raise SystemExit(appconfig.missing_config_message())
    cfg = appconfig.load_config(cfg_path)
    paths = cfg["paths"]
    defaults = cfg["defaults"]
    heater = cfg["heater"]
    # [omnic], [live_plot], [export], [backgrounds] sections are optional.
    omnic_cfg = cfg["omnic"] if cfg.has_section("omnic") else {}
    live_cfg = cfg["live_plot"] if cfg.has_section("live_plot") else {}
    ir_cfg = cfg["ir_plot"] if cfg.has_section("ir_plot") else {}
    export_cfg = cfg["export"] if cfg.has_section("export") else {}
    bg_cfg = cfg["backgrounds"] if cfg.has_section("backgrounds") else {}
    bg_max_age_days = float(bg_cfg.get("max_age_warn_days", "1"))
    # Minutes written to OMNIC's Collect/MaxBackgroundAge so the "background is
    # old -- collect a new one?" prompt never fires (see omnic.set_background_aging).
    bg_max_age_min = str(bg_cfg.get("max_bg_age_min", "999999")).strip()
    bg_default_mode = str(bg_cfg.get("match_mode", "exact")).strip().lower()
    if bg_default_mode not in ("exact", "closest", "fixed"):
        bg_default_mode = "exact"

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
    bg_mode = "exact"
    bg_fixed_temp: Optional[int] = None
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

        # Background temperature matching.  Exact-temperature backgrounds are
        # not strictly required (the cell sits in a controlled glovebox), so
        # the user can fall back to the nearest available BG, or pin one BG
        # for the whole run.  The actual plan + any age/mismatch warnings are
        # logged in the preflight below.
        print("\nBackground temperature matching:")
        print("  exact   - use the BG collected at each sample temperature")
        print("  closest - use the nearest-temperature BG available for each step")
        print("  fixed   - use ONE background (you pick which) for every step")
        bg_mode = ask_choice("Choose matching mode",
                             ["exact", "closest", "fixed"], default=bg_default_mode)
        if bg_mode == "fixed":
            avail = survey_sample_bgs(
                build_session_dir(
                    Path(paths.get("bg_root", paths["output_root"])), sample),
                sample)
            if avail:
                avail_temps = [t for t, _ in avail]
                opts = ", ".join(str(t) for t in avail_temps)
                while True:
                    raw = ask(f"Which BG temperature to use for ALL steps? "
                              f"Available: {opts}", str(avail_temps[0]))
                    try:
                        v = int(raw)
                    except ValueError:
                        v = None
                    if v in avail_temps:
                        bg_fixed_temp = v
                        break
                    print(f"  (choose one of: {opts})")

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
    # Plot snapshots go to a parallel "plots" folder; default beside output_root.
    plot_dir_cfg = paths.get("plot_dir", "").strip()
    plot_dir = Path(plot_dir_cfg) if plot_dir_cfg else out_root.parent / "plots"
    # One run stamp drives both the log filename and the per-run done-file the
    # live plotter watches for.
    run_stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = configure_logging(log_dir, sample, mode_label, run_stamp)

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
    # bg_for_T maps each unique sample temperature to the BG file the run will
    # use.  Built here (sample mode only) so the plan -- and every age/mismatch
    # warning -- is logged once, up front, before the user confirms.
    bg_for_T: Dict[int, Path] = {}
    if mode == "1":
        logging.info("Background folder:     %s", bg_dir)
        logging.info("Background matching:   %s%s", bg_mode,
                     f" (fixed at {bg_fixed_temp} C)" if bg_mode == "fixed" else "")
        available = survey_sample_bgs(bg_dir, sample)

        # No backgrounds at all for this sample -> the helpful "where are the
        # BGs" guidance (unchanged), since no matching mode can rescue that.
        if not available:
            msg = [
                f"No background files found for sample {sample!r}.",
                f"Looked in:  {bg_dir}",
            ]
            others = survey_bg_folders(bg_root)
            others.pop(sample, None)
            if others:
                msg.append("")
                msg.append("Sample folders that DO have BG files:")
                for name, temps in others.items():
                    msg.append(f"  - {name}   ({', '.join(str(t) for t in temps)} C)")
                msg.append("")
                msg.append(f"Either rerun BG mode first under name {sample!r}, "
                           f"or restart and pick one of the names above.")
            else:
                msg.append("")
                msg.append("No other sample folders have BG files either -- "
                           "you need to run BG mode first.")
            sys.exit("\n".join(msg))

        plan = resolve_bg_plan(Ts, available, bg_mode, fixed_temp=bg_fixed_temp)

        # In exact mode a missing temperature is fatal -- but now we point the
        # user at the closest/fixed modes as a way to proceed anyway.
        missing = [T for T in Ts if plan.get(T) is None]
        if missing:
            have = ", ".join(f"{t} C" for t, _ in available)
            sys.exit(
                f"Missing exact-temperature backgrounds for sample "
                f"{sample!r}: {', '.join(f'{T} C' for T in missing)}.\n"
                f"Available BG temperatures: {have}.\n"
                f"Either run BG mode for the missing temperatures, or re-run "
                f"and choose the 'closest' or 'fixed' matching mode to proceed "
                f"with the backgrounds you already have."
            )

        # Log the full plan + warnings.  This is the audit trail: anyone asking
        # later how a given measurement's background was chosen finds it here.
        logging.info("Background plan (sample T -> BG used):")
        for T in Ts:
            bt, bpath = plan[T]
            age = bg_age_days(bpath)
            exact = (bt == T)
            logging.info("  %4d C  ->  %d C BG   %-28s  (%.1f days old)%s",
                         T, bt, bpath.name, age,
                         "" if exact else "   [TEMPERATURE MISMATCH]")
            if not exact:
                logging.warning(
                    "Background temperature mismatch at %d C: using a %d C "
                    "background (off by %d C).", T, bt, abs(bt - T))
            if age > bg_max_age_days:
                logging.warning(
                    "Background for %d C is %.1f days old (> %.1f day threshold): %s",
                    T, age, bg_max_age_days, bpath.name)
            bg_for_T[T] = bpath
    logging.info("Log file:              %s", log_file)
    logging.info("Specac exe:            %s", paths["specac_exe"])
    print()

    if ask_choice("Ready? (y = start, n = abort)", ["y", "n"], "y") != "y":
        logging.info("Aborted by user.")
        return 1

    # -- spawn live plot windows (each in its own console) ----------------
    # Done AFTER the Ready prompt so windows don't pop up while the user is still
    # typing answers, and locked onto the just-confirmed sample.  Two windows:
    # the temperature/setpoint overlay and the stacked-IR-spectrum plot.  Both
    # watch the same per-run ``done_file`` sentinel (written in the finally block)
    # to know when the run is over.  done_file stays None unless at least one
    # window is launched.
    done_file: Optional[Path] = None
    since_iso = dt.datetime.now().isoformat(timespec="seconds")

    def _flag_on(section, default: str = "true") -> bool:
        return str(section.get("enabled", default)).strip().lower() in (
            "1", "true", "yes", "y", "on"
        )

    live_enabled = _flag_on(live_cfg)
    ir_enabled = _flag_on(ir_cfg)
    want_temp = (not args.no_live_plot) and live_enabled
    want_ir = (not args.no_live_plot) and (not args.no_ir_plot) and ir_enabled

    if args.no_live_plot:
        logging.info("Live plot windows disabled by --no-live-plot.")
    else:
        if not live_enabled:
            logging.info("Temperature overlay plot disabled in config ([live_plot] enabled).")
        if args.no_ir_plot:
            logging.info("IR stack plot disabled by --no-ir-plot.")
        elif not ir_enabled:
            logging.info("IR stack plot disabled in config ([ir_plot] enabled).")

    if want_temp or want_ir:
        done_file = session_dir / f"{run_stamp}.done"
        # Remove any stale sentinel (shouldn't exist for a fresh stamp, but be safe).
        try:
            done_file.unlink()
        except FileNotFoundError:
            pass
    if want_temp:
        spawn_live_plot(
            sample, cfg_path, live_cfg, args.dry_run,
            since_iso=since_iso,
            mode_label=mode_label,
            done_file=done_file,
            plot_dir=plot_dir,
            save_delay_s=live_cfg.get("save_delay_s", "600"),
        )
    if want_ir:
        spawn_ir_plot(
            sample, cfg_path, ir_cfg, args.dry_run,
            since_iso=since_iso,
            mode_label=mode_label,
            done_file=done_file,
            plot_dir=plot_dir,
        )

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

        # Pre-flight: confirm the controller actually responds to a 'temp?'
        # query.  Catches Manual-Mode-not-set and other GUI-state issues
        # before we waste a session collecting backgrounds at the wrong
        # temperature.  _run() already raises on the known fatal error
        # strings, so reaching here with start_T None means the controller
        # returned something we couldn't parse -- still cause for abort.
        start_T = specac.get_temp()
        if start_T is None:
            raise RuntimeError(
                "Specac CLI is reachable but 'temp?' did not return a "
                "parseable number.\n"
                f"Last Specac output: {specac.last_output!r}\n"
                "Open the controller GUI, verify Manual Mode is active, "
                "and try again."
            )
        logging.info("Cell temperature before ramping: %.1f C.", start_T)

        # OMNIC preamble  ----------------------------------------------
        with OmnicDDE(
            dry_run=args.dry_run,
            poll_interval_s=float(omnic_cfg.get("poll_interval_s", "2")),
            collect_timeout_s=float(omnic_cfg.get("collect_timeout_s", "1800")),
            collect_retries=int(omnic_cfg.get("collect_retries", "2")),
            retry_delay_s=float(omnic_cfg.get("retry_delay_s", "5")),
        ) as omnic:
            omnic.load_parameters(exp_file, keep_bkg=True)
            # Defeat OMNIC's "background is old -- collect a new one?" prompt by
            # forcing the reuse-current-background mode with a very large max age.
            # Done here (after load, before any collection) per the DDE manual.
            omnic.set_background_aging(bg_max_age_min)

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
                ramp_timeout = float(heater.get("ramp_timeout_s", "3600"))
                try:
                    specac.goto_and_wait(T, timeout=ramp_timeout)
                except subprocess.TimeoutExpired:
                    # The cell never reached the setpoint within ramp_timeout_s.
                    # On the cool-down tail this is expected: passive cooling is
                    # slow near the (glovebox) ambient and may never reach a low
                    # setpoint within tolerance.  Skip this step's collection
                    # rather than crashing the whole run -- every spectrum already
                    # collected is kept, and the heater is still switched off
                    # cleanly in the finally block.
                    logging.warning(
                        "Setpoint wait for %d C (%s) timed out after %.0f s.  The "
                        "cell could not reach %d C within +/-%s C in time -- on a "
                        "cool-down this usually means the target is too close to "
                        "the glovebox ambient for passive cooling.  Skipping this "
                        "step and continuing.  To capture it: raise [heater] "
                        "ramp_timeout_s, widen [heater] tolerance_c, or keep the "
                        "cool-down's lowest temperature higher.",
                        T, direction, ramp_timeout, T,
                        heater.get("tolerance_c", "1"),
                    )
                    continue

                # Sanity check: Specac.Cmd has been observed returning from
                # "sp T w" while the cell was still far from T (stuck "Wait"
                # state, or controller GUI not in Manual Mode).  Query temp?
                # and abort before OMNIC collects at the wrong temperature.
                # If temp? can't be read at all, abort too -- "proceeding
                # without sanity check" once produced a full session of 12
                # backgrounds collected at room temperature.
                actual_T = specac.get_temp()
                if actual_T is None:
                    raise RuntimeError(
                        f"Could not read cell temperature via 'temp?' after "
                        f"the setpoint wait for {T} C.\n"
                        f"Last Specac output: {specac.last_output!r}\n"
                        f"The controller may have lost Manual Mode mid-run "
                        f"or changed its output format.  Aborting before "
                        f"OMNIC collects at an unknown temperature."
                    )
                delta = abs(actual_T - T)
                logging.info("Cell temperature: %.1f C (target %d C, delta %.1f C).",
                             actual_T, T, delta)
                if delta > sp_check_margin:
                    raise RuntimeError(
                        f"Specac reported 'setpoint reached' but cell is at "
                        f"{actual_T:.1f} C (target {T} C, allowed delta "
                        f"{sp_check_margin:.1f} C).  This is the false-wait "
                        f"bug -- usually fixed by closing and reopening the "
                        f"Specac controller GUI to clear any stuck program "
                        f"state.  Aborting before OMNIC starts a collection "
                        f"at the wrong temperature."
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
                    out, used_bg = step_sample(omnic, sample, T, session_dir,
                                               bg_for_T[T], suffix=suffix,
                                               extra_formats=extra_formats,
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
        # Signal the live plotter that this run is over, so it can capture the
        # cool-down tail and auto-save its SVG.  Written on every exit path
        # (normal finish OR abort) so a snapshot is always taken.
        if done_file is not None and not args.dry_run:
            try:
                done_file.write_text(
                    dt.datetime.now().isoformat(timespec="seconds"),
                    encoding="utf-8",
                )
            except Exception as e:
                logging.warning("Could not write run-complete sentinel: %s", e)

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
