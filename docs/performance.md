# Performance guide

[← Back to README](../README.md)

Last reconciled: 2026-08-09.

There are two timing scopes in these records. They must not be compared
without naming the scope:

- **plugin check**: camera fetch, image preparation, AI processing, and
  result handling — the closest number to what you experience;
- **AI only**: the resident detection process by itself, excluding most of
  the camera and image-preparation work — the best number for comparing
  hardware.

## Plugin-check measurements

These are the closest measurements to what an OctoPrint user experiences.

| Device | Uses | Time/check | Ceiling/min | Notes |
|---|---|---:|---:|---|
| Raspberry Pi 5, 64-bit | CPU | 271 ms | 221 | 100% CPU pool |
| Raspberry Pi 5, 32-bit | CPU | 411 ms | 146 | 32-bit userspace |
| Raspberry Pi 4 / CM4 | CPU | 2.6 s | 23 | 100% CPU pool |
| Raspberry Pi 3B+ | CPU | 7.7 s | 8 | unexplained slowdown versus the AI-only run is retained as measured |
| RK3566 | NPU | 279 ms | 215 | one NPU core |
| RK3576 | NPU | 165 ms | 364 | two NPU cores |
| RK3588 | NPU | 96 ms | 625 | recorded before performance-core-aware CPU selection; see note below |
| Allwinner A733 | NPU | 110 ms | 545 | |

The RK3588's 96 ms result predates performance-core-aware CPU selection:
the older rule happened to pick the four slower cores for the CPU side of
the work. After the change, the AI-only path measured 45.5 ms on two fast
cores versus 91.1 ms on two slow ones. A fresh full camera-to-result
measurement is still required before replacing 96 ms in the table.

## AI-only measurements

These isolate the detection process more closely and are useful for comparing
silicon, but they are not the final rate at which the plugin can inspect
camera frames.

| Device | Uses | Median | Ceiling/min |
|---|---|---:|---:|
| Raspberry Pi Zero 2 W / Pi CM0 | CPU | 4,931 ms | 12.2 |
| Raspberry Pi 3B+ | CPU | 4,154 ms | 14.4 |
| Raspberry Pi 4 / CM4 | CPU | 2,582 ms | 23.2 |
| Raspberry Pi 5, 32-bit | CPU | 372 ms | 161 |
| Raspberry Pi 5, 64-bit | CPU | 271 ms | 221 |
| Ryzen 9 5950X | CPU | 73.4 ms | 817 |
| RK3566 | NPU, one core | 295.0 ms | 203 |
| RK3576 | NPU, two cores | 89.0 ms | 674 |
| RK3588 | NPU, three cores | 52.5 ms | 1,143 |
| Allwinner A733 | NPU | 63.2 ms | 949 |
| Jetson Orin Nano Super | GPU | 179 ms | 335 |

The x86_64 measurement used the production detection process and the exact
same model file shipped to ARM. Against the recorded ARM64 run, all 34
reference images kept the same detection count and alarm decision; scores
differed by at most small rounding, so results are portable across CPU
architectures even though bit-for-bit identical numbers are not guaranteed.

`Raspberry Pi 2-class CPU` in the sensitivity help is a starting-point label,
not a Raspberry Pi 2 Model B benchmark. No real Pi 2 Model B measurement is
recorded. The measured small-board tier is the **Raspberry Pi Zero 2 W / Pi CM0**, which is
a different, newer machine.

The actual sustained check rate may be lower than every ceiling above because
camera frame rate, sampling policy and Minimum Check Interval can all become
the limiting stage.

## Why the Jetson Orin is not faster than it could be

The Jetson build runs on **Vulkan**, the portable GPU interface — not on
NVIDIA's own TensorRT. That is a deliberate trade, and it costs real speed:
TensorRT was measured at **6.4 ms** per image on the same Orin Nano Super
against Vulkan's **179 ms**.

Jetson is not executing AMD code. On Jetson, the generic runner submits Vulkan
work through NVIDIA's Vulkan driver; on a qualified AMD machine, the same PTE
and protocol use AMD's Vulkan driver instead. The portability cost comes from
using one cross-vendor ExecuTorch/Vulkan graph rather than NVIDIA-specific
TensorRT/CUDA graph fusion, tuned kernels and memory planning.

The reason is that TensorRT does not produce a portable file. It compiles an
engine by timing every layer on the exact machine it is building for, and
the result is locked to that GPU architecture, that CPU architecture and
that TensorRT version — a file built on one machine is rejected outright by
a different one, and building takes minutes. Shipping that would mean either
a compile step on every user's board or a separate download per GPU, driver
and version combination. Vulkan gives one build that runs on qualified GPUs
as-is.

**179 ms is 335 checks per minute, which is far beyond what print monitoring
needs** — a monitor sampling every few seconds uses single digits. The speed
left on the table is real, but it buys nothing here, and portability is what
keeps the plugin installable without a per-machine build.

## Which boards were actually tested

Every number on this page came from one of these physical machines, not
from a simulator or an estimate:

| Chip | Board measured | Uses |
|---|---|---|
| Broadcom (Raspberry Pi) | Raspberry Pi 5, Pi 4 / CM4, Pi 3B+, Pi Zero 2 W / Pi CM0 | CPU |
| AMD x86_64 | Ryzen 9 5950X desktop | CPU |
| Rockchip RK3566 | Orange Pi 3B, BIQU CB2 | NPU |
| Rockchip RK3576 | Radxa ROCK 4D | NPU |
| Rockchip RK3588 | LubanCat-4 | NPU |
| Allwinner A733 | Radxa A733 | NPU |
| Jetson Orin | Orin Nano Super | GPU |

The **BIQU CB2** is the one board here designed for 3D printers, so it is
worth calling out: on Armbian it auto-selects its NPU with no manual
configuration, exactly like the Orange Pi 3B.

**Other boards with the same chip should work but are untested.** The
runtime is selected by the chip, not by the board, so another RK3566 board
is expected to behave like the two above — expected, not verified. Vendor
system images differ in which NPU driver they ship, and that is the part
most likely to vary. If a board is not listed here and its NPU is
unavailable, PiNozCam falls back to the CPU rather than failing.

## No add-on AI accelerator needed

PiNozCam does **not** need a plug-in AI accelerator such as the Raspberry Pi
AI Kit / AI HAT+ (Hailo-8L). A plain Raspberry Pi 5 already reaches 221
checks per minute on its own CPU, which is far more than print monitoring
needs.

If you already own one, PiNozCam will not use it — it is not among the
supported backends, and detection simply runs on the CPU instead. The
accelerated options listed above are all built into the board's own chip
(Rockchip and Allwinner NPUs, the Jetson Orin GPU), not accessory cards.

## 32-bit vs 64-bit userspace

If the board supports both, choose a **64-bit AArch64 userspace**. This is the
recommended ARM installation and is usually faster. On the same Pi 5, the
measured rate was **221 AI images/min at 64-bit** versus **161 images/min at
32-bit**. The 32-bit ARMHF path remains fully supported, but its 372 ms/image
was about 37% slower than the 64-bit path's 271 ms/image in that comparison.
The exact gain varies by board and operating system, so this is a platform
recommendation rather than a promised speed multiplier.

The installer uses Python's actual pointer size rather than only `uname -m`.
This matters on the common Raspberry Pi configuration with a 64-bit kernel
and 32-bit userspace: it correctly installs the 32-bit runtime.

## Memory footprint

The detector itself peaked at about **146 MB** in the measured CPU setup. A
complete 512 MB Pi Zero 2 W / Pi CM0 system peaked at 353 MB of 362 MB usable.
Candidate frames add resolution-dependent memory: capacity 16 is about 44 MB
of pixels at 720p and 100 MB at 1080p. Keep the default capacity on
low-memory boards.

A 1 GB board is enough for a normal headless OctoPrint + PiNozCam
installation. A desktop session, many memory-heavy plugins, or a very large
1080p candidate buffer may justify more RAM.

### Raspberry Pi Zero 2 W / Pi CM0: enable zram

A 512 MB Zero 2 W or Pi CM0 has very little memory headroom once OctoPrint
and the CPU detector are both resident. Enable **zram** so memory pressure is
handled by compressed RAM before the system falls back to the much slower SD
card swap file. Keep `/var/swap` enabled as a last-resort safeguard; give zram
the higher priority instead of disabling the fallback.

PiNozCam does not install or enable zram automatically. This extra system
setup is recommended for the 512 MB Zero 2 W / Pi CM0 tier; boards with more
memory do not normally need it. The plugin runs as the unprivileged OctoPrint
user and has neither the authority nor the intention to install operating-
system packages, change swap devices, or edit boot firmware settings. A device
administrator must perform the following setup once.

On Raspberry Pi OS Bookworm, install the small `zram-tools` package:

```bash
sudo apt-get install zram-tools
sudoedit /etc/default/zramswap
```

Use these values in `/etc/default/zramswap`:

```ini
ALGO=lz4
PERCENT=50
PRIORITY=100
```

Then enable it immediately and on future boots:

```bash
sudo systemctl enable --now zramswap.service
cat /proc/swaps
```

`/dev/zram0` should have priority `100`; the normal `/var/swap` file should
remain at its lower priority (typically `-2`). zram is compressed swap, not
additional physical memory: its purpose here is to avoid SD-card I/O for as
long as possible. It cannot compensate for a large firmware memory reservation
or an oversized frame buffer.

The following settings are **required** when the OctoPi installation uses
the legacy Raspberry Pi camera firmware:

```ini
start_x=1
gpu_mem=128
```

Do not disable or reduce them merely to make PiNozCam faster: doing so can
break a camera that depends on the legacy stack. They are mandatory camera
platform settings when that stack is active, rather than optional PiNozCam
tuning. On the measured 512 MB Zero 2 W, those values left only 371 MB to
Linux. Even with zram enabled, the real plugin reached
**14,540 ms/check** at two of four CPU cores and eventually touched the SD
swap fallback. The clean one-shot Speed Test had looked faster, so it was not
a valid substitute for measuring the sampler, decoded frames, OctoPrint and
the resident detector together.

Only after confirming that the system uses the current OctoPrint camera stack
or a remote/custom camera source and does not depend on the legacy firmware,
the measured low-memory setup may instead use:

```ini
start_x=0
gpu_mem=32
```

That returned about 97 MB to Linux (468 MB usable instead of 371 MB). Treat
the `128`/`1` pair as a legacy-camera requirement with a significant memory
and performance cost whenever that camera stack is active. Enable zram in
either case.

## CPU cores and print quality

**CPU Cores Used** selects a percentage of the detected performance-core
pool. The relevant delay is not how late AI finishes: detection is allowed to
take seconds. It is how late other latency-sensitive work becomes while AI is
busy. The following measurements ran the production C++ detector continuously
while a separate thread requested a wake every 10 ms. The table reports that
thread's lateness.

The timing columns describe that wake-up delay:

- **Median** is the typical delay: half of the wake-ups were earlier and half
  were later.
- **p99** is the useful steady worst-case measure: 99% of wake-ups were no
  later than this value. Use this column when comparing CPU settings.
- **Worst** is the single largest delay in the whole run. It may include an
  unrelated one-off kernel, network, logging or storage event, so it should
  not be treated as a repeated pause or as proof that the print head stopped.

For example, `Worst 20 ms` means one test-thread wake-up happened 20 ms later
than requested. It does not mean AI paused the printer for 20 ms: printer
firmware normally continues executing motion already queued in its own
buffer. Repeated large p99 values, serial communication errors, or visible
motion stalls matter more than one Worst sample. Every loaded row below used
`nice=10`, so the detector already yielded CPU scheduling priority to ordinary
OctoPrint work.

| Board and CPU setting | Median | p99 | Worst | AI checks/min |
|---|---:|---:|---:|---:|
| Raspberry Pi 4 / CM4, AI idle | 0.111 ms | 0.145 ms | 6.007 ms | — |
| Raspberry Pi 4 / CM4, 25% (1 core), nice 10 | 0.095 ms | 0.202 ms | 2.709 ms | 8.3 |
| Raspberry Pi 4 / CM4, 100% (4 cores), nice 10 | 0.124 ms | 2.868 ms | 12.260 ms | 27.9 |
| Pi Zero 2 W / Pi CM0 tier, AI idle | 0.131 ms | 0.190 ms | 9.016 ms | — |
| Pi Zero 2 W / Pi CM0 tier, 25% (1 core), nice 10 | 0.179 ms | 0.503 ms | 19.438 ms | 3.5 |
| Pi Zero 2 W / Pi CM0 tier, 100% (4 cores), nice 10 | 0.233 ms | 5.404 ms | 23.546 ms | 11.4 |
| Raspberry Pi 3B+, AArch64, AI idle | 0.117 ms | 0.159 ms | 1.194 ms | — |
| Raspberry Pi 3B+, AArch64, 25% (1 core), nice 10 | 0.153 ms | 0.224 ms | 3.434 ms | 5.2 |
| Raspberry Pi 3B+, AArch64, 50% (2 cores), nice 10 | 0.171 ms | 0.336 ms | 4.158 ms | 10.1 |
| Raspberry Pi 3B+, AArch64, 75% (3 cores), nice 10 | 0.178 ms | 0.486 ms | 6.622 ms | 14.1 |
| Raspberry Pi 3B+, AArch64, 100% (4 cores), nice 10 | 0.220 ms | 1.322 ms | 4.790 ms | 17.8 |
| Ryzen 9 5950X, x86_64, AI idle | 0.072 ms | 0.444 ms | 0.892 ms | — |
| Ryzen 9 5950X, x86_64, 25% (8 logical cores), nice 10 | 0.076 ms | 0.090 ms | 1.030 ms | 418.9 |
| Ryzen 9 5950X, x86_64, 50% (16 logical cores), nice 10 | 0.074 ms | 0.221 ms | 1.043 ms | 687.6 |
| Ryzen 9 5950X, x86_64, 75% (24 logical cores), nice 10 | 0.068 ms | 0.329 ms | 1.821 ms | 762.6 |
| Ryzen 9 5950X, x86_64, 100% (32 logical cores), nice 10 | 0.073 ms | 1.860 ms | 3.403 ms | 785.9 |

The physical small-board run used a Pi Zero 2 W; Pi CM0 uses the same
performance tier and recommendation. Both boards remained unthrottled. Each
row contains at least 6,000 wake samples. The p99 column is the useful steady
comparison; a single worst value can also come from unrelated operating-system
work, as the idle rows show.

The Pi 3B+ and x86_64 rows were measured on 2026-08-09 with the 1.1.0rc1
production CPU daemon and PTE. The Pi was a four-core Cortex-A53 running a
64-bit AArch64 Debian 13 userspace at 1.4 GHz. The x86 host was a Ryzen 9
5950X with 16 physical cores and 32 logical CPUs. Each idle row contains
exactly 6,000 wake samples; the loaded rows contain 6,001–6,887 because a
request already in progress was allowed to finish after the 60-second target.
The detector used its normal 640×384 input and `nice=10`; camera fetch and the
rest of the OctoPrint plugin were intentionally outside this AI-only test.

The sustained rates correspond to mean daemon request times of 11,480, 5,919,
4,241 and 3,372 ms/check on the Pi 3B+ at 25%, 50%, 75% and 100%, respectively.
On x86_64 they were 143.24, 87.27, 78.68 and 76.35 ms/check. These are
AI-process round trips derived from the sustained rate, not plugin-check times
and not the wake-delay measurements in the three columns beside them.

At 25%, periodic work stayed close to its idle timing. At 100%, inference was
roughly three times faster, but the p99 wake delay rose to 2.9 ms on the CM4
and 5.4 ms on the Zero 2 W. The fresh Pi 3B+ run rose from 0.224 ms at 25%
to 1.322 ms at 100%; x86_64 rose from 0.090 ms to 1.860 ms while throughput
increased from 419 to 786 checks/min. These values are scheduling delay, not an
equal-length pause at the print head: printer firmware normally keeps queued
motion buffered. They show why reserving CPU headroom is still useful on a
slower board.

The current detector also runs as an independent C++ process. Model execution
and post-processing no longer hold the OctoPrint Python interpreter's GIL, so
the old in-process blocking path is absent; ordinary CPU and memory-bandwidth
competition is what remains. For a Pi 4 / CM4, Pi Zero 2 W or Pi CM0, 25% is
the conservative starting point. Increase it when faster checks matter, or
return to 25% if the rest of OctoPrint feels less responsive.

**Minimum Check Interval** of `0` means as fast as the camera and backend
allow. Raise it to reserve more resources for OctoPrint and gcode streaming.

The measurements used the same resident-daemon protocol as the plugin and
required no NumPy.

## Failure-ratio resolution on a slow board

The decision code does not convert the required frame count to an integer. It
computes the exact floating-point fraction:

```text
ratio = alarming_frames / frames_currently_in_window
trigger = armed AND ratio >= configured_failure_ratio
```

For a window containing `N` frames and a configured ratio `R`, the equivalent
minimum alarming-frame count is therefore:

```text
minimum_alarming_frames = ceil(R * N)
```

At 12.2 checks/min, a 90-second window contains about 18 or 19 frames. With
`R = 0.01`:

```text
ceil(0.01 * 18) = ceil(0.18) = 1
ceil(0.01 * 19) = ceil(0.19) = 1
```

This does **not** make the threshold zero: zero alarming frames still produce
`0 / N = 0`, which is below 0.01. It does make 1% effectively a **one alarming
frame** criterion on that slow board. The smallest non-zero ratio the window
can represent is 1/18 = 5.56% (or 1/19 = 5.26%); every configured value from
1% through roughly 5% has the same one-frame outcome.

To require at least two alarming frames in an 18-19-frame window, configure
6% or more. To require at least three, configure 12% or more. These are
discrete-sampling consequences, not floating-point rounding bugs.

One more guard applies at the beginning of detection: PiNozCam does not act
until both 20 frames and 30 seconds have been observed. At 12.2 checks/min,
20 frames take about 98 seconds, so a newly started detector cannot trigger
inside its first 90 seconds even if the Evaluation Window is set to 90 s.
After warm-up, however, one alarming frame is sufficient while the ratio is
configured at 1% and the window holds only 18-19 frames.
