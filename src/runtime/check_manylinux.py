#!/usr/bin/env python3
"""Fail when a Wheel requires a newer glibc than its release contract."""

import argparse
import re
import subprocess
import sys


def main():
    """Run auditwheel and compare its computed ABI floor with the limit."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--architecture", required=True)
    parser.add_argument("--max-glibc-minor", required=True, type=int)
    parser.add_argument("wheel")
    args = parser.parse_args()

    completed = subprocess.run(
        [sys.executable, "-m", "auditwheel", "show", args.wheel],
        check=True,
        capture_output=True,
        text=True,
    )
    output = completed.stdout + completed.stderr
    print(output, end="" if output.endswith("\n") else "\n")
    match = re.search(
        r'is\s+consistent\s+with\s+(?:the\s+)?following\s+platform\s+tag:\s*'
        r'"manylinux_2_([0-9]+)_([A-Za-z0-9_]+)"',
        output,
    )
    if not match:
        raise RuntimeError("auditwheel did not report a manylinux ABI floor")
    minor = int(match.group(1))
    architecture = match.group(2)
    if architecture != args.architecture:
        raise RuntimeError(
            f"auditwheel reported architecture {architecture}, "
            f"expected {args.architecture}"
        )
    if minor > args.max_glibc_minor:
        raise RuntimeError(
            f"Wheel requires GLIBC 2.{minor}, release contract allows "
            f"at most 2.{args.max_glibc_minor}"
        )
    print(
        f"PASS: auditwheel ABI floor GLIBC 2.{minor} <= "
        f"2.{args.max_glibc_minor}"
    )


if __name__ == "__main__":
    main()
