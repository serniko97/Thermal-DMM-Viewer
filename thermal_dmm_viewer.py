#!/usr/bin/env python3
"""
Thermal DMM Viewer - open-source, cross-platform viewer for the files saved by
thermal-imaging multimeters such as the FeelElec FR01 series and ToolTop
ET11S / ET12S / ET13S. An alternative to the vendor's Windows-only "IR-DMM"
software. Not affiliated with FeelElec or ToolTop.

https://github.com/serniko97/thermal-dmm-viewer

Pure Python 3 standard library only (tkinter for the GUI). Runs on Windows,
macOS and Linux.

What it does (everything the vendor's tool does, plus a few extras):
  * Thermal screenshots (.bmp) saved by the meter contain a hidden block of raw
    temperature data appended after the normal BMP image. This tool reads it so
    you can click / hover anywhere on the thermal image to read the temperature,
    see the hottest and coldest points, and export the data to CSV.
  * Optional sensor-grid outline and a false-colour overlay built from the
    sensor grid (several palettes, adjustable opacity), so you can see exactly
    which sensor cell you are measuring. Can be saved as PNG.
  * Multimeter recordings (.vom) are plotted as a zoomable chart and can be
    exported to CSV.
  * Opening a file from the terminal prints a summary (min, max, mean,
    median, ...); with --no-gui it works without any window.

File format (details in docs/technical.md):
  .bmp  bytes 2..5 (BMP "bfSize", little-endian) = offset of the IR block
        at that offset: uint16 LE version (200 is the only accepted value)
        then 48 x 48 uint16 LE raw values, row-major
        temperature degC = (raw - 3200) / 64
        the 48x48 grid is upscaled to 240x240 and maps to image pixels
        x = 0..239, y = 33..272 of the 240x320 screenshot
        (see README.md for a flaw in the vendor's upscaling and the options here)
  .vom  text file: line 1 header, line 2 "Mode: ...", line 3 "Cycle: <n>s",
        line 4 "Max: <n>..mV", line 5 "Min: <n>..mV", line 6 "Len: <n>",
        lines 7+ one value per line (mV)

Usage (run "python thermal_dmm_viewer.py --help" for all options):
  python thermal_dmm_viewer.py                       start the GUI
  python thermal_dmm_viewer.py FILE.bmp|FILE.vom     print a summary and open the file in the GUI
  python thermal_dmm_viewer.py FILE... --no-gui      only print the summary (min, max, mean, ...)
  python thermal_dmm_viewer.py FILE... --json        summary as JSON, for scripts
  python thermal_dmm_viewer.py FILE --no-gui --png OUT.png --grid --overlay
                                         save the view (grid / colour overlay) as PNG
  python thermal_dmm_viewer.py csv FILE [OUT.csv]    export temperatures / recording to CSV
"""

# Lets annotations like "int | None" and "list[float]" work on Python 3.8/3.9
# (they are only evaluated as strings, never at run time).
from __future__ import annotations

import csv
from array import array
from decimal import Decimal, ROUND_HALF_UP
import math
import os
import re
import struct
import sys
from dataclasses import dataclass, field

__version__ = "1.6.0"
APP_NAME = "Thermal DMM Viewer"

# Code map (top to bottom):
#   BMP decoding ............ decode_bmp()
#   Thermal data ............ interpolation (vendor / bilinear / nearest),
#                             IRImage, older colour-scale format
#                             (ColourScale, load_colour_scale), load_ir_bmp()
#   False-colour overlay .... palettes, overlay_rgb(), blend(), compose_image()
#   Rendering ............... resize_nearest(), cell_starts(), render_view(),
#                             write_png()  (shared by GUI and command line)
#   Multimeter recordings ... VomRecording, load_vom(), symlog() (log scale)
#   Command line ............ CSV export, statistics, cli()
#   GUI ..................... run_gui(): Theme, IRTab, DMMTab, menus

# ---------------------------------------------------------------------------
# Constants of the file format
# ---------------------------------------------------------------------------
IR_VERSION_V200 = 200
IR_RAW_OFFSET = 3200      # raw value at 0 degC
IR_RAW_SCALE = 64.0       # raw units per degC
IR_AREA_X = 0             # thermal area position inside the 240x320 screenshot
IR_AREA_Y = 33
IR_AREA_SIZE = 240


# ---------------------------------------------------------------------------
# BMP decoding (no Pillow needed)
# ---------------------------------------------------------------------------
class FormatError(Exception):
    pass


def _mask_shift_bits(mask: int) -> tuple[int, int]:
    """For a colour bit mask like 0xF800 return (shift, bits) = (11, 5):
    where the channel starts and how many bits wide it is.
    mask & -mask isolates the lowest set bit (two's-complement trick)."""
    if mask == 0:
        return 0, 0
    shift = (mask & -mask).bit_length() - 1
    bits = bin(mask >> shift).count("1")
    return shift, bits


_BMP16_LUT_CACHE: dict = {}


def decode_bmp(data: bytes) -> tuple[int, int, bytes]:
    """Decode an uncompressed / bitfield BMP. Returns (width, height, RGB bytes top-down).

    Written by hand so no image library is needed. Handles 1/4/8-bit
    palette, 16-bit (RGB555/RGB565), 24-bit and 32-bit images, which covers
    what the meter writes (16-bit RGB565) and anything a user might re-save."""
    # Header layout: 14-byte file header ("BM", size, reserved, pixel offset)
    # followed by an info header (40+ bytes: size, width, height, ...).
    if len(data) < 54 or data[:2] != b"BM":
        raise FormatError("not a BMP file")
    off_bits = struct.unpack_from("<I", data, 10)[0]
    hdr_size = struct.unpack_from("<I", data, 14)[0]
    if hdr_size < 40:
        raise FormatError("unsupported BMP header (OS/2 style)")
    width, height, planes, bpp, compression = struct.unpack_from("<iiHHI", data, 18)
    # BMP rows are normally stored bottom-up; a negative height means top-down.
    top_down = height < 0
    height = abs(height)
    if width <= 0 or height <= 0 or width > 10000 or height > 10000:
        raise FormatError("bad BMP dimensions")

    # Bit masks for 16/32 bpp: which bits of each pixel hold red, green, blue
    if compression == 3 or compression == 6:  # BI_BITFIELDS / BI_ALPHABITFIELDS
        # The masks are inside a V4/V5 header, or right after a 40-byte one
        if hdr_size >= 52:
            rm, gm, bm = struct.unpack_from("<III", data, 54)
        else:
            rm, gm, bm = struct.unpack_from("<III", data, 14 + hdr_size)
    elif compression == 0:
        if bpp == 16:
            rm, gm, bm = 0x7C00, 0x03E0, 0x001F  # default RGB555
        elif bpp == 32:
            rm, gm, bm = 0xFF0000, 0x00FF00, 0x0000FF
        else:
            rm = gm = bm = 0
    else:
        raise FormatError(f"compressed BMP not supported (compression={compression})")

    palette = []
    if bpp <= 8:
        clr_used = struct.unpack_from("<I", data, 46)[0] or (1 << bpp)
        pal_off = 14 + hdr_size
        for i in range(clr_used):
            b, g, r, _ = data[pal_off + 4 * i: pal_off + 4 * i + 4]
            palette.append((r, g, b))

    # Every row is padded to a multiple of 4 bytes
    row_size = ((width * bpp + 31) // 32) * 4
    if off_bits + row_size * height > len(data):
        raise FormatError("BMP pixel data truncated")

    out = bytearray(width * height * 3)
    if bpp in (16, 32):
        shifts = [_mask_shift_bits(m) for m in (rm, gm, bm)]
        masks = (rm, gm, bm)
        fmt = "<H" if bpp == 16 else "<I"
        step = bpp // 8
        # 16-bit: precompute all 65536 possible pixels -> RGB once (cached per
        # mask set), then decoding is a table lookup per pixel. Channels are
        # scaled from 5/6 bits to 8 bits with c * 255 / max.
        lut = None
        if bpp == 16:
            lut = _BMP16_LUT_CACHE.get(masks)
            if lut is None:
                lut = []
                for v in range(65536):
                    px = []
                    for m, (s, bits) in zip(masks, shifts):
                        c = (v & m) >> s
                        px.append((c * 255 // ((1 << bits) - 1)) if bits else 0)
                    lut.append(bytes(px))
                _BMP16_LUT_CACHE[masks] = lut
    for y in range(height):
        src_y = y if top_down else height - 1 - y
        row = off_bits + src_y * row_size
        o = y * width * 3
        if bpp == 24:
            for x in range(width):
                b, g, r = data[row + 3 * x: row + 3 * x + 3]   # BMP stores B, G, R
                out[o:o + 3] = bytes((r, g, b))
                o += 3
        elif bpp == 16:
            for x in range(width):
                v = data[row + 2 * x] | (data[row + 2 * x + 1] << 8)
                out[o:o + 3] = lut[v]
                o += 3
        elif bpp == 32:
            for x in range(width):
                v = struct.unpack_from(fmt, data, row + step * x)[0]
                px = []
                for m, (s, bits) in zip(masks, shifts):
                    c = (v & m) >> s
                    px.append((c * 255 // ((1 << bits) - 1)) if bits else 0)
                out[o:o + 3] = bytes(px)
                o += 3
        elif bpp in (1, 4, 8):
            # Several pixels per byte, most significant bits first
            for x in range(width):
                bit = x * bpp
                byte = data[row + bit // 8]
                idx = (byte >> (8 - bpp - bit % 8)) & ((1 << bpp) - 1)
                out[o:o + 3] = bytes(palette[idx] if idx < len(palette) else (0, 0, 0))
                o += 3
        else:
            raise FormatError(f"unsupported BMP bit depth: {bpp}")
    return width, height, bytes(out)


# ---------------------------------------------------------------------------
# Thermal data
# ---------------------------------------------------------------------------
def raw_to_celsius(raw: int) -> float:
    return (raw - IR_RAW_OFFSET) / IR_RAW_SCALE


def _f32(values):
    """Round a list of Python floats to IEEE single precision.

    Python floats are 64-bit; packing them into an array of C floats ("f")
    and reading them back rounds each one to the nearest single-precision value."""
    return array("f", values).tolist()


def interpolate_vendor(grid: list[list[float]], n: int, factor: float) -> list[list[float]]:
    """Upscale an n x n grid so the results match the vendor software
    exactly (the "Same as vendor" smoothing mode).

    It uses a bilinear blend over a 2-cell-wide window (cells int(f-1) ..
    int(f+1)), clamped at the edges, with a mix of single- and
    double-precision arithmetic; with exactly this combination the output
    matches the vendor software's to the last bit (see docs/technical.md).

    How it differs from normal bilinear interpolation: for output position f
    (in grid units) a correct version blends cells floor(f) and floor(f)+1.
    This one blends int(f-1) and int(f+1), i.e. the cells on either side, so
    at whole positions the cell itself gets zero weight and single-cell hot
    spots can disappear. See README "Smoothing modes".
    """
    factor = _f32([factor])[0]
    size = int(n * factor)
    last = n - 1
    # loop index / factor is computed in single precision, then widened
    pos = _f32([float(i) / factor for i in range(size)])

    def bounds(p):
        a = int(p - 1.0)  # int() truncates toward zero (needed for an exact match)
        b = int(p + 1.0)
        a = 0 if a < 0 else (last if a >= n else a)
        b = 0 if b < 0 else (last if b >= n else b)
        return a, b

    # The bounds only depend on the row or the column, so compute them once
    bnd = [bounds(p) for p in pos]
    out = []
    # out[i][j]: i along the
    # grid's first axis (rows) and j along the second (columns).
    for i in range(size):
        f = pos[i]
        x1, x2 = bnd[i]
        row1, row2 = grid[x1], grid[x2]
        row = []
        for j in range(size):
            g = pos[j]
            y1, y2 = bnd[j]
            den = (x2 - x1) * (y2 - y1)
            if den == 0:  # only possible for a 1-cell grid
                row.append(row1[y1])
                continue
            # Grid values are floats widened to double, the weights are
            # doubles, and the result is narrowed back to float (_f32 below)
            row.append((row1[y1] * (x2 - f) * (y2 - g)
                        + row2[y1] * (f - x1) * (y2 - g)
                        + row1[y2] * (x2 - f) * (g - y1)
                        + row2[y2] * (f - x1) * (g - y1)) / den)
        out.append(_f32(row))
    return out


def format_temp(c: float, decimals: int = 2) -> str:
    """Format a temperature the way the vendor software displays it: the
    value is first rounded to 7 significant digits, then to 2 decimals (half
    away from zero). The vendor's leading zero padding (e.g. "08.93") is not
    reproduced."""
    # Double rounding matters: 280.02499 (a float close to 280.025) shows as
    # 280.03 in the vendor software, because it first becomes "280.0250".
    d = Decimal(f"{c:.7g}").quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    if d == 0:
        d = abs(d)   # avoid printing "-0.00"
    return f"{d:.{decimals}f}"


def interpolate_bilinear(grid: list[list[float]], n: int, size: int) -> list[list[float]]:
    """Standard bilinear upscaling. Each sensor value sits at the centre of its
    block of pixels (block = size / n pixels) and every output pixel is a blend
    of the 4 nearest sensor values. Never overshoots the measured range."""
    scale = n / size

    def axis(p):
        # Position of pixel p's centre in grid units, where grid value k sits
        # at k (the centre of its block). E.g. 5 px per cell: pixel 2 -> 0.0,
        # pixel 7 -> 1.0. Clamped so edge pixels use the edge value.
        q = (p + 0.5) * scale - 0.5
        q = 0.0 if q < 0 else (n - 1.0 if q > n - 1 else q)
        i0 = int(q)
        i1 = i0 + 1 if i0 < n - 1 else i0
        return i0, i1, q - i0          # the two cells to blend and the weight

    ax = [axis(p) for p in range(size)]
    out = []
    for y in range(size):
        r0, r1, ty = ax[y]
        a, b = grid[r0], grid[r1]
        row = []
        for x in range(size):
            c0, c1, tx = ax[x]
            top = a[c0] + (a[c1] - a[c0]) * tx
            bot = b[c0] + (b[c1] - b[c0]) * tx
            row.append(top + (bot - top) * ty)
        out.append(row)
    return out


def interpolate_nearest(grid: list[list[float]], n: int, size: int) -> list[list[float]]:
    """No smoothing: every pixel shows the raw value of the sensor cell it lies in."""
    idx = [min(p * n // size, n - 1) for p in range(size)]
    return [[grid[r][c] for c in idx] for r in idx]


# Interpolation methods: key -> (label, description)
METHODS = {
    "bilinear": ("Bilinear (accurate)",
                 "Smooth blend of the 4 nearest sensor cells."),
    "vendor": ("Same as vendor software",
               "Matches the vendor software exactly, including its flaws."),
    "nearest": ("Raw sensor cells",
                "No smoothing; shows each sensor cell's own value."),
}
DEFAULT_METHOD = "bilinear"


@dataclass
class IRImage:
    path: str
    width: int
    height: int
    rgb: bytes
    has_ir: bool = False
    version: int | None = None
    ir_offset: int | None = None
    grid_n: int = 0
    grid: list[list[float]] = field(default_factory=list)      # [row][col] degC
    _temps: dict = field(default_factory=dict)                 # method -> 240x240 [row][col]
    tmin: float = 0.0
    tmax: float = 0.0
    min_pos: tuple[int, int] = (0, 0)  # (x, y) in screenshot pixels
    max_pos: tuple[int, int] = (0, 0)
    notes: list[str] = field(default_factory=list)
    # The BMP header's second "reserved" field. Normally 0; the ET13S always
    # writes 26119 (0x6607) here, the same in every file seen so far, whatever
    # the scene. Not a checksum; most likely a fixed firmware value.
    bmp_reserved2: int = 0
    # Set for the older "colour-scale" format (see load_colour_scale): no
    # sensor grid, temperatures are recovered from pixel colours instead.
    legacy: "ColourScale | None" = None

    @property
    def has_grid(self) -> bool:
        """True when temperatures come from a sensor grid (the ET13S format).
        Grid-only features - smoothing, raw cells, overlay, grid outline -
        need this; colour-scale files have temperatures but no grid."""
        return self.has_ir and self.grid_n > 0

    @property
    def area(self) -> tuple[int, int, int, int]:
        """Thermal area as (x, y, width, height) in screenshot pixels."""
        if self.legacy is not None:
            return self.legacy.area
        return IR_AREA_X, IR_AREA_Y, IR_AREA_SIZE, IR_AREA_SIZE

    def temps(self, method: str = DEFAULT_METHOD) -> list[list[float]]:
        """240x240 temperature map (one value per screen pixel) for a method."""
        if method not in self._temps:
            n = self.grid_n
            if method == "vendor":
                self._temps[method] = interpolate_vendor(self.grid, n, IR_AREA_SIZE / n)
            elif method == "nearest":
                self._temps[method] = interpolate_nearest(self.grid, n, IR_AREA_SIZE)
            elif method == "bilinear":
                self._temps[method] = interpolate_bilinear(self.grid, n, IR_AREA_SIZE)
            else:
                raise ValueError(f"unknown method {method!r}")
        return self._temps[method]

    def in_area(self, x: int, y: int) -> bool:
        ax, ay, aw, ah = self.area
        return self.has_ir and 0 <= x - ax < aw and 0 <= y - ay < ah

    def temp_at(self, x: int, y: int, method: str = DEFAULT_METHOD) -> float | None:
        """Temperature at screenshot pixel (x, y), or None outside the thermal
        area (or, for colour-scale files, where the colour isn't on the scale)."""
        if not self.in_area(x, y):
            return None
        if self.legacy is not None:
            return self.legacy.temp_at(self, x, y)
        return self.temps(method)[y - IR_AREA_Y][x - IR_AREA_X]

    def cell_at(self, x: int, y: int) -> tuple[int, int, float] | None:
        """(row, col, temperature) of the raw sensor cell containing pixel (x, y)."""
        if not self.has_grid or not self.in_area(x, y):
            return None
        n = self.grid_n
        r = min((y - IR_AREA_Y) * n // IR_AREA_SIZE, n - 1)
        c = min((x - IR_AREA_X) * n // IR_AREA_SIZE, n - 1)
        return r, c, self.grid[r][c]


# ---------------------------------------------------------------------------
# Older "colour-scale" format (EXPERIMENTAL)
# ---------------------------------------------------------------------------
# The format opened by the vendor's older PC software (2022), used with the
# earlier models - most likely the 24x32 and 90x120 ones (ToolTop ET11S /
# ET12S, FeelElec FR01A/B/C). Not yet checked against a real file from those
# meters.
#
# There is no appended data block. Instead, values are hidden in the colours
# of three pixels in the top row of the picture, each read as a 16-bit RGB565
# number:  pixel (0, 0) = minimum x 100, (239, 0) = maximum x 100,
# (238, 0) = format version. Temperatures elsewhere are recovered by finding
# a pixel's colour on the on-screen colour bar (255 rows) and interpolating
# linearly between minimum and maximum. Resolution is therefore
# (max - min) / 256, and pixels whose colour isn't on the bar (text, markers)
# have no temperature.

# Layouts by version: (thermal area x, y, w, h), colour-bar column
_COLOUR_SCALE_LAYOUTS = {
    12: ((0, 0, 214, 286), 217),      # colour bar on the right
}
_COLOUR_SCALE_DEFAULT = ((36, 24, 186, 250), 25)   # colour bar on the left
_BAR_TOP, _BAR_ROWS = 24, 255   # bar rows 24..278, read bottom (cold) to top (hot)


def rgb565(rgb: bytes, width: int, x: int, y: int) -> int:
    """A pixel's colour packed into a 16-bit RGB565 number:
    (R >> 3) << 11 | (G >> 2) << 5 | (B >> 3)."""
    o = (y * width + x) * 3
    r, g, b = rgb[o], rgb[o + 1], rgb[o + 2]
    return (r >> 3) << 11 | (g >> 2) << 5 | (b >> 3)


@dataclass
class ColourScale:
    version: int
    area: tuple[int, int, int, int]
    bar_x: int
    lookup: dict          # colour (3 bytes) -> position on the bar, 0 = coldest

    def temp_at(self, img: "IRImage", x: int, y: int) -> float | None:
        o = (y * img.width + x) * 3
        idx = self.lookup.get(img.rgb[o:o + 3])
        if idx is None:
            return None
        # min + (max-min)/256 * idx, in single precision
        step = _f32([_f32([img.tmax - img.tmin])[0] / 256.0])[0]
        return _f32([_f32([step * idx])[0] + img.tmin])[0]


def load_colour_scale(img: IRImage) -> bool:
    """Try to read the older colour-scale format into img. Returns True on
    success. Strict on purpose, so ordinary pictures aren't mistaken for
    thermal images: the image must be a 240x320 meter screenshot, the
    min/max must be within the meter's range, and most of the thermal area
    must be painted with colours from the colour bar. img is only changed
    when the file passes all checks."""
    w, h, rgb = img.width, img.height, img.rgb
    if (w, h) != (240, 320):
        return False
    version = rgb565(rgb, w, 238, 0)
    tmin = rgb565(rgb, w, 0, 0) / 100
    tmax = rgb565(rgb, w, 239, 0) / 100
    if version == 0 or not 0 <= tmin < tmax <= 600:
        return False
    area, bar_x = _COLOUR_SCALE_LAYOUTS.get(version, _COLOUR_SCALE_DEFAULT)
    # Bar position i (0 = bottom/coldest) is row 278 - i; if a colour appears
    # more than once on the bar, its lowest position counts.
    lookup: dict = {}
    for i in range(_BAR_ROWS):
        y = _BAR_TOP + _BAR_ROWS - 1 - i        # row 278 (i=0) up to row 24 (i=254)
        o = (y * w + bar_x) * 3
        lookup.setdefault(rgb[o:o + 3], i)
    if len(lookup) < 32:        # a real colour bar has many distinct colours
        return False
    # Scan the thermal area: count pixels with a bar colour, and find the
    # hottest / coldest ones (highest / lowest bar position).
    ax, ay, aw, ah = area
    matched = 0
    best_hi, best_lo = -1, 10 ** 9
    max_pos = min_pos = (ax, ay)
    for yy in range(ay, ay + ah):
        row = yy * w
        for xx in range(ax, ax + aw):
            o = (row + xx) * 3
            i = lookup.get(rgb[o:o + 3])
            if i is None:
                continue
            matched += 1
            if i > best_hi:
                best_hi, max_pos = i, (xx, yy)
            if i < best_lo:
                best_lo, min_pos = i, (xx, yy)
    # In a real screenshot nearly the whole thermal area is drawn from the
    # bar (only text and markers aren't). Random pictures match almost none.
    if matched < 0.5 * aw * ah:
        return False
    img.legacy = ColourScale(version, area, bar_x, lookup)
    img.version, img.has_ir = version, True
    img.tmin, img.tmax = _f32([tmin])[0], _f32([tmax])[0]
    img.max_pos, img.min_pos = max_pos, min_pos
    img.notes.append(
        f"Older colour-scale format (version {version}): EXPERIMENTAL, not yet "
        "tested on a real file. Temperatures "
        f"come from pixel colours, in steps of {(tmax - tmin) / 256:.2f} °C.")
    return True


def load_ir_bmp(path: str) -> IRImage:
    with open(path, "rb") as fh:
        data = fh.read()
    width, height, rgb = decode_bmp(data)
    img = IRImage(path=path, width=width, height=height, rgb=rgb,
                  bmp_reserved2=struct.unpack_from("<H", data, 8)[0])

    # The meter writes the BMP header's "file size" field as the size of the
    # picture alone, then appends the temperature block after it. So that
    # field doubles as the offset of the temperature data.
    off = struct.unpack_from("<I", data, 2)[0]
    img.ir_offset = off
    if off + 2 > len(data):
        # No appended block: maybe the older colour-scale format
        if not load_colour_scale(img):
            img.notes.append("No embedded temperature data (plain BMP).")
        return img

    version = struct.unpack_from("<H", data, off)[0]
    img.version = version
    payload = len(data) - off - 2
    n_values = payload // 2
    n = int(math.isqrt(n_values))

    if version == IR_VERSION_V200 and n_values >= 48 * 48:
        # The known format: exactly 48x48 values; anything that might follow
        # them is ignored.
        n = 48
    elif n * n == n_values and n >= 8:
        # Unknown version but the data length is a perfect square: probably a
        # grid of a different size (e.g. 192x192). Load it, with a warning.
        img.notes.append(
            f"Unknown data version {version}; interpreting {payload} bytes as a "
            f"{n}x{n} grid. Readings may be wrong.")
    else:
        img.notes.append(
            f"Unrecognised temperature block: version {version}, {payload} bytes "
            f"at offset {off}. First bytes: {data[off:off + 16].hex(' ')}")
        return img

    # n*n little-endian unsigned 16-bit values, row by row from the top-left
    raw = struct.unpack_from(f"<{n * n}H", data, off + 2)
    grid = [[raw_to_celsius(raw[r * n + c]) for c in range(n)] for r in range(n)]
    img.grid, img.grid_n, img.has_ir = grid, n, True

    if IR_AREA_SIZE % n:
        img.notes.append("Grid does not divide evenly into 240 pixels.")

    # Min / max are taken from the sensor grid; their
    # positions are reported at the centre of the corresponding grid cell.
    tmin, tmax = math.inf, -math.inf
    pmin = pmax = (0, 0)
    for r in range(n):
        for c in range(n):
            t = grid[r][c]
            if t < tmin:
                tmin, pmin = t, (r, c)
            if t > tmax:
                tmax, pmax = t, (r, c)
    cell = IR_AREA_SIZE / n
    img.tmin, img.tmax = tmin, tmax
    img.min_pos = (IR_AREA_X + int((pmin[1] + 0.5) * cell), IR_AREA_Y + int((pmin[0] + 0.5) * cell))
    img.max_pos = (IR_AREA_X + int((pmax[1] + 0.5) * cell), IR_AREA_Y + int((pmax[0] + 0.5) * cell))
    if height < IR_AREA_Y + IR_AREA_SIZE or width < IR_AREA_X + IR_AREA_SIZE:
        img.notes.append(f"Unexpected screenshot size {width}x{height}; expected 240x320.")
    return img


# ---------------------------------------------------------------------------
# False-colour overlay
# ---------------------------------------------------------------------------
# Colour stops (position 0..1, (r, g, b)), coldest first. Colours between
# stops are interpolated linearly. Two stops very close together (as in
# "Hot highlights" at 0.8 / 0.8001) make a hard edge instead of a gradient.
PALETTES = {
    "Iron": [(0, (0, 0, 0)), (0.2, (40, 0, 130)), (0.45, (180, 0, 140)),
             (0.65, (240, 70, 20)), (0.85, (255, 190, 0)), (1, (255, 255, 230))],
    "Rainbow": [(0, (0, 0, 120)), (0.15, (0, 0, 255)), (0.35, (0, 220, 255)),
                (0.5, (0, 220, 0)), (0.7, (255, 255, 0)), (0.85, (255, 80, 0)), (1, (230, 0, 0))],
    "White hot": [(0, (0, 0, 0)), (1, (255, 255, 255))],
    "Black hot": [(0, (255, 255, 255)), (1, (0, 0, 0))],
    "Hot highlights": [(0, (0, 0, 0)), (0.8, (200, 200, 200)), (0.8001, (255, 0, 0)),
                       (1, (255, 255, 0))],
    "Viridis": [(0, (68, 1, 84)), (0.25, (59, 82, 139)), (0.5, (33, 145, 140)),
                (0.75, (94, 201, 98)), (1, (253, 231, 37))],
    "Cold-hot": [(0, (20, 60, 220)), (0.5, (245, 245, 245)), (1, (220, 30, 30))],
}


def make_lut(name: str) -> list[bytes]:
    """256-entry colour table for a palette, as 3-byte RGB strings so they
    can be joined directly into image rows."""
    stops = PALETTES[name]
    lut = []
    for i in range(256):
        p = i / 255
        for k in range(len(stops) - 1):
            (p0, c0), (p1, c1) = stops[k], stops[k + 1]
            if p <= p1 or k == len(stops) - 2:
                t = 0.0 if p1 == p0 else min(max((p - p0) / (p1 - p0), 0.0), 1.0)
                lut.append(bytes(round(a + (b - a) * t) for a, b in zip(c0, c1)))
                break
    return lut


def overlay_rgb(img: IRImage, lut: list[bytes], lo: float, hi: float,
                smooth_method: str | None = None) -> bytes:
    """240x240 false-colour RGB image of the temperature data.
    smooth_method=None gives one flat block per sensor cell (pixelated)."""
    span = (hi - lo) or 1.0

    def colour(t):
        # Map lo..hi to palette index 0..255 (clamped: with a locked colour
        # range, temperatures can fall outside it)
        i = int((t - lo) / span * 255 + 0.5)
        return lut[0 if i < 0 else (255 if i > 255 else i)]

    size = IR_AREA_SIZE
    if smooth_method:
        return b"".join(b"".join(colour(t) for t in row) for row in img.temps(smooth_method))
    n = img.grid_n
    # cols[x] = which sensor column pixel x belongs to (same rule as cell_at)
    cols = [min(x * n // size, n - 1) for x in range(size)]
    out = []
    for y in range(size):
        r = min(y * n // size, n - 1)
        if y and r == min((y - 1) * n // size, n - 1):
            out.append(out[-1])   # same sensor row as the previous line: reuse it
            continue
        cell_colours = [colour(t) for t in img.grid[r]]
        out.append(b"".join(cell_colours[c] for c in cols))
    return b"".join(out)


def blend(base: bytes, top: bytes, opacity: float) -> bytes:
    """Alpha-blend two equally sized byte strings: base*(1-opacity) + top*opacity.

    Uses big-integer arithmetic so it runs at C speed without numpy:
    each byte is spread into its own 16-bit "lane" of one huge integer
    (bytes at even positions, zeros between), so a single multiplication
    scales every byte at once. The largest lane value is 255 * 256 = 65280,
    which still fits in 16 bits, so lanes never overflow into each other.
    The high byte of each lane is the blended value divided by 256."""
    a = int(round(min(max(opacity, 0.0), 1.0) * 256))
    if a == 0:
        return bytes(base)
    if a == 256:
        return bytes(top)
    n = len(base)
    wb = bytearray(2 * n)
    wt = bytearray(2 * n)
    wb[0::2] = base
    wt[0::2] = top
    mixed = (int.from_bytes(wb, "little") * (256 - a) + int.from_bytes(wt, "little") * a)
    return bytes(mixed.to_bytes(2 * n, "little")[1::2])   # high byte of each lane


def compose_image(img: IRImage, overlay: bytes | None, opacity: float) -> bytes:
    """The screenshot with the overlay blended into its thermal area.

    The thermal area's rows are cut out of the full image into one block,
    blended in a single call (fast), then written back row by row."""
    if overlay is None or not img.has_grid or opacity <= 0:
        return img.rgb
    w = img.width
    rows = []
    rgb = img.rgb
    start = IR_AREA_Y * w * 3
    for y in range(IR_AREA_SIZE):
        o = start + y * w * 3 + IR_AREA_X * 3
        rows.append(rgb[o:o + IR_AREA_SIZE * 3])
    mixed = blend(b"".join(rows), overlay, opacity)
    out = bytearray(rgb)
    step = IR_AREA_SIZE * 3
    for y in range(IR_AREA_SIZE):
        o = start + y * w * 3 + IR_AREA_X * 3
        out[o:o + step] = mixed[y * step:(y + 1) * step]
    return bytes(out)


# ---------------------------------------------------------------------------
# Rendering (shared by the GUI and the command line)
# ---------------------------------------------------------------------------
# Grid lines are drawn by remapping each colour channel c to 191 - c/2: light on
# dark areas, dark on bright areas, so they stay visible on any palette.
# bytes.translate() applies this table to a whole slice at C speed.
_GRID_LUT = bytes(191 - c // 2 for c in range(256))


def output_size(width: int, height: int, scale: float) -> tuple[int, int]:
    """Size of an image of width x height after scaling. The tiny epsilon stops
    float noise (e.g. 2.9999999) from losing a pixel at exact scales."""
    return max(1, int(width * scale + 1e-9)), max(1, int(height * scale + 1e-9))


def source_index(out_pos: int, scale: float) -> int:
    """Nearest-neighbour mapping used everywhere (rendering, mouse, markers):
    output pixel -> source pixel. Kept in one place so they always agree."""
    return int(out_pos / scale + 1e-9)


def cell_starts(scale: float, n: int, origin: int, limit: int,
                length: int = IR_AREA_SIZE) -> list[int]:
    """Output-pixel positions where each of the n sensor cells starts along one
    axis, plus one extra entry for the end of the thermal area.

    Computed by scanning the same nearest-neighbour mapping the renderer uses,
    so grid lines and cell outlines land exactly on the scaled cell borders
    even for fractional scales (where cells end up 1 pixel wider or narrower).
    limit is the output image size along this axis."""
    starts = [None] * (n + 1)
    end = 0
    out = 0
    while out < limit:
        src = source_index(out, scale)
        if src >= origin + length:
            break
        if src >= origin:
            cell = (src - origin) * n // length
            if starts[cell] is None:
                starts[cell] = out
            end = out + 1
        out += 1
    starts[n] = end
    # Very small scales can skip a cell entirely; give it its neighbour's start
    for k in range(n - 1, -1, -1):
        if starts[k] is None:
            starts[k] = starts[k + 1]
    return starts


def resize_nearest(rgb: bytes, w: int, h: int, scale: float) -> tuple[int, int, bytearray]:
    """Nearest-neighbour resize of packed RGB bytes (crisp pixels, no blur).

    Speed trick: each source row is split into 3-byte pixels once, then
    operator.itemgetter picks the output pixels in a single C-level call.
    Output rows that come from the same source row reuse the same bytes."""
    from operator import itemgetter
    W, H = output_size(w, h, scale)
    pick = itemgetter(*[min(source_index(X, scale), w - 1) for X in range(W)])
    stride = w * 3
    cache: dict[int, bytes] = {}
    rows = []
    for Y in range(H):
        y = min(source_index(Y, scale), h - 1)
        row = cache.get(y)
        if row is None:
            src = rgb[y * stride:(y + 1) * stride]
            pixels = [src[i:i + 3] for i in range(0, stride, 3)]
            got = pick(pixels)
            row = b"".join(got) if W > 1 else got
            cache[y] = row
        rows.append(row)
    return W, H, bytearray(b"".join(rows))


def render_view(img: IRImage, overlay: bytes | None, opacity: float,
                scale: float = 2, grid: bool = False) -> tuple[int, int, bytes]:
    """Final RGB image as shown on screen: overlay blended in, scaled by any
    factor (nearest neighbour), optionally with 1-pixel sensor grid lines."""
    rgb = compose_image(img, overlay, opacity)
    W, H, out = resize_nearest(rgb, img.width, img.height, scale)
    if grid and img.has_grid:
        n = img.grid_n
        xs = cell_starts(scale, n, IR_AREA_X, W)
        ys = cell_starts(scale, n, IR_AREA_Y, H)
        # One line at the start of every cell, plus the last pixel of the area
        xlines = sorted(set(xs[:-1] + [xs[-1] - 1]))
        ylines = sorted(set(ys[:-1] + [ys[-1] - 1]))
        stride = W * 3
        x0, x1 = xs[0], xs[-1]
        for Y in ylines:  # horizontal lines: one contiguous slice per line
            o = Y * stride
            out[o + x0 * 3:o + x1 * 3] = out[o + x0 * 3:o + x1 * 3].translate(_GRID_LUT)
        top, bottom = ys[0] * stride, ys[-1] * stride
        line_rows = [Y - ys[0] for Y in ylines]
        for X in xlines:  # vertical lines: an extended slice steps down a column
            for ch in range(3):
                o = top + X * 3 + ch
                col = out[o:bottom:stride]
                new = bytearray(col.translate(_GRID_LUT))
                for q in line_rows:  # crossings were already inverted by the
                    new[q] = col[q]  # horizontal pass; don't apply it twice
                out[o:bottom:stride] = new
    return W, H, bytes(out)


def write_png(path: str, width: int, height: int, rgb: bytes) -> None:
    """Write 8-bit RGB pixels as a PNG file (standard library only)."""
    import zlib

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    # PNG = signature + chunks (length, type, data, CRC). Each image row is
    # prefixed with a filter-type byte; 0 means "no filter".
    stride = width * 3
    raw = b"".join(b"\x00" + rgb[y * stride:(y + 1) * stride] for y in range(height))
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")
        # 8 bits per channel, colour type 2 (RGB), default compression/filter, no interlace
        fh.write(chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)))
        fh.write(chunk(b"IDAT", zlib.compress(raw, 9)))
        fh.write(chunk(b"IEND", b""))


# ---------------------------------------------------------------------------
# Multimeter recordings (.vom)
# ---------------------------------------------------------------------------
_NUM = re.compile(r"[-+]?\d+(?:[.,]\d+)?(?:[eE][-+]?\d+)?")


def _num(s: str) -> float | None:
    """First number in a string ("Max: 4924.5 mV" -> 4924.5). A decimal comma
    is accepted too, in case a file was edited on a PC that uses decimal
    commas."""
    m = _NUM.search(s)
    return float(m.group(0).replace(",", ".")) if m else None


@dataclass
class VomRecording:
    path: str
    header: str = ""
    mode: str = ""
    cycle: float = 1.0          # seconds between samples
    max_mv: float | None = None
    min_mv: float | None = None
    length: int | None = None
    values_mv: list[float] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # True when the file used the meter's "<value>.<sample number>" quirk and
    # the numbers after the dot were discarded (see load_vom).
    index_suffix: bool = False

    @property
    def in_volts(self) -> bool:
        # Show volts when Max > 1000 mV, like the vendor software
        top = self.max_mv if self.max_mv is not None else max(self.values_mv, default=0)
        return top > 1000

    def display_values(self) -> list[float]:
        div = 1000.0 if self.in_volts else 1.0
        return [v / div for v in self.values_mv]


def load_vom(path: str) -> VomRecording:
    """Parse a .vom recording. The header lines are identified by their line
    number, and the number in each is found wherever it is in the line, so
    small changes in spacing don't break reading."""
    rec = VomRecording(path=path)
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines()    # handles the meter's CRLF line ends
    raw_lines: list[tuple[int, str]] = []
    for i, line in enumerate(lines, start=1):
        s = line.strip()
        if i == 1:
            rec.header = s
        elif i == 2 and "Mode:" in s:
            rec.mode = s.split("Mode:", 1)[1].strip()
        elif i == 3 and "Cycle:" in s:
            v = _num(s.split("Cycle:", 1)[1])
            if v is not None:
                rec.cycle = v
        elif i == 4 and "Max:" in s:
            rec.max_mv = _num(s.split("Max:", 1)[1])
        elif i == 5 and "Min:" in s:
            rec.min_mv = _num(s.split("Min:", 1)[1])
        elif i == 6 and "Len:" in s:
            v = _num(s.split("Len:", 1)[1])
            rec.length = int(v) if v is not None else None
        elif i > 6 and s:
            raw_lines.append((i, s))

    # Firmware quirk: the meter writes each sample as "<whole mV>.<sample
    # number>", e.g. sample 10 at 3044 mV is "3044.10", so the digits after
    # the dot are a counter, not a decimal. Confirmed on a real ET13S file:
    # the counter matched on every line, and the whole-number parts matched
    # the header's Max/Min exactly. (The vendor software reads them as
    # decimals, so it shows e.g. -4.8 mV for a -4 mV sample.)
    # Only strip the counter when EVERY line follows the pattern, so a file
    # with genuine decimals is never truncated.
    def split_counter(text):
        whole, dot, frac = text.partition(".")
        return whole, frac if dot else None

    rec.index_suffix = bool(raw_lines) and all(
        split_counter(s)[1] == str(k) and re.fullmatch(r"[-+]?\d+", split_counter(s)[0])
        for k, (_, s) in enumerate(raw_lines))
    for i, s in raw_lines:
        v = float(split_counter(s)[0]) if rec.index_suffix else _num(s)
        if v is None:
            rec.notes.append(f"Line {i} skipped: {s[:40]!r}")
        else:
            rec.values_mv.append(v)
    if rec.length is not None and rec.length != len(rec.values_mv):
        rec.notes.append(f"Header says {rec.length} points but file has {len(rec.values_mv)}.")
    if not rec.values_mv:
        raise FormatError("no data points found in this .vom file")
    return rec


# Symmetric log scale for the recording chart. A plain log scale can't show 0 V
# or negative readings, and these recordings are full of both. A symmetric log
# ("symlog") scale is logarithmic for large values but linear within ±C of 0:
#     y = sign(v) · log10(1 + |v| / C)
# With C = one measurement step (1 mV): 0 stays 0, ±1 mV map to ±0.3,
# ±10 mV to ±1, ±100 mV to ±2, ±1 V to ±3 - one unit per decade.
def symlog(v: float, c: float) -> float:
    return math.copysign(math.log10(1 + abs(v) / c), v)


def symlog_threshold(rec: "VomRecording") -> float:
    """C in display units: 1 mV, or a finer power of ten if the recording has
    non-zero readings smaller than 1 mV (e.g. a millivolt range)."""
    tiny = [abs(v) for v in rec.values_mv if 0 < abs(v) < 1]
    c_mv = 10 ** math.floor(math.log10(min(tiny))) if tiny else 1.0
    return c_mv / 1000 if rec.in_volts else c_mv


def symlog_ticks(lo: float, hi: float, c: float) -> list[float]:
    """Tick values for a symlog axis showing lo..hi (in symlog coordinates):
    0 and ±C·10^k, plus ±2× and ±5× steps when only a couple of decades fit."""
    mults = (1,) if hi - lo > 2.5 else (1, 2, 5)
    vals = [0.0] + [sign * m * c * 10 ** k
                    for k in range(13) for m in mults for sign in (1, -1)]
    return sorted({v for v in vals if lo <= symlog(v, c) <= hi})


# ---------------------------------------------------------------------------
# CSV export / command line
# ---------------------------------------------------------------------------
def export_ir_csv(img: IRImage, out: str, raw_grid: bool = False,
                  method: str = DEFAULT_METHOD) -> None:
    if img.legacy is not None:
        # No grid: one value per pixel of the thermal area; blank where the
        # pixel's colour isn't on the colour bar (text, markers)
        ax, ay, aw, ah = img.area
        rows = [[img.temp_at(x, y) for x in range(ax, ax + aw)] for y in range(ay, ay + ah)]
    else:
        rows = img.grid if raw_grid else img.temps(method)
    with open(out, "w", newline="") as fh:
        w = csv.writer(fh)
        for row in rows:
            w.writerow(["" if t is None else format_temp(t) for t in row])


def export_vom_csv(rec: VomRecording, out: str) -> None:
    with open(out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["time_s", "value_mV"])
        for i, v in enumerate(rec.values_mv):
            w.writerow([f"{i * rec.cycle:.3f}", v])


def _median(vals: list[float]) -> float:
    v = sorted(vals)
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def _percentile(sorted_vals: list[float], p: float) -> float:
    """Linear-interpolated percentile (same as numpy's default)."""
    k = (len(sorted_vals) - 1) * p / 100
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def ir_stats(img: IRImage) -> dict:
    """Summary statistics of a thermal image, computed on the raw sensor grid."""
    info = {
        "file": img.path,
        "file_size": os.path.getsize(img.path) if os.path.exists(img.path) else None,
        "image_size": [img.width, img.height],
        "ir_offset": img.ir_offset,
        "ir_version": img.version,
        "bmp_reserved2": img.bmp_reserved2,
        "has_temperature_data": img.has_ir,
        "notes": img.notes,
    }
    if not img.has_ir:
        return info
    if img.legacy is not None:
        return _colour_scale_stats(img, info)
    vals = [t for row in img.grid for t in row]
    sv = sorted(vals)
    mean = sum(vals) / len(vals)
    std = math.sqrt(sum((t - mean) ** 2 for t in vals) / len(vals))
    cx, cy = IR_AREA_X + IR_AREA_SIZE // 2, IR_AREA_Y + IR_AREA_SIZE // 2
    r, c, t = img.cell_at(cx, cy)
    info.update({
        "grid_size": [img.grid_n, img.grid_n],
        "cell_size_px": IR_AREA_SIZE / img.grid_n,
        "thermal_area_px": [IR_AREA_X, IR_AREA_Y, IR_AREA_SIZE, IR_AREA_SIZE],
        "min": img.tmin, "min_pixel": list(img.min_pos), "min_cell": list(img.cell_at(*img.min_pos)[:2]),
        "max": img.tmax, "max_pixel": list(img.max_pos), "max_cell": list(img.cell_at(*img.max_pos)[:2]),
        "mean": mean,
        "median": _median(vals),
        "std_dev": std,
        "p5": _percentile(sv, 5),
        "p95": _percentile(sv, 95),
        "range": img.tmax - img.tmin,
        "centre_pixel": [cx, cy],
        "centre_cell": [r, c],
        "centre_raw": t,
        "centre_bilinear": img.temp_at(cx, cy, "bilinear"),
        "centre_vendor": img.temp_at(cx, cy, "vendor"),
    })
    return info


def _colour_scale_stats(img: IRImage, info: dict) -> dict:
    """Statistics for a colour-scale file, over every pixel of the thermal
    area whose colour is on the colour bar."""
    ax, ay, aw, ah = img.area
    vals = []
    for y in range(ay, ay + ah):
        for x in range(ax, ax + aw):
            t = img.temp_at(x, y)
            if t is not None:
                vals.append(t)
    cx, cy = ax + aw // 2, ay + ah // 2
    info.update({
        "format": "colour-scale (experimental)",
        "thermal_area_px": [ax, ay, aw, ah],
        "colour_bar_x": img.legacy.bar_x,
        "temperature_step": (img.tmax - img.tmin) / 256,
        "min": img.tmin, "min_pixel": list(img.min_pos),
        "max": img.tmax, "max_pixel": list(img.max_pos),
        "pixels_with_temperature": len(vals),
        "centre_pixel": [cx, cy],
        "centre": img.temp_at(cx, cy),
    })
    if vals:
        sv = sorted(vals)
        mean = sum(vals) / len(vals)
        info.update({
            "mean": mean, "median": _median(vals),
            "std_dev": math.sqrt(sum((t - mean) ** 2 for t in vals) / len(vals)),
            "p5": _percentile(sv, 5), "p95": _percentile(sv, 95),
            "range": img.tmax - img.tmin,
        })
    return info


def vom_stats(rec: VomRecording) -> dict:
    v = rec.values_mv
    mean = sum(v) / len(v)
    return {
        "file": rec.path, "header": rec.header, "mode": rec.mode,
        "interval_s": rec.cycle, "points": len(v), "header_points": rec.length,
        "duration_s": (len(v) - 1) * rec.cycle,
        "min_mV": min(v), "max_mV": max(v), "mean_mV": mean, "median_mV": _median(v),
        "std_dev_mV": math.sqrt(sum((x - mean) ** 2 for x in v) / len(v)),
        "header_min_mV": rec.min_mv, "header_max_mV": rec.max_mv,
        "sample_counter_suffix": rec.index_suffix,
        "notes": rec.notes,
    }


def _ft(c: float, unit: str) -> str:
    if unit == "F":
        return f"{format_temp(c * 9 / 5 + 32)} °F"
    return f"{format_temp(c)} °C"


def print_ir_summary(img: IRImage, unit: str = "C", points=(), method: str = DEFAULT_METHOD) -> None:
    st = ir_stats(img)
    w, h = st["image_size"]
    size = st["file_size"]
    print(f"File:          {img.path}" + (f"  ({size:,} bytes)" if size else ""))
    print(f"Image:         {w}x{h} px")
    if not img.has_ir:
        print("Temperature:   none in this file")
        for n in img.notes:
            print(f"Note:          {n}")
        return
    if img.legacy is not None:
        _print_colour_scale_summary(img, st, unit, points)
        return
    n = img.grid_n
    print(f"Data format:   version {img.version}, {n}x{n} sensor grid "
          f"({st['cell_size_px']:g}x{st['cell_size_px']:g} px per cell)")
    print(f"Thermal area:  x {IR_AREA_X}-{IR_AREA_X + IR_AREA_SIZE - 1}, "
          f"y {IR_AREA_Y}-{IR_AREA_Y + IR_AREA_SIZE - 1}")
    if img.bmp_reserved2:
        print(f"Unknown field: {img.bmp_reserved2} (0x{img.bmp_reserved2:04x}, BMP header bytes 8-9)")
    print("Statistics over all sensor cells:")
    print(f"  Max:         {_ft(st['max'], unit):>12}   cell {tuple(st['max_cell'])}, pixel {tuple(st['max_pixel'])}")
    print(f"  Min:         {_ft(st['min'], unit):>12}   cell {tuple(st['min_cell'])}, pixel {tuple(st['min_pixel'])}")
    print(f"  Mean:        {_ft(st['mean'], unit):>12}")
    print(f"  Median:      {_ft(st['median'], unit):>12}")
    print(f"  Std dev:     {format_temp(st['std_dev'] * (1.8 if unit == 'F' else 1)):>9} °{unit}")
    print(f"  5th / 95th:  {_ft(st['p5'], unit):>12} / {_ft(st['p95'], unit)}")
    print(f"Centre {tuple(st['centre_pixel'])}:")
    print(f"  Raw cell:    {_ft(st['centre_raw'], unit):>12}   cell {tuple(st['centre_cell'])}")
    print(f"  Bilinear:    {_ft(st['centre_bilinear'], unit):>12}")
    print(f"  Vendor:      {_ft(st['centre_vendor'], unit):>12}")
    for (x, y) in points:
        if not img.in_area(x, y):
            print(f"Point ({x}, {y}): outside the thermal area")
            continue
        r, c, t = img.cell_at(x, y)
        print(f"Point ({x}, {y}): {_ft(img.temp_at(x, y, method), unit)} ({METHODS[method][0]}), "
              f"raw cell ({r}, {c}) {_ft(t, unit)}")
    for note in img.notes:
        print(f"Note:          {note}")


def _print_colour_scale_summary(img: IRImage, st: dict, unit: str, points) -> None:
    ax, ay, aw, ah = st["thermal_area_px"]
    print(f"Data format:   older colour-scale format, version {img.version} (EXPERIMENTAL)")
    print(f"Thermal area:  x {ax}-{ax + aw - 1}, y {ay}-{ay + ah - 1}, "
          f"colour bar at x {st['colour_bar_x']}")
    print(f"Resolution:    {format_temp(st['temperature_step'])} °C per colour step")
    print(f"Statistics over {st['pixels_with_temperature']:,} pixels with a temperature colour:")
    print(f"  Max:         {_ft(st['max'], unit):>12}   pixel {tuple(st['max_pixel'])}")
    print(f"  Min:         {_ft(st['min'], unit):>12}   pixel {tuple(st['min_pixel'])}")
    if "mean" in st:
        print(f"  Mean:        {_ft(st['mean'], unit):>12}")
        print(f"  Median:      {_ft(st['median'], unit):>12}")
    c = st["centre"]
    print(f"Centre {tuple(st['centre_pixel'])}: "
          + (_ft(c, unit) if c is not None else "no temperature colour"))
    for (x, y) in points:
        t = img.temp_at(x, y)
        where = ("outside the thermal area" if not img.in_area(x, y) else
                 "no temperature colour" if t is None else _ft(t, unit))
        print(f"Point ({x}, {y}): {where}")
    for note in img.notes:
        print(f"Note:          {note}")


def print_vom_summary(rec: VomRecording) -> None:
    st = vom_stats(rec)
    print(f"File:          {rec.path}")
    print(f"Mode:          {rec.mode or '-'}")
    print(f"Samples:       {st['points']} every {rec.cycle:g} s  ({st['duration_s']:g} s total)")
    print(f"Max:           {st['max_mV']:g} mV")
    print(f"Min:           {st['min_mV']:g} mV")
    print(f"Mean:          {st['mean_mV']:.4g} mV")
    print(f"Median:        {st['median_mV']:.4g} mV")
    print(f"Std dev:       {st['std_dev_mV']:.4g} mV")
    if rec.index_suffix:
        print("Note:          sample numbers after the decimal point were removed "
              "(meter firmware quirk)")
    for note in rec.notes:
        print(f"Note:          {note}")


USAGE = """\
Usage:
  python thermal_dmm_viewer.py [FILES...] [options]       open files in the GUI and print a summary
  python thermal_dmm_viewer.py csv FILE [OUT.csv] [--raw] [--method M]

Options:
  -n, --no-gui       only print the summary, don't open the window
  --json             print the summary as JSON (implies --no-gui)
  --at X,Y           also print the temperature at screenshot pixel X,Y (repeatable)
  --method M         smoothing for --at and csv: bilinear (default), vendor, nearest
  -F, --fahrenheit   print temperatures in °F
  -q, --quiet        don't print summaries when using the window (at start-up
                     or when opening files in it)
  --theme T          window style: auto (default: native on Windows/macOS,
                     GNOME-like on Linux following its dark mode), native,
                     light or dark (flat GNOME-like style on any OS)
  -h, --help         this help
  -V, --version      program version

Image export (works without the GUI):
  --png OUT.png      save the image as PNG; with several input files use a
                     name containing {name}, e.g. --png "{name}_view.png"
  --grid             draw the sensor grid outline
  --overlay          blend in the false-colour sensor grid
  --palette NAME     overlay colours: PALETTE_LIST
  --opacity N        overlay opacity in percent (default 70)
  --smooth-overlay   smooth the overlay instead of showing flat cells
  --scale N          enlarge the PNG N times (default 2)

"info FILES..." is the same as "FILES... --no-gui".
""".replace("PALETTE_LIST", "\n                       ".join(
    ", ".join(list(PALETTES)[i:i + 4]) for i in range(0, len(PALETTES), 4)))


def cli(argv: list[str], gui: dict) -> int | None:
    """Handle the command line.

    Returns an exit code when the program should stop here, or None when the
    window should open; in that case the files to show and the theme are
    left in the gui dict ({"files": [...], "theme": "..."})."""
    gui.setdefault("files", [])
    gui.setdefault("theme", "auto")
    gui.setdefault("quiet", False)
    if argv and argv[0] in ("-h", "--help", "help"):
        print(__doc__.split("Usage")[0].rstrip() + "\n")
        print(USAGE)
        return 0
    if argv and argv[0] in ("-V", "--version"):
        print(__version__)
        return 0

    mode = "open"
    if argv and argv[0] in ("csv", "info"):
        mode, argv = argv[0], argv[1:]
    # A small hand-written parser rather than argparse: argparse can't easily
    # mix a list of files with an optional leading sub-command ("csv"/"info")
    # the way this tool's command line works.
    files, points = [], []
    opts = {"no_gui": mode == "info", "json": False, "raw": False, "quiet": False,
            "unit": "C", "method": DEFAULT_METHOD, "png": None, "grid": False,
            "overlay": False, "palette": "Iron", "opacity": 70.0, "smooth_overlay": False,
            "scale": 2}

    def value(a, it):
        """Option value given as "--opt=value" or as the next argument."""
        return a.split("=", 1)[1] if "=" in a else next(it, "")
    it = iter(argv)
    for a in it:
        if a in ("-n", "--no-gui"):
            opts["no_gui"] = True
        elif a == "--json":
            opts["json"] = opts["no_gui"] = True
        elif a == "--raw":
            opts["raw"] = True
        elif a in ("-q", "--quiet"):
            opts["quiet"] = True
            gui["quiet"] = True
        elif a in ("-F", "--fahrenheit"):
            opts["unit"] = "F"
        elif a == "--theme" or a.startswith("--theme="):
            t = value(a, it).lower()
            if t not in ("auto", "native", "light", "dark"):
                print("--theme must be auto, native, light or dark", file=sys.stderr)
                return 2
            gui["theme"] = t
        elif a == "--grid":
            opts["grid"] = True
        elif a == "--overlay":
            opts["overlay"] = True
        elif a == "--smooth-overlay":
            opts["smooth_overlay"] = opts["overlay"] = True
        elif a == "--png" or a.startswith("--png="):
            opts["png"] = value(a, it)
        elif a == "--palette" or a.startswith("--palette="):
            name = value(a, it)
            def norm(t):
                return re.sub(r"[\s_-]", "", t.lower())
            match = [p for p in PALETTES if norm(p) == norm(name)]
            if not match:
                print(f"--palette must be one of: {', '.join(PALETTES)}", file=sys.stderr)
                return 2
            opts["palette"] = match[0]
        elif a in ("--opacity", "--scale") or a.startswith(("--opacity=", "--scale=")):
            key = a.lstrip("-").split("=")[0]
            try:
                opts[key] = float(value(a, it)) if key == "opacity" else int(value(a, it))
            except ValueError:
                print(f"--{key} expects a number", file=sys.stderr)
                return 2
        elif a == "--method" or a.startswith("--method="):
            opts["method"] = a.split("=", 1)[1] if "=" in a else next(it, "")
        elif a == "--at" or a.startswith("--at="):
            v = a.split("=", 1)[1] if "=" in a else next(it, "")
            try:
                x, y = (int(p) for p in v.replace(" ", "").split(","))
            except ValueError:
                print(f"--at expects X,Y (pixels), got {v!r}", file=sys.stderr)
                return 2
            points.append((x, y))
        elif a.startswith("-") and len(a) > 1:
            print(f"Unknown option {a}\n\n{USAGE}", file=sys.stderr)
            return 2
        elif any(ch in a for ch in "*?[") and not os.path.exists(a):
            # Windows shells don't expand wildcards, so do it here
            import glob
            matches = sorted(glob.glob(a))
            if not matches:
                print(f"No files match {a}", file=sys.stderr)
            files.extend(matches)
        else:
            files.append(a)
    if opts["method"] not in METHODS:
        print(f"--method must be one of: {', '.join(METHODS)}", file=sys.stderr)
        return 2

    if mode == "csv":
        if not files:
            print("csv: missing input file (see --help)", file=sys.stderr)
            return 2
        src = files[0]
        out = files[1] if len(files) > 1 else os.path.splitext(src)[0] + ".csv"
        try:
            if src.lower().endswith(".vom"):
                export_vom_csv(load_vom(src), out)
            else:
                img = load_ir_bmp(src)
                if not img.has_ir:
                    print("; ".join(img.notes) or "no temperature data", file=sys.stderr)
                    return 1
                export_ir_csv(img, out, raw_grid=opts["raw"], method=opts["method"])
        except (OSError, FormatError) as e:
            print(f"{src}: {e}", file=sys.stderr)
            return 1
        print(f"Wrote {out}")
        return 0

    if opts["png"] is not None:
        if not opts["png"]:
            print("--png needs an output file name", file=sys.stderr)
            return 2
        bmps = [f for f in files if not f.lower().endswith(".vom")]
        if len(bmps) > 1 and "{name}" not in opts["png"]:
            print('--png with several images needs {name} in the file name, '
                  'e.g. --png "{name}_view.png"', file=sys.stderr)
            return 2
        if not 1 <= opts["scale"] <= 16:
            print("--scale must be between 1 and 16", file=sys.stderr)
            return 2

    if opts["no_gui"] and not files:
        print("No files given (see --help)", file=sys.stderr)
        return 2

    rc = 0
    results = []
    for i, p in enumerate(files):
        try:
            if p.lower().endswith(".vom"):
                rec = load_vom(p)
                if opts["json"]:
                    results.append(vom_stats(rec))
                elif not opts["quiet"]:
                    print_vom_summary(rec)
            else:
                img = load_ir_bmp(p)
                if not img.has_ir:
                    rc = 1
                if opts["json"]:
                    st = ir_stats(img)
                    st["points"] = []
                    for x, y in points:
                        cell = img.cell_at(x, y)
                        st["points"].append({
                            "pixel": [x, y], "method": opts["method"],
                            "temperature": img.temp_at(x, y, opts["method"]),
                            "raw_cell": list(cell[:2]) if cell else None,
                            "raw_temperature": cell[2] if cell else None})
                    results.append(st)
                elif not opts["quiet"]:
                    print_ir_summary(img, opts["unit"], points, opts["method"])
                if opts["png"]:
                    out = opts["png"].replace("{name}", os.path.splitext(os.path.basename(p))[0])
                    ov = None
                    if opts["overlay"] and img.has_grid:
                        ov = overlay_rgb(img, make_lut(opts["palette"]), img.tmin, img.tmax,
                                         opts["method"] if opts["smooth_overlay"] else None)
                    w, h, rgb = render_view(img, ov, opts["opacity"] / 100, opts["scale"],
                                            opts["grid"])
                    write_png(out, w, h, rgb)
                    print(f"Wrote {out}", file=sys.stderr if opts["json"] else sys.stdout)
            gui["files"].append(p)
        except BrokenPipeError:   # output closed early (e.g. piped into "head")
            raise
        except (OSError, FormatError, ValueError) as e:
            print(f"{p}: {e}", file=sys.stderr)
            rc = 1
        if not opts["json"] and not opts["quiet"] and i < len(files) - 1:
            print()
    if opts["json"]:
        import json
        print(json.dumps(results if len(results) != 1 else results[0], indent=2, ensure_ascii=False))
    if opts["no_gui"]:
        return rc
    return None


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
def run_gui(open_paths: list[str], theme_choice: str = "auto", quiet: bool = False) -> int:
    # tkinter is imported here, not at the top, so the command-line mode keeps
    # working on systems without tkinter or without a display.
    try:
        import tkinter as tk
        import tkinter.font as tkfont
        from tkinter import ttk, filedialog, messagebox
    except ImportError:
        v = f"{sys.version_info.major}.{sys.version_info.minor}"
        if sys.platform == "darwin":
            how = ("  python.org installer: includes tkinter and Tk - recommended\n"
                   f"  MacPorts:  sudo port install py{v.replace('.', '')}-tkinter\n"
                   f"  Homebrew:  brew install python-tk@{v}")
        elif sys.platform == "win32":
            how = ("  Re-run the python.org installer, choose Modify, and tick\n"
                   "  \"tcl/tk and IDLE\".")
        else:
            how = ("  Debian/Ubuntu:  sudo apt install python3-tk\n"
                   "  Fedora:         sudo dnf install python3-tkinter\n"
                   "  Arch:           sudo pacman -S tk")
        print(f"tkinter (needed for the window) is not available in this Python "
              f"({sys.executable}).\nTo get it:\n{how}\n"
              "The command-line mode still works: python thermal_dmm_viewer.py --help",
              file=sys.stderr)
        return 1

    if sys.platform == "win32":
        try:  # crisp text on high-DPI Windows screens (must run before Tk starts)
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    IS_MAC = sys.platform == "darwin"
    MOD = "Command" if IS_MAC else "Control"   # Cmd+O on macOS, Ctrl+O elsewhere

    # =======================================================================
    # Theme
    # =======================================================================
    # Two strategies:
    #  * "native": Windows and macOS ship real native Tk themes ("vista",
    #    "aqua") whose widgets are drawn by the OS. We use them untouched and
    #    only add fonts / text colours, so they keep looking (and behaving)
    #    like the platform.
    #  * "flat": Linux has no native Tk theme (Tk doesn't use GTK), so we build
    #    a GNOME/libadwaita-like flat theme on top of the built-in "clam"
    #    theme, in light or dark. It can also be forced on any OS (--theme).
    class Theme:
        LIGHT = dict(
            bg="#fafafa", card="#ffffff", fg="#2e2e2e", muted="#77767b",
            border="#dcdcdc", button="#ebebeb", button_hover="#dfdfdf",
            button_pressed="#d2d2d2", accent="#3584e4", accent_hover="#2b78d6",
            accent_pressed="#1c62b8", accent_fg="#ffffff", field="#ffffff",
            view="#ebebeb", chart_bg="#ffffff", chart_grid="#ededed",
            chart_axis="#c0bfbc", chart_line="#3584e4", hot="#c01c28",
            cold="#1a5fb4", warn="#c64600", dark=False)
        DARK = dict(
            bg="#242424", card="#303030", fg="#ffffff", muted="#9a9996",
            border="#3c3c3c", button="#3a3a3a", button_hover="#454545",
            button_pressed="#505050", accent="#3584e4", accent_hover="#4a91e8",
            accent_pressed="#2a6fc4", accent_fg="#ffffff", field="#1e1e1e",
            view="#1b1b1b", chart_bg="#2a2a2a", chart_grid="#363636",
            chart_axis="#5e5c64", chart_line="#78aeed", hot="#ff7b63",
            cold="#78aeed", warn="#ffa348", dark=True)

        def __init__(self, root, choice: str):
            self.root = root
            self.style = ttk.Style(root)
            names = self.style.theme_names()
            native = {"win32": "vista", "darwin": "aqua"}.get(sys.platform)
            if choice in ("auto", "native") and native in names:
                self.mode = "native"
                self.style.theme_use(native)
                self.c = dict(self.DARK if self._native_is_dark() else self.LIGHT)
                # In native mode the window colours come from the OS; ask the
                # theme for them instead of using our palette.
                bg = self.style.lookup("TFrame", "background") or root.cget("background")
                self.c.update(bg=bg, card=bg)
            else:
                dark = choice == "dark" or (choice in ("auto", "native") and self._system_prefers_dark())
                self.mode = "flat"
                self.c = dict(self.DARK if dark else self.LIGHT)
            self._setup_fonts()
            if self.mode == "flat":
                self._setup_flat()
            else:
                self._setup_native()

        # --- detection -----------------------------------------------------
        def _native_is_dark(self) -> bool:
            # macOS dark mode: aqua's window colour is dark. winfo_rgb resolves
            # system colour names to 16-bit RGB.
            try:
                bg = self.style.lookup("TFrame", "background") or self.root.cget("background")
                r, g, b = self.root.winfo_rgb(bg)
                return (r + g + b) / 3 < 0x8000
            except tk.TclError:
                return False

        @staticmethod
        def _system_prefers_dark() -> bool:
            """GNOME / GTK dark-mode preference (Linux). Best effort, never fails."""
            if "dark" in os.environ.get("GTK_THEME", "").lower():
                return True
            import shutil
            import subprocess
            if not shutil.which("gsettings"):
                return False
            for key in ("color-scheme", "gtk-theme"):
                try:
                    out = subprocess.run(
                        ["gsettings", "get", "org.gnome.desktop.interface", key],
                        capture_output=True, text=True, timeout=1).stdout.lower()
                except Exception:
                    continue
                if "dark" in out:
                    return True
            return False

        # --- fonts -----------------------------------------------------------
        def _setup_fonts(self):
            base = tkfont.nametofont("TkDefaultFont")
            if self.mode == "flat":
                # Prefer the desktop's UI font; Tk on Linux defaults to DejaVu.
                families = set(tkfont.families(self.root))
                for fam in ("Cantarell", "Inter", "Adwaita Sans", "Noto Sans", "Ubuntu",
                            "Segoe UI", "SF Pro Text", "DejaVu Sans"):
                    if fam in families:
                        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont",
                                     "TkHeadingFont", "TkCaptionFont", "TkTooltipFont"):
                            try:
                                tkfont.nametofont(name).configure(family=fam, size=10)
                            except tk.TclError:
                                pass
                        break
            # Tk font sizes can be negative (= pixels); actual() gives points.
            size = abs(base.actual("size")) or 10
            fam = base.actual("family")
            self.f_small = tkfont.Font(self.root, family=fam, size=max(size - 1, 8))
            self.f_bold = tkfont.Font(self.root, family=fam, size=size, weight="bold")
            self.f_heading = tkfont.Font(self.root, family=fam, size=size + 1, weight="bold")
            self.f_value = tkfont.Font(self.root, family=fam, size=size + 7, weight="bold")
            self.f_value_small = tkfont.Font(self.root, family=fam, size=size + 3, weight="bold")

        # --- native (Windows / macOS) ---------------------------------------
        def _setup_native(self):
            s, c = self.style, self.c
            muted = "#8e8e93" if c["dark"] else "#6e6e73"
            c["muted"] = muted
            # "Card.*" styles inherit everything from the native base styles;
            # only fonts and text colours are added on top.
            s.configure("Muted.TLabel", foreground=muted)
            s.configure("Muted.Card.TLabel", foreground=muted)
            s.configure("Small.Muted.Card.TLabel", foreground=muted, font=self.f_small)
            s.configure("Heading.TLabel", font=self.f_heading)
            s.configure("Value.Card.TLabel", font=self.f_value)
            s.configure("Hot.Value.Card.TLabel", font=self.f_value, foreground=c["hot"])
            s.configure("Cold.Value.Card.TLabel", font=self.f_value, foreground=c["cold"])
            s.configure("SmallValue.Card.TLabel", font=self.f_value_small)
            s.configure("Bold.Card.TLabel", font=self.f_bold)
            s.configure("Warn.Card.TLabel", foreground=c["warn"])
            s.configure("Card.TFrame")
            s.configure("CardInner.TFrame")
            s.configure("Toolbar.TFrame")

        # --- flat GNOME-like theme (Linux, or forced) -----------------------
        def _setup_flat(self):
            s, c = self.style, self.c
            s.theme_use("clam")
            self.root.configure(background=c["bg"])
            # clam draws 3-D bevels with light/dark colours; setting them to
            # the fill colour everywhere gives flat widgets.
            s.configure(".", background=c["bg"], foreground=c["fg"], bordercolor=c["border"],
                        lightcolor=c["bg"], darkcolor=c["bg"], troughcolor=c["button"],
                        focuscolor=c["accent"], selectbackground=c["accent"],
                        selectforeground=c["accent_fg"], fieldbackground=c["field"],
                        insertcolor=c["fg"], arrowcolor=c["fg"], font="TkDefaultFont")
            s.map(".", foreground=[("disabled", c["muted"])])

            s.configure("TFrame", background=c["bg"])
            s.configure("Toolbar.TFrame", background=c["bg"])
            s.configure("Card.TFrame", background=c["card"], bordercolor=c["border"],
                        relief="solid", borderwidth=1)
            # plain frames placed inside a card (no border of their own)
            s.configure("CardInner.TFrame", background=c["card"], borderwidth=0)
            s.configure("TSeparator", background=c["border"])

            for name, bg in (("TLabel", c["bg"]), ("Card.TLabel", c["card"])):
                s.configure(name, background=bg, foreground=c["fg"])
            s.configure("Muted.TLabel", foreground=c["muted"])
            s.configure("Heading.TLabel", font=self.f_heading)
            s.configure("Muted.Card.TLabel", foreground=c["muted"])
            s.configure("Small.Muted.Card.TLabel", foreground=c["muted"], font=self.f_small)
            s.configure("Value.Card.TLabel", font=self.f_value)
            s.configure("Hot.Value.Card.TLabel", font=self.f_value, foreground=c["hot"])
            s.configure("Cold.Value.Card.TLabel", font=self.f_value, foreground=c["cold"])
            s.configure("SmallValue.Card.TLabel", font=self.f_value_small)
            s.configure("Bold.Card.TLabel", font=self.f_bold)
            s.configure("Warn.Card.TLabel", foreground=c["warn"])

            def button(name, bg, hover, pressed, fg):
                s.configure(name, background=bg, foreground=fg, bordercolor=bg,
                            lightcolor=bg, darkcolor=bg, padding=(12, 5), relief="flat",
                            focusthickness=0, anchor="center")
                states = [("disabled", c["bg"] if bg == c["bg"] else c["button"]),
                          ("pressed", pressed), ("active", hover)]
                for opt in ("background", "bordercolor", "lightcolor", "darkcolor"):
                    s.map(name, **{opt: states})
                s.map(name, foreground=[("disabled", c["muted"])])

            button("TButton", c["button"], c["button_hover"], c["button_pressed"], c["fg"])
            button("Accent.TButton", c["accent"], c["accent_hover"], c["accent_pressed"],
                   c["accent_fg"])
            button("Flat.TButton", c["bg"], c["button_hover"], c["button_pressed"], c["fg"])

            # Check boxes / radio buttons: square accent-coloured indicator
            for name, bg in (("TCheckbutton", c["bg"]), ("Card.TCheckbutton", c["card"]),
                             ("TRadiobutton", c["bg"]), ("Card.TRadiobutton", c["card"])):
                s.configure(name, background=bg, foreground=c["fg"], padding=(2, 3),
                            indicatorbackground=c["field"], indicatorforeground=c["accent_fg"],
                            upperbordercolor=c["muted"], lowerbordercolor=c["muted"],
                            indicatormargin=(0, 0, 8, 0), focusthickness=0)
                s.map(name, background=[("active", bg)],
                      indicatorbackground=[("selected", c["accent"]), ("active", c["button"])],
                      upperbordercolor=[("selected", c["accent"])],
                      lowerbordercolor=[("selected", c["accent"])])

            # Segmented buttons (°C / °F): Toolbutton-style radio buttons
            s.configure("Seg.Toolbutton", background=c["button"], foreground=c["fg"],
                        bordercolor=c["button"], lightcolor=c["button"], darkcolor=c["button"],
                        padding=(14, 4), anchor="center", relief="flat", focusthickness=0)
            s.map("Seg.Toolbutton",
                  background=[("selected", c["accent"]), ("active", c["button_hover"])],
                  bordercolor=[("selected", c["accent"])],
                  lightcolor=[("selected", c["accent"])], darkcolor=[("selected", c["accent"])],
                  foreground=[("selected", c["accent_fg"])])

            # Combobox: flat button look when read-only (we only use read-only)
            s.configure("TCombobox", fieldbackground=c["button"], background=c["button"],
                        bordercolor=c["button"], lightcolor=c["button"], darkcolor=c["button"],
                        arrowcolor=c["fg"], foreground=c["fg"], padding=(8, 4), arrowsize=12)
            s.map("TCombobox",
                  fieldbackground=[("readonly", "active", c["button_hover"]), ("readonly", c["button"])],
                  background=[("active", c["button_hover"])],
                  bordercolor=[("focus", c["accent"])],
                  selectbackground=[("readonly", c["button"])],
                  selectforeground=[("disabled", c["muted"]), ("readonly", c["fg"])],
                  # first matching state wins, so "disabled" must come first
                  foreground=[("disabled", c["muted"]), ("readonly", c["fg"])])
            # The drop-down list is a classic Tk listbox: style it via the
            # option database (must happen before any combobox is opened).
            for opt, val in (("background", c["card"]), ("foreground", c["fg"]),
                             ("selectBackground", c["accent"]), ("selectForeground", c["accent_fg"]),
                             ("font", "TkDefaultFont"), ("borderWidth", 0)):
                self.root.option_add(f"*TCombobox*Listbox.{opt}", val)

            # Slider: thin trough, accent-coloured handle
            for name, bg in (("Horizontal.TScale", c["bg"]), ("Card.Horizontal.TScale", c["card"])):
                s.configure(name, background=c["accent"], troughcolor=c["button"],
                            bordercolor=bg, lightcolor=c["accent"], darkcolor=c["accent"],
                            sliderlength=16, sliderthickness=16, gripcount=0, troughrelief="flat")
                s.map(name, background=[("active", c["accent_hover"])])

            # Notebook tabs, GNOME "view switcher" style
            s.configure("TNotebook", background=c["bg"], bordercolor=c["bg"],
                        lightcolor=c["bg"], darkcolor=c["bg"], tabmargins=(10, 8, 10, 0))
            s.configure("TNotebook.Tab", background=c["bg"], foreground=c["muted"],
                        bordercolor=c["bg"], lightcolor=c["bg"], darkcolor=c["bg"],
                        padding=(16, 6), font=self.f_bold, focusthickness=0)
            s.map("TNotebook.Tab",
                  background=[("selected", c["button"]), ("active", c["button_hover"])],
                  bordercolor=[("selected", c["button"])],
                  lightcolor=[("selected", c["button"])], darkcolor=[("selected", c["button"])],
                  foreground=[("selected", c["fg"])], expand=[("selected", (0, 0, 0, 0))])

            # Classic Tk menus (menu bar, drop-downs) use the option database too
            for opt, val in (("background", c["card"]), ("foreground", c["fg"]),
                             ("activeBackground", c["accent"]), ("activeForeground", c["accent_fg"]),
                             ("selectColor", c["fg"]), ("relief", "flat"), ("borderWidth", 1),
                             ("activeBorderWidth", 0), ("disabledForeground", c["muted"])):
                self.root.option_add(f"*Menu.{opt}", val)

    # =======================================================================
    # Small building blocks
    # =======================================================================
    def make_card(parent, title: str):
        """A titled group of controls. Returns (outer_frame, body_frame).

        Flat theme: heading above a bordered white/grey box (GNOME preference
        group). Native theme: just a bold heading over plain content, which
        looks at home on Windows and macOS without mixing background shades."""
        outer = ttk.Frame(parent)
        ttk.Label(outer, text=title, style="Heading.TLabel").pack(anchor="w", pady=(0, 6))
        if theme.mode == "flat":
            body = ttk.Frame(outer, style="Card.TFrame", padding=(14, 10))
        else:
            body = ttk.Frame(outer, style="Card.TFrame", padding=(2, 0))
        body.pack(fill="both", expand=True)
        return outer, body

    def card_separator(parent, row, columnspan=2):
        ttk.Separator(parent).grid(row=row, column=0, columnspan=columnspan,
                                   sticky="ew", pady=8)

    def announce(print_summary, *args):
        """Print a file's summary to the terminal when the window opens a file
        (Open…, Previous/Next), so the terminal keeps a log like
        the one printed at start-up. Skipped with --quiet, and when there's no
        console to print to (e.g. started with pythonw on Windows)."""
        if quiet or sys.stdout is None:
            return
        try:
            print()
            print_summary(*args)
            sys.stdout.flush()
        except (OSError, ValueError):   # console closed or not writable
            pass

    def fmt_temp(c: float | None, unit: str) -> str:
        if c is None:
            return "–"
        if unit == "°F":
            return f"{format_temp(c * 9 / 5 + 32)} °F"
        return f"{format_temp(c)} °C"

    # =======================================================================
    # Thermal image tab
    # =======================================================================
    class IRTab(ttk.Frame):
        CELL_COLOUR = "#ffd60a"     # outline of the clicked / hovered sensor cell
        PAD = 12                    # gap around the image inside its area

        def __init__(self, master):
            super().__init__(master, padding=(12, 10, 12, 8))
            c = theme.c
            self.img: IRImage | None = None
            self.photo = None               # Tk needs a Python reference to the image
            self.image_item = None
            self.scale = 2.0                # current zoom (output px per source px)
            self.offset = (0, 0)            # where the image sits on the canvas
            self.xs = self.ys = None        # cell_starts() for the current zoom
            self.click_xy: tuple[int, int] | None = None
            self.hover_cell: tuple[int, int] | None = None
            self._overlay_cache: dict = {}
            self._render_job = None
            self.locked_range: tuple[float, float] | None = None

            # ---- settings, shared with the menu bar ----
            self.unit = tk.StringVar(self, value="°C")
            self.method_labels = {v[0]: k for k, v in METHODS.items()}
            self.method_var = tk.StringVar(self, value=METHODS[DEFAULT_METHOD][0])
            self.show_markers = tk.BooleanVar(self, value=True)
            self.show_grid = tk.BooleanVar(self, value=False)
            self.show_overlay = tk.BooleanVar(self, value=False)
            self.overlay_smooth = tk.BooleanVar(self, value=False)
            self.palette = tk.StringVar(self, value="Iron")
            self.opacity = tk.DoubleVar(self, value=70.0)
            self.lock_range = tk.BooleanVar(self, value=False)

            # trace_add fires whenever a variable changes, whether from the
            # panel, the menu or code, so every control stays in sync.
            self.unit.trace_add("write", lambda *a: self.refresh_labels())
            self.method_var.trace_add("write", lambda *a: self.on_method_change())
            self.show_markers.trace_add("write", lambda *a: self.draw_markers())
            for v in (self.show_grid, self.show_overlay, self.overlay_smooth, self.palette):
                v.trace_add("write", lambda *a: self.render())
            self.opacity.trace_add("write", lambda *a: self.render_soon())
            self.lock_range.trace_add("write", lambda *a: self.on_lock_range())

            # ---- header bar ----
            bar = ttk.Frame(self, style="Toolbar.TFrame")
            bar.pack(fill="x", pady=(0, 10))
            ttk.Button(bar, text="Open…", style="Accent.TButton",
                       command=self.ask_open).pack(side="left")
            self.export_btn = ttk.Button(bar, text="Export CSV…", command=self.export,
                                         state="disabled")
            self.export_btn.pack(side="left", padx=(8, 0))
            self.png_btn = ttk.Button(bar, text="Save PNG…", command=self.export_png,
                                      state="disabled")
            self.png_btn.pack(side="left", padx=(8, 0))
            self.next_btn = ttk.Button(bar, text="›", width=3, command=lambda: self.step(1),
                                       state="disabled")
            self.next_btn.pack(side="right")
            self.prev_btn = ttk.Button(bar, text="‹", width=3, command=lambda: self.step(-1),
                                       state="disabled")
            self.prev_btn.pack(side="right", padx=(0, 6))
            self.path_var = tk.StringVar(self, value="No file open")
            ttk.Label(bar, textvariable=self.path_var, style="Muted.TLabel").pack(
                side="right", padx=12)

            # ---- body: image (grows with the window) | readings | display ----
            body = ttk.Frame(self)
            body.pack(fill="both", expand=True)
            body.columnconfigure(0, weight=1)
            body.rowconfigure(0, weight=1)

            self.canvas = tk.Canvas(body, background=c["view"], highlightthickness=0,
                                    cursor="crosshair", width=240, height=320)
            self.canvas.grid(row=0, column=0, sticky="nsew")
            self.canvas.bind("<Configure>", lambda e: self.render_soon())
            self.canvas.bind("<Button-1>", self.on_click)
            self.canvas.bind("<Motion>", self.on_motion)
            self.canvas.bind("<Leave>", self.on_leave)

            readings_outer, rd = make_card(body, "Readings")
            # sticky "new": cards keep their natural height instead of stretching
            readings_outer.grid(row=0, column=1, sticky="new", padx=(14, 0))
            display_outer, dp = make_card(body, "Display")
            display_outer.grid(row=0, column=2, sticky="new", padx=(14, 0))
            self._build_readings(rd)
            self._build_display(dp)

            self.hint = ttk.Label(self, style="Muted.TLabel", text=(
                "Click to pin a point   ·   G grid   ·   O overlay   ·   ← → browse folder"))
            self.hint.pack(anchor="w", pady=(8, 0))
            self.draw_placeholder()

        # ---- panels ----
        def _build_readings(self, rd):
            rd.columnconfigure(0, weight=1, minsize=210)
            self.vars = {k: tk.StringVar(self, value="–") for k in (
                "max", "min", "click", "cell", "hover", "pos", "ver", "note")}
            row = 0

            def item(label, key, style, sub=None):
                nonlocal row
                ttk.Label(rd, text=label, style="Muted.Card.TLabel").grid(
                    row=row, column=0, sticky="w")
                ttk.Label(rd, textvariable=self.vars[key], style=style).grid(
                    row=row + 1, column=0, sticky="w")
                row += 2
                if sub:
                    ttk.Label(rd, textvariable=self.vars[sub], style="Small.Muted.Card.TLabel").grid(
                        row=row, column=0, sticky="w")
                    row += 1
                rd.rowconfigure(row, minsize=8)   # empty row = vertical spacing
                row += 1

            item("Highest", "max", "Hot.Value.Card.TLabel")
            item("Lowest", "min", "Cold.Value.Card.TLabel")
            card_separator(rd, row, 1); row += 1
            item("Clicked point", "click", "Value.Card.TLabel", sub="cell")
            item("Under cursor", "hover", "SmallValue.Card.TLabel", sub="pos")
            card_separator(rd, row, 1); row += 1
            ttk.Label(rd, textvariable=self.vars["ver"], style="Small.Muted.Card.TLabel").grid(
                row=row, column=0, sticky="w"); row += 1
            ttk.Label(rd, textvariable=self.vars["note"], style="Warn.Card.TLabel",
                      wraplength=210, justify="left").grid(row=row, column=0, sticky="w", pady=(6, 0))

        def _build_display(self, dp):
            dp.columnconfigure(1, weight=1)
            # Controls that only make sense with a sensor grid; greyed out for
            # colour-scale files (see update_grid_controls)
            self.grid_controls = []
            r = 0
            ttk.Label(dp, text="Unit", style="Card.TLabel").grid(row=r, column=0, sticky="w", pady=3)
            seg = ttk.Frame(dp, style="CardInner.TFrame")
            seg.grid(row=r, column=1, sticky="w", pady=3)
            # Flat theme: segmented buttons. Native themes: plain radio buttons,
            # because macOS's toolbutton style barely shows which one is selected.
            seg_style = "Seg.Toolbutton" if theme.mode == "flat" else "Card.TRadiobutton"
            for u in ("°C", "°F"):
                ttk.Radiobutton(seg, text=u, value=u, variable=self.unit,
                                style=seg_style).pack(side="left", padx=(0, 0 if theme.mode == "flat" else 10))
            r += 1
            ttk.Label(dp, text="Smoothing", style="Card.TLabel").grid(row=r, column=0, sticky="w",
                                                                     pady=3, padx=(0, 12))
            cb = ttk.Combobox(dp, textvariable=self.method_var, values=list(self.method_labels),
                              width=21, state="readonly")
            cb.grid(row=r, column=1, sticky="ew", pady=3)
            self.grid_controls.append(cb)
            r += 1
            card_separator(dp, r); r += 1
            for text, var in (("Max / min markers", self.show_markers),
                              ("Sensor grid outline", self.show_grid),
                              ("Sensor grid overlay", self.show_overlay)):
                chk = ttk.Checkbutton(dp, text=text, variable=var, style="Card.TCheckbutton")
                chk.grid(row=r, column=0, columnspan=2, sticky="w")
                if var is not self.show_markers:
                    self.grid_controls.append(chk)
                r += 1
            card_separator(dp, r); r += 1
            ttk.Label(dp, text="Overlay", style="Bold.Card.TLabel").grid(
                row=r, column=0, columnspan=2, sticky="w", pady=(0, 4))
            r += 1
            ttk.Label(dp, text="Colours", style="Card.TLabel").grid(row=r, column=0, sticky="w", pady=3)
            pal = ttk.Combobox(dp, textvariable=self.palette, values=list(PALETTES), width=14,
                               state="readonly")
            pal.grid(row=r, column=1, sticky="ew", pady=3)
            self.grid_controls.append(pal)
            r += 1
            ttk.Label(dp, text="Opacity", style="Card.TLabel").grid(row=r, column=0, sticky="w", pady=3)
            of = ttk.Frame(dp, style="CardInner.TFrame")
            of.grid(row=r, column=1, sticky="ew", pady=3)
            of.columnconfigure(0, weight=1)
            sc = ttk.Scale(of, from_=0, to=100, variable=self.opacity,
                           style="Card.Horizontal.TScale")
            sc.grid(row=0, column=0, sticky="ew")
            self.grid_controls.append(sc)
            self.opacity_label = ttk.Label(of, width=5, anchor="e", style="Card.TLabel")
            self.opacity_label.grid(row=0, column=1, padx=(6, 0))
            r += 1
            # The colour bar is drawn on a canvas; its width follows the panel.
            self.scale_canvas = tk.Canvas(dp, width=200, height=40, highlightthickness=0,
                                          background=theme.c["card"])
            self.scale_canvas.grid(row=r, column=0, columnspan=2, sticky="ew", pady=(6, 4))
            self.scale_canvas.bind("<Configure>", lambda e: self.draw_scale())
            r += 1
            for text, var in (("Smooth overlay", self.overlay_smooth),
                              ("Lock colour range", self.lock_range)):
                chk = ttk.Checkbutton(dp, text=text, variable=var, style="Card.TCheckbutton")
                chk.grid(row=r, column=0, columnspan=2, sticky="w")
                self.grid_controls.append(chk)
                r += 1
            ttk.Label(dp, text="Lock keeps the same colours for the same\n"
                               "temperatures while browsing images.",
                      style="Small.Muted.Card.TLabel", justify="left").grid(
                row=r, column=0, columnspan=2, sticky="w", pady=(2, 0))
            self.update_opacity_label()

        def update_grid_controls(self):
            """Grey out grid-only controls when the file has no sensor grid.
            A readonly combobox must go back to "readonly", not "normal"."""
            enable = not (self.img and self.img.has_ir and not self.img.has_grid)
            for w in self.grid_controls:
                if isinstance(w, ttk.Combobox):
                    w.configure(state="readonly" if enable else "disabled")
                else:
                    w.state(["!disabled"] if enable else ["disabled"])

        # ---- settings helpers ----
        @property
        def method(self) -> str:
            return self.method_labels.get(self.method_var.get(), DEFAULT_METHOD)

        def update_opacity_label(self):
            self.opacity_label.config(text=f"{self.opacity.get():.0f} %")

        def colour_range(self):
            if self.locked_range:
                return self.locked_range
            return self.img.tmin, self.img.tmax

        def on_lock_range(self):
            if self.lock_range.get():
                if self.img and self.img.has_grid:
                    self.locked_range = (self.img.tmin, self.img.tmax)
            else:
                self.locked_range = None
            self.render()

        def on_method_change(self):
            if self.overlay_smooth.get():
                self.render()
            self.refresh_labels()

        # ---- files ----
        def ask_open(self):
            p = filedialog.askopenfilename(
                title="Open thermal image",
                filetypes=[("Thermal images", "*.bmp *.BMP"), ("All files", "*.*")])
            if p:
                self.load(p)

        def folder_files(self):
            if not self.img:
                return []
            d = os.path.dirname(os.path.abspath(self.img.path))
            try:
                names = sorted(n for n in os.listdir(d) if n.lower().endswith(".bmp"))
            except OSError:
                return []
            return [os.path.join(d, n) for n in names]

        def step(self, delta):
            files = self.folder_files()
            if not files:
                return
            cur = os.path.abspath(self.img.path)
            i = files.index(cur) if cur in files else -1
            j = i + delta
            if 0 <= j < len(files):
                self.load(files[j], keep_click=True)

        def update_nav(self):
            files = self.folder_files()
            cur = os.path.abspath(self.img.path) if self.img else None
            i = files.index(cur) if cur in files else -1
            self.prev_btn.config(state="normal" if i > 0 else "disabled")
            self.next_btn.config(state="normal" if 0 <= i < len(files) - 1 else "disabled")
            if i >= 0:
                self.path_var.set(f"{os.path.basename(cur)}  ·  {i + 1} of {len(files)}")

        def load(self, path: str, keep_click: bool = False, announce_it: bool = True):
            try:
                img = load_ir_bmp(path)
            except (OSError, FormatError) as e:
                messagebox.showerror("Could not open file", f"{path}\n\n{e}")
                return
            self.img = img
            self._overlay_cache.clear()
            if self.lock_range.get() and self.locked_range is None and img.has_grid:
                self.locked_range = (img.tmin, img.tmax)
            # Keep the pinned point when browsing, so the same spot can be compared
            if not (keep_click and self.click_xy and img.in_area(*self.click_xy)):
                self.click_xy = None
            self.path_var.set(os.path.basename(path))
            self.export_btn.config(state="normal" if img.has_ir else "disabled")
            self.png_btn.config(state="normal")
            self.vars["note"].set("\n".join(img.notes) if img.notes else
                                  ("" if img.has_ir else "This image has no temperature data."))
            self.winfo_toplevel().title(f"{os.path.basename(path)} – {APP_NAME}")
            self.update_nav()
            self.update_grid_controls()
            self.render()
            self.refresh_labels()
            if announce_it:
                announce(print_ir_summary, img, "F" if self.unit.get() == "°F" else "C",
                         (), self.method)

        # ---- drawing ----
        def render_soon(self):
            """Coalesce bursts of events (slider drags, window resizes) into one
            render ~30 ms later, so the UI stays responsive."""
            self.update_opacity_label()
            if self._render_job is None:
                self._render_job = self.after(30, self.render)

        def get_overlay(self):
            img = self.img
            lo, hi = self.colour_range()
            smooth = self.method if self.overlay_smooth.get() else None
            key = (self.palette.get(), smooth, lo, hi)
            if key not in self._overlay_cache:
                if len(self._overlay_cache) > 8:
                    self._overlay_cache.clear()
                self._overlay_cache[key] = overlay_rgb(img, make_lut(self.palette.get()), lo, hi, smooth)
            return self._overlay_cache[key]

        def draw_placeholder(self):
            c = self.canvas
            c.delete("all")
            self.image_item = None
            w, h = max(c.winfo_width(), 200), max(c.winfo_height(), 200)
            c.create_text(w // 2, h // 2 - 10, text="No thermal image open",
                          fill=theme.c["muted"], font=theme.f_heading)
            c.create_text(w // 2, h // 2 + 14, text="File ▸ Open, or press Ctrl+O" if not IS_MAC
                          else "File ▸ Open, or press ⌘O", fill=theme.c["muted"])

        def render(self):
            self._render_job = None
            self.update_opacity_label()
            img = self.img
            if not img:
                self.draw_placeholder()
                return
            cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
            if cw < 20 or ch < 20:   # not laid out yet; <Configure> will call again
                return
            # Largest zoom that fits the area, never below 0.5x
            scale = max(min((cw - 2 * self.PAD) / img.width, (ch - 2 * self.PAD) / img.height), 0.5)
            overlay_on = self.show_overlay.get() and img.has_grid
            W, H, rgb = render_view(img, self.get_overlay() if overlay_on else None,
                                    self.opacity.get() / 100, scale, self.show_grid.get())
            self.scale = scale
            self.offset = ((cw - W) // 2, (ch - H) // 2)
            self.xs = self.ys = None
            if img.has_grid:
                self.xs = cell_starts(scale, img.grid_n, IR_AREA_X, W)
                self.ys = cell_starts(scale, img.grid_n, IR_AREA_Y, H)
            # Tk can read binary PPM ("P6") straight from memory: the fastest
            # way to get raw RGB into a PhotoImage without Pillow.
            self.photo = tk.PhotoImage(master=self, data=b"P6 %d %d 255\n" % (W, H) + rgb,
                                        format="PPM")
            self.canvas.delete("all")
            self.image_item = self.canvas.create_image(*self.offset, image=self.photo, anchor="nw")
            self.draw_markers()
            self.draw_scale()

        def draw_scale(self):
            c = self.scale_canvas
            c.delete("all")
            w = c.winfo_width() - 2
            if w < 20 or not self.img or not self.img.has_grid:
                return
            lut = make_lut(self.palette.get())
            # one 1-px line per column is plenty fast for ~200 columns
            for x in range(w):
                col = lut[x * 255 // max(w - 1, 1)]
                c.create_line(x + 1, 2, x + 1, 18, fill="#%02x%02x%02x" % tuple(col))
            c.create_rectangle(1, 2, w, 18, outline=theme.c["border"])
            lo, hi = self.colour_range()
            u = self.unit.get()
            c.create_text(1, 22, text=fmt_temp(lo, u), anchor="nw", fill=theme.c["muted"],
                          font=theme.f_small)
            c.create_text(w, 22, text=fmt_temp(hi, u), anchor="ne", fill=theme.c["muted"],
                          font=theme.f_small)
            if self.locked_range:
                c.create_text(w // 2, 22, text="locked", anchor="n", fill=theme.c["muted"],
                              font=theme.f_small)

        # Coordinate helpers: canvas <-> screenshot pixels, using the same
        # nearest-neighbour mapping as the renderer.
        def to_canvas(self, x: float, y: float) -> tuple[float, float]:
            """Centre of screenshot pixel (x, y) on the canvas."""
            ox, oy = self.offset
            return ox + (x + 0.5) * self.scale, oy + (y + 0.5) * self.scale

        def from_canvas(self, cx: int, cy: int) -> tuple[int, int] | None:
            ox, oy = self.offset
            if cx < ox or cy < oy:
                return None
            x, y = source_index(cx - ox, self.scale), source_index(cy - oy, self.scale)
            if x >= self.img.width or y >= self.img.height:
                return None
            return x, y

        def cell_rect(self, r, c):
            """Canvas rectangle covering sensor cell (r, c) exactly."""
            ox, oy = self.offset
            return (ox + self.xs[c], oy + self.ys[r], ox + self.xs[c + 1], oy + self.ys[r + 1])

        def _marker(self, x, y, colour, label):
            cx, cy = self.to_canvas(x, y)
            r = 7
            self.canvas.create_oval(cx - r, cy - r, cx + r, cy + r, outline="white", width=4, tags="marker")
            self.canvas.create_oval(cx - r, cy - r, cx + r, cy + r, outline=colour, width=2, tags="marker")
            self.canvas.create_text(cx + 11, cy - 11, text=label, fill=colour, anchor="w",
                                    font=theme.f_bold, tags="marker")

        def draw_markers(self):
            c = self.canvas
            c.delete("marker")
            img = self.img
            if not img or not img.has_ir:
                return
            if self.show_markers.get():
                self._marker(*img.max_pos, "#ff3b30", "MAX")
                self._marker(*img.min_pos, "#0a84ff", "MIN")
            if self.click_xy:
                g = 4
                if img.has_grid and self.xs is not None:
                    r, cc, _ = img.cell_at(*self.click_xy)
                    x0, y0, x1, y1 = self.cell_rect(r, cc)
                    c.create_rectangle(x0, y0, x1, y1, outline="black", width=3, tags="marker")
                    c.create_rectangle(x0, y0, x1, y1, outline=self.CELL_COLOUR, width=1, tags="marker")
                    # crosshair arms start outside the cell so its outline stays visible
                    g = max((x1 - x0) / 2 + 2, 4)
                cx, cy = self.to_canvas(*self.click_xy)
                for col, w in (("black", 4), ("white", 2)):
                    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                        c.create_line(cx + dx * g, cy + dy * g, cx + dx * (g + 9), cy + dy * (g + 9),
                                      fill=col, width=w, tags="marker")
            self.draw_hover_cell()

        def draw_hover_cell(self):
            self.canvas.delete("hovercell")
            if self.hover_cell and self.img and self.img.has_grid and self.xs and \
                    (self.show_grid.get() or self.show_overlay.get()):
                x0, y0, x1, y1 = self.cell_rect(*self.hover_cell)
                self.canvas.create_rectangle(x0, y0, x1, y1, outline=self.CELL_COLOUR,
                                             width=2, tags="hovercell")

        # ---- readings ----
        def refresh_labels(self):
            u = self.unit.get()
            img = self.img
            self.draw_scale()
            if not img or not img.has_ir:
                for k in ("max", "min", "click", "cell"):
                    self.vars[k].set("–")
                self.vars["ver"].set(f"Data format: version {img.version}"
                                     if img and img.version is not None else "")
                return
            self.vars["max"].set(fmt_temp(img.tmax, u))
            self.vars["min"].set(fmt_temp(img.tmin, u))
            if img.legacy is not None:
                self.vars["ver"].set(f"Colour-scale format v{img.version} (experimental)")
            else:
                self.vars["ver"].set(f"Data v{img.version} · {img.grid_n}×{img.grid_n} sensor grid")
            if self.click_xy:
                t = img.temp_at(*self.click_xy, self.method)
                self.vars["click"].set(fmt_temp(t, u))
                cell = img.cell_at(*self.click_xy)
                if cell:
                    self.vars["cell"].set(f"raw cell [{cell[0]}, {cell[1]}]  {fmt_temp(cell[2], u)}")
                else:
                    self.vars["cell"].set("from the pixel colour" if t is not None
                                          else "no temperature colour here")
            else:
                self.vars["click"].set("–")
                self.vars["cell"].set("click the image to pin a point")

        def on_click(self, event):
            if not self.img or not self.img.has_ir:
                return
            xy = self.from_canvas(event.x, event.y)
            if not xy or not self.img.in_area(*xy):
                self.vars["cell"].set("click inside the thermal area")
                return
            self.click_xy = xy
            self.draw_markers()
            self.refresh_labels()

        def on_motion(self, event):
            if not self.img:
                return
            xy = self.from_canvas(event.x, event.y)
            cell = self.img.cell_at(*xy) if xy else None
            if xy:
                self.vars["pos"].set(f"x {xy[0]}, y {xy[1]}" +
                                     (f"  ·  cell [{cell[0]}, {cell[1]}]" if cell else ""))
                self.vars["hover"].set(fmt_temp(self.img.temp_at(*xy, self.method), self.unit.get()))
            else:
                self.vars["pos"].set("")
                self.vars["hover"].set("–")
            new = cell[:2] if cell else None
            if new != self.hover_cell:
                self.hover_cell = new
                self.draw_hover_cell()

        def on_leave(self, event):
            self.vars["hover"].set("–")
            self.vars["pos"].set("")
            self.hover_cell = None
            self.draw_hover_cell()

        # ---- export ----
        def export(self):
            if not self.img or not self.img.has_ir:
                return
            base = os.path.splitext(os.path.basename(self.img.path))[0]
            p = filedialog.asksaveasfilename(
                title="Export temperatures", defaultextension=".csv",
                initialfile=base + ".csv", filetypes=[("CSV", "*.csv")])
            if not p:
                return
            raw = False
            if self.img.has_grid:
                raw = messagebox.askyesno(
                    "Export",
                    "Export the raw sensor grid "
                    f"({self.img.grid_n}×{self.img.grid_n}) instead of the 240×240 image grid?\n\n"
                    "Yes = raw sensor grid, No = 240×240 (one value per pixel, "
                    f"smoothing: {self.method_var.get()}).")
            try:
                export_ir_csv(self.img, p, raw_grid=raw, method=self.method)
            except OSError as e:
                messagebox.showerror("Export failed", str(e))

        def export_png(self):
            if not self.img:
                return
            base = os.path.splitext(os.path.basename(self.img.path))[0]
            p = filedialog.asksaveasfilename(
                title="Save view as PNG", defaultextension=".png",
                initialfile=base + "_view.png", filetypes=[("PNG image", "*.png")])
            if not p:
                return
            # Save at a whole-number zoom (at least 2x) so every sensor cell
            # has the same size in the file, whatever the window size is.
            scale = max(2, round(self.scale))
            overlay_on = self.show_overlay.get() and self.img.has_grid
            w, h, rgb = render_view(self.img, self.get_overlay() if overlay_on else None,
                                    self.opacity.get() / 100, scale, self.show_grid.get())
            try:
                write_png(p, w, h, rgb)
            except OSError as e:
                messagebox.showerror("Save failed", str(e))

    # =======================================================================
    # Multimeter recording tab
    # =======================================================================
    class DMMTab(ttk.Frame):
        PAD_L, PAD_R, PAD_T, PAD_B = 76, 24, 20, 44   # chart margins (px)

        def __init__(self, master):
            super().__init__(master, padding=(12, 10, 12, 8))
            self.rec: VomRecording | None = None
            self.view = (0.0, 1.0)  # visible time range in seconds
            self._drag = None
            self.log_scale = tk.BooleanVar(self, value=False)
            self.log_scale.trace_add("write", lambda *a: self.draw())

            bar = ttk.Frame(self, style="Toolbar.TFrame")
            bar.pack(fill="x", pady=(0, 10))
            ttk.Button(bar, text="Open…", style="Accent.TButton",
                       command=self.ask_open).pack(side="left")
            self.export_btn = ttk.Button(bar, text="Export CSV…", command=self.export,
                                         state="disabled")
            self.export_btn.pack(side="left", padx=(8, 0))
            ttk.Button(bar, text="Reset zoom", command=self.reset_zoom).pack(side="left", padx=(8, 0))
            ttk.Checkbutton(bar, text="Log scale", variable=self.log_scale).pack(
                side="left", padx=(14, 0))
            self.path_var = tk.StringVar(self, value="No file open")
            ttk.Label(bar, textvariable=self.path_var, style="Muted.TLabel").pack(side="right")

            # Row of small statistic tiles
            tiles = ttk.Frame(self)
            tiles.pack(fill="x", pady=(0, 10))
            self.vars = {}
            for i, (key, label) in enumerate((("mode", "Mode"), ("cycle", "Interval"),
                                              ("len", "Samples"), ("max", "Max"),
                                              ("min", "Min"), ("cursor", "Cursor"))):
                # Equal-width tiles ("uniform" group), the cursor tile twice as wide
                if key == "cursor":
                    tiles.columnconfigure(i, weight=2)
                else:
                    tiles.columnconfigure(i, weight=1, uniform="tile")
                if theme.mode == "flat":
                    t = ttk.Frame(tiles, style="Card.TFrame", padding=(12, 8))
                else:
                    t = ttk.Frame(tiles, padding=(2, 4))
                t.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 8, 0))
                ttk.Label(t, text=label, style="Muted.Card.TLabel").pack(anchor="w")
                v = tk.StringVar(self, value="–")
                ttk.Label(t, textvariable=v, style="SmallValue.Card.TLabel").pack(anchor="w")
                self.vars[key] = v
            self.note_var = tk.StringVar(self, value="")
            ttk.Label(self, textvariable=self.note_var, style="Muted.TLabel").pack(anchor="w")

            c = theme.c
            frame = ttk.Frame(self, style="Card.TFrame" if theme.mode == "flat" else "TFrame",
                              padding=1)
            frame.pack(fill="both", expand=True)
            self.canvas = tk.Canvas(frame, background=c["chart_bg"], highlightthickness=0,
                                    height=380, cursor="crosshair")
            self.canvas.pack(fill="both", expand=True)
            self.canvas.bind("<Configure>", lambda e: self.draw())
            # Mouse wheel differs per OS: <MouseWheel> with event.delta on
            # Windows/macOS, Button-4/5 events on X11 (Linux).
            self.canvas.bind("<MouseWheel>", self.on_wheel)
            self.canvas.bind("<Button-4>", lambda e: self.zoom_at(e.x, 0.5))
            self.canvas.bind("<Button-5>", lambda e: self.zoom_at(e.x, 2.0))
            self.canvas.bind("<ButtonPress-1>", self.on_press)
            self.canvas.bind("<B1-Motion>", self.on_drag)
            self.canvas.bind("<Motion>", self.on_motion)
            self.canvas.bind("<Leave>", lambda e: (self.canvas.delete("cursor"),
                                                   self.vars["cursor"].set("–")))
            ttk.Label(self, text="Scroll to zoom   ·   drag to pan   ·   L log scale",
                      style="Muted.TLabel").pack(
                anchor="w", pady=(8, 0))

        # --- data ---
        def ask_open(self):
            p = filedialog.askopenfilename(
                title="Open multimeter recording",
                filetypes=[("Multimeter recordings", "*.vom *.VOM"), ("All files", "*.*")])
            if p:
                self.load(p)

        def load(self, path, announce_it: bool = True):
            try:
                rec = load_vom(path)
            except (OSError, FormatError, ValueError) as e:
                messagebox.showerror("Could not open file", f"{path}\n\n{e}")
                return
            self.rec = rec
            self.path_var.set(os.path.basename(path))
            self.export_btn.config(state="normal")
            unit = "V" if rec.in_volts else "mV"
            vals = rec.display_values()
            self.vars["mode"].set(rec.mode or "–")
            self.vars["cycle"].set(f"{rec.cycle:g} s")
            self.vars["len"].set(str(len(vals)))
            self.vars["max"].set(f"{max(vals):.4g} {unit}")
            self.vars["min"].set(f"{min(vals):.4g} {unit}")
            self.note_var.set("  ".join(rec.notes[:3]))
            self.reset_zoom()
            if announce_it:
                announce(print_vom_summary, rec)

        def export(self):
            if not self.rec:
                return
            base = os.path.splitext(os.path.basename(self.rec.path))[0]
            p = filedialog.asksaveasfilename(title="Export recording", defaultextension=".csv",
                                             initialfile=base + ".csv", filetypes=[("CSV", "*.csv")])
            if p:
                try:
                    export_vom_csv(self.rec, p)
                except OSError as e:
                    messagebox.showerror("Export failed", str(e))

        # --- view ---
        def total_span(self):
            n = len(self.rec.values_mv) if self.rec else 2
            return 0.0, max((n - 1) * (self.rec.cycle if self.rec else 1), 1e-9)

        def reset_zoom(self):
            self.view = self.total_span()
            self.draw()

        def plot_area(self):
            w = self.canvas.winfo_width()
            h = self.canvas.winfo_height()
            return (self.PAD_L, self.PAD_T, max(w - self.PAD_R, self.PAD_L + 10),
                    max(h - self.PAD_B, self.PAD_T + 10))

        def x_to_t(self, px):
            x0, _, x1, _ = self.plot_area()
            a, b = self.view
            return a + (px - x0) / (x1 - x0) * (b - a)

        def zoom_at(self, px, factor):
            """Zoom the time axis keeping the time under the mouse fixed."""
            if not self.rec:
                return
            lo, hi = self.total_span()
            a, b = self.view
            t = min(max(self.x_to_t(px), a), b)
            span = (b - a) * factor
            span = max(span, self.rec.cycle * 4)    # never closer than 4 samples
            span = min(span, hi - lo)
            na = t - (t - a) / (b - a) * span
            na = min(max(na, lo), hi - span)
            self.view = (na, na + span)
            self.draw()

        def on_wheel(self, e):
            self.zoom_at(e.x, 0.5 if e.delta > 0 else 2.0)

        def on_press(self, e):
            self._drag = (e.x, self.view)

        def on_drag(self, e):
            if not self.rec or not self._drag:
                return
            x0, _, x1, _ = self.plot_area()
            sx, (a, b) = self._drag
            dt = (e.x - sx) / (x1 - x0) * (b - a)
            lo, hi = self.total_span()
            na = min(max(a - dt, lo), hi - (b - a))
            self.view = (na, na + (b - a))
            self.draw()

        def on_motion(self, e):
            if not self.rec:
                return
            x0, _, x1, _ = self.plot_area()
            if not (x0 <= e.x <= x1):
                self.canvas.delete("cursor")
                self.vars["cursor"].set("–")
                return
            i = round(self.x_to_t(e.x) / self.rec.cycle)
            vals = self.rec.display_values()
            if 0 <= i < len(vals):
                unit = "V" if self.rec.in_volts else "mV"
                self.vars["cursor"].set(f"{vals[i]:.4g} {unit}  @ {i * self.rec.cycle:.1f} s")
                self.draw_cursor(i, vals[i])

        @staticmethod
        def nice_ticks(lo, hi, n=8):
            """Round tick positions (1, 2, 2.5, 5 × 10^k steps) covering lo..hi."""
            if hi <= lo:
                hi = lo + 1
            raw = (hi - lo) / n
            mag = 10 ** math.floor(math.log10(raw))
            step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
            t = math.ceil(lo / step) * step
            out = []
            while t <= hi + step * 1e-9:
                out.append(round(t, 10))
                t += step
            return out, step

        # ---- symmetric log scale (see symlog() for the maths) ----
        def log_threshold(self) -> float:
            return symlog_threshold(self.rec)

        def fy(self, v: float) -> float:
            """Value -> vertical chart coordinate (identity when linear)."""
            return symlog(v, self.log_threshold()) if self.log_scale.get() else v

        def log_ticks(self, lo: float, hi: float) -> list[float]:
            return symlog_ticks(lo, hi, self.log_threshold())

        def y_range(self):
            """Vertical range in chart coordinates (after fy), with padding."""
            vals = [self.fy(v) for v in self.rec.display_values()]
            lo, hi = min(vals), max(vals)
            if hi - lo < 1e-9:
                lo, hi = lo - 1, hi + 1
            pad = (hi - lo) * 0.08
            return lo - pad, hi + pad

        def to_px(self, t, v):
            x0, y0, x1, y1 = self.plot_area()
            a, b = self.view
            lo, hi = self.y_range()
            return (x0 + (t - a) / (b - a) * (x1 - x0),
                    y1 - (self.fy(v) - lo) / (hi - lo) * (y1 - y0))

        def draw_cursor(self, i, v):
            c = theme.c
            self.canvas.delete("cursor")
            x, y = self.to_px(i * self.rec.cycle, v)
            _, y0, _, y1 = self.plot_area()
            self.canvas.create_line(x, y0, x, y1, fill=c["chart_axis"], dash=(3, 3), tags="cursor")
            self.canvas.create_oval(x - 5, y - 5, x + 5, y + 5, fill=c["chart_line"],
                                    outline=c["chart_bg"], width=2, tags="cursor")

        def draw(self):
            c = theme.c
            cv = self.canvas
            cv.delete("all")
            if not self.rec:
                cv.create_text(cv.winfo_width() // 2, cv.winfo_height() // 2 - 10,
                               text="No recording open", fill=c["muted"], font=theme.f_heading)
                cv.create_text(cv.winfo_width() // 2, cv.winfo_height() // 2 + 14,
                               text="Open a .vom file saved by the meter", fill=c["muted"])
                return
            x0, y0, x1, y1 = self.plot_area()
            a, b = self.view
            lo, hi = self.y_range()
            unit = "V" if self.rec.in_volts else "mV"
            font = theme.f_small
            if self.log_scale.get():
                yt = self.log_ticks(lo, hi)
                ylabel = lambda v: f"{v:g} {unit}"   # 0.001 V, 0.1 V, 1 V …
            else:
                yt, ystep = self.nice_ticks(lo, hi, 7)
                ydec = max(0, -int(math.floor(math.log10(ystep)))) if ystep < 1 else 0
                ylabel = lambda v: f"{v:.{ydec}f} {unit}"
            # to_px() applies fy(), so tick positions below need real values
            xt, xstep = self.nice_ticks(a, b, 10)
            xdec = max(0, -int(math.floor(math.log10(xstep)))) if xstep < 1 else 0
            for v in yt:
                _, y = self.to_px(a, v)
                cv.create_line(x0, y, x1, y, fill=c["chart_grid"])
            for t in xt:
                x = x0 + (t - a) / (b - a) * (x1 - x0)
                cv.create_line(x, y0, x, y1, fill=c["chart_grid"])

            vals = self.rec.display_values()
            cyc = self.rec.cycle
            # Only draw the samples in view (+1 each side so the line reaches
            # the edges), and thin out to ~2 points per pixel when zoomed out.
            i0 = max(int(a / cyc) - 1, 0)
            i1 = min(int(b / cyc) + 2, len(vals))
            idx = range(i0, i1)
            stride = max(1, len(idx) // max(2 * int(x1 - x0), 1))
            pts = []
            for i in idx[::stride]:
                pts.extend(self.to_px(i * cyc, vals[i]))
            if len(pts) >= 4:
                cv.create_line(*pts, fill=c["chart_line"], width=2, joinstyle="round")
            elif len(pts) == 2:
                cv.create_oval(pts[0] - 2, pts[1] - 2, pts[0] + 2, pts[1] + 2, fill=c["chart_line"])
            # Canvas has no clipping: paint the margins over anything drawn
            # outside the plot area, then put the axis labels on top.
            w, h = cv.winfo_width(), cv.winfo_height()
            for rect in ((0, 0, x0 - 1, h), (x1 + 1, 0, w, h)):
                cv.create_rectangle(*rect, fill=c["chart_bg"], outline=c["chart_bg"])
            for v in yt:
                _, y = self.to_px(a, v)
                cv.create_text(x0 - 8, y, text=ylabel(v), anchor="e", font=font,
                               fill=c["muted"])
            cv.create_line(x0, y1, x1, y1, fill=c["chart_axis"])
            for t in xt:
                x = x0 + (t - a) / (b - a) * (x1 - x0)
                cv.create_text(x, y1 + 8, text=f"{t:.{xdec}f} s", anchor="n", font=font,
                               fill=c["muted"])

    # =======================================================================
    # Window
    # =======================================================================
    # className sets the X11 WM_CLASS, which Linux docks use to group windows.
    # Tk must only be started once: on macOS a second start crashes the process.
    try:
        root = tk.Tk(className="thermal-dmm-viewer")
    except tk.TclError as e:
        # tkinter is present but Tk itself can't start: usually a broken or
        # mismatched Tk install (e.g. tkinter built for Tk 8.6 while the
        # package manager installed Tk 9), or no display on Linux.
        first = str(e).strip().splitlines()[0] if str(e).strip() else repr(e)
        print(f"The window couldn't start: Tk failed to initialise.\n  {first}\n"
              f"Python: {sys.executable} (built for Tk {tk.TkVersion})\n"
              "This is a problem with this Python's Tk installation, not with the "
              "viewer. Try a Python whose installer includes Tk (e.g. python.org), "
              "or reinstall this Python's Tk package. The command-line mode still "
              "works: add --no-gui.", file=sys.stderr)
        return 1
    root.withdraw()          # build everything before showing the window
    theme = Theme(root, theme_choice)
    root.title(APP_NAME)
    root.configure(background=theme.c["bg"])

    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True)
    ir_tab, dmm_tab = IRTab(nb), DMMTab(nb)
    nb.add(ir_tab, text="Thermal image")
    nb.add(dmm_tab, text="Multimeter recording")

    # ----- menu bar -----
    def show_ir():
        nb.select(ir_tab)

    def show_dmm():
        nb.select(dmm_tab)

    acc = "⌘" if IS_MAC else "Ctrl+"
    menubar = tk.Menu(root)
    m_file = tk.Menu(menubar, tearoff=False)
    m_file.add_command(label="Open thermal image…", accelerator=f"{acc}O",
                       command=lambda: (show_ir(), ir_tab.ask_open()))
    m_file.add_command(label="Open multimeter recording…",
                       command=lambda: (show_dmm(), dmm_tab.ask_open()))
    m_file.add_separator()
    m_file.add_command(label="Previous image", accelerator="←", command=lambda: ir_tab.step(-1))
    m_file.add_command(label="Next image", accelerator="→", command=lambda: ir_tab.step(1))
    m_file.add_separator()
    m_file.add_command(label="Export thermal data (CSV)…", command=ir_tab.export)
    m_file.add_command(label="Save thermal view as PNG…", command=ir_tab.export_png)
    m_file.add_command(label="Export recording (CSV)…", command=dmm_tab.export)
    if not IS_MAC:  # macOS provides Quit in the application menu
        m_file.add_separator()
        m_file.add_command(label="Quit", accelerator=f"{acc}Q", command=root.destroy)
    menubar.add_cascade(label="File", menu=m_file)

    m_view = tk.Menu(menubar, tearoff=False)
    m_view.add_checkbutton(label="Max / min markers", variable=ir_tab.show_markers)
    m_view.add_checkbutton(label="Sensor grid outline", accelerator="G", variable=ir_tab.show_grid)
    m_view.add_checkbutton(label="Sensor grid overlay", accelerator="O", variable=ir_tab.show_overlay)
    m_view.add_checkbutton(label="Smooth overlay", variable=ir_tab.overlay_smooth)
    m_view.add_checkbutton(label="Lock colour range", variable=ir_tab.lock_range)
    m_pal = tk.Menu(m_view, tearoff=False)
    for name in PALETTES:
        m_pal.add_radiobutton(label=name, value=name, variable=ir_tab.palette)
    m_view.add_cascade(label="Overlay colours", menu=m_pal)
    m_op = tk.Menu(m_view, tearoff=False)
    for pct in (25, 50, 70, 85, 100):
        m_op.add_radiobutton(label=f"{pct} %", value=float(pct), variable=ir_tab.opacity)
    m_view.add_cascade(label="Overlay opacity", menu=m_op)
    m_view.add_separator()
    m_sm = tk.Menu(m_view, tearoff=False)
    for key, (label, _) in METHODS.items():
        m_sm.add_radiobutton(label=label, value=label, variable=ir_tab.method_var)
    m_view.add_cascade(label="Smoothing", menu=m_sm)
    m_view.add_checkbutton(label="Log scale (recordings)", accelerator="L",
                           variable=dmm_tab.log_scale)
    m_unit = tk.Menu(m_view, tearoff=False)
    for u in ("°C", "°F"):
        m_unit.add_radiobutton(label=u, value=u, variable=ir_tab.unit)
    m_view.add_cascade(label="Unit", menu=m_unit)
    menubar.add_cascade(label="View", menu=m_view)

    m_help = tk.Menu(menubar, tearoff=False)
    m_help.add_command(label=f"About {APP_NAME}", command=lambda: messagebox.showinfo(
        "About", f"{APP_NAME} {__version__}\n\n"
                 "Viewer for thermal images and multimeter recordings from "
                 "FeelElec / ToolTop thermal multimeters.\n"
                 "Not affiliated with FeelElec or ToolTop.\n\n"
                 "Run with --help for command-line options."))
    menubar.add_cascade(label="Help", menu=m_help)
    root.config(menu=menubar)

    # ----- keyboard shortcuts -----
    def on_ir_tab():
        return nb.select() == str(ir_tab)

    def key_toggle(var):
        def handler(event):
            # Don't steal keys typed into an entry or combobox
            if isinstance(event.widget, (tk.Entry, ttk.Entry, ttk.Combobox)):
                return
            if on_ir_tab():
                var.set(not var.get())
        return handler

    root.bind("<Left>", lambda e: on_ir_tab() and ir_tab.step(-1))
    root.bind("<Right>", lambda e: on_ir_tab() and ir_tab.step(1))
    root.bind(f"<{MOD}-o>", lambda e: (show_ir(), ir_tab.ask_open()))
    root.bind(f"<{MOD}-q>", lambda e: root.destroy())
    root.bind("g", key_toggle(ir_tab.show_grid))
    root.bind("o", key_toggle(ir_tab.show_overlay))

    def toggle_log(event):
        if nb.select() == str(dmm_tab) and not isinstance(
                event.widget, (tk.Entry, ttk.Entry, ttk.Combobox)):
            dmm_tab.log_scale.set(not dmm_tab.log_scale.get())
    root.bind("l", toggle_log)

    # Files from the command line: their summaries were already printed by cli()
    for p in open_paths:
        if p.lower().endswith(".vom"):
            dmm_tab.load(p, announce_it=False)
            nb.select(dmm_tab)
        else:
            ir_tab.load(p, announce_it=False)
            nb.select(ir_tab)

    # Start at a comfortable size, but never larger than the screen
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    w, h = min(1280, sw - 80), min(820, sh - 100)
    root.geometry(f"{w}x{h}+{max((sw - w) // 2, 0)}+{max((sh - h) // 3, 0)}")
    root.minsize(min(980, sw - 40), min(620, sh - 80))
    root.deiconify()
    root.mainloop()
    return 0


def main() -> int:
    gui: dict = {}
    rc = cli(sys.argv[1:], gui)
    if rc is not None:
        return rc
    return run_gui(gui["files"], gui["theme"], quiet=gui["quiet"])


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:  # e.g. output piped into "head"
        sys.exit(0)
