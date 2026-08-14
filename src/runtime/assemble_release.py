#!/usr/bin/env python3
"""Assemble and verify every runtime Wheel for one release."""

import argparse
import hashlib
import json
import os
import shutil
import zipfile


TARGETS = {
    "armhf": ("pinozcam_runtime", "pinozcam-runtime",
              "pinozcam_runner", "manylinux2014_armv7l"),
    "aarch64": ("pinozcam_runtime", "pinozcam-runtime",
                "pinozcam_runner", "manylinux2014_aarch64"),
    "x86_64": ("pinozcam_runtime", "pinozcam-runtime",
               "pinozcam_runner", "manylinux2014_x86_64"),
    "macos_arm64": ("pinozcam_runtime", "pinozcam-runtime",
                    "pinozcam_runner", "macosx_14_0_arm64"),
    "vulkan": ("pinozcam_runtime_gpu", "pinozcam-runtime-gpu",
               "pinozcam_runner_gpu", "manylinux_2_35_aarch64"),
    "vulkan_x86_64": (
        "pinozcam_runtime_gpu", "pinozcam-runtime-gpu",
        "pinozcam_runner_gpu", "manylinux_2_35_x86_64"),
    "rknn3566": (
        "pinozcam_runtime_rknn3566", "pinozcam-runtime-rknn3566",
        "pinozcam_runtime_rknn3566", "linux_aarch64"),
    "rknn3576": (
        "pinozcam_runtime_rknn3576", "pinozcam-runtime-rknn3576",
        "pinozcam_runtime_rknn3576", "linux_aarch64"),
    "rknn3588": (
        "pinozcam_runtime_rknn3588", "pinozcam-runtime-rknn3588",
        "pinozcam_runtime_rknn3588", "linux_aarch64"),
    "awnn": ("pinozcam_runtime_a733", "pinozcam-runtime-a733",
             "pinozcam_runtime_a733", "linux_aarch64"),
}


def _sha256(path):
    """Return the lowercase SHA-256 digest of one file."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _one(names, suffix):
    """Return the sole archive member ending in ``suffix``."""
    matches = [name for name in names if name.endswith(suffix)]
    if len(matches) != 1:
        raise RuntimeError(
            "expected one %s member, found %d" % (suffix, len(matches)))
    return matches[0]


def _expected(version):
    """Return expected filenames keyed by runtime target."""
    return {
        target: "%s-%s-py3-none-%s.whl" % (stem, version, platform)
        for target, (stem, _dist, _module, platform) in TARGETS.items()
    }


def _discover(root):
    """Return unique Wheel paths keyed by basename below ``root``."""
    wheels = {}
    for directory, _subdirs, filenames in os.walk(root):
        for filename in filenames:
            if not filename.endswith(".whl"):
                continue
            path = os.path.join(directory, filename)
            if filename in wheels:
                if _sha256(path) != _sha256(wheels[filename]):
                    raise RuntimeError(
                        "duplicate Wheel basename has different bytes: %s" %
                        filename)
                continue
            wheels[filename] = path
    return wheels


def _verify(path, target, version, revision):
    """Verify release identity, manifest, source revision, and platform tag."""
    stem, distribution, module, platform = TARGETS[target]
    expected_name = "%s-%s-py3-none-%s.whl" % (stem, version, platform)
    if os.path.basename(path) != expected_name:
        raise RuntimeError("unexpected Wheel filename: %s" % path)
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        manifest_name = _one(names, "%s/manifest.json" % module)
        manifest = json.loads(archive.read(manifest_name))
        expected_source = (
            "https://github.com/DrAlexLiu/OctoPrint-PiNozCam/commit/%s" %
            revision
        )
        checks = {
            "distribution": distribution,
            "module": module,
            "plugin_version": version,
            "source": expected_source,
            "target": target,
        }
        for key, expected_value in checks.items():
            if manifest.get(key) != expected_value:
                raise RuntimeError(
                    "%s manifest %s is %r, expected %r" % (
                        expected_name, key, manifest.get(key), expected_value))
        wheel_metadata = _one(names, ".dist-info/WHEEL")
        tag = "Tag: py3-none-%s" % platform
        if tag not in archive.read(wheel_metadata).decode("utf-8"):
            raise RuntimeError("%s is missing %s" % (expected_name, tag))


def main():
    """Validate, copy, and checksum a complete runtime release bundle."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()

    expected = _expected(args.version)
    discovered = _discover(os.path.abspath(args.artifact_root))
    missing = sorted(set(expected.values()) - set(discovered))
    unexpected = sorted(set(discovered) - set(expected.values()))
    if missing or unexpected:
        raise RuntimeError(
            "runtime Wheel set differs from the %d-file contract; "
            "missing=%r, unexpected=%r"
            % (len(TARGETS), missing, unexpected))

    output = os.path.abspath(args.output)
    os.makedirs(output, exist_ok=True)
    for target, filename in sorted(expected.items()):
        source = discovered[filename]
        _verify(source, target, args.version, args.revision)
        shutil.copy2(source, os.path.join(output, filename))

    checksum_path = os.path.join(output, "CHECKSUMS.txt")
    with open(checksum_path, "w", encoding="ascii", newline="\n") as handle:
        for filename in sorted(expected.values()):
            handle.write("%s  %s\n" % (
                _sha256(os.path.join(output, filename)), filename))
    print("PASS: assembled nine runtime Wheels for %s" % args.version)


if __name__ == "__main__":
    main()
