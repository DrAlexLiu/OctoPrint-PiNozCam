"""Telegram polling, delivery and button transport."""

import threading
import time

import requests
import telebot

from . import credentials

# A sufficiently long polling run resets the consecutive-failure counter.
HEALTHY_RUN = 120.0
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
            'Yes', callback_data='yes:%s:%s' % (action, token)),
        telebot.types.InlineKeyboardButton(
            'No', callback_data='no:%s:%s' % (action, token)),
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


class TelegramBot(object):
    """Long-poll client plus the REST calls that go with it.

    Sending does not need the poller: send() is an ordinary REST call, and
    the polling thread exists only so that a button press has somewhere to
    arrive. So a bot whose polling has died still delivers alerts.
    """

    def __init__(self, token, chat_id, logger, on_command):
        """Hold the credentials and the callback; connect nothing yet."""
        self.token = token
        self.chat_id = str(chat_id)
        self._logger = logger
        self.on_command = on_command        # (name, call) -> str | None

        self.running = False
        self.last_error = None
        self.thread = None
        self.stop_event = threading.Event()
        self._bot = None

    def _timed(self, what, fn):
        """Run fn and log its elapsed time."""
        started = time.monotonic()
        try:
            return fn()
        finally:
            self._logger.info("[timing] %s took %.0f ms", what,
                              (time.monotonic() - started) * 1000.0)

    # ---- ownership ----------------------------------------------------

    def _from_owner(self, source, what):
        """Accept commands only from the configured chat, failing closed."""
        chat_id = getattr(getattr(source, "chat", None), "id", None)
        if str(chat_id) == str(self.chat_id):
            return True
        self._logger.warning(
            "Ignoring %s from Telegram chat %r; this bot is configured for "
            "%r.", what, chat_id, self.chat_id)
        return False

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
            self._bot = telebot.TeleBot(self.token)
            self._register()
            self.stop_event.clear()
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

    def stop(self, timeout=10.0):
        """Stop polling and wait for the thread. Safe to call twice."""
        self.stop_event.set()
        self.running = False
        bot, self._bot = self._bot, None
        thread, self.thread = self.thread, None
        if bot is not None:
            try:
                bot.stop_polling()
                # stop_bot also closes the worker pool telebot may have
                # started; older versions do not have it.
                if hasattr(bot, "stop_bot"):
                    bot.stop_bot()
            except Exception as exc:                        # noqa: BLE001
                self._logger.error(
                    "Error occurred while stopping Telegram bot polling: %s",
                    credentials.redact(str(exc)))
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
            if thread.is_alive():
                self._logger.warning(
                    "Telegram polling thread has not stopped.")
                return False
        self._logger.info("Telegram bot has been stopped.")
        return True

    def verify(self):
        """Return an error string if Telegram rejects the credentials."""
        url = "https://api.telegram.org/bot%s/getChat" % self.token
        try:
            response = self._timed(
                "telegram verify",
                lambda: requests.get(url, params={"chat_id": self.chat_id},
                                     timeout=(5, 15)))
            if response.status_code == 200:
                self.last_error = None
                return None
            error = ("Telegram refused the credentials (HTTP %s)."
                     % response.status_code)
        except Exception as exc:                            # noqa: BLE001
            error = credentials.redact(str(exc))
        self._logger.error("Telegram check failed: %s", error)
        self.last_error = error
        return error

    # ---- sending ------------------------------------------------------

    def send(self, caption="", image=None, keyboard=None, silent=False):
        """Send one message and return its ID, or None on failure."""
        bot = self._bot
        if bot is None:
            return None
        try:
            if image:
                message = self._timed(
                    "telegram send_photo",
                    lambda: bot.send_photo(
                        self.chat_id, image, caption=caption,
                        reply_markup=keyboard, disable_notification=silent))
            else:
                message = self._timed(
                    "telegram send_message",
                    lambda: bot.send_message(
                        self.chat_id, text=caption, reply_markup=keyboard,
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
            self.on_command("/hi", None)

        @bot.message_handler(func=lambda message: True)
        def _anything_else(message):
            """Explain unsupported text sent by the configured owner."""
            if not self._from_owner(message, "a message"):
                return
            text = self.on_command("", None)
            if text:
                self.reply_to(message, text)

        @bot.callback_query_handler(func=lambda call: True)
        def _button(call):
            """Validate and dispatch one Telegram inline-button press."""
            # FAILS CLOSED. An earlier version skipped the check when the
            # chat id could not be read, which is backwards: a bot added to
            # a second group would otherwise take pause and stop from
            # anyone there.
            if not self._from_owner(getattr(call, "message", None),
                                    "a button"):
                self.answer(call.id, text="Not the configured chat.")
                return
            # button forever if the query goes unanswered, and the old
            # handler answered on several separate paths -- one of which
            # could be missed by a new branch.
            try:
                self.on_command(call.data or "", call)
            finally:
                self.answer(call.id)

    def _poll(self):
        """Long-poll until stopped, backing off on repeated failure."""
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
                    self.last_error = ("polling gave up after %d failures"
                                       % consecutive)
                    self.running = False
                    return
                self._logger.info(
                    "Retrying Telegram bot polling in %d seconds...", backoff)
                if self.stop_event.wait(backoff):
                    return
                backoff = min(backoff * 3, 60)
