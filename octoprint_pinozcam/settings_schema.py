"""Canonical settings metadata used by the plugin and validation tools."""

# Keep this module independent of OctoPrint so validation tools can import it.
# Older 64x64 masks are expanded by mask._mask_matrix().
MASK_GRID = 128


class Setting(object):
    """One setting: what it is called, what it holds, what bounds it.

    `key`    the name in config.yaml and in the posted JSON
    `attr`   the plugin attribute it is mirrored to, or None if nothing
             reads it as one (maskSignature is handled by mask.py)
    `kind`   int, float, str, bool, or None for a value stored verbatim
    `lo`/`hi`  inclusive bounds, or None. Present means it is clamped.
    `choices`  allowed values for a string enum
    `strip`  "ends" or "all" -- whitespace removal on the way in and out
    `secret` restricted to admins over the REST API
    `resets` changing it invalidates the detection window and evidence
    `parse`  callable(raw, current) for the few that are not uniform
    """

    __slots__ = ("key", "attr", "kind", "default", "lo", "hi", "choices",
                 "strip", "secret", "resets", "parse", "note")

    def __init__(self, key, attr=None, kind=None, default=None, lo=None,
                 hi=None, choices=None, strip=None, secret=False,
                 resets=False, parse=None, note=""):
        """Describe one setting's storage, validation and reset behavior."""
        self.key = key
        self.attr = attr
        self.kind = kind
        self.default = default
        self.lo = lo
        self.hi = hi
        self.choices = choices
        self.strip = strip
        self.secret = secret
        self.resets = resets
        self.parse = parse
        self.note = note

    def clamp(self, value):
        """`value` brought inside the declared bounds."""
        if self.lo is not None:
            value = max(self.lo, value)
        if self.hi is not None:
            value = min(self.hi, value)
        return value


def parse_bool(raw, fallback):
    """Parse common boolean representations without string truthiness."""
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off", ""):
            return False
    return bool(fallback)


def _ms_to_seconds(raw, current):
    """Convert the wire-format sampling interval from ms to seconds."""
    if raw is None:
        return current
    return min(1000, max(10, int(raw))) / 1000.0


SETTINGS = (
    Setting("maskImageData", "mask_image_data", str,
            default="0" * (MASK_GRID * MASK_GRID), strip="ends"),
    # The signature derives from the camera URL and is therefore restricted.
    Setting("maskSignature", None, default={}, secret=True),

    # Stable opaque instance identifier used to route shared-channel buttons.
    Setting("printerId", "printer_id", str, default=""),

    Setting("enableAI", "enable_AI", bool, default=True),
    # Backend selection is applied when the next detector process starts.
    # NOTE Every kind _resolve_backend accepts must appear here, or forcing
    # it is impossible: config.py rejects any value outside this tuple, so a
    # missing entry makes that branch unreachable while auto-detection keeps
    # working -- which is exactly why "bpu" went unnoticed after the RDK X5
    # backend landed.
    Setting("aiBackend", "ai_backend", str, default="auto",
            choices=("auto", "cpu", "rknn", "awnn", "acl", "bpu", "vulkan",
                     "coreml")),
    Setting("action", "action", int, default=0, lo=0, hi=2),
    Setting("aiStartDelay", "ai_start_delay", int, default=0, lo=0,
            hi=60000),
    # Zero means no added delay between detections.
    Setting("detectionInterval", "detection_interval", int, default=0,
            lo=0, hi=3600),
    Setting("printLayoutThreshold", "print_layout_threshold", float,
            default=0.5, lo=0.0, hi=1.0),
    Setting("imgSensitivity", "img_sensitivity", float, default=0.04,
            lo=1e-4, hi=1.0, resets=True),
    Setting("scoresThreshold", "scores_threshold", float, default=0.87,
            lo=0.0, hi=1.0, resets=True),
    # The one-hour maximum fits the bounded 20,000-result history.
    Setting("countTime", "count_time", int, default=120, lo=10, hi=3600,
            resets=True),
    Setting("frameSampleInterval", "frame_sample_interval", int,
            default=200, lo=10, hi=1000, resets=True, parse=_ms_to_seconds,
            note="milliseconds on the wire, seconds in the attribute"),
    # Buffer settings apply when the next detection run starts.
    Setting("frameBufferMaxAge", "frame_buffer_max_age", int,
            default=4, lo=4, hi=16),
    Setting("frameBufferCapacity", "frame_buffer_capacity", int,
            default=5, lo=4, hi=16),
    Setting("failureRatio", "failure_ratio", float, default=0.05, lo=0.01,
            hi=1.0),

    Setting("cpuSpeedControl", "cpu_speed_control", float, default=0.5,
            lo=0.01, hi=1.0),
    Setting("maxNotification", "max_notification", int, default=0, lo=0,
            hi=60000),
    # Alerts suppressed by this throttle are dropped rather than queued.
    Setting("notifyInterval", "notify_interval", int, default=60, lo=0,
            hi=3600),
    Setting("customSnapshotURL", "custom_snapshot_url", str, default="",
            strip="all", secret=True,
            note="an IP camera URL routinely carries user:pass@"),

    Setting("enableTelegram", "enable_telegram", bool, default=True),
    Setting("telegramBotToken", "telegram_bot_token", str, default="",
            strip="ends", secret=True),
    Setting("telegramChatID", "telegram_chat_id", str, default="",
            strip="ends", secret=True),
    Setting("enableDiscord", "enable_discord", bool, default=True),
    Setting("discordBotToken", "discord_bot_token", str, default="",
            strip="ends", secret=True),
    Setting("discordChannelID", "discord_channel_id", str, default="",
            strip="ends", secret=True),

    # Retained only for compatibility with existing configuration files.
    Setting("maxCount", "max_count", int, default=2, lo=1, hi=100),
    Setting("enableMaxFailureCountNotification",
            "enable_max_failure_count_notification", bool, default=True),
    Setting("riseFactor", None, float, default=0.5, lo=0.01, hi=5.0),
)

BY_KEY = dict((s.key, s) for s in SETTINGS)

# Obsolete keys removed from config.yaml on the next settings save.
RETIRED_KEYS = ("blurThreshold", "notifyRatio")


def defaults():
    """The dict OctoPrint registers, so an unset key still reads sanely."""
    return dict((s.key, s.default) for s in SETTINGS)


def numeric():
    """{key: (converter, low, high)} for every bounded number."""
    return dict((s.key, (s.kind, s.lo, s.hi)) for s in SETTINGS
                if s.kind in (int, float) and s.lo is not None)


def strings():
    """Keys whose value is a string and must never be stored as null."""
    return tuple(s.key for s in SETTINGS if s.kind is str)


def whitespace_free():
    """Return keys whose internal whitespace is always invalid."""
    return frozenset(s.key for s in SETTINGS if s.strip == "all")


def restricted():
    """Keys the REST API returns to administrators only."""
    return [[s.key] for s in SETTINGS if s.secret]


def resetting_attrs():
    """Attributes whose change makes the accumulated statistics stale."""
    return tuple(s.attr for s in SETTINGS if s.resets and s.attr)
