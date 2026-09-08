"""The undetect zone, and the coordinate system it lives in.

The mask is a grid the user paints on ONE camera frame. Everything here
exists because that frame can change underneath it: a different webcam
provider, a flip or rotation turned on, a different aspect ratio. The
signature machinery records which frame a mask was drawn against, detects
when that no longer matches, and converts the grid where the conversion is
exact.
"""

import math

from PIL import ImageDraw

from .settings_schema import MASK_GRID

# Re-exported: it is DECLARED in settings_schema, which owns it because it
# fixes the on-disk format of the maskImageData setting, and which must
# import nothing from this package to stay loadable by tree-level tools.
# Callers still reach it here, where the code that uses it lives.
MASK_GRID = MASK_GRID
# How far two aspect ratios may drift before they count as different
# cameras. Absolute, on the ratio itself: it has to absorb the rounding
# between equivalent resolutions (1920/1080 and 1280/720 both 1.7778)
# while still separating every ratio a webcam actually offers -- the
# closest pair in practice is 4:3 = 1.3333 against 5:4 = 1.25.
MASK_ASPECT_TOL = 0.02


def _grid_flip_h(grid):
    """Mirror left-right, i.e. Image.FLIP_LEFT_RIGHT on the frame."""
    return [row[::-1] for row in grid]


def _grid_flip_v(grid):
    """Mirror top-bottom, i.e. Image.FLIP_TOP_BOTTOM on the frame."""
    return grid[::-1]


def _grid_rot90_ccw(grid):
    """Rotate counter-clockwise, i.e. Image.rotate(90, expand=True).

    NOT a plain transpose. A transpose is a reflection about the main
    diagonal, which is orientation-reversing; a rotation is a transpose
    followed by reversing the row order. Verified against Pillow:
    [[a, b], [c, d]] rotates to [[b, d], [a, c]].
    """
    size = len(grid)
    return [[grid[c][size - 1 - r] for c in range(size)]
            for r in range(size)]


def _grid_rot90_cw(grid):
    """Rotate clockwise -- the inverse of _grid_rot90_ccw()."""
    size = len(grid)
    return [[grid[size - 1 - c][r] for c in range(size)]
            for r in range(size)]


def _grid_to_string(grid):
    """Encode a boolean grid back into the stored '0'/'1' string."""
    return "".join("1" if cell else "0" for row in grid for cell in row)


class MaskMixin:
    """Mixed into PinozcamPlugin; see the module docstring."""

    def _camera_signature(self):
        """The coordinate system the next snapshot will arrive in.

        aspect is the ratio of the SOURCE frame, i.e. before rotate90
        swaps the axes, and is None until a frame has actually been seen.
        Storing the post-transform ratio instead would be actively
        harmful: toggling rotate90 would move the ratio AND the flag at
        once, so a plain rotation would look like a camera swap -- the one
        case that must NOT be auto-corrected.
        """
        source, detail, flip_h, flip_v, rotate = self._snapshot_geometry_now()
        seen = self.frame_geometry or {}
        aspect = None
        if seen.get("source") == source and seen.get("detail") == detail:
            # A ratio measured on a different camera says nothing about
            # this one, and pretending it does would report an aspect
            # change when the real change is the camera.
            aspect = seen.get("aspect")
        return {
            "source": source,
            "detail": detail,
            "aspect": aspect,
            "flipH": flip_h,
            "flipV": flip_v,
            "rotate90": rotate,
        }

    def _describe_signature(self, signature):
        """One line for the log and for the tab. Human-readable only."""
        flips = [
            name for name in ("flipH", "flipV", "rotate90")
            if signature.get(name)
        ]
        aspect = signature.get("aspect")
        # now, which tells a reader nothing, and before that it was the URL --
        # which is exactly what must not appear in a warning that reaches the
        # browser and octoprint.log. For a provider it is "identifier/name",
        # which is what the user needs to see to know WHICH camera moved.
        detail = signature.get("detail")
        if signature.get("source") == "custom":
            detail = None
        return "{}{}, aspect {}, {}".format(
            signature.get("source") or "?",
            " " + detail if detail else "",
            f"{aspect:.3f}" if aspect else "unknown",
            "+".join(flips) if flips else "no flip",
        )

    def _stored_mask_signature(self):
        """Return the stored mask signature, normalizing legacy custom URLs."""
        stored = self._settings.get(["maskSignature"])
        if not (isinstance(stored, dict) and stored):
            return None
        detail = stored.get("detail") or ""
        if stored.get("source") == "custom" and "://" in detail:
            stored = dict(stored)
            stored["detail"] = self._url_fingerprint(detail)
        return stored

    def _store_mask_signature(self, signature):
        """Persist a changed signature and save settings."""
        if signature == self._settings.get(["maskSignature"]):
            return
        self._settings.set(["maskSignature"], signature)
        self._settings.save()

    def _retarget_mask(self, data, old, new):
        """Move a mask grid from one flip/rotate setting to another.

        The mask was drawn on a frame that already had the OLD transforms
        applied, so its coordinates are T_old(source); it has to end up as
        T_new(source). The grid therefore goes through T_new . T_old^-1.

        transform_image() applies flipH, flipV, rotate90, so T = R . V . H
        and T^-1 = H . V . R^-1 (both flips are involutions). Read
        right-to-left: undo the OLD transforms in REVERSE order (rotation,
        flipV, flipH), then apply the NEW ones FORWARD. Six steps.

        flip+rotate change silently wrong -- they do not commute.
        """
        grid = self._mask_matrix(data)
        if old.get("rotate90"):
            grid = _grid_rot90_cw(grid)
        if old.get("flipV"):
            grid = _grid_flip_v(grid)
        if old.get("flipH"):
            grid = _grid_flip_h(grid)
        if new.get("flipH"):
            grid = _grid_flip_h(grid)
        if new.get("flipV"):
            grid = _grid_flip_v(grid)
        if new.get("rotate90"):
            grid = _grid_rot90_ccw(grid)
        return _grid_to_string(grid)

    def _camera_moved(self, old, new):
        """True when the frame changed in a way the mask cannot follow."""
        if (old.get("source") != new.get("source")
                or old.get("detail") != new.get("detail")):
            return True
        old_aspect, new_aspect = old.get("aspect"), new.get("aspect")
        if not old_aspect or not new_aspect:
            # One side predates any frame being fetched. Unknown is not
            # the same as different: claiming a change here would nag
            # every user whose mask was drawn before the ratio was known.
            return False
        return abs(old_aspect - new_aspect) > MASK_ASPECT_TOL

    def _flips_moved(self, old, new):
        """True if flip or rotate differs between two mask signatures."""
        return any(old.get(name) != new.get(name)
                   for name in ("flipH", "flipV", "rotate90"))

    def _sync_mask_signature(self):
        """Align stored signature and grid with the current frame in one lock."""
        with self.mask_lock:
            current = self._camera_signature()
            stored = self._stored_mask_signature()

            if not self.mask_image_data or '1' not in self.mask_image_data:
                self.mask_warning = None
                self._store_mask_signature(current)
                return

            if not stored:
                self.mask_warning = None
                self._store_mask_signature(current)
                self._logger.info(
                    "Undetect Zone carries no coordinate-system signature "
                    "(drawn by an earlier version); assuming it matches the "
                    "current frame: %s.", self._describe_signature(current))
                return

            if self._camera_moved(stored, current):
                warning = (
                    f"Undetect Zone was drawn on a different camera frame ({self._describe_signature(stored)}) "
                    f"than the one being analysed now ({self._describe_signature(current)}). It cannot be "
                    "converted automatically -- please redraw it."
                )
                # Only log on a change of state: this runs whenever the
                # observed frame geometry moves, and a camera that alternates
                # resolutions would otherwise fill the log.
                if warning != self.mask_warning:
                    self._logger.warning("%s", warning)
                self.mask_warning = warning
                return

            self.mask_warning = None
            if not self._flips_moved(stored, current):
                self._store_mask_signature(current)
                return

            previous = self.mask_image_data
            self.mask_image_data = self._retarget_mask(previous, stored, current)
            self._mask_before_retarget = previous
            # Both keys then one save, rather than _store_mask_signature():
            # the grid and the signature it belongs to must never reach
            # config.yaml separately.
            self._settings.set(["maskImageData"], self.mask_image_data)
            self._settings.set(["maskSignature"], current)
            self._settings.save()
            self._logger.info(
                "Undetect Zone realigned from %s to %s; the transform is exact, "
                "no redraw needed.", self._describe_signature(stored),
                self._describe_signature(current))
            # The grid now describes the NEW orientation, so any candidate
            # frame captured in the old one would be masked with a zone
            # that has moved out from under it. This path is reached when
            # the flips change outside the plugin's own settings dialog --
            # OctoPrint's webcam settings -- so on_settings_save does not
            # run and nothing else would drop those frames. The live
            # view's published frame is from the old orientation too.
            self.frame_buffer.bump_epoch()
            # above already said the published frame is from the old
            # orientation too -- and the code then dropped only the camera
            # frame, leaving that published frame and its cached JPEG to
            # be served under a mask that had moved out from under them.
            self.frames.invalidate()

    def _note_source_frame(self, img, geometry_now=None):
        """Record the geometry of a frame we were fetching anyway.

        Called with the SOURCE image, before transform_image() touches it,
        so the aspect needs no un-rotating. Every frame path passes through
        here, which is why the signature never costs a fetch of its own --
        and why a flip toggled in OctoPrint's OWN webcam settings gets
        noticed, since that never reaches our on_settings_save().

        ACTUALLY produced `img`, and the provider path must pass it.
        Without it this predicts, and a prediction names the DESIGNATED
        snapshot webcam -- so when that one is down and a spare answered,
        the frame is recorded under the wrong camera's flips and the
        Undetect Zone realigned into a coordinate system the picture never
        had. Prediction is still right for the paths with no per-frame
        config to offer (custom URL, legacy snapshot URL).
        """
        width, height = img.size
        if not width or not height:
            return
        source, detail, flip_h, flip_v, rotate = (
            geometry_now if geometry_now is not None
            else self._snapshot_geometry_now())
        geometry = {
            "source": source,
            "detail": detail,
            "aspect": width / float(height),
            "flipH": flip_h,
            "flipV": flip_v,
            "rotate90": rotate,
        }
        # frame_geometry, deciding it moved, and only then taking a lock would
        # let a second thread pass the same test and retarget the grid again.
        with self.mask_lock:
            if geometry == self.frame_geometry:
                return
            previous = self.frame_geometry
            self.frame_geometry = geometry
            # A different SOURCE or a different aspect is a different
            # camera as far as the statistics are concerned -- this is the
            # same test the mask uses. The flag is consumed by the
            # detection thread (detect.py), which resets the ratio window
            # and the evidence; raising it here and acting there keeps
            # this thread out of the detection lock. Flips and rotations
            # are deliberately NOT a camera change: the grid realigns
            # exactly and the scene is the same scene.
            if previous is not None and self._camera_moved(previous,
                                                           geometry):
                self.camera_changed = True
                # transaction. Only the flip/rotate branch of
                # _sync_mask_signature bumped the epoch, so a camera swap
                # left camera A's candidates in the buffer -- and the
                # detector, having just reset its window, picked one of them
                # (Top-N selection prefers a sharp frame regardless of which
                # camera produced it) and counted it as the new camera's
                # first measurement.
                #
                # Deferring this to the detection thread with the flag would
                # not do: the sampler keeps putting frames, and the window
                # is reset there, so the very first frame of the fresh
                # window is the one most likely to be stale.
                self.frame_buffer.bump_epoch()
                self.frames.invalidate()
            self._note_source_frame_locked()

    def _note_source_frame_locked(self):
        """Reconcile the mask with the geometry just recorded. Holds the lock."""
        try:
            self._sync_mask_signature()
        except Exception as exc:
            # A signature problem must never break camera capture: on the
            # WebcamProviderPlugin path this call sits inside
            # get_snapshot()'s own try/except, where an exception would be
            # reported as a failed snapshot and skip to the next camera.
            # Logged with a traceback, not swallowed.
            self._logger.exception(
                "Undetect Zone signature check failed: %s", exc)

    def _mask_matrix(self, data=None):
        """Decode the stored mask string into a square boolean matrix.

        The grid resolution is derived from the string length rather than
        hardcoded, so a settings file written by an older version (64x64
        = 4096 chars) still loads. Anything smaller than MASK_GRID is
        upscaled by nearest-neighbour, which is exact for power-of-two
        ratios and means users never have to redraw their mask.
        """
        data = self.mask_image_data if data is None else data
        if not data:
            return [[False] * MASK_GRID for _ in range(MASK_GRID)]
        size = int(math.sqrt(len(data)))
        if size * size != len(data) or size == 0:
            self._logger.warning(
                "Mask data length %d is not a square; ignoring mask.",
                len(data),
            )
            return [[False] * MASK_GRID for _ in range(MASK_GRID)]
        rows = [
            [data[r * size + c] == '1' for c in range(size)]
            for r in range(size)
        ]
        if size == MASK_GRID:
            return rows
        return [
            [rows[r * size // MASK_GRID][c * size // MASK_GRID]
             for c in range(MASK_GRID)]
            for r in range(MASK_GRID)
        ]

    def apply_mask_to_image(self, input_image):
        """Return a copy with mask cells filled by black pixels."""
        # only ever set when the zone was drawn on a different camera or
        # aspect (see _sync_mask_signature) -- coordinates in a system the
        # current frame does not have. Painting them anyway blacks out an
        # arbitrary region of the new picture, and a failure inside that
        # region is invisible: a silent detection hole. Suspending the mask
        # trades that for the false positives the zone was drawn to hide,
        # which the user can see and fix; the warning in the UI already
        # says to redraw. Flips/rotations never set the warning -- the grid
        # realigns exactly and stays in force.
        masked = input_image.convert("RGB")
        if self.mask_warning:
            return masked
        mask_matrix = self._mask_matrix()
        if not any(any(row) for row in mask_matrix):
            return masked

        draw = ImageDraw.Draw(masked)
        width, height = masked.size
        for row in range(MASK_GRID):
            # Compute both edges from the exact ratio instead of
            # multiplying a ceil()'d block size, which overran the image
            # by up to 63 px on heights that are not a multiple of the
            # grid (e.g. 1080 -> 64 * ceil(1080/64) = 1088).
            y1 = height * row // MASK_GRID
            y2 = height * (row + 1) // MASK_GRID
            col = 0
            while col < MASK_GRID:
                if not mask_matrix[row][col]:
                    col += 1
                    continue
                # Merge horizontally adjacent cells into one rectangle:
                # 128x128 cells would otherwise be up to 16384 draw
                # calls per frame on a 1.0 GHz A53.
                start = col
                while col < MASK_GRID and mask_matrix[row][col]:
                    col += 1
                x1 = width * start // MASK_GRID
                x2 = width * col // MASK_GRID
                # A frame smaller than the grid collapses cells to zero
                # extent -- 100 px of height over 128 rows leaves 28 rows
                # with y1 == y2 -- and Pillow rejects a rectangle whose
                # second corner precedes its first. Skipping them is
                # correct: sub-pixel cells have nothing to cover, and the
                # mask simply becomes as coarse as the frame allows.
                if x2 > x1 and y2 > y1:
                    draw.rectangle((x1, y1, x2 - 1, y2 - 1),
                                   fill=(0, 0, 0))

        return masked
