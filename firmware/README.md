# Firmware notes

Notes on the firmware of FeelElec / ToolTop thermal multimeters (FR01 series;
ToolTop ET11S / ET12S / ET13S).

**Status: the update file is encrypted. No open firmware exists yet, and this
page does not contain one.** It documents what the update looks like, what has
been ruled out, and what would be needed to go further. The goal is to learn how
these devices work and to lay groundwork for anyone who wants to continue.

> ⚠️ Do not flash a file from one model onto another. Each build targets one
> sensor and screen. A wrong file will most likely brick the meter, and there is
> no known recovery path short of a hardware programmer.

## How updating works (from the vendor documentation)

The meter has no network or serial update path. In USB mode it appears as a
small USB mass-storage drive called `FEELELEC`. To update, you copy a single
file named exactly `UPDATA.FED` to that drive, unplug, pick the file on the
meter's Storage screen, and the meter reboots and flashes itself. So all the
decryption and flashing happens **inside the meter's own bootloader** — the PC
is only a way to deliver the file.

This matters: the decryption key (if any) lives in the meter, not on the PC.
The vendor's PC software only opens saved `.bmp` and `.vom` files; it has no
update function and doesn't talk to the meter. So the PC side gives us nothing
here.

## The file analysed

| | |
| --- | --- |
| Source | FeelElec 90×120 model (ToolTop ET12S), firmware **V117** |
| From | `doc.feelelec.cn` → 90×120 → "USB operating instructions / firmware upgrade" |
| Name inside the zip | `UPDATA.FED` |
| Size | 134,432 bytes (0x20D20) |
| SHA-256 | `7121fa7d637e118fe8c5dacaca3ee11e4c9a899ac88f0181eab96bcf751fb66b` |

The file itself is **not included** in this repository (it is the vendor's
copyrighted firmware). Download it yourself from the link above if you want to
reproduce the analysis, then run:

```
python firmware/tools/fedtool.py UPDATA.FED
```

## What the analysis shows

Full output is in [`analysis.txt`](analysis.txt). Summary:

| Measurement | Result | Meaning |
| --- | --- | --- |
| Entropy | 7.9985 bits/byte | Indistinguishable from random |
| Chi-square vs uniform | 271.6 (255 dof) | Within the random range (≈ 255 ± 23) |
| Bit ratio | 0.4996 | Perfectly balanced |
| Distinct byte values | 256 / 256 | Every value present |
| Duplicate 16 B / 8 B blocks | 0 / 0 | No ECB-mode or repeated-plaintext pattern |
| Repeating-XOR excess over random | ≈ 0 | No repeating-key XOR of any tested period |
| First 4 bytes | `3b 22 6a 8c` | No known container/compression magic |
| Trailer CRC-32 / MD5 / SHA-1 / SHA-256 | none match | No plaintext integrity field at the end |
| Printable strings | none | No version string, path or symbol |

**Conclusion: the payload is encrypted (or otherwise whitened to look random).**

What this rules out:

- Not a plain or compressed image (gzip, zlib, xz, lzma, zip, bzip2, UPX, a bare
  ELF or U-Boot uImage): those have recognisable magic bytes and lower entropy.
- Not a simple obfuscation such as repeating-key XOR, byte rotation or a fixed
  substitution: those leave statistical fingerprints this file doesn't have.
- Not a block cipher in ECB mode: that would repeat 16-byte blocks wherever the
  plaintext repeated (firmware has lots of `0x00`/`0xFF` runs); there are zero
  repeats.

What it's consistent with (can't be told apart from one file): a stream cipher,
or a block cipher in CBC/CTR, with the key in the meter's bootloader.

### Why one file isn't enough

With a single encrypted blob there is no lever to pull. The useful next step is
**more files**, because differences between them leak structure:

- **Two versions for the same model** (e.g. V115 and V117). If the cipher is CTR
  or CBC with a fixed key/IV, the two ciphertexts will match byte-for-byte
  wherever the firmware didn't change, exposing block boundaries and which
  regions are code vs data. `fedtool.py` compares equal-length files and reports
  the differing byte positions for exactly this reason.
- **The same version for different models** (24×32 vs 90×120 vs 192×192). Shared
  header or padding regions would stand out.

If you have any other `UPDATA.FED` files, please open an issue — even old ones.

## Hardware route (the reliable way in)

Because the key is in the meter, the practical path to open firmware is through
the hardware, not the file:

1. **Open the meter and identify the main MCU.** The FeelElec FY-series
   signal generators use an ST **STM32F103** (ARM Cortex-M3); a related part is
   likely here, but this must be confirmed by reading the chip markings.
2. **Find the debug port** (SWD: SWDIO, SWCLK, GND, often also NRST and 3V3).
   Many of these boards leave test pads or a header.
3. **Try to read the flash** with an ST-Link / J-Link and OpenOCD. If the
   readout-protection (RDP) bit is not set, you get the running firmware
   directly — including whatever routine decrypts `UPDATA.FED`, which is the key
   to the whole format. Chinese test gear often leaves RDP disabled.
4. **Back up first.** If RDP is off, dump and save the flash before doing
   anything else; it's the only recovery image if a later step goes wrong.

With a flash dump, the decryption routine can be found (look for the code that
reads the `FEELELEC` volume and for an AES/SM4/TEA key schedule), and from there
the `.FED` format can be fully documented and an open build produced. That work
is still to be done.

## Toward open firmware

Realistic order of work:

1. Confirm the MCU and whether SWD is accessible and unlocked. *(blocked on
   hardware)*
2. Dump the stock firmware; identify the decryptor and the `.FED` layout.
3. Document registers for the display, the thermal sensor front-end and the
   multimeter's ADC.
4. Only then, a minimal open firmware (self-test, display, live thermal image).

Steps 2–4 need step 1, which needs physical access to a meter. Everything on
the PC side that could be done without opening a meter has been done and is in
this repository: the file format of the *saved images* is fully documented (see
[`../docs/technical.md`](../docs/technical.md)), and the update file is
characterised here.

## Reproducing

```
# Download UPDATA.FED from the vendor doc site (see above), then:
python firmware/tools/fedtool.py UPDATA.FED
python firmware/tools/fedtool.py --json UPDATA.FED        # machine-readable
python firmware/tools/fedtool.py v115.FED v117.FED        # compare two files
```

`fedtool.py` is standard-library Python 3 and never modifies the input.

## Legal note

This documents the update file through statistical analysis of the file
itself. No vendor firmware or code is redistributed here. Flashing
custom or mismatched firmware can permanently damage a device and void its
warranty; do it only on hardware you own and accept the risk for.
