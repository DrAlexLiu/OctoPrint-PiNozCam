#!/usr/bin/env python3
"""Verify an accelerator runtime Wheel without installing it."""

import argparse
import hashlib
import json
import os
import posixpath
import sys
import zipfile

import package_accelerator_runtime as package_runtime


def _digest(data):
    """Return the lowercase SHA-256 digest of bytes."""
    return hashlib.sha256(data).hexdigest()


def _one(names, suffix):
    """Return the sole archive member ending in suffix."""
    matches = [name for name in names if name.endswith(suffix)]
    if len(matches) != 1:
        raise RuntimeError(
            "expected one %s member, found %d" % (suffix, len(matches))
        )
    return matches[0]


def main():
    """Validate platform tag, payload membership, hashes, and file modes."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=sorted(
        package_runtime.TARGETS), required=True)
    parser.add_argument("--wheel", required=True)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()

    spec = package_runtime.TARGETS[args.target]
    expected_suffix = "-py3-none-%s.whl" % spec["platform"]
    if not os.path.basename(args.wheel).endswith(expected_suffix):
        raise RuntimeError("Wheel has the wrong platform tag")

    with zipfile.ZipFile(args.wheel) as archive:
        names = archive.namelist()
        for name in names:
            if name.startswith("/") or ".." in name.split("/"):
                raise RuntimeError("unsafe Wheel member: %s" % name)

        module_suffix = "%s/manifest.json" % spec["module"]
        manifest_name = _one(names, module_suffix)
        if ".data/purelib/" in manifest_name:
            raise RuntimeError("native runtime payload is stored in purelib")
        module_root = posixpath.dirname(manifest_name)
        manifest = json.loads(archive.read(manifest_name))
        expected_source = (
            "https://github.com/DrAlexLiu/OctoPrint-PiNozCam/commit/%s"
            % args.revision
        )
        expected_manifest = {
            "distribution": spec["dist"],
            "format": 2,
            "module": spec["module"],
            "runner_protocol": 1,
            "source": expected_source,
            "target": args.target,
        }
        for key, expected in expected_manifest.items():
            if manifest.get(key) != expected:
                raise RuntimeError(
                    "manifest %s is %r, expected %r"
                    % (key, manifest.get(key), expected)
                )

        expected_models = {
            name: package_runtime.MODEL_METADATA[name]
            for name in spec["models"]
        }
        if manifest.get("models") != expected_models:
            raise RuntimeError("manifest model metadata is wrong")
        expected_payload = {
            "bin/%s" % name for name in spec["bins"]
        } | {
            "models/%s" % name for name in spec["models"]
        }
        recorded = manifest.get("files", {})
        actual_payload = {
            relative for relative in recorded
            if relative.startswith("bin/") or relative.startswith("models/")
        }
        if actual_payload != expected_payload:
            raise RuntimeError("manifest payload set is wrong")

        archive_payload = set()
        for name in names:
            if not name.startswith(module_root + "/"):
                continue
            relative = name[len(module_root) + 1:]
            if relative.startswith("bin/") or relative.startswith("models/"):
                archive_payload.add(relative)
        if archive_payload != expected_payload:
            raise RuntimeError("Wheel contains an unexpected runtime payload")

        for relative, metadata in recorded.items():
            member = "%s/%s" % (module_root, relative)
            try:
                info = archive.getinfo(member)
            except KeyError as exc:
                raise RuntimeError("missing recorded payload: %s" % member) \
                    from exc
            data = archive.read(info)
            if len(data) != metadata.get("bytes"):
                raise RuntimeError("byte count mismatch: %s" % relative)
            if _digest(data) != metadata.get("sha256"):
                raise RuntimeError("SHA-256 mismatch: %s" % relative)
            if relative.startswith("bin/"):
                mode = (info.external_attr >> 16) & 0o777
                if not mode & 0o111:
                    raise RuntimeError("runner is not executable: %s" % relative)

        wheel_metadata = _one(names, ".dist-info/WHEEL")
        tag = "Tag: py3-none-%s" % spec["platform"]
        if tag not in archive.read(wheel_metadata).decode("utf-8"):
            raise RuntimeError("Wheel metadata is missing %s" % tag)

    print(
        "PASS: %s contains %d runners and %d models"
        % (args.target, len(spec["bins"]), len(spec["models"]))
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("FAIL: %s" % exc, file=sys.stderr)
        raise
