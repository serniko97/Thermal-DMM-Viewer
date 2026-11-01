# Sample files

`IR_0013.BMP` is an original, unmodified thermal screenshot from a
ToolTop ET13S (taken 2025-12-03). The meter showed 23.1 °C at the centre spot.
The automated tests use it, so please don't edit it.

`VOM_0001.VOM` is an original multimeter recording from the same meter:
67 DC-voltage samples at 0.5 s intervals, switching between 0 V and about
3.05 V. It shows the meter's sample-numbering quirk (see
[docs/technical.md](../docs/technical.md#multimeter-recording-vom)).

Try them:

```
python thermal_dmm_viewer.py samples/IR_0013.BMP
python thermal_dmm_viewer.py samples/VOM_0001.VOM
```

Files from other meter models are welcome. Please add them with a note about the
model and firmware version (Settings → About on the meter).
