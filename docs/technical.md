# Technical notes

How the meter's files are structured, how this tool differs from the vendor
software, and how its results were verified.

## File formats

Checked against real files from a ToolTop ET13S, and against the results of the
vendor's Windows software, IR-DMM (version 1.0.0.0, 2022).

### Thermal image (`.bmp`)

A normal 240×320 BMP screenshot (16-bit RGB565) with extra data appended after it.
On a real ET13S file the extra block is exactly 4,610 bytes, with nothing after it.

| Where | Type | Meaning |
| --- | --- | --- |
| bytes 2–5 of the file | uint32 LE | The BMP header's file-size field, which is the offset of the extra data |
| offset + 0 | uint16 LE | Format version. Must be **200** |
| offset + 2 | 48×48 × uint16 LE | Raw values, row by row from the top-left |

- Temperature in °C = (raw − 3200) / 64, so the resolution is 1/64 °C.
- BMP header bytes 8–9 (the second "reserved" field, normally 0) always hold
  26119 (0x6607) on the ET13S, the same in every file and scene tested. It is
  not a checksum of anything in the file. It's most likely a fixed firmware value
  and carries no information. `info` and `--json` show it in case another model
  or firmware writes something different.
- Nothing else is stored: no emissivity, palette, timestamp or settings. The
  date/time and readings visible on the screenshot are part of the picture
  itself. The file's date on the meter's drive is the only other timestamp.
- The 48×48 grid maps to the 240×240 thermal area at screenshot pixels x = 0–239, y = 33–272.

The vendor software only opens files with version 200. Earlier models use a
different format, described [below](#older-colour-scale-format-bmp-experimental).
If a file has a different version but its data length is a perfect square
(for example 192×192), this tool still loads it and shows a warning that the
readings may be wrong.

### Multimeter recording (`.vom`)

Plain ASCII text with Windows (CRLF) line endings. A real file from an ET13S
([`samples/VOM_0001.VOM`](../samples/VOM_0001.VOM)) starts like this:

```
FeelTech Vom Data
Mode:DC/V
Cycle:0.5s
Max:3048.0mV
Min:-4.0mV
Len:67
0.0
0.1
0.2
0.3
-2.4
-3.5
...
3044.10
3043.11
```

| Line | Content |
| --- | --- |
| 1 | `FeelTech Vom Data`, a fixed title. FeelTech is FeelElec's earlier brand name. |
| 2 | Measurement mode, e.g. `DC/V` |
| 3 | Seconds between samples |
| 4–5 | Highest and lowest sample in mV, always with `.0` |
| 6 | Number of samples |
| 7… | One sample per line |

There is nothing else in the file: no date, time, range or meter settings.

**The samples are not decimals.** Each sample line is `<whole mV>.<sample
number>`: sample 10 at 3044 mV is written `3044.10`, and the first samples are
`0.0`, `0.1`, `0.2`… even when the reading is exactly 0 mV. In the real file
the number after the dot equals the line's sample index on all 67 lines, and the
whole-number parts reproduce the header's Max (3048) and Min (−4) exactly. It
looks like a firmware formatting bug: the counter is printed where a decimal
should be.

The vendor software reads these as decimals, so its charts are slightly wrong
(−4 mV shows as −4.8 mV; the error grows with the sample number, up to almost
1 mV). This tool strips the counter, but only when **every** sample line follows
the pattern, so a file with genuine decimals is still read as written. The
command-line summary notes when this happened, and `--json` reports it as
`sample_counter_suffix`.

Sample *i* is at time *i* × Cycle seconds. The vendor program silently draws
nothing when the number of samples doesn't match `Len`. This tool plots the data
anyway and shows a warning.

### Older colour-scale format (`.bmp`, experimental)

The format opened by the vendor's **older** PC software (`IR-DMM.rar` on
FeelElec's 90×120 documentation page, November 2022). It is most likely what
the earlier models write: the 24×32 and 90×120 meters (ToolTop ET11S / ET12S,
FeelElec FR01A/B/C). **No real file from those meters
has been tested yet**; so far it has only been checked with synthetic test files.

There is no appended data block. Everything is hidden in the picture itself,
which is why the format survives only in lossless files (BMP/PNG, not JPG):

| Where | Read as | Meaning |
| --- | --- | --- |
| pixel (0, 0) | colour as an RGB565 number ÷ 100 | minimum temperature, °C |
| pixel (239, 0) | colour as an RGB565 number ÷ 100 | maximum temperature, °C |
| pixel (238, 0) | colour as an RGB565 number | format version |
| colour bar, 255 rows (24–278) | colours, bottom = coldest | the temperature scale |

RGB565 packing: `(R >> 3) << 11 | (G >> 2) << 5 | (B >> 3)`.

Two screen layouts, chosen by the version number:

| Version | Thermal area (x, y, w, h) | Colour bar column |
| --- | --- | --- |
| 12 | 0, 0, 214, 286 | x = 217 (right) |
| anything else | 36, 24, 186, 250 | x = 25 (left) |

To read a temperature, the clicked pixel's exact colour is looked up on the
colour bar (first match), giving a position *i* from 0 (bottom) to 254 (top):

    temperature = min + i × (max − min) / 256

Consequences, all inherited from the vendor design:

- Resolution is (max − min) / 256: about 0.1 °C for a 25 °C range.
- The top of the bar (*i* = 254) reads slightly below the stored maximum,
  because the formula divides by 256.
- Pixels whose colour isn't on the bar (text, markers, the three data pixels)
  have no temperature.
- In the version-12 layout the thermal area includes pixel (0, 0), which holds
  the minimum value rather than a temperature colour.
- The vendor program shows "≥ max" for a colour that isn't on the bar in the
  version-12 layout (a bug); this tool shows no value instead.

To avoid treating ordinary pictures as thermal images, this tool only accepts a
file as colour-scale when it is exactly 240×320, has no appended data block,
the min/max are within 0–600 °C with min < max, the bar has at least 32
distinct colours, and at least half of the thermal area uses bar colours.
In tests, random pictures were all rejected.

Grid-only features (smoothing, raw cells, grid outline, overlay) don't apply
to this format and are greyed out. The ET13S doesn't use it: in its files the
three data pixels are black.

## How the data processing was verified

- The **vendor** mode was compared with the vendor software's results on
  30 test grids: random raw values across the full 16-bit range, values within
  the meter's −20 to 550 °C range, and realistic hot-spot scenes. All
  1,728,000 displayed values matched, and so did the underlying
  single-precision numbers, including how the vendor software rounds when
  displaying them.
- The same comparison on the real sample file (`samples/IR_0013.BMP`) also
  matches exactly.
- Max/min and the file reading give the same results as the vendor software.
- **Bilinear** mode was checked to return each cell's exact value at its block
  centre and to stay within the measured range.

The automated tests (`tests/`) keep this in place. The vendor-mode result for
the sample file is checked against a fingerprint of the vendor software's
result, so any change that breaks the exact match fails the tests.

## Differences from the vendor software

No vendor code or files are included in this repository; all code here was
written from scratch.

Things this tool deliberately does differently from the vendor software:

- **Interpolation:** see [Smoothing modes](../README.md#smoothing-modes). The
  vendor's smoothing gives each sensor cell zero weight at its own position.
- **`.vom` headers:** this tool finds the numbers in the header lines wherever
  they are, so small changes in spacing don't break reading.
- **Sample count mismatch:** the vendor software silently shows nothing; this
  tool plots the data and warns.

## Accuracy against the meter

The meter writes its own centre-spot reading onto the screenshot (the large
number in the top bar). It should match the **centre pixel** value from
`python thermal_dmm_viewer.py FILE --no-gui` to within about 0.1–0.2 °C. On real ET13S files: screen
23.1 / 34.2 / 34.0 °C against raw centre cell 23.06 / 34.28 / 33.98 °C. The meter
computes its spot reading from its full-resolution sensor, so it can't match the
stored 48×48 grid exactly.

The MAX and MIN markers should sit where the meter drew its own red and green
marker boxes. That confirms the grid isn't mirrored or rotated.

The two small numbers next to the main reading (red and green) are the meter's
max and min, cut to whole degrees, and they also set its colour scale. On real
ET13S files they agree with **Highest / Lowest**: 34 / 21 on screen against
34.52 / 21.42 °C in the data, and 35 / 22 against 35.12 / 22.41 °C. In one file
the screen showed 28 / 20 while the stored data spans 21.88–30.41 °C. The meter
seems to update these numbers (and the colours) slightly later than the frame
it saves, so expect occasional differences when the scene is changing.

If the centre reading is far off, or the markers don't line up, the file
format differs from the one described above. Please open an issue with the
output of `python thermal_dmm_viewer.py FILE --no-gui` and, if possible, the file.

## Firmware

Updating the meter is separate from the saved-file formats above: the meter
flashes itself from an encrypted `UPDATA.FED` file, with the key in its own
bootloader. See [../firmware/README.md](../firmware/README.md) for the analysis.

## Code overview

Everything is in `thermal_dmm_viewer.py`, ordered from low-level to high-level. A code map at
the top lists the sections. Parts that are unusual are commented where they occur:

- **Exact vendor match:** `interpolate_vendor()` and `format_temp()`
  use single-precision arithmetic and the same display rounding as the vendor
  software, so results match it exactly.
- **Fast pixel work without numpy:** `blend()` packs bytes into one large integer
  to blend a whole image with a single multiplication. `resize_nearest()` and
  `render_view()` use `operator.itemgetter`, `bytes.translate` and extended slices
  so the work runs in C.
- **One coordinate mapping:** `source_index()` / `cell_starts()` are used by the
  renderer, the mouse handling and the cell outlines, so they always agree at any zoom.
- **GUI theming:** `Theme` in `run_gui()` uses the native Tk themes on Windows/macOS
  and builds the flat theme on Linux from Tk's built-in "clam" theme.

Run the tests with:

```
python -m unittest discover -s tests -v
```
