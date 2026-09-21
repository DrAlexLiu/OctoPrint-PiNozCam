"""Settings defaults, validation and save-time publication."""

import uuid

import octoprint.plugin
import octoprint.access

from . import settings_schema as schema
from . import credentials


class ConfigMixin:
    """Mixed into PinozcamPlugin; see the module docstring."""

    def get_settings_restricted_paths(self):
        """Restrict credentials and camera-derived secrets to admins."""
        return {"admin": schema.restricted()}

    def get_settings_defaults(self):
        """Every setting and its default, from settings_schema."""
        return schema.defaults()

    NUMERIC_SETTINGS = schema.numeric()
    STRING_SETTINGS = schema.strings()
    WHITESPACE_FREE_SETTINGS = schema.whitespace_free()

    def _detection_fingerprint(self):
        """Return settings whose changes invalidate accumulated results."""
        return tuple(getattr(self, attr)
                     for attr in schema.resetting_attrs())

    def _clean_setting_string(self, key, value):
        """Remove whitespace according to the setting's schema policy."""
        if key in self.WHITESPACE_FREE_SETTINGS:
            return "".join(value.split())
        return value.strip()

    def _sanitise_settings(self, data):
        """Coerce and clamp posted values before OctoPrint stores them."""
        cleaned = dict(data)
        for key in self.STRING_SETTINGS:
            if key in cleaned and cleaned[key] is None:
                # Dropping null preserves the stored value; "" clears it.
                self._logger.warning(
                    "Ignoring null for %s; keeping the stored value", key)
                cleaned.pop(key)
            elif key in cleaned and not isinstance(cleaned[key], str):
                self._logger.warning(
                    "Coercing %s from %s to str", key,
                    type(cleaned[key]).__name__)
                cleaned[key] = str(cleaned[key])
            if key in cleaned and isinstance(cleaned[key], str):
                fixed = self._clean_setting_string(key, cleaned[key])
                if fixed != cleaned[key]:
                    self._logger.info("Removed whitespace from %s", key)
                cleaned[key] = fixed

        # Keep IDs as strings: Discord values can exceed JS integer precision.
        id_checks = {
            "telegramChatID": credentials.telegram_chat_id_error,
            "discordChannelID": credentials.discord_channel_id_error,
        }
        for key, validate in id_checks.items():
            if key not in cleaned:
                continue
            problem = validate(cleaned[key])
            if problem:
                self._logger.warning(
                    "Ignoring invalid %s: %s", key, problem)
                cleaned.pop(key)
        for key, (convert, low, high) in self.NUMERIC_SETTINGS.items():
            if key not in cleaned:
                continue
            try:
                value = convert(cleaned[key])
            except (TypeError, ValueError):
                self._logger.warning(
                    "Ignoring unusable value for %s: %r", key, cleaned[key])
                cleaned.pop(key)
                continue
            if value != value:                      # NaN
                self._logger.warning("Ignoring NaN for %s", key)
                cleaned.pop(key)
                continue
            cleaned[key] = min(high, max(low, value))
        return cleaned

    def on_settings_save(self, data):
        """Validate, persist and atomically publish posted settings."""
        # Keep the mask read-modify-write transaction under mask_lock.
        with self.mask_lock:
            # Capture the server-owned signature before OctoPrint merges data.
            previous_signature = self._stored_mask_signature()

            before = self._detection_fingerprint()

            # Compare channel values to rebuild only changed channels.
            channel_before = (self.telegram_bot_token,
                              self.telegram_chat_id,
                              self.enable_telegram,
                              self.discord_bot_token,
                              self.discord_channel_id,
                              self.enable_discord)

            old_custom_snapshot_url = self.custom_snapshot_url

            # OctoPrint persists the mapping passed to the base implementation.
            data = self._sanitise_settings(data)

            octoprint.plugin.SettingsPlugin.on_settings_save(self, data)

            # OctoPrint does not remove keys absent from current defaults.
            for retired_key in schema.RETIRED_KEYS:
                try:
                    self._settings.remove([retired_key])
                except Exception:                           # noqa: BLE001
                    pass

            if self._stored_mask_signature() != previous_signature:
                self._settings.set(["maskSignature"], previous_signature or {})

            previous_mask = self.mask_image_data
            posted_mask = data.get("maskImageData", previous_mask)
            if posted_mask != previous_mask \
                    and posted_mask == self._mask_before_retarget:
                # Reject a stale editor echo after automatic mask realignment.
                self._logger.info(
                    "Ignoring an Undetect Zone echoed from a page that was "
                    "loaded before the mask was realigned.")
                self._settings.set(["maskImageData"], previous_mask)
                posted_mask = previous_mask
            mask_redrawn = posted_mask != previous_mask
            self.mask_image_data = posted_mask
            # Parse into locals, then publish all values with one generation.
            new_enable_AI = schema.parse_bool(
                data.get("enableAI", self.enable_AI), self.enable_AI)
            new_ai_backend = data.get("aiBackend", self.ai_backend)
            if new_ai_backend not in schema.BY_KEY["aiBackend"].choices:
                self._logger.warning(
                    "Unknown aiBackend %r; keeping %r.",
                    new_ai_backend, self.ai_backend)
                new_ai_backend = self.ai_backend
            new_enable_telegram = schema.parse_bool(
                data.get("enableTelegram", self.enable_telegram),
                self.enable_telegram)
            new_enable_discord = schema.parse_bool(
                data.get("enableDiscord", self.enable_discord),
                self.enable_discord)
            new_action = int(data.get("action", self.action))
            new_ai_start_delay = int(data.get("aiStartDelay", self.ai_start_delay))
            new_print_layout_threshold = float(data.get(
                "printLayoutThreshold", self.print_layout_threshold))
            new_img_sensitivity = float(data.get(
                "imgSensitivity", self.img_sensitivity))
            new_scores_threshold = float(data.get(
                "scoresThreshold", self.scores_threshold))
            new_max_count = int(data.get("maxCount", self.max_count))
            new_enable_max_failure_count_notification = \
                self._settings.get_boolean(
                    ["enableMaxFailureCountNotification"])
            new_count_time = int(data.get("countTime", self.count_time))
            new_frame_buffer_max_age = int(data.get(
                "frameBufferMaxAge", self.frame_buffer_max_age))
            new_frame_buffer_capacity = int(data.get(
                "frameBufferCapacity", self.frame_buffer_capacity))
            new_cpu_speed_control = float(data.get(
                "cpuSpeedControl", self.cpu_speed_control))
            new_custom_snapshot_url = data.get(
                "customSnapshotURL", self.custom_snapshot_url)
            new_max_notification = int(data.get(
                "maxNotification", self.max_notification))
            new_notify_interval = int(data.get(
                "notifyInterval", self.notify_interval))
            new_detection_interval = max(0, int(
                data.get("detectionInterval", self.detection_interval)))
            new_failure_ratio = float(
                data.get("failureRatio", self.failure_ratio))
            # Stored in seconds; the UI and settings API use milliseconds.
            new_frame_sample_interval = min(1000, max(10, int(data.get(
                "frameSampleInterval",
                int(round(self.frame_sample_interval * 1000)))))) / 1000.0
            new_telegram_bot_token = data.get(
                "telegramBotToken", self.telegram_bot_token)
            new_telegram_chat_id = data.get("telegramChatID", self.telegram_chat_id)
            new_discord_bot_token = data.get("discordBotToken", self.discord_bot_token)
            new_discord_channel_id = data.get(
                "discordChannelID", self.discord_channel_id)

            # Clamp server-side; img_sensitivity is a C++ divisor.
            new_img_sensitivity = min(1.0, max(1e-4, new_img_sensitivity))
            new_scores_threshold = min(1.0, max(0.0, new_scores_threshold))
            new_failure_ratio = min(1.0, max(0.01, new_failure_ratio))
            new_count_time = max(10, new_count_time)
            new_frame_buffer_max_age = max(4, min(16, new_frame_buffer_max_age))
            new_frame_buffer_capacity = max(4, min(16, new_frame_buffer_capacity))

            camera_source_changed = (
                new_custom_snapshot_url != old_custom_snapshot_url)

            # Values and generation must become visible atomically.
            with self.lock:
                self.enable_AI = new_enable_AI
                self.ai_backend = new_ai_backend
                self.enable_telegram = new_enable_telegram
                self.enable_discord = new_enable_discord
                self.action = new_action
                self.ai_start_delay = new_ai_start_delay
                self.print_layout_threshold = new_print_layout_threshold
                self.img_sensitivity = new_img_sensitivity
                self.scores_threshold = new_scores_threshold
                self.max_count = new_max_count
                self.enable_max_failure_count_notification = \
                    new_enable_max_failure_count_notification
                self.count_time = new_count_time
                self.frame_buffer_max_age = new_frame_buffer_max_age
                self.frame_buffer_capacity = new_frame_buffer_capacity
                self.cpu_speed_control = new_cpu_speed_control
                self.custom_snapshot_url = new_custom_snapshot_url
                self.max_notification = new_max_notification
                self.notify_interval = new_notify_interval
                self.detection_interval = new_detection_interval
                self.failure_ratio = new_failure_ratio
                self.frame_sample_interval = new_frame_sample_interval
                self.telegram_bot_token = new_telegram_bot_token
                self.telegram_chat_id = new_telegram_chat_id
                self.discord_bot_token = new_discord_bot_token
                self.discord_channel_id = new_discord_channel_id
                # Rebuild the active source only when its URL changes.
                if camera_source_changed:
                    self._camera_source_generation += 1
                self.settings_generation += 1
                self.setting_change_while_printing = True
            changed_detection = before != self._detection_fingerprint()
            self._logger.info("Plugin settings saved.")

            # re-initialize the parameters
            self._thread_calculation()
            self.initialize_cameras()

            # Compute the signature after selecting the effective camera.
            if mask_redrawn:
                # A redrawn mask belongs to the frame shown by the editor.
                self.mask_warning = None
                self._mask_before_retarget = None
                self._store_mask_signature(
                    dict(self.frame_geometry) if self.frame_geometry
                    else self._camera_signature())
            else:
                self._sync_mask_signature()

        self.initialize_font()
        self.notification_reach_to_max = False

        # Retire buffered and in-flight frames from the previous settings.
        self.frame_buffer.bump_epoch()
        self.frames.invalidate()

        if changed_detection:
            # Evidence and statistics must use one parameter set.
            self.reset_measurement()
            self._logger.info(
                "Detection parameters changed; window and evidence reset, "
                "warming up again.")

        # Rebuild only channels whose configuration changed.
        (old_token, old_chat, old_enable_tg,
         old_bot, old_channel, old_enable_dc) = channel_before
        telegram_changed = (self.telegram_bot_token != old_token
                            or self.telegram_chat_id != old_chat
                            or self.enable_telegram != old_enable_tg)
        discord_changed = (self.discord_bot_token != old_bot
                           or self.discord_channel_id != old_channel
                           or self.enable_discord != old_enable_dc)
        # Network setup and welcome delivery stay off the request thread.
        self.start_channel_setup(telegram=telegram_changed,
                                 discord=discord_changed, welcome=True)

    def _coerce(self, setting, raw):
        """Coerce one stored value, falling back on invalid input."""
        if setting.parse is not None:
            try:
                return setting.parse(raw, getattr(self, setting.attr, None))
            except (TypeError, ValueError):
                return setting.parse(setting.default, None)
        if setting.kind is bool:
            return schema.parse_bool(raw, setting.default)
        if setting.kind is str:
            value = setting.default if raw is None else str(raw)
            value = self._clean_setting_string(setting.key, value)
            if setting.choices and value not in setting.choices:
                self._logger.warning("Unknown %s %r; using %r.",
                                     setting.key, value, setting.default)
                return setting.default
            return value
        if setting.kind in (int, float):
            try:
                return setting.clamp(setting.kind(raw))
            except (TypeError, ValueError):
                return setting.default
        return setting.default if raw is None else raw

    def load_settings(self):
        """Mirror every declared setting onto its runtime attribute."""
        for setting in schema.SETTINGS:
            if setting.attr is None:
                continue
            setattr(self, setting.attr,
                    self._coerce(setting, self._settings.get([setting.key])))
        self._ensure_printer_id()

    def _ensure_printer_id(self):
        """Mint this printer's identity once, on the first load ever.

        DEFAULT would be the same value on every installation -- which is
        exactly the case it exists to distinguish. It has to be created by
        the instance, once, and then persist.

        Saved immediately: if the process dies before the first settings
        save, the next start would mint a different id and every button in
        every message already sent would stop matching.
        """
        if self.printer_id:
            return
        self.printer_id = uuid.uuid4().hex
        self._settings.set(["printerId"], self.printer_id)
        self._settings.save()
        self._logger.info(
            "Generated this printer's identity: %s. It tells this instance "
            "apart from others sharing a Discord channel; it is not a "
            "credential and authorises nothing.", self.printer_id)
