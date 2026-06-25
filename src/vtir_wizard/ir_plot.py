#!/usr/bin/env python3
"""
vtir_wizard.ir_plot  --  Live stacked-IR-spectrum window for a VT-IR session.

The companion to ``temp_plot`` (which tracks temperature vs. time): this one
tracks the spectra themselves.  While the wizard is running it re-reads the
``.SPA`` files in the current session folder every few seconds and re-draws a
temperature-colored waterfall of every scan collected so far -- the stack grows
as each new scan lands.

It is launched automatically by the orchestrator (one window per run) next to
the temperature overlay, and can also be run standalone to re-stack a finished
run.

Key behaviours:

  * --since <ISO>   Clip to a single run's spectra (by file mtime) so an earlier
                    run's files in the same sample folder don't pile in.
  * --mode          ``stack`` (offset waterfall, default) or ``overlay`` (shared
                    baseline).  --unit selects Absorbance (A) or %Transmittance.
  * Zoom is preserved across refreshes; press 'f' to resume auto-follow.
  * Single overwriting SVG.  After every new scan it overwrites
    ``<plot-dir>/<sample>/<sample>_IR_stack.svg`` -- so only the final stacked
    spectrum persists -- and writes a last copy when the run finishes
    (--done-file) or the window is closed.

Usage:
    python -m vtir_wizard.ir_plot --sample MOF42_run3
    python -m vtir_wizard.ir_plot --sample MOF42_run3 --mode overlay --unit T
    python -m vtir_wizard.ir_plot --once          # render once and exit
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import numpy as np
import matplotlib
import matplotlib.pyplot as plt
from matplotlib.cm import ScalarMappable

from vtir_wizard import config as appconfig
from vtir_wizard.spectra_io import (
    Spectrum, auto_offset, convert_unit, load_directory, make_cmap_norm,
    style_axis,
)


# ---------------------------------------------------------------------------
# Discovery + loading
# ---------------------------------------------------------------------------
def find_session_dir(output_root: Path, sample: Optional[str]) -> Optional[Path]:
    if sample:
        d = output_root / sample
        return d if d.exists() else None
    subdirs = [p for p in output_root.iterdir() if p.is_dir()] if output_root.exists() else []
    return max(subdirs, key=lambda p: p.stat().st_mtime) if subdirs else None


def collect_run_spectra(session_dir: Optional[Path], select: str,
                        since: Optional[datetime]) -> List[Spectrum]:
    """Every spectrum of ``select`` kind for the *current* run in this folder.

    Two layers keep stale files from an earlier run in the same sample folder
    (e.g. an aborted previous run) out of the stack:

      1. file mtime >= ``since`` (the run start), and
      2. de-duplication by measurement point: at most one trace per
         (temperature, direction), keeping the most recently written file.

    The second layer matters because mtime alone is fragile -- copying or syncing
    a folder bumps timestamps, which would otherwise let a leftover ``*_up.SPA``
    sit next to this run's file for the same temperature (the "two 40 C" bug)."""
    if not session_dir or not session_dir.exists():
        return []
    specs = load_directory(session_dir, select)
    if since is not None:
        specs = [s for s in specs
                 if datetime.fromtimestamp(s.path.stat().st_mtime) >= since]
    newest: dict = {}
    for s in specs:
        key = (round(s.temperature, 3), s.direction)
        try:
            mtime = s.path.stat().st_mtime
        except OSError:
            mtime = 0.0
        if key not in newest or mtime > newest[key][0]:
            newest[key] = (mtime, s)
    return [s for _, s in newest.values()]


def fingerprint(spectra: Sequence[Spectrum]) -> Tuple:
    """A cheap signature that changes whenever a new scan lands (or one is
    rewritten), used to decide when to redraw + re-save."""
    out = []
    for s in spectra:
        try:
            out.append((s.path.name, s.path.stat().st_mtime))
        except OSError:
            out.append((s.path.name, 0.0))
    return tuple(sorted(out))


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------
def render_stack(ax, spectra: List[Spectrum], *, mode: str, unit: str,
                 offset: Optional[float], cmap, norm, tick_step: Optional[float],
                 xlim: Optional[Tuple[float, float]] = None):
    """(Re-)draw the temperature-colored stack on ``ax``.  Returns a
    ScalarMappable (for an optional colorbar) or None if there was nothing to
    plot."""
    ax.clear()
    if not spectra:
        ax.text(0.5, 0.5,
                "Waiting for spectra ...\n(no .SPA files in this run yet)",
                ha="center", va="center", transform=ax.transAxes,
                fontsize=11, color="grey")
        ax.set_xticks(())
        ax.set_yticks(())
        return None

    spectra = sorted(spectra, key=lambda s: s.order_key)
    # Convert each trace to the requested display unit up front.
    conv = [(s, convert_unit(s.y, s.unit, unit)[0]) for s in spectra]

    if xlim is None:
        xlim = (max(float(s.x.max()) for s, _ in conv),
                min(float(s.x.min()) for s, _ in conv))
    span = max(xlim) - min(xlim)
    hi = max(xlim)

    if mode == "overlay":
        off_step = 0.0
    elif offset is not None:
        off_step = offset
    else:
        amps = [float(np.percentile(y, 99) - np.percentile(y, 1)) for _, y in conv]
        off_step = 0.9 * (float(np.median(amps)) if amps else 1.0)

    for i, (s, y) in enumerate(conv):
        base = i * off_step
        color = cmap(norm(s.temperature))
        ax.plot(s.x, y + base, color=color, lw=0.8)
        if off_step:  # label each curve with its temperature at the flat hi-nu end
            tail = s.x >= hi - 0.06 * span
            yb = float(np.mean(y[tail])) if tail.any() else float(y[-1])
            ax.text(hi, base + yb, f" {s.temperature:g} °C", color=color,
                    fontsize=7, ha="left", va="bottom")

    style_axis(ax, unit, xlim, hide_yticks=(mode != "overlay"), tick_step=tick_step)
    ax.margins(y=0.04)
    return ScalarMappable(norm=norm, cmap=cmap)


# Figure-height policy ------------------------------------------------------
# A fixed-height figure squashes a tall waterfall: with N spectra each gets
# canvas/N of the height, so peaks flatten as N grows.  Instead the height
# scales with the number of spectra so every spectrum keeps a roughly constant
# vertical slice (tunable via [ir_plot] height_per_spectrum / max_height).
# Overlay mode shares one baseline, so it uses a fixed comfortable height.
_BASE_HEIGHT_IN = 2.6        # fixed overhead: axis labels, ticks, colorbar
_MIN_HEIGHT_IN = 5.0
_OVERLAY_HEIGHT_IN = 6.0
_INTERACTIVE_MAX_IN = 13.0   # keep the on-screen window usable; the SVG may be taller


def stack_fig_height(n: int, mode: str, height_per: float, max_height: float) -> float:
    """Figure height in inches for an ``n``-spectrum stack."""
    if mode == "overlay":
        h = _OVERLAY_HEIGHT_IN
    else:
        h = _BASE_HEIGHT_IN + height_per * max(n, 1)
    return float(min(max(h, _MIN_HEIGHT_IN), max_height))


def save_ir_svg(out_path: Path, spectra: List[Spectrum], *, mode: str, unit: str,
                offset: Optional[float], cmap_name: str, tick_step: Optional[float],
                fig_width: float, height_per: float, max_height: float) -> bool:
    """Render onto a FRESH figure (always full extent, independent of any zoom in
    the interactive window) and overwrite ``out_path`` as SVG.  The figure height
    scales with the number of spectra so a tall stack stays legible.  Returns
    False if there was nothing to plot."""
    if not spectra:
        return False
    height = stack_fig_height(len(spectra), mode, height_per, max_height)
    cmap, norm = make_cmap_norm([s.temperature for s in spectra], cmap_name)
    fig, ax = plt.subplots(figsize=(fig_width, height), layout="constrained")
    try:
        sm = render_stack(ax, spectra, mode=mode, unit=unit, offset=offset,
                          cmap=cmap, norm=norm, tick_step=tick_step)
        if sm is None:
            return False
        cbar = fig.colorbar(sm, ax=ax, pad=0.015, fraction=0.045)
        cbar.set_label(r"Temperature  /  $^\circ$C", size=10)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(out_path)  # SVG inferred from the .svg extension
    finally:
        plt.close(fig)
    return True


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def _select_from_label(label: str) -> str:
    """The orchestrator passes --label BG/SAMPLE; a BG run stacks the single-beam
    backgrounds, a sample run stacks the ratioed samples."""
    return "bg" if label.strip().upper().startswith("BG") else "sample"


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--config", default=None,
                   help="Path to vt_ir_config.ini (default: search order -- "
                        "current folder then %%APPDATA%%/vtir-wizard).")
    p.add_argument("--sample", default=None,
                   help="Sample name to follow (default: newest folder under output_root).")
    p.add_argument("--plot-dir", default=None,
                   help="Where to save the overwriting SVG (default: [paths] "
                        "plot_dir, else a 'plots' folder next to output_root).")
    p.add_argument("--mode", default=None, choices=("stack", "overlay"),
                   help="stack (offset waterfall, default) or overlay (shared baseline).")
    p.add_argument("--unit", default=None, choices=("A", "T"),
                   help="Display unit: A=absorbance, T=%%transmittance.")
    p.add_argument("--offset", type=float, default=None,
                   help="Fixed vertical offset for stack mode (default: auto).")
    p.add_argument("--cmap", default=None, help="Matplotlib colormap (by T).")
    p.add_argument("--tick-step", type=float, default=None,
                   help="Major x-tick spacing in cm^-1 (0 = auto).")
    p.add_argument("--fig-width", type=float, default=None,
                   help="Figure width in inches (default 10).")
    p.add_argument("--height-per-spectrum", type=float, default=None,
                   help="Inches of figure height added per stacked spectrum, so a "
                        "tall stack keeps its peaks legible (default 0.5).")
    p.add_argument("--max-height", type=float, default=None,
                   help="Cap on the saved figure height in inches (default 30).")
    p.add_argument("--interval", type=float, default=None,
                   help="Refresh interval in seconds (default 5).")
    p.add_argument("--select", default=None, choices=("sample", "bg", "all"),
                   help="Which spectra to stack (default: inferred from --label).")
    p.add_argument("--once", action="store_true",
                   help="Render the stack once and exit.")
    # Args below are normally supplied by the orchestrator when it spawns this.
    p.add_argument("--since", default=None,
                   help="ISO timestamp; stack only files at/after this time.")
    p.add_argument("--label", default="SAMPLE",
                   help="BG or SAMPLE -- selects which kind to stack and used in logs.")
    p.add_argument("--done-file", default=None,
                   help="Sentinel the wizard writes when the run finishes; "
                        "triggers a final save.")
    args = p.parse_args(argv)

    cfg_path = appconfig.resolve_config_path(args.config)
    cfg = appconfig.load_config_optional(cfg_path)
    paths = cfg["paths"] if cfg.has_section("paths") else {}
    ir = cfg["ir_plot"] if cfg.has_section("ir_plot") else {}

    # CLI flag wins, else [ir_plot] config, else a sensible default.
    mode = args.mode or str(ir.get("mode", "stack")).strip().lower()
    unit = args.unit or str(ir.get("unit", "A")).strip().upper()
    cmap_name = args.cmap or str(ir.get("cmap", "gnuplot2")).strip()
    interval = args.interval if args.interval is not None else float(ir.get("interval_s", "5"))
    tick_step = args.tick_step if args.tick_step is not None else float(ir.get("tick_step", "500"))
    if tick_step == 0:
        tick_step = None
    offset = args.offset
    if offset is None:
        cfg_off = str(ir.get("offset", "")).strip()
        offset = float(cfg_off) if cfg_off else None
    select = args.select or _select_from_label(args.label)

    # Figure sizing.  The height grows with the number of spectra so a tall
    # stack doesn't squash the peaks (height_per_spectrum inches per scan, up to
    # max_height); the interactive window is capped so it stays usable on screen.
    fig_width = args.fig_width if args.fig_width is not None else float(ir.get("fig_width", "10"))
    height_per = args.height_per_spectrum if args.height_per_spectrum is not None \
        else float(ir.get("height_per_spectrum", "0.5"))
    max_height = args.max_height if args.max_height is not None else float(ir.get("max_height", "30"))

    output_root = Path(paths.get("output_root", "."))
    plot_dir_str = (args.plot_dir or paths.get("plot_dir") or "").strip()
    plot_dir = Path(plot_dir_str) if plot_dir_str else output_root.parent / "plots"

    since: Optional[datetime] = None
    if args.since:
        try:
            since = datetime.fromisoformat(args.since)
        except ValueError:
            print(f"[warn] could not parse --since {args.since!r}; stacking all data.")
    done_file = Path(args.done_file) if args.done_file else None

    fig, ax = plt.subplots(figsize=(fig_width, _MIN_HEIGHT_IN), layout="constrained")

    # 'f' key resumes auto-follow after the user has zoomed/panned.
    follow = {"force": False}

    def on_key(event):
        if event.key == "f":
            follow["force"] = True
            print("[f] resuming auto-follow (full extent).")

    if not args.once:
        try:
            fig.canvas.manager.set_window_title("VT-IR live stack")
        except Exception:
            pass
        fig.canvas.mpl_connect("key_press_event", on_key)
        try:
            matplotlib.use(matplotlib.get_backend())
        except Exception:
            pass

    session_dir: Optional[Path] = None
    last_fp: Optional[Tuple] = None
    saved_done = False
    last_summary = ""
    # The view limits we last applied via autoscale.  Used to tell our own
    # rescale apart from a user zoom/pan (see redraw_and_save).
    auto_view = {"xlim": None, "ylim": None}

    def out_svg() -> Path:
        name = args.sample or (session_dir.name if session_dir else "session")
        return plot_dir / name / f"{name}_IR_stack.svg"

    def redraw_and_save(spectra: List[Spectrum]) -> None:
        """Redraw the interactive axes (preserving a manual zoom) and overwrite
        the SVG."""
        cmap, norm = make_cmap_norm([s.temperature for s in spectra], cmap_name)
        # Zoom preservation.  We can't use ax.get_autoscale*_on() here the way the
        # temperature plot does: style_axis always sets an explicit *inverted*
        # x-limit (high wavenumber on the left), which turns x-autoscale off on
        # every frame and would look like a permanent manual zoom -- freezing the
        # y-axis so new, taller spectra never get room.  Instead we compare the
        # current view to the limits WE last applied: if they differ, the user
        # zoomed/panned, so keep their view; otherwise rescale to full extent.
        cur_xlim, cur_ylim = ax.get_xlim(), ax.get_ylim()
        user_zoomed = (auto_view["xlim"] is not None
                       and (cur_xlim != auto_view["xlim"] or cur_ylim != auto_view["ylim"]))
        if follow["force"]:
            user_zoomed = False
            follow["force"] = False
        render_stack(ax, spectra, mode=mode, unit=unit, offset=offset,
                     cmap=cmap, norm=norm, tick_step=tick_step)
        n = len(spectra)
        ax.set_title(f"VT-IR live stack   --   {out_svg().parent.name}   ({n} scan{'s' if n != 1 else ''})",
                     fontsize=10)
        if user_zoomed:
            ax.set_xlim(cur_xlim)
            ax.set_ylim(cur_ylim)
        else:
            # Remember the fresh full-extent limits so the next frame can tell our
            # own rescale apart from a user zoom.
            auto_view["xlim"] = ax.get_xlim()
            auto_view["ylim"] = ax.get_ylim()
        # Grow the on-screen window with the stack (capped) so peaks stay legible.
        want_h = min(stack_fig_height(n, mode, height_per, max_height), _INTERACTIVE_MAX_IN)
        if abs(fig.get_figheight() - want_h) > 0.1:
            try:
                fig.set_size_inches(fig_width, want_h, forward=True)
            except Exception:
                pass
        if spectra:
            try:
                save_ir_svg(out_svg(), spectra, mode=mode, unit=unit,
                            offset=offset, cmap_name=cmap_name, tick_step=tick_step,
                            fig_width=fig_width, height_per=height_per, max_height=max_height)
            except Exception as e:
                print(f"[warn] could not save SVG: {e}")

    try:
        while True:
            session_dir = find_session_dir(output_root, args.sample)
            spectra = collect_run_spectra(session_dir, select, since)
            fp = fingerprint(spectra)
            changed = fp != last_fp

            if changed:
                redraw_and_save(spectra)
                last_fp = fp

            summary = (f"session={session_dir.name if session_dir else '<none>'}  "
                       f"scans={len(spectra)}  -> {out_svg().name}")
            if summary != last_summary:
                print(f"[{datetime.now():%H:%M:%S}] {summary}")
                last_summary = summary

            if args.once:
                if not changed:  # ensure at least one draw happened
                    redraw_and_save(spectra)
                # Only pop a window on an interactive backend; a headless re-stack
                # (Agg) just writes the SVG without the "cannot be shown" warning.
                if "agg" not in matplotlib.get_backend().lower():
                    plt.show()
                return 0

            # On run completion, take one final save (no delay needed -- the IR
            # stack has no cool-down tail to wait for) and keep the window open.
            if done_file is not None and not saved_done and done_file.exists():
                redraw_and_save(spectra)
                saved_done = True
                print(f"[done] run finished; final stack saved to {out_svg()}")

            try:
                plt.pause(max(0.2, interval))
            except Exception:
                break
            if not plt.fignum_exists(fig.number):
                break
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        # Save-on-exit (manual close / Ctrl-C), unless a one-shot render.
        if not args.once:
            try:
                spectra = collect_run_spectra(
                    find_session_dir(output_root, args.sample), select, since)
                if spectra and save_ir_svg(out_svg(), spectra, mode=mode, unit=unit,
                                           offset=offset, cmap_name=cmap_name,
                                           tick_step=tick_step, fig_width=fig_width,
                                           height_per=height_per, max_height=max_height):
                    print(f"[saved on exit] {out_svg()}")
            except Exception as e:
                print(f"[warn] could not save on exit: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
