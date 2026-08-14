#!/usr/bin/env bash
# Build the macOS arm64 PiNozCam CPU daemon on an Apple Silicon host.
#
# Same shape as bootstrap_aarch64_cpu.sh, with the three differences that
# are Darwin toolchain facts rather than choices:
#
#   * No -DCMAKE_EXE_LINKER_FLAGS=-static. macOS ships no static libSystem;
#     fully static executables are unsupported by design, so the daemon
#     links libSystem and libc++ and is verified with otool instead.
#   * build_macos.sh replaces build.sh, because the link line needs Apple
#     ld spellings (-force_load, and no -z noexecstack / --whole-archive).
#   * The libm.so fixup is absent: that works around a GNU ld behaviour
#     when a static link is requested, and nothing here requests one.
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "usage: $0 <work-root> <artifact-root>" >&2
  exit 2
fi
if [ "$(uname -s)" != "Darwin" ] || [ "$(uname -m)" != "arm64" ]; then
  echo "this native build requires an Apple Silicon macOS host" >&2
  exit 2
fi

WORK_ROOT=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
ARTIFACT_ROOT=$(cd "$(dirname "$2")" && pwd)/$(basename "$2")
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
ET_ROOT="$WORK_ROOT/executorch"
ET_BUILD_DIR="$ET_ROOT/cmake-out-macos-arm64"
VENV="$WORK_ROOT/codegen-venv"
PATCH="$SCRIPT_DIR/patches/upsample_nearest2d_2x_fastpath.patch"

ET_TAG=v1.3.1
ET_COMMIT=e2f18eb23c45bd22ca332b0b8b49a81de304b472
TORCH_VERSION=2.12.1
JOBS=${JOBS:-$(sysctl -n hw.ncpu)}

mkdir -p "$WORK_ROOT" "$ARTIFACT_ROOT/bin"
if [ ! -d "$ET_ROOT/.git" ]; then
  git clone --branch "$ET_TAG" --depth 1 --recurse-submodules \
    --shallow-submodules https://github.com/pytorch/executorch.git "$ET_ROOT"
fi

actual_commit=$(git -C "$ET_ROOT" rev-parse HEAD)
if [ "$actual_commit" != "$ET_COMMIT" ]; then
  echo "ExecuTorch commit mismatch: $actual_commit (expected $ET_COMMIT)" >&2
  exit 1
fi
if git -C "$ET_ROOT" apply --reverse --check "$PATCH" >/dev/null 2>&1; then
  echo "PiNozCam ExecuTorch patch is already applied"
elif git -C "$ET_ROOT" apply --check "$PATCH"; then
  git -C "$ET_ROOT" apply "$PATCH"
else
  echo "PiNozCam ExecuTorch patch does not apply to $ET_COMMIT" >&2
  exit 1
fi
git -C "$ET_ROOT" diff --check

if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --disable-pip-version-check --upgrade pip
# torchgen must exist BEFORE cmake configures: Codegen.cmake probes for it
# with execute_process and bakes the resulting path into the build. Install
# it afterwards and configure silently records an empty root, which fails
# much later as "/packaged/ATen/native/native_functions.yaml not found".
"$VENV/bin/python" -m pip install --disable-pip-version-check \
  "torch==$TORCH_VERSION"
"$VENV/bin/python" -m pip install --disable-pip-version-check \
  "PyYAML==6.0.3" "ruamel.yaml==0.19.1" "flatbuffers==25.12.19"
"$VENV/bin/python" -c "import torchgen, os; \
  print('torchgen:', os.path.dirname(torchgen.__file__))"

cmake -S "$ET_ROOT" -B "$ET_BUILD_DIR" \
  -DCMAKE_BUILD_TYPE=Release \
  -DPython3_EXECUTABLE="$VENV/bin/python" \
  -DEXECUTORCH_BUILD_XNNPACK=ON \
  -DEXECUTORCH_BUILD_EXECUTOR_RUNNER=ON \
  -DEXECUTORCH_BUILD_EXTENSION_DATA_LOADER=ON \
  -DEXECUTORCH_BUILD_EXTENSION_RUNNER_UTIL=ON
cmake --build "$ET_BUILD_DIR" --parallel "$JOBS" --target executor_runner

OUT="$ET_BUILD_DIR/nozcam_daemon" \
ET_ROOT="$ET_ROOT" ET_BUILD_DIR="$ET_BUILD_DIR" \
  "$SCRIPT_DIR/build_macos.sh"

daemon="$ET_BUILD_DIR/nozcam_daemon"
if [ ! -x "$daemon" ]; then
  echo "daemon was not produced: $daemon" >&2
  exit 1
fi
if ! file "$daemon" | grep -q 'Mach-O 64-bit executable arm64'; then
  echo "daemon is not an arm64 Mach-O executable" >&2
  file "$daemon" >&2
  exit 1
fi
# A macOS binary cannot be fully static, but it must not have picked up
# anything beyond the two system libraries every C++ program here needs.
extra=$(otool -L "$daemon" | tail -n +2 | awk '{print $1}' \
        | grep -vE '^/usr/lib/(libSystem\.B|libc\+\+\.1)\.dylib$' || true)
if [ -n "$extra" ]; then
  echo "daemon links unexpected libraries:" >&2
  echo "$extra" >&2
  exit 1
fi

install -m 0755 "$daemon" "$ARTIFACT_ROOT/bin/nozcam_daemon.macos.arm64"
shasum -a 256 "$ARTIFACT_ROOT/bin/nozcam_daemon.macos.arm64"
