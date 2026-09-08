import glob
import os
import struct
import sys
import sysconfig

from setuptools import setup


plugin_version = "1.1.0"
runtime_version = "1.1.0"
plugin_license = "AGPL-3.0-only"
plugin_url = "https://github.com/DrAlexLiu/OctoPrint-PiNozCam"

# Native inference is supplied by a target-specific runtime dependency.
plugin_requires = [
    "pillow",
    "pyTelegramBotAPI",
    "websocket-client",
]

# Build metadata selects one target runtime. Accelerator variants share the
# platform tag of their CPU architecture and are distinguished by
# distribution/version name.
_ARCH_PLAT = {
    "armhf": "linux_armv7l",
    "aarch64": "linux_aarch64",
    "aarch64-rknn3566": "linux_aarch64",
    "aarch64-rknn3576": "linux_aarch64",
    "aarch64-rknn3588": "linux_aarch64",
    "aarch64-awnn": "linux_aarch64",
    "aarch64-bpu-x5": "linux_aarch64",
    "aarch64-awnnt527": "linux_aarch64",
    "aarch64-vulkan": "linux_aarch64",
    "x86_64": "linux_x86_64",
    "x86_64-vulkan": "linux_x86_64",
    "macos_arm64": "macosx_14_0_arm64",
}
# Targets with a published native runtime.
_RUNNABLE_ARCHES = {
    "armhf",
    "aarch64",
    "aarch64-rknn3566",
    "aarch64-rknn3576",
    "aarch64-rknn3588",
    "aarch64-awnn",
    "aarch64-bpu-x5",
    "aarch64-awnnt527",
    "aarch64-vulkan",
    "x86_64",
    "x86_64-vulkan",
    "macos_arm64",
}
# Map platform tags to CPU ABI; accelerator type is detected separately.
_HOST_CPU_ARCH = {
    "linux_armv7l": "armhf",
    "linux_aarch64": "aarch64",
    "linux_x86_64": "x86_64",
}
_X86_VULKAN_GPU_VENDORS = frozenset(("0x1002", "0x10de", "0x8086"))

_host = sysconfig.get_platform().replace("-", "_").replace(".", "_")
_host_cpu_arch = _HOST_CPU_ARCH.get(_host)
# macOS reports macosx_<major>_<minor>_arm64, which changes with every
# OS release, so it cannot be a key in _HOST_CPU_ARCH. uname is stable.
if _host_cpu_arch is None and os.uname().sysname == "Darwin" \
        and os.uname().machine.lower() in ("arm64", "aarch64"):
    _host_cpu_arch = "macos_arm64"
# A 32-bit Raspberry Pi userspace may report an aarch64 kernel platform.
# Interpreter pointer size decides which daemon can actually execute.
_HOST_PLAT_OVERRIDE = None
if struct.calcsize("P") * 8 == 32 and _host_cpu_arch == "aarch64":
    _host_cpu_arch = "armhf"
    _HOST_PLAT_OVERRIDE = _ARCH_PLAT["armhf"]

_TARGET_ARCH = os.environ.get("PINOZCAM_ARCH", "").strip()
if _TARGET_ARCH and _TARGET_ARCH not in _ARCH_PLAT:
    print("PINOZCAM_ARCH must be one of: %s" % ", ".join(sorted(_ARCH_PLAT)))
    sys.exit(-1)
_TARGET_PLAT = _ARCH_PLAT.get(_TARGET_ARCH)

# Fail closed when an ambient build has no published runtime target.
if not _TARGET_ARCH and not os.environ.get("PINOZCAM_ALLOW_HOST_WHEEL"):
    _runnable_plats = set(_ARCH_PLAT[_a] for _a in _RUNNABLE_ARCHES)
    # Compare the RESOLVED architecture, not the raw platform string. On
    # Linux the two are the same ("linux_aarch64" either way), but macOS
    # reports the running OS version (macosx_26_0_arm64) while the Wheel tag
    # is pinned at the minimum supported one (macosx_14_0_arm64), so a
    # literal comparison rejects every Mac newer than 14.0 -- including the
    # ones that can run the daemon perfectly well.
    _host_plat_for_check = _ARCH_PLAT.get(_host_cpu_arch, _host)
    if _host_plat_for_check not in _runnable_plats:
        print("This host (%s) has no shipped daemon binary, so a wheel "
              "built here would install but never infer." % _host)
        print("Set the target architecture explicitly:")
        for _arch in sorted(_RUNNABLE_ARCHES):
            print("    PINOZCAM_ARCH=%-14s -> %s" % (_arch, _ARCH_PLAT[_arch]))
        print("or PINOZCAM_ALLOW_HOST_WHEEL=1 for a UI-only local "
              "development wheel.")
        sys.exit(-1)


# Keep build-time hardware probes independent of runtime dependencies.
def _device_tree_compatible():
    """Return the device tree's compatible entries, empty when there is none."""
    try:
        with open("/proc/device-tree/compatible", "rb") as handle:
            return handle.read().split(b"\x00")
    except (IOError, OSError):
        return []


def _detect_rockchip_chip():
    """Return the Rockchip SoC name from the device tree, if present."""
    for entry in _device_tree_compatible():
        if not entry.startswith(b"rockchip,"):
            continue
        candidate = entry[len(b"rockchip,"):]
        if b"-" not in candidate:
            return candidate.decode("ascii", "replace")
    return None


def _rknn_runtime_present():
    """Return whether a supported Rockchip userspace runtime is installed."""
    return any(os.path.exists(p) for p in (
        "/usr/lib/librknnrt.so", "/usr/lib/aarch64-linux-gnu/librknnrt.so"))


def _awnn_runtime_present():
    """Return whether the Allwinner NPU device and runtime are available."""
    if not os.path.exists("/dev/vipcore"):
        return False
    return any(os.path.exists(p) for p in (
        "/usr/local/lib/libNBGlinker.so", "/usr/lib/libNBGlinker.so"))


def _acl_runtime_present():
    """Return whether the Ascend device and CANN userspace are available.

    Mirrors nozcam_backend._acl_runtime_present. Duplicated rather than
    imported because that module needs Pillow, which may not be installed
    yet at this point of a fresh install.
    """
    if not os.path.exists("/dev/davinci0"):
        return False
    for root in ("/usr/local/Ascend/ascend-toolkit/latest",
                 os.path.expanduser("~/Ascend/ascend-toolkit/latest")):
        for sub in ("lib64", "aarch64-linux/lib64"):
            if os.path.exists(os.path.join(root, sub, "libascendcl.so")):
                return True
    return False


# Duplicated from nozcam_backend rather than imported: that module does
# "from PIL import Image", which need not be installable yet at this point in
# a fresh install. The two copies must stay in step.
_AWNN_V113_LIB_DIRS = (
    "/usr/local/lib",
    "/usr/lib",
    "/usr/lib/walnutpi/walnutpi.npu/walnutpi_npu/_awnn_lib/t527/lib",
)


def _detect_allwinner_vip_chip():
    """Return the Allwinner SoC name from the device tree, or None."""
    for entry in _device_tree_compatible():
        if entry.startswith(b"allwinner,"):
            return entry[len(b"allwinner,"):].decode("ascii", "replace")
    return None


def _awnn_t527_runtime_present():
    """Return whether this is a T527 carrying the VIPLite v1.13 runtime.

    Tested before the A733 check because it is the more specific claim: both
    chips expose /dev/vipcore, so the SoC name is what separates them.
    """
    if not os.path.exists("/dev/vipcore"):
        return False
    if _detect_allwinner_vip_chip() != "t527":
        return False
    return any(os.path.exists(os.path.join(directory, "libVIPlite.so"))
               for directory in _AWNN_V113_LIB_DIRS)


# Duplicated from nozcam_backend rather than imported: that module does
# "from PIL import Image", which need not be installable yet during a fresh
# install. The two copies must stay in step.
_BPU_LIB_PATHS = (
    "/usr/lib/libdnn.so",
    "/usr/lib/aarch64-linux-gnu/libdnn.so",
    "/usr/local/lib/libdnn.so",
)

_SUPPORTED_BPU_CHIPS = ("x5",)


def _detect_drobotics_chip():
    """Return the D-Robotics SoC name from the device tree, or None."""
    for entry in _device_tree_compatible():
        text = entry.decode("ascii", "replace").strip()
        if "," not in text:
            continue
        vendor, _, chip = text.partition(",")
        if vendor.strip().lower() == "d-robotics":
            return chip.strip().lower()
    return None


def _bpu_runtime_present():
    """Return whether the D-Robotics BPU runtime and device are installed.

    The X5 exposes no dedicated node -- the BPU is reached through libdnn
    over /dev/ion -- so both the device and the library are required.
    """
    if not os.path.exists("/dev/ion"):
        return False
    return any(os.path.exists(path) for path in _BPU_LIB_PATHS)


def _detect_nvidia_tegra():
    """Return whether the device tree identifies an NVIDIA Tegra SoC."""
    return any(entry.startswith(b"nvidia,tegra")
               for entry in _device_tree_compatible())


def _vulkan_runtime_present():
    """Return whether this 64-bit ARM or x86 host has a Vulkan loader."""
    if struct.calcsize("P") * 8 != 64:
        return False
    machine = os.uname().machine.lower()
    multiarch = {
        "aarch64": "aarch64-linux-gnu",
        "arm64": "aarch64-linux-gnu",
        "x86_64": "x86_64-linux-gnu",
        "amd64": "x86_64-linux-gnu",
    }.get(machine)
    if multiarch is None:
        return False
    return any(os.path.exists(p) for p in (
        "/lib/%s/libvulkan.so.1" % multiarch,
        "/usr/lib/%s/libvulkan.so.1" % multiarch,
        "/usr/local/lib/libvulkan.so.1",
    ))


def _detect_x86_vulkan_gpu():
    """Return whether x86 PCI data shows a supported display GPU vendor."""
    if _host_cpu_arch != "x86_64":
        return False
    for device in glob.glob("/sys/bus/pci/devices/*"):
        try:
            with open(os.path.join(device, "class"), "r") as handle:
                pci_class = handle.read().strip().lower()
            with open(os.path.join(device, "vendor"), "r") as handle:
                vendor = handle.read().strip().lower()
        except (IOError, OSError):
            continue
        if (pci_class.startswith("0x03")
                and vendor in _X86_VULKAN_GPU_VENDORS):
            return True
    return False


# PEP 440 local versions distinguish accelerator builds sharing one ABI tag.
_VERSION_SUFFIX = {
    "aarch64-rknn3566": "+rknn3566",
    "aarch64-rknn3576": "+rknn3576",
    "aarch64-rknn3588": "+rknn3588",
    "aarch64-awnn": "+awnn",
    "aarch64-bpu-x5": "+bpux5",
    "aarch64-awnnt527": "+awnnt527",
    "aarch64-vulkan": "+vulkan",
    "x86_64-vulkan": "+vulkan",
}
if _TARGET_ARCH in _VERSION_SUFFIX:
    plugin_version = plugin_version + _VERSION_SUFFIX[_TARGET_ARCH]

try:
    from setuptools.command.bdist_wheel import bdist_wheel as _bdist_wheel
except ImportError:
    try:
        from wheel.bdist_wheel import bdist_wheel as _bdist_wheel
    except ImportError:
        _bdist_wheel = None

if _bdist_wheel is not None:
    class bdist_wheel(_bdist_wheel):
        """A wheel tagged for a platform, never `any`."""

        def finalize_options(self):
            """Mark the wheel platform-specific after normal setup."""
            _bdist_wheel.finalize_options(self)
            self.root_is_pure = False

        def get_tag(self):
            """Return a Python-agnostic tag for the selected Linux target."""
            # Python code is ABI-neutral; only the native runtime is platformed.
            _python, _abi, plat = _bdist_wheel.get_tag(self)
            return "py3", "none", (
                _TARGET_PLAT or _HOST_PLAT_OVERRIDE or plat)
    _WHEEL_CMDCLASS = {"bdist_wheel": bdist_wheel}
else:
    # A source installation does not require Wheel build support.
    _WHEEL_CMDCLASS = {}

setup_parameters = {
    "license": plugin_license,
    "version": plugin_version,
    "install_requires": list(plugin_requires),
    "url": plugin_url,
    "project_urls": {"Homepage": plugin_url},
}

if _WHEEL_CMDCLASS:
    setup_parameters.setdefault("cmdclass", {}).update(_WHEEL_CMDCLASS)

# Metadata describing which external runtime setup.py requests.
_ARCH_RUNTIME = {
    "armhf": "armhf",
    "aarch64": "aarch64",
    "aarch64-rknn3566": "rknn3566",
    "aarch64-rknn3576": "rknn3576",
    "aarch64-rknn3588": "rknn3588",
    "aarch64-awnn": "awnn",
    "aarch64-awnnt527": "awnnt527",
    "aarch64-bpu-x5": "bpu_x5",
    "aarch64-acl": "acl",
    "aarch64-vulkan": "vulkan",
    "macos_arm64": "macos_arm64",
    "x86_64": "x86_64",
    "x86_64-vulkan": "vulkan_x86_64",
}
#  This runs for the AMBIENT case too: it is the real distribution path.
# OctoPrint's Plugin Manager runs `pip install <source archive>` directly on
# the target board, so setup.py can name the one runtime dependency that board
# needs instead of downloading every backend.
#
# PINOZCAM_ARCH stays meaningful on top of this for the one case
# detection cannot cover: pre-building a wheel on a DIFFERENT machine than
# the one it will be installed on (e.g. a CI box with no NPU hardware of
# its own, producing an aarch64-rknn3588 wheel for later distribution).
if _TARGET_ARCH:
    _target_arch = _TARGET_ARCH
elif _host_cpu_arch == "aarch64":
    _chip = _detect_rockchip_chip()
    _rknn_arch = (
        "aarch64-rknn%s" % (_chip[2:] if _chip.startswith("rk") else _chip)
        if _chip
        else None
    )
    if _rknn_arch in _ARCH_RUNTIME and _rknn_runtime_present():
        _target_arch = _rknn_arch
    elif _awnn_t527_runtime_present():
        _target_arch = "aarch64-awnnt527"
    elif _detect_drobotics_chip() in _SUPPORTED_BPU_CHIPS and _bpu_runtime_present():
        _target_arch = "aarch64-bpu-x5"
    elif _awnn_runtime_present():
        _target_arch = "aarch64-awnn"
    elif _acl_runtime_present():
        _target_arch = "aarch64-acl"
    elif _detect_nvidia_tegra() and _vulkan_runtime_present():
        _target_arch = "aarch64-vulkan"
    else:
        _target_arch = "aarch64"
elif (
    _host_cpu_arch == "x86_64"
    and _detect_x86_vulkan_gpu()
    and _vulkan_runtime_present()
):
    _target_arch = "x86_64-vulkan"
else:
    # An unrecognised host falls back to "ship nothing platform-specific"
    # rather than guessing.
    _target_arch = _host_cpu_arch

# The GitHub source archive contains only the plugin. The outer pip process
# installs one immutable target Wheel, pinned to this exact version. The
# CPU and GPU runtimes are published to PyPI (see _PYPI_PUBLISHED_DISTS) and
# resolve through the normal index; every other target has no PyPI project
# and keeps depending on the matching GitHub Release asset by direct PEP 508
# URL, not a nested installer or custom downloader.
_RUNTIME_REQUIREMENTS = {
    "armhf": (
        "pinozcam-runtime",
        "pinozcam_runtime-%s-py3-none-manylinux2014_armv7l.whl",
    ),
    "aarch64": (
        "pinozcam-runtime",
        "pinozcam_runtime-%s-py3-none-manylinux2014_aarch64.whl",
    ),
    "x86_64": (
        "pinozcam-runtime",
        "pinozcam_runtime-%s-py3-none-manylinux2014_x86_64.whl",
    ),
    "macos_arm64": (
        "pinozcam-runtime",
        "pinozcam_runtime-%s-py3-none-macosx_14_0_arm64.whl",
    ),
    "rknn3566": (
        "pinozcam-runtime-rknn3566",
        "pinozcam_runtime_rknn3566-%s-py3-none-linux_aarch64.whl",
    ),
    "rknn3576": (
        "pinozcam-runtime-rknn3576",
        "pinozcam_runtime_rknn3576-%s-py3-none-linux_aarch64.whl",
    ),
    "rknn3588": (
        "pinozcam-runtime-rknn3588",
        "pinozcam_runtime_rknn3588-%s-py3-none-linux_aarch64.whl",
    ),
    "awnn": (
        "pinozcam-runtime-a733",
        "pinozcam_runtime_a733-%s-py3-none-linux_aarch64.whl",
    ),
    "bpu_x5": (
        "pinozcam-runtime-rdkx5",
        "pinozcam_runtime_rdkx5-%s-py3-none-linux_aarch64.whl",
    ),
    "awnnt527": (
        "pinozcam-runtime-t527",
        "pinozcam_runtime_t527-%s-py3-none-linux_aarch64.whl",
    ),
    "vulkan": (
        "pinozcam-runtime-gpu",
        "pinozcam_runtime_gpu-%s-py3-none-manylinux_2_35_aarch64.whl",
    ),
    "vulkan_x86_64": (
        "pinozcam-runtime-gpu",
        "pinozcam_runtime_gpu-%s-py3-none-manylinux_2_35_x86_64.whl",
    ),
}
_RUNTIME_RELEASE_BASE = (
    "https://github.com/DrAlexLiu/OctoPrint-PiNozCam/releases/download")
# Dists actually published to PyPI (publish-pypi.yml, "cpu" and "gpu"
# targets only). Everything else -- the NPU-specific runtimes -- has no
# PyPI project and must keep resolving from the GitHub Release asset.
_PYPI_PUBLISHED_DISTS = frozenset(("pinozcam-runtime", "pinozcam-runtime-gpu"))

_runtime_target = _ARCH_RUNTIME.get(_target_arch)

if _runtime_target in _RUNTIME_REQUIREMENTS:
    _runtime_dist, _runtime_wheel = _RUNTIME_REQUIREMENTS[_runtime_target]
    if _runtime_dist in _PYPI_PUBLISHED_DISTS:
        _runtime_requirement = "%s==%s" % (_runtime_dist, runtime_version)
    else:
        _runtime_url = "%s/%s/%s" % (
            _RUNTIME_RELEASE_BASE, runtime_version,
            _runtime_wheel % runtime_version)
        _runtime_requirement = "%s @ %s" % (_runtime_dist, _runtime_url)
    setup_parameters.setdefault("install_requires", []).append(
        _runtime_requirement)
    print("PiNozCam runtime target: %s" % _runtime_target)
    print("PiNozCam runtime dependency: %s" % _runtime_requirement)

setup(**setup_parameters)
