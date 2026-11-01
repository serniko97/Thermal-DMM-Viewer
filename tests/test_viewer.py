"""Tests for thermal_dmm_viewer. Standard library only, no display needed.

Run from the repository root:
    python -m unittest discover -s tests -v
"""

import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import thermal_dmm_viewer as tdv  # noqa: E402

SAMPLE = os.path.join(ROOT, "samples", "IR_0013.BMP")
SAMPLE_VOM = os.path.join(ROOT, "samples", "VOM_0001.VOM")
SCRIPT = os.path.join(ROOT, "thermal_dmm_viewer.py")


def make_bmp(grid_values=None, version=200, n=48):
    """Build a small valid 240x320 RGB565 BMP, optionally with a temperature block."""
    w, h = 240, 320
    pixels = bytes(w * h * 2)
    off = 14 + 40 + 12
    header = (b"BM" + struct.pack("<IHHI", off + len(pixels), 0, 0, off)
              + struct.pack("<IiiHHIIiiII", 40, w, h, 1, 16, 3, len(pixels), 0, 0, 0, 0)
              + struct.pack("<III", 0xF800, 0x07E0, 0x001F))
    data = header + pixels
    if grid_values is not None:
        data += struct.pack("<H", version) + struct.pack(f"<{n * n}H", *grid_values)
    return data


def read_bytes(path):
    with open(path, "rb") as fh:   # closed promptly: Windows can't delete open files
        return fh.read()


def read_text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _expand565(v):
    """RGB565 value -> 8-bit (r, g, b), replicating high bits like real decoders."""
    r, g, b = (v >> 11) & 31, (v >> 5) & 63, v & 31
    return (r << 3 | r >> 2, g << 2 | g >> 4, b << 3 | b >> 2)


def make_colour_scale_bmp(version=12, tmin=20.0, tmax=45.6, bpp=24, fill=None):
    """Synthetic file in the older colour-scale format, following the format
    description in docs/technical.md: min/max/version in three top-row pixels,
    a 255-row colour bar, and a thermal area painted with bar colours.
    fill(x, y) -> bar position 0..254 (or None to leave a non-bar colour)."""
    w, h = 240, 320
    area, bar_x = ((0, 0, 214, 286), 217) if version == 12 else ((36, 24, 186, 250), 25)
    # 255 distinct bar colours, each exactly representable in RGB565
    bar = [_expand565(0x0400 + i * 97) for i in range(255)]
    assert len(set(bar)) == 255
    px = [[(10, 10, 10)] * w for _ in range(h)]       # dark background, not on the bar
    ax, ay, aw, ah = area
    for y in range(ay, ay + ah):
        for x in range(ax, ax + aw):
            i = fill(x, y) if fill else (x - ax) * 254 // (aw - 1)
            if i is not None:
                px[y][x] = bar[i]
    for i in range(255):
        px[278 - i][bar_x] = bar[i]                    # bar: row 278 = coldest
    px[0][0] = _expand565(int(round(tmin * 100)))
    px[0][239] = _expand565(int(round(tmax * 100)))
    px[0][238] = _expand565(version)
    if bpp == 24:
        row = ((w * 3 + 3) // 4) * 4
        pixels = b"".join(bytes(c for p in px[y] for c in (p[2], p[1], p[0])).ljust(row, b"\0")
                          for y in range(h - 1, -1, -1))
        off = 54
        hdr = (b"BM" + struct.pack("<IHHI", off + len(pixels), 0, 0, off)
               + struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, len(pixels), 0, 0, 0, 0))
    else:
        pixels = b"".join(struct.pack("<H", (p[0] >> 3) << 11 | (p[1] >> 2) << 5 | p[2] >> 3)
                          for y in range(h - 1, -1, -1) for p in px[y])
        off = 66
        hdr = (b"BM" + struct.pack("<IHHI", off + len(pixels), 0, 0, off)
               + struct.pack("<IiiHHIIiiII", 40, w, h, 1, 16, 3, len(pixels), 0, 0, 0, 0)
               + struct.pack("<III", 0xF800, 0x07E0, 0x001F))
    return hdr + pixels, area, bar_x


def celsius_to_raw(c):
    return int(round(c * 64 + 3200))


class TempFileMixin:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, name, data, mode="wb"):
        path = os.path.join(self.tmp.name, name)
        with open(path, mode) as fh:
            fh.write(data)
        return path


class TestRealSample(unittest.TestCase):
    """samples/IR_0013.BMP was taken with a ToolTop ET13S. The meter showed
    23.1 °C at the centre spot."""

    @classmethod
    def setUpClass(cls):
        cls.img = tdv.load_ir_bmp(SAMPLE)

    def test_structure(self):
        img = self.img
        self.assertEqual((img.width, img.height), (240, 320))
        self.assertTrue(img.has_ir)
        self.assertEqual(img.version, 200)
        self.assertEqual(img.grid_n, 48)
        self.assertEqual(img.ir_offset, 153666)
        self.assertEqual(img.bmp_reserved2, 0x6607)
        self.assertEqual(img.notes, [])

    def test_picture_decoding(self):
        digest = hashlib.sha256(self.img.rgb).hexdigest()
        self.assertEqual(digest, "9afe7569d1c4655905b1a27e1079ec9f5a2748c3bf12c55c31d3513b680eeba8")

    def test_temperatures(self):
        img = self.img
        self.assertEqual(img.tmax, 30.40625)
        self.assertEqual(img.tmin, 21.875)
        self.assertEqual(img.max_pos, (237, 165))
        self.assertEqual(img.min_pos, (2, 170))
        # Centre cell vs the meter's own on-screen reading of 23.1 °C
        self.assertEqual(img.grid[24][24], 23.0625)
        self.assertAlmostEqual(img.temp_at(120, 153, "bilinear"), 23.1, delta=0.1)

    def test_vendor_interpolation_bit_exact(self):
        """Fingerprint of the vendor software's 240x240 result for this file.
        The "vendor" mode must match it exactly; any change to it
        breaks this test."""
        bits = b"".join(struct.pack("<f", v) for row in self.img.temps("vendor") for v in row)
        self.assertEqual(hashlib.sha256(bits).hexdigest(),
                         "17a38b7eb9ffc173fcd2c308c161321fbe1ddc4f80e912052013524b44e24514")
        v = self.img.temps("vendor")
        self.assertEqual(v[120][120], 23.0546875)
        self.assertEqual(v[239][239], 22.811248779296875)

    def test_stats(self):
        st = tdv.ir_stats(self.img)
        self.assertEqual(st["grid_size"], [48, 48])
        self.assertEqual(st["max_cell"], [26, 47])
        self.assertEqual(st["min_cell"], [27, 0])
        self.assertAlmostEqual(st["mean"], 23.52, delta=0.01)
        self.assertAlmostEqual(st["median"], 23.16, delta=0.01)


class TestRealRecording(unittest.TestCase):
    """samples/VOM_0001.VOM is an original ET13S recording (CRLF line ends)."""

    @classmethod
    def setUpClass(cls):
        cls.rec = tdv.load_vom(SAMPLE_VOM)

    def test_header(self):
        rec = self.rec
        self.assertEqual(rec.header, "FeelTech Vom Data")
        self.assertEqual(rec.mode, "DC/V")
        self.assertEqual(rec.cycle, 0.5)
        self.assertEqual(rec.length, 67)
        self.assertEqual(rec.notes, [])

    def test_sample_counter_removed(self):
        rec = self.rec
        self.assertTrue(rec.index_suffix)
        self.assertEqual(len(rec.values_mv), 67)
        # Whole-mV values reproduce the header's Max/Min exactly
        self.assertEqual(max(rec.values_mv), rec.max_mv)
        self.assertEqual(min(rec.values_mv), rec.min_mv)
        self.assertEqual(rec.values_mv[:12], [0, 0, 0, 0, -2, -3, -4, -3, -4, 3046, 3044, 3043])
        self.assertTrue(all(v == int(v) for v in rec.values_mv))

    def test_file_is_byte_exact(self):
        # Guards against Git line-ending conversion of the sample
        self.assertIn(b"\r\n", read_bytes(SAMPLE_VOM))


class TestSymlog(unittest.TestCase):
    def test_transform(self):
        c = 1.0
        self.assertEqual(tdv.symlog(0, c), 0)
        self.assertAlmostEqual(tdv.symlog(9, c), 1.0)
        self.assertAlmostEqual(tdv.symlog(-99, c), -2.0)
        # monotonic across zero
        xs = [-1000, -10, -1, -0.1, 0, 0.1, 1, 10, 1000]
        ys = [tdv.symlog(x, c) for x in xs]
        self.assertEqual(ys, sorted(ys))

    def test_threshold_and_ticks_on_real_file(self):
        rec = tdv.load_vom(SAMPLE_VOM)          # values in volts on screen
        c = tdv.symlog_threshold(rec)
        self.assertEqual(c, 0.001)              # 1 mV
        vals = [tdv.symlog(v, c) for v in rec.display_values()]
        ticks = tdv.symlog_ticks(min(vals), max(vals), c)
        self.assertIn(0.0, ticks)
        self.assertIn(1.0, ticks)
        self.assertIn(-0.001, ticks)
        self.assertTrue(all(min(vals) <= tdv.symlog(v, c) <= max(vals) for v in ticks))

    def test_threshold_follows_finer_data(self):
        rec = tdv.VomRecording(path="x", values_mv=[0.0, 0.05, 0.3], max_mv=0.3)
        self.assertEqual(tdv.symlog_threshold(rec), 0.01)


class TestInterpolation(unittest.TestCase):
    def setUp(self):
        import random
        rnd = random.Random(1)
        self.n = 48
        self.grid = [[rnd.uniform(-20, 550) for _ in range(48)] for _ in range(48)]

    def test_bilinear_exact_at_cell_centres_and_in_range(self):
        out = tdv.interpolate_bilinear(self.grid, 48, 240)
        lo = min(map(min, self.grid))
        hi = max(map(max, self.grid))
        for r in range(48):
            for c in range(48):
                self.assertAlmostEqual(out[5 * r + 2][5 * c + 2], self.grid[r][c], places=9)
        flat = [v for row in out for v in row]
        self.assertGreaterEqual(min(flat), lo - 1e-9)
        self.assertLessEqual(max(flat), hi + 1e-9)

    def test_nearest_matches_cells(self):
        out = tdv.interpolate_nearest(self.grid, 48, 240)
        for y in range(0, 240, 7):
            for x in range(0, 240, 7):
                self.assertEqual(out[y][x], self.grid[y // 5][x // 5])

    def test_vendor_flaw_single_hot_cell(self):
        """Documents the vendor flaw: a single hot cell vanishes along its own row."""
        g = [[20.0] * 48 for _ in range(48)]
        g[24][24] = 100.0
        vendor = tdv.interpolate_vendor(g, 48, 5.0)
        bilinear = tdv.interpolate_bilinear(g, 48, 240)
        self.assertEqual(vendor[122][122], 20.0)
        self.assertEqual(bilinear[122][122], 100.0)


class TestFormatting(unittest.TestCase):
    def test_dotnet_double_rounding(self):
        # 280.025 isn't representable; the vendor software shows the float nearest to it as 280.03
        f = struct.unpack("<f", struct.pack("<f", 280.025))[0]
        self.assertEqual(tdv.format_temp(f), "280.03")
        self.assertEqual(tdv.format_temp(-0.001), "0.00")
        self.assertEqual(tdv.format_temp(23.0625), "23.06")


class TestRendering(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.img = tdv.load_ir_bmp(SAMPLE)

    def test_blend_matches_naive(self):
        import random
        rnd = random.Random(2)
        a = bytes(rnd.randrange(256) for _ in range(3000))
        b = bytes(rnd.randrange(256) for _ in range(3000))
        for op in (0.0, 0.3, 0.5, 0.77, 1.0):
            k = int(round(op * 256))
            expect = bytes((x * (256 - k) + y * k) >> 8 for x, y in zip(a, b))
            self.assertEqual(tdv.blend(a, b, op), expect)

    def test_render_sizes(self):
        lut = tdv.make_lut("Iron")
        ov = tdv.overlay_rgb(self.img, lut, self.img.tmin, self.img.tmax)
        self.assertEqual(len(ov), 240 * 240 * 3)
        for scale in (0.5, 1, 2, 2.37, 3, 4.6):
            w, h, rgb = tdv.render_view(self.img, ov, 0.6, scale, grid=True)
            self.assertEqual((w, h), tdv.output_size(240, 320, scale))
            self.assertEqual(len(rgb), w * h * 3)

    def test_cell_starts_follow_pixel_mapping(self):
        for scale in (1, 2, 2.37, 3.3):
            w, h = tdv.output_size(240, 320, scale)
            xs = tdv.cell_starts(scale, 48, tdv.IR_AREA_X, w)
            for cell in range(48):
                for out in range(xs[cell], xs[cell + 1]):
                    src = tdv.source_index(out, scale) - tdv.IR_AREA_X
                    self.assertEqual(src * 48 // 240, cell)

    def test_all_palettes(self):
        for name in tdv.PALETTES:
            lut = tdv.make_lut(name)
            self.assertEqual(len(lut), 256)
            self.assertTrue(all(len(c) == 3 for c in lut))


class TestFiles(TempFileMixin, unittest.TestCase):
    def test_png_writer(self):
        w, h, rgb = tdv.render_view(tdv.load_ir_bmp(SAMPLE), None, 0, 1)
        path = os.path.join(self.tmp.name, "out.png")
        tdv.write_png(path, w, h, rgb)
        data = read_bytes(path)
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        ihdr = struct.unpack(">IIBBBBB", data[16:29])
        self.assertEqual(ihdr, (240, 320, 8, 2, 0, 0, 0))
        idat_len = struct.unpack(">I", data[33:37])[0]
        raw = zlib.decompress(data[41:41 + idat_len])
        stride = w * 3 + 1
        self.assertEqual(b"".join(raw[i * stride + 1:(i + 1) * stride] for i in range(h)), rgb)

    def test_plain_bmp(self):
        img = tdv.load_ir_bmp(self.write("plain.bmp", make_bmp()))
        self.assertFalse(img.has_ir)
        self.assertIn("No embedded temperature data", img.notes[0])

    def test_synthetic_v200(self):
        vals = [celsius_to_raw(20 + (r + c) / 10) for r in range(48) for c in range(48)]
        img = tdv.load_ir_bmp(self.write("syn.bmp", make_bmp(vals)))
        self.assertTrue(img.has_ir)
        self.assertEqual(img.tmin, 20.0)
        self.assertAlmostEqual(img.tmax, 20 + 94 / 10, delta=1 / 64)
        self.assertEqual(img.cell_at(239, 272)[:2], (47, 47))
        self.assertIsNone(img.temp_at(0, 20))

    def test_unknown_square_grid(self):
        vals = [celsius_to_raw(25)] * (192 * 192)
        img = tdv.load_ir_bmp(self.write("big.bmp", make_bmp(vals, version=201, n=192)))
        self.assertTrue(img.has_ir)
        self.assertEqual(img.grid_n, 192)
        self.assertIn("Unknown data version 201", img.notes[0])

    def test_not_a_bmp(self):
        with self.assertRaises(tdv.FormatError):
            tdv.load_ir_bmp(self.write("x.bmp", b"hello"))

    def test_vom(self):
        text = ("Header\nMode: DCV\nCycle: 0.5s\nMax: 2,5 mV\nMin: 1 mV\nLen: 3\n"
                "1.0\n2,5\n1.5\n")
        rec = tdv.load_vom(self.write("r.vom", text, "w"))
        self.assertEqual(rec.mode, "DCV")
        self.assertEqual(rec.cycle, 0.5)
        self.assertEqual(rec.values_mv, [1.0, 2.5, 1.5])
        self.assertEqual(rec.notes, [])
        self.assertFalse(rec.in_volts)
        st = tdv.vom_stats(rec)
        self.assertEqual(st["duration_s"], 1.0)
        self.assertEqual(st["median_mV"], 1.5)

    def test_vom_genuine_decimals_kept(self):
        # Fractions that don't follow the sample counter must stay decimals
        text = "H\nMode:DC/V\nCycle:1s\nMax:9.3mV\nMin:5.0mV\nLen:3\n5.0\n7.1\n9.3\n"
        rec = tdv.load_vom(self.write("d.vom", text, "w"))
        self.assertFalse(rec.index_suffix)
        self.assertEqual(rec.values_mv, [5.0, 7.1, 9.3])

    def test_vom_counter_pattern_synthetic(self):
        lines = "\r\n".join(f"{v}.{i}" for i, v in enumerate([12, -7, 0, 1500] * 4))
        text = "FeelTech Vom Data \r\nMode:DC/V\r\nCycle:1s\r\nMax:1500.0mV\r\nMin:-7.0mV\r\nLen:16\r\n" + lines + "\r\n"
        rec = tdv.load_vom(self.write("c.vom", text.encode(), "wb"))
        self.assertTrue(rec.index_suffix)
        self.assertEqual(rec.values_mv[:5], [12, -7, 0, 1500, 12])

    def test_vom_length_mismatch_and_empty(self):
        rec = tdv.load_vom(self.write("a.vom", "H\nMode: X\nCycle: 1s\nMax: 5000mV\nMin: 0mV\nLen: 5\n1\n2\n", "w"))
        self.assertTrue(rec.in_volts)
        self.assertIn("Header says 5 points", rec.notes[0])
        with self.assertRaises(tdv.FormatError):
            tdv.load_vom(self.write("b.vom", "H\nMode: X\n", "w"))


class TestCommandLine(TempFileMixin, unittest.TestCase):
    def run_cli(self, *args):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        return subprocess.run([sys.executable, SCRIPT, *args], capture_output=True,
                              text=True, encoding="utf-8", env=env, timeout=120)

    def test_vom_summary(self):
        r = self.run_cli(SAMPLE_VOM, "--no-gui")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Min:           -4 mV", r.stdout)
        self.assertIn("firmware quirk", r.stdout)
        r = self.run_cli(SAMPLE_VOM, "--json")
        self.assertTrue(json.loads(r.stdout)["sample_counter_suffix"])

    def test_json(self):
        r = self.run_cli(SAMPLE, "--json", "--at", "120,153")
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.loads(r.stdout)
        self.assertEqual(d["max"], 30.40625)
        self.assertEqual(d["points"][0]["raw_cell"], [24, 24])

    def test_summary_no_gui(self):
        r = self.run_cli(SAMPLE, "--no-gui")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("48x48 sensor grid", r.stdout)
        self.assertIn("30.41 °C", r.stdout)

    def test_csv_and_png(self):
        out_csv = os.path.join(self.tmp.name, "t.csv")
        r = self.run_cli("csv", SAMPLE, out_csv, "--raw")
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = read_text(out_csv).strip().splitlines()
        self.assertEqual(len(rows), 48)
        self.assertEqual(len(rows[0].split(",")), 48)
        out_png = os.path.join(self.tmp.name, "v.png")
        r = self.run_cli(SAMPLE, "-n", "-q", "--png", out_png, "--overlay", "--grid",
                         "--palette", "rainbow")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(read_bytes(out_png).startswith(b"\x89PNG"))

    def test_quiet_flag_reaches_gui(self):
        gui = {}
        rc = tdv.cli(["-q", SAMPLE], gui)     # None = would open the window
        self.assertIsNone(rc)
        self.assertTrue(gui["quiet"])
        self.assertEqual(gui["files"], [SAMPLE])
        gui = {}
        tdv.cli([SAMPLE_VOM], gui)            # default: summaries on
        self.assertFalse(gui["quiet"])

    def test_bad_arguments(self):
        self.assertEqual(self.run_cli("--no-gui").returncode, 2)
        self.assertEqual(self.run_cli(SAMPLE, "-n", "--palette", "nope").returncode, 2)
        self.assertEqual(self.run_cli(SAMPLE, "-n", "--bogus").returncode, 2)


if __name__ == "__main__":
    unittest.main()


class TestColourScaleFormat(TempFileMixin, unittest.TestCase):
    """The older colour-scale format (experimental)."""

    def check_layout(self, version, bpp):
        data, area, bar_x = make_colour_scale_bmp(version=version, tmin=20.0, tmax=45.6, bpp=bpp)
        img = tdv.load_ir_bmp(self.write(f"cs{version}_{bpp}.bmp", data))
        self.assertTrue(img.has_ir)
        self.assertFalse(img.has_grid)
        self.assertEqual(img.version, version)
        self.assertEqual(img.area, area)
        self.assertEqual(img.legacy.bar_x, bar_x)
        self.assertAlmostEqual(img.tmin, 20.0, places=4)
        self.assertAlmostEqual(img.tmax, 45.6, places=4)
        ax, ay, aw, ah = area
        step = (45.6 - 20.0) / 256
        # left edge = bar position 0 = minimum; right edge = position 254
        self.assertAlmostEqual(img.temp_at(ax, ay + 5), 20.0, places=3)
        self.assertAlmostEqual(img.temp_at(ax + aw - 1, ay + 5), 20.0 + 254 * step, places=3)
        mid = ax + aw // 2
        i = (mid - ax) * 254 // (aw - 1)
        self.assertAlmostEqual(img.temp_at(mid, ay + 10), 20.0 + i * step, places=3)
        # outside the thermal area -> None
        self.assertIsNone(img.temp_at(239, 319))
        # hottest / coldest pixels found on the right / left edges
        self.assertEqual(img.max_pos[0], ax + aw - 1)
        self.assertEqual(img.min_pos[0], ax)
        self.assertIn("EXPERIMENTAL", img.notes[0])

    def test_version_12_layout_24bit(self):
        self.check_layout(12, 24)

    def test_version_12_layout_16bit(self):
        self.check_layout(12, 16)

    def test_other_layout_24bit(self):
        self.check_layout(7, 24)

    def test_other_layout_16bit(self):
        self.check_layout(7, 16)

    def test_non_bar_colours_have_no_temperature(self):
        data, area, _ = make_colour_scale_bmp(
            version=12, fill=lambda x, y: None if x < 20 else 100)
        img = tdv.load_ir_bmp(self.write("holes.bmp", data))
        self.assertIsNone(img.temp_at(5, 5))
        self.assertIsNotNone(img.temp_at(50, 5))

    def test_stats_summary_and_csv(self):
        data, area, _ = make_colour_scale_bmp(version=12)
        path = self.write("cs.bmp", data)
        st = tdv.ir_stats(tdv.load_ir_bmp(path))
        self.assertEqual(st["format"], "colour-scale (experimental)")
        # The version-12 thermal area starts at (0, 0), which is the pixel that
        # stores the minimum - so exactly one pixel has no temperature colour.
        self.assertEqual(st["pixels_with_temperature"], 214 * 286 - 1)
        out = os.path.join(self.tmp.name, "cs.csv")
        tdv.export_ir_csv(tdv.load_ir_bmp(path), out)
        rows = read_text(out).strip().splitlines()
        self.assertEqual(len(rows), 286)
        self.assertEqual(len(rows[0].split(",")), 214)

    def test_plain_pictures_are_not_mistaken(self):
        # Ordinary pictures must not be read as thermal images
        import random
        w, h = 240, 320
        hdr = (b"BM" + struct.pack("<IHHI", 54 + w * h * 3, 0, 0, 54)
               + struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, w * h * 3, 0, 0, 0, 0))
        for seed in range(20):
            rnd = random.Random(seed)
            noise = bytes(rnd.randrange(256) for _ in range(w * h * 3))
            self.assertFalse(tdv.load_ir_bmp(self.write(f"noise{seed}.bmp", hdr + noise)).has_ir)
        flat = hdr + bytes([40, 90, 200]) * (w * h)
        self.assertFalse(tdv.load_ir_bmp(self.write("flat.bmp", flat)).has_ir)
        # A screenshot-sized picture with a valid-looking header but no bar colours
        data, _, _ = make_colour_scale_bmp(version=12, fill=lambda x, y: None)
        self.assertFalse(tdv.load_ir_bmp(self.write("nobar.bmp", data)).has_ir)

    def test_et13s_files_unaffected(self):
        img = tdv.load_ir_bmp(SAMPLE)
        self.assertIsNone(img.legacy)
        self.assertTrue(img.has_grid)


class TestFedtool(TempFileMixin, unittest.TestCase):
    """The firmware analysis tool (firmware/tools/fedtool.py)."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        path = os.path.join(ROOT, "firmware", "tools", "fedtool.py")
        spec = importlib.util.spec_from_file_location("fedtool", path)
        cls.fed = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.fed)

    def test_flags_random_as_encrypted(self):
        import os as _os
        data = _os.urandom(134432)
        r = self.fed.analyse(self.write("rand.FED", data))
        self.assertGreater(r["entropy_bits_per_byte"], 7.99)
        self.assertEqual(r["duplicate_16B_blocks"], 0)
        self.assertLess(r["repeating_xor_excess_over_random"], 0.01)
        self.assertIn("encrypted", r["verdict"])

    def test_flags_structured_payload(self):
        # Repeated 16-byte block = ECB-like structure; must NOT read as encrypted
        data = (b"FIRMWARE_v1_HDR\x00" * 4000)[:64000]
        r = self.fed.analyse(self.write("struct.FED", data))
        self.assertGreater(r["duplicate_16B_blocks"], 0)
        self.assertNotIn("encrypted", r["verdict"])

    def test_detects_repeating_xor(self):
        import os as _os, itertools
        key = _os.urandom(32)
        plain = bytes(itertools.islice(itertools.cycle(
            b"the quick brown fox jumps over the lazy dog 0123456789\n"), 40000))
        data = bytes(p ^ key[i % len(key)] for i, p in enumerate(plain))
        r = self.fed.analyse(self.write("xor.FED", data))
        self.assertGreater(r["repeating_xor_excess_over_random"], 0.01)
        self.assertNotIn("encrypted", r["verdict"])

    def test_detects_crc_trailer(self):
        import os as _os, struct as _s, zlib as _z
        body = _os.urandom(1000)
        data = body + _s.pack("<I", _z.crc32(body) & 0xFFFFFFFF)
        r = self.fed.analyse(self.write("crc.FED", data))
        self.assertTrue(r["trailer"]["crc32_match"])
