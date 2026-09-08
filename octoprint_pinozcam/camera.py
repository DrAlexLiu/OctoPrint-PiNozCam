"""Camera acquisition, transforms, masking, sharpness and annotations."""

import base64
import hashlib
import logging
import os
import threading
import time
from email.message import Message
from io import BytesIO
from urllib.parse import urlparse
import requests
from PIL import (Image, ImageChops, ImageDraw, ImageFilter, ImageFont,
                 ImageStat)
import octoprint.plugin
import octoprint.access
from . import framesource


class CameraMixin(object):
    """Mixed into PinozcamPlugin; see the module docstring."""

    def initialize_cameras(self):
        """Find the webcam providers OctoPrint offers, if any.

        Re-run on settings save, because a provider plugin may have been
        enabled since startup."""
        self._logger.info("Initialize the camera")
        if hasattr(octoprint.plugin.types, "WebcamProviderPlugin"):
            webcam_type = octoprint.plugin.types.WebcamProviderPlugin
            self.cameras = self._plugin_manager.get_implementations(
                webcam_type)
            self.snap_new_method = True
        else:
            self.cameras = []
            self.snap_new_method = False
    # Prefer redistributable system fonts, then Pillow's built-in font.
    SYSTEM_FONTS = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/Library/Fonts/Arial Unicode.ttf",
    )

    def load_font(self, font_size=28):
        """Return a scalable system or built-in font without raising."""
        for path in self.SYSTEM_FONTS:
            if not os.path.exists(path):
                continue
            try:
                return ImageFont.truetype(path, font_size)
            except (IOError, OSError):
                continue
        try:
            # Pillow >= 10.1 only; older versions reject the argument.
            return ImageFont.load_default(size=font_size)
        except TypeError:
            return ImageFont.load_default()

    def initialize_font(self, font_size=28):
        """Load the label font once and cache it on the instance."""
        self.font = self.load_font(font_size)
        self._logger.info("Label font: %s",
                          getattr(self.font, "path", "Pillow built-in"))

    def _encode_no_camera_image(self):
        """Return the cached no-camera placeholder as a data URL."""
        return ("data:image/jpeg;base64,"
                + base64.b64encode(self._no_camera_jpeg()).decode("utf-8"))

    def _global_flips(self):
        """The user's webcam transform, from wherever this OctoPrint keeps it.

        ⚠️ NOT simply `webcam.flipH` any more. OctoPrint 1.9's classicwebcam
        plugin migrates flipH, flipV and rotate90 into its own settings and
        then calls `global_remove(["webcam", "flipH"])` on the originals
        (plugins/classicwebcam/__init__.py, on_settings_migrate). Reading the
        old keys on 1.9+ therefore returns the DEFAULT rather than the user's
        choice -- so a flip the user had ticked was silently not applied to
        the frame the model sees, and this detector is not flip invariant.

        The provider path never had this problem: it reads flipH/flipV/
        rotate90 off the webcam's own configuration. This is the fallback
        for the two paths that have no provider config to read -- a custom
        snapshot URL, and the default-snapshot fallback -- and it now asks
        the same place the provider path does before dropping back to the
        old keys, which remain correct on OctoPrint < 1.9.
        """
        if self.snap_new_method:
            config = self._default_webcam_config()
            if config is not None:
                return (bool(getattr(config, "flipH", False)),
                        bool(getattr(config, "flipV", False)),
                        bool(getattr(config, "rotate90", False)))
        return (
            bool(self._settings.global_get_boolean(["webcam", "flipH"])),
            bool(self._settings.global_get_boolean(["webcam", "flipV"])),
            bool(self._settings.global_get_boolean(["webcam", "rotate90"])),
        )

    def _default_snapshot_url(self):
        """The snapshot URL for the last-resort path, or "".

        ⚠️ `webcam.snapshot` is one of the eleven keys OctoPrint 1.9's
        classicwebcam plugin migrates and then global_remove()s, so reading
        it alone made this fallback DEAD on 1.9+: it could only ever find
        nothing and log "No snapshot URL configured". Ask the designated
        snapshot webcam first -- that is where the value lives now -- and
        keep the old key for OctoPrint < 1.9.

        Reached only when the provider path found no webcam that answered,
        so a line saying which source rescued it is worth having: the two
        failure modes look identical from the outside.
        """
        config = self._default_webcam_config() if self.snap_new_method \
            else None
        if config is not None:
            compat = getattr(config, "compat", None)
            url = getattr(compat, "snapshot", None) if compat else None
            if url:
                self._logger.debug(
                    "Default snapshot URL came from the snapshot webcam's "
                    "configuration, not the legacy webcam.snapshot setting")
                return url
        return self._settings.global_get(["webcam", "snapshot"])

    def _default_webcam_config(self):
        """The snapshot webcam's configuration object, or None.

        Separate from _provider_candidates() on purpose: that one enumerates
        every webcam to find one that answers, which is the right question
        when actually taking a picture. Here the question is only "what
        transform did the user configure", so the designated snapshot webcam
        is the answer, and a camera that happens to be unreachable does not
        change it.
        """
        try:
            import octoprint.webcams
            provided = octoprint.webcams.get_snapshot_webcam()
        except Exception as exc:                              # noqa: BLE001
            self._logger.debug("get_snapshot_webcam failed: %s", exc)
            return None
        return getattr(provided, "config", None) if provided else None

    # One JPEG frame is single-digit MB even at 4K. The realistic runaway
    # is a provider answering take_webcam_snapshot() with its MJPEG
    # STREAM iterator, which never ends -- bound the total so that costs
    # one failed tick instead of a sampler thread stuck joining forever.
    SNAPSHOT_MAX_BYTES = 16 * 1024 * 1024

    # Wall-clock bound on ONE snapshot read, whole body included.
    #
    # the total. A source that dribbles -- an MJPEG stream reached through a
    # snapshot URL, which is a common misconfiguration -- refreshes the read
    # timeout with every chunk and never trips it, so the transfer runs until
    # the size cap. At the sampler's five ticks a second that is a 16 MB
    # allocation five times a second on a 2 GB board, and it blocks the
    # sampler for the whole read.
    SNAPSHOT_DEADLINE = 20.0

    def _read_snapshot(self, chunks, deadline=None):
        """b"".join of a provider's byte iterator, with a size bound.

        join, not repeated +=: each += copies the whole accumulated
        buffer again, so a 2 MB JPEG arriving in small chunks was
        quadratic in chunk count.

        A provider that BLOCKS without yielding cannot be bounded from
        in here (the iterator owns the blocking read); the detection
        loop's camera watch reports the resulting frame famine as a
        camera outage after 30 s, so that failure is loud too.
        """
        parts = []
        total = 0
        for chunk in chunks:
            total += len(chunk)
            if total > self.SNAPSHOT_MAX_BYTES:
                raise IOError(
                    "snapshot passed %d MB and is still going -- this "
                    "looks like a stream, not a snapshot"
                    % (self.SNAPSHOT_MAX_BYTES // (1024 * 1024)))
            if deadline is not None and time.monotonic() > deadline:
                raise IOError(
                    "snapshot still arriving after %.0f s -- this looks "
                    "like a stream, not a snapshot"
                    % self.SNAPSHOT_DEADLINE)
            parts.append(chunk)
        return b"".join(parts)

    def _fetch_snapshot_image(self, url, quiet=False):
        """Fetch one snapshot URL with bounded download and decoding."""
        raw = framesource._fetch_snapshot_bytes(
            url, self.SNAPSHOT_MAX_BYTES, self.SNAPSHOT_DEADLINE)
        return framesource._decode_source_image(raw)

    def _log_camera_failure(self, name, exc):
        """Log one outage line once per camera outage cycle."""
        text = self.redact(str(exc))
        state = self._camera_failures.get(name)
        if state is not None and state["text"] == text:
            state["n"] += 1
            return
        self._camera_failures[name] = {"text": text, "n": 1}
        self._logger.error("Camera %s failed: %s",
                           name if name else "(unnamed)", text)

    def _note_camera_recovered(self, name):
        """Close out a logged outage for `name`, if there was one."""
        state = self._camera_failures.pop(name, None)
        if state is not None:
            self._logger.info(
                "Camera %s is answering again (%d failed attempt%s "
                "suppressed).", name if name else "(unnamed)", state["n"],
                "" if state["n"] == 1 else "s")

    def _provider_candidates(self):
        """(provider, config) pairs in the order snapshots are tried.

        The webcam OctoPrint designates for snapshots comes first: with
        several cameras, "the first provider that answers" need not be
        the one the user pointed at the printer, and the detector, the
        Live Camera toggle and the Undetect Zone must all mean the same
        device. get_snapshot_webcam() also covers a stale designation by
        falling back to the first snapshot-capable webcam itself.

        The old any-provider scan stays behind it as a fallback, so a
        designated camera that is down does not end detection while
        another camera still answers; snapshot_source_name records who
        actually served, and the Live Camera toggle hides itself on any
        mismatch.
        """
        candidates = []
        seen = set()
        try:
            import octoprint.webcams
            provided = octoprint.webcams.get_snapshot_webcam()
        except Exception as exc:
            # Covers OctoPrint < 1.9 too (no octoprint.webcams module),
            # though snap_new_method is False there anyway.
            provided = None
            self._logger.debug("get_snapshot_webcam failed: %s", exc)
        if provided is not None:
            config = getattr(provided, "config", None)
            camera = getattr(provided, "providerPlugin", None)
            if config is not None and camera is not None:
                candidates.append((camera, config))
                seen.add(getattr(config, "name", None))
        for camera in self.cameras:
            try:
                configs = camera.get_webcam_configurations()
            except Exception as exc:
                # A provider that cannot describe itself must not take
                # snapshots down for everyone else.
                self._logger.debug(
                    "Cannot read webcam configuration: %s", exc)
                continue
            for config in configs:
                name = getattr(config, "name", None)
                if name in seen:
                    continue
                seen.add(name)
                candidates.append((camera, config))
        return candidates

    @staticmethod
    def _url_fingerprint(url):
        """A stable opaque id for a URL, carrying none of it.

        Only ever compared for equality, so a truncated digest is enough and
        keeps the value short in config.yaml. Prefixed so a stored value is
        recognisably a fingerprint and not a URL somebody can try to open.
        """
        digest = hashlib.sha256((url or "").encode("utf-8")).hexdigest()
        return "url#%s" % digest[:16]

    def _snapshot_geometry_now(self):
        """(source, detail, flipH, flipV, rotate90) for the NEXT snapshot.

        Mirrors get_snapshot()'s own decision order rather than reading
        the global webcam settings for everything, because the three
        paths genuinely disagree:
          * a custom file:// URL never reaches transform_image(), so no
            flip is applied there no matter what the settings say;
          * a WebcamProviderPlugin carries its own flipH/flipV/rotate90 in
            its configuration. OctoPrint 1.9 moved the classic webcam's
            flips out of the global webcam settings, so a settings-only
            guess would miss a flip change on that path entirely -- the
            frame would turn over and the mask would not follow.
        Predicting the FIRST provider config, where get_snapshot() takes
        the first one that answers, is the single approximation here; it
        only matters on a machine with several webcams, one of them dead.
        """
        if self.custom_snapshot_url:
            # in maskSignature, handed out by the settings REST API to any
            # logged-in user, and interpolated into the mask warning that
            # reaches the browser and octoprint.log. An IP camera URL
            # routinely carries its password -- http://user:pass@cam/snap --
            # so storing it here re-exposed the one setting that IS declared
            # restricted, through a derived value that was not.
            #
            # A hash serves the only purpose the detail has: telling whether
            # the camera is still the same one. Nothing needs to read it back.
            detail = self._url_fingerprint(self.custom_snapshot_url)
            if self.custom_snapshot_url.startswith("file://"):
                return ("custom", detail, False, False, False)
            return ("custom", detail) + self._global_flips()

        if self.snap_new_method:
            # Same order as get_snapshot() itself -- the point of this
            # method is to predict THAT choice, so they must share it.
            for camera, config in self._provider_candidates():
                detail = "%s/%s" % (
                    getattr(camera, "_identifier", "?"),
                    getattr(config, "name", "") or "?",
                )
                return ("provider", detail,
                        bool(getattr(config, "flipH", False)),
                        bool(getattr(config, "flipV", False)),
                        bool(getattr(config, "rotate90", False)))

        return ("global", "") + self._global_flips()

    def _global_webcam_flags(self):
        """Read webcam flip and rotate flags in one call.

        ⚠️ Kept as a name, not as a second implementation. The two
        differed in docstring and formatting -- one returned a wrapped
        tuple, the other a single-line one -- but read the identical three
        settings paths and returned the identical value, which is exactly
        why the OctoPrint 1.9 fix had to be made in both places or neither:
        two call sites went through this name, two through the other.
        """
        return self._global_flips()

    def transform_image(self, img, must_flip_h, must_flip_v, must_rotate):
        # Only call Pillow if we need to transpose anything
        """Apply webcam flip/rotate transforms to a frame."""
        if must_flip_h or must_flip_v or must_rotate:
            # candidate, so at the 200 ms default this wrote five identical
            # INFO lines a second -- about 18,000 an hour into the file users
            # attach to bug reports. Same class as the camera-failure spam
            # fixed in _log_camera_failure. The information is worth having
            # once, because a wrong flip is invisible in the numbers and
            # obvious in this line.
            flags = (bool(must_flip_h), bool(must_flip_v), bool(must_rotate))
            if flags != self._logged_transform:
                self._logged_transform = flags
                self._logger.info(
                    "Transformations : FlipH=%s, FlipV=%s Rotate=%s",
                    must_flip_h, must_flip_v, must_rotate)

            if must_flip_h:
                img = img.transpose(Image.FLIP_LEFT_RIGHT)
            if must_flip_v:
                img = img.transpose(Image.FLIP_TOP_BOTTOM)
            if must_rotate:
                img = img.rotate(90, expand=True)
        return img

    def _prepare_source_image(self, image, geometry, record_geometry=True):
        """Optionally record source geometry and apply transforms."""
        if record_geometry:
            self._note_source_frame(image, geometry)
        return self.transform_image(
            image, geometry[2], geometry[3], geometry[4])

    def get_snapshot(self, url_override=None, record_geometry=None,
                     quiet=False):
        """Fetch one unmasked frame, or None if unavailable."""
        url = (self.custom_snapshot_url if url_override is None
               else url_override)
        if record_geometry is None:
            record_geometry = url_override is None
        if url:
            self._logger.debug("Using custom URL")
            try:
                if url.startswith("file://"):
                    # Handle local file paths
                    file_path = url.partition('file://')[2]
                    with open(file_path, "rb") as file:
                        img = Image.open(file)
                        if record_geometry:
                            # values are the same ones the predictor
                            # already special-cases for a file URL: this
                            # path applies NO transform, because a file is
                            # whatever is on disk. Binding it here means
                            # the record cannot drift from the code above
                            # if that ever changes.
                            self._note_source_frame(img, (
                                "custom",
                                self._url_fingerprint(url),
                                False, False, False))
                        return img.copy()

                else:
                    # Classify HTTP URLs before reading; they may be MJPEG.
                    kind = _classify_custom_url(url)
                    if kind == "mjpeg":
                        img = _grab_one_mjpeg_frame(
                            url, logger=self._logger)
                    else:
                        img = self._fetch_snapshot_image(url, quiet=quiet)
                    # Before the transforms on purpose: the mask signature
                    # stores the SOURCE aspect ratio.
                    # the record. Reading them twice -- once to predict,
                    # once to apply -- let a settings save land between,
                    # so the frame carried a description of flips it had
                    # not been given.
                    # admin-restricted precisely because an IP camera URL
                    # routinely carries user:pass@.
                    flip_h, flip_v, rotate = self._global_webcam_flags()
                    geometry = (
                        "custom", self._url_fingerprint(url),
                        flip_h, flip_v, rotate)
                    return self._prepare_source_image(
                        img, geometry, record_geometry)
            except requests.RequestException as e:
                if not quiet:
                    self._logger.error(
                        "Failed to fetch custom snapshot URL (%s)",
                        type(e).__name__)
                return None
            except ValueError as e:
                # _classify_custom_url's own failure mode: the camera
                # answered, but with a Content-Type that is neither an
                # MJPEG stream nor a still image. Its message is always
                # built from the Content-Type header VALUE alone, never
                # from the URL or a request/urllib3 exception (see that
                # function's own docstring) -- redact() is applied for
                # the same reason the clauses around this one apply it,
                # not because this particular text needs it.
                if not quiet:
                    self._logger.error(
                        "Custom snapshot URL did not answer usably: %s",
                        self.redact(str(e)))
                return None
            except IOError as e:
                if not quiet:
                    self._logger.error(
                        "Failed to open local file from custom snapshot "
                        "URL: %s", self.redact(str(e)))
                return None

        if self.snap_new_method:
            for camera, config in self._provider_candidates():
                try:
                    # The documented parameter is the webcam's NAME.
                    # OctoPrint's own classic webcam ignores it, which is
                    # how passing the whole config object here looked
                    # fine for years; a provider that honours the
                    # argument needs a name it can resolve.
                    snapshot_iter = camera.take_webcam_snapshot(
                        getattr(config, "name", ""))
                    snapshot = self._read_snapshot(snapshot_iter)

                    must_flip_h = config.flipH
                    must_flip_v = config.flipV
                    must_rotate = config.rotate90

                    # Create an Image object from the snapshot bytes
                    img = Image.open(BytesIO(snapshot))
                    # predicted. _note_source_frame's own prediction
                    # names the DESIGNATED webcam, so on a fallback --
                    # designated camera down, a spare answered -- the
                    # frame would be filed under flips it does not have
                    # and the Undetect Zone realigned into a coordinate
                    # system that never existed. Same tuple shape as
                    # _snapshot_geometry_now() returns.
                    geometry = (
                        "provider",
                        "%s/%s" % (
                            getattr(camera, "_identifier", "?"),
                            getattr(config, "name", "") or "?"),
                        bool(must_flip_h), bool(must_flip_v),
                        bool(must_rotate))
                    img = self._prepare_source_image(
                        img, geometry, record_geometry)
                    # Which camera ACTUALLY answered. The designated
                    # snapshot webcam is tried first, so normally this
                    # matches OctoPrint's designation; after a fallback
                    # (designated camera down, another one answered) it
                    # differs, and the Live Camera toggle compares this
                    # name against the designation and hides itself on
                    # a mismatch, so it can never stream a camera the
                    # detector is not watching.
                    self.snapshot_source_name = getattr(
                        config, "name", None)
                    self._note_camera_recovered(self.snapshot_source_name)
                    return img
                except Exception as e:
                    if not quiet:
                        self._log_camera_failure(
                            getattr(config, "name", None), e)

        self._logger.debug("Falling back to default snapshot method")
        # Not a provider, so no identity to compare -- the Live Camera
        # toggle stays hidden rather than guessing.
        self.snapshot_source_name = None
        snapshot = None
        snapshot_url = self._default_snapshot_url()
        if not snapshot_url:
            if not quiet:
                self._logger.error("No snapshot URL configured")
            return None

        try:
            if snapshot_url.startswith("file://"):
                # Handling local file paths
                file_path = snapshot_url.partition('file://')[2]
                with open(file_path, "rb") as file:
                    # copy() inside the with block: Pillow is lazy, so an
                    # Image left open past its file gives a working .size
                    # and then raises ValueError on the first real access --
                    # which neither except clause below catches, so it
                    # escaped as a 500 on every one-second poll.
                    img = Image.open(file).copy()
            else:
                # Handling URLs
                img = self._fetch_snapshot_image(snapshot_url, quiet=quiet)

            # One read, used for both the record and the transform. See
            # the custom-URL path above for why that matters. "global"
            # and "" are what the predictor files this path under; only
            # the flags are bound here.
            must_flip_h, must_flip_v, must_rotate = \
                self._global_webcam_flags()
            geometry = ("global", "", must_flip_h, must_flip_v, must_rotate)
            return self._prepare_source_image(
                img, geometry, record_geometry)
        except requests.RequestException as e:
            if not quiet:
                self._logger.error("Failed to fetch default snapshot: %s",
                                   self.redact(str(e)))
            return None
        except IOError as e:
            if not quiet:
                self._logger.error("Failed to open local snapshot file: %s",
                                   self.redact(str(e)))
            return None

    def _cached_masked_snapshot(self):
        """Return a one-second cached masked frame and ETag."""
        with self.lock:
            cached = self.snapshot_cache
            if (cached is not None
                    and time.monotonic() - cached['time'] < 1.0):
                return cached['etag'], cached['jpeg']

        refresh = False
        # Serialise cache misses across browser tabs.
        with self.frames.encode_lock:
            now = time.monotonic()
            with self.lock:
                cached = self.snapshot_cache
                if cached is not None and now - cached['time'] < 1.0:
                    return cached['etag'], cached['jpeg']

            held = self.frames.camera_raw()
            # Only an actively sampling producer owns the camera.
            sampler_owns_camera = self.sampler_active and self.enable_AI
            if held is not None and (sampler_owns_camera
                                     or now - held[1] <= 2.0):
                raw = held[0]
            elif sampler_owns_camera:
                raw = None
            else:
                # Keep a blocking provider out of the Flask request.
                refresh = True
                raw = None
            if raw is None:
                jpeg = self._no_camera_jpeg()
            else:
                jpeg = self.encode_image_to_jpeg_bytes(
                    self.apply_mask_to_image(raw))
            # The ETag also serves as the browser cache-buster.
            etag = "c%d" % int(now)
            with self.lock:
                self.snapshot_cache = {
                    'time': now, 'etag': etag, 'jpeg': jpeg}
        # Publish the placeholder before the worker can invalidate it.
        if refresh:
            self._request_live_snapshot_refresh()
        return etag, jpeg

    def _request_live_snapshot_refresh(self):
        """Start at most one between-print camera refresh in the background."""
        if self.shutting_down.is_set():
            return
        with self._live_snapshot_lock:
            thread = self._live_snapshot_thread
            if thread is not None and thread.is_alive():
                return
            thread = threading.Thread(
                target=self._refresh_live_snapshot,
                name="pinozcam-live-snapshot", daemon=True)
            self._live_snapshot_thread = thread
            thread.start()

    def _refresh_live_snapshot(self):
        """Fetch one live-view frame without occupying an HTTP worker."""
        current = threading.current_thread()
        try:
            image = self.get_snapshot(quiet=True)
            if image is not None and not self.shutting_down.is_set():
                with self.lock:
                    # Keep publication atomic with the request's cache check.
                    self.frames.publish_camera(image, time.monotonic())
                    self.snapshot_cache = None
        except Exception as exc:                           # noqa: BLE001
            self._logger.warning(
                "Background live-view snapshot failed: %s",
                self.redact(str(exc)))
        finally:
            with self._live_snapshot_lock:
                if self._live_snapshot_thread is current:
                    self._live_snapshot_thread = None

    def notification_image(self, fallback_text, url_override=None):
        """Return a masked notification frame or generated fallback image."""
        image = None
        try:
            image = self.get_snapshot(url_override=url_override,
                                      quiet=True)
        except Exception:                                   # noqa: BLE001
            image = None
        if image is None:
            return self.create_image_with_text(fallback_text)
        return self.apply_mask_to_image(image)

    def current_view_image(self):
        """The newest frame already in hand, else a fresh grab. UNMASKED.

        For "show me the printer right now" -- the Telegram /hi and
        Check commands and Discord's check. While a print runs the
        sampler is already grabbing several frames a second, so its
        newest one answers without touching the camera again; PEEKED,
        never taken -- take() would steal a candidate from the detector.
        (The frames are decoded PIL images already; "decode" happens at
        capture, the send re-encodes to JPEG.) Falls back to
        get_snapshot() when the sampler is off -- no print running -- or
        its frame has gone stale. Same 2 s freshness bound the live
        view's fallback uses.
        """
        held = self.frames.camera()
        if held is not None:
            return held
        return self.get_snapshot()

    def _no_camera_jpeg(self):
        """The 'no camera' placeholder as raw JPEG bytes.

        Cached, because the live view asks for this once a second for as
        long as the camera stays broken and the drawing is identical every
        time. Keyed on the size so a change to the processing resolution
        does not serve a stale one.
        """
        size = (self.proc_img_width, self.proc_img_height)
        cached = self._no_camera_cache
        if cached is not None and cached[0] == size:
            return cached[1]
        jpeg = self.encode_image_to_jpeg_bytes(
            self.create_no_camera_image(size))
        self._no_camera_cache = (size, jpeg)
        return jpeg

    # Sharpness is measured on a centre crop of this size, not the whole
    # frame. See measure_sharpness() for why it is a crop, and why it is
    # not smaller.
    SHARPNESS_CROP = (512, 512)

    def measure_sharpness(self, image):
        """Measure high-pass energy on a centre crop without NumPy."""
        width, height = image.size
        crop_w = min(width, self.SHARPNESS_CROP[0])
        crop_h = min(height, self.SHARPNESS_CROP[1])
        left = (width - crop_w) // 2
        top = (height - crop_h) // 2
        # Crop before conversion to avoid processing unused pixels.
        grey = image.crop(
            (left, top, left + crop_w, top + crop_h)).convert("L")
        blurred = grey.filter(ImageFilter.GaussianBlur(1))
        return ImageStat.Stat(ImageChops.difference(grey, blurred)).mean[0]

    def boxes_to_draw(self, scores, boxes):
        """The (box, score) pairs the annotated view shows.

        One definition for the display rule, used both by
        draw_response_data below and by the /check payload the browser
        draws its canvas from -- so the two views can never disagree
        about which detections are visible.

        Kept exactly as the drawing loop always behaved: a `break`, not a
        filter, relying on scores arriving sorted descending (which the
        backend's NMS guarantees). A stray low score mid-list would end
        the list there, and that has been the shipped behaviour all
        along.
        """
        shown = []
        for box, score in zip(boxes, scores):
            if score < self.scores_threshold:
                break
            shown.append((box, score))
        return shown

    def draw_response_data(self, scores, boxes, labels, severity, image):
        """Render inference boxes and labels onto the image."""
        draw = ImageDraw.Draw(image)
        color = "green"  # Default color for bounding boxes

        # Change the color based on the severity of the detection.
        # drawOverlay); change one and the other must follow.
        if severity > 0.66:
            color = "red"
        elif severity > 0.33:
            color = "yellow"

        for box, score in self.boxes_to_draw(scores, boxes):
            x1, y1, x2, y2 = box
            draw.rectangle([(x1, y1), (x2, y2)], outline=color, width=2)

            # Text to be drawn
            spaghetti_text = ""
            score_text = f"{score:.2f}"  # Format the score to two decimal places

            # Adjust text position so it does not overlap with the bounding box
            spaghetti_text_position = (x1, y1 - 32)
            score_text_position = (x2 - 56, y1 - 26)

            # Ensure the text stays within the image boundaries
            if spaghetti_text_position[1] < 0:
                spaghetti_text_position = (x1, y2 + 5)
            if score_text_position[1] < 0:
                score_text_position = (x2 - 5, y2 + 5)

            # Draw text
            draw.text(
                spaghetti_text_position, spaghetti_text,
                fill=color, font=self.font)
            draw.text(score_text_position, score_text, fill=color, font=self.font)

        return image

    def create_image_with_text(self, text, image_size=None, text_color="black"):
        # Determine the image size
        """A plain image with centred text, for placeholders.

        Loads the font itself if startup has not run yet -- a blueprint
        route can be reached before on_after_startup."""
        if image_size is None:
            image_size = (self.proc_img_width, self.proc_img_height)

        # Create a blank image
        image = Image.new('RGB', image_size, (255, 255, 255))
        draw = ImageDraw.Draw(image)

        # self.font is only set by on_after_startup, and a blueprint route
        # can be reached before that has run -- which made this raise
        # AttributeError on None. Fall back rather than fail: the caller
        # wants a picture with words on it, not a specific typeface.
        font = self.font
        if font is None:
            font = self.load_font(28)

        mask = font.getmask(text)
        bbox = mask.getbbox()
        if bbox is None:
            # An empty or whitespace-only string has no ink and therefore no
            # bounding box.
            x = y = 0
        else:
            x = (image.width - (bbox[2] - bbox[0])) / 2
            y = (image.height - (bbox[3] - bbox[1])) / 2

        draw.text((x, y), text, fill=text_color, font=font)

        return image

    # Drawn at this multiple of the target size and scaled back down.
    # ImageDraw has no antialiasing, so a circle drawn at final size has
    # visibly stepped edges.
    #
    # 2x is enough to antialias the circle and diagonal without paying the
    # former 4x draw/resample cost (57 ms on a Pi 5 A76). Every consumer goes
    # through _no_camera_jpeg, so even this smaller cost is paid only once.
    # Last (flipH, flipV, rotate90) actually applied, so transform_image can
    # log a change instead of every frame. None means "nothing logged yet".
    _logged_transform = None

    NO_CAMERA_SUPERSAMPLE = 2

    # (size, jpeg) for _no_camera_jpeg. A class attribute rather than
    # something initialize() sets, because a blueprint route can be served
    # before initialize() has run -- the same ordering that made
    # create_image_with_text load its own font.
    #
    # No lock: two threads racing here both draw the same bytes and the
    # tuple assignment is atomic, so the only cost of losing the race is
    # drawing twice once.
    _no_camera_cache = None

    def create_no_camera_image(self, image_size=None):
        """The "no camera" placeholder: black screen, "NO SIGNAL" in gray.

        Matches ustreamer's own --blank default look on purpose. The two
        placeholders mean different things -- ustreamer's fires when the
        camera DEVICE is offline, this one when this plugin failed to fetch
        a frame (customSnapshotURL misconfigured, a network hiccup; the
        camera can be live and streaming fine while this still shows) -- but
        there is no reason for the two failure layers to look unrelated to
        whoever is staring at the webcam view.

        Drawn rather than loaded, replacing a packaged static/no_camera.jpg
        that was 432x360 -- a different aspect ratio from every other picture
        the plugin produces, on the one image shown when nothing works. This
        draws at the processing size and cannot go missing (both former call
        sites carried a file-not-found branch).

        All geometry is a fraction of the shorter side, so it is correct at
        any size and any aspect ratio.
        """
        if image_size is None:
            image_size = (self.proc_img_width, self.proc_img_height)
        scale = self.NO_CAMERA_SUPERSAMPLE
        width, height = image_size[0] * scale, image_size[1] * scale
        side = min(width, height)
        image = Image.new("RGB", (width, height), (0, 0, 0))
        draw = ImageDraw.Draw(image)

        text = "NO SIGNAL"
        gray_white = (176, 176, 176)
        font = self.load_font(int(round(side * 0.09)))
        mask = font.getmask(text)
        bbox = mask.getbbox()
        if bbox is None:
            x, y = width / 2.0, height / 2.0
        else:
            x = (width - (bbox[2] - bbox[0])) / 2.0 - bbox[0]
            y = (height - (bbox[3] - bbox[1])) / 2.0 - bbox[1]
        draw.text((x, y), text, fill=gray_white, font=font)

        return image.resize(image_size, Image.LANCZOS)

    def encode_image_to_jpeg_bytes(self, image):
        """A PIL image as plain JPEG bytes, for /frame.jpg.

        No base64: encoding it inflated every transfer by 33% and cost a
        decode on the other side, which is why the live view moved to
        binary. convert("RGB") is defensive -- placeholder images can be
        RGBA/L, and JPEG cannot store either.
        """
        buffered = BytesIO()
        image.convert("RGB").save(buffered, format="JPEG")
        return buffered.getvalue()

    def encode_image_to_base64(self, image):
        """
        Encodes a PIL Image object to a base64 string for easy embedding or storage.
        """
        buffered = BytesIO()
        image.convert("RGB").save(buffered, format="JPEG")
        encoded = base64.b64encode(buffered.getvalue()).decode("utf-8")
        return "data:image/jpeg;base64," + encoded


# ---- CameraSourceFactory ---------------------------------------------------

class CameraSourceUnavailable(Exception):
    """No FrameSource can be built from the current configuration.

    Mirrors nozcam_backend.BackendUnavailable's shape: ONE exception a
    caller can catch regardless of which underlying reason applies --
    a custom URL that does not answer, or answers with neither MJPEG
    nor a still image; OctoPrint has no snapshot webcam configured at
    all; or its designated webcam has neither a compatible MJPEG
    stream nor a snapshot URL.
    """


def _classify_content_type(content_type):
    """The base media type of a Content-Type header value: lower-cased
    and stripped of every parameter (boundary, charset, ...).

    Same email.message.Message technique mjpegstream.parse_boundary()
    already relies on for the identical reason (it lower-cases the
    type and strips quoting for free, verified interactively there
    before being trusted) -- but deliberately not that function
    itself: a classification probe must accept
    multipart/x-mixed-replace even when the boundary parameter is
    missing or malformed. A genuinely missing boundary belongs to
    MjpegFrameSource's own real parse_boundary() call, once it opens
    its own connection, as a connection error subject to that
    source's own reconnect/backoff -- not here, where it would only
    stop the right FrameSource from ever being constructed at all.
    """
    message = Message()
    message["content-type"] = content_type or ""
    return message.get_content_type()


def _classify_custom_url(url):
    """Classify an HTTP(S) URL as "mjpeg" or "snapshot"."""
    response = requests.get(
        url, stream=True,
        timeout=(framesource.SNAPSHOT_CONNECT_TIMEOUT,
                 framesource.SNAPSHOT_READ_TIMEOUT))
    try:
        response.raise_for_status()
        media_type = _classify_content_type(
            response.headers.get("Content-Type", ""))
    finally:
        response.close()
    if media_type == "multipart/x-mixed-replace":
        return "mjpeg"
    if media_type.startswith("image/"):
        return "snapshot"
    raise ValueError(
        "unrecognised Content-Type %r -- neither an MJPEG stream nor "
        "a still image" % media_type)


def _grab_one_mjpeg_frame(
        url, logger=None,
        timeout=framesource.MJPEG_CONNECT_TIMEOUT
        + framesource.MJPEG_STALL_TIMEOUT):
    """One decoded frame from the MJPEG stream at `url`, or raise.

    get_snapshot()'s counterpart to _fetch_snapshot_image for a custom
    URL _classify_custom_url has identified as "mjpeg" rather than
    "snapshot": _fetch_snapshot_image does one bounded GET and decodes
    the body as a single JPEG, which is the wrong read for an endless
    multipart/x-mixed-replace stream -- it would simply run until
    SNAPSHOT_MAX_BYTES/SNAPSHOT_DEADLINE trips and raise IOError,
    never returning a frame. This reuses framesource.MjpegFrameSource
    -- the same reader CameraSourceFactory builds for the sampler's
    long-lived case -- for a single, throwaway grab instead of writing
    a second, ad-hoc stream reader: build it, start it, wait once for
    its first frame, and close it again regardless of outcome, so
    nothing here ever leaves a reader thread running past this
    function's own return.

    Raises IOError if no frame arrives within `timeout`. That single
    outcome covers two different situations indistinguishably: a
    stream that connects fine but never completes one multipart part,
    and a connection that fails outright (refused, DNS failure, HTTP
    error, ...) -- because MjpegFrameSource's reader thread
    (framesource.py) absorbs every connection/HTTP failure into its
    own internal reconnect/backoff loop and retries forever rather
    than raising, so the only signal this function ever gets that
    `url` is not answering usably is wait_next() itself returning None
    once `timeout` elapses, exactly as it would for a stream that is
    simply silent. This also raises whatever _decode_source_image
    raises on the rare part that arrives corrupt: MjpegFrameSource
    already discards a part that fails its own cheap JPEG-looking
    check without ever publishing it, so this only fires if that check
    itself passed on something Pillow still cannot decode.

    `timeout` defaults to MJPEG_CONNECT_TIMEOUT + MJPEG_STALL_TIMEOUT
    (framesource.py) -- this project's existing worst-case bound on
    one MJPEG connection attempt, reused rather than a fresh number of
    its own. It happens to equal CameraMixin.SNAPSHOT_DEADLINE (20 s),
    this same method's own bound for one plain snapshot fetch.

    `logger`, when given, is used for MjpegFrameSource's own
    reconnect/recovery log lines instead of framesource.py's module
    logger -- get_snapshot() passes its instance's real self._logger,
    the same choice CameraSourceFactory already makes and for the same
    reason: those lines then reach octoprint.log through the plugin's
    own configured logger instead of a logger nothing has configured.

    A separate, reusable function rather than inlined into
    get_snapshot(), because a later "Test Camera: read one MJPEG part"
    feature needs the identical capability.
    """
    identity = CameraMixin._url_fingerprint(url)
    # geometry_provider is never read by MjpegFrameSource itself (see
    # that class's own __init__ docstring) -- this SourceSpec is
    # throwaway, and nothing here ever needs a geometry callback.
    spec = framesource.SourceSpec("mjpeg", identity, url, None)
    source = framesource.MjpegFrameSource(spec, logger=logger)
    try:
        source.start()
        frame = source.wait_next(0, threading.Event(), timeout)
    finally:
        source.close()
    if frame is None:
        raise IOError(
            "no frame received from MJPEG stream within %.0f s"
            % timeout)
    return framesource._decode_source_image(frame.jpeg_bytes)


def _is_hls_or_webrtc_stream_url(url):
    """True if `url` is HLS or WebRTC -- OctoPrint's own webcam UI
    switches players on these same markers, and neither is something
    an <img> tag, or this project's own MJPEG multipart parser, can
    read.

    Parsed, not string-matched: a bare `endswith(".m3u8")` misses a
    query-stringed URL like `/stream.m3u8?token=...`, and a bare
    `startswith("webrtc")` would match a path that merely began with
    those letters -- the exact finding ApiMixin._stream_info()
    (api.py) already recorded for this identical check. SHARED with
    that method, not re-derived here, so the two can never disagree
    about what counts as either.
    """
    parts = urlparse(url)
    return (parts.scheme.lower().startswith("webrtc")
            or parts.path.lower().endswith(".m3u8"))


def _is_fetchable_stream_url(url):
    """True if CameraSourceFactory can open its OWN HTTP connection to
    `url` as an MJPEG stream: an absolute http(s) URL that is neither
    HLS nor WebRTC.

    RELATIVE URL by default (e.g. "/webcam/?action=stream") -- meant
    to be resolved by the BROWSER against the page origin, the same
    way ApiMixin._stream_info() hands it straight to an <img> tag.
    This factory has no page origin to resolve a relative URL
    against: requests.get() on one raises MissingSchema (a
    requests.RequestException subclass), which MjpegFrameSource's
    reconnect loop (framesource.py) treats as an ordinary transient
    connection failure and retries forever -- logging only the
    exception's type name, never its message -- while
    compat.snapshot, which WOULD work, is never even tried. A
    relative (or HLS/WebRTC) compat.stream must fall through to
    compat.snapshot instead of being accepted as MJPEG at all.
    """
    parts = urlparse(url)
    if parts.scheme.lower() not in ("http", "https"):
        return False
    return not _is_hls_or_webrtc_stream_url(url)


class CameraSourceFactory(object):
    """Build one frame source from a custom URL or OctoPrint webcam config."""

    def __init__(self, plugin, logger=None):
        """Bind the plugin configuration used to select a frame source."""
        self._plugin = plugin
        self._logger = logger or getattr(plugin, "_logger", None) \
            or logging.getLogger(__name__)

    def create(self):
        """Return a new unstarted source or raise CameraSourceUnavailable."""
        custom_url = self._plugin.custom_snapshot_url
        if custom_url:
            return self._from_custom_url(custom_url)
        return self._from_provider()

    def _from_custom_url(self, url):
        """Build a source from the configured custom URL."""
        identity = self._plugin._url_fingerprint(url)
        geometry = self._plugin._snapshot_geometry_now
        if url.startswith("file://"):
            spec = framesource.SourceSpec("file", identity, url, geometry)
            self._logger.info("Camera source: static file (%s)", identity)
            return framesource.StaticFileFrameSource(
                spec, logger=self._logger)
        try:
            kind = _classify_custom_url(url)
        except ValueError as exc:
            # This controlled error contains a response header, not the URL.
            raise CameraSourceUnavailable(
                "the custom camera URL did not answer usably: %s"
                % exc) from exc
        except requests.RequestException as exc:
            # Exception text may contain URL credentials.
            raise CameraSourceUnavailable(
                "the custom camera URL did not answer usably: %s"
                % type(exc).__name__) from exc
        spec = framesource.SourceSpec(kind, identity, url, geometry)
        if kind == "mjpeg":
            self._logger.info(
                "Camera source: custom URL, MJPEG stream (%s)", identity)
            return framesource.MjpegFrameSource(spec, logger=self._logger)
        self._logger.info(
            "Camera source: custom URL, HTTP snapshot (%s)", identity)
        return framesource.HttpSnapshotFrameSource(spec, logger=self._logger)

    def _from_provider(self):
        """Build a source from OctoPrint's designated snapshot webcam."""
        try:
            import octoprint.webcams
            provided = octoprint.webcams.get_snapshot_webcam()
        except Exception as exc:
            # Keep the user-facing error independent of provider internals.
            self._logger.debug("get_snapshot_webcam failed: %s", exc)
            raise CameraSourceUnavailable(
                "could not ask OctoPrint for its designated snapshot "
                "webcam: %s" % type(exc).__name__) from exc
        config = getattr(provided, "config", None)
        if config is None:
            raise CameraSourceUnavailable(
                "no snapshot webcam is configured in OctoPrint")
        name = getattr(config, "name", "") or "?"
        camera = getattr(provided, "providerPlugin", None)
        provider_detail = "%s/%s" % (
            getattr(camera, "_identifier", "?"), name)
        geometry = self._plugin._snapshot_geometry_now
        compat = getattr(config, "compat", None)
        stream_url = getattr(compat, "stream", None)
        if stream_url and not _is_fetchable_stream_url(stream_url):
            # Relative, HLS and WebRTC stream URLs are not MJPEG sources.
            self._logger.debug(
                "Camera source: OctoPrint webcam %s's stream URL is "
                "not directly fetchable (relative, HLS or WebRTC); "
                "trying its snapshot URL instead", provider_detail)
            stream_url = None
        if stream_url:
            identity = self._plugin._url_fingerprint(stream_url)
            spec = framesource.SourceSpec(
                "mjpeg", identity, stream_url, geometry)
            self._logger.info(
                "Camera source: OctoPrint webcam %s, MJPEG stream",
                provider_detail)
            return framesource.MjpegFrameSource(spec, logger=self._logger)
        snapshot_url = getattr(compat, "snapshot", None)
        if snapshot_url:
            identity = self._plugin._url_fingerprint(snapshot_url)
            spec = framesource.SourceSpec(
                "snapshot", identity, snapshot_url, geometry)
            self._logger.info(
                "Camera source: OctoPrint webcam %s, HTTP snapshot",
                provider_detail)
            return framesource.HttpSnapshotFrameSource(
                spec, logger=self._logger)
        raise CameraSourceUnavailable(
            "OctoPrint's designated snapshot webcam %s has neither a "
            "compatible MJPEG stream nor a snapshot URL" % provider_detail)
