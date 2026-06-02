#!/usr/bin/env python3
"""
analyse_live.py  --  Live-updating overlay plot for a VT-IR session.

While vt_ir.py is running, this opens a matplotlib window that re-reads the
Specac controller's .log file and the .SPA files in the current session folder
every few seconds and re-draws the temperature/setpoint trace with scan-event
overlays.

vt_ir.py launches this automatically (one window per run).  You can also run it
standalone to re-examine a finished run.

Key behaviours:

  * --since <ISO>   Clip the view to a single run's time window.  vt_ir.py
                    passes its run-start time here so the background pass and
                    the sample pass don't pile into one cramped plot.
  * Zoom is preserved across refreshes.  Once you zoom or pan, live updates
    stop rescaling the axes; press 'f' in the window to resume auto-follow.
  * SVG auto-save.  When vt_ir.py signals the run is finished (via --done-file),
    the plot saves itself <save-delay> seconds later (capturing the cool-down
    tail).  It also saves on manual window close.  Files land in
    <plot-dir>/<sample>/<sample>_<LABEL>_<timestamp>.svg.

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
# Parsing -- tolerant of an in-progress (actively-written) log file.
# ---------------------------------------------------------------------------

def get_data(path: str) -> Dict[str, list]:
    """Read a Specac controller .log file.  Returns parallel columns keyed by
    header name.  Robust against an incomplete trailing row."""
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
    return newest(list(folder.glob("*.log")))


def find_session_dir(output_root: Path, sample: Optional[str]) -> Optional[Path]:
    if sample:
        d = output_root / sample
        return d if d.exists() else None
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
           scan_duration_s: float,
           since: Optional[datetime] = None) -> Tuple[str, int, int]:
    """(Re-)draw the overlay plot on ``ax`` at full extent (autoscaling).

    ``since`` clips the Specac trace and .SPA markers to a single run's
    time window.  Returns a status string + (#trace points, #scan markers)."""
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
            xs_all = [log_start + timedelta(seconds=s) for s in data["Time (s)"]]
            y_T_all = data.get("Temperature (C)", [])
            y_sp_all = data.get("Setpoint (C)", [])

            # Clip to the current run's window.  keep[i] gates the trace AND
            # the event markers below so an old run's data can't drag the view.
            keep = [since is None or x >= since for x in xs_all]
            xs = [x for x, k in zip(xs_all, keep) if k]
            y_T = [v for v, k in zip(y_T_all, keep) if k]
            y_sp = [v for v, k in zip(y_sp_all, keep) if k]

            ax.plot(xs, y_T, "r-", lw=0.8, label="Temperature")
            ax.plot(xs, y_sp, "g-", lw=0.8, label="Setpoint")
            n_pts = len(xs)

            # Wait Started / Wait Completed event lines (also clipped).
            for prefix, color, label in [
                ("Wait Started",   "orange", "Wait Started"),
                ("Wait Completed", "purple", "Wait Completed"),
            ]:
                drawn = False
                for idx in find_events(data, prefix):
                    if 0 <= idx < len(xs_all) and keep[idx]:
                        ax.axvline(xs_all[idx], lw=0.6, color=color, ls="dashed",
                                   label=label if not drawn else None)
                        drawn = True

    # --- scan markers from .SPA files ---
    n_spa = 0
    if session_dir and session_dir.exists():
        # On Windows (the documented target) pathlib globs are case-insensitive,
        # so a single "*.SPA" pattern already matches .SPA, .spa, etc.
        spa_paths = sorted(session_dir.glob("*.SPA"),
                           key=lambda p: p.stat().st_mtime)
        labelled = set()
        for path in spa_paths:
            end_time = get_file_creation_date(str(path))
            # Skip files from earlier runs (e.g. BG files during a sample run).
            if since is not None and end_time < since:
                continue
            kind = classify_spa(path.name)
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


def save_snapshot(plot_dir: Path, sample: str, label: str,
                  specac_log: Optional[Path], session_dir: Optional[Path],
                  scan_duration_s: float,
                  since: Optional[datetime]) -> Optional[Path]:
    """Render the overlay onto a FRESH figure (so it is always full extent,
    independent of any zoom in the interactive window) and save it as SVG to
    ``<plot_dir>/<sample>/<sample>_<label>_<timestamp>.svg``.  Returns the
    path written, or None if there was nothing to plot."""
    out_dir = Path(plot_dir) / sample
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = out_dir / f"{sample}_{label}_{stamp}.svg"

    fig2, ax2 = plt.subplots(figsize=(12, 5), layout="constrained")
    try:
        _, n_pts, n_spa = render(ax2, specac_log, session_dir,
                                 scan_duration_s, since)
        if not (n_pts or n_spa):
            return None
        fig2.savefig(out)  # SVG inferred from the .svg extension
    finally:
        plt.close(fig2)
    return out


def _read_finish_time(done_file: Path) -> float:
    """Epoch seconds the run finished.  Prefers the ISO timestamp written
    inside the sentinel; falls back to the file's mtime."""
    try:
        return datetime.fromisoformat(done_file.read_text(encoding="utf-8").strip()).timestamp()
    except Exception:
        return done_file.stat().st_mtime


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
                   help="Estimated scan duration in seconds (shaded band width).")
    p.add_argument("--interval", type=float, default=5.0,
                   help="Refresh interval in seconds (default 5).")
    p.add_argument("--once", action="store_true",
                   help="Render the plot once and exit (no auto-save).")
    # Args below are normally supplied by vt_ir.py when it spawns the plotter.
    p.add_argument("--since", default=None,
                   help="ISO timestamp; clip the view to data at/after this time "
                        "(scopes the plot to one run).")
    p.add_argument("--label", default="SNAPSHOT",
                   help="BG or SAMPLE -- used in the saved SVG filename.")
    p.add_argument("--done-file", default=None,
                   help="Sentinel file vt_ir.py writes when the run finishes; "
                        "triggers the delayed auto-save.")
    p.add_argument("--plot-dir", default=None,
                   help="Where to save SVG snapshots (default: [paths] plot_dir, "
                        "else a 'plots' folder next to output_root).")
    p.add_argument("--save-delay", type=float, default=None,
                   help="Seconds after run finish before auto-saving "
                        "(default: [live_plot] save_delay_s, else 600).")
    args = p.parse_args(argv)

    cfg = load_config(Path(args.config))
    paths = cfg["paths"] if cfg.has_section("paths") else {}
    live = cfg["live_plot"] if cfg.has_section("live_plot") else {}

    output_root = Path(paths.get("output_root", "."))
    specac_logs = Path(
        args.specac_logs or paths.get("specac_log_dir") or DEFAULT_SPECAC_LOGS
    )
    plot_dir_str = (args.plot_dir or paths.get("plot_dir") or "").strip()
    plot_dir = Path(plot_dir_str) if plot_dir_str else output_root.parent / "plots"
    save_delay = (args.save_delay if args.save_delay is not None
                  else float(live.get("save_delay_s", "600")))

    since: Optional[datetime] = None
    if args.since:
        try:
            since = datetime.fromisoformat(args.since)
        except ValueError:
            print(f"[warn] could not parse --since {args.since!r}; showing all data.")

    done_file = Path(args.done_file) if args.done_file else None

    fig, ax = plt.subplots(figsize=(12, 5), layout="constrained")

    # 'f' key resumes auto-follow after the user has zoomed/panned.
    follow = {"force": False}

    def on_key(event):
        if event.key == "f":
            follow["force"] = True
            print("[f] resuming auto-follow (full extent).")

    if not args.once:
        try:
            fig.canvas.manager.set_window_title("VT-IR live")
        except Exception:
            pass
        fig.canvas.mpl_connect("key_press_event", on_key)
        try:
            matplotlib.use(matplotlib.get_backend())
        except Exception:
            pass

    # State carried across loop iterations.
    saved = False
    done_at: Optional[float] = None
    last_summary = ""
    specac_log: Optional[Path] = None
    session_dir: Optional[Path] = None

    def snapshot_name() -> str:
        return args.sample or (session_dir.name if session_dir else "session")

    try:
        while True:
            specac_log = find_specac_log(specac_logs)
            session_dir = find_session_dir(output_root, args.sample)

            # --- zoom preservation -------------------------------------
            # matplotlib turns autoscale OFF on an axis the moment the user
            # zooms or pans.  render() does ax.clear() (autoscale back ON) and
            # autoscales to full.  So: if autoscale is off going in, the user
            # has zoomed -> remember and restore their view after the redraw.
            frozen = not (ax.get_autoscalex_on() and ax.get_autoscaley_on())
            if follow["force"]:
                frozen = False
                follow["force"] = False
            prev_xlim, prev_ylim = ax.get_xlim(), ax.get_ylim()

            status, n_pts, n_spa = render(
                ax, specac_log, session_dir, args.scan_duration, since,
            )

            if frozen:
                ax.set_xlim(prev_xlim)
                ax.set_ylim(prev_ylim)

            summary = (
                f"specac_log={specac_log.name if specac_log else '<none>'}  "
                f"session={session_dir.name if session_dir else '<none>'}  "
                f"pts={n_pts}  scans={n_spa}"
                + ("  [zoom held; press f to follow]" if frozen else "")
            )
            if summary != last_summary:
                print(f"[{datetime.now():%H:%M:%S}] {summary}")
                last_summary = summary

            if args.once:
                plt.show()
                return 0

            # --- delayed auto-save once the run is flagged finished ----
            if done_file is not None and done_at is None and done_file.exists():
                finish = _read_finish_time(done_file)
                done_at = finish + save_delay
                print(f"[done] run finished {datetime.fromtimestamp(finish):%H:%M:%S}; "
                      f"snapshot scheduled for {datetime.fromtimestamp(done_at):%H:%M:%S}")
            if done_at is not None and not saved and time.time() >= done_at:
                out = save_snapshot(plot_dir, snapshot_name(), args.label,
                                    specac_log, session_dir, args.scan_duration, since)
                saved = True
                print(f"[saved] {out}" if out else "[saved] nothing to plot yet.")

            # plt.pause yields to the GUI event loop AND sleeps.  If the window
            # was closed it raises -> we drop out and the finally saves.
            try:
                plt.pause(max(0.1, args.interval))
            except Exception:
                break
            if not plt.fignum_exists(fig.number):
                break
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        # Save-on-exit (manual close / Ctrl-C), unless the timer already did,
        # or this was a one-shot render.
        if not saved and not args.once:
            try:
                out = save_snapshot(plot_dir, snapshot_name(), args.label,
                                    specac_log, session_dir, args.scan_duration, since)
                if out:
                    print(f"[saved on exit] {out}")
            except Exception as e:
                print(f"[warn] could not save on exit: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
