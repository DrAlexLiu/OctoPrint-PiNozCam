#!/usr/bin/env python3
"""Exercise one production daemon over its binary pipe protocol."""

import argparse
import json
import math
import struct
import subprocess


HEADER = struct.Struct("<II")
INFER_PREFIX = struct.Struct("<Iiiii")
WIDTH = 640
HEIGHT = 384
FRAME_BYTES = WIDTH * HEIGHT * 3


def _request(command, payload=b""):
    """Encode one little-endian daemon request frame."""
    return HEADER.pack(command, len(payload)) + payload


def _responses(data, expected):
    """Decode exactly the expected number of daemon response frames."""
    offset = 0
    result = []
    for _ in range(expected):
        if offset + HEADER.size > len(data):
            raise RuntimeError("truncated response header")
        status, length = HEADER.unpack_from(data, offset)
        offset += HEADER.size
        end = offset + length
        if end > len(data):
            raise RuntimeError("truncated response payload")
        result.append((status, data[offset:end]))
        offset = end
    if offset != len(data):
        raise RuntimeError(
            "unexpected stdout bytes after protocol frames: %d" %
            (len(data) - offset)
        )
    return result


def main():
    """Run PING, INFO, one inference, and a clean shutdown."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--daemon", required=True)
    parser.add_argument("--launcher", action="append", default=[])
    parser.add_argument("--model", required=True)
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args()

    request_id = 0x51A7C0DE
    frame = bytes(range(256)) * (FRAME_BYTES // 256)
    infer_payload = INFER_PREFIX.pack(
        request_id, 0, 12, WIDTH, HEIGHT - 24
    ) + frame
    wire = b"".join(
        (
            _request(1),
            _request(3),
            _request(2, infer_payload),
            _request(4),
        )
    )

    process = subprocess.Popen(
        args.launcher
        + [args.daemon, args.model, "640", "384", "0.5", "0.05"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
        bufsize=0,
    )
    stdout, stderr = process.communicate(wire, timeout=args.timeout)
    if process.returncode != 0:
        raise RuntimeError(
            "daemon exited %d: %s" %
            (process.returncode, stderr.decode("utf-8", "replace"))
        )

    ping, info_frame, infer_frame, shutdown = _responses(stdout, 4)
    if ping != (0, b"pong"):
        raise RuntimeError("bad PING response: %r" % (ping,))
    if shutdown != (0, b"bye"):
        raise RuntimeError("bad SHUTDOWN response: %r" % (shutdown,))

    if info_frame[0] != 0:
        raise RuntimeError("INFO failed: %r" % (info_frame,))
    info = json.loads(info_frame[1].decode("utf-8"))
    if info["proc_w"] != WIDTH or info["proc_h"] != HEIGHT:
        raise RuntimeError("wrong input geometry: %r" % (info,))
    if info["n_outputs"] != 10:
        raise RuntimeError("wrong output count: %r" % (info,))

    if infer_frame[0] != 0:
        raise RuntimeError("INFER failed: %r" % (infer_frame,))
    result = json.loads(infer_frame[1].decode("utf-8"))
    if result["req_id"] != request_id:
        raise RuntimeError("request ID was not preserved")
    if not math.isfinite(float(result["severity"])):
        raise RuntimeError("severity is not finite")
    if not 0.0 <= float(result["severity"]) <= 1.0:
        raise RuntimeError("severity is outside 0..1")
    if len(result["scores"]) != len(result["boxes"]):
        raise RuntimeError("score/box counts differ")

    print(
        "PASS: PING/INFO/INFER/SHUTDOWN; %d boxes; severity %.6f" %
        (len(result["boxes"]), float(result["severity"]))
    )


if __name__ == "__main__":
    main()
