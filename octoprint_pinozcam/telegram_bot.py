"""Telegram polling, delivery and button transport."""

import collections
import logging
import threading
import time

import requests
import telebot

from . import credentials

# A sufficiently long polling run resets the consecutive-failure counter.
HEALTHY_RUN = 120.0

# Consecutive 409s -- none of them explained by a client of ours that was
# just replaced, and no successful poll in between -- that mean another
# process polls this token. Another poller takes the stream back again and
# again, so its 409s keep coming; one stray 409 can be a request that was
# delayed on the network.
CONFLICT_LIMIT = 3

# How long a client that has stopped polling lets the handlers still
# queued on its worker pool run before closing the pool. A press that was
# confirmed to Telegram (the next getUpdates went out with a higher
# offset) exists nowhere else, so dropping it would lose it.
DRAIN_WAIT = 30.0

# One getUpdates consumer per token. A client stopped for a rebuild (stop
# with wait=False) may still have a request on its way to Telegram; until
# its thread has exited, a 409 on the client that replaced it can be that
# request and proves nothing about other processes.
_RETIRING = {}                  # token -> set of TelegramBot, thread alive
_RETIRING_LOCK = threading.Lock()

# What was already acted on, per token, across clients. A press one client
# handled but whose confirmation never went out (its next getUpdates did
# not happen) comes to its successor again; Telegram only forgets an update
# once a later offset confirms it. Bounded; 512 is hours of presses.
_HANDLED = {}                   # token -> OrderedDict(key -> None)
_HANDLED_MAX = 512


def _first_time(token, key):
    """Record key as handled for token; False if it already was."""
    with _RETIRING_LOCK:
        seen = _HANDLED.setdefault(token, collections.OrderedDict())
        if key in seen:
            return False
        seen[key] = None
        while len(seen) > _HANDLED_MAX:
            seen.popitem(last=False)
        return True


def _handing_over(token):
    """True while a replaced client for token may still have a request out.

    A client is done when its polling thread has exited AND no getUpdates
    it sent is still in flight -- a handler exception makes telebot give up
    on its own polling thread without waiting for the request that thread
    has out, so the outer thread can be gone while the request is not.
    Clients that are done are dropped here.
    """
    with _RETIRING_LOCK:
        clients = _RETIRING.get(token)
        if not clients:
            return False
        live = set(c for c in clients if not c._thread_done or c._in_flight)
        if live:
            _RETIRING[token] = live
            return True
        del _RETIRING[token]
        return False


# What a client answers to a press that reached it after the settings it
# was built for were switched off or replaced. Under 200 characters, the
# limit for answerCallbackQuery text.
RETIRED_TEXT = ("PiNozCam's Telegram settings changed after this message was "
                "sent, so this button no longer works. Press one on a newer "
                "message.")
OTHER_CHAT_TEXT = "Not the configured chat."

# The client and chat a command arrived through, for the thread running
# its handler. Replies made while handling it go back there -- through that
# client and to that chat -- and not to whatever a Save landing meanwhile
# has made current: a Check from chat A that was fetching a frame while the
# chat was changed to B used to deliver A's frame to B.
_REPLYING = threading.local()


def reply_target():
    """(client, chat id) of the command being handled on this thread, or
    (None, None) outside a handler."""
    return (getattr(_REPLYING, "bot", None),
            getattr(_REPLYING, "chat_id", None))
MAX_CONSECUTIVE = 3


def _is_conflict(exc):
    """Detect Telegram's 409 error for competing getUpdates clients."""
    code = getattr(exc, "error_code", None)
    if code is None:
        result = getattr(exc, "result", None)
        code = getattr(result, "status_code", None)
    if code == 409:
        return True
    text = str(exc).lower()
    return ("terminated by other getupdates" in text
            or "only one bot instance" in text
            or "409" in text and "conflict" in text)


class _QuietDeliberateStop(logging.Filter):
    """Drop telebot's two ERROR lines for a stop that was asked for.

    infinity_polling logs "polling exited" and "Break infinity polling" at
    ERROR whenever polling ends, and it ends normally only when
    stop_polling() was called -- every Save that touches Telegram, and
    every shutdown. They read as failures and are not.
    """

    QUIET = ("Infinity polling: polling exited", "Break infinity polling")

    def filter(self, record):
        return record.getMessage() not in self.QUIET


telebot.logger.addFilter(_QuietDeliberateStop())


class _PollErrors(telebot.ExceptionHandler):
    """telebot's hook for polling errors, the only place a 409 is visible.

    infinity_polling catches every polling error itself, logs it and
    retries, so nothing reaches TelegramBot._poll: its 409 branch never
    ran, and a token shared by two printers only produced an endless
    stream of tracebacks.
    """

    def __init__(self, owner):
        self.owner = owner

    def handle(self, exception):
        return self.owner._on_poll_error(exception)


def buttons(paused=False, muted=False):
    """Build alert controls using idempotent target-state commands."""
    keyboard = telebot.types.InlineKeyboardMarkup()
    keyboard.row(
        telebot.types.InlineKeyboardButton(
            '🔍Check', callback_data='check'),
        telebot.types.InlineKeyboardButton(
            '🔊Unmute' if muted else '🔇Mute',
            callback_data='unmute' if muted else 'mute'),
    )
    keyboard.row(
        telebot.types.InlineKeyboardButton(
            '▶️Resume' if paused else '⏸️Pause', callback_data='pause'),
        telebot.types.InlineKeyboardButton('🛑Stop', callback_data='stop'),
    )
    return keyboard


def confirm_buttons(action, token):
    """Build a one-use confirmation row within Telegram's size limit."""
    keyboard = telebot.types.InlineKeyboardMarkup()
    keyboard.row(
        telebot.types.InlineKeyboardButton(
            'Yes', callback_data=f'yes:{action}:{token}'),
        telebot.types.InlineKeyboardButton(
            'No', callback_data=f'no:{action}:{token}'),
    )
    return keyboard


def send_draft(token, chat_id, image=None, caption=""):
    """Test unsaved credentials with an isolated non-polling client."""
    bot = telebot.TeleBot(token)
    if image is not None:
        bot.send_photo(chat_id, image, caption=caption,
                       disable_notification=True)
    else:
        bot.send_message(chat_id, text=caption,
                         disable_notification=True)


class TelegramBot:
    """Long-poll client plus the REST calls that go with it.

    Sending does not need the poller: send() is an ordinary REST call, and
    the polling thread exists only so that a button press has somewhere to
    arrive. So a bot whose polling has died still delivers alerts.
    """

    def __init__(self, token, chat_id, logger, on_command,
                 source_problem=None):
        """Hold the credentials and the callbacks; connect nothing yet.

        `source_problem(token, chat_id)` says why a command from that chat,
        through this token, must be refused right now (text), or None.
        It is the plugin's view of the settings as COMMITTED by the last
        Save -- not this client's copy of them, which a Save replaces
        asynchronously: between the two, a client still running under
        settings the user has already switched off accepted a Stop.
        Without it, the client falls back to its own chat id.
        """
        self.token = token
        self.chat_id = str(chat_id)
        self._logger = logger
        self.on_command = on_command        # (name, call) -> str | None
        self.source_problem = source_problem

        self.running = False
        self.last_error = None
        self.thread = None
        self.stop_event = threading.Event()
        self._bot = None
        self._conflicts = 0                 # 409s since the last good poll
        self._in_flight = 0                 # getUpdates calls not yet back
        self._thread_done = False           # _poll has run its finally
        self._stopped = False               # stop() was called
        self._finished = False              # _finish has run

    def _timed(self, what, fn):
        """Run fn and log its elapsed time."""
        started = time.monotonic()
        try:
            return fn()
        finally:
            self._logger.info("[timing] %s took %.0f ms", what,
                              (time.monotonic() - started) * 1000.0)

    # ---- ownership ----------------------------------------------------

    def _source_problem(self, source, what):
        """Why a command from `source` is refused now, or None.

        Fails closed: no readable chat id is refused too -- a bot added to
        a second group would otherwise take Pause and Stop from anyone
        there.
        """
        chat_id = getattr(getattr(source, "chat", None), "id", None)
        if self.source_problem is not None:
            problem = self.source_problem(self.token, chat_id)
        elif str(chat_id) == str(self.chat_id):
            problem = None
        else:
            problem = OTHER_CHAT_TEXT
        if problem is not None:
            self._logger.warning(
                "Ignoring %s from Telegram chat %r: %s", what, chat_id,
                problem)
        return problem

    def _from_owner(self, source, what):
        """Accept commands only from the chat configured right now."""
        return self._source_problem(source, what) is None

    def _as_source(self, source):
        """Run the handler body with replies bound to where it came from."""
        chat_id = getattr(getattr(source, "chat", None), "id", None)
        _REPLYING.bot, _REPLYING.chat_id = self, str(chat_id)

    @staticmethod
    def _done_as_source():
        _REPLYING.bot = _REPLYING.chat_id = None

    def answer(self, call_id, text=None):
        """Acknowledge a button press. Telegram spins it forever otherwise."""
        try:
            self._bot.answer_callback_query(call_id, text=text)
        except Exception as exc:                            # noqa: BLE001
            self._logger.error("answer_callback_query failed: %s",
                               credentials.redact(str(exc)))

    # ---- lifecycle ----------------------------------------------------

    def start(self):
        """Register handlers once and start the polling worker."""
        try:
            self._bot = telebot.TeleBot(
                self.token, exception_handler=_PollErrors(self))
            self._track_polls(self._bot)
            self._register()
            self.stop_event.clear()
            self._thread_done = False
            self._stopped = False
            self._finished = False
            self._in_flight = 0
            self.thread = threading.Thread(target=self._poll,
                                           name="pinozcam-telegram")
            self.thread.daemon = True
            self.thread.start()
            self.running = True
            return True
        except Exception as exc:                            # noqa: BLE001
            self.last_error = credentials.redact(str(exc))
            self._logger.error(
                "An error occurred while setting up the Telegram bot: %s",
                self.last_error)
            self.running = False
            self._bot = None
            return False

    def stop(self, timeout=10.0, wait=True):
        """Stop polling. Safe to call twice.

        Only asks: the polling thread sits in a long poll of up to 60 s
        that nothing here can interrupt, and the worker pool may be in the
        middle of a handler. Both are wound up by the polling thread itself
        when the poll returns (_finish), so this never blocks on either.
        wait=True (shutdown) then joins the thread, with `timeout`;
        wait=False (a rebuild) returns at once -- the client replacing this
        one ends the long poll with its first getUpdates. Waiting for that
        delayed every Save by the full join.
        """
        self.stop_event.set()
        self.running = False
        self._stopped = True
        bot = self._bot
        # Kept, not cleared: a second stop() must see the thread is still
        # alive and return at once too, not take it for never started.
        thread = self.thread
        if bot is not None:
            try:
                bot.stop_polling()
            except Exception as exc:                        # noqa: BLE001
                self._logger.error(
                    "Error occurred while stopping Telegram bot polling: %s",
                    credentials.redact(str(exc)))
        if thread is not None and thread.is_alive():
            if not wait:
                with _RETIRING_LOCK:
                    # Under the lock _finish takes: a thread that has
                    # already run its finally must not be registered, or
                    # the entry would never be removed.
                    if not self._thread_done:
                        _RETIRING.setdefault(self.token, set()).add(self)
                self._logger.info("Telegram bot is stopping.")
                return True
            thread.join(timeout=timeout)
            if thread.is_alive():
                self._logger.warning(
                    "Telegram polling thread has not stopped.")
                return False
        if bot is not None and not self._finished:
            # Never polled (start failed, or a client built for a one-off
            # send): there is no polling thread to do this.
            self._finish(bot)
        self._bot = None
        self._logger.info("Telegram bot has been stopped.")
        return True

    def _track_polls(self, bot):
        """Reset the 409 count whenever a getUpdates returns normally."""
        real = getattr(bot, "get_updates", None)
        if real is None:
            return

        def tracked(*args, **kwargs):
            with _RETIRING_LOCK:
                self._in_flight += 1
            try:
                result = real(*args, **kwargs)
            finally:
                with _RETIRING_LOCK:
                    self._in_flight -= 1
            self._poll_succeeded()
            return result
        bot.get_updates = tracked

    def _poll_succeeded(self):
        """A getUpdates came back: whatever 409s came before are over."""
        self._conflicts = 0

    def _finish(self, bot):
        """Wind up a client whose polling has ended.

        Lets the handlers still queued run (a press confirmed to Telegram
        exists nowhere else), then closes the worker pool -- which joins a
        handler mid-flight, so that runs to its end too. On the polling
        thread, or from stop() when there never was one.

        The REST client stays unless stop() was called: a client whose
        polling gave up (409s, repeated failures) still delivers alerts,
        it only cannot take button presses.
        """
        if self._finished:
            return
        self._finished = True
        pool = getattr(bot, "worker_pool", None)
        tasks = getattr(pool, "tasks", None)
        if tasks is not None:
            deadline = time.monotonic() + DRAIN_WAIT
            while not tasks.empty() and time.monotonic() < deadline:
                time.sleep(0.05)
            if not tasks.empty():
                self._logger.warning(
                    "Telegram handlers still queued after %.0f s; dropping "
                    "them.", DRAIN_WAIT)
        try:
            # stop_bot closes the pool as well as polling; older telebot
            # versions do not have it.
            if hasattr(bot, "stop_bot"):
                bot.stop_bot()
        except Exception as exc:                            # noqa: BLE001
            self._logger.error(
                "Error occurred while stopping Telegram bot polling: %s",
                credentials.redact(str(exc)))
        if self._stopped and self._bot is bot:
            self._bot = None

    def verify(self):
        """Return an error string if Telegram rejects the credentials."""
        url = f"https://api.telegram.org/bot{self.token}/getChat"
        try:
            response = self._timed(
                "telegram verify",
                lambda: requests.get(url, params={"chat_id": self.chat_id},
                                     timeout=(5, 15)))
            if response.status_code == 200:
                self.last_error = None
                return None
            error = (f"Telegram refused the credentials (HTTP {response.status_code}).")
        except Exception as exc:                            # noqa: BLE001
            error = credentials.redact(str(exc))
        self._logger.error("Telegram check failed: %s", error)
        self.last_error = error
        return error

    # ---- sending ------------------------------------------------------

    def send(self, caption="", image=None, keyboard=None, silent=False,
             chat_id=None):
        """Send one message and return its ID, or None on failure.

        `chat_id` pins the destination. A Save that only changes the chat
        keeps this client and rewrites self.chat_id, so a caller that
        checked the chat a moment ago (a Test, a welcome) must say which
        one it meant rather than read it again here.
        """
        bot = self._bot
        if bot is None:
            return None
        chat = chat_id or self.chat_id
        try:
            if image:
                message = self._timed(
                    "telegram send_photo",
                    lambda: bot.send_photo(
                        chat, image, caption=caption,
                        reply_markup=keyboard, disable_notification=silent))
            else:
                message = self._timed(
                    "telegram send_message",
                    lambda: bot.send_message(
                        chat, text=caption, reply_markup=keyboard,
                        disable_notification=silent))
            message_id = getattr(message, "message_id", None)
            self._logger.info(
                "Message sent to Telegram successfully. Message ID: %s",
                message_id)
            return message_id
        except Exception as exc:                            # noqa: BLE001
            self._logger.error("Failed to send message to Telegram: %s",
                               credentials.redact(str(exc)))
            return None

    def reply_to(self, message, text):
        """Reply in-thread to a message the user sent."""
        bot = self._bot
        if bot is None:
            return
        try:
            bot.reply_to(message, text)
        except Exception as exc:                            # noqa: BLE001
            self._logger.error("Failed to reply on Telegram: %s",
                               credentials.redact(str(exc)))

    # ---- the handlers -------------------------------------------------

    def _register(self):
        """Wire telebot's three entry points to the one callback."""
        bot = self._bot

        @bot.message_handler(commands=['hi'])
        def _hi(message):
            """Dispatch an authenticated ``/hi`` command."""
            if not self._from_owner(message, "/hi"):
                return
            key = ("hi", message.chat.id, message.message_id)
            if self._retired(key) or not self._first_time(key):
                return
            self._as_source(message)
            try:
                self.on_command("/hi", None)
            finally:
                self._done_as_source()

        @bot.message_handler(func=lambda message: True)
        def _anything_else(message):
            """Explain unsupported text sent by the configured owner."""
            if not self._from_owner(message, "a message"):
                return
            key = ("text", message.chat.id, message.message_id)
            if self._retired(key) or not self._first_time(key):
                return
            self._as_source(message)
            try:
                text = self.on_command("", None)
            finally:
                self._done_as_source()
            if text:
                self.reply_to(message, text)

        @bot.callback_query_handler(func=lambda call: True)
        def _button(call):
            """Validate and dispatch one Telegram inline-button press."""
            if self._retired(("press", call.id)):
                # Whatever chat it came from: the settings this client was
                # built for are gone, and the printer is not driven from
                # them any more. Answered, so the button stops spinning.
                self.answer(call.id, text=RETIRED_TEXT)
                return
            message = getattr(call, "message", None)
            problem = self._source_problem(message, "a button")
            if problem is not None:
                # Recorded as handled: a press refused under the settings
                # committed now is not to be retried by the next client.
                self._first_time(("press", call.id))
                self.answer(call.id, text=problem)
                return
            if not self._first_time(("press", call.id)):
                # Handled already, by this client or the one it replaced,
                # and answered then; answering again would be refused.
                return
            # button forever if the query goes unanswered, and the old
            # handler answered on several separate paths -- one of which
            # could be missed by a new branch.
            self._as_source(message)
            try:
                self.on_command(call.data or "", call)
            finally:
                self._done_as_source()
                self.answer(call.id)

    def _first_time(self, key):
        """See _first_time at module level; per token, across clients."""
        return _first_time(self.token, key)

    def _retired(self, key):
        """True once this client is no longer the configured one.

        stop() sets the flag: the settings this client was built for were
        switched off or replaced. Nothing received after that may act on
        the printer or answer into whatever chat is configured now -- a
        Stop confirmed a moment after Telegram was switched off still
        cancelled the print. The key is recorded as handled, so the client
        that replaced this one, which may receive the same update (this
        client's confirming getUpdates never went out), does not act on it
        either: the press is refused once, not deferred.
        """
        if not self.stop_event.is_set():
            return False
        _first_time(self.token, key)
        return True

    def _on_poll_error(self, exc):
        """True when the error is dealt with here, False for telebot's own
        log-and-retry."""
        if not _is_conflict(exc):
            return False
        if self.stop_event.is_set():
            # Our own replacement took the update stream: expected.
            return True
        if _handing_over(self.token):
            # The client this one replaced may still have a request on its
            # way; its arrival ends our poll with exactly this 409.
            return True
        self._conflicts += 1
        if self._conflicts < CONFLICT_LIMIT:
            return True                     # telebot retries shortly
        # Not ours: another process polls this token. Each takes the stream
        # from the other in turn, so presses reach whichever holds it.
        # Nothing here can resolve it -- the fix is one token per printer --
        # so say that once and stop, rather than fight.
        self.last_error = (
            "this bot token is already being polled by another PiNozCam "
            "instance -- use one Telegram bot token per printer (a shared "
            "chat is fine)")
        self._logger.error(
            "Telegram refused polling with 409 Conflict: another client is "
            "polling this bot token. Alerts will still be delivered, but "
            "BUTTONS AND TYPED COMMANDS WILL NOT WORK on this printer. Give "
            "each printer its own bot token; they may share one chat.")
        self.running = False
        self.stop_event.set()
        bot = self._bot
        if bot is not None:
            bot.stop_polling()
        return True

    def _poll(self):
        """Long-poll until stopped, backing off on repeated failure."""
        bot = self._bot
        try:
            self._poll_loop()
        finally:
            with _RETIRING_LOCK:
                self._thread_done = True
            # Drops this client from the hand-over registry -- unless a
            # getUpdates of its own is still out, see _handing_over.
            _handing_over(self.token)
            self._finish(bot)

    def _poll_loop(self):
        """The loop behind _poll; returns when polling is over."""
        consecutive = 0
        backoff = 5
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                self._bot.infinity_polling(long_polling_timeout=60)
                # Returns normally only when stop_polling() was called.
                self._logger.info("Telegram bot polling stopped.")
                return
            except Exception as exc:                        # noqa: BLE001
                ran_for = time.monotonic() - started
                # getUpdates is exclusive: Telegram gives the update stream to
                # one consumer per token and answers everyone else with
                # "Conflict: terminated by other getUpdates request". Two
                # PiNozCam instances on one token therefore fight, each press
                # reaches whichever won, and the loser's retries only take the
                # stream away from the other one for a moment before losing it
                # again. Nothing here can resolve it -- the fix is a bot token
                # per printer -- so say that and stop.
                #
                if _is_conflict(exc):
                    self.last_error = (
                        "this bot token is already being polled by another "
                        "PiNozCam instance -- use one Telegram bot token per "
                        "printer (a shared chat is fine)")
                    self._logger.error(
                        "Telegram refused polling with 409 Conflict: another "
                        "client is polling this bot token. Alerts will still "
                        "be delivered, but BUTTONS AND TYPED COMMANDS WILL "
                        "NOT WORK on this printer. Give each printer its own "
                        "bot token; they may share one chat.")
                    self.running = False
                    return
                if ran_for >= HEALTHY_RUN:
                    consecutive, backoff = 0, 5
                consecutive += 1
                # redact(): the polling exception carries the same
                # token-bearing URL as the send path.
                self._logger.error(
                    "Telegram bot polling error after %.0fs (consecutive "
                    "%d/%d): %s", ran_for, consecutive, MAX_CONSECUTIVE,
                    credentials.redact(str(exc)))
                if consecutive >= MAX_CONSECUTIVE:
                    self._logger.error(
                        "Telegram bot polling failed %d times in a row "
                        "without staying up; stopping. Check the token and "
                        "the network, then save the settings to retry.",
                        consecutive)
                    self.last_error = (f"polling gave up after "
                                       f"{consecutive} failures")
                    self.running = False
                    return
                self._logger.info(
                    "Retrying Telegram bot polling in %d seconds...", backoff)
                if self.stop_event.wait(backoff):
                    return
                backoff = min(backoff * 3, 60)
