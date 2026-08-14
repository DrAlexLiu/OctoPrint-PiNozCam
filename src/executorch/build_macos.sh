#!/usr/bin/env bash
# Build nozcam_daemon on macOS (Apple Silicon), against a native ExecuTorch
# build tree.
#
#     ET_BUILD_DIR=~/executorch/cmake-out-native ./build_daemon_macos.sh
#
# Deliberately separate from build_daemon.sh rather than adding branches to
# it: that script produces the shipped armhf and aarch64 binaries, which are
# expected to rebuild byte-for-byte, and this one differs in four places
# that are all Linux-vs-Darwin toolchain facts:
#
#   -static            Apple ships no static libSystem; fully static
#                      executables are unsupported on macOS, by design.
#   -z noexecstack     A GNU ld option. Mach-O has no PT_GNU_STACK segment
#                      to mark, so the hazard it guards against does not
#                      exist here.
#   --whole-archive    GNU ld. Apple's equivalent is per-archive:
#                      -Wl,-force_load,<archive>.
#   readelf GNU_STACK  ELF-only. Checked with otool/file instead.
#
# Everything else -- scraping CXX flags and the library list out of
# executor_runner's flags.make and link.txt -- is the same trick, for the
# same reason: identical flags, identical archives, a build measured in
# seconds.
set -euo pipefail

ET_ROOT=${ET_ROOT:-$HOME/executorch}
BD=${ET_BUILD_DIR:-$ET_ROOT/cmake-out-native}
CXX=${CXX:-clang++}
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=${OUT:-$BD/nozcam_daemon}

[ -d "$BD" ] || { echo "ExecuTorch build directory not found: $BD" >&2; exit 1; }
[ "$(uname -s)" = "Darwin" ] || {
  echo "this script is for macOS; use build_daemon.sh on Linux" >&2; exit 1; }

# Same layout convention as build.sh: the daemon lives beside this script
# and the postprocess is shared from ../common, so all four runners compile
# the identical file rather than each carrying a copy that can drift.
SRC_DAEMON=$HERE/nozcam_daemon.cpp
SRC_POST=$HERE/../common/nozcam_postprocess.cpp
INC_DIR=$HERE/../common
for source in "$SRC_DAEMON" "$SRC_POST" "$INC_DIR/nozcam_postprocess.h"; do
  [ -f "$source" ] || { echo "source not found: $source" >&2; exit 1; }
done

FLAGS=$(find "$BD" -path "*executor_runner.dir/flags.make" | head -1)
LINK=$(find "$BD" -name link.txt -path "*executor_runner*" | head -1)
[ -n "$FLAGS" ] && [ -n "$LINK" ] || {
  echo "no flags.make / link.txt under $BD -- build ExecuTorch first" >&2
  exit 1; }

get () { grep -E "^$1 = " "$FLAGS" | sed "s/^$1 = //"; }
DEFS=$(get CXX_DEFINES); INCS=$(get CXX_INCLUDES); CFLAGS=$(get CXX_FLAGS)

OBJ=$(mktemp -d)
trap 'rm -rf "$OBJ"' EXIT

echo "=== compile (macOS $(uname -m)) ==="
for src in "$SRC_DAEMON" "$SRC_POST"; do
  echo "  $(basename "$src")"
  # shellcheck disable=SC2086
  $CXX $DEFS $INCS $CFLAGS -I"$INC_DIR" -c "$src" \
       -o "$OBJ/$(basename "${src%.cpp}").o"
done

echo "=== link (reusing executor_runner's library list) ==="
LIBS=$(tr ' ' '\n' < "$LINK" | awk '
  /^-o$/ {skip=1; next} skip==1 {skip=0; next}
  /\.o$/ {next}
  /executor_runner/ {next}
  /^-static$/ {next}
  NR==1 {next}
  {print}' | tr '\n' ' ')
LDFLAGS=$(head -1 "$LINK" | grep -oE '(-O[0-9]|-pthread)' | tr '\n' ' ' || true)

cd "$BD"
DL=$(find "$BD" -name libextension_data_loader.a | head -1)
DL_ARGS=()
if [ -n "$DL" ]; then
  # Apple ld: force_load takes the archive as its argument, and unlike
  # --whole-archive it does not need a matching "off" switch afterwards.
  DL_ARGS=(-Wl,-force_load,"$DL")
else
  DL_OBJ=$(find "$BD" \
    -path "*executor_runner.dir/extension/data_loader/file_data_loader.cpp.o" \
    | head -1)
  [ -n "$DL_OBJ" ] || { echo "FileDataLoader not found under $BD" >&2; exit 1; }
  DL_ARGS=("$DL_OBJ")
fi

# shellcheck disable=SC2086
$CXX $LDFLAGS -o "$OUT" "$OBJ"/*.o "${DL_ARGS[@]}" $LIBS

echo "=== result ==="
ls -la "$OUT"
file -b "$OUT" | cut -c1-78
echo "  dynamic libraries:"
otool -L "$OUT" | tail -n +2 | awk '{print "    " $1}'
