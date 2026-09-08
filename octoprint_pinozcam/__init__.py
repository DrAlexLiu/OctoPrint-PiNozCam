import os
import threading
import time
from collections import deque
import octoprint.plugin
import octoprint.access
from octoprint.events import Events
from .framebuffer import FrameBuffer
from .framestore import FrameStore
from . import settings_schema as schema
from .nozcam_backend import BackendUnavailable, NozcamBackend

from .channels import ChannelManager
from .confirm import ConfirmMixin
# Preserve the package-level mask-helper API.
from .mask import (MASK_GRID, MASK_ASPECT_TOL, MaskMixin, _grid_flip_h,
                   _grid_flip_v, _grid_rot90_ccw, _grid_rot90_cw,
                   _grid_to_string)
from .camera import CameraMixin
from .detect import DetectMixin
from .notify import NotifyMixin
from .config import ConfigMixin
from .api import ApiMixin

__all__ = [
    "PinozcamPlugin",
    "MASK_GRID", "MASK_ASPECT_TOL",
    "_grid_flip_h", "_grid_flip_v", "_grid_rot90_ccw", "_grid_rot90_cw",
    "_grid_to_string",
]


class PinozcamPlugin(ConfirmMixin,
                     MaskMixin,
                     CameraMixin,
                     DetectMixin,
                     NotifyMixin,
                     ConfigMixin,
                     ApiMixin,
                     octoprint.plugin.StartupPlugin,
                     octoprint.plugin.ShutdownPlugin,
                     octoprint.plugin.TemplatePlugin,
                     octoprint.plugin.WizardPlugin,
                     octoprint.plugin.SettingsPlugin,
                     octoprint.plugin.AssetPlugin,
                     octoprint.plugin.BlueprintPlugin,
                     octoprint.plugin.EventHandlerPlugin):
    """Assemble the plugin's focused mixins and OctoPrint interfaces.

    Mixins precede OctoPrint base classes so their hook implementations win
    method-resolution order.
    """

    def __init__(self):
        """Initialise state without touching the camera, printer or network."""
        self.lock = threading.Lock()
        # Never join a worker while holding the state lock it needs.
        self.thread_lock = threading.Lock()
        self.stop_event = threading.Event()
        # Linearise run retirement with printer actions under a dedicated lock.
        self.action_gate = threading.Lock()
        self.run_generation = 0
        self.ai_thread = None
        self.ai_starter = None
        # "A worker should exist at all" -- distinct from stop_event, which a
        # starter legitimately clears. The only thing the starter acts on.
        self.want_ai_running = False
        # Whether a starter is between "spawned" and "returned". NOT derived
        # from ai_starter.is_alive(): a starter that has decided to give up is
        # still alive for a moment, and in that window nobody would spawn.
        self.ai_starter_active = False
        # Which starter owns ai_starter_active, so one that is finishing
        # cannot clear the flag of the starter that replaced it.
        self.ai_starter_token = 0
        # Set once, by on_shutdown. The starter has to tell "this print
        # ended" from "the plugin is going away".
        self.shutting_down = threading.Event()

        # Mirror schema defaults until OctoPrint loads persisted settings.
        for _setting in schema.SETTINGS:
            if _setting.attr is None:
                continue
            _value = _setting.default
            if _setting.parse is not None:
                _value = _setting.parse(_value, None)
            elif _setting.kind is bool:
                _value = bool(_value)
            setattr(self, _setting.attr, _value)
        self.discord_bot = None
        # Mute applies to every channel and resets for each print.
        self.alerts_muted = False

        # Confirmation nonces are scoped, single-use and lock-protected.
        self.confirm_lock = threading.Lock()
        # Guard complete mask-retarget transactions; helpers may re-enter.
        self.mask_lock = threading.RLock()
        self.confirm_tokens = {}
        self.confirm_scope = 0

        self.notify_met = False

        self.mask_image_data = '0' * (MASK_GRID * MASK_GRID)
        # Set when the Undetect Zone can no longer be trusted against the
        # current frame. Polled by the tab through /check -- a warning that
        # only reaches the log is one the user never sees. While set, the
        # mask is also NOT applied (mask.py): coordinates drawn on a
        # different camera would black out an arbitrary region of this one.
        self.mask_warning = None
        # Raised by the geometry watcher when the frame SOURCE or its
        # aspect moved -- a different camera as far as statistics are
        # concerned. Consumed by the detection thread, which resets the
        # ratio window and the evidence rather than mixing two cameras'
        # frames in one measurement.
        self.camera_changed = False
        # Only one speed-test daemon may run at a time.
        self._test_inference_lock = threading.Lock()
        # Coordinate system of the last SOURCE frame:
        # {source, detail, aspect, flipH, flipV, rotate90}.
        self.frame_geometry = None
        # The mask as it was BEFORE an automatic realignment, so a browser
        # echoing back its stale grid can be told apart from a redraw.
        self._mask_before_retarget = None
        self.count = 0
        self.welcome_text = "Welcome to PiNozCam!"
        self.proc_img_width = 640
        self.proc_img_height = 384
        self.font = None
        self.ai_running = False
        self.num_threads = 1
        self.ai_cpus = None
        self.ai_input_image = None
        self.backend = None
        self.backend_kind = None
        self.backend_error = None
        # Everything the browser can be shown: the newest analysed frame
        # as DATA (PIL frame, boxes normalised to 0..1, an increasing id),
        # the two it replaced, the shared JPEG cache, and the sampler's
        # most recent grab. Nothing is drawn or encoded until asked --
        # /check reports metadata, /frame.jpg encodes on demand -- and
        # there is ONE invalidate for the lot. See framestore.py.
        self.frames = FrameStore()
        # True exactly while a sampler thread is alive. NOT derived from
        # ai_running, which is true through the preflight and the whole
        # aiStartDelay. The live view reads it to know who owns the camera.
        self.sampler_active = False
        # Which sampler generation owns the camera; only that generation may
        # clear sampler_active. See _sample_frames.
        self.sampler_token = None
        # Let the detector's outer boundary stop its active sampler.
        self._active_sampler_stop = None
        # Bumped by config.py's on_settings_save, ONLY when
        # customSnapshotURL actually changes value -- never on an
        # unrelated save. The sampler compares this against the
        # generation its currently-held FrameSource was built under
        # and rebuilds on a mismatch (detect.py _sample_frames), so a
        # camera URL changed mid-print takes effect within one tick
        # instead of waiting for the next print. Separate from
        # settings_generation below, which is about a DECISION's
        # parameters staying coherent, not camera identity.
        self._camera_source_generation = 0
        # Camera-outage watch (detect.py _camera_watch).
        self.camera_ok_at = time.monotonic()
        self.camera_alerted = False
        # .name of the provider config that served the last snapshot, or
        # None. The Live Camera toggle hides itself on any mismatch.
        self.snapshot_source_name = None
        # name -> last message logged and how many identical failures
        # followed, so a down camera does not write a line every tick.
        self._camera_failures = {}
        # Time-pruned history sized for the maximum configured window.
        self.ai_results = deque(maxlen=20000)
        # Candidate frames waiting for the detector; see framebuffer.py.
        self.frame_buffer = FrameBuffer(
            capacity=self.frame_buffer_capacity,
            max_age=self.frame_buffer_max_age)
        # Seconds between samples. No zero/off value -- the sampler is the
        # detector's only frame source, and pausing is Enable AI's job.
        self.frame_sample_interval = 0.2
        self.last_elapsed_time = None
        # Alarming frames worth keeping a picture of, a min-heap keyed on
        # time (detect.py _push_evidence) so the cap evicts the OLDEST
        # entry once EVIDENCE_KEEP is exceeded.
        self.evidence = []
        self.evidence_seq = 0
        self.snapshot_cache = None
        # Single-flight refresh for the between-print live view.
        self._live_snapshot_lock = threading.Lock()
        self._live_snapshot_thread = None
        # A frame is alarming when its raw detected area exceeds the Failure
        # Area Threshold; the criterion is the fraction of alarming frames in
        # the window.
        self.detect_started_at = None
        self.detect_frames = 0
        self.last_ratio = None
        # Edge-trigger notifications while the criterion remains true.
        self.criterion_met = False
        # Highest printer action already applied in the current episode.
        self.episode_action_level = 0
        self.notification_reach_to_max = False
        self.notifications_sent = 0
        # monotonic() of the last alert that actually reached someone, or
        # None. What notifyInterval is measured from.
        self.last_notify_at = None
        # Consecutive frames whose every channel refused. Bounded by
        # NOTIFY_ATTEMPTS so a dead transport is not retried all print.
        self.notify_attempts = 0
        # Pending alert IDs map to the print generation that owns them.
        self.alerts_inflight = {}
        self.alert_seq = 0
        self.setting_change_while_printing = False
        # Bumped by every settings save. A frame records it with its verdict
        # and re-checks before the printer is touched, so a decision made
        # under one parameter set is never executed under another.
        self.settings_generation = 0
        self.current_telegram_message_set = set()
        self.current_telegram_message_paused = False
        self.telegram_server_running = False
        # Declared here so teardown can check it unconditionally.
        # A TelegramBot, or None. It owns the telebot instance and the
        # polling thread; the plugin only says start, stop and send.
        self.telegram_bot = None
        # Why the Telegram chip is red, or None when it is not. Set by
        # telegram_verify, which runs after setup rather than gating it.
        self.telegram_error = None
        # generations, the pending markers, the two locks and the worker --
        # lives in ChannelManager. This object supplies only what a channel
        # IS: how to rebuild one, how to verify one, and the two halves of
        # a welcome. Splitting the welcome in two is deliberate; see
        # channels.py.
        self.channels = ChannelManager(
            get_logger=lambda: self._logger,
            shutting_down=self.shutting_down,
            setup=self._setup_channel,
            verify=self._verify_channel,
            welcome_image=self._welcome_image,
            welcome_send=self._welcome_send)
        # name -> (Queue, worker thread). One resident worker per medium, so
        # a slow Telegram cannot delay a Discord alert and a broken network
        # cannot accumulate threads. Created on first use.
        self.notify_queues = {}
        self.notify_queue_lock = threading.Lock()

        self.plugin_dir = os.path.dirname(__file__)
        self.cameras = []
        self.snap_new_method = False

    def cpu_is_raspberry_pi(self):
        """True when /proc/cpuinfo says this is a Raspberry Pi."""
        try:
            with open("/proc/cpuinfo") as f:
                return "Raspberry Pi" in f.read()
        except OSError:
            return False

    # Thermal zone `type` values meaning "this is the CPU", in preference
    # order: zone0 is the GPU or the PMIC on some SoCs, and reporting those
    # would be quietly wrong rather than merely unavailable.
    CPU_THERMAL_TYPES = ("cpu-thermal", "cpu_thermal", "soc_thermal",
                         "x86_pkg_temp", "coretemp", "CPU-therm")

    def get_cpu_temperature(self):
        """CPU temperature in Celsius, or 0 where there is no sensor.

        /proc/cpuinfo only names a Pi on a Pi, while /sys/class/thermal
        exists on essentially every ARM SoC and on x86 -- gating it made an
        Orin report "n/a" while reading 54 C.
        """
        base = "/sys/class/thermal"
        candidates = []
        try:
            zones = sorted(name for name in os.listdir(base)
                           if name.startswith("thermal_zone"))
        except OSError:
            return 0
        for zone in zones:
            try:
                with open(os.path.join(base, zone, "type")) as f:
                    kind = f.read().strip()
            except OSError:
                kind = ""
            if kind in self.CPU_THERMAL_TYPES:
                candidates.insert(0, zone)          # a named CPU zone wins
            else:
                candidates.append(zone)
        for zone in candidates:
            try:
                with open(os.path.join(base, zone, "temp")) as f:
                    milli = int(f.read().strip())
            except (OSError, ValueError):
                continue
            celsius = milli / 1000.0
            # Some drivers park an unused zone at 0 or at a sentinel like
            # -274; publishing that is worse than saying nothing.
            if 1.0 <= celsius <= 150.0:
                return celsius
        return 0

    def on_after_startup(self):
        """Load settings, then bring up the camera, backend and bots."""
        try:
            self._after_startup_impl()
        except Exception as exc:                           # noqa: BLE001
            # Contain plugin startup failures inside the plugin lifecycle.
            self.ai_running = False
            self.backend_error = "PiNozCam startup failed: {}".format(self.redact(
                str(exc)))
            self._logger.exception("PiNozCam startup failed: %s", exc)

    def _after_startup_impl(self):
        """Perform startup work under the lifecycle exception boundary."""
        # Every setting, from settings_schema -- default, type, bounds and
        # the bespoke transforms all in one declaration.
        #
        # config.yaml, because OctoPrint persists the posted blob before
        # this plugin validates it, and the first arithmetic on None used
        # to raise HERE and kill the whole hook: no detection for the
        # session and nothing in the UI to explain it. See _coerce.
        self.load_settings()

        self._thread_calculation()
        self.initialize_cameras()
        self.initialize_font()
        # No snapshot first, on purpose: no frame has been fetched so the
        # aspect half of the check is skipped and runs by itself on the first
        # real frame. The flip/rotate half needs only settings.
        self._sync_mask_signature()

        # Probed now rather than at the first print, so a missing binary is
        # visible in the startup log instead of minutes into a job.
        try:
            probe = NozcamBackend(self.plugin_dir, self._logger,
                                  backend=self.ai_backend)
            probe.preflight()
            self.backend_kind = probe.kind
            self.backend_error = None
            self._logger.info("Inference backend ready: %s", probe.describe())
        except BackendUnavailable as exc:
            self.backend_kind = None
            self.backend_error = str(exc)
            self._logger.error("Inference backend unavailable: %s", exc)

        # Network setup must not block OctoPrint's startup-hook sequence.
        self.start_channel_setup(telegram=True, discord=True)

    def _notify_print_lost(self, payload):
        """Notify both media when PrintFailed reports a printer error."""
        if (payload.get("reason") or "").lower() != "error":
            return
        # Truncated: it is the firmware's own string and goes into a chat.
        error = str(payload.get("error") or "").strip()
        progress = payload.get("progress")
        where = ""
        if isinstance(progress, (int, float)):
            where = " at %.0f%%" % (progress * 100 if progress <= 1
                                    else progress)
        name = str(payload.get("name") or payload.get("path") or "").strip()
        text = (f"🔌 The print stopped{where}: OctoPrint lost the connection to "
                "the printer.")
        if name:
            text += f"\nFile: {name}"
        if error:
            text += f"\nPrinter reported: {error[:200]}"
        text += ("\nFailure detection has stopped for this print. Check the "
                 "USB cable and reconnect.")
        self._logger.warning("Print failed with reason=error; telling the "
                             "user. error=%r progress=%r", error[:200],
                             progress)
        self.notify_all(text, buttons=True, wait=False)

    def on_event(self, event, payload):
        """Start detection when a print starts or resumes, stop on any end."""
        if event in [Events.PRINT_STARTED, Events.PRINT_RESUMED]:
            self._logger.info(f"{event}: {payload}")
            self._logger.info("Print started, beginning AI image processing.")
            if event == Events.PRINT_STARTED:
                self._logger.info("Count and results are cleared.")
                with self.lock:
                    self.count = 0
                    self.ai_results.clear()
                    self.evidence = []
                    # bump_epoch rather than clear, so a camera read that
                    # started before this print cannot land afterwards.
                    self.frame_buffer.bump_epoch()
                    self.frames.invalidate()
                    self.detect_started_at = None
                    self.detect_frames = 0
                    self.last_ratio = None
                    self.criterion_met = self.notify_met = False
                    self.episode_action_level = 0
                    self.notify_attempts = 0
                    # Retire receipts before resetting their counters.
                    self.alerts_inflight.clear()
                with self.action_gate:
                    self.run_generation += 1
                self.notification_reach_to_max = False
                self.notifications_sent = 0
                self.last_notify_at = None
                self.current_telegram_message_paused = False
                self.alerts_muted = False
                self.current_telegram_message_set.clear()
                self._new_confirm_scope()
            self.start_ai_thread()
        elif event in [
                Events.PRINT_DONE, Events.PRINT_FAILED,
                Events.PRINT_CANCELLED, Events.PRINT_PAUSED]:
            self._logger.info(f"{event}: {payload}")
            self._logger.info("Print ended, stopping AI image processing.")
            # Stop detection before any potentially blocking notification.
            self.stop_ai_thread()
            if event == Events.PRINT_FAILED:
                self._notify_print_lost(payload or {})
            self.current_telegram_message_set.clear()
            # Any outstanding confirmation belonged to the print that ended.
            self._new_confirm_scope()
        if event == Events.PRINT_PAUSED:
            # Shadow state the confirmation handlers read. Without it a pause
            # from the web UI left Telegram's Resume button doing nothing.
            self.current_telegram_message_paused = True
        elif event == Events.PRINT_RESUMED:
            self.current_telegram_message_paused = False

    # Total time on_shutdown may take. OctoPrint runs shutdown hooks in
    # sequence, and on a Pi a slow shutdown is one the user power-cycles
    # through -- worse than an unclean thread.
    SHUTDOWN_BUDGET = 12.0

    def on_shutdown(self):
        """Stop workers, notification clients and daemon within one budget."""
        self._logger.info("PiNozCam shutting down.")
        deadline = time.monotonic() + self.SHUTDOWN_BUDGET
        self.shutting_down.set()
        with self.thread_lock:
            self.want_ai_running = False
        self.ai_running = False
        self.stop_event.set()

        def slice_of(cap):
            """Limit one shutdown step by its cap and the shared budget."""
            return max(0.0, min(cap, deadline - time.monotonic()))

        # Retire setup generations before stopping channel clients.
        self.channels.retire()

        # Starter first and briefly -- it is about to give up or hand over,
        # both quick. The detection thread may have a frame in flight.
        channel_workers = self.channels.workers()
        workers = [(self.ai_starter, "detection starter", 2.0)]
        workers.extend((thread, "channel setup", 3.0)
                       for thread in channel_workers)
        workers.append((self.ai_thread, "detection", 5.0))
        for thread, what, cap in workers:
            if thread is not None and thread.is_alive():
                thread.join(timeout=slice_of(cap))
                if thread.is_alive():
                    self._logger.warning(
                        "%s thread still running at shutdown.", what)

        self.stop_notification_workers(timeout=slice_of(1.0))

        if self.discord_bot is not None:
            try:
                self.discord_bot.stop(timeout=slice_of(3.0))
            except Exception:
                self._logger.exception("Error stopping the Discord bot.")

        if self.telegram_bot is not None:
            try:
                self.telegram_bot.stop(timeout=slice_of(2.0))
            except Exception:
                self._logger.exception("Error stopping the Telegram bot.")
            self.telegram_server_running = False

        backend = self.backend
        if backend is not None:
            try:
                # flight holds for its whole deadline, so it would blow the
                # budget in exactly the case that matters.
                backend.kill()
            except Exception:
                self._logger.exception("Error stopping the inference daemon.")
        self._logger.info("PiNozCam shutdown took %.1fs.",
                          self.SHUTDOWN_BUDGET - (deadline - time.monotonic()))

    def is_template_autoescaped(self):
        """Opt in to Jinja autoescaping, which 1.13.0 will enforce anyway."""
        return True

    def get_template_vars(self):
        """Expose the installed plugin version to PiNozCam templates."""
        return {"plugin_version": self._plugin_version}

    def get_template_configs(self):
        """Return PiNozCam tab, settings and wizard templates."""
        return [
            dict(type="tab", custom_bindings=True),
            dict(type="settings", name="PiNozCam", custom_bindings=True,
                 template="pinozcam_settings.jinja2"),
            dict(type="wizard", name="PiNozCam", custom_bindings=True,
                 template="pinozcam_wizard.jinja2"),
        ]

    def get_wizard_version(self):
        """Bump to show the wizard again after a change that needs a choice."""
        return 1

    def is_wizard_required(self):
        """Offer the wizard once per wizard version through OctoPrint."""
        return True

    def get_wizard_details(self):
        """Extra data for the wizard template. None needed."""
        return {}

    def _any_camera_configured(self):
        """True if some camera source is configured. Does not fetch."""
        if self.custom_snapshot_url:
            return True
        if self._settings.global_get(["webcam", "snapshot"]):
            return True
        return bool(self.cameras)

    def on_wizard_finish(self, handled):
        """Log what the first-run wizard ended up configuring."""
        self._logger.info(
            "Setup wizard finished (handled=%s): action=%s, camera=%s",
            handled, self.action,
            "configured" if self._any_camera_configured() else "none")

    def get_assets(self):
        """The JavaScript and CSS OctoPrint should serve for this plugin."""
        # the settings template uses, so it has to be evaluated before the
        # view model is bound.
        return dict(js=["js/pinozcam_mask.js", "js/pinozcam.js"],
                    css=["css/pinozcam.css"],
                    )

    def get_update_information(self, *args, **kwargs):
        """Return the stable GitHub-release update configuration."""
        return dict(
            pinozcam=dict(
                displayName="PiNozCam",
                displayVersion=self._plugin_version,
                type="github_release",
                current=self._plugin_version,
                user="DrAlexLiu",
                repo="OctoPrint-PiNozCam",
                prerelease=False,
                # update method: pip
                pip=(
                    "https://github.com/DrAlexLiu/"
                    "OctoPrint-PiNozCam/archive/{target}.zip")
            )
        )


__plugin_name__ = "PiNozCam"
__plugin_pythoncompat__ = ">=3.7,<4"
# Detection is local, but the optional Telegram and Discord integrations
# upload camera images to a third party. The Plugin Manager surfaces this
# link where a user deciding whether to install will look for it.
__plugin_privacypolicy__ = (
    "https://github.com/DrAlexLiu/OctoPrint-PiNozCam/"
    "blob/master/docs/notifications.md#privacy-notes"
)


def __plugin_load__():
    """Entry point OctoPrint calls to construct the plugin."""
    global __plugin_implementation__
    plugin = PinozcamPlugin()
    __plugin_implementation__ = plugin

    global __plugin_hooks__
    __plugin_hooks__ = {
        "octoprint.plugin.softwareupdate.check_config": plugin.get_update_information,
    }
