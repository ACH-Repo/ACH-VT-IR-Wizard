"""vtir_wizard -- variable-temperature IR orchestrator for a Thermo Nicolet iS5
+ Specac heated Golden Gate ATR + Specac USB temperature controller.

The package exposes three entry points:

    vtir_wizard.orchestrator   the run wizard (``vtir-wizard`` console script)
    vtir_wizard.temp_plot      live temperature/setpoint overlay window
    vtir_wizard.ir_plot        live stacked-IR-spectrum window

Only light-weight, dependency-free shared constants live in this top-level
module so the plot subprocesses (which import ``SCHEDULE_KINDS``) don't drag in
the orchestrator's pywin32 dependency.
"""
from __future__ import annotations

__version__ = "1.4.3"

# Single source of truth for per-direction metadata used across the wizard
# summary (glyph), the per-step log line (arrow), and the live plots' overlay
# shading (color, label).  Adding a new schedule kind means adding one row here
# -- not editing four sites.  Background ("bg") is also listed even though it
# isn't a step direction, because the live plots shade BG scans too.
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

__all__ = ["__version__", "SCHEDULE_KINDS"]
