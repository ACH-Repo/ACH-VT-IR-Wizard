# VT-IR Wizard

One Python script that drives a **Thermo Nicolet iS5** FTIR and a **Specac
heated Golden Gate ATR** from a single process — turning a heater + spectrometer
from two different manufacturers into one variable-temperature IR experiment
with chronologically-indexed output files and a live overlay plot.

<!-- TODO: replace with a screenshot of analyse_live.py running alongside a
     real VT-IR experiment.  Best frame: the temperature/setpoint trace already
     covers a few steps so the shaded scan bands (BG = blue, up = red,
     down = orange, return = green) are clearly visible. -->
![Live overlay plot of a VT-IR run](docs/images/live-overlay.png)

## Quick start

```
pip install pywin32 matplotlib
```

1. Open `vt_ir_config.ini` and replace every `<you>` with your Windows user name (and adjust the paths if your data lives elsewhere).
2. Open OMNIC (the DDE conversation needs it running).
3. Double-click `run_vt_ir.bat`.

The script asks for the sample name, mode (background or sample), temperatures,
and a couple of optional measurement modifiers. After you confirm, it
**(a)** drives the Specac controller temperature-by-temperature via its CLI,
**(b)** waits for the cell to actually reach each setpoint,
**(c)** collects the BG / sample spectrum over OMNIC's DDE interface, and
**(d)** exports it as both `.SPA` and a plotting-friendly format
(`.csv` by default).

A separate console window opens automatically with a live overlay plot —
temperature and setpoint trace from the Specac log file, with each completed
scan shaded onto the timeline.

## What you'll see

```
==================================================================
  VT-IR Orchestrator   --   iS5  +  Specac heated Golden Gate
==================================================================
Sample / experiment name: MOF42_run3
Mode -- 0 = Background  (empty ATR)  /  1 = Sample (0/1) [0]: 1
Temperatures -- list "50 100 150" or range "(50,200,20)" [(50,200,20)]:
Add a cool-down (down-scan) after heating, to check reversibility? (y/n) [n]: n
Add ONE final measurement at the starting temperature after cooling? (y/n) [n]: y
OMNIC experiment file (.exp) [C:\my documents\omnic\Param\VT_128_2.exp]:
Extra equilibration seconds at each set point (after Specac says ready) [120]:
2026-06-01 12:00:00  INFO  Sample:                MOF42_run3
2026-06-01 12:00:00  INFO  Mode:                  Sample
2026-06-01 12:00:00  INFO  Temperatures (C):      50, 70, 90, 110, 130, 150, 170, 190, 200
2026-06-01 12:00:00  INFO  Extra steps:           yes  (10 total: up then one final point at starting T)
2026-06-01 12:00:00  INFO  Schedule:              50^  70^  90^  110^  130^  150^  170^  190^  200^  50*
2026-06-01 12:00:00  INFO  OMNIC experiment file: C:\my documents\omnic\Param\VT_128_2.exp
2026-06-01 12:00:00  INFO  Extra export formats:  csv
...
Ready? (y = start, n = abort) (y/n) [y]: y
```

<!-- TODO: optional screenshot of the orchestrator console + OMNIC + the live
     plot side-by-side during a real run.  Demonstrates the "single launch,
     two windows" workflow nicely. -->

After the confirmation the wizard runs unattended; every Specac CLI call,
every DDE command, and the actual `temp?` reading at each set point land in a
timestamped log file under your configured `log_dir`.

## Temperature input syntax

| Form | Example | Expands to |
|------|---------|-----------|
| Range tuple | `(50,200,20)` | `50, 70, 90, 110, 130, 150, 170, 190, 200` |
| Whitespace-separated list | `50 100 150 200` | `50, 100, 150, 200` |
| Comma-separated list | `50, 100, 150` | `50, 100, 150` |

All temperatures are in °C and must fall within `[tmin, tmax]` from the config.

## Features

- **Two-pass workflow** — one heating run with the ATR empty produces the
  per-temperature backgrounds; a second run with the sample loaded re-uses
  those BGs to ratio the sample spectra automatically. No "needs a background
  first" error and no manual sample swap at temperature.
- **Optional cool-down (down-scan)** — ascending then descending, files tagged
  `_up` / `_down`, reuses the up-scan BGs.
- **Optional single return-to-start** — for users who want a lightweight
  reversibility check without a full down-scan pass; produces one extra
  `…_return.SPA` at the lowest temperature after the heating ramp.
- **Flexible background matching** — backgrounds do *not* have to match the
  sample temperature exactly (the cell sits in a controlled glovebox). Per
  run you pick `exact`, `closest` (nearest available BG temperature per step),
  or `fixed` (one chosen BG for every step). Temperature mismatches and
  backgrounds older than `[backgrounds] max_age_warn_days` are **warned about
  but never block the run** — and every such decision is written to the run
  log so there's a clear record of exactly which background each measurement
  used.
- **Chronological filename index** — every spectrum gets a zero-padded
  prefix (`00_`, `01_`, …) so Explorer's name sort matches the order they
  were collected. Padding adapts to the schedule length.
- **Configurable extra export formats** — every collection is written as
  OMNIC's native `.SPA` plus any extension listed in `[export]
  additional_formats` (default `csv`; `jdx` and others work too).
- **Robust against the long-collection DDE timeout** — uses OMNIC's `Polling`
  keyword + `MenuStatus` polling, the pattern the DDE manual recommends on
  page 124.
- **Robust against stuck-Wait state on the Specac controller** — sends an
  explicit `off` → `on` at start, queries `temp?` after every `sp T w`, and
  aborts before OMNIC collects at the wrong temperature.
- **Live overlay plot** — auto-launches alongside the orchestrator and
  re-reads the Specac log + the session folder every few seconds. It is
  scoped to the current run (the background pass and the sample pass don't
  pile into one cramped image), preserves your zoom across refreshes (press
  `f` to resume auto-follow), and auto-saves an SVG snapshot — once the run
  finishes plus `save_delay_s` (default 10 min, to capture the cool-down
  tail), and again on manual window close. Snapshots land in
  `<plot_dir>/<sample>/<sample>_<BG|SAMPLE>_<timestamp>.svg`.
- **Tracebacks land in the run log** — if something blows up at 3 AM, the
  full traceback is in the same `.log` file as the call history that
  preceded it.

## Installation

Requirements:

- Python 3.10+
- `pywin32` (DDE bindings) — `pip install pywin32`
- `matplotlib` (for `analyse_live.py`) — `pip install matplotlib`
- Specac USB Temperature Controller software **v1.0.23.0 or later** (the CLI
  was added in this version). Confirm with `specac.cmd temp?` in a terminal.
- Thermo OMNIC. Any version that supports DDE (i.e. ≥ 6.0).

Tested combination: OMNIC + Nicolet iS5 + Specac heated Golden Gate ATR +
Specac controller software v1.0.23.0+.

## Project layout

```
ACH-VT-IR-Wizard/
├── vt_ir.py              the orchestrator
├── analyse_live.py       live-updating overlay plot
├── vt_ir_config.ini      machine-specific paths and defaults
├── run_vt_ir.bat         double-click launcher for the orchestrator
├── run_analyse_live.bat  double-click launcher for the plot (standalone use)
├── README.md             you are here
├── LICENSE               MIT
└── .gitignore
```

## How it works

<details>
<summary>Architecture and DDE call shapes (click to expand)</summary>

### Why the obvious approach doesn't work

OMNIC and the Specac controller come from different manufacturers and don't
talk to each other. The naive "run a Specac heating program in parallel with
an OMNIC macro" workflow has two failure modes that turn up immediately on
real hardware:

1. **Timing drift.** A `Wait <N> s` slot in the Specac `.prog` that's sized to
   match an estimated OMNIC scan duration breaks the moment scans run longer
   than expected. Every subsequent measurement lands inside the next ramp.
2. **"OMNIC needs a background first."** OMNIC refuses to collect a sample
   without a current background, and a parallel macro has no way to attach a
   different BG to each temperature.

### The Specac side

Specac's USB controller v1.0.23.0+ exposes a CLI:

```
specac.cmd on | off | c | f | k
specac.cmd tol <degrees>
specac.cmd ramp <C/min>
specac.cmd sp <T>      # setpoint only
specac.cmd sp <T> w    # setpoint AND block until reached within tolerance
specac.cmd temp? | sp? | ramp?
```

The `sp <T> w` call is the key — it returns when the cell is actually within
tolerance of the setpoint, so the Python orchestrator never has to guess
timings.

The controller has been observed returning from `sp T w` while the cell was
still far from `T` (a stuck "Wait" state left over from a previous `.prog`
session). To defend against that, `vt_ir.py` sends an explicit `off` before
`on` in its preamble and queries `temp?` after every wait — if the reading
is more than `tolerance + 5 °C` from the setpoint, the run aborts before
OMNIC starts a doomed collection.

### The OMNIC side

OMNIC's DDE interface (manual: `OMNIC DDE.pdf`, app name `OMNIC`, topic
`Spectra`) handles the spectrometer. A few details from the trenches:

- **The 60-second DDE Exec timeout.** pywin32's `Conversation.Exec(...)` has
  a hard 60 s internal timeout, but a 128-scan @ 2 cm⁻¹ collection is ~3.5
  min. The fix is the `Polling` keyword (DDE manual p. 124): the command
  returns immediately, and Python waits by polling
  `MenuStatus CollectBackground` / `…CollectSample` until OMNIC reports the
  menu enabled again.
- **`SetAsBackground` instead of `Collect/BackgroundFileName`.** The
  documented way to bind a saved `.SPA` as the next ratio reference is to
  poke the `Collect` group's `BackgroundHandling = ThisBkg` and
  `BackgroundFileName = <path>` parameters. On the tested iS5 build, OMNIC
  rejects writes to `BackgroundFileName` over both `DDEPoke` and
  `[Set …]` Exec. The GUI-equivalent workaround works fine:
  ```
  [Import "<bg.spa>"] -> [Display] -> [SetAsBackground]
  -> [DeleteSelectedSpectra] -> [CollectSample …]
  ```
- **Without `Invoke`, collected spectra land in OMNIC's invisible DDE
  window.** A `[Display]` before `[Export]` makes the new spectrum the
  active/selected one so Export saves what we just collected.

### The orchestrator side

The per-step body in `vt_ir.py` follows the same shape regardless of mode:

```
specac.cmd sp <T> w      # heat to T, block until reached
specac.cmd temp?         # sanity check the cell is at T
sleep <equilibration_s>  # let the sample equilibrate
collect spectrum         # see DDE call shapes above
export .SPA + extras     # one [Export] per requested format
clear workspace
```

Sample-mode adds the BG bind (Import + SetAsBackground) before the collect.

### Filename convention

```
<output_root>/<sample>/NN_BG_<sample>_<T>C.SPA          # backgrounds
<output_root>/<sample>/NN_<sample>_<T>C.SPA             # samples, plain
<output_root>/<sample>/NN_<sample>_<T>C_up.SPA          # samples, down-scan enabled
<output_root>/<sample>/NN_<sample>_<T>C_down.SPA        # samples, down-scan enabled
<output_root>/<sample>/NN_<sample>_<T>C_return.SPA      # samples, return-to-start enabled
```

`NN` is the zero-padded chronological index for that session. Each extra
format from `[export] additional_formats` produces a parallel file with the
same stem (`NN_<sample>_<T>C.csv`, …).

The sample-mode BG lookup uses a glob that matches both indexed
(`NN_BG_…`) and unindexed (`BG_…`) names, so BG sets collected before the
index feature was introduced still work without renaming.

</details>

<details>
<summary>Troubleshooting (click to expand)</summary>

**`pywin32 is required for OMNIC DDE access`** — `pip install pywin32`.

**`Could not open DDE conversation with OMNIC`** — OMNIC isn't running, or
its DDE server isn't ready. Open OMNIC first, leave at least one spectral
window visible, then re-run.

**`Could not find specac.cmd at: …`** — update the Specac controller
software to v1.0.23.0+ or correct the `specac_exe` path in
`vt_ir_config.ini`. Confirm with `where specac.cmd.exe` in a terminal.

**`Specac CLI refused the command. … Not Running In Manual Mode`** — the
Specac controller GUI is in Program Mode (the tab you use to run a
`.prog` file), and the CLI only works in Manual Mode. Switch the GUI to
Manual Mode (the home tab with the setpoint readout and arrow buttons)
and re-run. The orchestrator aborts on this within one second — before
OMNIC opens — so no spectra are collected at the wrong temperature.

**`Specac CLI refused the command. … Received Invalid Value`** — Specac
rejected a numeric argument. Most common cause: a leading decimal point
like `tolerance_c = .5` in `vt_ir_config.ini`. Use `0.5` instead. (Recent
versions of `vt_ir.py` normalize this automatically, but the underlying
CLI is picky about it.)

**`No background files found for sample …`** — the sample-mode preflight
found no `BG_<sample>_<T>C.SPA` at all for this sample. The message lists
which other sample folders DO have BG files, in case you typed the name
slightly wrong. (Run BG mode first if there genuinely are none.)

**`Missing exact-temperature backgrounds for sample …`** — you chose the
`exact` matching mode but at least one sample temperature has no BG at that
exact temperature. Either collect the missing BGs, or re-run and pick the
`closest` or `fixed` matching mode to proceed with the backgrounds you
already have (mismatches are warned about and logged, not blocked).

**`Specac reported 'setpoint reached' but cell is at X C`** — the
controller's CLI returned from `sp T w` while the cell was still far from
`T`. The orchestrator already sends `off` before `on` to flush this state;
if it triggers anyway, close and reopen the Specac controller GUI to fully
clear it, then re-run.

**`OMNIC did not accept 'sample …' / 'LoadParameters' after N attempt(s)` /
repeated collections fail to start** — OMNIC accepted the file/display
commands (Import, SetAsBackground) but could not *start* the actual
collection — or a fresh run failed immediately on `LoadParameters`. The DDE
channel is fine; the **iS5 bench is wedged** — a dialog is open in OMNIC, a
scan is already running, or (most common) the spectrometer has dropped
offline. The orchestrator reconnects and retries `collect_retries` times
first (this now also covers the `LoadParameters` preamble, so a run started
right after a wedged one gets a chance to recover); if that fails it aborts
with this message. **Fix: restart OMNIC.** The bench can stay wedged across
runs until OMNIC is restarted — which is why re-running without restarting
keeps failing on the very first command. If it recurs specifically during a
long passive cool-down, the lengthening gap between scans may be letting the
bench idle out; restarting OMNIC before the sample pass and keeping an eye on
the first down-scan collection is the practical workaround.

**`dde.error: Exec failed` mid-collection** — the underlying pywin32 error
behind the message above (pywin32's DDE Exec has a hard 60 s transaction
timeout, which trips when OMNIC can't start the command). Handled by the
retry/reconnect logic; see the entry above.

**Heater not switching off after a crash** — the `finally` block calls
`specac.cmd off` on every exit path, including Ctrl-C. If you killed the
process hard, run `specac.cmd off` manually in a terminal.

</details>

## Authorship and history

This project was written by **[@p3rAsperaAdAstra](https://github.com/p3rAsperaAdAstra)**
in collaboration with **Claude (Anthropic's AI assistant)** in May–June 2026.
The earlier parallel-process design (a Specac `.prog` generator plus an OMNIC
Macros\Basic script timed against each other) is preserved for reference in
the author's notes; this repository is the rewrite that replaced it with a
single Python orchestrator talking to both instruments over their respective
control interfaces.

Specific user-visible features added during the rewrite:

- Single-process orchestration via DDE + Specac CLI (no parallel macros).
- Per-temperature background binding via `Import` + `SetAsBackground`.
- Polling-based long-collection support.
- Live overlay plot (`analyse_live.py`) auto-launched alongside the run.
- Optional cool-down and optional single return-to-start measurement.
- Chronological filename indices, configurable extra export formats.
- Sanity-checked Specac waits, traceback-routed log files.

This note is included for transparency about what was written by hand vs.
with AI assistance.
