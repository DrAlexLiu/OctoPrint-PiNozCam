"""Detection loop and sustained-failure criterion.

The detection thread owns scheduling and process handoff.
The criterion owns the alarm signal: a sustained fraction of alarming
frames in the rolling window drives both notification and action.
"""

import heapq
import threading
import time
from . import cpu_affinity
from . import framesource
from .camera import CameraSourceFactory
from .framebuffer import UNMEASURED, FrameBuffer
from .nozcam_backend import BackendUnavailable, NozcamBackend

# How many alarming frames are retained as recent evidence.
EVIDENCE_KEEP = 3
# Require minimum samples and time before any decision is trusted.
INIT_MIN_FRAMES = 20
INIT_MIN_SECONDS = 30.0
# Rate-limit repeated sampler warnings.
SAMPLER_WARN_EVERY = 30.0
# Cap each source wait so stop/pause is noticed quickly.
SAMPLER_SOURCE_WAIT = 1.0
# Camera watch interval before emitting a blind-camera notice.
CAMERA_OFFLINE_AFTER = 30.0
# Wait time for a candidate before treating a cycle as frameless.
CONSUMER_WAIT = 1.0
# Retry window for delivery attempts; printer action is decided separately.
NOTIFY_ATTEMPTS = 3
# Rebuild attempts for a dead sampler before disabling detection.
SAMPLER_RESTARTS = 3


class DetectMixin:
    """Mixed into PinozcamPlugin."""

    # Which sampler generation owns the camera. A class attribute so the
    # finally in _sample_frames can read it whatever constructed the object;
    # PinozcamPlugin.__init__ sets an instance value over it.
    sampler_token = None
    # Same reasoning, for the same reason: a class attribute so
    # _sample_frames finds a defined value even before instance init.
    # PinozcamPlugin.__init__ sets an instance value over it.
    _camera_source_generation = 0

    def _prune_and_count(self):
        """Drop expired entries and recompute the failure count.

        The count is derived from the window on every prune to avoid stale
        state drift. Caller must hold self.lock.
        """
        cutoff = time.monotonic() - self.count_time
        while self.ai_results and self.ai_results[0]['time'] < cutoff:
            self.ai_results.popleft()
        self.count = sum(1 for entry in self.ai_results if entry['alarming'])
        # Evidence is a tiny bounded heap; rebuild it after filtering.
        self.evidence = [
            item for item in self.evidence if item[2]['time'] >= cutoff
        ]
        heapq.heapify(self.evidence)
        return self.count

    def _armed(self, now):
        """True once there is enough evidence to judge anything.

        Both bounds must hold. Frames make the ratio statistically
        meaningful; seconds stop a fast board acting on twenty frames
        gathered inside nine seconds, which could all be one transient.
        """
        if self.detect_started_at is None:
            return False
        return (self.detect_frames >= INIT_MIN_FRAMES
                and now - self.detect_started_at >= INIT_MIN_SECONDS)

    def _decide(self, now):
        """One criterion, one threshold. Caller holds lock.

        Returns (threshold_met, ratio) using the configured ratio and
        window state.
        """
        ratio = 0.0
        if self.ai_results:
            ratio = self.count / float(len(self.ai_results))
        self.last_ratio = ratio
        if not self._armed(now):
            return False, ratio
        return ratio >= self.failure_ratio, ratio

    def _action_escalates(self, threshold_met, decided_action):
        """True when this frame is a new escalation worth acting on.

        Escalation is decided by configured action level and existing
        episode level so notification-only frames do not block real actions.
        """
        return (threshold_met
                and decided_action > 0
                and decided_action > self.episode_action_level)

    def _push_evidence(self, entry):
        """Retain this alarming frame and evict oldest evidence entry if full.

        The sequence number stabilizes tie-breaking when timestamps match.
        Caller must hold self.lock.
        """
        self.evidence_seq += 1
        heapq.heappush(
            self.evidence, (entry['time'], self.evidence_seq, entry))
        while len(self.evidence) > EVIDENCE_KEEP:
            heapq.heappop(self.evidence)

    def _wait(self, seconds):
        """Interruptible delay. Returns False when asked to stop.

        Every wait in the detection loop goes through here and remains
        responsive to stop_event.
        """
        if seconds > 0:
            self.stop_event.wait(seconds)
        return self.ai_running and not self.stop_event.is_set()

    def _camera_watch(self, ok, now):
        """Edge-triggered camera-outage detector for the chat channels.

        Returns "offline" once per outage (after CAMERA_OFFLINE_AFTER
        seconds without a frame), "recovered" once when frames return after
        an alert, None otherwise.

        Only the detection loop calls this, so it fires only during a print.
        """
        if ok:
            self.camera_ok_at = now
            if self.camera_alerted:
                self.camera_alerted = False
                return "recovered"
            return None
        if (not self.camera_alerted
                and now - self.camera_ok_at > CAMERA_OFFLINE_AFTER):
            self.camera_alerted = True
            return "offline"
        return None

    def _notify_camera(self, text):
        """One operational notice to every configured medium.

        Mute is respected. Used for camera-offline events and is outside
        the normal notification budget.
        """
        self._logger.warning("%s", text)
        self.notify_all(text, buttons=True, wait=False)

    @staticmethod
    def _sampler_wait(stop, seconds):
        """Wait on a sampler's PRIVATE stop; True means keep going.

        Same sense as _wait(), deliberately not the same signal. See
        _sample_frames for why a sampler must never read stop_event.
        """
        if seconds > 0:
            stop.wait(seconds)
        return not stop.is_set()

    def _build_camera_source(self):
        """One freshly constructed, started FrameSource, for whichever
        of _sample_frames' three reasons to call this: sampler-
        generation start, a pause/resume cycle, or a mismatch between
        _camera_source_generation and the value the previously held
        source was built under -- see that method's own docstring for
        when each applies. May run more than once in a single print.

        A separate method, rather than calling CameraSourceFactory
        directly inline in _sample_frames, purely so a test can
        override it with a scripted fake source.

        Raises whatever CameraSourceFactory.create() or the source's
        own start() raises: CameraSourceUnavailable when nothing
        usable is configured, or e.g. an IOError from
        StaticFileFrameSource.start() reading a missing file.
        _sample_frames catches broadly and retries, so a camera problem
        must never end sampling.

        Close the new source on start failure so half-built instances
        do not leak.
        """
        source = CameraSourceFactory(self, logger=self._logger).create()
        try:
            source.start()
        except Exception:                            # noqa: BLE001
            try:
                source.close()
            except Exception as exc:                 # noqa: BLE001
                self._logger.warning(
                    "Camera source failed to close after its own "
                    "start() also failed: %s", exc)
            raise
        return source

    def _decode_encoded_frame(self, frame, warn_state):
        """Decode and geometry-transform one EncodedFrame, or None.

        Geometry is read fresh here, once per retained frame, matching
        _prepare_source_image's own "reads geometry once" contract
        (camera.py) -- never baked into the frame itself, so a live
        flip/rotate change is picked up on the very next frame this
        returns, with no sampler restart needed.

        Any failure -- a JPEG that will not fully decode, or anything
        _prepare_source_image itself raises -- is logged (rate
        limited) and treated as a dropped tick: the sampler must keep
        running past one bad frame.
        """
        try:
            image = framesource._decode_source_image(frame.jpeg_bytes)
            return self._prepare_source_image(
                image, self._snapshot_geometry_now())
        except Exception as exc:                        # noqa: BLE001
            self._note_sampler_failure(warn_state, exc)
            return None

    def _put_and_publish(self, buf, image, score, captured_at, epoch):
        """put() one candidate and, only on acceptance, publish it.

        Gating the publish on put()'s own return is what stops a
        frame refused for a stale epoch (or a closed buffer) from
        reaching the live view: it predates a settings/camera change
        and would be shown under a mask that has since moved.
        """
        accepted = buf.put(image, score, captured_at, epoch)
        if accepted:
            self.frames.publish_camera(image, captured_at)
        return accepted

    def _note_sampler_failure(self, warn_state, exc):
        """Log one dropped candidate, rate-limited.

        Same throttle this class has always used: the usual cause can
        fail on every tick, and unthrottled that is several lines a
        second.
        """
        warn_state["count"] += 1
        now = time.monotonic()
        if now - warn_state["at"] > SAMPLER_WARN_EVERY:
            self._logger.warning(
                "Frame sampler could not use a frame (%d since the "
                "last message): %s", warn_state["count"], exc)
            warn_state["at"] = now
            warn_state["count"] = 0

    def _sample_mjpeg_tick(self, buf, frame, last_accepted_at,
                           warn_state):
        """Handle one MJPEG push and update last_accepted_at on acceptance.

        `frame` stays compressed unless THIS tick decides to decode
        it: the whole point of the state machine is that a deep
        buffer inside the interval can refuse a frame for the cost of
        one comparison, never a JPEG decode.
        """
        epoch = buf.epoch                    # read before anything else
        now = time.monotonic()
        if not buf.should_measure():
            # Shallow: keep it unconditionally, ignoring the interval,
            # and never score it -- an unmeasured frame is only ever
            # taken when nothing better is waiting.
            image = self._decode_encoded_frame(frame, warn_state)
            if image is None:
                return last_accepted_at
            score = UNMEASURED
        elif now - last_accepted_at < self.frame_sample_interval:
            # Deep enough to rank, but the interval has not elapsed:
            # discard the still-compressed bytes untouched -- no
            # decode, no sharpness, no put.
            return last_accepted_at
        else:
            image = self._decode_encoded_frame(frame, warn_state)
            if image is None:
                return last_accepted_at
            # Re-checked AFTER the decode: the buffer's depth may
            # have drained while this frame was being prepared (a
            # concurrent take(), or ordinary expiry) -- scoring a
            # frame that has already fallen back below the threshold
            # would cost time and decide nothing.
            if buf.should_measure():
                try:
                    score = self.measure_sharpness(image)
                except Exception as exc:                # noqa: BLE001
                    self._note_sampler_failure(warn_state, exc)
                    return last_accepted_at
            else:
                score = UNMEASURED
        if self._put_and_publish(buf, image, score, frame.captured_at,
                                 epoch):
            last_accepted_at = time.monotonic()
        return last_accepted_at

    def _sample_simple_tick(self, buf, frame, warn_state):
        """One HTTP-snapshot/static-file candidate.

        Both sources already self-pace to their own fixed ~10 fps, so
        every wait_next() return is decoded, scored if the buffer is
        deep enough to care, and offered to the buffer -- no interval
        logic of this method's own; that all lives inside
        HttpSnapshotFrameSource/StaticFileFrameSource already.
        """
        epoch = buf.epoch
        image = self._decode_encoded_frame(frame, warn_state)
        if image is None:
            return
        score = UNMEASURED
        if buf.should_measure():
            try:
                score = self.measure_sharpness(image)
            except Exception as exc:                    # noqa: BLE001
                self._note_sampler_failure(warn_state, exc)
                return
        self._put_and_publish(buf, image, score, frame.captured_at, epoch)

    def _sample_frames(self, buf, stop, token=None):
        """Producer: keep `buf` topped up with candidates until `stop`.

        Three tiers of thread, and this method is the middle one --
        worth naming up front, since the rest of this docstring is
        about the isolation BETWEEN them:

          * DETECTION thread (process_ai_image, one level up): owns
            one print's whole generation. Builds `buf`/`stop`/
            `token` as its own locals and starts this method on a
            fresh thread to run them; also runs the stop sequence
            (sampler_stop.set() -> buf.close() -> a BOUNDED join)
            that can return before this thread actually has.
          * SAMPLER thread (this method): owns the depth/interval
            state machine below, turning whatever the FrameSource
            hands it into candidates for `buf`.
          * FrameSource PRODUCER thread (framesource.py): for
            MjpegFrameSource/HttpSnapshotFrameSource, owns the actual
            network read on ITS OWN background thread, behind
            wait_next(). StaticFileFrameSource is the exception and
            has no producer thread at all -- its own docstring
            explains why -- so for a `file://` source (what
            test-board camera fakes use) wait_next() does inline
            wall-clock arithmetic on THIS (the sampler) thread
            instead.

        `buf` and `stop` are PER DETECTION THREAD and passed in, never read
        off self. `token` identifies this generation for camera ownership.

        FrameSource instances are per-generation local objects in this
        method. This method owns only a generation-local buffer,
        stop event and identity token, so stale generations cannot share
        or mutate current generation state.

        Pacing is no longer this method's own job for
        HttpSnapshotFrameSource/StaticFileFrameSource, which already
        self-pace to a fixed ~10 fps internally: every wait_next()
        return from either is decoded and scored. For MjpegFrameSource,
        which pushes as fast as the camera does, pacing IS the
        depth/interval state machine in _sample_mjpeg_tick: a deep
        buffer inside frame_sample_interval refuses a pushed frame's
        still-COMPRESSED bytes for the cost of one comparison, never a
        JPEG decode.
        """
        buf.reopen()
        self._logger.info(
            "Frame sampler started: keeping %d, scoring only from depth "
            "%d; %.0f ms MJPEG ranking interval (HTTP/file sources "
            "self-pace and ignore it).",
            buf.capacity, buf.measure_from_depth,
            self.frame_sample_interval * 1000)
        # sampler_active marks which generation currently owns the camera.
        self.sampler_active = True
        source = None
        # Which _camera_source_generation value `source` was built
        # under, or None while `source` itself is None. Compared each
        # tick below; a mismatch means customSnapshotURL changed since
        # this source was built.
        source_generation = None
        source_is_mjpeg = False
        last_sequence = 0
        last_accepted_at = 0.0
        source_warned_at = 0.0
        decode_warnings = {"count": 0, "at": 0.0}
        try:
            while not stop.is_set():
                interval = self.frame_sample_interval
                # Read once per tick, same as `interval` above: cheap,
                # no lock, eventually consistent -- this codebase's
                # established idiom for a signal shared between the
                # settings-save thread and this one (frame_buffer.epoch,
                # sampler_token, settings_generation all follow it too).
                generation = self._camera_source_generation
                if interval <= 0 or not self.enable_AI:
                    # Paused with the AI. Stay alive but idle and drop
                    # what is held, so it can be turned back on mid-print
                    # without a restart and without the detector then
                    # taking a frame from before the gap. (The interval
                    # cannot reach 0 through settings any more -- this
                    # thread is the detector's only frame source -- but
                    # the guard stays for a hand-set value.)
                    buf.clear()
                    if source is not None:
                        try:
                            source.close()
                        except Exception as exc:        # noqa: BLE001
                            self._logger.warning(
                                "Frame sampler's camera source failed "
                                "to close cleanly while pausing: %s",
                                exc)
                        source = None
                        source_generation = None
                    if not self._sampler_wait(stop, 1.0):
                        break
                    continue

                # Rebuild source when URL generation changed so this print
                # immediately follows new settings.
                if source is not None and source_generation != generation:
                    try:
                        source.close()
                    except Exception as exc:            # noqa: BLE001
                        self._logger.warning(
                            "Frame sampler's camera source failed to "
                            "close cleanly after a camera setting "
                            "changed: %s", exc)
                    source = None
                    source_generation = None

                if source is None:
                    source_generation = generation
                    try:
                        source = self._build_camera_source()
                    except Exception as exc:            # noqa: BLE001
                        # Camera setup failures must not end sampling;
                        # retry with bounded pacing.
                        now = time.monotonic()
                        if now - source_warned_at > SAMPLER_WARN_EVERY:
                            self._logger.warning(
                                "Frame sampler has no camera source: "
                                "%s", exc)
                            source_warned_at = now
                        if not self._sampler_wait(stop, 1.0):
                            break
                        continue
                    source_is_mjpeg = isinstance(
                        source, framesource.MjpegFrameSource)
                    last_sequence = 0
                    last_accepted_at = 0.0

                frame = source.wait_next(
                    last_sequence, stop, SAMPLER_SOURCE_WAIT)
                if frame is None:
                    continue      # timeout, stop, or a closed source
                last_sequence = frame.sequence
                # wait_next() can return after control changed; use a
                # generation recheck before processing this frame.
                if stop.is_set():
                    continue
                if source_generation != self._camera_source_generation:
                    continue
                if source_is_mjpeg:
                    last_accepted_at = self._sample_mjpeg_tick(
                        buf, frame, last_accepted_at, decode_warnings)
                else:
                    self._sample_simple_tick(buf, frame, decode_warnings)
        finally:
            # In finally: always release resources, and only the owner
            # may clear active camera ownership.
            if self.sampler_token is token:
                self.sampler_active = False
                self.sampler_token = None
            else:
                self._logger.info(
                    "A superseded frame sampler exited; leaving camera "
                    "ownership with the current one.")
            buf.close()
            self._logger.info("Frame sampler exiting: %s", buf.stats())
            # Close source last and defensively, so ownership is released
            # even if close() blocks or raises.
            if source is not None:
                try:
                    source.close()
                except Exception as exc:                # noqa: BLE001
                    self._logger.warning(
                        "Frame sampler's camera source failed to "
                        "close cleanly: %s", exc)

    def process_ai_image(self):
        """Run the detector behind one thread-level exception boundary."""
        try:
            self._process_ai_image_impl()
        except Exception as exc:                           # noqa: BLE001
            self.ai_running = False
            self.backend_error = "Detection thread stopped: {}".format(self.redact(
                str(exc)))
            self._logger.exception("Detection thread stopped unexpectedly: %s",
                                   exc)
        finally:
            # These operations are idempotent and cover unexpected exits.
            sampler_stop = self._active_sampler_stop
            if sampler_stop is not None:
                sampler_stop.set()
            try:
                self.frame_buffer.close()
            except Exception as exc:                      # noqa: BLE001
                self._logger.warning(
                    "Could not close the frame buffer after detection: %s",
                    exc)
            self.frames.invalidate()
            try:
                self._shutdown_backend()
            except Exception as exc:                      # noqa: BLE001
                self._logger.exception(
                    "Could not stop the inference backend after detection: %s",
                    exc)
            self.ai_running = False
            if self._active_sampler_stop is sampler_stop:
                self._active_sampler_stop = None

    def _process_ai_image_impl(self):
        """Detection loop: one frame every detection_interval seconds.

        Runs in its own thread and ends when stop_ai_thread() sets
        stop_event, which every wait in here honours.

        The fixed interval keeps sample cadence stable across hardware and
        prevents runaway CPU usage.
        """
        try:
            self.backend = NozcamBackend(self.plugin_dir, self._logger,
                                         backend=self.ai_backend)
            self.backend.preflight()
            self.backend_kind = getattr(
                self.backend, "kind", getattr(self, "backend_kind", None))
            self.backend_error = None
            self._logger.info(
                "Inference backend: %s", self.backend.describe()
            )
        except BackendUnavailable as exc:
            self.backend_error = str(exc)
            self._logger.error("Inference backend unavailable: %s", exc)
            self.ai_running = False
            return

        if self.ai_start_delay > 0:
            self._logger.info(
                "Waiting %ds before starting AI processing",
                self.ai_start_delay,
            )
        if not self._wait(self.ai_start_delay):
            self._logger.info(
                "Detection thread stopped during the start delay."
            )
            self._shutdown_backend()
            return

        # The camera watch starts its grace period NOW: a print that
        # begins with a dead camera deserves the same notice as one
        # whose camera dies mid-print.
        self.camera_ok_at = time.monotonic()
        self.camera_alerted = False

        # Started only here, below every early return above, so no exit
        # path before this point has a thread to clean up.
        #
        # A fresh buffer and stop Event per detection thread, held as LOCALS
        # and handed over: that is what stops a sampler still blocked in a
        # camera read from rejoining the next print. self.frame_buffer points
        # at the current generation for the rest of the plugin; the sampler
        # never reads it.
        sampler_stop = threading.Event()
        self._active_sampler_stop = sampler_stop
        # Snapshot the retention policy for this detection generation.
        # Settings saved mid-print take effect on the next run; a bounded
        # sampler restart must rebuild the same generation rather than
        # silently switching policy half way through it.
        buffer_capacity = self.frame_buffer_capacity
        buffer_max_age = self.frame_buffer_max_age
        buf = FrameBuffer(
            capacity=buffer_capacity, max_age=buffer_max_age)
        self.frame_buffer = buf
        # Tokenized sampler ownership prevents old generations from
        # re-taking the camera after replacement.
        sampler_token = object()
        self.sampler_token = sampler_token
        sampler = threading.Thread(target=self._sample_frames,
                                   args=(buf, sampler_stop, sampler_token),
                                   name="pinozcam-sample")
        sampler.daemon = True
        sampler.start()
        sampler_restarts = 0

        while self.ai_running and not self.stop_event.is_set():
            cycle_started = time.monotonic()

            if not self.enable_AI:
                if not self._wait(1):
                    break
                continue

            # Sampler death is full sampling blackout; restart is bounded
            # and only runs after the previous sampler thread has exited.
            if not sampler.is_alive() and not sampler_stop.is_set():
                if sampler_restarts < SAMPLER_RESTARTS:
                    sampler_restarts += 1
                    self._logger.error(
                        "Frame sampler died; restarting it (attempt %d of "
                        "%d).", sampler_restarts, SAMPLER_RESTARTS)
                    buf = FrameBuffer(
                        capacity=buffer_capacity, max_age=buffer_max_age)
                    self.frame_buffer = buf
                    sampler_token = object()
                    self.sampler_token = sampler_token
                    sampler = threading.Thread(
                        target=self._sample_frames,
                        args=(buf, sampler_stop, sampler_token),
                        name="pinozcam-sample")
                    sampler.daemon = True
                    sampler.start()
                else:
                    # Out of attempts: say so where the UI can see it and
                    # stop pretending to watch the print.
                    self.backend_error = (
                        "The frame sampler stopped and could not be "
                        "restarted, so no camera frames are reaching the "
                        "detector. Check octoprint.log.")
                    self._logger.error(
                        "Frame sampler died %d times; giving up and "
                        "stopping detection.", sampler_restarts)
                    self._notify_camera(
                        "⚠️ PiNozCam has stopped: the frame sampler failed "
                        "repeatedly, so print failure detection is OFF.")
                    self.ai_running = False
                    break

            try:
                self._process_one_frame(buf)
            except BackendUnavailable as exc:
                # Expected while the backend is in restart backoff. Not a
                # traceback-worthy event, and explicitly NOT a reason to
                # stop the loop or touch the print: a monitor that gave up
                # looks exactly like one that sees nothing wrong.
                self.backend_error = str(exc)
                self._logger.warning("Detection skipped: %s", exc)
            except Exception as exc:
                # One bad frame must never end the print monitor.
                self._logger.error(
                    "Detection cycle failed: %s", exc, exc_info=True
                )

            elapsed = time.monotonic() - cycle_started
            # Log only when detection_interval is configured.
            if self.detection_interval and elapsed > self.detection_interval:
                self._logger.info(
                    "Cycle took %.1fs, longer than the %ds detection "
                    "interval; running as fast as the hardware allows.",
                    elapsed, self.detection_interval,
                )
            if not self._wait(self.detection_interval - elapsed):
                break

        # Both before the join: the Event ends the sampler's loop and
        # close() wakes anything blocked in take(). The join stays bounded
        # -- a tick can be 20 s inside a camera read -- but a sampler left
        # behind is now harmless, because the buffer and the Event it
        # holds belong to this thread and to nothing that comes after.
        sampler_stop.set()
        buf.close()
        sampler.join(timeout=2)
        # Release the frames this print was holding for the live view --
        # two full-resolution PIL images (~5.5 MB at 720p) that would
        # otherwise sit referenced until the NEXT print replaces them.
        # The tab falls back to the camera path, which is what "the print
        # ended" should look like anyway.
        self.frames.invalidate()
        self._logger.info("Detection thread exiting.")
        self._shutdown_backend()

    def reset_measurement(self):
        """Restart the ratio window, the warm-up and the evidence.

        One measurement: the window, its counters, the alarm edges and the
        retained evidence frames were all gathered under one set of
        conditions -- detection parameters AND the camera producing the
        frames. Whichever of those changes, statistics from before it are
        not comparable with statistics from after it, so the whole
        measurement restarts. Shared by the settings-save path and the
        camera-change path below.
        """
        with self.lock:
            self.detect_started_at = None
            self.detect_frames = 0
            self.ai_results.clear()
            self.count = 0
            self.last_ratio = None
            self.criterion_met = self.notify_met = False
            self.episode_action_level = 0
            self.notify_attempts = 0
            self.evidence = []
            # In-flight alerts belonged to the measurement being discarded.
            # Dropping the ids retires them: a receipt that lands afterwards
            # finds no entry and changes nothing. The messages themselves
            # still go out -- the user asked for them and they describe
            # something that really happened.
            self.alerts_inflight.clear()

    def _process_one_frame(self, buf):
        """Grab, mask, infer, annotate and act on a single frame.

        `buf` is this detection thread's own candidate buffer, passed in
        for the same reason the sampler's is: nothing here should be able
        to reach a previous generation's.
        """
        # Reset all state when camera identity or geometry changes.
        if self.camera_changed:
            self.camera_changed = False
            self.reset_measurement()
            self.frames.invalidate()
            self._logger.warning(
                "Camera identity or aspect changed; the ratio window, "
                "warm-up and evidence were reset. Statistics start over "
                "on the new camera's frames.")
        with self.lock:
            self._prune_and_count()

        # Wait for a candidate from the buffer; do not initiate camera
        # reads here.
        entered = time.monotonic()
        candidate = buf.take(timeout=CONSUMER_WAIT)
        now_grab = time.monotonic()
        if candidate is None:
            # Frameless cycle: report it, never fetch. The camera watch
            # turns 30 s of these into ONE chat notice.
            if self._camera_watch(False, now_grab) == "offline":
                self._notify_camera(
                    f"⚠️ Camera lost: no frame for {int(CAMERA_OFFLINE_AFTER)} "
                    "s. Print failure detection is BLIND until the camera "
                    "returns.")
            # A CLOSED buffer returns None without waiting -- the
            # sampler closes it as it dies -- so make the empty cycle
            # cost the full tick either way: a dead sampler must poll
            # at 1 Hz, not spin the loop.
            self._wait(max(0.0, CONSUMER_WAIT - (now_grab - entered)))
            return
        unmasked = candidate.image
        frame_age = now_grab - candidate.captured_at
        if self._camera_watch(True, now_grab) == "recovered":
            self._notify_camera("⚠️ Camera is back. Print failure detection "
                                "has resumed.")

        ai_input_image = self.apply_mask_to_image(unmasked)
        image_width, image_height = ai_input_image.size

        scores, boxes, labels, severity, percentage_area, elapsed_time = \
            self.backend.infer(
                ai_input_image,
                self.scores_threshold,
                self.img_sensitivity,
                self.ai_cpus,
            )
        # Infer can return after stop was requested; drop stale results if
        # the run is no longer active before publishing or acting.
        if self.stop_event.is_set():
            self._logger.info(
                "Discarding a verdict that finished after the run was "
                "stopped (%.3fs of inference).", elapsed_time)
            return
        self.last_elapsed_time = elapsed_time
        self.backend_error = None
        now = time.monotonic()
        if self.detect_started_at is None:
            self.detect_started_at = now
        self.detect_frames += 1
        # This frame is alarming when detected area exceeds the threshold.
        alarming = percentage_area > self.img_sensitivity
        self._logger.info(
            "scores=%s severity=%.4f area=%.4f thr=%.4f alarming=%s "
            "ratio=%s age=%.2fs elapsed=%.3fs%s",
            [f"{s:.3f}" for s in scores], severity, percentage_area,
            self.img_sensitivity, alarming,
            "n/a" if self.last_ratio is None else f"{self.last_ratio:.2f}",
            frame_age,
            elapsed_time,
            "" if self._armed(now) else " WARMUP",
        )

        # Publish detections as data only; annotation is done on notify edge.
        self.frames.publish_analysis(
            ai_input_image, now,
            boxes=[
                    [box[0] / float(image_width),
                     box[1] / float(image_height),
                     box[2] / float(image_width),
                     box[3] / float(image_height),
                     score]
                    for box, score in self.boxes_to_draw(scores, boxes)
                ],
            severity=severity, alarming=alarming)

        with self.lock:
            if self.setting_change_while_printing:
                # This frame straddles a settings change: it was captured under
                # the old parameters and annotated under the new ones.
                # Dropping it is cheaper than reasoning about which half
                # is which.
                self.setting_change_while_printing = False
                return
            self.ai_results.append({
                'time': now,
                'alarming': alarming,
                'severity': severity,
                'percentage_area': percentage_area,
                'elapsed_time': elapsed_time,
            })
            self._prune_and_count()
            threshold_met, ratio = self._decide(now)
            failure_count = self.count
            decided_generation = self.settings_generation
            decided_action = self.action
        # The RUN this verdict belongs to. Read outside self.lock, under the
        # gate that stop_ai_thread retires it with, so the value cannot be
        # mid-change. Compared again at the moment of acting.
        with self.action_gate:
            decided_run = self.run_generation

        if not threshold_met:
            if self.criterion_met or self.notify_met:
                self._logger.info("criterion cleared (ratio %.2f)", ratio)
            self.criterion_met = False
            self.notify_met = False
            self.episode_action_level = 0
            # The episode is over, so its retry allowance goes with it: a
            # scene that settles and later fails again gets a fresh one.
            self.notify_attempts = 0
            return

        # Three edges derived from the same threshold:
        #  - new_episode: first threshold crossing in this episode.
        #  - new_notify: no alert has been sent yet this episode.
        #  - new_act: this cycle requires a stronger printer action.
        new_episode = threshold_met and not self.criterion_met
        new_notify = threshold_met and not self.notify_met
        new_act = self._action_escalates(threshold_met, decided_action)
        if not new_episode and not new_notify and not new_act:
            return                      # already reported this episode

        self._logger.info(
            "Failure criterion met: ratio %.2f / threshold %.2f, %d of "
            "%d frames", ratio, self.failure_ratio, failure_count,
            len(self.ai_results),
        )

        # The printer action is the safety boundary. Evidence drawing,
        # JPEG encoding and printer-status formatting are useful alert
        # material, but none may prevent an already-valid Pause or Stop.
        if self.stop_event.is_set():
            self._logger.info(
                "Run stopped after this frame was judged; not touching "
                "the printer or sending its alert.")
            return

        action_failed = None
        if self._action_escalates(threshold_met, decided_action):
            if decided_generation != self.settings_generation:
                self._logger.info(
                    "Settings changed while this frame was being judged; "
                    "not acting on it. The next frame decides under the new "
                    "parameters.")
            else:
                did = self.ACTION_NAMES.get(decided_action)
                acted = False
                with self.action_gate:
                    if self.run_generation != decided_run:
                        self._logger.info(
                            "Run was stopped while this frame was being "
                            "judged; not touching the printer.")
                    else:
                        try:
                            self.perform_action(decided_action)
                        except Exception as exc:             # noqa: BLE001
                            action_failed = (
                                "PiNozCam could not perform the requested "
                                "printer action. Check the printer connection "
                                "and octoprint.log.")
                            self._logger.exception(
                                "Requested printer action %s failed: %s",
                                decided_action, exc)
                        else:
                            self.episode_action_level = decided_action
                            acted = True
                if acted and not new_notify and did:
                    self.notify_all(
                        f"⚠️ {did}. The failure is now sustained over "
                        f"{ratio * 100:.0f}% of the last "
                        f"{int(self.count_time)}s, past the "
                        f"{self.failure_ratio * 100:.0f}% action threshold.",
                        buttons=True, respect_confirm=True, wait=False)
                elif action_failed and not new_notify:
                    self.notify_all(
                        f"⚠️ {action_failed}",
                        buttons=True, respect_confirm=True, wait=False)

        # Drawn and encoded HERE, on the notification edge, not per frame.
        # Copy-on-write is used because the underlying image is shared
        # with /frame.jpg live view rendering.
        annotated = self.draw_response_data(
            scores, boxes, labels, severity, ai_input_image.copy())
        encoded_result = self.encode_image_to_base64(annotated)
        # Push evidence once per episode even if retry/act edges repeat.
        if new_episode:
            with self.lock:
                # The trigger is the sustained ratio, not this frame's own
                # severity -- record the window state too, or a clean
                # frame that merely happened to be current when an old
                # frame aged out of the window reads as unexplained.
                self._push_evidence({
                    'time': now,
                    'image': encoded_result,
                    'severity': severity,
                    'percentage_area': percentage_area,
                    'ratio': ratio,
                    'failure_count': failure_count,
                    'window_frames': len(self.ai_results),
                })
            # Set criterion_met only after evidence is prepared so
            # a transient exception does not suppress future retries.
            self.criterion_met = True

        # Notify image is always the frame that triggered this alert.
        notify_image = annotated

        # Notifications are bounded by per-episode state to include
        # in-flight alerts.
        with self.lock:
            sending = (self.notifications_sent + len(self.alerts_inflight)
                       + (1 if new_notify else 0))
            if (new_notify and not self.notification_reach_to_max
                    and self.max_notification != 0
                    and sending > self.max_notification):
                self.notification_reach_to_max = True
                self._logger.info(
                    "Notification limit reached (%d of %d); staying quiet "
                    "until the print ends.", sending, self.max_notification)

        title, state, progress, nozzle_temp, bed_temp, file_metadata = \
            self.get_printer_status()
        status_message = (
            f"Printer: {title}\nStatus: {state}\nProgress: {progress}\n"
            f"Nozzle Temp: {nozzle_temp}°C\nBed Temp: {bed_temp}°C"
        )
        if file_metadata:
            status_message += f"\nFile: {file_metadata.get('name', 'Unknown')}"
        if action_failed:
            status_message += f"\n⚠️ {action_failed}"

        caption = (
            f"{status_message}\n"
            f"Failure Area: {percentage_area:.2f}\n"
            f"Severity: {severity * 100:.2f}%\n"
            f"Sustained: {ratio * 100:.0f}% of the last "
            f"{int(self.count_time)}s ({failure_count} of "
            f"{len(self.ai_results)} frames)\n"
        )

        # Notifying is separate from act; only one alert per episode is
        # emitted and notify_met is set before dispatch.
        alert_id = None
        with self.lock:
            suppressed = (new_notify
                          and self.notify_interval > 0
                          and self.last_notify_at is not None
                          and now - self.last_notify_at
                          < self.notify_interval)
            if suppressed:
                self._logger.info(
                    "Alert suppressed: %.0fs since the last one, minimum is "
                    "%ds. The printer action, if any, still ran.",
                    now - self.last_notify_at, self.notify_interval)
            will_send = (new_notify and not suppressed
                         and not self.notification_reach_to_max)
            if will_send:
                self.alert_seq += 1
                alert_id = self.alert_seq
                self.alerts_inflight[alert_id] = self.run_generation
            self.notify_met = True

        if will_send:
            try:
                attempted, delivered = self.notify_all(
                    caption, image=notify_image, buttons=True,
                    respect_confirm=True, wait=False,
                    on_settled=lambda any_sent, aid=alert_id:
                        self._alert_settled(any_sent, aid))
            except Exception as exc:                       # noqa: BLE001
                self._logger.error(
                    "Alert dispatch failed before it could be queued: %s",
                    self.redact(str(exc)))
                self._alert_settled(False, alert_id)
                return
            if attempted and not delivered:
                # Nothing was even accepted, so no receipt will ever come
                # for this id. Settle it here rather than leaking it into
                # alerts_inflight, where it would inflate the quota
                # projection for the rest of the print.
                self._alert_settled(False, alert_id)
            elif not attempted:
                # Every medium was filtered out (none configured, muted, or
                # a confirmation pending). Same reasoning: no receipt.
                self._alert_settled(True, alert_id, silent=True)

    def _alert_settled(self, any_sent, alert_id, silent=False):
        """The receipt for one alert: every medium has sent or given up.

        Runs on whichever notification worker finished last -- or inline,
        when nothing was queued -- so every field it touches is under
        self.lock, the same lock the decision above used.

        Identity is checked so stale receipts cannot affect a later run.

        `silent` covers "there was nothing to send", which settles the id
        without claiming anything reached anyone.
        """
        with self.lock:
            generation = self.alerts_inflight.pop(alert_id, None)
            if generation is None:
                self._logger.info(
                    "Ignoring a receipt for alert %s: already settled.",
                    alert_id)
                return
            if generation != self.run_generation:
                self._logger.info(
                    "Ignoring a receipt for alert %s from an earlier run "
                    "(%s, now %s).", alert_id, generation,
                    self.run_generation)
                return
            if silent:
                return
            if any_sent:
                self.notify_attempts = 0
                self.notifications_sent += 1
                # The interval runs from the moment delivery SETTLED, which
                # is the earliest moment "an alert reached someone" is true.
                self.last_notify_at = time.monotonic()
                return
            self.notify_attempts += 1
            if self.notify_attempts < NOTIFY_ATTEMPTS:
                # Re-arm the episode: the next frame that still meets the
                # criterion sends a fresh alert (with the then-current
                # picture, which is better than re-posting this one).
                self.notify_met = False
                self._logger.warning(
                    "Alert was queued but no channel delivered it (attempt "
                    "%d of %d); the episode is re-armed and the next "
                    "alarming frame will retry.",
                    self.notify_attempts, NOTIFY_ATTEMPTS)
                return
            self._logger.error(
                "Alert was queued but no channel delivered it after %d "
                "attempts; giving up on messaging this episode. The "
                "printer action, if any, has already been performed.",
                self.notify_attempts)

    def _thread_calculation(self):
        """Select the CPU affinity mask shared by every inference backend.

        ExecuTorch ignores every thread-count knob -- its XNNPACK pool is
        not driven by torch.set_num_threads() or OMP_NUM_THREADS
        (measured: 327-347% CPU while asking for 2 threads). CPU affinity
        is the only lever that works, so the "CPU Cores Used" setting
        is translated into a cpuset rather than a thread count. RKNN, AWNN
        and Vulkan use the same mask for their CPU-side input, runtime and
        output work even though their main model executes on an accelerator.
        """
        raw = cpu_affinity.read_cpu_topology()
        topology = cpu_affinity.detect_cpu_topology(raw)
        selection = cpu_affinity.select_ai_cpus(
            self.cpu_speed_control, topology)

        self.cpu_topology = topology
        self.ai_cpus = selection.cpus
        self.num_threads = len(selection.cpus)

        def values_for(metric):
            """Format the available per-CPU values for one topology metric."""
            values = []
            for cpu_id in sorted(topology.allowed):
                value = (raw.get("cpus", {}).get(cpu_id) or {}).get(metric)
                if value is not None:
                    values.append(f"{cpu_id}={value}")
            return ",".join(values) or "unavailable"

        self._logger.info(
            "CPU topology: allowed=%s; capacity=%s; max_freq=%s; "
            "performance pool=%s; method=%s; %s",
            ",".join(str(cpu) for cpu in sorted(topology.allowed)),
            values_for("capacity"), values_for("max_freq"),
            ",".join(str(cpu) for cpu in topology.performance_pool),
            topology.method, selection.description,
        )

    # What perform_action() actually did, for the message that reports it.
    # 0 is absent on purpose: "notify only" performs nothing, so there is
    # nothing to announce.
    ACTION_NAMES = {1: "Print paused", 2: "Print stopped"}

    def perform_action(self, action=None):
        """Pause (1), cancel (2) or do nothing (0). Returns nothing.

        `action` is the value the caller DECIDED on; it defaults to the live
        setting for callers that have no decision to carry. The detection
        path passes its own, so a settings save landing between the
        generation check and this call cannot change what runs.
        """
        action = self.action if action is None else action
        if action == 1:
            self._logger.info("Pausing print...")
            self._printer.pause_print()
            self._logger.info("Print paused.")
        elif action == 2:
            self._logger.info("Stopping print...")
            self._printer.cancel_print()
            self._logger.info("Print stopped.")
        else:
            self._logger.info("No interference with the printing process.")

    def start_ai_thread(self):
        """Start the detection thread, waiting out any previous one.

        Runs on its own short-lived starter thread to avoid blocking
        OctoPrint event handlers.
        """
        with self.thread_lock:
            self.want_ai_running = True
            self.stop_event.set()      # ask any previous worker to stop
            if self.ai_starter_active:
                # One starter is enough. It re-reads want_ai_running on
                # every pass, so it will act on this decision -- and it can
                # only still be marked active if it has not yet reached the
                # critical section where it gives up, so it WILL see this.
                return
            self.ai_starter_active = True
            self.ai_starter_token += 1
            token = self.ai_starter_token
            self.ai_starter = threading.Thread(
                target=self._start_ai_thread_blocking, args=(token,),
                name="pinozcam-start")
            self.ai_starter.daemon = True
            self.ai_starter.start()

    def _release_starter_locked(self, token):
        """Mark no starter active. **Call with thread_lock already held.**

        Guarded by token so a starter that is finishing cannot clear the flag
        belonging to a NEWER starter that replaced it.
        """
        if self.ai_starter_token == token:
            self.ai_starter_active = False

    def _start_ai_thread_blocking(self, token):
        """Wait out the previous detection thread, then start a new one.

        Runs on its own thread; only one starter may run at once.
        The pipe protocol is strictly
        synchronous, so two interleaving frames desynchronise every later
        reply. Starting is conditional on the intent still being "running"
        and the plugin not shutting down, both read fresh under thread_lock.

        `token` stops a finishing starter from clearing a NEWER one's flag.
        """
        deadline = time.monotonic() + 60
        try:
            while True:
                with self.thread_lock:
                    if not self.want_ai_running:
                        self._logger.info(
                            "Detection start superseded before it happened; "
                            "not starting.")
                        self._release_starter_locked(token)
                        return
                    if self.shutting_down.is_set():
                        self._logger.info(
                            "Shutting down; not starting a detection "
                            "thread.")
                        self._release_starter_locked(token)
                        return
                    previous = self.ai_thread
                    if previous is None or not previous.is_alive():
                        self.stop_event.clear()
                        self.ai_running = True
                        self.ai_thread = threading.Thread(
                            target=self.process_ai_image,
                            name="pinozcam-detect")
                        self.ai_thread.daemon = True
                        self.ai_thread.start()
                        self._release_starter_locked(token)
                        return
                    if time.monotonic() > deadline:
                        self._logger.error(
                            "Previous detection thread has not stopped "
                            "after 60s; giving up on starting a new one. "
                            "Detection is OFF for this print.")
                        self._release_starter_locked(token)
                        return
                    self.stop_event.set()
                # Join outside thread_lock to avoid deadlock.
                previous.join(timeout=1)
        finally:
            # Only reached with the flag still set if this thread raised --
            # every ordinary exit already released it inside its own critical
            # section. Token-guarded so an exception here cannot clear a
            # newer starter's flag.
            with self.thread_lock:
                self._release_starter_locked(token)

    def stop_ai_thread(self):
        """Signal the detection thread to stop, without blocking.

        Clearing want_ai_running is what makes this authoritative: a starter
        currently waiting for the previous worker re-reads it and gives up,
        instead of starting a worker for a print that has already ended.

        Run generation is retired under the action gate so stop and action
        cannot overlap.
        """
        with self.thread_lock:
            self.want_ai_running = False
        self.ai_running = False
        with self.action_gate:
            self.run_generation += 1
        self.stop_event.set()

    def _shutdown_backend(self):
        """Release the inference daemon held by the detection thread."""
        backend, self.backend = self.backend, None
        if backend is not None:
            self.backend_kind = getattr(
                backend, "kind", getattr(self, "backend_kind", None))
            backend.stop()
