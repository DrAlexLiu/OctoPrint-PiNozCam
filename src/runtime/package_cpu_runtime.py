#!/usr/bin/env python3
"""Create one generic CPU runtime Wheel from verified build artifacts."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TARGETS = {
    "armhf": {
        "module": "pinozcam_runner",
        "platform": "manylinux2014_armv7l",
        "runner": "nozcam_daemon.armhf.static",
    },
    "aarch64": {
        "module": "pinozcam_runner",
        "platform": "manylinux2014_aarch64",
        "runner": "nozcam_daemon.aarch64.static",
    },
    "macos_arm64": {
        "module": "pinozcam_runner",
        # No ".static": macOS ships no static libSystem, so this runner
        # links libSystem and libc++ like every other Mach-O executable.
        "platform": "macosx_14_0_arm64",
        "runner": "nozcam_daemon.macos.arm64",
        # The only target that ships a second model. One daemon runs both:
        # it is built with the CoreML delegate as well as XNNPACK, so the
        # choice is which .pte to hand it, not which binary to launch.
        # CoreML reaches the Neural Engine and is what normally runs; the
        # CPU model stays as the fallback for a Mac where CoreML will not
        # load it.
        "extra_models": {
            "nozcam-coreml.pte": {
                "backend": "coreml",
                "format": "pte",
                "quantization": "int8",
            },
        },
    },
    "x86_64": {
        "module": "pinozcam_runner",
        "platform": "manylinux2014_x86_64",
        "runner": "nozcam_daemon.x86_64.static",
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
    """Copy one required payload file and set its installed mode."""
    if not os.path.isfile(source):
        raise RuntimeError("missing runtime input: %s" % source)
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    shutil.copy2(source, destination)
    if executable:
        os.chmod(destination, 0o755)


def _write(path, value):
    """Write deterministic UTF-8 text with Unix newlines."""
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(value)


def _stage(stage, artifacts, target, version, revision):
    """Assemble the temporary Python package consumed by wheel."""
    spec = TARGETS[target]
    module = spec["module"]
    package = os.path.join(stage, module)
    runner = spec["runner"]
    model = "nozcam-cpu.pte"
    models = {model: {"backend": "xnnpack", "format": "pte",
                      "quantization": "int8"}}
    models.update(spec.get("extra_models", {}))
    _copy(os.path.join(artifacts, "bin", runner),
          os.path.join(package, "bin", runner), executable=True)
    for name in sorted(models):
        _copy(os.path.join(artifacts, "models", name),
              os.path.join(package, "models", name))
    _copy(os.path.join(ROOT, "LICENSE"),
          os.path.join(package, "PiNozCam.LICENSE"))

    notices = os.path.join(ROOT, "THIRD_PARTY_LICENSES")
    for name in sorted(os.listdir(notices)):
        source = os.path.join(notices, name)
        if os.path.isfile(source):
            _copy(source, os.path.join(
                package, "THIRD_PARTY_LICENSES", name))

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
    manifest = {
        "distribution": "pinozcam-runtime",
        "files": files,
        "format": 2,
        "models": models,
        "module": module,
        "plugin_version": version,
        "runner_protocol": 1,
        "source": (
            "https://github.com/DrAlexLiu/OctoPrint-PiNozCam/commit/%s" %
            revision
        ),
        "target": target,
    }
    _write(os.path.join(package, "manifest.json"),
           json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    init_source = (
        '"""Native PiNozCam CPU runtime payload."""\n'
        "import os\n\n"
        "ROOT = os.path.dirname(os.path.abspath(__file__))\n"
        'BIN_DIR = os.path.join(ROOT, "bin")\n'
        'MODEL_DIR = os.path.join(ROOT, "models")\n'
        "TARGET = %r\n"
        "VERSION = %r\n" % (target, version)
    )
    _write(os.path.join(package, "__init__.py"), init_source)

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
    name="pinozcam-runtime",
    version={version!r},
    description="Native CPU runner and model payload for PiNozCam",
    long_description=(
        "PiNozCam native XNNPACK CPU runtime. One Wheel is published per "
        "platform -- Linux armv7l, aarch64 and x86_64, and macOS on Apple "
        "silicon -- and pip installs the one matching this machine. "
        "Install OctoPrint-PiNozCam for the user interface and "
        "configuration; this package carries only the compiled runner and "
        "its model."
    ),
    long_description_content_type="text/plain",
    url="https://github.com/DrAlexLiu/OctoPrint-PiNozCam",
    project_urls={{
        "Source": "https://github.com/DrAlexLiu/OctoPrint-PiNozCam",
    }},
    license="AGPL-3.0-only",
    classifiers=[
        "Development Status :: 4 - Beta",
        "Environment :: Web Environment",
        "Intended Audience :: End Users/Desktop",
        # The distribution spans both; each Wheel's platform tag is what
        # actually decides installability.
        "Operating System :: POSIX :: Linux",
        "Operating System :: MacOS :: MacOS X",
        "Programming Language :: C++",
        "Programming Language :: Python :: 3",
        "Topic :: Printing",
    ],
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
        target=target,
        version=version,
        module=module,
    )
    _write(os.path.join(stage, "setup.py"), setup_source)


def main():
    """Parse build inputs, create the Wheel, and record its digest."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--target", choices=sorted(TARGETS), required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()

    artifacts = os.path.abspath(args.artifact_root)
    output = os.path.abspath(args.output)
    os.makedirs(output, exist_ok=True)
    with tempfile.TemporaryDirectory(
            prefix="pinozcam_runtime_%s_" % args.target) as stage:
        _stage(stage, artifacts, args.target, args.version, args.revision)
        subprocess.run(
            [sys.executable, "setup.py", "bdist_wheel", "--dist-dir", output],
            cwd=stage,
            env=dict(os.environ, SOURCE_DATE_EPOCH="1786200000"),
            check=True,
        )

    filename = "pinozcam_runtime-%s-py3-none-%s.whl" % (
        args.version, TARGETS[args.target]["platform"])
    wheel = os.path.join(output, filename)
    if not os.path.isfile(wheel):
        raise RuntimeError("expected Wheel was not produced: %s" % wheel)
    digest = _sha256(wheel)
    _write(os.path.join(output, "SHA256SUMS"),
           "%s  %s\n" % (digest, filename))
    print("%s  %s  %d" % (digest, filename, os.path.getsize(wheel)))


if __name__ == "__main__":
    main()
