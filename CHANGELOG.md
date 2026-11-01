# Changelog

## 1.6.0

- Experimental support for the older "colour-scale" image format, most likely
  used by the 24×32 and 90×120 models (ToolTop ET11S / ET12S, FeelElec
  FR01A/B/C). Not yet tested on a real file. Temperatures come from pixel colours on the colour
  bar; grid-only controls are greyed out for these files.
- Strict detection so ordinary pictures aren't mistaken for thermal images.
- Disabled drop-down menus now show greyed text in the Linux theme.

## 1.5.0

- Removed drag and drop. It relied on the third-party `tkinterdnd2` package,
  which has no build for some setups (e.g. Intel Macs with Tk 9) and caused
  crashes there. The viewer is standard-library-only again, with no optional
  extras. Kept from 1.4.x: terminal summaries for files opened in the window,
  and the clearer messages when tkinter or Tk is missing or broken.

## 1.4.4

- If Tk can't start (a broken or mismatched Tk installation), the viewer now
  explains the problem instead of showing a Python traceback.

## 1.4.3

- The "tkinter is not available" message now gives instructions for macOS
  (python.org, MacPorts, Homebrew) and Windows, not just Linux, and shows
  which Python is missing it.

## 1.4.2

- Fix: crash (segmentation fault) on macOS when `tkinterdnd2` is installed but
  can't load, e.g. on Intel Macs with Tk 9 (Python 3.14), for which
  `tkinterdnd2` has no build. Tk is now started only once and drag and drop is
  loaded into the existing window; if that fails, the window simply works
  without it and the terminal says why.

## 1.4.1

- Fix: the window failed with `image "pyimage1" doesn't exist` when
  `tkinterdnd2` was installed but its native library couldn't load (e.g. with a
  Tk version it doesn't support). The half-created Tk instance is now cleaned up,
  the terminal explains why drag and drop is off, and every image and setting is
  tied to its own window so this can't recur.

## 1.4.0

- Drag and drop: drop `.bmp` / `.vom` files or a folder onto the window.
  Needs the optional `tkinterdnd2` package (`pip install tkinterdnd2`, or the
  `dnd` extra); without it the hint line explains how to enable it.
- Files opened in the window (Open…, Previous/Next, drag and drop) print their
  summary in the terminal, like files given at start-up. `-q` silences this.

## 1.3.0

- Multimeter recordings: optional log scale (checkbox, **L** key, View menu).
  Symmetric log, linear within ±1 mV of zero, so 0 V and negative readings stay
  visible.

## 1.2.1

- Multimeter recordings: the meter writes each sample as "<mV>.<sample
  number>"; the sample number is now removed instead of being read as a
  decimal (e.g. a -4 mV sample was shown as -4.8 mV, as in the vendor software).
  Only applied when every line follows the pattern.
- Real multimeter recording added to `samples/` and the tests.
- °C/°F selector uses regular radio buttons on Windows/macOS, where the
  selected toolbutton was hard to see.
- README screenshots replaced with macOS ones.

## 1.2.0

First public release.

- Firmware notes (`firmware/`): analysis of the encrypted `UPDATA.FED` update
  file and a reusable analysis tool (`firmware/tools/fedtool.py`)
- Thermal images: click/hover temperature readout, max/min markers, raw
  sensor-cell values, three smoothing modes (bilinear, vendor-exact, raw cells)
- Sensor grid outline and false-colour overlay with 7 palettes, opacity and
  lockable colour range
- Multimeter recordings: zoomable chart with cursor readout
- CSV and PNG export
- Command line: statistics, JSON output, batch PNG export, no-GUI mode
- Resizable window; native look on Windows/macOS, GNOME-style light/dark theme on Linux
