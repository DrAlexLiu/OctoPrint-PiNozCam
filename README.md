# OctoPrint-PiNozCam

<p align="center"><strong>🔎 Detect failures. 📱 Check your printer. ⏸️ Pause or stop from your phone.</strong></p>
<p align="center">AI failure detection + phone monitoring &amp; control for OctoPrint —<br>
all yours on a <strong>~US$45 Raspberry Pi 5 (1 GB)</strong>. No cloud, no subscription.</p>

<div align="center">
  <img src="assets/images/failure_detection1.jpg" alt="PiNozCam detecting a print failure" width="40%">
  <img src="assets/images/failure_detection_side.jpeg" alt="PiNozCam overview-camera detection" width="48%">
</div>

<p align="center">
  <a href="https://github.com/DrAlexLiu/OctoPrint-PiNozCam/releases"><img src="https://img.shields.io/badge/version-1.1.0rc1-orange.svg" alt="Version 1.1.0rc1"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0-blue.svg" alt="License AGPL-3.0"></a>
  <a href="#-supported-platforms"><img src="https://img.shields.io/badge/platform-Linux-lightgrey.svg" alt="Linux"></a>
  <a href="https://discord.gg/gv4tKJ2ZKr"><img src="https://img.shields.io/discord/1158238902197424251.svg?label=Discord&amp;logo=discord&amp;logoColor=ffffff&amp;color=7389D8&amp;labelColor=555555" alt="Join Discord"></a>
</p>

PiNozCam watches your OctoPrint camera, spots failures, and alerts you —
or pauses or stops the print for you. Everything runs on your own machine,
and camera frames never leave your network.

| 🔎 **AI Failure Detection** | 📱 **Remote Printer Monitor & Control** |
|---|---|
| Watches your camera frames locally | Press **Check** in Telegram or Discord |
| Shows every detection with boxes | See the camera and printer status anytime |
| You decide how sensitive it is | Mute alerts, Pause or Resume the print |
| Alert, Pause, or Stop — your call | Stop the print with one confirmation tap |

## 📱 Your printer, in your pocket

Connect Telegram or Discord: AI failure alerts land on your phone, and you
can see the camera, pause, or stop the print right from the chat. Don't
wait for an alert — press **Check** whenever you're curious.

| Telegram | Discord |
|---|---|
| <img src="assets/images/telegram_remote_control.jpg" alt="Telegram remote printer monitor and control" width="360"> | <img src="assets/images/discord_notification.jpg" alt="Discord remote printer monitor and control" width="360"> |

- 🔍 **Check** — current camera view + printer status
- 🔇 **Mute / Unmute** — quiet alerts for this print
- ⏸️ **Pause / Resume** — step in from anywhere
- ⏹️ **Stop** — cancel with a confirmation tap
- 🚨 **Failure alert** — the analysed image, boxes included
- 🖨️ **Multiple printers** — one chat, all your printers

Setup guide: [docs/notifications.md](docs/notifications.md)

## ✨ Why PiNozCam

- **100% local AI** — no account, no cloud, no telemetry, no fee.
- **Cheap hardware is enough** — a ~US$45 Raspberry Pi 5 with 1 GB RAM
  runs it comfortably.
- **CPU, NPU, and GPU support** — Raspberry Pi, x86, selected Rockchip and
  Allwinner NPUs, and Jetson Orin.
- **Works with your existing camera** — whatever OctoPrint already uses.
- **Undetect Zones** — mask bed clips, cables, and logos that confuse the AI.
- **Gentle on your printer** — cap CPU use and check rate so printing
  always comes first.

> [!IMPORTANT]
> PiNozCam is a monitoring aid, not a safety system. Start with
> **Alert only**, watch a few prints, then decide whether to allow automatic
> Pause or Stop. Never leave a printer unattended just because monitoring
> is on.

## 🧩 Supported platforms

Any Linux machine with Python 3.7+ — every Raspberry Pi from the Zero 2 W / Pi CM0
up, any ARM64 or x86_64 PC, and NPU boards like the **Orange Pi 3B**,
**BIQU CB2**, Radxa ROCK 4D, LubanCat-4 and Radxa A733, plus the Jetson
Orin GPU. PiNozCam finds the fastest option your board has on its own, and
falls back to the CPU when there is nothing faster. *(No Windows, macOS,
FreeBSD, or Android/Octo4a.)*

If an ARM board supports both operating systems, prefer **64-bit AArch64**:
it is usually faster and is the recommended installation. The 32-bit ARMHF
runtime remains supported for boards and OctoPi images that require it.

### ⚡ How fast is it?

| Device | AI images/min |
|---|---:|
| Raspberry Pi 5 | 221 |
| BIQU CB2 / Orange Pi 3B (RK3566 NPU) | 203 |
| Jetson Orin Nano Super | 335 |
| Raspberry Pi 4 / CM4 | 23 |
| Radxa A733 (Allwinner NPU) | 949 |
| LubanCat-4 (RK3588 NPU) | 1,143 |
| Raspberry Pi 3B+ | 14 |

Even a few checks per minute is plenty to catch a failing print.

**And it's light on memory:** OctoPrint + PiNozCam together stay under
**512 MB** — even a 512 MB Pi Zero 2 W / Pi CM0 works. A **1 GB Raspberry Pi 5
(~US$45)** runs it comfortably. Full tables, 32-bit vs 64-bit numbers, and
memory figures: [docs/performance.md](docs/performance.md). On a 512 MB Zero
2 W / Pi CM0, an administrator should follow that guide's manual zram setup
before production use so memory pressure uses compressed RAM before slow
SD-card swap. PiNozCam cannot make system-level swap changes itself.

## 📦 Install

Install **PiNozCam** from **Settings → Plugin Manager → Get More**, or by
URL:

```text
https://github.com/DrAlexLiu/OctoPrint-PiNozCam/archive/refs/tags/1.1.0rc1.zip
```

The installer picks the right build for your machine automatically. Restart
OctoPrint, then follow the first-run wizard.

## 🚀 First run — three decisions

1. **Camera** — PiNozCam checks the webcam OctoPrint already uses.
2. **Sensitivity** — pick a preset; you can refine it later.
3. **Action on Failure** — start with **Alert only**.

Tune it like this: stay on **Alert only**, start sensitive, and step down
until false alerts stop bothering you. Full logic and preset tables:
[docs/detection-and-tuning.md](docs/detection-and-tuning.md)

## 📷 Camera tips

A rigid mount, even lighting, and a clean lens matter more than resolution.
Point a nozzle camera 5–10 cm from the nozzle, or use an overview camera
that keeps the whole part visible. MJPEG streams, HTTP snapshots, and local test
images all work.

Placement photos and the full checklist: [docs/camera.md](docs/camera.md)

## 🔒 Private by default

Frames, results, and settings stay on your OctoPrint machine. Nothing leaves
your network unless **you** connect Telegram or Discord — and then only the
analysed image and print status go out. Keep bot tokens secret like
passwords.

## 🖥️ OctoPrint interface

PiNozCam adds its own OctoPrint status tab and keeps all detector controls
together under **Settings → PiNozCam**.

<p align="center">
  <img src="assets/images/tab.jpg" alt="PiNozCam tab in OctoPrint" width="629">
</p>

<p align="center">
  <img src="assets/images/screenshot.jpg" alt="PiNozCam status panel in OctoPrint" width="520">
</p>

## 📖 License

Open source under [AGPL-3.0](LICENSE). Third-party notices:
[THIRD_PARTY_LICENSES/](THIRD_PARTY_LICENSES/).

## 🤝 Support

- 💬 Questions: [PiNozCam Discord](https://discord.gg/gv4tKJ2ZKr)
- 🐛 Bugs: [GitHub Issues](https://github.com/DrAlexLiu/OctoPrint-PiNozCam/issues)
