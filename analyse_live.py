#!/usr/bin/env python3
"""
analyse_live.py  --  Live-updating overlay plot for a VT-IR session.

While vt_ir.py is running on the lab PC, launch this script in a separate
console.  It opens a matplotlib window that re-reads the Specac controller's
.log file and the .SPA files in the current session folder every few seconds
and re-draws the temperature/setpoint trace with scan-event overlays.

The data-loading logic is the same as the stand-alone analyse.py reference --
only difference is that here it's wrapped in a polling loop and the paths can
be auto-detected (or overridden via CLI).

Usage:
    python analyse_live.py                       # auto-detect newest session + log
    python analyse_live.py --sample MOF42_run3   # follow a specific session
    python analyse_live.py --interval 10         # refresh every 10 s
    python analyse_live.py --once                # render once and exit

Close the matplotlib window to stop the loop.
"""
from __future__ import annotations

import argparse
import configparser
import os
import re
import sys
import time
from datetime import datetime, timedelta
from glob import glob
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import matplotlib

import matplotlib.pyplot as plt
from matplotlib.dates import DateFormatter

# Single source of truth for per-direction shading colors + legend labels.
# Lives in vt_ir.py because it's also used by the orchestrator's wizard
# summary and per-step log line -- importing here keeps the producer and
# consumer in sync, so renaming a suffix or adding a kind only requires
# editing one table.
from vt_ir import SCHEDULE_KINDS


CONFIG_FILE = Path(__file__).with_name("vt_ir_config.ini")

# Default Specac controller log folder; overridable via config + CLI.
DEFAULT_SPECAC_LOGS = r"C:\Users\<you>\Documents\Specac Temperature Controller\Logs"


# ---------------------------------------------------------------------------
# Parsing -- same shape as analyse.py, but tolerant of an in-progress file.
# ---------------------------------------------------------------------------

def get_data(path: str) -> Dict[str, list]:
    """Read a Specac controller .log file.  Returns parallel columns keyed by
    header name.  Robust against an incomplete trailing row (the file may be
    actively being written)."""
    with open(path, "r", encoding="utf-8", errors="replace") as inf:
        lines = inf.read().strip().split("\n")
    if len(lines) < 3:
        # Header-only file, no data yet.
        return {"Time (s)": [], "Setpoint (C)": [], "Temperature (C)": [], "Events": []}
    headers = [c.strip() for c in lines[1].split(",") if c.strip()]
    rows = []
    for raw in lines[2:]:
        cells = [c.strip() for c in raw.split(",")][: len(headers)]
        if len(cells) < len(headers):
            cells += [""] * (len(headers) - len(cells))
        cells = ["0" if c == "" and h != "Events" else c
                 for c, h in zip(cells, headers)]
        rows.append(cells)
    dtypes = {"Time (s)": float, "Setpoint (C)": float,
              "Temperature (C)": float, "Events": str}
    data: Dict[str, list] = {h: [] for h in headers}
    for r in rows:
        try:
            for i, h in enumerate(headers):
                data[h].append(dtypes.get(h, str)(r[i]))
        except (ValueError, IndexError):
            # skip a partial last row
            continue
    return data


def find_events(data: Dict[str, list], prefix: str) -> List[int]:
    return [i for i, v in enumerate(data.get("Events", []))
            if isinstance(v, str) and v.startswith(prefix)]


def get_file_creation_date(path: str) -> datetime:
    return datetime.fromtimestamp(os.path.getctime(path))


_BG_NAME_RE = re.compile(r"^(?:\d+_)?BG_", re.IGNORECASE)


def classify_spa(name: str) -> str:
    """Map a .SPA filename to a key of SCHEDULE_KINDS based on naming
    convention emitted by vt_ir.py: ``[NN_]BG_<sample>_<T>C.SPA`` for
    backgrounds, ``[NN_]<sample>_<T>C[_up|_down|_return].SPA`` for samples.
    The leading ``NN_`` chronological index is optional -- matches both
    indexed (current) and unindexed (pre-index-feature) files."""
    if _BG_NAME_RE.match(name):
        return "bg"
    if "_return" in name:
        return "return"
    if "_down" in name:
        return "down"
    return "up"


# ---------------------------------------------------------------------------
# Auto-detection helpers
# ---------------------------------------------------------------------------

def newest(paths: List[Path]) -> Optional[Path]:
    paths = [p for p in paths if p.exists()]
    if not paths:
        return None
    return max(paths, key=lambda p: p.stat().st_mtime)


def find_specac_log(folder: Path) -> Optional[Path]:
    candidates = list(folder.glob("*.log"))
    return newest(candidates)


def find_session_dir(output_root: Path, sample: Optional[str]) -> Optional[Path]:
    if sample:
        d = output_root / sample
        return d if d.exists() else None
    # Newest sub-folder under output_root.
    subdirs = [p for p in output_root.iterdir() if p.is_dir()] if output_root.exists() else []
    return newest(subdirs)


def load_config(path: Path) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    if path.exists():
        cfg.read(path, encoding="utf-8")
    return cfg


# ---------------------------------------------------------------------------
# Plot
# ---------------------------------------------------------------------------

def render(ax, specac_log: Optional[Path], session_dir: Optional[Path],
           scan_duration_s: float) -> Tuple[str, int, int]:
    """(Re-)draw the overlay plot.  Returns a status string + counts for logging."""
    ax.clear()

    # --- temperature trace ---
    n_pts = 0
    log_start: Optional[datetime] = None
    if specac_log and specac_log.exists():
        try:
            data = get_data(str(specac_log))
        except Exception as e:
            data = {}
            ax.text(0.5, 0.95, f"Specac log parse error: {e}",
                    ha="center", va="top", transform=ax.transAxes, color="red")
        if data.get("Time (s)"):
            log_start = get_file_creation_date(str(specac_log))
            xs = [log_start + timedelta(seconds=s) for s in data["Time (s)"]]
            y_T = data.get("Temperature (C)", [])
            y_sp = data.get("Setpoint (C)", [])
            ax.plot(xs, y_T, "r-", lw=0.8, label="Temperature")
            ax.plot(xs, y_sp, "g-", lw=0.8, label="Setpoint")
            n_pts = len(xs)

            # Wait Started / Wait Completed event lines
            for prefix, color, label in [
                ("Wait Started",   "orange", "Wait Started"),
                ("Wait Completed", "purple", "Wait Completed"),
            ]:
                inds = find_events(data, prefix)
                for i, idx in enumerate(inds):
                    if 0 <= idx < len(xs):
                        ax.axvline(xs[idx], lw=0.6, color=color, ls="dashed",
                                   label=label if i == 0 else None)

    # --- scan markers from .SPA files ---
    n_spa = 0
    if session_dir and session_dir.exists():
        # On Windows (the documented target) pathlib globs are case-insensitive,
        # so a single "*.SPA" pattern already matches .SPA, .spa, .Spa, etc.
        spa_paths = sorted(session_dir.glob("*.SPA"),
                           key=lambda p: p.stat().st_mtime)
        labelled = set()
        for path in spa_paths:
            kind = classify_spa(path.name)
            end_time = get_file_creation_date(str(path))
            start_time = end_time - timedelta(seconds=scan_duration_s)
            lab = SCHEDULE_KINDS[kind]["label"] if kind not in labelled else None
            labelled.add(kind)
            ax.axvspan(start_time, end_time, color=SCHEDULE_KINDS[kind]["color"],
                       zorder=-1, label=lab)
            n_spa += 1

    # --- formatting ---
    ax.set_xlabel("Time")
    ax.set_ylabel(r"T  /  $^\circ$C")
    ax.xaxis.set_major_formatter(DateFormatter("%H:%M:%S"))
    plt.setp(ax.get_xticklabels(), rotation=30, ha="right")
    ax.grid(lw=0.4)
    ax.margins(x=0.01)

    title = "VT-IR live overlay"
    if session_dir:
        title += f"   --   {session_dir.name}"
    if log_start:
        title += f"   (log start: {log_start:%Y-%m-%d %H:%M})"
    ax.set_title(title, fontsize=10)

    handles, labels = ax.get_legend_handles_labels()
    if handles:
        ax.legend(handles, labels, loc="upper left", fontsize="small", ncol=2)
    if not (n_pts or n_spa):
        ax.text(0.5, 0.5,
                "Waiting for data ...\n"
                "(no Specac log entries and no .SPA files yet)",
                ha="center", va="center", transform=ax.transAxes,
                fontsize=11, color="grey")

    return ("ok", n_pts, n_spa)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--config", default=str(CONFIG_FILE),
                   help="Path to vt_ir_config.ini (default: next to this script).")
    p.add_argument("--sample", default=None,
                   help="Sample name to follow (default: newest folder under output_root).")
    p.add_argument("--specac-logs", default=None,
                   help="Override Specac controller log folder.")
    p.add_argument("--scan-duration", type=float, default=210.0,
                   help="Estimated scan duration in seconds (used to draw the "
                        "shaded scan band; default 210 s = 128 scans @ 2 cm-1).")
    p.add_argument("--interval", type=float, default=5.0,
                   help="Refresh interval in seconds (default 5).")
    p.add_argument("--once", action="store_true",
                   help="Render the plot once and exit.")
    args = p.parse_args(argv)

    cfg = load_config(Path(args.config))
    paths = cfg["paths"] if cfg.has_section("paths") else {}

    output_root = Path(paths.get("output_root", "."))
    specac_logs = Path(
        args.specac_logs
        or paths.get("specac_log_dir")
        or DEFAULT_SPECAC_LOGS
    )

    fig, ax = plt.subplots(figsize=(12, 5), layout="constrained")
    if not args.once:
        fig.canvas.manager.set_window_title("VT-IR live")
        # Make sure plt.pause() actually yields the GUI loop.
        try:
            matplotlib.use(matplotlib.get_backend())
        except Exception:
            pass

    last_summary = ""
    try:
        while True:
            specac_log = find_specac_log(specac_logs)
            session_dir = find_session_dir(output_root, args.sample)

            status, n_pts, n_spa = render(
                ax, specac_log, session_dir, args.scan_duration,
            )
            summary = (
                f"specac_log={specac_log.name if specac_log else '<none>'}  "
                f"session={session_dir.name if session_dir else '<none>'}  "
                f"pts={n_pts}  scans={n_spa}"
            )
            if summary != last_summary:
                print(f"[{datetime.now():%H:%M:%S}] {summary}")
                last_summary = summary

            if args.once:
                plt.show()
                return 0

            # plt.pause yields to the GUI event loop AND sleeps -- exactly what
            # we want for a low-effort live update.  If the user has closed
            # the window, plt.pause raises -> we exit cleanly.
            try:
                plt.pause(max(0.1, args.interval))
            except Exception:
                return 0
            if not plt.fignum_exists(fig.number):
                return 0
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
