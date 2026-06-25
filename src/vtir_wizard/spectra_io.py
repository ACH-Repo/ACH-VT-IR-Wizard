"""Spectrum readers + light styling helpers for the live IR-stack window.

Trimmed and vendored from the companion **ACH-VT-IR-Plotter** (`plot_vt_ir.py`)
so the wizard can read and stack spectra without a runtime dependency on that
separate project.  Kept deliberately close to the original so fixes can be
ported back and forth.

Covers exactly what `ir_plot.py` needs:

    * the wizard file-naming convention (`classify`, `NAME_RE`)
    * native readers for `.SPA` / `.csv` / `.jdx` (`read_xy` and friends)
    * Absorbance/Transmittance resolution + conversion
    * a temperature colormap, an auto offset, and IR-convention axis styling
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from matplotlib import colormaps
from matplotlib.colors import LinearSegmentedColormap, Normalize
from matplotlib.ticker import AutoMinorLocator, MultipleLocator


# --------------------------------------------------------------------------- #
# File-naming convention (emitted by the wizard)
#
#   [NN_]BG_<sample>_<T>C.<ext>                    background (single beam)
#   [NN_]<sample>_<T>C[_up|_down|_return].<ext>    sample spectrum
#
# The leading ``NN_`` is an optional chronological index.
# --------------------------------------------------------------------------- #
NAME_RE = re.compile(
    r"^(?:(?P<idx>\d+)_)?"
    r"(?P<bg>BG_)?"
    r"(?P<sample>.+?)_"
    r"(?P<temp>-?\d+(?:[.,]\d+)?)C"
    r"(?:_(?P<direction>up|down|return))?$",
    re.IGNORECASE,
)

SUPPORTED_EXTS = (".csv", ".spa", ".jdx", ".dx", ".jcm", ".txt")

# OMNIC SPA y data-type code -> internal unit token (per spectrochempy).
OMNIC_YCODE: Dict[int, str] = {
    17: "A",     # absorbance
    16: "T",     # %transmittance
    11: "R%",    # reflectance (percent)
    12: "logR",  # log(1/R)
    15: "SB",    # single beam
    20: "KM",    # Kubelka-Munk
    21: "R%",    # reflectance
    22: "IFG",   # detector signal / interferogram (V)
    26: "PA",    # photoacoustic
    31: "Raman",
}

UNIT_LABEL: Dict[str, str] = {
    "A": r"$\mathrm{Absorbance}\ /\ \mathrm{arb.\ units}$",
    "T": r"$\mathrm{Transmittance}\ /\ \%$",
    "SB": r"$\mathrm{Single\text{-}beam\ intensity}$",
    "R%": r"$\mathrm{Reflectance}\ /\ \%$",
    "logR": r"$\log(1/R)$",
    "KM": r"$\mathrm{Kubelka\text{-}Munk}$",
    "IFG": r"$\mathrm{Interferogram}\ /\ \mathrm{V}$",
    "PA": r"$\mathrm{Photoacoustic}$",
    "Raman": r"$\mathrm{Raman\ intensity}$",
    "INT": r"$\mathrm{Intensity}$",
}

DIRECTION_LABEL = {"up": "heating (up)", "down": "cooling (down)", "return": "return"}


# --------------------------------------------------------------------------- #
# Spectrum container
# --------------------------------------------------------------------------- #
@dataclass
class Spectrum:
    path: Path
    x: np.ndarray            # wavenumber, ascending
    y: np.ndarray
    sample: str
    temperature: float
    direction: Optional[str]  # 'up' | 'down' | 'return' | None (background)
    kind: str                 # 'sample' | 'bg'
    index: Optional[int]      # chronological NN_ prefix, if present
    unit: str = "A"           # internal unit token; see UNIT_LABEL
    unit_source: str = ""     # how the unit was decided (for the report)

    @property
    def order_key(self) -> Tuple[int, float]:
        # Order by acquisition (NN_ index) when available, else by temperature.
        idx = self.index if self.index is not None else 10 ** 9
        return (idx, self.temperature)


# --------------------------------------------------------------------------- #
# Readers -- each returns (x, y, native_unit_or_None)
# --------------------------------------------------------------------------- #
def read_csv(path: Path) -> Tuple[np.ndarray, np.ndarray, Optional[str]]:
    """Two-column (wavenumber, intensity) text. Auto-detects the ``;`` / ``,`` /
    whitespace delimiter and European (comma) decimals, and skips any
    non-numeric header lines. A bare CSV carries no unit information."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise ValueError("empty file")

    delim: Optional[str] = None
    probe = lines[0]
    if ";" in probe:
        delim = ";"
    elif "\t" in probe:
        delim = "\t"
    elif probe.count(",") == 1 and "." in probe:
        delim = ","

    rows: List[Tuple[str, str]] = []
    for ln in lines:
        parts = ln.split(delim) if delim else ln.split()
        if len(parts) < 2:
            continue
        a, b = parts[0].strip(), parts[1].strip()
        try:
            float(a.replace(",", "."))
        except ValueError:
            continue  # header / comment line
        rows.append((a, b))
    if not rows:
        raise ValueError("no numeric rows found")

    arr = np.array([[c.replace(",", ".") for c in r] for r in rows], dtype=float)
    return arr[:, 0], arr[:, 1], None


def read_spa(path: Path) -> Tuple[np.ndarray, np.ndarray, Optional[str]]:
    """Thermo OMNIC ``.SPA`` binary. Walks the section table to find the
    spectral-header block (key 2: point count + first/last wavenumber + y
    data-type code) and the intensity block (key 3: float32 values)."""
    raw = Path(path).read_bytes()
    n = len(raw)
    nx = first_x = last_x = None
    ycode: Optional[int] = None
    data_off = data_size = None

    pos = 304  # section table start
    for _ in range(64):
        if pos + 10 > n:
            break
        key = struct.unpack_from("<H", raw, pos)[0]
        off = struct.unpack_from("<I", raw, pos + 2)[0]
        size = struct.unpack_from("<I", raw, pos + 6)[0]
        if key == 0 and off == 0:
            break
        if key == 2 and 0 < off < n:           # spectral header
            nx = struct.unpack_from("<I", raw, off + 4)[0]
            ycode = raw[off + 12]               # y data-type code (uint8)
            first_x = struct.unpack_from("<f", raw, off + 16)[0]
            last_x = struct.unpack_from("<f", raw, off + 20)[0]
        elif key == 3 and 0 < off < n:         # intensities
            data_off, data_size = off, size
        pos += 16

    if data_off is None or first_x is None:
        raise ValueError("not a recognizable OMNIC SPA file (no key 2/3 block)")

    y = np.frombuffer(raw, dtype="<f4", count=data_size // 4, offset=data_off).astype(float)
    x = np.linspace(first_x, last_x, len(y))
    unit = OMNIC_YCODE.get(ycode) if ycode is not None else None
    return x, y, unit


def read_jcampdx(path: Path) -> Tuple[np.ndarray, np.ndarray, Optional[str]]:
    """JCAMP-DX reader (handles the common ``(X++(Y..Y))`` tabular form)."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    fields = [f for f in text.split("##") if f.strip()]

    meta: Dict[str, object] = {}
    for field in fields:
        key, _, val = field.strip().partition("=")
        val = val.strip().replace("\n", " ")
        key = key.strip().upper()
        try:
            meta[key] = float(val)
        except ValueError:
            meta[key] = val

    npoints = int(meta.get("NPOINTS", 0))
    first_x = float(meta.get("FIRSTX", 0.0))
    last_x = float(meta.get("LASTX", 0.0))
    xfactor = float(meta.get("XFACTOR", 1.0))
    yfactor = float(meta.get("YFACTOR", 1.0))
    x = np.linspace(first_x, last_x, npoints) * xfactor

    m = re.search(r"##XYDATA=\s*\(X\+\+\(Y\.\.Y\)\)", text)
    if not m:
        raise ValueError("unsupported JCAMP-DX variant (no (X++(Y..Y)) table)")
    body = text[m.end():].split("##", 1)[0].strip()
    y_vals: List[float] = []
    for line in body.splitlines():
        toks = line.split()[1:]  # first token is the line's x anchor
        y_vals.extend(float(t) for t in toks)
    y = np.array(y_vals, dtype=float) * yfactor
    if len(y) != len(x):  # tolerate small grid mismatches
        x = np.linspace(first_x, last_x, len(y)) * xfactor

    yunits = str(meta.get("YUNITS", "")).strip().lower()
    unit = None
    if "abs" in yunits:
        unit = "A"
    elif "trans" in yunits:
        unit = "T"
    return x, y, unit


def read_xy(path: Path) -> Tuple[np.ndarray, np.ndarray, Optional[str]]:
    ext = path.suffix.lower()
    if ext == ".spa":
        return read_spa(path)
    if ext in (".jdx", ".dx", ".jcm"):
        return read_jcampdx(path)
    return read_csv(path)


# --------------------------------------------------------------------------- #
# Classification + unit resolution
# --------------------------------------------------------------------------- #
def classify(path: Path) -> Optional[dict]:
    """Parse the wizard naming convention from a filename stem."""
    m = NAME_RE.match(path.stem)
    if not m:
        return None
    direction = m["direction"].lower() if m["direction"] else None
    kind = "bg" if m["bg"] else "sample"
    if kind == "sample" and direction is None:
        direction = "up"  # up-only runs omit the suffix
    return {
        "index": int(m["idx"]) if m["idx"] else None,
        "temperature": float(m["temp"].replace(",", ".")),
        "direction": direction,
        "kind": kind,
        "sample": m["sample"],
    }


def heuristic_unit(y: np.ndarray) -> str:
    """Scale-free Absorbance/Transmittance guess for unit-less CSVs: baseline at
    the bottom (peaks up) -> A; baseline at the top (dips down) -> T."""
    y = y[np.isfinite(y)]
    if y.size == 0:
        return "A"
    lo, med, hi = np.percentile(y, [1, 50, 99])
    rng = hi - lo
    if rng <= 0:
        return "A"
    baseline_pos = (med - lo) / rng
    return "T" if baseline_pos > 0.55 else "A"


def resolve_unit(meta: dict, native_unit: Optional[str], y: np.ndarray) -> Tuple[str, str]:
    """Apply the unit-resolution hierarchy. Returns (unit_token, source)."""
    if native_unit:
        return native_unit, "native"
    if meta["kind"] == "bg":
        return "SB", "background"
    return heuristic_unit(y), "heuristic"


def convert_unit(y: np.ndarray, src: str, dst: str) -> Tuple[np.ndarray, str]:
    """Convert between absorbance and %transmittance. Other unit pairs are left
    untouched (returned as-is)."""
    if src == dst:
        return y, dst
    if src == "A" and dst == "T":
        return 100.0 * 10.0 ** (-y), "T"
    if src == "T" and dst == "A":
        frac = y / 100.0 if np.nanmax(y) > 1.5 else y  # %T vs fractional T
        return -np.log10(np.clip(frac, 1e-6, None)), "A"
    return y, src


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def load_directory(folder: Path, select: str) -> List[Spectrum]:
    """Read and classify every supported, recognizable file in ``folder``.

    ``select`` is ``"sample"``, ``"bg"`` or ``"all"``.  One unreadable file is
    skipped, not fatal (the live plot keeps going as scans land)."""
    chosen = sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS
        and classify(p) and (select == "all" or classify(p)["kind"] == select)
    )
    out: List[Spectrum] = []
    for p in chosen:
        meta = classify(p)
        try:
            x, y, native_unit = read_xy(p)
        except Exception:  # noqa: BLE001 -- one bad/partial file shouldn't abort
            continue
        if x.size and x[0] > x[-1]:           # normalize to ascending wavenumber
            x, y = x[::-1], y[::-1]
        unit, source = resolve_unit(meta, native_unit, y)
        out.append(Spectrum(path=p, x=x, y=y, unit=unit, unit_source=source, **meta))
    return out


# --------------------------------------------------------------------------- #
# Styling helpers
# --------------------------------------------------------------------------- #
def make_cmap_norm(temps: Sequence[float], cmap_name: str,
                   truncate: Tuple[float, float] = (0.1, 0.85)):
    base = colormaps[cmap_name]
    cmap = LinearSegmentedColormap.from_list(
        f"{cmap_name}_t", base(np.linspace(truncate[0], truncate[1], 256))
    )
    tmin, tmax = (min(temps), max(temps)) if temps else (0.0, 1.0)
    if tmin == tmax:
        tmax = tmin + 1.0
    return cmap, Normalize(tmin, tmax)


def auto_offset(spectra: Sequence[Spectrum]) -> float:
    """A fixed offset just under one typical spectrum's amplitude, so adjacent
    traces separate with a little overlap."""
    amps = [float(np.percentile(s.y, 99) - np.percentile(s.y, 1)) for s in spectra]
    return 0.9 * (float(np.median(amps)) if amps else 1.0)


def style_axis(ax, unit: str, xlim: Tuple[float, float], hide_yticks: bool,
               tick_step: Optional[float]) -> None:
    hi, lo = max(xlim), min(xlim)
    ax.set_xlim(hi, lo)  # IR convention: high wavenumber on the left
    ax.set_xlabel(r"$\tilde\nu\ /\ \mathrm{cm^{-1}}$", size=11)
    ax.set_ylabel(UNIT_LABEL.get(unit, UNIT_LABEL["INT"]), size=11, labelpad=7)
    if tick_step:
        ax.xaxis.set_major_locator(MultipleLocator(tick_step))
    ax.xaxis.set_minor_locator(AutoMinorLocator())
    ax.yaxis.set_minor_locator(AutoMinorLocator())
    ax.tick_params(axis="both", which="both", direction="in", labelsize=8)
    if hide_yticks:
        ax.set_yticks(())
