"""Thread-safe storage for analysis and live-view frames."""

import threading
import time
from collections import deque

# Keep enough superseded frames to complete in-flight browser fetches.
HISTORY = 2
JPEG_CACHE = 3
# A camera frame older than this is not worth serving as "live".
CAMERA_FRESH = 2.0


class FrameStore(object):
    """Store UI frames while serialising state and JPEG-cache updates."""

    def __init__(self):
        """Create empty frame slots, history and encode caches."""
        self._lock = threading.Lock()
        # Shared with camera.py; always acquire before a state lock.
        self.encode_lock = threading.Lock()
        self._analysis = None
        self._history = deque(maxlen=HISTORY)
        self._jpeg = {}
        self._camera = None
        self._seq = 0

    # ---- publishing ----------------------------------------------------

    def publish_analysis(self, frame, when, boxes, severity=0.0,
                         alarming=False):
        """Record the frame the detector just judged. Returns its id.

        The frame it replaces stays servable: /check has advertised that
        id to every open tab and the fetch for it may still be in flight.
        """
        with self._lock:
            self._seq += 1
            if self._analysis is not None:
                self._history.append((self._analysis['frame_id'],
                                      self._analysis['frame']))
            self._analysis = {
                'frame': frame,
                'frame_id': self._seq,
                'time': when,
                'boxes': boxes,
                'severity': severity,
                # The alert flash follows the criterion, not box colour.
                'alarming': alarming,
            }
            return self._seq

    def publish_camera(self, image, when):
        """Record the sampler's most recent frame."""
        with self._lock:
            self._camera = (image, when)

    # ---- reading -------------------------------------------------------

    def analysis(self, hold):
        """Return the current analysis if it is younger than hold."""
        with self._lock:
            latest = self._analysis
        if latest is None:
            return None
        return latest if _fresh(latest['time'], hold) else None

    def frame_for(self, etag):
        """Return the exact frame for an analysis ETag, or None."""
        with self._lock:
            latest = self._analysis
            if latest is not None and etag == "a%d" % latest['frame_id']:
                return latest['frame']
            for fid, held in self._history:
                if etag == "a%d" % fid:
                    return held
        return None

    def camera(self, max_age=CAMERA_FRESH):
        """The sampler's frame if it is fresh enough, else None."""
        with self._lock:
            held = self._camera
        if held is None:
            return None
        return held[0] if _fresh(held[1], max_age) else None

    def camera_raw(self):
        """The sampler's frame and its timestamp, unjudged, or None."""
        with self._lock:
            return self._camera

    # ---- the JPEG cache -------------------------------------------------

    def jpeg(self, etag, frame, encode):
        """The encoded frame for `etag`, encoding it once if need be."""
        with self._lock:
            cached = self._jpeg.get(etag)
        if cached is not None:
            return cached
        # Encode outside the state lock and re-check after serialisation.
        with self.encode_lock:
            with self._lock:
                cached = self._jpeg.get(etag)
            if cached is not None:
                return cached
            data = encode(frame)
            with self._lock:
                self._jpeg[etag] = data
                while len(self._jpeg) > JPEG_CACHE:
                    self._jpeg.pop(next(iter(self._jpeg)))
        return data

    # ---- invalidation ---------------------------------------------------

    def invalidate(self):
        """Atomically discard all frames and encodings, preserving the ID."""
        with self._lock:
            self._analysis = None
            self._history.clear()
            self._jpeg = {}
            self._camera = None


def _fresh(when, limit):
    """True while `when` is within `limit` seconds of now."""
    return time.monotonic() - when <= limit
