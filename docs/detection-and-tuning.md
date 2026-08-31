# Detection logic and tuning guide

[← Back to README](../README.md)

This guide explains exactly how PiNozCam decides a print is failing, and how
to tune that decision for your camera and printer.

## How detection is decided

For every valid analysed frame:

1. Boxes at or below **Detection Score Threshold** are ignored.
2. The union area of the remaining boxes is divided by the camera-content
   area.
3. If that fraction exceeds **Failure Area Threshold**, the frame is alarming.
4. PiNozCam calculates `alarming frames / frames in the Black Box`.
5. When that ratio reaches **Failure Ratio Threshold**, it performs the
   selected action once for that failure episode.

## The three thresholds

The three values apply at different stages. They are not three names for the
same confidence setting:

```text
box score > Detection Score Threshold
    ↓
accepted-box union area / camera-content area > Failure Area Threshold
    ↓
alarming frames / frames in the Black Box >= Failure Ratio Threshold
```

### Detection Score Threshold

This is applied to each individual detection box. A model score ranges from
`0` to `1`; only boxes **strictly above** this threshold contribute to the
failure area. Lowering it admits less-certain boxes and makes detection more
sensitive, but can create more false positives. Raising it keeps only more
confident boxes and can miss subtle failures.

### Failure Area Threshold

This decides whether one analysed frame is alarming. PiNozCam takes the union
of all accepted boxes, so overlapping pixels count once, and divides that area
by the real camera-content area. Black letterbox padding is excluded from the
denominator. An Undetect Zone suppresses detections in that region but remains
part of the camera-content area. Lowering the threshold allows a smaller
visible failure to mark a frame alarming; raising it requires a larger one.

### Failure Ratio Threshold

This applies across time. PiNozCam counts the alarming frames still inside
the **Black Box Length** and divides by all valid analysed frames still inside
it. When that ratio is equal to or above the threshold, the selected Action
on Failure can trigger after warm-up. Lowering it reacts when fewer recent
frames are alarming; raising it requires the failure to persist across more
of the recent history.

For example, suppose the detection scores are `0.95`, `0.90`, and `0.70`:

1. With Detection Score Threshold `0.85`, the `0.95` and `0.90` boxes count;
   the `0.70` box is ignored.
2. If the union of those two boxes covers 5% of the camera content and Failure
   Area Threshold is `0.04`, that frame is alarming because 5% is above 4%.
3. If 3 of the 20 frames in the Black Box are alarming, Failure Ratio is
   `3 / 20 = 0.15`. A Failure Ratio Threshold of `0.10` is therefore met.

The detector does not act until it has observed both 20 frames and 30 seconds.
Changing detection criteria discards results made under the old settings, so
an in-flight result cannot act using a newly saved policy.

Each frame is judged independently. PiNozCam does **not** learn a fixed object
away over time. If a bed clip, cable, logo, reflection, or dark gap is
consistently detected, draw an **Undetect Zone** over it on the PiNozCam tab.

## Sensitivity presets

The five Sensitivity presets change three values together:

| Preset | Suggested starting point | Detection score | Failure area | Failure ratio |
|---|---|---:|---:|---:|
| Lowest | Fast GPU/NPU | 0.93 | 0.06 | 0.13 |
| Low | Raspberry Pi 5 | 0.90 | 0.05 | 0.10 |
| Medium | Raspberry Pi 4 | 0.87 | 0.04 | 0.07 |
| High | Raspberry Pi 3 | 0.84 | 0.03 | 0.03 |
| Highest | Slow CPU / initial search | 0.80 | 0.02 | 0.01 |

These hardware labels are starting points, not compatibility requirements.
Editing one of the raw values changes the preset label to Custom.

## Recommended tuning workflow

1. Keep **Action on Failure** at **Alert only**.
2. Start at the **Highest** sensitivity preset.
3. Run several typical prints. Note missed failures and false alerts.
4. Move one step toward **Lowest** whenever false alerts dominate; stay when
   the balance is acceptable.
5. Only enable automatic **Pause** or **Stop** after the alert behaviour has
   been trustworthy across several prints.

**Black Box Length** is independent of Sensitivity. It is the recent detection
history used for the failure ratio; it does not record video or write a video
buffer to disk.

> [!WARNING]
> Very low ratios have coarse resolution on slow boards. If a 90-second
> window contains 18 checks, one alarming frame is already 5.6%; configured
> ratios from 1% through 5% therefore all behave as a one-frame criterion.
> The full arithmetic is in
> [performance.md](performance.md#failure-ratio-resolution-on-a-slow-board).
