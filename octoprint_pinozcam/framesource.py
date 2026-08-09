"""Encoded frame sources for MJPEG, HTTP snapshots, and static files.

Each source exposes the same bounded, interruptible latest-frame interface.
Multipart parsing is isolated in :mod:`mjpegstream`; decoding remains deferred
until a retained frame is consumed. Camera URLs and network exception text may
contain credentials and must never be logged.
"""

import logging
import socket
import threading
import time
from collections import namedtuple
from io import BytesIO

import requests
from PIL import Image

from . import mjpegstream


# ``identity`` is safe to log; ``url`` may contain credentials and is not.
# The geometry provider is read when retaining a frame so orientation changes
# are paired with the pixels captured under them.
SourceSpec = namedtuple(
    "SourceSpec", "kind identity url geometry_provider")


# Frames remain encoded until selected. ``captured_at`` uses monotonic time;
# ``sequence`` is meaningful only within one source instance.
EncodedFrame = namedtuple("EncodedFrame", "jpeg_bytes captured_at sequence")


class FrameSource(object):
    """Bounded, interruptible source of monotonically sequenced frames."""

    def start(self):
        """Begin acquiring frames without waiting for the first frame."""
        raise NotImplementedError

    def wait_next(self, last_sequence, stop, timeout):
        """Return a newer frame, or None on timeout, stop, or closure."""
        raise NotImplementedError

    def close(self):
        """Release resources and promptly wake blocked waiters."""
        raise NotImplementedError

    def stats(self):
        """Return received, yielded, dropped, errors, and reconnects."""
        raise NotImplementedError


# ---- shared snapshot fetch/decode ------------------------------------------

# Shared one-shot HTTP timeouts.
SNAPSHOT_CONNECT_TIMEOUT = 5.0
SNAPSHOT_READ_TIMEOUT = 15.0


def _read_bounded(chunks, max_bytes, deadline, deadline_seconds):
    """Join chunks while enforcing byte and absolute monotonic limits."""
    parts = []
    total = 0
    for chunk in chunks:
        total += len(chunk)
        if total > max_bytes:
            raise IOError(
                "snapshot passed %d MB and is still going -- this "
                "looks like a stream, not a snapshot"
                % (max_bytes // (1024 * 1024)))
        if deadline is not None and time.monotonic() > deadline:
            raise IOError(
                "snapshot still arriving after %.0f s -- this looks "
                "like a stream, not a snapshot" % deadline_seconds)
        parts.append(chunk)
    return b"".join(parts)


def _fetch_snapshot_bytes(url, max_bytes, deadline_seconds):
    """Fetch one encoded snapshot with declared and actual size bounds."""
    deadline = time.monotonic() + deadline_seconds
    response = requests.get(
        url, timeout=(SNAPSHOT_CONNECT_TIMEOUT, SNAPSHOT_READ_TIMEOUT),
        stream=True)
    try:
        response.raise_for_status()
        declared = response.headers.get("Content-Length")
        if declared is not None:
            try:
                if int(declared) > max_bytes:
                    raise IOError(
                        "camera announced %s bytes, over the %d MB "
                        "limit" % (declared, max_bytes // (1024 * 1024)))
            except ValueError:
                pass                # a junk header is not a reason to fail
        return _read_bounded(
            response.iter_content(chunk_size=64 * 1024),
            max_bytes, deadline, deadline_seconds)
    finally:
        response.close()


def _decode_source_image(raw_bytes):
    """Fully decode bytes now, rather than returning Pillow's lazy image."""
    image = Image.open(BytesIO(raw_bytes))
    image.load()
    return image


def _next_backoff(current, cap):
    """Double a retry delay without exceeding its cap."""
    return min(current * 2, cap)


# ---- MjpegFrameSource -----------------------------------------------------

MJPEG_CONNECT_TIMEOUT = 5.0
# The per-read timeout is also the bound on a stalled close operation.
MJPEG_STALL_TIMEOUT = 15.0
MJPEG_INITIAL_BACKOFF = 0.1
MJPEG_MAX_BACKOFF = 5.0
# A small chunk avoids waiting for much of the following frame before a
# boundary-less transport read releases the current part's tail.
MJPEG_CHUNK_SIZE = 1024
# Reconnect rather than accept indefinitely misaligned MIME parts.
MJPEG_MAX_CONSECUTIVE_INVALID = 5
# Condition.wait cannot also watch the caller's Event, so poll it promptly.
_STOP_POLL_INTERVAL = 0.1

_JPEG_SOI = b"\xff\xd8"


def _looks_like_jpeg(data):
    """Check the JPEG SOI marker without decoding the frame."""
    return len(data) >= 4 and data[:2] == _JPEG_SOI


class MjpegFrameSource(FrameSource):
    """Read MJPEG on one background thread into a capacity-one slot.

    Errors clear the slot and reconnect to the same URL with bounded
    exponential backoff. Source selection and fallback are handled elsewhere.
    """

    def __init__(self, spec, logger=None,
                 connect_timeout=MJPEG_CONNECT_TIMEOUT,
                 stall_timeout=MJPEG_STALL_TIMEOUT,
                 chunk_size=MJPEG_CHUNK_SIZE,
                 max_part_bytes=mjpegstream.MAX_PART_BYTES,
                 max_consecutive_invalid=MJPEG_MAX_CONSECUTIVE_INVALID,
                 initial_backoff=MJPEG_INITIAL_BACKOFF,
                 max_backoff=MJPEG_MAX_BACKOFF):
        """Build the source without connecting; start() launches it."""
        self._spec = spec
        self._logger = logger or logging.getLogger(__name__)
        self._connect_timeout = connect_timeout
        self._stall_timeout = stall_timeout
        self._chunk_size = chunk_size
        self._max_part_bytes = max_part_bytes
        self._max_consecutive_invalid = max_consecutive_invalid
        self._initial_backoff = initial_backoff
        self._max_backoff = max_backoff
        self._backoff = initial_backoff

        # Shared by the reader and all waiters.
        self._cv = threading.Condition()
        self._latest = None
        self._last_yielded_sequence = 0
        self._sequence = 0
        self._closed = False
        self._down = False
        self._counts = dict(received=0, yielded=0, dropped=0, errors=0,
                            reconnects=0)

        self._stop = threading.Event()
        self._thread = None
        # Network I/O must not block the frame condition variable.
        self._resp_lock = threading.Lock()
        self._response = None

    def start(self):
        """Atomically create and launch at most one reader thread."""
        with self._cv:
            if self._closed:
                raise ValueError(
                    "cannot start() a closed MjpegFrameSource")
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._run, name="pinozcam-mjpeg-reader",
                daemon=True)
            self._thread.start()

    def wait_next(self, last_sequence, stop, timeout):
        """Wait for a newer frame while polling the caller's stop Event."""
        deadline = time.monotonic() + timeout
        with self._cv:
            while True:
                if (self._latest is not None
                        and self._latest.sequence > last_sequence):
                    frame = self._latest
                    self._last_yielded_sequence = frame.sequence
                    self._counts["yielded"] += 1
                    return frame
                if self._closed:
                    return None
                if stop is not None and stop.is_set():
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cv.wait(min(remaining, _STOP_POLL_INTERVAL))

    def close(self):
        """Stop, interrupt the active socket, and wake all waiters."""
        with self._cv:
            if self._closed:
                return
            self._closed = True
            self._cv.notify_all()
        self._stop.set()
        with self._resp_lock:
            response = self._response
        if response is not None:
            self._interrupt_response(response)
        if self._thread is not None:
            self._thread.join(
                timeout=self._connect_timeout + self._stall_timeout)

    @staticmethod
    def _interrupt_response(response):
        """Interrupt a blocked recv without racing response.close().

        Failure is bounded by the read timeout; only the reader thread owns
        response close.
        """
        try:
            response.raw._fp.fp.raw._sock.shutdown(socket.SHUT_RDWR)
        except Exception:                   # noqa: BLE001
            pass

    def stats(self):
        """See FrameSource.stats -- exactly the five required keys."""
        with self._cv:
            return dict(self._counts)

    # ---- reader thread -----------------------------------------------

    def _run(self):
        """Reconnect after every stream end until stopped."""
        attempt = 0
        while not self._stop.is_set():
            if attempt > 0:
                with self._cv:
                    self._counts["reconnects"] += 1
            attempt += 1
            try:
                self._stream_once()
            except Exception as exc:          # noqa: BLE001
                with self._cv:
                    self._counts["errors"] += 1
                    self._clear_latest_locked()
                self._down = True
                # URL and exception text may both contain camera credentials.
                self._logger.warning(
                    "MJPEG stream error (%s), reconnecting: %s",
                    self._spec.identity, type(exc).__name__)
            if self._stop.is_set():
                return
            if self._stop.wait(self._backoff):
                return
            self._backoff = _next_backoff(self._backoff, self._max_backoff)

    def _clear_latest_locked(self):
        """Clear an unconsumed pre-error frame while preserving sequence.

        Caller holds _cv. Clean EOF deliberately retains a published frame,
        because short finite streams may publish and end before a waiter runs.
        """
        self._latest = None

    def _stream_once(self):
        """One connection attempt: connect, parse, publish, until the
        stream ends for any reason or stop() fires."""
        response = requests.get(
            self._spec.url, stream=True,
            timeout=(self._connect_timeout, self._stall_timeout))
        with self._resp_lock:
            self._response = response
        try:
            response.raise_for_status()
            boundary = mjpegstream.parse_boundary(
                response.headers.get("Content-Type", ""))
            parser = mjpegstream.MultipartParser(
                boundary, max_part_bytes=self._max_part_bytes)
            consecutive_invalid = 0
            for chunk in response.iter_content(
                    chunk_size=self._chunk_size):
                if self._stop.is_set():
                    return
                if not chunk:
                    continue
                for part in parser.feed(chunk):
                    with self._cv:
                        self._counts["received"] += 1
                    if _looks_like_jpeg(part.body):
                        consecutive_invalid = 0
                        self._backoff = self._initial_backoff
                        self._publish(part.body)
                    else:
                        consecutive_invalid += 1
                        with self._cv:
                            self._counts["errors"] += 1
                        if (consecutive_invalid
                                >= self._max_consecutive_invalid):
                            self._logger.warning(
                                "MJPEG stream (%s) gave up after %d "
                                "consecutive invalid parts",
                                self._spec.identity, consecutive_invalid)
                            return
        finally:
            response.close()
            with self._resp_lock:
                self._response = None

    def _publish(self, jpeg_bytes):
        """Publish into the capacity-one slot and count unread overwrites."""
        with self._cv:
            if self._closed:
                return
            if (self._latest is not None
                    and self._latest.sequence
                    > self._last_yielded_sequence):
                self._counts["dropped"] += 1
            self._sequence += 1
            self._latest = EncodedFrame(
                jpeg_bytes, time.monotonic(), self._sequence)
            self._cv.notify_all()
        if self._down:
            self._down = False
            self._logger.info(
                "MJPEG stream recovered: %s", self._spec.identity)


# ---- HttpSnapshotFrameSource -----------------------------------------------

# Minimum start-to-start interval; slow requests are never followed by bursts.
HTTP_SNAPSHOT_MIN_INTERVAL = 0.1
# Per-read timeouts do not bound a response that continually dribbles bytes.
HTTP_SNAPSHOT_DEADLINE = 20.0
HTTP_SNAPSHOT_INITIAL_BACKOFF = 0.1
HTTP_SNAPSHOT_MAX_BACKOFF = 5.0


class HttpSnapshotFrameSource(FrameSource):
    """Poll snapshots into a capacity-one slot without overlapping GETs.

    Failed ticks clear stale data and use bounded exponential backoff. Slow
    requests reduce the effective rate rather than creating catch-up bursts.
    """

    def __init__(self, spec, logger=None,
                 min_interval=HTTP_SNAPSHOT_MIN_INTERVAL,
                 deadline_seconds=HTTP_SNAPSHOT_DEADLINE,
                 max_bytes=mjpegstream.MAX_PART_BYTES,
                 initial_backoff=HTTP_SNAPSHOT_INITIAL_BACKOFF,
                 max_backoff=HTTP_SNAPSHOT_MAX_BACKOFF):
        """Build the source without connecting; start() launches it."""
        self._spec = spec
        self._logger = logger or logging.getLogger(__name__)
        self._min_interval = min_interval
        self._deadline_seconds = deadline_seconds
        self._max_bytes = max_bytes
        self._initial_backoff = initial_backoff
        self._max_backoff = max_backoff
        self._backoff = initial_backoff

        # Shared by the poller and all waiters.
        self._cv = threading.Condition()
        self._latest = None
        self._last_yielded_sequence = 0
        self._sequence = 0
        self._closed = False
        self._down = False
        self._counts = dict(received=0, yielded=0, dropped=0, errors=0,
                            reconnects=0)

        self._stop = threading.Event()
        self._thread = None

    def start(self):
        """Atomically create and launch at most one poller thread."""
        with self._cv:
            if self._closed:
                raise ValueError(
                    "cannot start() a closed HttpSnapshotFrameSource")
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._run, name="pinozcam-snapshot-poller",
                daemon=True)
            self._thread.start()

    def wait_next(self, last_sequence, stop, timeout):
        """Wait for a newer frame while polling the caller's stop Event."""
        deadline = time.monotonic() + timeout
        with self._cv:
            while True:
                if (self._latest is not None
                        and self._latest.sequence > last_sequence):
                    frame = self._latest
                    self._last_yielded_sequence = frame.sequence
                    self._counts["yielded"] += 1
                    return frame
                if self._closed:
                    return None
                if stop is not None and stop.is_set():
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cv.wait(min(remaining, _STOP_POLL_INTERVAL))

    def close(self):
        """Stop the poller and wake waiters within one bounded fetch."""
        with self._cv:
            if self._closed:
                return
            self._closed = True
            self._cv.notify_all()
        self._stop.set()
        if self._thread is not None:
            self._thread.join(
                timeout=SNAPSHOT_CONNECT_TIMEOUT + self._deadline_seconds
                + SNAPSHOT_READ_TIMEOUT)

    def stats(self):
        """Return source counters; reconnects is always zero."""
        with self._cv:
            return dict(self._counts)

    # ---- poller thread -------------------------------------------------

    def _run(self):
        """Run non-overlapping fetches, backing off after failures."""
        while not self._stop.is_set():
            tick_start = time.monotonic()
            if self._try_fetch_one():
                self._backoff = self._initial_backoff
                elapsed = time.monotonic() - tick_start
                remaining = self._min_interval - elapsed
                if remaining > 0 and self._stop.wait(remaining):
                    return
            else:
                if self._stop.wait(self._backoff):
                    return
                self._backoff = _next_backoff(
                    self._backoff, self._max_backoff)

    def _try_fetch_one(self):
        """Fetch, cheaply validate, and publish one encoded frame.

        Full decoding is deferred to the consumer. SOI-valid corrupt data may
        therefore retry at the normal poll rate rather than error backoff.
        """
        try:
            raw = _fetch_snapshot_bytes(
                self._spec.url, self._max_bytes, self._deadline_seconds)
        except Exception as exc:                          # noqa: BLE001
            with self._cv:
                self._counts["errors"] += 1
                self._clear_latest_locked()
            self._down = True
            # URL and exception text may both contain camera credentials.
            self._logger.warning(
                "HTTP snapshot fetch failed (%s), retrying: %s",
                self._spec.identity, type(exc).__name__)
            return False
        with self._cv:
            self._counts["received"] += 1
        if not _looks_like_jpeg(raw):
            with self._cv:
                self._counts["errors"] += 1
                self._clear_latest_locked()
            self._down = True
            self._logger.warning(
                "HTTP snapshot did not look like a JPEG (%s), "
                "retrying", self._spec.identity)
            return False
        self._publish(raw)
        return True

    def _clear_latest_locked(self):
        """Atomically clear a pre-failure frame without resetting sequence."""
        self._latest = None

    def _publish(self, jpeg_bytes):
        """Publish into the capacity-one slot and count unread overwrites."""
        with self._cv:
            if self._closed:
                return
            if (self._latest is not None
                    and self._latest.sequence
                    > self._last_yielded_sequence):
                self._counts["dropped"] += 1
            self._sequence += 1
            self._latest = EncodedFrame(
                jpeg_bytes, time.monotonic(), self._sequence)
            self._cv.notify_all()
        if self._down:
            self._down = False
            self._logger.info(
                "HTTP snapshot recovered: %s", self._spec.identity)


# ---- StaticFileFrameSource --------------------------------------------

# A static file has no native cadence; synthesize the same 10 fps ceiling.
STATIC_FILE_TICK_SECONDS = 0.1


class StaticFileFrameSource(FrameSource):
    """Cache one validated file and synthesize a logical 10 fps stream.

    Sequence and capture time derive from monotonic elapsed time, so no
    producer thread is needed. The file is read once per source instance.
    """

    def __init__(self, spec, logger=None,
                 tick_seconds=STATIC_FILE_TICK_SECONDS):
        """Build the source without reading; start() performs disk I/O."""
        self._spec = spec
        self._logger = logger or logging.getLogger(__name__)
        self._tick_seconds = tick_seconds

        # Serializes start/close and protects cached state and counters.
        # _closed is also the wake-up primitive for waiters.
        self._lock = threading.Lock()
        self._jpeg_bytes = None
        self._start_monotonic = None
        self._counts = dict(received=0, yielded=0, dropped=0, errors=0,
                            reconnects=0)
        self._closed = threading.Event()

    def start(self):
        """Read, fully validate, and cache the file once."""
        with self._lock:
            if self._closed.is_set():
                raise ValueError(
                    "cannot start() a closed StaticFileFrameSource")
            if self._jpeg_bytes is not None:
                return
            jpeg_bytes = self._read_and_validate()
            self._jpeg_bytes = jpeg_bytes
            self._start_monotonic = time.monotonic()
            self._counts["received"] = 1

    def _read_and_validate(self):
        """Read raw file bytes and verify a complete Pillow decode."""
        file_path = self._spec.url.partition("file://")[2]
        with open(file_path, "rb") as handle:
            raw = handle.read()
        _decode_source_image(raw)
        return raw

    def wait_next(self, last_sequence, stop, timeout):
        """Wait for a synthesized tick, caller stop, timeout, or close.

        The closed check and cached-state read must share _lock with close();
        monotonic time would otherwise keep synthesizing frames after closure.
        The _closed Event wakes waiters immediately; caller stop is polled.
        """
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                if self._closed.is_set():
                    return None
                jpeg_bytes = self._jpeg_bytes
                start_monotonic = self._start_monotonic
            if jpeg_bytes is not None:
                sequence = self._sequence_at(
                    time.monotonic(), start_monotonic)
                if sequence > last_sequence:
                    with self._lock:
                        self._counts["yielded"] += 1
                    return EncodedFrame(
                        jpeg_bytes,
                        self._captured_at(sequence, start_monotonic),
                        sequence)
            if stop is not None and stop.is_set():
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            sleep_for = min(remaining, _STOP_POLL_INTERVAL)
            if jpeg_bytes is not None:
                sleep_for = min(sleep_for, self._time_until_next_tick(
                    start_monotonic))
            if self._closed.wait(max(sleep_for, 0.0)):
                return None

    def close(self):
        """Serialize with start() and wake all waiters."""
        with self._lock:
            if self._closed.is_set():
                return
            self._closed.set()

    def stats(self):
        """Return counters; only received and yielded can be nonzero."""
        with self._lock:
            return dict(self._counts)

    # ---- sequence/timing arithmetic ------------------------------------

    def _sequence_at(self, now, start_monotonic):
        """Return the due sequence, with sequence 1 immediately available."""
        elapsed = now - start_monotonic
        if elapsed < 0:
            # Defensive: monotonic clocks should not move backward.
            elapsed = 0.0
        return int(elapsed // self._tick_seconds) + 1

    def _captured_at(self, sequence, start_monotonic):
        """Return the nominal tick time, independent of scheduling jitter."""
        return start_monotonic + (sequence - 1) * self._tick_seconds

    def _time_until_next_tick(self, start_monotonic):
        """Return seconds until the next synthesized sequence is due."""
        now = time.monotonic()
        current = self._sequence_at(now, start_monotonic)
        next_due = self._captured_at(current + 1, start_monotonic)
        return max(0.0, next_due - now)
