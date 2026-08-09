"""Client for resident CPU, NPU and GPU inference daemons.

All backends use the same little-endian pipe protocol:
    request   [4B cmd][4B len][payload]
    response  [4B status][4B len][payload]
    cmd 2 INFER, payload = [4B request_id][4x int32 content rect]
                 + HWC uint8 RGB, 4 + 16 + 640*384*3 = 737,300 B

The request ID remains in the payload so the fixed eight-byte header stays
compatible with older daemons. Pillow performs the model resize to preserve
the preprocessing used for calibration.
"""

import hashlib
import importlib
import json
import math
import os
import select
import struct
import subprocess
import threading
import time

from PIL import Image

PROC_WIDTH = 640
PROC_HEIGHT = 384

# Finite pipe deadlines keep a wedged daemon from blocking detection.
START_TIMEOUT = 120.0
INFER_TIMEOUT = 60.0

# Legitimate JSON replies are small; a larger reply means desynchronisation.
MAX_RESPONSE = 1 << 20

# Fixed model/postprocess contract.
EXPECTED_OUTPUTS = 10

# Bound retries without permanently disabling the monitor.
BACKOFF_SECONDS = (0.0, 1.0, 5.0, 30.0)

_CMD_PING = 1
_CMD_INFER = 2
_CMD_INFO = 3
_CMD_SHUTDOWN = 4

# CPU model calibrated for the fixed fit preprocessing path.
DEFAULT_MODEL = "nozcam-cpu.pte"

# Native payloads live in target-specific runtime distributions. Distinct
# package names prevent same-platform accelerators from sharing stale files.
_RUNTIME_MODULES = {
    "armhf": "pinozcam_runtime_armhf",
    "aarch64": "pinozcam_runtime_aarch64",
    "x86_64": "pinozcam_runtime_x86_64",
    "rknn3566": "pinozcam_runtime_rknn3566",
    "rknn3576": "pinozcam_runtime_rknn3576",
    "rknn3588": "pinozcam_runtime_rknn3588",
    "awnn": "pinozcam_runtime_a733",
    "vulkan": "pinozcam_runtime_jetson_orin",
}


class BackendUnavailable(Exception):
    """No daemon binary ships for this platform, or the model is absent."""


class BackendMismatch(BackendUnavailable):
    """The daemon, model, protocol, or expected result shape disagrees."""


class BackendTimeout(IOError):
    """A pipe operation exceeded its deadline and requires daemon restart."""


def _machine_tag():
    """Return the native binary suffix for this Python process.

    Process word size precedes kernel architecture because a 32-bit userland
    may run under a 64-bit kernel.
    """
    bits = struct.calcsize("P") * 8
    if bits == 32:
        return "armhf"
    machine = os.uname().machine.lower()
    if machine in ("x86_64", "amd64"):
        return "x86_64"
    if machine in ("aarch64", "arm64"):
        return "aarch64"
    raise BackendUnavailable(
        "no nozcam_daemon binary for %d-bit %s" % (bits, machine)
    )


def _detect_rockchip_chip():
    """Return the plain Rockchip SoC compatible name, or None.

    Hyphenated board-level compatible entries are intentionally ignored so
    Python and the RKNN daemon identify the same chip entry.
    """
    try:
        with open("/proc/device-tree/compatible", "rb") as handle:
            compatible = handle.read()
    except (IOError, OSError):
        return None
    for entry in compatible.split(b"\x00"):
        if not entry.startswith(b"rockchip,"):
            continue
        candidate = entry[len(b"rockchip,"):]
        if b"-" not in candidate:
            return candidate.decode("ascii", "replace")
    return None


def _rknn_runtime_present():
    """Return whether the Rockchip userspace runtime is installed."""
    return any(os.path.exists(path) for path in (
        "/usr/lib/librknnrt.so",
        "/usr/lib/aarch64-linux-gnu/librknnrt.so",
    ))


_SUPPORTED_RKNN_CHIPS = ("rk3566", "rk3576", "rk3588")


def _awnn_runtime_present():
    """Return whether the a733 VIPLite device and v2 runtime are present."""
    if not os.path.exists("/dev/vipcore"):
        return False
    return any(os.path.exists(path) for path in (
        "/usr/local/lib/libNBGlinker.so",
        "/usr/lib/libNBGlinker.so",
    ))


def _detect_nvidia_tegra():
    """Return whether the device tree identifies NVIDIA Tegra hardware."""
    try:
        with open("/proc/device-tree/compatible", "rb") as handle:
            entries = handle.read().split(b"\x00")
    except (IOError, OSError):
        return False
    return any(entry.startswith(b"nvidia,tegra") for entry in entries)


def _vulkan_runtime_present():
    """Return whether this aarch64 process can see a Vulkan loader.

    The daemon load and warm-up handshake performs the authoritative GPU
    capability check.
    """
    if struct.calcsize("P") * 8 != 64:
        return False
    if os.uname().machine.lower() not in ("aarch64", "arm64"):
        return False
    return any(os.path.exists(path) for path in (
        "/lib/aarch64-linux-gnu/libvulkan.so.1",
        "/usr/lib/aarch64-linux-gnu/libvulkan.so.1",
        "/usr/local/lib/libvulkan.so.1",
    ))


def _resolve_backend(requested):
    """Return ``(kind, chip)`` for an automatic or forced backend.

    Forced unavailable backends raise instead of silently falling back.
    """
    requested = requested or "auto"
    if requested == "cpu":
        return "cpu", None
    if requested == "rknn":
        chip = _detect_rockchip_chip()
        if chip not in _SUPPORTED_RKNN_CHIPS or not _rknn_runtime_present():
            raise BackendUnavailable(
                "aiBackend is forced to rknn, but no Rockchip NPU runtime "
                "and supported RK3566/RK3576/RK3588 SoC were detected on "
                "this machine"
            )
        return "rknn", chip
    if requested == "awnn":
        if not _awnn_runtime_present():
            raise BackendUnavailable(
                "aiBackend is forced to awnn, but no a733 VIPLite runtime "
                "was detected on this machine"
            )
        return "awnn", None
    if requested == "vulkan":
        if not _vulkan_runtime_present():
            raise BackendUnavailable(
                "aiBackend is forced to vulkan, but no supported aarch64 "
                "Vulkan loader was detected on this machine"
            )
        return "vulkan", None
    # Treat unrecognised persisted/API values as auto so detection stays up.
    chip = _detect_rockchip_chip()
    if chip in _SUPPORTED_RKNN_CHIPS and _rknn_runtime_present():
        return "rknn", chip
    if _awnn_runtime_present():
        return "awnn", None
    if _detect_nvidia_tegra() and _vulkan_runtime_present():
        return "vulkan", None
    return "cpu", None


def _runtime_target(kind, chip):
    """Return the runtime package owning this runner and model.

    Accelerator packages also own their board's aarch64 CPU fallback, even if
    the accelerator runtime is temporarily unavailable.
    """
    if kind == "rknn":
        return "rknn%s" % (chip[2:] if chip.startswith("rk") else chip)
    if kind in ("awnn", "vulkan"):
        return kind

    detected_chip = _detect_rockchip_chip()
    if detected_chip in _SUPPORTED_RKNN_CHIPS:
        suffix = (detected_chip[2:] if detected_chip.startswith("rk")
                  else detected_chip)
        return "rknn%s" % suffix
    if os.path.exists("/dev/vipcore"):
        return "awnn"
    if _detect_nvidia_tegra():
        return "vulkan"
    return _machine_tag()


def _runtime_directories(plugin_dir, kind, chip):
    """Return native payload directories and target package name.

    Only absence of the target module permits the source-checkout fallback;
    import failures inside an installed runtime are propagated.
    """
    target = _runtime_target(kind, chip)
    module_name = _RUNTIME_MODULES.get(target)
    if module_name is None:
        raise BackendUnavailable(
            "no PiNozCam runtime package is defined for target %s" % target)
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        if getattr(exc, "name", None) not in (None, module_name):
            raise
    else:
        bin_dir = getattr(module, "BIN_DIR", None)
        model_dir = getattr(module, "MODEL_DIR", None)
        packaged_target = getattr(module, "TARGET", None)
        if packaged_target != target:
            raise BackendUnavailable(
                "runtime package %s identifies itself as %r, expected %r"
                % (module_name, packaged_target, target))
        if not bin_dir or not model_dir:
            raise BackendUnavailable(
                "runtime package %s has no BIN_DIR/MODEL_DIR metadata"
                % module_name)
        return bin_dir, model_dir, target

    # Source-checkout fallback accepts either package or repository root.
    legacy_roots = (
        plugin_dir,
        os.path.join(plugin_dir, "octoprint_pinozcam"),
    )
    for legacy_root in legacy_roots:
        bin_dir = os.path.join(legacy_root, "static", "bin")
        model_dir = os.path.join(legacy_root, "static", "models")
        if os.path.isdir(bin_dir) and os.path.isdir(model_dir):
            return bin_dir, model_dir, "legacy-in-tree"
    raise BackendUnavailable(
        "native runtime is not installed: expected Python package %s"
        % module_name)


def model_identity(path):
    """Return an opaque, low-cost model identifier for diagnostics.

    It hashes the size and edge blocks rather than the entire large model;
    structural compatibility is checked separately by the handshake.
    """
    try:
        size = os.path.getsize(path)
        digest = hashlib.sha256()
        digest.update(str(size).encode())
        with open(path, "rb") as handle:
            digest.update(handle.read(65536))
            if size > 131072:
                handle.seek(-65536, os.SEEK_END)
                digest.update(handle.read(65536))
        return "%s size=%d id=%s" % (
            os.path.basename(path), size, digest.hexdigest()[:16]
        )
    except (IOError, OSError) as exc:
        return "%s (unreadable: %s)" % (os.path.basename(path), exc)


class NozcamBackend(object):
    """Resident nozcam_daemon, restarted on demand.

    Thread safety: one lock serialises the whole request/response
    exchange. The protocol is strictly synchronous -- a second writer
    would interleave frames into the same pipe and desynchronise every
    later reply.
    """

    def __init__(self, plugin_dir, logger, model_name=None, backend=None):
        """Configure a lazily started client for the selected backend."""
        self._logger = logger
        self._lock = threading.Lock()
        self._proc = None
        self._stderr_thread = None
        # Daemon argv parameters require a restart when they change.
        self._live_score_threshold = None
        self._live_sensitivity = None
        self._live_cpus = None
        # Failures slow retries but never permanently disable monitoring.
        self._fail_count = 0
        self._retry_after = 0.0
        self._call_started = 0.0
        # Request IDs survive daemon restarts, wrap at uint32, and skip zero.
        # This prevents a late reply or legacy zero echo from matching.
        self._req_id = 0
        self._last_req_id = None
        # Instance timeouts allow platform-specific adjustment.
        self.start_timeout = START_TIMEOUT
        self.infer_timeout = INFER_TIMEOUT
        # Positive niceness yields CPU time independently of affinity.
        self.nice = 10

        self._requested_backend = backend or "auto"
        self._kind, chip = _resolve_backend(self._requested_backend)
        self._bin_dir, self._model_dir, self._runtime_target = (
            _runtime_directories(plugin_dir, self._kind, chip))
        self._select_payload(self._kind, chip, model_name)

    def _select_payload(self, kind, chip=None, model_name=None):
        """Select one runner/model pair from the owning runtime package."""
        self._kind = kind
        if kind == "rknn":
            self._daemon_path = os.path.join(
                self._bin_dir, "nozcam_daemon.rknn.aarch64")
            # Each RKNN runtime owns exactly one chip-specific model.
            self._model_path = self._pick_model(
                model_name, os.path.join(
                    self._model_dir, "nozcam-%s.rknn" % chip))
        elif kind == "awnn":
            self._daemon_path = os.path.join(
                self._bin_dir, "nozcam_daemon.awnn.aarch64")
            self._model_path = self._pick_model(
                model_name, os.path.join(self._model_dir, "nozcam-a733.nb"))
        elif kind == "vulkan":
            self._daemon_path = os.path.join(
                self._bin_dir, "nozcam_daemon.vulkan.aarch64")
            self._model_path = self._pick_model(
                model_name, os.path.join(self._model_dir, "nozcam-gpu.pte"))
        else:
            self._tag = _machine_tag()
            self._daemon_path = os.path.join(
                self._bin_dir, "nozcam_daemon.%s.static" % self._tag)
            self._model_path = self._pick_model(
                model_name, os.path.join(self._model_dir, DEFAULT_MODEL))

    def _may_fallback_to_cpu(self):
        """Return whether an auto-selected accelerator may use CPU."""
        return (self._requested_backend == "auto"
                and self._kind in ("rknn", "awnn", "vulkan"))

    def _pick_model(self, model_name, default):
        """Return the caller's model override or this backend's default."""
        if model_name:
            return os.path.join(self._model_dir, model_name)
        return default

    def preflight(self):
        """Raise BackendUnavailable unless the binary and model exist.

        Also restores the executable bit: setuptools' package_data does
        not preserve mode bits, so a pip-installed daemon arrives 0644
        and exec() fails with EACCES.
        """
        if not os.path.exists(self._daemon_path):
            raise BackendUnavailable(
                "daemon binary missing: %s" % self._daemon_path
            )
        if not os.path.exists(self._model_path):
            raise BackendUnavailable(
                "model missing: %s" % self._model_path
            )
        if not os.access(self._daemon_path, os.X_OK):
            try:
                os.chmod(self._daemon_path, 0o755)
                self._logger.info(
                    "Restored +x on %s", self._daemon_path
                )
            except OSError as exc:
                raise BackendUnavailable(
                    "cannot make %s executable: %s"
                    % (self._daemon_path, exc)
                )

    def describe(self):
        """One line naming the binary and model in use, for the log."""
        model = os.path.basename(self._model_path)
        if os.path.isdir(self._model_path):
            model = "%s/ (auto-select for this chip)" % model
        return "%s + %s" % (os.path.basename(self._daemon_path), model)

    @property
    def kind(self):
        """Return the backend this live client is currently using.

        Automatic accelerator startup can fall back to the bundled CPU runtime,
        so status must read the live client instead of guessing from the
        saved setting.
        """
        return self._kind

    def is_alive(self):
        """True while the daemon process exists and has not exited."""
        return self._proc is not None and self._proc.poll() is None

    # Demote verbose runtime probes and startup details already in handshake.
    _NOISE = ("cpuinfo_utils", "daemon ready:")

    def _drain_stderr(self, proc):
        """Continuously drain daemon stderr so its pipe cannot block."""
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if not line:
                    continue
                if any(token in line for token in self._NOISE):
                    self._logger.debug("nozcam_daemon: %s", line)
                else:
                    self._logger.info("nozcam_daemon: %s", line)
        except Exception:
            pass

    def _note_failure(self):
        """Record a failure and schedule the next allowed attempt."""
        self._fail_count += 1
        index = min(self._fail_count, len(BACKOFF_SECONDS) - 1)
        delay = BACKOFF_SECONDS[index]
        self._retry_after = time.monotonic() + delay
        return delay

    def _note_success(self):
        """Clear the failure count after a frame goes through."""
        self._fail_count = 0
        self._retry_after = 0.0

    def ensure_started(self, score_threshold, sensitivity, cpus=None):
        """Start the daemon, or restart it if its parameters changed."""
        with self._lock:
            changed = (
                score_threshold != self._live_score_threshold
                or sensitivity != self._live_sensitivity
                or cpus != self._live_cpus
            )
            if self.is_alive() and not changed:
                return
            remaining = self._retry_after - time.monotonic()
            if remaining > 0:
                raise BackendUnavailable(
                    "backend failed %d times; next attempt in %.0fs"
                    % (self._fail_count, remaining)
                )
            if self.is_alive():
                self._logger.info(
                    "Backend parameters changed, restarting daemon."
                )
            self._stop_locked()
            try:
                self._start_locked(score_threshold, sensitivity, cpus)
            except Exception as exc:
                # Public stop() would reacquire this non-reentrant lock.
                self._stop_locked(graceful=False)
                if self._may_fallback_to_cpu():
                    failed_kind = self._kind
                    failed_label = {
                        "rknn": "Rockchip NPU",
                        "awnn": "A733 NPU",
                        "vulkan": "Vulkan GPU",
                    }[failed_kind]
                    self._logger.warning(
                        "Auto-selected %s backend failed to start; "
                        "using the bundled CPU fallback for this detector "
                        "session: %s", failed_label, exc
                    )
                    self._select_payload("cpu")
                    try:
                        self._start_locked(score_threshold, sensitivity, cpus)
                    except Exception as cpu_exc:
                        self._stop_locked(graceful=False)
                        delay = self._note_failure()
                        self._logger.error(
                            "%s failed and its CPU fallback also failed "
                            "(%d in a row), retrying in %.0fs: %s",
                            failed_label, self._fail_count, delay, cpu_exc
                        )
                        raise BackendUnavailable(
                            "automatic %s start failed (%s); bundled CPU "
                            "fallback also failed (%s)"
                            % (failed_kind, exc, cpu_exc)
                        )
                    self._note_success()
                    return
                delay = self._note_failure()
                self._logger.error(
                    "Backend start failed (%d in a row), retrying in "
                    "%.0fs: %s", self._fail_count, delay, exc
                )
                raise
            self._note_success()

    def _start_locked(self, score_threshold, sensitivity, cpus):
        """Spawn and validate the daemon while the caller holds _lock."""
        self.preflight()
        # The daemon applies scheduling before runtime/thread-pool startup.
        # An empty CPU list leaves affinity unchanged.
        argv = [
            self._daemon_path,
            self._model_path,
            str(PROC_WIDTH),
            str(PROC_HEIGHT),
            str(score_threshold),
            str(sensitivity),
            ",".join(str(cpu) for cpu in sorted(cpus)) if cpus else "",
            str(self.nice or 0),
        ]
        started = time.monotonic()
        try:
            self._proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                # Do not inherit OctoPrint camera or notification sockets.
                # This is descriptor hygiene, not a network sandbox.
                close_fds=True,
                bufsize=0,
            )
        except OSError as exc:
            raise BackendUnavailable(
                "cannot start %s: %s" % (self._daemon_path, exc))
        # Non-blocking protocol fds make select-based deadlines effective;
        # stderr remains blocking for its dedicated readline drain.
        os.set_blocking(self._proc.stdin.fileno(), False)
        os.set_blocking(self._proc.stdout.fileno(), False)

        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, args=(self._proc,)
        )
        self._stderr_thread.daemon = True
        self._stderr_thread.start()

        self._live_score_threshold = score_threshold
        self._live_sensitivity = sensitivity
        self._live_cpus = cpus

        # PING follows model load; the handshake also executes a warm-up.
        if self._call_locked(_CMD_PING, timeout=self.start_timeout) != b"pong":
            raise BackendUnavailable("daemon did not answer ping")
        info, warm_ms, warm_boxes = self._handshake_locked()
        # INFO reports effective scheduling, not merely requested values.
        self._logger.info(
            "nozcam_daemon ready in %.0f ms (%s, cpus=%s, nice=%s)",
            (time.monotonic() - started) * 1000.0,
            self.describe(),
            info.get("cpus") or "all", info.get("nice"),
        )
        self._check_scheduling(info, cpus)
        # Prefer the model path the daemon says it opened.
        self._logger.info(
            "backend handshake ok: %s, warm-up %.0f ms (%d detections)",
            model_identity(info.get("model") or self._model_path),
            warm_ms, warm_boxes,
        )

    def _check_scheduling(self, info, cpus):
        """Warn when reported scheduling differs from requested policy."""
        if "cpus" not in info:
            self._logger.warning(
                "Daemon does not report its CPU mask; affinity and niceness "
                "may not have been applied."
            )
            return
        if cpus:
            want = sorted(str(cpu) for cpu in cpus)
            got = sorted(part for part in (info.get("cpus") or "").split(",")
                         if part)
            if want != got:
                self._logger.warning(
                    "Asked the daemon for CPUs %s, it reports %s.",
                    ",".join(want), info.get("cpus") or "(none)"
                )
        if info.get("nice") != (self.nice or 0):
            self._logger.warning(
                "Asked the daemon for niceness %d, it reports %r.",
                self.nice or 0, info.get("nice")
            )

    def _stop_locked(self, graceful=True):
        """Tear down the daemon while the caller holds _lock.

        Ungraceful shutdown skips protocol I/O that a wedged daemon may never
        consume.
        """
        proc, self._proc = self._proc, None
        self._live_score_threshold = None
        self._live_sensitivity = None
        self._live_cpus = None
        if proc is None:
            return
        if graceful and proc.poll() is None:
            try:
                self._write_fd(
                    proc.stdin.fileno(),
                    struct.pack("<II", _CMD_SHUTDOWN, 0),
                    time.monotonic() + 1.0,
                )
            except (IOError, OSError, ValueError):
                pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                stream.close()
            except (IOError, OSError):
                pass
        # Teardown is best-effort after clearing the only process reference.
        try:
            if proc.poll() is None:
                if graceful:
                    try:
                        proc.wait(timeout=5)
                        return
                    except subprocess.TimeoutExpired:
                        pass
                proc.kill()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._logger.error("nozcam_daemon would not die after SIGKILL")
        except Exception as exc:
            self._logger.error("Error while stopping nozcam_daemon: %s", exc)

    def stop(self, graceful=True):
        """Shut down the daemon, optionally skipping protocol shutdown."""
        with self._lock:
            self._stop_locked(graceful)

    def kill(self):
        """Kill without the request lock so shutdown remains bounded.

        Closing the pipes wakes a concurrent inference; its existing error
        path treats the process as dead.
        """
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            proc.kill()
        except Exception:
            pass
        for stream in (proc.stdin, proc.stdout):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass
        try:
            # Reap the already-signalled process.
            proc.wait(timeout=2)
        except Exception:
            pass

    def _wait_fd(self, fd, deadline, for_write):
        """Wait for fd readiness against one absolute deadline."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BackendTimeout("deadline already passed")
        if for_write:
            _, ready, _ = select.select([], [fd], [], remaining)
        else:
            ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            raise BackendTimeout(
                "daemon did not respond within %.0fs" % (
                    deadline - self._call_started)
            )

    def _write_fd(self, fd, data, deadline):
        """Write all bytes to an explicit fd against an absolute deadline."""
        view = memoryview(data)
        sent = 0
        while sent < len(view):
            self._wait_fd(fd, deadline, for_write=True)
            try:
                sent += os.write(fd, view[sent:])
            except BlockingIOError:
                continue
            except OSError as exc:
                raise IOError("write to daemon failed: %s" % exc)

    def _write_exact(self, data, deadline):
        """Write every byte, honoring short writes and the deadline."""
        self._write_fd(self._proc.stdin.fileno(), data, deadline)

    def _read_exact(self, count, deadline):
        """Read exactly count bytes, or raise before the deadline."""
        fd = self._proc.stdout.fileno()
        chunks = []
        remaining = count
        while remaining > 0:
            self._wait_fd(fd, deadline, for_write=False)
            try:
                chunk = os.read(fd, remaining)
            except BlockingIOError:
                continue
            except OSError as exc:
                raise IOError("read from daemon failed: %s" % exc)
            if not chunk:
                raise IOError("daemon closed the pipe (crashed?)")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _call_locked(self, cmd, payload=b"", timeout=None, rect=None):
        """Exchange one framed message while the caller holds _lock.

        Header, payload, and response share one absolute deadline. INFER
        payloads receive a monotonic request ID and per-frame content rect.
        """
        if cmd == _CMD_INFER:
            self._req_id = self._req_id % 0xFFFFFFFF + 1
            self._last_req_id = self._req_id
            # Exclude letterbox bars from severity's area denominator. The
            # per-frame rect also makes payload length a protocol version gate.
            if rect is None:
                rect = (0, 0, PROC_WIDTH, PROC_HEIGHT)
            payload = (struct.pack("<I", self._req_id)
                       + struct.pack("<4i", *rect) + payload)
        self._call_started = time.monotonic()
        deadline = self._call_started + (
            self.infer_timeout if timeout is None else timeout)
        self._write_exact(struct.pack("<II", cmd, len(payload)), deadline)
        if payload:
            self._write_exact(payload, deadline)
        status, length = struct.unpack("<II", self._read_exact(8, deadline))
        if length > MAX_RESPONSE:
            # A desynchronised length has no safe drain/resync point.
            raise IOError(
                "daemon announced a %d byte response (max %d); stream is "
                "desynchronised" % (length, MAX_RESPONSE)
            )
        body = self._read_exact(length, deadline) if length else b""
        if status != 0:
            raise IOError(
                "daemon error %d: %s"
                % (status, body.decode("utf-8", "replace"))
            )
        return body

    def _infer_locked(self, payload, timeout=None, rect=None):
        """Infer and validate while the caller holds the request lock."""
        body = self._call_locked(_CMD_INFER, payload, timeout=timeout,
                                 rect=rect)
        expect_id = self._last_req_id
        try:
            result = json.loads(body)
        except ValueError as exc:
            raise BackendMismatch(
                "daemon's reply is not JSON (%s): %r" % (exc, body[:120]))
        self._validate_result(result, expect_id)
        return result

    def _validate_result(self, result, expect_id=None):
        """Validate every INFER reply's request ID, shape, and ranges."""
        if expect_id is not None and result.get("req_id") != expect_id:
            # The protocol has no resynchronisation point; restart is required.
            raise BackendMismatch(
                "reply carries req_id %r, sent %d; the byte stream is "
                "desynchronised or the daemon predates the request id"
                % (result.get("req_id"), expect_id)
            )
        for key in ("scores", "boxes", "labels", "severity",
                    "percentage_area", "total_area"):
            if key not in result:
                raise BackendMismatch("result is missing '%s'" % key)
        scores, boxes, labels = (result["scores"], result["boxes"],
                                 result["labels"])
        if not (isinstance(scores, list) and isinstance(boxes, list)
                and isinstance(labels, list)):
            raise BackendMismatch("scores/boxes/labels are not all lists")
        if not len(scores) == len(boxes) == len(labels):
            raise BackendMismatch(
                "scores/boxes/labels lengths disagree: %d/%d/%d"
                % (len(scores), len(boxes), len(labels))
            )
        for box in boxes:
            if not isinstance(box, list) or len(box) != 4:
                raise BackendMismatch("a box is not four numbers: %r" % (box,))
            for value in box:
                if not math.isfinite(value):
                    raise BackendMismatch("a box holds %r" % (value,))
        for value in scores:
            if not math.isfinite(value):
                raise BackendMismatch("a score is %r" % (value,))
        severity = result["severity"]
        if not math.isfinite(severity) or not 0.0 <= severity <= 1.0:
            raise BackendMismatch("severity out of range: %r" % (severity,))
        if not math.isfinite(result["percentage_area"]):
            raise BackendMismatch(
                "percentage_area is %r" % (result["percentage_area"],))
        return len(scores)

    def _handshake_locked(self):
        """Validate INFO and run a synthetic warm-up under _lock.

        INFO verifies the fixed tensor contract; warm-up proves execution and
        pays first-run allocation before a real camera frame.
        """
        raw = self._call_locked(_CMD_INFO,
                                timeout=min(10.0, self.start_timeout))
        try:
            info = json.loads(raw)
        except ValueError as exc:
            raise BackendMismatch(
                "daemon's INFO reply is not JSON (%s): %r" % (exc, raw[:120]))
        # in_bytes describes image bytes, excluding request ID and rect.
        expect_bytes = PROC_WIDTH * PROC_HEIGHT * 3
        problems = []
        if ((info.get("proc_w"), info.get("proc_h"))
                != (PROC_WIDTH, PROC_HEIGHT)):
            problems.append(
                "daemon works at %sx%s, this code sends %dx%d"
                % (info.get("proc_w"), info.get("proc_h"),
                   PROC_WIDTH, PROC_HEIGHT)
            )
        if info.get("in_bytes") != expect_bytes:
            problems.append(
                "daemon expects %s input bytes, this code sends %d"
                % (info.get("in_bytes"), expect_bytes)
            )
        if info.get("n_outputs") != EXPECTED_OUTPUTS:
            problems.append(
                "output count %s does not match what the postprocess "
                "needs" % (info.get("n_outputs"),)
            )
        if problems:
            raise BackendMismatch("; ".join(problems))

        # A deterministic ramp exercises output decoding better than zeros.
        probe = bytes(range(256)) * (expect_bytes // 256)
        started = time.monotonic()
        result = self._infer_locked(probe, timeout=self.start_timeout)
        return (info, (time.monotonic() - started) * 1000.0,
                len(result["scores"]))

    def info(self):
        """Return daemon diagnostics, representing failures as data."""
        with self._lock:
            if not self.is_alive():
                return {}
            try:
                return json.loads(self._call_locked(
                    _CMD_INFO, timeout=min(10.0, self.start_timeout)))
            except (IOError, OSError, ValueError, struct.error) as exc:
                return {"error": str(exc)}

    @staticmethod
    def _fit(image):
        """Return a letterboxed model canvas, content rect, and turn flag.

        Portrait input is turned clockwise after OctoPrint's transforms. The
        content rect excludes padding from the daemon's severity denominator.
        """
        image = image.convert("RGB")
        turned = image.height > image.width
        if turned:
            # Pillow names clockwise 90 degrees as counter-clockwise 270.
            image = image.transpose(Image.ROTATE_270)
        scale = min(PROC_WIDTH / float(image.width),
                    PROC_HEIGHT / float(image.height))
        fit_w = max(1, int(round(image.width * scale)))
        fit_h = max(1, int(round(image.height * scale)))
        pad_x = (PROC_WIDTH - fit_w) // 2
        pad_y = (PROC_HEIGHT - fit_h) // 2
        # Match the black used for masked regions.
        canvas = Image.new("RGB", (PROC_WIDTH, PROC_HEIGHT), (0, 0, 0))
        canvas.paste(image.resize((fit_w, fit_h)), (pad_x, pad_y))
        return canvas, (pad_x, pad_y, fit_w, fit_h), turned

    @staticmethod
    def _unfit(box, rect, turned, width, height):
        """Map one canvas box back to the post-OctoPrint input image.

        Only this backend's portrait turn is undone; upstream transforms stay.
        """
        pad_x, pad_y, fit_w, fit_h = rect
        # The turn swapped the axes, so the frame _fit scaled is h x w.
        src_w, src_h = (height, width) if turned else (width, height)
        # Integer-rounded fit dimensions require independent axis scales.
        x1 = (box[0] - pad_x) * src_w / float(fit_w)
        y1 = (box[1] - pad_y) * src_h / float(fit_h)
        x2 = (box[2] - pad_x) * src_w / float(fit_w)
        y2 = (box[3] - pad_y) * src_h / float(fit_h)
        # Boxes may overlap padding, so clamp them to the source image.
        x1 = min(max(x1, 0.0), src_w)
        x2 = min(max(x2, 0.0), src_w)
        y1 = min(max(y1, 0.0), src_h)
        y2 = min(max(y2, 0.0), src_h)
        if turned:
            # Invert the clockwise mapping: (x, y) -> (src_w - y, x).
            x1, y1, x2, y2 = y1, src_w - x2, y2, src_w - x1
        # Rotation can swap endpoint order; consumers require ordered boxes.
        return [min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)]

    def infer(self, image, score_threshold, sensitivity, cpus=None):
        """Run one frame and map detections back to input-image coordinates."""
        self.ensure_started(score_threshold, sensitivity, cpus)
        image_width, image_height = image.size

        # RGB Pillow bytes are contiguous HWC uint8; C++ transposes them.
        resized, rect, turned = self._fit(image)
        payload = resized.tobytes()

        started = time.monotonic()
        try:
            # Invalid replies require the same restart path as pipe failures.
            with self._lock:
                result = self._infer_locked(payload, rect=rect)
        except (IOError, OSError, ValueError, struct.error,
                BackendMismatch) as exc:
            # stop() must run outside _lock because it acquires that lock.
            delay = self._note_failure()
            self._logger.error(
                "Backend call failed (%d in a row), next attempt in "
                "%.0fs: %s", self._fail_count, delay, exc
            )
            self.stop(graceful=False)
            raise
        self._note_success()
        elapsed_time = time.monotonic() - started

        scaled_boxes = [
            self._unfit(box, rect, turned, image_width, image_height)
            for box in result["boxes"]
        ]
        return (
            result["scores"],
            scaled_boxes,
            result["labels"],
            result["severity"],
            result["percentage_area"],
            elapsed_time,
        )
