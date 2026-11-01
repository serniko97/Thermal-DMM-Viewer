# Command-line reference

Any file given on the command line is summarised in the terminal. Add `--no-gui`
(or `-n`) to stop there without opening a window. This works on machines
without a display and doesn't need tkinter.

While the window is open, every file opened in it (Open… or Previous/Next)
prints the same summary in the terminal. `-q` / `--quiet` turns off
all of these summaries.

```
python thermal_dmm_viewer.py IR_0013.bmp                 # summary, then open the GUI
python thermal_dmm_viewer.py IR_0013.bmp -q              # open the GUI without the summary
python thermal_dmm_viewer.py *.bmp --no-gui              # summaries only (also: info *.bmp)
python thermal_dmm_viewer.py IR_0013.bmp -n --at 120,153 --at 60,90   # temperatures at pixels
python thermal_dmm_viewer.py IR_0013.bmp -n -F           # in °F
python thermal_dmm_viewer.py *.bmp --json > stats.json   # machine-readable, for scripts
python thermal_dmm_viewer.py --help                      # all options
```

The summary for a thermal image, here the sample file in this repository (statistics are computed over all 48×48 sensor cells):

```
$ python thermal_dmm_viewer.py samples/IR_0013.BMP --no-gui --at 70,100
File:          samples/IR_0013.BMP  (158,276 bytes)
Image:         240x320 px
Data format:   version 200, 48x48 sensor grid (5x5 px per cell)
Thermal area:  x 0-239, y 33-272
Unknown field: 26119 (0x6607, BMP header bytes 8-9)
Statistics over all sensor cells:
  Max:             30.41 °C   cell (26, 47), pixel (237, 165)
  Min:             21.88 °C   cell (27, 0), pixel (2, 170)
  Mean:            23.52 °C
  Median:          23.16 °C
  Std dev:          1.22 °C
  5th / 95th:      22.42 °C / 26.47 °C
Centre (120, 153):
  Raw cell:        23.06 °C   cell (24, 24)
  Bilinear:        23.03 °C
  Vendor:          23.05 °C
Point (70, 100): 23.54 °C (Bilinear (accurate)), raw cell (13, 14) 23.59 °C
```

`--at X,Y` takes screenshot pixel coordinates (the same as **Position** in the GUI).
The thermal area is x 0–239, y 33–272. The value uses `--method` (default bilinear),
and the raw cell value is shown alongside. For `.vom` files the summary gives mode,
number of samples, interval, duration, max, min, mean, median and standard deviation.

`--theme auto|native|light|dark` picks the window style (see [Appearance](../README.md#appearance)).

`--json` prints the same information plus the 5th/95th percentiles, value range and
cell positions as JSON: one object for one file, a list for several.

## Saving images

```
python thermal_dmm_viewer.py IR_0013.bmp -n --png view.png --grid
python thermal_dmm_viewer.py IR_0013.bmp -n --png view.png --overlay --palette rainbow --opacity 60
python thermal_dmm_viewer.py *.bmp -n -q --png "{name}_overlay.png" --overlay --grid --scale 3
```

| Option | Meaning |
| --- | --- |
| `--png OUT.png` | Save the image as PNG. With several input files, `{name}` is replaced by each file's name. |
| `--grid` | Draw the sensor grid outline |
| `--overlay` | Blend in the pixelated false-colour sensor grid |
| `--smooth-overlay` | Smoothed overlay instead of flat cells (uses `--method`) |
| `--palette NAME` | `iron` (default), `rainbow`, `white-hot`, `black-hot`, `hot-highlights`, `viridis`, `cold-hot` |
| `--opacity N` | Overlay opacity in percent (default 70) |
| `--scale N` | Enlarge N times (default 2, as on screen) |

## CSV export

```
python thermal_dmm_viewer.py csv FILE.bmp [OUT.csv]          # 240×240 temperatures
python thermal_dmm_viewer.py csv FILE.bmp --raw              # 48×48 raw sensor grid
python thermal_dmm_viewer.py csv FILE.bmp --method vendor    # smoothing: bilinear | vendor | nearest
python thermal_dmm_viewer.py csv FILE.vom [OUT.csv]          # multimeter recording
```

If `OUT.csv` is omitted, the CSV is written next to the input file with the same
name. Temperatures in CSV files are always in °C.
