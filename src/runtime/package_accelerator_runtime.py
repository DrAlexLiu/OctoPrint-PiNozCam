#!/usr/bin/env python3
"""Build one accelerator runtime Wheel from already verified artifacts."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

TARGETS = {
    "rknn3566": {
        "dist": "pinozcam-runtime-rknn3566",
        "module": "pinozcam_runtime_rknn3566",
        "platform": "linux_aarch64",
        "bins": (
            "nozcam_daemon.aarch64.static",
            "nozcam_daemon.rknn.aarch64",
        ),
        "models": ("nozcam-cpu.pte", "nozcam-rk3566.rknn"),
    },
    "rknn3576": {
        "dist": "pinozcam-runtime-rknn3576",
        "module": "pinozcam_runtime_rknn3576",
        "platform": "linux_aarch64",
        "bins": (
            "nozcam_daemon.aarch64.static",
            "nozcam_daemon.rknn.aarch64",
        ),
        "models": ("nozcam-cpu.pte", "nozcam-rk3576.rknn"),
    },
    "rknn3588": {
        "dist": "pinozcam-runtime-rknn3588",
        "module": "pinozcam_runtime_rknn3588",
        "platform": "linux_aarch64",
        "bins": (
            "nozcam_daemon.aarch64.static",
            "nozcam_daemon.rknn.aarch64",
        ),
        "models": ("nozcam-cpu.pte", "nozcam-rk3588.rknn"),
    },
    "awnn": {
        "dist": "pinozcam-runtime-a733",
        "module": "pinozcam_runtime_a733",
        "platform": "linux_aarch64",
        "bins": (
            "nozcam_daemon.aarch64.static",
            "nozcam_daemon.awnn.aarch64",
        ),
        "models": ("nozcam-cpu.pte", "nozcam-a733.nb"),
    },
    "vulkan": {
        "dist": "pinozcam-runtime-gpu-aarch64",
        "module": "pinozcam_runtime_gpu_aarch64",
        "platform": "manylinux_2_35_aarch64",
        "bins": (
            "nozcam_daemon.aarch64.static",
            "nozcam_daemon.vulkan.aarch64",
        ),
        "models": ("nozcam-cpu.pte", "nozcam-gpu.pte"),
    },
    "vulkan_x86_64": {
        "dist": "pinozcam-runtime-gpu-x86-64",
        "module": "pinozcam_runtime_gpu_x86_64",
        "platform": "manylinux_2_35_x86_64",
        "bins": (
            "nozcam_daemon.x86_64.static",
            "nozcam_daemon.vulkan.x86_64",
        ),
        "models": ("nozcam-cpu.pte", "nozcam-gpu.pte"),
    },
}

MODEL_METADATA = {
    "nozcam-cpu.pte": {
        "backend": "xnnpack",
        "format": "pte",
        "quantization": "int8",
    },
    "nozcam-gpu.pte": {
        "backend": "vulkan",
        "format": "pte",
        "quantization": "int8",
    },
    "nozcam-rk3566.rknn": {
        "backend": "rknn",
        "format": "rknn",
        "hardware": "rk3566",
        "quantization": "int8",
    },
    "nozcam-rk3576.rknn": {
        "backend": "rknn",
        "format": "rknn",
        "hardware": "rk3576",
        "quantization": "int8",
    },
    "nozcam-rk3588.rknn": {
        "backend": "rknn",
        "format": "rknn",
        "hardware": "rk3588",
        "quantization": "int8",
    },
    "nozcam-a733.nb": {
        "backend": "viplite",
        "format": "nb",
        "hardware": "a733",
        "quantization": "int8",
    },
}


def _sha256(path):
    """Return the lowercase SHA-256 digest of one file."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy(source, destination, executable=False):
    """Copy one required artifact into the staged package."""
    if not os.path.isfile(source):
        raise RuntimeError("missing runtime artifact: %s" % source)
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    shutil.copy2(source, destination)
    if executable:
        os.chmod(destination, 0o755)


def _write(path, value):
    """Write deterministic UTF-8 text with Unix newlines."""
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(value)


def _record_payload(package, spec):
    """Record byte sizes and hashes for every installed payload file."""
    files = {}
    for relative in ("bin", "models", "THIRD_PARTY_LICENSES"):
        directory = os.path.join(package, relative)
        for name in sorted(os.listdir(directory)):
            path = os.path.join(directory, name)
            files["%s/%s" % (relative, name)] = {
                "bytes": os.path.getsize(path),
                "sha256": _sha256(path),
            }
    license_path = os.path.join(package, "PiNozCam.LICENSE")
    files["PiNozCam.LICENSE"] = {
        "bytes": os.path.getsize(license_path),
        "sha256": _sha256(license_path),
    }
    expected = set(spec["bins"]) | set(spec["models"])
    present = {
        os.path.basename(name)
        for name in files
        if name.startswith("bin/") or name.startswith("models/")
    }
    if present != expected:
        raise RuntimeError("staged payload does not match target definition")
    return files


def _stage(stage, artifacts, target, version, revision):
    """Assemble the temporary Python package consumed by wheel."""
    spec = TARGETS[target]
    package = os.path.join(stage, spec["module"])
    for name in spec["bins"]:
        _copy(
            os.path.join(artifacts, "bin", name),
            os.path.join(package, "bin", name),
            executable=True,
        )
    for name in spec["models"]:
        _copy(
            os.path.join(artifacts, "models", name),
            os.path.join(package, "models", name),
        )
    notices = os.path.join(ROOT, "THIRD_PARTY_LICENSES")
    for name in sorted(os.listdir(notices)):
        source = os.path.join(notices, name)
        if os.path.isfile(source):
            _copy(
                source,
                os.path.join(package, "THIRD_PARTY_LICENSES", name),
            )
    _copy(
        os.path.join(ROOT, "LICENSE"),
        os.path.join(package, "PiNozCam.LICENSE"),
    )

    manifest = {
        "distribution": spec["dist"],
        "files": _record_payload(package, spec),
        "format": 2,
        "models": {
            name: MODEL_METADATA[name] for name in spec["models"]
        },
        "module": spec["module"],
        "plugin_version": version,
        "runner_protocol": 1,
        "source": (
            "https://github.com/DrAlexLiu/OctoPrint-PiNozCam/commit/%s"
            % revision
        ),
        "target": target,
    }
    _write(
        os.path.join(package, "manifest.json"),
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    )
    _write(
        os.path.join(package, "__init__.py"),
        (
            '"""Native PiNozCam runtime payload."""\n'
            "import os\n\n"
            "ROOT = os.path.dirname(os.path.abspath(__file__))\n"
            'BIN_DIR = os.path.join(ROOT, "bin")\n'
            'MODEL_DIR = os.path.join(ROOT, "models")\n'
            "TARGET = %r\n"
            "VERSION = %r\n"
        )
        % (target, version),
    )

    setup_source = '''from setuptools import Distribution, setup
from wheel.bdist_wheel import bdist_wheel as _bdist_wheel


class BinaryDistribution(Distribution):
    def has_ext_modules(self):
        return True


class bdist_wheel(_bdist_wheel):
    def finalize_options(self):
        _bdist_wheel.finalize_options(self)
        self.root_is_pure = False

    def get_tag(self):
        return "py3", "none", {platform!r}


setup(
    name={dist!r},
    version={version!r},
    description="Native runner and model payload for PiNozCam ({target})",
    long_description=(
        "PiNozCam native runtime for the {target} target. "
        "Install OctoPrint-PiNozCam for its user interface and configuration."
    ),
    long_description_content_type="text/plain",
    url="https://github.com/DrAlexLiu/OctoPrint-PiNozCam",
    project_urls={{
        "Source": "https://github.com/DrAlexLiu/OctoPrint-PiNozCam",
    }},
    license="AGPL-3.0-only",
    python_requires=">=3.7,<4",
    packages=[{module!r}],
    package_data={{{module!r}: [
        "manifest.json", "PiNozCam.LICENSE", "bin/*", "models/*",
        "THIRD_PARTY_LICENSES/*",
    ]}},
    include_package_data=False,
    zip_safe=False,
    distclass=BinaryDistribution,
    cmdclass={{"bdist_wheel": bdist_wheel}},
)
'''.format(
        platform=spec["platform"],
        dist=spec["dist"],
        version=version,
        target=target,
        module=spec["module"],
    )
    _write(os.path.join(stage, "setup.py"), setup_source)


def main():
    """Parse inputs, build one Wheel, and record its digest."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=sorted(TARGETS), required=True)
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()

    artifacts = os.path.abspath(args.artifact_root)
    output = os.path.abspath(args.output)
    os.makedirs(output, exist_ok=True)
    spec = TARGETS[args.target]
    with tempfile.TemporaryDirectory(
            prefix="pinozcam_runtime_%s_" % args.target) as stage:
        _stage(
            stage,
            artifacts,
            args.target,
            args.version,
            args.revision,
        )
        subprocess.run(
            [sys.executable, "setup.py", "bdist_wheel", "--dist-dir", output],
            cwd=stage,
            env=dict(os.environ, SOURCE_DATE_EPOCH="1786200000"),
            check=True,
        )

    filename = "%s-%s-py3-none-%s.whl" % (
        spec["dist"].replace("-", "_"),
        args.version,
        spec["platform"],
    )
    wheel = os.path.join(output, filename)
    if not os.path.isfile(wheel):
        raise RuntimeError("expected Wheel was not produced: %s" % wheel)
    wheels = sorted(
        name for name in os.listdir(output) if name.endswith(".whl")
    )
    _write(
        os.path.join(output, "SHA256SUMS"),
        "".join(
            "%s  %s\n" % (_sha256(os.path.join(output, name)), name)
            for name in wheels
        ),
    )
    print("%s  %s  %d" % (
        _sha256(wheel),
        filename,
        os.path.getsize(wheel),
    ))


if __name__ == "__main__":
    main()
