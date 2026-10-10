"""A bounded buffer for candidate frames between capture and AI.

The sampler writes candidates here, and the detector takes from it. This
keeps capture and inference loosely coupled and avoids dropping every frame
while inference is running.
"""

import threading
import time
from collections import namedtuple

# Unmeasured frames carry no useful sharpness signal yet still preserve
# continuity while the buffer is shallow.
UNMEASURED = float("-inf")

# Measure from this depth so startup frames are inserted quickly without
# expensive scoring when scoring cannot change which frame is selected.
MEASURE_FROM_DEPTH = 3
# How many candidates are retained for selection and resilience through
# transient blur episodes.
DEFAULT_CAPACITY = 5
# Keep age and latency bounded. Also controls the maximum backward jump
# in scene timing from score-only selection.
DEFAULT_MAX_AGE = 4.0

Candidate = namedtuple("Candidate", "image score captured_at")


class FrameBuffer:
    """Thread-safe candidate cache with bounded depth and age.

    Rules that protect correctness:
    - Expire stale candidates before scoring/selection decisions.
    - Evict the lowest score candidate when over capacity.
    - Select by highest score and return/remove the chosen candidate.
    - Gate live-view publishing on put() acceptance so stale epochs are
      never shown.

    One producer, any number of consumers.
    """

    def __init__(self, capacity=DEFAULT_CAPACITY, max_age=DEFAULT_MAX_AGE,
                 measure_from_depth=MEASURE_FROM_DEPTH):
        """Build an empty buffer.

        capacity is how many candidates are held, max_age how long one stays
        eligible, and measure_from_depth the queue depth below which sharpness
        is not scored at all -- see the class docstring for why each exists.
        """
        self._cv = threading.Condition()
        self._items = []
        self._capacity = max(1, int(capacity))
        self._max_age = float(max_age)
        self._measure_from_depth = int(measure_from_depth)
        self._closed = False
        # Bump when a pending grab can no longer be shown or used:
        # camera settings, masking or print state changed while it was
        # in flight.
        self._epoch = 0
        self._counts = dict(produced=0, measured=0, taken=0, expired=0,
                            evicted=0, starved=0, stale_epoch=0)

    @property
    def capacity(self):
        """How many candidates are kept. Constant for the object's life."""
        return self._capacity

    @property
    def measure_from_depth(self):
        """Depth at which scoring starts paying for itself."""
        return self._measure_from_depth

    @property
    def epoch(self):
        """Read before a grab; hand back to put() when it lands."""
        with self._cv:
            return self._epoch

    def bump_epoch(self):
        """Discard everything held, and everything already in flight.

        Returns the new epoch. Call this instead of clear() whenever the
        meaning of a frame changes rather than merely its age -- a
        settings save, an Undetect Zone realignment, a new print.
        """
        with self._cv:
            self._epoch += 1
            self._items = []
            return self._epoch

    def put(self, image, score, captured_at, epoch=None):
        """Add a candidate. Never blocks; drops the worst when full.

        Returns True if the frame was ACCEPTED, False if it was refused
        (closed, or captured under an epoch that has since moved).
        `captured_at` is monotonic and must be the time the GRAB began,
        not the time of this call: a grab and its optional measurement
        take 40-170 ms depending on the board, and the age that matters
        is the age of the picture.

        `epoch` is what the caller read before starting that grab. If it
        has moved since, the frame belongs to a configuration that no
        longer applies and is dropped. Passing None skips the check,
        which is for tests and for callers that are the only writer.
        """
        with self._cv:
            if self._closed:
                return False
            if epoch is not None and epoch != self._epoch:
                self._counts["stale_epoch"] += 1
                return False
            # Expire stale candidates before eviction and insertion decisions.
            self._expire(time.monotonic())
            self._items.append(Candidate(image, score, captured_at))
            self._counts["produced"] += 1
            if score != UNMEASURED:
                self._counts["measured"] += 1
            while len(self._items) > self._capacity:
                # Keep only the best scored candidates; this drops stale
                # high-score candidates if they expired earlier.
                worst = min(self._items,
                            key=lambda c: (c.score, c.captured_at))
                self._items.remove(worst)
                self._counts["evicted"] += 1
            self._cv.notify()
            return True

    def take(self, timeout=0.0):
        """Best-scoring candidate, or None. Removes what it returns.

        Selection is score-based; captured_at can go backwards across takes.
        Expiry and max_age bound that reordering.

        `timeout` waits for a candidate; the detection loop passes
        CONSUMER_WAIT. An empty buffer means "the next frame is on its way",
        never "fetch one yourself" -- the sampler is the only camera reader.
        put() notifies, so the wait normally ends within one sampling
        interval. A closed buffer returns None at once, which is how a
        consumer learns the sampler is gone.
        """
        deadline = time.monotonic() + timeout
        with self._cv:
            while True:
                now = time.monotonic()
                self._expire(now)
                if self._items:
                    # Ties break to the newest, which is what decides
                    # between two unmeasured frames: both score -inf, so
                    # recency is the only thing left to separate them.
                    best = max(self._items,
                               key=lambda c: (c.score, c.captured_at))
                    self._items.remove(best)
                    self._counts["taken"] += 1
                    return best
                if self._closed:
                    return None
                remaining = deadline - now
                if remaining <= 0:
                    self._counts["starved"] += 1
                    return None
                self._cv.wait(remaining)

    def _expire(self, now):
        """Drop candidates too old to analyse. Call with the lock held."""
        if not self._items:
            return
        cutoff = now - self._max_age
        fresh = [c for c in self._items if c.captured_at >= cutoff]
        self._counts["expired"] += len(self._items) - len(fresh)
        self._items = fresh

    def should_measure(self):
        """Whether scoring the next frame can change which one is taken.

        False while fewer than measure_from_depth candidates are waiting:
        with nothing to compare against, the score costs real time and
        changes nothing. This is what makes the sampler nearly free on
        fast hardware.
        """
        with self._cv:
            self._expire(time.monotonic())
            return len(self._items) >= self._measure_from_depth

    def depth(self):
        """How many candidates could still be handed out.

        Expires first, for the same reason should_measure() does: a
        depth that counts frames take() would discard is not a depth.
        """
        with self._cv:
            self._expire(time.monotonic())
            return len(self._items)

    def clear(self):
        """Drop every candidate and release the images."""
        with self._cv:
            self._items = []

    def close(self):
        """Refuse further puts and wake every waiting consumer."""
        with self._cv:
            self._closed = True
            self._items = []
            self._cv.notify_all()

    def reopen(self):
        """Accept puts again and reset the counters, for the next print."""
        with self._cv:
            self._closed = False
            self._items = []
            self._counts = dict.fromkeys(self._counts, 0)

    def stats(self):
        """Counters since the last reopen(), for measuring whether this
        buffer is earning its keep. `taken` against `produced` says how
        much of the sampling was wasted; `starved` counts the cycles that
        fell back to grabbing a frame directly."""
        with self._cv:
            snapshot = dict(self._counts)
            snapshot["depth"] = len(self._items)
            return snapshot
