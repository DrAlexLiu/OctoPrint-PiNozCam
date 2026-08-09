"""HTTP endpoints, and who is allowed to reach them.

Every route here is behind a dedicated permission resolved AT REQUEST
TIME. A decorator cannot do it: Permissions.PLUGIN_PINOZCAM_CONTROL is a
lazy placeholder while the module is imported, so a decorator evaluated
then raised AttributeError on every call.
"""

import json
import multiprocessing
import time

import flask
from flask import Response
import octoprint.plugin
import octoprint.access
from octoprint.access.permissions import Permissions

from . import camera
from .mask import MASK_GRID
from .nozcam_backend import NozcamBackend


class ApiMixin(object):
    """Mixed into PinozcamPlugin; see the module docstring."""

    def _affinity_core_summary(self):
        """Describe the live affinity mask in terms the UI can display.

        ``totalCores`` remains the machine total for API compatibility.
        ``poolCores`` is the denominator the percentage setting actually
        uses: all allowed CPUs on a homogeneous board, performance CPUs on
        a heterogeneous one.  The topology attribute is created during
        detector setup, so startup/status paths must retain a safe fallback.
        """
        topology = getattr(self, "cpu_topology", None)
        if topology is None:
            pool_cores = multiprocessing.cpu_count()
            heterogeneous = False
        else:
            pool_cores = len(topology.performance_pool)
            heterogeneous = topology.is_heterogeneous
        return {
            "selected": getattr(self, "num_threads", 0),
            "pool": pool_cores,
            "total": multiprocessing.cpu_count(),
            "heterogeneous": heterogeneous,
        }

    def _stream_info(self):
        """The SNAPSHOT webcam's own stream, if the browser could use it.

        Feeds the tab's Live Camera toggle -- a direct browser-to-stream
        connection costs this plugin nothing. None hides the toggle, and
        None is the answer whenever the stream could show something other
        than what the detector sees:

        * a customSnapshotURL is set, so the detector watches a DIFFERENT
          camera than OctoPrint streams;
        * The stream must come from get_snapshot_webcam(), never from
          "the first webcam with a stream" -- with several cameras those
          are different devices, and a live view of a camera the AI cannot
          see is worse than none;
        * the snapshot webcam has no stream URL;
        * rotate90 is set. Frames are rotated server-side; doing the same
          to a stream in CSS needs rotated-box layout or the image
          overflows the tab.

        The flip flags ride along: the raw stream is untransformed, so only
        it needs them applied in CSS.
        """
        if self.custom_snapshot_url:
            return None
        try:
            import octoprint.webcams
            provided = octoprint.webcams.get_snapshot_webcam()
        except Exception:
            return None
        config = getattr(provided, "config", None)
        compat = getattr(config, "compat", None)
        stream = getattr(compat, "stream", None) if compat else None
        if not stream or getattr(config, "rotate90", False):
            return None
        # Match against snapshot_source_name so the live stream always comes
        # from the same webcam used for detection.
        if self.snapshot_source_name != getattr(config, "name", None):
            return None
        stream = str(stream)
        # <img> speaks MJPEG and nothing else. OctoPrint's stream URL can
        # also be HLS or WebRTC (its own UI switches players on these
        # same markers); offering those would render a broken image.
        # Shared with CameraSourceFactory (camera.py), which needs the
        # identical check for its own, unrelated reason -- so the two
        # can never disagree about what counts as either.
        if camera._is_hls_or_webrtc_stream_url(stream):
            return None
        return {
            "url": stream,
            "flipH": bool(getattr(config, "flipH", False)),
            "flipV": bool(getattr(config, "flipV", False)),
        }

    def _fresh_analysis(self):
        """The latest analysis while it is still the current picture.

        The hold window is what /check always used: the detector runs
        every detection_interval seconds, so a fixed 5 s would blank the
        result for the rest of a 10 s cycle.
        """
        return self.frames.analysis(max(5, self.detection_interval))

    def _current_frame_jpeg(self, want=None):
        """(etag, jpeg_bytes) for the current frame, or (None, None).

        `want` is the fid the browser asked for. For ANALYSIS frames it is
        honoured EXACTLY: the boxes came from the /check reply that named
        that fid, so serving a different frame would let one picture wear
        another's boxes -- a real race, the analysis moves every ~440 ms on
        a Pi 5. (None, None) makes the route 404 and the browser keeps its
        last consistent picture. Camera fids are not matched; they carry no
        boxes and roll once a second by design.

        Encoding is single-flight on cache misses and uses a single lock order
        to avoid duplicate work and lock inversions.

        The frame object is never mutated -- the notification path draws on
        a copy -- so encoding from a local reference is safe against the
        detector replacing latest_analysis mid-encode.
        """
        analysis = self._fresh_analysis()
        if analysis is None:
            if want is not None and want.startswith("a"):
                return None, None
            return self._cached_masked_snapshot()
        etag = "a%d" % analysis['frame_id']
        frame = analysis['frame']
        if want is not None and want.startswith("a") and want != etag:
            # Serve only still-current IDs to keep boxes aligned to the same
            # frame; stale IDs remain 404 and are not remapped.
            frame = self.frames.frame_for(want)
            if frame is None:
                return None, None
            etag = want
        return etag, self.frames.jpeg(etag, frame,
                                      self.encode_image_to_jpeg_bytes)

    def check_response(self):
        """
        Construct a JSON response describing the AI processing status.

        Returns:
        - Flask.Response: JSON response containing the frame metadata and
          additional status information. The picture itself is exposed via
          /frame.jpg (binary, ETag-cached). The browser fetches it only when
          frameId changes and draws the boxes locally.
        """
        analysis = self._fresh_analysis()
        if analysis is not None:
            frame_kind = "analysis"
            frame_id = "a%d" % analysis['frame_id']
            frame_boxes = analysis['boxes']
            frame_severity = round(analysis['severity'], 3)
            # The criterion's own per-frame judgement -- what the live
            # view's alert flash keys on. NOT the box colour: severity
            # saturates at 1.0 on most scenes, so a colour-based flash
            # never ended.
            frame_alarming = bool(analysis.get('alarming'))
        else:
            # The camera fallback re-encodes at most once a second, so
            # its id only needs to move at the same cadence. int() of the
            # monotonic clock does exactly that, and /frame.jpg's own
            # ETag is what guarantees correctness.
            frame_kind = "camera"
            frame_id = "c%d" % int(time.monotonic())
            frame_boxes = []
            frame_severity = None
            frame_alarming = False
        with self.lock:
            failure_count = self.count
            # Newest first, without the JPEGs -- the list is polled once a
            # second and three base64 frames would be ~120 KB per poll.
            # The image is fetched separately, only when one is opened.
            evidence = [
                {'id': item[1],
                 'age': round(time.monotonic() - item[2]['time'], 1),
                 'severity': round(item[2]['severity'], 3),
                 'area': round(item[2].get('percentage_area', 0.0), 4)}
                for item in sorted(self.evidence, key=lambda i: -i[2]['time'])
            ]
        thread = self.ai_thread
        ai_on = bool(
            self.enable_AI and self.ai_running
            and thread is not None and thread.is_alive()
        )
        affinity = self._affinity_core_summary()
        backend = self.backend
        backend_kind = (getattr(backend, "kind", None)
                        if backend is not None else None)
        if backend_kind is None:
            backend_kind = getattr(self, "backend_kind", None)
        response_data = {
            # What the live view should be showing. frameId is a cheap
            # change detector: the browser refetches /frame.jpg only when
            # it moves. Boxes are normalised to 0..1, already filtered by
            # the same display rule the annotated view used
            # (boxes_to_draw), and only present for kind "analysis" --
            # boxes from one frame must never be drawn over another.
            "frameKind": frame_kind,
            "frameId": frame_id,
            "boxes": frame_boxes,
            "severity": frame_severity,
            "alarming": frame_alarming,
            # null hides the Live Camera toggle. Cheap enough to resolve
            # per poll: attribute reads plus one walk over the webcam
            # registry.
            "stream": self._stream_info(),
            "failureCount": failure_count,
            "aiStatus": "ON" if ai_on else "OFF",
            "telegramStatus": "ON" if self.telegram_server_running else "OFF",
            "aiState": self._ai_state(ai_on),
            "telegramState": self._telegram_state(),
            "discordStatus": self._discord_status(),
            "cpuTemperature": int(self.get_cpu_temperature()),
            "detectionInterval": self.detection_interval,
            "failureRatio": None if self.last_ratio is None
            else round(self.last_ratio, 3),
            "failureRatioThreshold": self.failure_ratio,
            "windowFrames": len(self.ai_results),
            "evidence": evidence,
            "armed": self._armed(time.monotonic()),
            "cores": affinity["selected"],
            "totalCores": affinity["total"],
            "affinityPoolCores": affinity["pool"],
            "heterogeneousCpu": affinity["heterogeneous"],
            "inferenceMs": None if self.last_elapsed_time is None
            else int(self.last_elapsed_time * 1000),
            "backendKind": backend_kind,
            "backendError": self.backend_error,
            # Same reasoning as backendError: a mask that no longer
            # matches the frame is only actionable if the person looking
            # at the tab is told about it.
            "maskWarning": self.mask_warning,
            # The camera outage the chat channels are told about, for
            # people with neither channel configured -- the AI state
            # chip alone kept saying "watching" over a dead camera.
            # Gated on ai_on: between prints nothing watches the
            # camera, so a stale flag from a print that ENDED offline
            # must not read as a live outage.
            "cameraState": ("offline" if (ai_on and self.camera_alerted)
                            else "ok"),
        }
        return Response(json.dumps(response_data), mimetype="application/json")

    @octoprint.plugin.BlueprintPlugin.route("/check", methods=["GET"])
    def check(self):
        """AI status and current-frame metadata; the picture itself is
        /frame.jpg. Polled once a second by every open tab."""
        return self.check_response()

    @octoprint.plugin.BlueprintPlugin.route("/frame.jpg", methods=["GET"])
    def frame_jpg(self):
        """The picture /check describes, as plain JPEG bytes.

        Not behind _require_control(), along with /check:
        it took over the `image` field /check always shipped to any
        logged-in user, so tightening it would blank the live view for
        exactly the people who could see it yesterday. The blueprint's
        default protection still applies.

        Caching: the ETag names the frame. An analysis ?fid= that is no
        longer current gets a 404, never a silently different frame --
        those boxes are already in the browser's hands. no-cache means
        "revalidate", so a second tab on the same frame gets a 304.
        set_etag/if_none_match rather than raw headers, so the quoting is
        RFC form and a client's `If-None-Match: "a17"` matches.
        """
        etag, jpeg = self._current_frame_jpeg(flask.request.args.get("fid"))
        if etag is None:
            return Response(status=404)
        if flask.request.if_none_match.contains(etag):
            response = Response(status=304)
        else:
            response = Response(jpeg, mimetype="image/jpeg")
        response.set_etag(etag)
        response.headers["Cache-Control"] = "no-cache"
        return response

    @octoprint.plugin.BlueprintPlugin.route("/evidence/<int:eid>",
                                            methods=["GET"])
    def evidence_image(self, eid):
        """One retained annotated frame, by id.

        Served on demand rather than inlined into /check: three base64
        JPEGs are ~120 KB, and /check is polled once a second by every
        open tab.
        """
        denied = self._require_control()
        if denied is not None:
            return denied
        with self.lock:
            entry = next(
                (item[2] for item in self.evidence if item[1] == eid), None)
        if entry is None:
            return Response(
                json.dumps({"error": "no such evidence frame"}),
                status=404, mimetype="application/json")
        return Response(
            json.dumps({"image": entry['image'],
                        "age": round(time.monotonic() - entry['time'], 1),
                        "severity": entry['severity']}),
            mimetype="application/json")

    def _ai_state(self, ai_on):
        """Which of the five AI situations this is.

        disabled  -- Enable AI Detection is unticked
        watching  -- running and past the warm-up
        warming   -- running, still building the baseline
        starting  -- asked to run, worker not up yet; the AI Start Delay lives
                     here and it can be minutes
        idle      -- enabled, but nothing is printing
        """
        if not self.enable_AI:
            return "disabled"
        if ai_on:
            try:
                return "watching" if self._armed(time.monotonic()) else (
                    "warming")
            except Exception:                                 # noqa: BLE001
                return "watching"
        if getattr(self, "want_ai_running", False):
            return "starting"
        return "idle"

    def _telegram_state(self):
        """unconfigured / starting / on / muted / failed.

        "failed" means Telegram credentials exist but are not connected.
        """
        if not self.enable_telegram:
            return "unconfigured"
        if not (self.telegram_bot_token and self.telegram_chat_id):
            return "unconfigured"
        if self.telegram_error:
            return "failed"
        if not self.telegram_server_running:
            if self.channel_setup_running("telegram"):
                return "starting"
            return "failed"
        if self.alerts_muted:
            return "muted"
        return "on"

    def _draft(self, *names):
        """Values the browser sent for fields the user has edited
        but not saved.

        Returns only the names actually PRESENT, so a missing key means
        "use the saved value" and an empty string means "the user cleared
        it" -- different answers that `or` cannot tell apart.

        This uses the same cleaning and key parsing as save.
        """
        try:
            body = flask.request.get_json(silent=True) or {}
        except Exception:                                   # noqa: BLE001
            body = {}
        if not isinstance(body, dict):
            return {}
        return {name: ("" if body[name] is None
                       else self._clean_setting_string(name, str(body[name])))
                for name in names if name in body}

    @octoprint.plugin.BlueprintPlugin.route("/test_notify", methods=["POST"])
    def test_notify(self):
        """Test one selected channel or every configured channel."""
        denied = self._require_control()
        if denied is not None:
            return denied
        # An omitted channel retains the combined-test API behaviour.
        wanted = (flask.request.args.get("channel") or "").strip().lower()
        if wanted and wanted not in ("telegram", "discord"):
            return Response(
                json.dumps({"error": "unknown channel %r" % wanted}),
                status=400, mimetype="application/json")
        # Read once and never log the draft credentials.
        draft = self._draft("telegramBotToken", "telegramChatID",
                            "discordBotToken", "discordChannelID",
                            "customSnapshotURL")
        denied = self._require_admin_for_draft(draft)
        if denied is not None:
            return denied

        id_checks = []
        if wanted in ("", "telegram"):
            id_checks.append(self.telegram_chat_id_error(
                draft.get("telegramChatID", self.telegram_chat_id)))
        if wanted in ("", "discord"):
            id_checks.append(self.discord_channel_id_error(
                draft.get("discordChannelID", self.discord_channel_id)))
        for problem in id_checks:
            if problem:
                self._logger.warning(
                    "Notification connection test rejected: %s", problem)
                return Response(json.dumps({"ok": False,
                                            "message": problem}),
                                mimetype="application/json")
        image = self.notification_image(
            "PiNozCam test", url_override=draft.get("customSnapshotURL"))
        # The control row in test and alert is shared on the saved-credential
        # path.
        caption = ("PiNozCam test message. If you can read this, monitoring "
                   "replies and failure alerts from this printer can reach "
                   "you.")
        results = {}
        if wanted == "discord":
            pass
        elif (draft.get("telegramBotToken", self.telegram_bot_token)
              and draft.get("telegramChatID", self.telegram_chat_id)):
            token = draft.get("telegramBotToken")
            chat = draft.get("telegramChatID")
            # The pair actually about to be used: the draft where the user
            # has edited it, the saved value where they have not.
            use_token = (token if token is not None
                         else self.telegram_bot_token)
            use_chat = chat if chat is not None else self.telegram_chat_id
            try:
                if token is not None or chat is not None:
                    # At least one credential is unsaved, so send with
                    # the pair the user is looking at, not the one on
                    # disk.
                    self.telegram_send_draft(use_token, use_chat,
                                             image=image, caption=caption)
                # notify_all, so the control row is built in ONE place --
                # the same place an alert builds it, including deriving the
                # Pause/Resume label from the printer state.
                #
                # respect_mute=False: the user pressed Test and is owed an
                # answer whatever they muted during an earlier print.
                # wait=True (the default) because "sent" has to mean sent.
                elif not self.notify_all(
                        caption, image=image, buttons=True, silent=True,
                        respect_mute=False, only=("telegram",))[1]:
                    raise RuntimeError(
                        "Telegram refused the message; the reason is in "
                        "octoprint.log.")
                results["telegram"] = "sent"
            except Exception as exc:
                detail = self.redact(str(exc))
                hint = self.describe_telegram_problem(use_token, use_chat)
                if hint:
                    detail = "%s (%s)" % (detail, hint)
                results["telegram"] = "failed: %s" % detail
        else:
            results["telegram"] = "not configured"

        if wanted == "telegram":
            pass
        else:
            token = draft.get("discordBotToken")
            channel = draft.get("discordChannelID")
            # The pair actually about to be used: the draft where the user
            # has edited it, the saved value where they have not.
            use_token = (token if token is not None
                         else self.discord_bot_token)
            use_channel = (channel if channel is not None
                           else self.discord_channel_id)
            if not (use_token and use_channel):
                if use_token or use_channel:
                    results["discord"] = (
                        "failed: %s"
                        % self.describe_discord_problem(use_token,
                                                        use_channel))
                else:
                    results["discord"] = "not configured"
            else:
                try:
                    if token is not None or channel is not None:
                        # At least one credential is unsaved, so send with
                        # the pair the user is looking at, not the one on
                        # disk.
                        self.discord_send_draft(use_token, use_channel,
                                                image=image, caption=caption)
                    elif self.notify_all(
                            caption, image=image, buttons=True, silent=True,
                            respect_mute=False, only=("discord",))[1]:
                        pass
                    else:
                        raise RuntimeError(
                            "the saved Discord credentials did not start a "
                            "bot; the reason is in octoprint.log at startup.")
                    results["discord"] = "sent"
                except Exception as exc:              # noqa: BLE001
                    # redact(): requests puts the failing URL in its
                    # exception message and for Discord that URL carries the
                    # token. This string goes to the browser AND, one line
                    # below, into octoprint.log.
                    detail = self.redact(str(exc))
                    hint = self.describe_discord_problem(use_token,
                                                         use_channel)
                    if hint:
                        detail = "%s (%s)" % (detail, hint)
                    results["discord"] = "failed: %s" % detail

        if wanted != "telegram" and (self.discord_bot_token
                                     and self.discord_channel_id):
            # A separate line because it answers a different question:
            # "discord" says whether a message got through, this says whether
            # a BUTTON would work. Sending is REST and needs no gateway, so
            # "sent" plus "not running" is a real and coherent state.
            bot = self.discord_bot
            results["discordButtons"] = (
                "connected" if bot is not None and bot.connected
                else ("connecting" if bot is not None else "not running"))
        self._logger.info("Test notification: %s", results)
        attempted = {k: v for k, v in results.items()
                     if v not in ("not configured",)}
        failed = [k for k, v in attempted.items()
                  if not (v == "sent" or v in ("connected", "connecting"))]
        if not attempted:
            return Response(
                json.dumps({"ok": None,
                            "message": "Nothing configured for this "
                                       "channel."}),
                mimetype="application/json")
        message = ", ".join("%s: %s" % (k, v)
                            for k, v in sorted(results.items()))
        # Button connectivity is diagnostic. A REST message that arrived is
        # still a successful connection test while the gateway reconnects.
        failed = [key for key in failed if key != "discordButtons"]
        return Response(
            json.dumps({"ok": not failed, "message": message}),
            mimetype="application/json")

    @octoprint.plugin.BlueprintPlugin.route("/test_snapshot", methods=["POST"])
    def test_snapshot(self):
        """Fetch one frame and report what came back.

        Answers "can this URL be read", which is otherwise only discoverable
        by starting a print and seeing whether detection does anything. The
        camera is the one thing the plugin cannot supply for itself, so it is
        also the most common thing to have wrong.

        Says only whether an image is there and can be used, and which of the
        three places it came from -- a local file, a URL, or OctoPrint's own
        webcam. Deliberately no resolution and no timing: the question this
        button answers is "have I pointed it at something that exists", and
        the speed of one fetch is the Speed Test's job.

        A custom URL that fails is reported as URL-specific and does not fall
        back to the webcam.
        """
        denied = self._require_control()
        if denied is not None:
            return denied
        # The URL in the box, not the one last saved. Pressing Test beside a
        # field you have just edited and having it check the previous value is
        # worse than having no button.
        draft = self._draft("customSnapshotURL")
        # A draft URL is fetched from this request context:
        # endpoint fetch anything the caller names, from the printer's own
        # network or its filesystem, and CONTROL reaches down to the USER
        # group. Testing the SAVED url stays open; naming a new one is admin
        # work. The reply here is only ok/failed and a size, so it is a
        # weaker oracle than the notification test -- but it is still a
        # request made on the server's behalf to an address a non-admin
        # chose.
        denied = self._require_admin_for_draft(draft)
        if denied is not None:
            return denied
        override = draft.get("customSnapshotURL")
        try:
            image = self.get_snapshot(url_override=override)
        except Exception as exc:
            return Response(
                json.dumps({"ok": False,
                            "message": "%s: %s" % (type(exc).__name__,
                                                   self.redact(str(exc)))}),
                mimetype="application/json")
        if image is None:
            return Response(json.dumps({
                "ok": False,
                "message": ("No frame. Check the URL, that the camera is "
                            "reachable from this machine, and that it serves "
                            "a still image -- RTSP is not supported.")}),
                            mimetype="application/json")
        # override is None is the only way to distinguish "no draft" from
        # "user cleared the box"; empty draft means webcam source.
        url = ((self.custom_snapshot_url if override is None else override)
               or "").strip()
        if not url:
            where = "OctoPrint's own webcam"
        elif url.startswith("file://"):
            where = "the local file"
        else:
            where = "the URL"
        return Response(json.dumps({
            "ok": True,
            "message": "An image was read from %s and can be used." % where}),
            mimetype="application/json")

    @octoprint.plugin.BlueprintPlugin.route(
        "/test_inference", methods=["POST"])
    def test_inference(self):
        """Run ONE frame end to end and report how long it took.

        "Will detection keep up on this board" cannot be answered from
        specs -- the same binary is 271 ms on a 64-bit Pi 5 and 7.7 s on a
        Pi 3B+ -- so it is measured here, on the user's own hardware.

        The displayed metric is model time, with throughput derived from it.
        `fetch_ms` and `total_ms` are returned separately so camera speed does
        not affect model latency.
        """
        denied = self._require_control()
        if denied is not None:
            return denied
        if not self._test_inference_lock.acquire(blocking=False):
            return Response(json.dumps({
                "ok": False,
                "message": "A speed test is already running; wait for it "
                           "to finish."}),
                status=409, mimetype="application/json")
        try:
            return self._test_inference_locked()
        finally:
            self._test_inference_lock.release()

    def _test_inference_locked(self):
        """The body of /test_inference; caller holds the one-test lock."""
        backend, temporary = self.backend, False
        if backend is None:
            try:
                backend = NozcamBackend(self.plugin_dir, self._logger,
                                        backend=self.ai_backend)
                backend.preflight()
                temporary = True
            except Exception as exc:
                return Response(json.dumps({
                    "ok": False,
                    "message": "Inference backend unavailable: %s"
                               % self.redact(str(exc))}),
                    mimetype="application/json")
        cpus = getattr(self, "ai_cpus", None)
        if cpus is None:
            self._thread_calculation()
            cpus = getattr(self, "ai_cpus", None)
        if temporary:
            try:
                backend.ensure_started(self.scores_threshold,
                                       self.img_sensitivity, cpus)
                warm, _warm_source = self._inference_test_frame()
                # One throwaway frame: ExecuTorch grows its arena on the
                # first execute, which is measurable on its own.  A camera
                # is not required to benchmark the runner, so an in-memory
                # placeholder keeps the warm-up contract intact offline.
                backend.infer(warm, self.scores_threshold,
                              self.img_sensitivity, cpus)
            except Exception as exc:
                try:
                    backend.stop()
                except Exception:
                    pass
                return Response(json.dumps({
                    "ok": False,
                    "message": "Backend would not start: %s"
                               % self.redact(str(exc))}),
                    mimetype="application/json")
        started = time.monotonic()
        try:
            image, frame_source = self._inference_test_frame()
            fetch_ms = (time.monotonic() - started) * 1000.0
            # Same arguments the detection loop passes, so this is that
            # loop's measurement and not a different one -- the thresholds and
            # the CPU set both affect it, and a mismatch would additionally
            # make ensure_started restart the daemon mid-timing.
            result = backend.infer(image, self.scores_threshold,
                                   self.img_sensitivity, cpus)
            round_trip_ms = (time.monotonic() - started) * 1000.0
        except Exception as exc:
            return Response(json.dumps({
                "ok": False,
                "message": "%s: %s" % (type(exc).__name__,
                                       self.redact(str(exc)))}),
                            mimetype="application/json")
        finally:
            if temporary:
                # Never leave a daemon behind. It is a child process holding
                # 146 MB, and one per Test press would accumulate.
                try:
                    backend.stop()
                except Exception:
                    self._logger.exception(
                        "Could not stop the temporary test daemon.")
        total_ms = round_trip_ms
        self._logger.info(
            "Speed test: %.0f ms model, %.0f ms fetch, %.0f ms round trip",
            float(result[-1]) * 1000.0, fetch_ms, total_ms)
        # num_threads is what _thread_calculation() actually pinned to, not
        # what the setting asked for -- the two differ, because the lower
        # settings deliberately leave a core for the gcode streamer.
        affinity = self._affinity_core_summary()
        cores = affinity["selected"] or 1
        pool_cores = affinity["pool"] or 1
        pool_label = ("performance core" if affinity["heterogeneous"]
                      else "core")
        # infer() returns elapsed_time as its last element.
        model_ms = float(result[-1]) * 1000.0
        # per_min is derived from the MODEL time, and the field names say
        # which is which so a reader does not have to guess.
        per_min = 60000.0 / model_ms if model_ms > 0 else 0.0
        return Response(json.dumps({
            "ok": True,
            "model_ms": round(model_ms, 1),
            "fetch_ms": round(fetch_ms, 1),
            "total_ms": round(total_ms, 1),
            "frame_source": frame_source,
            "per_min_basis": "model_ms",
            "message": ("%.0f ms per inference = at most %.0f checks/min, "
                        "on %d of %d %s%s%s"
                        % (model_ms, per_min, cores, pool_cores, pool_label,
                           "" if pool_cores == 1 else "s",
                           ("; using the built-in no-camera test image"
                            if frame_source == "placeholder" else "")))}),
                        mimetype="application/json")

    def _inference_test_frame(self):
        """Return a frame for Speed Test and identify where it came from.

        Speed Test measures the local runner, not camera availability.  The
        snapshot test owns camera diagnosis, so an offline or not-yet-set-up
        camera must not prevent users from checking whether inference works.
        The existing in-memory no-camera artwork is deterministic, costs no
        packaged image asset, and exercises the exact same Pillow/Image path
        as a real frame.
        """
        image = self.get_snapshot()
        if image is not None:
            return image, "camera"
        return self.create_no_camera_image(), "placeholder"

    @octoprint.plugin.BlueprintPlugin.route("/snapshot_raw", methods=["GET"])
    def snapshot_raw(self):
        """Return an unmasked snapshot for the mask editor."""
        denied = self._require_control()
        if denied is not None:
            return denied
        raw = self.get_snapshot()
        if raw is None:
            return Response(
                json.dumps({"image": self._encode_no_camera_image(),
                            "hasCamera": False}),
                mimetype="application/json",
            )
        return Response(
            json.dumps({"image": self.encode_image_to_base64(raw),
                        "hasCamera": True,
                        "width": raw.size[0], "height": raw.size[1],
                        "maskGrid": MASK_GRID}),
            mimetype="application/json",
        )

    def is_blueprint_csrf_protected(self):
        """Require CSRF tokens on this plugin's endpoints."""
        return True

    def _require_admin_for_draft(self, draft):
        """Refuse a draft-credential test unless the caller is an admin.

        Returns a 403 Response to return, or None to continue.

        Saved configuration tests use USER group access, while draft tests use
        ADMIN due to the request-scope URL/token combination.
        """
        if not draft:
            return None
        try:
            if Permissions.ADMIN.can():
                return None
        except Exception:                                   # noqa: BLE001
            # Unresolvable permission: refuse, rather than fall open. The
            # same fail-closed choice _require_control makes.
            pass
        self._logger.warning(
            "Refusing a draft settings test from a non-admin caller "
            "(fields: %s).", ", ".join(sorted(draft)))
        return Response(
            json.dumps({
                "ok": False,
                "message": "Testing unsaved values needs an administrator. "
                           "Save the settings first, then test."}),
            status=403, mimetype="application/json")

    def _require_control(self):
        """Refuse the request unless the caller may control this plugin.

        Not a decorator. Permissions.PLUGIN_PINOZCAM_CONTROL is a lazy
        placeholder at import time -- the real permission does not exist
        until OctoPrint has registered it -- so a decorator evaluated while
        the module loads raises AttributeError on every request. Resolving
        it here, per request, is what makes it work.

        Returns a 403 Response to return, or None to continue.
        """
        try:
            permission = Permissions.PLUGIN_PINOZCAM_CONTROL
            if permission.can():
                return None
        except Exception:
            # If the permission cannot be resolved at all, fall back to
            # requiring an admin rather than letting everyone through.
            try:
                if Permissions.ADMIN.can():
                    return None
            except Exception:
                pass
        return Response(
            json.dumps({"error": "not permitted"}),
            status=403, mimetype="application/json")

    def get_additional_permissions(self):
        """A permission for the endpoints that see or change things.

        Being logged in is not a sufficient bar for these: snapshot_raw
        returns the unmasked camera view, and test_notify sends a message
        to the owner's phone.
        OctoPrint's own model is that a viewer-level account can watch a
        print but not reconfigure it, and these belong on the other side of
        that line.

        The permission defaults to the admin and user groups, so an
        existing installation keeps working; a read-only account does not
        get it.
        """
        return [
            {
                "key": "CONTROL",
                "name": "Control PiNozCam",
                "description": "Read the raw camera view, edit the Undetect "
                               "Zone and test monitoring or alert channels.",
                "roles": ["control"],
                "dangerous": False,
                "default_groups": [octoprint.access.ADMIN_GROUP,
                                   octoprint.access.USER_GROUP],
            }
        ]
