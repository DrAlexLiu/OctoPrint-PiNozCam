"""Asynchronous, generation-safe notification-channel setup."""

import threading

CHANNELS = ("telegram", "discord")


class ChannelManager(object):
    """Serialise channel rebuilds and reject superseded worker results."""

    def __init__(self, get_logger, shutting_down, setup, verify,
                 welcome_image, welcome_send):
        """Store channel callbacks and initialise per-channel generations."""
        # OctoPrint injects the logger after plugin construction.
        self._get_logger = get_logger
        self._shutting_down = shutting_down
        self._setup = setup
        self._verify = verify
        self._welcome_image = welcome_image
        self._welcome_send = welcome_send

        self.setup_lock = threading.Lock()
        self.gen_lock = threading.Lock()
        # Each channel can supersede only its own pending setup.
        self.generation = dict((name, 0) for name in CHANNELS)
        # Pending tokens also drive each channel's UI connection state.
        self.pending = dict((name, None) for name in CHANNELS)
        # Keep the latest worker for UI compatibility and all workers for shutdown.
        self.thread = None
        self._threads = set()

    # ---- what the UI asks ---------------------------------------------

    def running(self, channel=None):
        """Return whether any or the selected channel is connecting."""
        if channel is None:
            return any(v is not None for v in self.pending.values())
        return self.pending.get(channel) is not None

    def is_current(self, channel, token):
        """Return whether token is the newest generation for channel."""
        if token is None:
            return False
        with self.gen_lock:
            return token == self.generation[channel]

    # ---- what a save asks ----------------------------------------------

    def request(self, telegram=False, discord=False, welcome=False):
        """Rebuild selected channels asynchronously and return the worker."""
        tokens = {}
        with self.gen_lock:
            for name, wanted in (("telegram", telegram),
                                 ("discord", discord)):
                if wanted:
                    self.generation[name] += 1
                    tokens[name] = self.generation[name]
                    # Publish immediately so the UI reports connecting.
                    self.pending[name] = tokens[name]
        if not tokens:
            return None
        thread = threading.Thread(target=self._work, args=(tokens, welcome),
                                  name="pinozcam-channels")
        thread.daemon = True
        with self.gen_lock:
            self.thread = thread
            self._threads.add(thread)
        try:
            thread.start()
        except Exception:
            with self.gen_lock:
                self._threads.discard(thread)
                for name, token in tokens.items():
                    if self.pending.get(name) == token:
                        self.pending[name] = None
            raise
        return thread

    def workers(self):
        """Return every live or not-yet-reaped setup worker."""
        with self.gen_lock:
            return tuple(self._threads)

    def retire(self):
        """Invalidate every pending generation before channel shutdown."""
        with self.gen_lock:
            for name in self.generation:
                self.generation[name] += 1
                self.pending[name] = None

    # ---- the worker -----------------------------------------------------

    def _work(self, tokens, welcome):
        """One generation of setup, its verification, then its welcome."""
        try:
            if self._shutting_down.is_set():
                return
            with self.setup_lock:
                doing = dict(
                    (name, self.is_current(name, tokens.get(name)))
                    for name in CHANNELS)
                if self._shutting_down.is_set():
                    return
                if not any(doing.values()):
                    return          # superseded while we queued
                up = {}
                for name in CHANNELS:
                    if self._shutting_down.is_set():
                        return
                    token = tokens.get(name)
                    if doing[name] and self.is_current(name, token):
                        up[name] = bool(self._setup(name))

            # Verification is intentionally outside setup lock to avoid blocking.
            # It runs for each rebuilt channel only; unchanged channels keep
            # their prior verification state.
            for name in CHANNELS:
                if self._shutting_down.is_set():
                    return
                if up.get(name):
                    token = tokens.get(name)
                    self._verify(name, lambda n=name, t=token:
                                 self.is_current(n, t))

            if not welcome:
                return
            media = tuple(name for name in CHANNELS
                          if up.get(name)
                          and self.is_current(name, tokens.get(name)))
            if not media or self._shutting_down.is_set():
                return
            image = self._welcome_image()
            # Fetching can be slow, so revalidate tokens before delivery.
            media = tuple(name for name in media
                          if self.is_current(name, tokens.get(name)))
            if not media or self._shutting_down.is_set():
                return
            self._welcome_send(media, image)
        except Exception:                                   # noqa: BLE001
            self._get_logger().exception("Channel setup failed.")
        finally:
            # Never clear a newer worker's pending token.
            with self.gen_lock:
                for name, token in tokens.items():
                    if self.pending.get(name) == token:
                        self.pending[name] = None
                self._threads.discard(threading.current_thread())
