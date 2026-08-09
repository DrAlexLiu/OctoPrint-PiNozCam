# Rockchip NPU setup

[← Back to README](../README.md)

PiNozCam supplies the chip-specific model and runner for RK3566, RK3576 and
RK3588. The board image must supply two system components:

- a kernel with the Rockchip RKNPU driver enabled and bound to the NPU; and
- a compatible `librknnrt.so` in `/usr/lib` or
  `/usr/lib/aarch64-linux-gnu`.

Rockchip describes the split between its userspace runtime and kernel driver in
the [RKNN Toolkit2 README](https://github.com/airockchip/rknn-toolkit2#readme).
Prefer the runtime supplied for the board's BSP. Replacing only the library
does not upgrade the kernel driver and can create a version mismatch.

## Check the driver and device

The driver may be built into the kernel or loaded as a module, so `lsmod` alone
is not a reliable test. These commands work for either case:

```bash
grep -ao 'rockchip,rk35[0-9]*' /proc/device-tree/compatible | head -1
test -d /sys/module/rknpu && echo "RKNPU driver present"

for node in /dev/rknpu /dev/dri/card* /dev/dri/renderD*; do
    [ -e "$node" ] || continue
    device_path=$(udevadm info --query=path --name="$node" 2>/dev/null || true)
    case "$node:$device_path" in
        /dev/rknpu:*|*:*npu*) ls -l "$node" ;;
    esac
done
```

The first line should identify `rk3566`, `rk3576` or `rk3588`; the second
should report the driver; the loop should print at least one NPU device. Kernel
and board-image versions differ: some expose `/dev/rknpu`, while DRM-based
drivers expose a `/dev/dri/cardN` and sometimes a matching `renderDN`. The
number is not fixed, so do not assume that every `renderD128` is the NPU.

The OctoPrint service user needs read/write access to the device that the NPU
driver exposes. Find that user with:

```bash
ps -eo user,args | grep '[o]ctoprint.*serve'
```

Then test the NPU device reported above, replacing the example values:

```bash
octoprint_user=pi
npu_device=/dev/dri/card1
sudo -u "$octoprint_user" test -r "$npu_device" \
    && sudo -u "$octoprint_user" test -w "$npu_device" \
    && echo "NPU device access OK"
```

If access fails and the device is owned by an ordinary hardware group such as
`video` or `render`, an administrator can grant that group once:

```bash
npu_group=$(stat -c %G "$npu_device")
getent group "$npu_group"
sudo usermod -aG "$npu_group" "$octoprint_user"
```

Restart the OctoPrint service after changing group membership. Do not add the
service user to `root` and do not make the device world-writable. If the node
is owned by `root:root`, install or repair the board vendor's RKNPU/udev
package instead.

The `render` group is **not** an unconditional RKNN requirement. It is needed
only when the actual NPU device is owned by that group. Several vendor kernels
use a DRM `cardN` owned by `video`; other images may use `render` or a dedicated
group. Grant the group shown by `ls -l` for the device identified by its
`udevadm` path.

## Check the userspace runtime

PiNozCam checks these two standard locations:

```bash
ls -l /usr/lib/librknnrt.so \
      /usr/lib/aarch64-linux-gnu/librknnrt.so 2>/dev/null
strings /usr/lib/librknnrt.so 2>/dev/null \
    | grep -m1 'librknnrt version'
```

At least one library path must exist. If the first path does not exist but the
second does, run `strings` against the second path instead. A newer library is
not automatically compatible with an older board kernel. After installation,
a successful **Speed Test** or an `Inference backend ready` log entry is the
authoritative end-to-end check.

Some board images omit `CONFIG_ROCKCHIP_RKNPU_DEBUG_FS` and
`CONFIG_ROCKCHIP_RKNPU_PROC_FS`. That removes optional load files such as
`/sys/kernel/debug/rknpu/load`, but it does **not** disable inference;
Rockchip's
[RKNPU Kconfig](https://github.com/rockchip-linux/kernel/blob/develop-5.10/drivers/rknpu/Kconfig)
describes those options as debugging interfaces.

PiNozCam cannot install a kernel driver, replace a vendor runtime, change udev
rules or add the OctoPrint account to system groups. Those are deliberate
administrator-only operations. If the RKNN runtime is absent, **Auto-detect**
does not select the NPU. If an automatically selected Rockchip, A733 or Vulkan
accelerator cannot start or complete warm-up, PiNozCam uses the CPU fallback
bundled in the same runtime package for that detector session. A backend that
the user explicitly forces remains fail-closed and reports the error instead
of silently choosing different hardware.

Whenever the CPU backend runs on a Rockchip or A733 runtime package, it follows
the same policy as every other CPU backend: Linux `cpu_capacity` (with maximum
frequency as the fallback signal) identifies the performance-core pool, then
**CPU Cores Used** selects a percentage of that pool. It does not blindly use
every core. When an NPU is active, the setting limits only the runner's
CPU-side work and does not limit the NPU's own cores.
