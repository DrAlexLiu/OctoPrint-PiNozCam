#!/usr/bin/env bash
# Build the AArch64 PiNozCam Vulkan daemon from a pinned ExecuTorch release.
#
# This is a native build: run it on an AArch64 Linux host. The Vulkan backend
# is compiled without opening a Vulkan device, so the build host does not need
# an NVIDIA GPU (or any GPU at all).
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "usage: $0 <work-root> <artifact-root>" >&2
  exit 2
fi

if [ "$(uname -m)" != "aarch64" ] && [ "$(uname -m)" != "arm64" ]; then
  echo "this must be a native AArch64 build (host is $(uname -m))" >&2
  exit 1
fi

WORK_ROOT=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
ARTIFACT_ROOT=$(cd "$(dirname "$2")" && pwd)/$(basename "$2")
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
ET_ROOT="$WORK_ROOT/executorch"
ET_BUILD_DIR="$ET_ROOT/cmake-out-vulkan-aarch64"
VENV="$WORK_ROOT/codegen-venv"
PATCH="$SCRIPT_DIR/patches/upsample_nearest2d_2x_fastpath.patch"
SHADERC_ROOT="$WORK_ROOT/shaderc"
SHADERC_BUILD_DIR="$SHADERC_ROOT/cmake-out-release"

ET_TAG=v1.3.1
ET_COMMIT=e2f18eb23c45bd22ca332b0b8b49a81de304b472
SHADERC_TAG=v2025.3
SHADERC_COMMIT=8c2e602ce440b7739c95ff3d69cecb1adf6becda
TORCH_VERSION=2.12.1
PYYAML_VERSION=6.0.3
RUAMEL_YAML_VERSION=0.19.1
FLATBUFFERS_VERSION=25.12.19
JOBS=${JOBS:-$(getconf _NPROCESSORS_ONLN)}
CC=${CC:-gcc}
CXX=${CXX:-g++}

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
"$VENV/bin/python" -m pip install --disable-pip-version-check \
  --index-url https://download.pytorch.org/whl/cpu \
  "torch==$TORCH_VERSION"
"$VENV/bin/python" -m pip install --disable-pip-version-check \
  "PyYAML==$PYYAML_VERSION" \
  "ruamel.yaml==$RUAMEL_YAML_VERSION" \
  "flatbuffers==$FLATBUFFERS_VERSION"

# Ubuntu 24.04's shaderc 2023.8 cannot compile the integer-dot-product
# shaders shipped by ExecuTorch 1.3.1. Build glslc from the pinned Shaderc
# release and its commit-pinned dependency set instead of silently using the
# distro executable. This is native host tooling and is not shipped in the
# runtime Wheel.
if [ ! -d "$SHADERC_ROOT/.git" ]; then
  git clone --branch "$SHADERC_TAG" --depth 1 \
    https://github.com/google/shaderc.git "$SHADERC_ROOT"
fi
actual_shaderc_commit=$(git -C "$SHADERC_ROOT" rev-parse HEAD)
if [ "$actual_shaderc_commit" != "$SHADERC_COMMIT" ]; then
  echo "Shaderc commit mismatch: $actual_shaderc_commit" \
    "(expected $SHADERC_COMMIT)" >&2
  exit 1
fi
"$SHADERC_ROOT/utils/git-sync-deps"
cmake -S "$SHADERC_ROOT" -B "$SHADERC_BUILD_DIR" \
  -DCMAKE_BUILD_TYPE=Release \
  -DSHADERC_SKIP_TESTS=ON \
  -DSHADERC_SKIP_EXAMPLES=ON \
  -DSHADERC_SKIP_COPYRIGHT_CHECK=ON \
  -DSPIRV_SKIP_TESTS=ON
cmake --build "$SHADERC_BUILD_DIR" --parallel "$JOBS" --target glslc_exe
GLSLC="$SHADERC_BUILD_DIR/glslc/glslc"
if [ ! -x "$GLSLC" ]; then
  echo "pinned glslc was not produced: $GLSLC" >&2
  exit 1
fi
"$GLSLC" --version

# Volk loads the board's Vulkan ICD at runtime. Deliberately disable optional
# Boost stacktrace discovery so the published daemon has a small, deterministic
# dependency set which is present in the JetPack base image.
cmake -S "$ET_ROOT" -B "$ET_BUILD_DIR" \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_C_COMPILER="$CC" \
  -DCMAKE_CXX_COMPILER="$CXX" \
  -DCMAKE_DISABLE_FIND_PACKAGE_Boost=ON \
  -DGLSLC_PATH="$GLSLC" \
  -DPython3_EXECUTABLE="$VENV/bin/python" \
  -DEXECUTORCH_BUILD_VULKAN=ON \
  -DEXECUTORCH_BUILD_EXECUTOR_RUNNER=ON \
  -DEXECUTORCH_BUILD_EXTENSION_DATA_LOADER=ON \
  -DEXECUTORCH_BUILD_EXTENSION_RUNNER_UTIL=ON
cmake --build "$ET_BUILD_DIR" --parallel "$JOBS" --target executor_runner

ET_ROOT="$ET_ROOT" ET_BUILD_DIR="$ET_BUILD_DIR" CC="$CC" CXX="$CXX" \
  "$SCRIPT_DIR/build.sh" aarch64-vulkan

daemon="$ET_BUILD_DIR/nozcam_daemon"
if [ ! -x "$daemon" ]; then
  echo "daemon was not produced: $daemon" >&2
  exit 1
fi
strip "$daemon"

if ! file "$daemon" | grep -Eq 'ELF 64-bit LSB.*ARM aarch64'; then
  echo "daemon is not an AArch64 ELF" >&2
  file "$daemon" >&2
  exit 1
fi
if ! file "$daemon" | grep -q 'dynamically linked'; then
  echo "Vulkan daemon must be dynamically linked so Volk can load an ICD" >&2
  file "$daemon" >&2
  exit 1
fi

stack_flags=$(readelf -lW "$daemon" | awk '/GNU_STACK/ {print $7; exit}')
if [ "$stack_flags" != "RW" ]; then
  echo "unexpected GNU_STACK flags: ${stack_flags:-missing} (expected RW)" >&2
  exit 1
fi

needed=$(readelf -dW "$daemon" | sed -n 's/.*Shared library: \[\([^]]*\)\].*/\1/p' | sort -u)
expected=$(printf '%s\n' \
  ld-linux-aarch64.so.1 \
  libc.so.6 \
  libgcc_s.so.1 \
  libm.so.6 \
  libstdc++.so.6 | sort -u)
if [ "$needed" != "$expected" ]; then
  echo "unexpected Vulkan daemon shared-library dependencies:" >&2
  printf '%s\n' "$needed" >&2
  exit 1
fi

max_glibc=$(readelf --version-info "$daemon" \
  | grep -oE 'GLIBC_[0-9]+(\.[0-9]+)+' | sort -Vu | tail -1)
if [ -z "$max_glibc" ]; then
  echo "Vulkan daemon has no readable GLIBC version requirements" >&2
  exit 1
fi
highest=$(printf '%s\n' "$max_glibc" GLIBC_2.35 | sort -V | tail -1)
if [ "$highest" != "GLIBC_2.35" ]; then
  echo "Vulkan daemon requires $max_glibc; Wheel tag permits at most GLIBC_2.35" >&2
  exit 1
fi

max_glibcxx=$(readelf --version-info "$daemon" \
  | grep -oE 'GLIBCXX_[0-9]+(\.[0-9]+)+' | sort -Vu | tail -1)
highest=$(printf '%s\n' "$max_glibcxx" GLIBCXX_3.4.29 | sort -V | tail -1)
if [ -z "$max_glibcxx" ] || [ "$highest" != "GLIBCXX_3.4.29" ]; then
  echo "Vulkan daemon requires ${max_glibcxx:-unknown}; maximum is GLIBCXX_3.4.29" >&2
  exit 1
fi

max_cxxabi=$(readelf --version-info "$daemon" \
  | grep -oE 'CXXABI_[0-9]+(\.[0-9]+)+' | sort -Vu | tail -1)
highest=$(printf '%s\n' "$max_cxxabi" CXXABI_1.3.13 | sort -V | tail -1)
if [ -z "$max_cxxabi" ] || [ "$highest" != "CXXABI_1.3.13" ]; then
  echo "Vulkan daemon requires ${max_cxxabi:-unknown}; maximum is CXXABI_1.3.13" >&2
  exit 1
fi

install -m 0755 "$daemon" \
  "$ARTIFACT_ROOT/bin/nozcam_daemon.vulkan.aarch64"
file "$ARTIFACT_ROOT/bin/nozcam_daemon.vulkan.aarch64"
printf 'GNU_STACK=%s maximum-%s maximum-%s maximum-%s\n' \
  "$stack_flags" "$max_glibc" "$max_glibcxx" "$max_cxxabi"
sha256sum "$ARTIFACT_ROOT/bin/nozcam_daemon.vulkan.aarch64"
