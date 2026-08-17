# Allwinner NPU setup

[← Back to README](../README.md)

PiNozCam supplies the chip-specific model and runner for the Allwinner A733
and T527. The board image must supply two system components:

- a kernel with the VeriSilicon VIP driver enabled and bound to the NPU,
  exposing `/dev/vipcore`; and
- the matching VIPLite userspace libraries.

Which libraries those are depends on the chip, and the two sets are not
interchangeable:

| chip | VIPLite | libraries the board must provide |
|---|---|---|
| A733 | v2.0 | `libNBGlinker.so`, `libVIPhal.so` |
| T527 | v1.13 | `libVIPlite.so`, `libVIPuser.so` |

The userspace library and the kernel module must be the same generation.
Installing the other one does not upgrade the driver; it fails at
initialisation, and the kernel log names both versions:

```
npu lib version mismatch, vipuser.so=0x00020003, vipcore.ko=0x00010d00
```

Prefer whatever the board's own BSP ships. On a WalnutPi T527 image the
v1.13 libraries are installed under the vendor's own directory rather than
on the loader path, and PiNozCam finds them there without configuration.

## Check the chip and device

```bash
tr '\0' '\n' < /proc/device-tree/compatible | grep '^allwinner,'
ls -l /dev/vipcore
```

The first line should print `allwinner,a733` or `allwinner,t527`; the second
should show the device node. Both chips expose `/dev/vipcore`, so the SoC
name is what distinguishes them — and it has to, because each chip's model
file carries a hardware identifier that the other chip's driver rejects
while reading it.

Confirm the libraries are present, using the row for your chip above:

```bash
for lib in libVIPlite.so libVIPuser.so; do
    find /usr/lib /usr/local/lib -name "$lib" -print -quit
done
```

## Check device access

The OctoPrint service user needs read/write access to `/dev/vipcore`. Find
that user with:

```bash
ps -eo user,args | grep '[o]ctoprint.*serve'
```

Then test it, replacing the example user:

```bash
octoprint_user=pi
sudo -u "$octoprint_user" test -r /dev/vipcore \
    && sudo -u "$octoprint_user" test -w /dev/vipcore \
    && echo "NPU device access OK"
```

## Confirm PiNozCam selected the NPU

With **AI backend** left at `auto`, the plugin picks the NPU when the chip,
the device node and the matching libraries are all present, and otherwise
falls back to the CPU. The startup log names what it chose:

```
Inference backend ready: nozcam_daemon.awnn113.aarch64 + nozcam-t527.nb
```

`awnn113` is the T527 runner and `awnn` the A733 one. If the log names
`nozcam_daemon.aarch64.static` instead, the NPU was not selected and
detection is still running on the CPU — check the sections above, then set
**AI backend** to `awnn` to make the failure explicit rather than silent.

⚠️ OctoPrint's own log is the place to look, not the systemd journal: the
plugin logs to `~/.octoprint/logs/octoprint.log`, and `journalctl` for the
service shows nothing from it.

## Measured throughput

Camera-to-result, at the default settings:

| board | time per check | checks/min |
|---|---:|---:|
| Radxa A733 | 110 ms | 545 |
| WalnutPi T527 | 125 ms | 481 |

See [performance](performance.md) for how these compare with the CPU and
other accelerators.
