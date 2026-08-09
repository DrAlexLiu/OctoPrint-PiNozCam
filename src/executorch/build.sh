#!/usr/bin/env bash
# Build nozcam_daemon, the resident inference runner.
#
# It reuses static libraries that an ExecuTorch tree has already built, and
# copies the compile and link flags out of executor_runner's flags.make and
# link.txt, substituting our own object files. That is what makes the build
# take seconds and reproduce byte for byte -- same flags, same archives.
#
# Usage:  ./build.sh armhf|aarch64|aarch64-vulkan|native
#
# The ExecuTorch build directory is found from ET_BUILD_DIR if set, else
# $ET_ROOT/cmake-out-<arch>, else $HOME/executorch/cmake-out-<arch>.
#
#  This script does NOT configure or build ExecuTorch. Do that once, per
# the instructions in ../README.md, and leave a populated cmake-out-<arch>.
# In particular do not go looking for a build_armhf.sh to run: those
# wrapper scripts begin with `rm -rf cmake-out-<arch>`, which destroys the
# tree this needs and costs a quarter of an hour to rebuild.
set -euo pipefail
ARCH=${1:?usage: build_daemon.sh armhf|aarch64|aarch64-vulkan|native}
ET_ROOT=${ET_ROOT:-$HOME/executorch}
case "$ARCH" in
  armhf)   DEFAULT_BD=$ET_ROOT/cmake-out-armhf;   CXX=${CXX:-arm-linux-gnueabihf-g++} ;;
  aarch64) DEFAULT_BD=$ET_ROOT/cmake-out-aarch64; CXX=${CXX:-aarch64-linux-gnu-g++} ;;
  aarch64-vulkan) DEFAULT_BD=$ET_ROOT/cmake-out-vulkan-aarch64; CXX=${CXX:-aarch64-linux-gnu-g++} ;;
  native)  DEFAULT_BD=$ET_ROOT/cmake-out-native;  CXX=${CXX:-g++} ;;   # on the board
  *) echo "unsupported arch $ARCH"; exit 1 ;;
esac
BD=${ET_BUILD_DIR:-$DEFAULT_BD}
HERE=$(cd "$(dirname "$0")" && pwd)
[ -d "$BD" ] || {
  echo "ExecuTorch build directory not found: $BD"
  echo "Configure and build ExecuTorch first (see README.md), or point"
  echo "ET_BUILD_DIR at an existing cmake-out-<arch>."
  exit 1
}

SRC_DAEMON=$HERE/nozcam_daemon.cpp
SRC_POST=$HERE/../common/nozcam_postprocess.cpp
INC_DIR=$HERE/../common
for source in "$SRC_DAEMON" "$SRC_POST" "$INC_DIR/nozcam_postprocess.h"; do
  [ -f "$source" ] || { echo "source not found: $source" >&2; exit 1; }
done

FLAGS=$(find "$BD" -path "*executor_runner.dir/flags.make" | head -1)
LINK=$(find "$BD" -name link.txt -path "*executor_runner*" | head -1)
[ -n "$FLAGS" ] && [ -n "$LINK" ] || { echo "no flags.make / link.txt under $BD"; exit 1; }

get () { grep -E "^$1 = " "$FLAGS" | sed "s/^$1 = //"; }
DEFS=$(get CXX_DEFINES)
# ExecuTorch's own generated flags use -I for all dependency headers. Mark
# those external directories as system includes so -Werror below checks
# PiNozCam source without failing on diagnostics inside dependency headers.
INCS=$(get CXX_INCLUDES | sed 's/-I/-isystem /g')
CFLAGS=$(get CXX_FLAGS)
# Release builds must reject warnings from both the protocol loop and the
# postprocessor. These flags do not alter generated code; they turn compiler
# diagnostics into a reproducible build gate on every target toolchain.
# The JSON buffer is sized from the validated canvas coordinate range. GCC's
# conservative generic-double analysis cannot see that bound and emits a
# false format-truncation warning for the four-value snprintf.
WARN_FLAGS="-Wall -Wextra -Werror -Wno-format-truncation"

OUT=$BD/nozcam_daemon
OBJ=$BD/nozcam_daemon_objs; mkdir -p "$OBJ"
echo "=== compile (${ARCH}) ==="
for src in "$SRC_DAEMON" "$SRC_POST"; do
  o="$OBJ/$(basename "${src%.cpp}").o"
  echo "  $(basename "$src")"
  # shellcheck disable=SC2086
  $CXX $DEFS $INCS $CFLAGS $WARN_FLAGS -I"$INC_DIR" -std=c++17 \
       -c "$src" -o "$o"
done

echo "=== link (reusing executor_runner's library list) ==="
# link.txt looks like: <CXX> <flags> -o <out> <objs...> <libs...>
# Drop its own -o and .o entries and substitute ours. -static is dropped
# here and re-added below with the stack flag, so both end up on the line
# exactly once.
LIBS=$(tr ' ' '\n' < "$LINK" | awk '
  /^-o$/ {skip=1; next} skip==1 {skip=0; next}
  /\.o$/ {next}
  /executor_runner/ {next}
  /^-static$/ {next}
  NR==1 {next}
  {print}' | tr '\n' ' ')
LDFLAGS=$(head -1 "$LINK" | grep -oE '(--sysroot=[^ ]+|-mfpu=[^ ]+|-mfloat-abi=[^ ]+|-Wl,[^ ]+|-O[0-9]|-pthread)' | tr '\n' ' ')

#  -z noexecstack is not optional. Several XNNPACK aarch32 assembly
# objects carry no .note.GNU-stack section, and the linker's fallback for
# that is to mark the whole program's stack executable: the armhf binary
# came out with GNU_STACK RWE. Nothing here needs an executable stack, and
# on a hardened kernel it is the difference between running and not. The
# linker says so itself during the link:
#   "missing .note.GNU-stack section implies executable stack"
if [ "$ARCH" = "aarch64-vulkan" ]; then
  # Vulkan loads the board vendor's ICD at runtime through Volk/dlopen.
  # A fully static executable cannot do that; keep libstdc++/glibc dynamic,
  # exactly like executor_runner from this same build tree.
  LDFLAGS="$LDFLAGS -Wl,-z,noexecstack"
else
  LDFLAGS="$LDFLAGS -static -Wl,-z,noexecstack"
fi

#  The library paths inside link.txt are relative to the build directory,
# so the link has to happen there or it is a wall of "cannot find
# libexecutorch.a".
cd "$BD"
#  executor_runner's link.txt has no extension_data_loader -- it uses a
# different loader. We use FileDataLoader, so it must be added explicitly or
# the link fails with "undefined reference to vtable for
# executorch::extension::FileDataLoader".
DL=$(find "$BD" -name libextension_data_loader.a | head -1)
DL_ARGS=()
if [ -n "$DL" ]; then
  DL_ARGS=(-Wl,--whole-archive "$DL" -Wl,--no-whole-archive)
else
  # ExecuTorch's Vulkan executor_runner compiles FileDataLoader directly
  # into its executable instead of producing libextension_data_loader.a.
  # Reuse that exact object from the same build tree; link.txt's generic
  # object filter above deliberately removed it together with runner main.
  DL_OBJ=$(find "$BD" -path "*executor_runner.dir/extension/data_loader/file_data_loader.cpp.o" | head -1)
  [ -n "$DL_OBJ" ] || {
    echo "FileDataLoader library/object not found under $BD"
    exit 1
  }
  DL_ARGS=("$DL_OBJ")
fi
# shellcheck disable=SC2086
$CXX $LDFLAGS -o "$OUT" "$OBJ"/*.o "${DL_ARGS[@]}" $LIBS
echo "=== result ==="
ls -la "$OUT"
file -b "$OUT" | cut -c1-78
DYNAMIC_GLIBC=$(nm -D "$OUT" 2>/dev/null | grep -c GLIBC || true)
if [ "$ARCH" = "aarch64-vulkan" ]; then
  echo "  dynamic GLIBC symbols: ${DYNAMIC_GLIBC:-0} (expected for Vulkan)"
else
  echo "  dynamic GLIBC symbols (want 0): ${DYNAMIC_GLIBC:-0}"
fi

#  A real assertion: parse the GNU_STACK line and EXIT NON-ZERO if the
# stack is executable. An earlier version did `grep -A1 GNU_STACK | tail -1`,
# which read the FOLLOWING segment (GNU_RELRO) because GNU_STACK's flags are
# on its own line -- so it was checking the wrong thing -- and its failure
# branch only echoed, leaving the script's exit status 0. A check that
# cannot fail the build is not a check.
#
# readelf -lW prints, on one line:
#   GNU_STACK  0x000000 0x00000000 0x00000000 0x00000 0x00000 RW  0x10
# so the flags are the field after the two sizes.
STACK_LINE=$(readelf -lW "$OUT" | grep -E '^\s*GNU_STACK' | head -1)
[ -n "$STACK_LINE" ] || { echo "  FAIL: no GNU_STACK segment found"; exit 1; }
STACK_FLAGS=$(echo "$STACK_LINE" | grep -oE '\b[RWE]{1,3}\b[[:space:]]+0x' \
              | grep -oE '^[RWE]{1,3}')
echo "  stack flags: ${STACK_FLAGS:-?}"
case "$STACK_FLAGS" in
  *E*) echo "  FAIL: executable stack (GNU_STACK $STACK_FLAGS)."
       echo "        -Wl,-z,noexecstack did not take effect."
       exit 1 ;;
  RW)  echo "  ok: stack is RW, not executable" ;;
  *)   echo "  FAIL: unexpected GNU_STACK flags '$STACK_FLAGS'"; exit 1 ;;
esac
