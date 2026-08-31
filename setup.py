# coding=utf-8

import glob
import os
import struct
import sysconfig

from setuptools import setup


plugin_identifier = "pinozcam"
plugin_package = "octoprint_pinozcam"
plugin_name = "OctoPrint-PiNozCam"
plugin_version = "1.1.0rc16"
runtime_version = "1.1.0rc16"
plugin_description = (
    "AI print-failure detection that runs entirely on your printer's own "
    "board. Runs on Linux (ARM32, ARM64, x86-64) and Apple Silicon macOS."
)
plugin_author = "DrAlexLiu"
plugin_author_email = "liu1111w@uwindsor.ca"
plugin_url = "https://github.com/DrAlexLiu/OctoPrint-PiNozCam"
plugin_license = "AGPL-3.0-only"

# Native inference is supplied by a target-specific runtime dependency.
plugin_requires = [
    "pillow",
    "pyTelegramBotAPI",
    "websocket-client",
]

# Templates, static files and translations are included automatically. Update
# MANIFEST.in too if this list gains files, so source distributions include
# them as well.
plugin_additional_data = []

# Additional packages outside <plugin_package>.*.
plugin_additional_packages = []

# Packages below <plugin_package>.* that should not be installed.
plugin_ignored_packages = []

additional_setup_parameters = {
    "python_requires": ">=3.7,<4",
    "dependency_links": [],
    "classifiers": [
        "Development Status :: 4 - Beta",
        "Environment :: Web Environment",
        "Framework :: OctoPrint",
        "Intended Audience :: End Users/Desktop",
        "Operating System :: POSIX :: Linux",
        "Programming Language :: Python :: 3",
        "Programming Language :: C++",
        "Topic :: Printing",
    ],
}

# Build metadata selects one target runtime. Accelerator variants share the
# aarch64 platform tag and are distinguished by distribution/version name.
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
    "armhf", "aarch64", "aarch64-rknn3566", "aarch64-rknn3576",
    "aarch64-rknn3588", "aarch64-awnn", "aarch64-awnnt527",
    "aarch64-vulkan", "x86_64",
    "x86_64-vulkan",
    "macos_arm64",
}
# Map platform tags to CPU ABI; accelerator type is detected separately.
_HOST_CPU_ARCH = {"linux_armv7l": "armhf", "linux_aarch64": "aarch64",
                  "linux_x86_64": "x86_64"}
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
    import sys

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
        import sys

        print("This host (%s) has no shipped daemon binary, so a wheel "
              "built here would install but never infer." % _host)
        print("Set the target architecture explicitly:")
        for _arch in sorted(_RUNNABLE_ARCHES):
            print("    PINOZCAM_ARCH=%-14s -> %s" % (_arch, _ARCH_PLAT[_arch]))
        print("or PINOZCAM_ALLOW_HOST_WHEEL=1 for a UI-only local "
              "development wheel.")
        sys.exit(-1)


# Keep build-time hardware probes independent of runtime dependencies.
def _detect_rockchip_chip():
    """Return the Rockchip SoC name from the device tree, if present."""
    try:
        with open("/proc/device-tree/compatible", "rb") as handle:
            compatible = handle.read()
    except (IOError, OSError):
        return None
    for entry in compatible.split(b"\x00"):
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
    try:
        with open("/proc/device-tree/compatible", "rb") as handle:
            entries = handle.read().split(b"\x00")
    except (IOError, OSError):
        return None
    for entry in entries:
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
    try:
        with open("/proc/device-tree/compatible", "rb") as handle:
            entries = handle.read().split(b"\x00")
    except (IOError, OSError):
        return None
    for entry in entries:
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
    try:
        with open("/proc/device-tree/compatible", "rb") as handle:
            entries = handle.read().split(b"\x00")
    except (IOError, OSError):
        return False
    return any(entry.startswith(b"nvidia,tegra") for entry in entries)


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

try:
    import octoprint_setuptools
except ImportError:
    print(
        "Could not import OctoPrint's setuptools. Are you running under "
        "the same python installation that OctoPrint is installed under?"
    )
    import sys

    sys.exit(-1)

setup_parameters = octoprint_setuptools.create_plugin_setup_parameters(
    identifier=plugin_identifier,
    package=plugin_package,
    name=plugin_name,
    version=plugin_version,
    description=plugin_description,
    author=plugin_author,
    mail=plugin_author_email,
    url=plugin_url,
    license=plugin_license,
    requires=plugin_requires,
    additional_packages=plugin_additional_packages,
    ignored_packages=plugin_ignored_packages,
    additional_data=plugin_additional_data,
)

if len(additional_setup_parameters):
    from octoprint.util import dict_merge

    setup_parameters = dict_merge(setup_parameters, additional_setup_parameters)

# OctoPrint already supplies an explicit package_data list for static files,
# templates and translations. Disable setuptools' second, implicit discovery
# pass so those data directories are not misclassified as namespace packages.
setup_parameters["include_package_data"] = False

if _WHEEL_CMDCLASS:
    setup_parameters.setdefault("cmdclass", {}).update(_WHEEL_CMDCLASS)

# Binary-distribution notices must be inside the Wheel, not merely beside it
# in the source archive. Keep one canonical copy in THIRD_PARTY_LICENSES and
# install it under the environment's share/doc directory; duplicating the
# files inside the Python package would create two copies that can drift.
# Keep source paths relative. setuptools rejects absolute paths when the
# caller places egg-info/build outside the checkout (which our Wheel test and
# reproducible release build deliberately do).
_NOTICE_FILES = sorted(glob.glob(os.path.join(
    "THIRD_PARTY_LICENSES", "*")))
setup_parameters["data_files"] = [
    ("share/doc/OctoPrint-PiNozCam/THIRD_PARTY_LICENSES", _NOTICE_FILES)
]

# Metadata describing which external runtime setup.py requests. Accelerator
# runtime Wheels contain their CPU fallback; the plugin Wheel contains neither.
_TARGET_CONTENT = {
    "armhf": {
        "cpu_arch": "armhf", "rknn_chip": None,
        "awnn": False, "vulkan": False,
    },
    "aarch64": {
        "cpu_arch": "aarch64", "rknn_chip": None,
        "awnn": False, "vulkan": False,
    },
    "aarch64-rknn3566": {
        "cpu_arch": "aarch64", "rknn_chip": "rk3566",
        "awnn": False, "vulkan": False,
    },
    "aarch64-rknn3576": {
        "cpu_arch": "aarch64", "rknn_chip": "rk3576",
        "awnn": False, "vulkan": False,
    },
    "aarch64-rknn3588": {
        "cpu_arch": "aarch64", "rknn_chip": "rk3588",
        "awnn": False, "vulkan": False,
    },
    "aarch64-awnn": {
        "cpu_arch": "aarch64", "rknn_chip": None,
        "awnn": True, "vulkan": False,
    },
    "aarch64-bpu-x5": {
        "cpu_arch": "aarch64", "rknn_chip": None,
        "awnn": False, "bpu_chip": "x5", "vulkan": False,
    },
    # Both Allwinner targets set "awnn"; "awnn_chip" separates them because
    # their NBG files carry incompatible hardware target IDs and their
    # daemons link different VIPLite library names.
    "aarch64-awnnt527": {
        "cpu_arch": "aarch64", "rknn_chip": None,
        "awnn": True, "awnn_chip": "t527", "vulkan": False,
    },
    "aarch64-acl": {
        "cpu_arch": "aarch64", "rknn_chip": None,
        "awnn": False, "acl": True, "vulkan": False,
    },
    "aarch64-vulkan": {
        "cpu_arch": "aarch64", "rknn_chip": None,
        "awnn": False, "vulkan": True,
    },
    "macos_arm64": {
        "cpu_arch": "macos_arm64", "rknn_chip": None,
        "awnn": False, "vulkan": False,
    },
    "x86_64": {
        "cpu_arch": "x86_64", "rknn_chip": None,
        "awnn": False, "vulkan": False,
    },
    "x86_64-vulkan": {
        "cpu_arch": "x86_64", "rknn_chip": None,
        "awnn": False, "vulkan": True,
    },
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
    _content = _TARGET_CONTENT[_TARGET_ARCH]
elif _host_cpu_arch == "aarch64":
    _chip = _detect_rockchip_chip()
    _rknn_target = ("aarch64-rknn%s" % (
        _chip[2:] if _chip.startswith("rk") else _chip)
        if _chip else None)
    if (_rknn_target in _TARGET_CONTENT and
            _rknn_runtime_present()):
        _content = _TARGET_CONTENT[_rknn_target]
    elif _awnn_t527_runtime_present():
        _content = _TARGET_CONTENT["aarch64-awnnt527"]
    elif (_detect_drobotics_chip() in _SUPPORTED_BPU_CHIPS
          and _bpu_runtime_present()):
        _content = _TARGET_CONTENT["aarch64-bpu-x5"]
    elif _awnn_runtime_present():
        _content = _TARGET_CONTENT["aarch64-awnn"]
    elif _acl_runtime_present():
        _content = _TARGET_CONTENT["aarch64-acl"]
    elif _detect_nvidia_tegra() and _vulkan_runtime_present():
        _content = _TARGET_CONTENT["aarch64-vulkan"]
    else:
        _content = _TARGET_CONTENT["aarch64"]
elif (_host_cpu_arch == "x86_64" and _detect_x86_vulkan_gpu()
      and _vulkan_runtime_present()):
    _content = _TARGET_CONTENT["x86_64-vulkan"]
else:
    # An unrecognised host falls back to "ship nothing platform-specific"
    # rather than guessing.
    _content = _TARGET_CONTENT.get(_host_cpu_arch,
                                   {"cpu_arch": None, "rknn_chip": None,
                                    "awnn": False, "vulkan": False})

# The GitHub source archive contains only the plugin. The outer pip process
# installs one immutable target Wheel from the matching GitHub Release. This is
# a normal PEP 508 dependency, not a nested installer or custom downloader.
_RUNTIME_REQUIREMENTS = {
    "armhf": "pinozcam-runtime",
    "aarch64": "pinozcam-runtime",
    "x86_64": "pinozcam-runtime",
    "macos_arm64": "pinozcam-runtime",
    "rknn3566": "pinozcam-runtime-rknn3566",
    "rknn3576": "pinozcam-runtime-rknn3576",
    "rknn3588": "pinozcam-runtime-rknn3588",
    "awnn": "pinozcam-runtime-a733",
    "bpu_x5": "pinozcam-runtime-rdkx5",
    "awnnt527": "pinozcam-runtime-t527",
    "vulkan": "pinozcam-runtime-gpu",
    "vulkan_x86_64": "pinozcam-runtime-gpu",
}
_RUNTIME_WHEEL_NAMES = {
    "armhf": "pinozcam_runtime-%s-py3-none-manylinux2014_armv7l.whl",
    "aarch64": "pinozcam_runtime-%s-py3-none-manylinux2014_aarch64.whl",
    "x86_64": "pinozcam_runtime-%s-py3-none-manylinux2014_x86_64.whl",
    "macos_arm64": (
        "pinozcam_runtime-%s-py3-none-macosx_14_0_arm64.whl"),
    "rknn3566": (
        "pinozcam_runtime_rknn3566-%s-py3-none-linux_aarch64.whl"),
    "rknn3576": (
        "pinozcam_runtime_rknn3576-%s-py3-none-linux_aarch64.whl"),
    "rknn3588": (
        "pinozcam_runtime_rknn3588-%s-py3-none-linux_aarch64.whl"),
    "awnn": "pinozcam_runtime_a733-%s-py3-none-linux_aarch64.whl",
    "bpu_x5": "pinozcam_runtime_rdkx5-%s-py3-none-linux_aarch64.whl",
    "awnnt527": "pinozcam_runtime_t527-%s-py3-none-linux_aarch64.whl",
    "vulkan": (
        "pinozcam_runtime_gpu-%s-py3-none-manylinux_2_35_aarch64.whl"),
    "vulkan_x86_64": (
        "pinozcam_runtime_gpu-%s-py3-none-manylinux_2_35_x86_64.whl"),
}
_RUNTIME_RELEASE_BASE = (
    "https://github.com/DrAlexLiu/OctoPrint-PiNozCam/releases/download")

if _content["rknn_chip"]:
    _chip = _content["rknn_chip"]
    _runtime_target = "rknn%s" % (
        _chip[2:] if _chip.startswith("rk") else _chip)
elif _content.get("bpu_chip"):
    _runtime_target = "bpu_%s" % _content["bpu_chip"]
elif _content["awnn"]:
    _runtime_target = ("awnn%s" % _content["awnn_chip"]
                       if _content.get("awnn_chip") else "awnn")
elif _content.get("acl"):
    # No entry in _RUNTIME_REQUIREMENTS yet: the Ascend runtime Wheel is
    # not published, so it is installed from a local file instead of being
    # pulled in as a PEP 508 dependency. Naming the target here still stops
    # this board from being mistaken for a plain aarch64 CPU one and
    # dragging in the wrong runtime.
    _runtime_target = "acl"
elif _content["vulkan"]:
    _runtime_target = (
        "vulkan_x86_64"
        if _content["cpu_arch"] == "x86_64" else "vulkan"
    )
else:
    _runtime_target = _content["cpu_arch"]

if _runtime_target in _RUNTIME_REQUIREMENTS:
    _runtime_dist = _RUNTIME_REQUIREMENTS[_runtime_target]
    _runtime_filename = (
        _RUNTIME_WHEEL_NAMES[_runtime_target] % runtime_version)
    _runtime_url = "%s/%s/%s" % (
        _RUNTIME_RELEASE_BASE, runtime_version, _runtime_filename)
    _runtime_requirement = "%s @ %s" % (_runtime_dist, _runtime_url)
    setup_parameters.setdefault("install_requires", []).append(
        _runtime_requirement)
    print("PiNozCam runtime target: %s" % _runtime_target)
    print("PiNozCam runtime dependency: %s" % _runtime_requirement)

# exclude_package_data is a second mechanical guard. The files have been
# removed from Git, but a developer may stage local artifacts under static/
# while testing; they still must never leak into the plugin Wheel.
_exclude = setup_parameters.setdefault(
    "exclude_package_data", {}).setdefault(plugin_package, [])
# Runtime payloads belong exclusively to external runtime Wheels. Exclude
# every known native file even while the tracked copies remain in this branch
# for the migration test; once validation passes they are removed from Git as
# well, which is what makes GitHub's automatically generated tag ZIP small.
_exclude.extend([
    "static/bin/*",
    "static/models/*",
])

setup(**setup_parameters)
