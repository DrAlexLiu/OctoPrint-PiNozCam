"""Telling the user, and letting them answer.

Telegram and Discord, each configured by one credential pair -- a bot token
plus a chat or channel id -- and each interactive: messages carry buttons and
clicks come back. Also the credential redaction every log line on these paths
goes through, because both services put the URL -- which IS the credential --
into their exception messages.
"""

import queue
import re
import threading
import time
from io import BytesIO
from . import credentials
from .discord_bot import (DiscordBot, buttons as discord_buttons,
                          confirm_buttons as discord_confirm_buttons)
from .telegram_bot import (TelegramBot, buttons as telegram_buttons,
                           confirm_buttons as telegram_confirm_buttons,
                           send_draft as telegram_send_draft)


MUTED_LINE = ("All alerts are muted for this print -- on Telegram AND "
              "Discord. They come back on automatically when the next print "
              "starts.")

# A polling run this long counts as healthy: whatever ends it afterwards is
# a new problem, not a continuation of the last one. Long enough to be well
# past a token or connectivity failure, which show up immediately.
TELEGRAM_HEALTHY_RUN = 120.0


class AlertReceipt(object):
    """Collect final delivery outcomes and report once when all finish."""

    def __init__(self, expected, on_final, logger):
        """Track ``expected`` final delivery reports and call once."""
        self._lock = threading.Lock()
        self._expected = expected
        self._finished = 0
        self._sent = 0
        self._on_final = on_final
        self._logger = logger

    def report(self, sent):
        """One medium's final word. The last report triggers on_final."""
        with self._lock:
            self._finished += 1
            self._sent += 1 if sent else 0
            if self._finished != self._expected:
                return
            any_sent = self._sent > 0
        try:
            self._on_final(any_sent)
        except Exception:                               # noqa: BLE001
            self._logger.exception("Alert receipt callback failed.")


class NotifyMixin(object):
    """Mixed into PinozcamPlugin; see the module docstring."""

    MUTED_TEXT = ("🔇 " + MUTED_LINE + " Send /hi (Telegram) or press Check "
                  "to see the camera meanwhile.")
    UNMUTED_TEXT = "🔊 Alerts are back on."

    # ⚠️ Referenced by handle_discord_command's "help" branch below, which
    # existed with no constant defined -- an AttributeError the Discord
    # worker's own try/except (discord_bot.py) swallows into a log line, so
    # !help produced no reply at all, silently, for anyone who typed it.
    DISCORD_HELP = (
        "PiNozCam commands: `!check` -- the current camera view and "
        "printer status. `!status` -- printer status only, no image. "
        "`!pause` / `!resume` / `!stop` -- control the print (asks you "
        "to confirm first). `!mute` / `!unmute` -- toggle alerts. "
        "`!help` -- this message."
    )

    # Re-export shared credential helpers through the plugin mixin.
    TELEGRAM_TOKEN_RE = credentials.TELEGRAM_TOKEN_RE
    TELEGRAM_CHAT_RE = credentials.TELEGRAM_CHAT_RE
    DISCORD_CHANNEL_RE = credentials.DISCORD_CHANNEL_RE
    DISCORD_TOKEN_RE = credentials.DISCORD_TOKEN_RE

    redact = staticmethod(credentials.redact)
    _describe_pieces = staticmethod(credentials.describe_pieces)
    describe_telegram_problem = staticmethod(
        credentials.describe_telegram_problem)
    describe_discord_problem = staticmethod(
        credentials.describe_discord_problem)
    telegram_chat_id_error = staticmethod(
        credentials.telegram_chat_id_error)
    discord_channel_id_error = staticmethod(
        credentials.discord_channel_id_error)

    def _timed(self, what, fn):
        """Run fn, log elapsed time and return its result with milliseconds."""
        started = time.monotonic()
        try:
            return fn(), (time.monotonic() - started) * 1000.0
        finally:
            self._logger.info("[timing] %s took %.0f ms", what,
                              (time.monotonic() - started) * 1000.0)

    def discord_send_draft(self, token, channel_id, image=None, caption=""):
        """Test unsaved Discord credentials without adding buttons."""
        stream = None
        if image is not None:
            stream = BytesIO()
            image.save(stream, format="JPEG")
            stream.seek(0)
        bot = DiscordBot(token, channel_id, self._logger,
                         self.handle_discord_command)
        if not bot.verify():
            raise RuntimeError(bot.last_error)
        if not bot.send(content=caption, image=stream, silent=True):
            raise RuntimeError(
                "Discord accepted the credentials but refused the message. "
                "The usual cause is channel permissions: the bot needs View "
                "Channel, Send Messages and Attach Files in that channel.")
        return True

    def handle_telegram_command(self, command, call):
        """Execute one authorised Telegram message or button command."""
        if call is None:
            if command == "/hi":
                image, caption = self.check_reply()
                self.telegram_send_with_reply(image=image, caption=caption,
                                              reply_buttons=4,
                                              disable_notification=True)
                return None
            return ("I am PiNozCam. Send or click /hi or click Check button "
                    "from previous messages to see the current camera view "
                    "and printer info.")

        message_id = call.message.message_id
        data = command
        if data.split(":")[0] in ("yes", "no"):
            parts = data.split(":")
            verb = parts[0]
            action = parts[1] if len(parts) > 2 else ""
            nonce = parts[2] if len(parts) > 2 else ""
            if not action:
                self.telegram_send_with_reply(
                    caption="That button is from an older message and no "
                            "longer works. Press Pause or Stop again.",
                    reply_buttons=0, disable_notification=True)
                return
            if verb == "no":
                self._drop_confirm(action, nonce, channel="telegram")
                self._logger.info(
                    "Telegram user declined %s (message %s)",
                    action, message_id)
                self.telegram_send_with_reply(
                    caption="Never Mind.", reply_buttons=0,
                    disable_notification=True)
                return
            verdict = self._consume_confirm(action, nonce, "telegram")
            if verdict == "expired":
                self.telegram_send_with_reply(
                    caption="You have to respond within %d seconds."
                            % self.CONFIRM_TTL,
                    reply_buttons=0, disable_notification=True)
            elif verdict != "ok":
                self._logger.warning(
                    "Ignoring a Telegram %s confirmation that is no "
                    "longer valid (earlier print, already used, or "
                    "declined).", action)
                self.telegram_send_with_reply(
                    caption="That button is no longer valid. Press "
                            "Pause or Stop again if you still want it.",
                    reply_buttons=0, disable_notification=True)
            else:
                # Same re-check as the Discord side: the print can end
                # between the offer and the tap, and pause/cancel on an
                # idle printer is a no-op that was reported as done.
                problem = self._action_state_problem(action)
                if problem is not None:
                    self._logger.info(
                        "Telegram confirmation of %s refused: %s", action,
                        problem)
                    self.telegram_send_with_reply(
                        caption=problem, reply_buttons=0,
                        disable_notification=True)
                    return
                self._logger.info("Telegram user confirmed %s", action)
                if action == "pause":
                    self._printer.pause_print()
                    self.current_telegram_message_paused = True
                    self.telegram_send_with_reply(
                        caption="The print job has been paused.",
                        reply_buttons=0, disable_notification=True)
                elif action == "resume":
                    self._printer.resume_print()
                    self.current_telegram_message_paused = False
                    self.telegram_send_with_reply(
                        caption="The print job has been resumed.",
                        reply_buttons=0, disable_notification=True)
                elif action == "stop":
                    self._printer.cancel_print()
                    self.telegram_send_with_reply(
                        caption="The print job has been stopped.",
                        reply_buttons=0, disable_notification=True)
            return
        if call.data == "check":
            self._logger.info(
                "User clicked 'Check' button for message ID: %s", message_id)
            image, caption = self.check_reply()
            self.telegram_send_with_reply(image=image, caption=caption,
                                          reply_buttons=4,
                                          disable_notification=True)
        elif call.data in ("mute", "unmute"):
            # too. See PinozcamPlugin.__init__ for why. Set, never
            # toggled -- the button says which way it goes.
            self.alerts_muted = call.data == "mute"
            self._logger.info(
                "User clicked '%s' for message ID: %s",
                "Mute" if self.alerts_muted else "Unmute", message_id)
            self.telegram_send_with_reply(
                caption=(self.MUTED_TEXT if self.alerts_muted
                         else self.UNMUTED_TEXT),
                reply_buttons=0, disable_notification=True)
        # Printer state, not message bookkeeping, authorises actions.
        elif call.data in ("pause", "stop"):
            paused = self._printer.get_state_id() == "PAUSED"
            action = "resume" if (call.data == "pause" and paused) \
                else call.data
            problem = self._action_state_problem(action)
            if problem is not None:
                self._logger.info(
                    "Telegram %s offer refused: %s", action, problem)
                self.telegram_send_with_reply(
                    caption=problem, reply_buttons=0,
                    disable_notification=True)
                return None
            self._logger.info("User clicked '%s' button for message ID: %s",
                              call.data.capitalize(), message_id)
            self.telegram_send_with_reply(
                caption="Are you sure you want to %s the print job?" % action,
                reply_buttons=2, disable_notification=True,
                confirm=(action, self._issue_confirm(action, "telegram")))
        return None

    def telegram_verify(self):
        """Ask Telegram whether the saved credentials work, or None.

        Delegates; see TelegramBot.verify for why it runs AFTER setup and
        why it does not publish its own answer.
        """
        bot = self.telegram_bot
        if bot is None:
            return "Telegram is not set up."
        return bot.verify()

    # ---- the four things ChannelManager calls back into ----------------

    def _setup_channel(self, name):
        """Rebuild one channel. Returns whether it came up."""
        if name == "telegram":
            self._timed("telegram setup", self.setup_telegram_bot)
            return self.telegram_server_running
        self._timed("discord setup", self.setup_discord_bot)
        return self.discord_bot is not None

    def _verify_channel(self, name, guard):
        """Check one channel's credentials and colour its chip.

        `guard()` says whether this generation still owns the settings, so
        a slow answer for credentials that have already been replaced
        publishes nothing.
        """
        if name == "telegram":
            if not (self.enable_telegram and self.telegram_server_running):
                return
            error = self.telegram_verify()
            if guard():
                self.telegram_error = error
            return
        bot = self.discord_bot
        if bot is not None:
            # bot.verify writes to the BOT object, and a superseded
            # generation's bot is orphaned -- _discord_status reads
            # self.discord_bot, so a stale write cannot reach the chip.
            self._timed("discord verify", bot.verify)

    def _welcome_image(self):
        """The picture a welcome carries. Slow: it fetches a camera frame."""
        image, _ms = self._timed(
            "welcome camera fetch",
            lambda: self.notification_image(self.welcome_text))
        return image

    def _welcome_send(self, media, image):
        """Deliver the welcome to the channels that are still current."""
        self._timed("welcome send to %s" % ",".join(media), lambda:
                    self.notify_all(
                        "Welcome to PiNozCam! Press the buttons on an "
                        "alert, or send /hi (Telegram) or !check "
                        "(Discord) to see the camera and the printer "
                        "status. Send !help in Discord for the full "
                        "command list.",
                        image=image, buttons=False, silent=True,
                        respect_mute=False, only=media))

    # ---- what the rest of the plugin still calls -----------------------

    def start_channel_setup(self, telegram=False, discord=False,
                            welcome=False):
        """Start selected channels away from the caller's request thread."""
        return self.channels.request(telegram=telegram, discord=discord,
                                     welcome=welcome)

    def channel_setup_running(self, channel=None):
        """True while that channel's setup is still connecting."""
        return self.channels.running(channel)

    def setup_telegram_bot(self):
        """Restart Telegram when enabled and fully configured."""
        self.stop_telegram_bot()
        if self.shutting_down.is_set():
            return
        if not (self.enable_telegram and self.telegram_bot_token
                and self.telegram_chat_id):
            # Not an error: no credentials means the user has not
            # configured Telegram, which is the default state.
            self._logger.info(
                "Telegram is not configured; not starting the bot.")
            return
        bot = TelegramBot(self.telegram_bot_token, self.telegram_chat_id,
                          self._logger, self.handle_telegram_command)
        try:
            running = bot.start()
        except Exception:
            bot.stop(timeout=0)
            raise
        if self.shutting_down.is_set():
            bot.stop(timeout=0)
            return
        self.telegram_bot = bot
        self.telegram_server_running = running

    def stop_telegram_bot(self):
        """Detach and stop the active Telegram client."""
        bot, self.telegram_bot = self.telegram_bot, None
        self.telegram_server_running = False
        if bot is not None:
            bot.stop()

    def telegram_send_with_reply(self, image=None, caption='',
                                 reply_buttons=0,
                                 disable_notification=False, confirm=None):
        """Send Telegram content with optional controls or confirmation."""
        bot = self.telegram_bot
        if bot is None:
            return False
        keyboard = None
        if reply_buttons == 2:
            if not confirm:
                # Refusing is the safe direction: a Yes/No row with no
                # nonce cannot be answered, so sending one would offer the
                # user a button that never works.
                self._logger.error(
                    "Refusing to send a Telegram confirmation with no "
                    "token; this is a programming error.")
                return False
            keyboard = telegram_confirm_buttons(*confirm)
        elif reply_buttons == 4:
            # Read printer state directly so both channels show the same action.
            keyboard = telegram_buttons(
                paused=self._printer.get_state_id() == "PAUSED",
                muted=self.alerts_muted)
        message_id = bot.send(caption=caption, image=image,
                              keyboard=keyboard,
                              silent=disable_notification)
        if message_id is None:
            return False
        if self.ai_running:
            self.current_telegram_message_set.add(message_id)
        return True

    def telegram_send_draft(self, token, chat_id, image=None, caption=""):
        """Send with credentials that are NOT the saved ones, for Test."""
        telegram_send_draft(token, chat_id, image=image, caption=caption)

    def setup_discord_bot(self):
        """Restart Discord when enabled and fully configured."""
        self.stop_discord_bot()
        if self.shutting_down.is_set():
            return
        # See setup_telegram_bot: unticked means no gateway at all.
        if not (self.enable_discord and self.discord_bot_token
                and self.discord_channel_id):
            return
        bot = DiscordBot(self.discord_bot_token, self.discord_channel_id,
                         self._logger, self.handle_discord_command,
                         printer_id=self.printer_id)
        try:
            bot.start()
        except Exception:
            bot.stop(timeout=0)
            raise
        if self.shutting_down.is_set():
            bot.stop(timeout=0)
            return
        self.discord_bot = bot

    def stop_discord_bot(self):
        """Stop the Discord client, if one is running."""
        bot, self.discord_bot = self.discord_bot, None
        if bot is not None:
            bot.stop()

    def _media(self):
        """Every medium a message can go out through.

        (name, configured, muted, send) per medium. Adding a third service
        means adding a row here; it must never mean adding a call site.
        """
        return (
            ("telegram",
             bool(self.enable_telegram and self.telegram_bot_token
                  and self.telegram_chat_id),
             bool(self.alerts_muted),
             self._send_telegram),
            ("discord",
             bool(self.enable_discord) and self.discord_bot is not None,
             bool(self.alerts_muted),
             self._send_discord),
        )

    def _send_telegram(self, caption, image, buttons, silent):
        """Send one Telegram message and return final success."""
        return bool(self.telegram_send_with_reply(
            image=image, caption=caption,
            reply_buttons=4 if buttons else 0,
            disable_notification=silent))

    def _send_discord(self, caption, image, buttons, silent):
        """One message to Discord. Returns False on failure.

        discord_deliver, not a notify-level helper: the mute check belongs
        to notify_all, which applies it to every medium by the same rule.
        """
        components = None
        if buttons:
            # Derived here rather than passed in, so the two media cannot be
            # given different ideas of whether the printer is paused.
            components = discord_buttons(
                self.printer_id,
                paused=self._printer.get_state_id() == "PAUSED",
                muted=self.alerts_muted)
        return self.discord_deliver(image=image, caption=caption,
                                    components=components, silent=silent)

    # Messages one medium may have waiting. Small on purpose: this is an
    # alert channel, not a mail spool, and a backlog of stale spaghetti
    # warnings helps nobody. Overflow drops the OLDEST -- see _enqueue.
    NOTIFY_QUEUE_MAX = 8
    # How many times a worker retries one message before giving up on it.
    NOTIFY_SEND_ATTEMPTS = 3

    def _medium_worker(self, name, send, work):
        """Drain one medium's bounded queue in order."""
        while True:
            job = work.get()
            try:
                if job is None:                     # shutdown sentinel
                    return
                caption, image, buttons, silent, receipt = job
                if self.shutting_down.is_set():
                    if receipt is not None:
                        receipt.report(False)
                    return
                sent = False
                for attempt in range(1, self.NOTIFY_SEND_ATTEMPTS + 1):
                    try:
                        if send(caption, image, buttons, silent):
                            sent = True
                            break
                    except Exception as exc:        # noqa: BLE001
                        self._logger.error("%s message failed: %s", name,
                                           self.redact(str(exc)))
                    if attempt == self.NOTIFY_SEND_ATTEMPTS:
                        self._logger.error(
                            "Giving up on a %s message after %d attempts.",
                            name, attempt)
                        break
                    # The retry belongs HERE rather than in the detection
                    # loop: a frame-coupled retry spent one attempt per
                    # frame, so its spacing was whatever the board's
                    # inference speed happened to be.
                    if self.shutting_down.wait(2.0 * attempt):
                        # Shutting down mid-job: the message did not go out,
                        # and the receipt must not be left dangling.
                        if receipt is not None:
                            receipt.report(False)
                        return
                # The medium's FINAL word, success or exhausted retries --
                # this is what lets the alert path account for delivery
                # instead of for queueing.
                if receipt is not None:
                    receipt.report(sent)
            except Exception:                       # noqa: BLE001
                self._logger.exception("Notification worker error.")
            finally:
                work.task_done()

    def _enqueue(self, name, send, job):
        """Hand one message to a medium's worker. Never blocks.

        Returns True if it was accepted. A full queue drops the OLDEST
        waiting message rather than this one: the newest alert describes
        the printer as it is NOW, and the stale one it replaces described a
        state the user can no longer act on.
        """
        with self.notify_queue_lock:
            if self.shutting_down.is_set():
                return False
            entry = self.notify_queues.get(name)
            if entry is None:
                work = queue.Queue(maxsize=self.NOTIFY_QUEUE_MAX)
                thread = threading.Thread(
                    target=self._medium_worker, args=(name, send, work),
                    name="pinozcam-notify-%s" % name)
                thread.daemon = True
                self.notify_queues[name] = (work, thread)
                thread.start()
                entry = (work, thread)
            work = entry[0]
            while True:
                try:
                    work.put_nowait(job)
                    return True
                except queue.Full:
                    try:
                        evicted = work.get_nowait()
                        work.task_done()
                        self._logger.warning(
                            "%s queue is full; dropped the oldest waiting "
                            "message.", name)
                        if evicted is not None and evicted[4] is not None:
                            evicted[4].report(False)
                    except queue.Empty:
                        return False

    def stop_notification_workers(self, timeout=1.0):
        """Discard queued sends, wake every worker and join within timeout."""
        with self.notify_queue_lock:
            entries = list(self.notify_queues.items())
            self.notify_queues = {}

        for _name, (work, _thread) in entries:
            while True:
                try:
                    job = work.get_nowait()
                except queue.Empty:
                    break
                try:
                    if job is not None and job[4] is not None:
                        job[4].report(False)
                finally:
                    work.task_done()
            work.put_nowait(None)

        deadline = time.monotonic() + max(0.0, timeout)
        for name, (_work, thread) in entries:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                self._logger.warning(
                    "%s notification worker still running at shutdown.",
                    name)

    def notify_all(self, caption, image=None, buttons=False, silent=False,
                   respect_mute=True, respect_confirm=False, only=None,
                   wait=True, on_settled=None):
        """Send one message through every configured medium.

        Returns ``(attempted, delivered)``. With ``wait=False``, ``delivered``
        counts jobs accepted by workers, not messages delivered by transports.
        When supplied, ``on_settled(any_sent)`` reports the final result once
        all queued calls finish. ``only`` limits the targeted media.
        """
        if self.shutting_down.is_set():
            return 0, 0
        attempted = delivered = 0
        targets = []
        for name, configured, muted, send in self._media():
            if only is not None and name not in only:
                continue
            if not configured:
                continue
            if respect_mute and muted:
                continue
            # Hold off while a Yes/No is outstanding on THAT medium, so a new
            # alert does not push the question the user is answering off
            # their screen. Per medium, so a pending Discord question does
            # not silence Telegram.
            if respect_confirm and self._confirm_pending(name):
                continue
            attempted += 1
            if not wait:
                targets.append((name, send))
                continue
            try:
                if send(caption, image, buttons, silent):
                    delivered += 1
            except Exception as exc:                        # noqa: BLE001
                self._logger.error("%s message failed: %s", name,
                                   self.redact(str(exc)))
        if not wait and targets:
            # The receipt's expected count must be final BEFORE the first
            # job is queued: a worker can report the moment its job lands,
            # and an expected count still growing at that point would fire
            # on_settled early. Hence the two phases.
            receipt = None
            if on_settled is not None:
                receipt = AlertReceipt(len(targets), on_settled,
                                       self._logger)
            for name, send in targets:
                try:
                    accepted = self._enqueue(
                        name, send,
                        (caption, image, buttons, silent, receipt))
                except Exception as exc:                  # noqa: BLE001
                    self._logger.error(
                        "%s message could not be queued: %s", name,
                        self.redact(str(exc)))
                    accepted = False
                if accepted:
                    delivered += 1
                elif receipt is not None:
                    # Never queued, never will be sent -- the receipt hears
                    # it here or waits forever.
                    receipt.report(False)
        return attempted, delivered

    def discord_deliver(self, image=None, caption="", components=None,
                        silent=False):
        """Post one Discord message via the bot transport."""
        bot = self.discord_bot
        if bot is None:
            return False
        stream = None
        if image is not None:
            stream = BytesIO()
            image.save(stream, format="JPEG")
            stream.seek(0)
        # nowhere is worse than no button: Discord answers the user with
        # "The application did not respond in time", which reads as a broken
        # plugin rather than a dropped connection.
        if components and not bot.connected:
            components = None
        return bot.send(content=caption, image=stream, components=components,
                        silent=silent)

    def _action_state_problem(self, action):
        """Return why action is invalid for the current printer state."""
        state = self._printer.get_state_id()
        if action == "pause":
            if state in ("PRINTING", "RESUMING"):
                return None
            if state in ("PAUSED", "PAUSING"):
                return "The print is already paused."
            return "There is no active print job."
        if action == "resume":
            if state in ("PAUSED", "PAUSING"):
                return None
            if state in ("PRINTING", "RESUMING"):
                return "The print is not paused."
            return "There is no active print job."
        if action == "stop":
            if state in ("PRINTING", "PAUSED", "PAUSING", "RESUMING"):
                return None
            return "There is no active print job."
        return None

    def handle_discord_command(self, command, interaction):
        """Run one command, from a button click or a typed message.

        interaction is None for typed commands, in which case replies go to
        the channel instead of back through the interaction token.
        """
        def reply(text, components=None, image=None):
            """Send one reply to an interaction or channel."""
            stream = None
            if image is not None:
                stream = BytesIO()
                image.save(stream, format="JPEG")
                stream.seek(0)
            bot = self.discord_bot
            if interaction is not None and bot is not None:
                bot.followup(interaction, text, components=components,
                             image=stream, silent=True)
            elif bot is not None:
                bot.send(content=text, image=stream, components=components,
                         silent=True)

        if command.startswith("confirm:"):
            parts = command.split(":")
            action = parts[1] if len(parts) > 1 else ""
            token = parts[2] if len(parts) > 2 else ""
            verdict = self._consume_confirm(action, token, "discord")
            if verdict == "stale":
                self._logger.warning(
                    "Ignoring a Discord %s button that does not match the "
                    "current request (earlier print, already used, or "
                    "withdrawn by Cancel).", action or "confirm")
                reply("That button is no longer valid. Press Pause or Stop "
                      "again if you still want it.", components=[])
                return
            if verdict == "expired":
                reply("That request expired. Press it again if you still "
                      "want it.", components=[])
                return
            # The print state may have changed since confirmation was offered.
            problem = self._action_state_problem(action)
            if problem is not None:
                self._logger.info(
                    "Discord confirmation of %s refused: %s", action,
                    problem)
                reply(problem, components=[])
                return
            if action == "pause":
                self._printer.pause_print()
            elif action == "resume":
                self._printer.resume_print()
            elif action == "stop":
                self._printer.cancel_print()
            self._logger.info("Discord user confirmed %s", action)
            reply({
                "pause": "Print paused.",
                "resume": "Print resumed.",
                "stop": "Print stopped.",
            }[action], components=[])
            return
        if command.startswith("cancel"):
            # Cancel must invalidate the paired confirmation nonce.
            parts = command.split(":")
            action = parts[1] if len(parts) > 1 else ""
            token = parts[2] if len(parts) > 2 else None
            if action:
                self._drop_confirm(action, token, channel="discord")
            else:
                # An old message whose button predates the action-carrying
                # id. Withdraw everything rather than leave a live Yes.
                self._new_confirm_scope()
            reply("Never mind.", components=[])
            return

        if command == "help":
            reply(self.DISCORD_HELP)
        elif command == "status":
            title, state, progress, nozzle, bed, meta = self.get_printer_status()
            body = ("Printer: %s\nStatus: %s\nProgress: %s\n"
                    "Nozzle: %s\u00b0C\nBed: %s\u00b0C"
                    % (title, state, progress, nozzle, bed))
            if meta:
                body += "\nFile: %s" % meta.get("name", "Unknown")
            reply(body)
        elif command == "check":
            # Camera and printer availability are independent.
            image, caption = self.check_reply()
            # get_state_id(), the idiom the rest of this file and detect.py
            # already use, rather than is_paused(): one way of asking, so the
            # label on this row cannot disagree with an alert's row.
            reply(caption, image=image,
                  components=discord_buttons(
                      self.printer_id,
                      paused=self._printer.get_state_id() == "PAUSED",
                      muted=self.alerts_muted))
        elif command in ("mute", "unmute"):
            # One target-state flag mutes both media idempotently.
            self.alerts_muted = command == "mute"
            # The SAME string both platforms use, from one constant, because
            # a promise that differs between them is one a user has to
            # discover. It says "all alerts" because that is now true.
            reply(self.MUTED_TEXT if self.alerts_muted else self.UNMUTED_TEXT)
        elif command in ("pause", "resume", "stop"):
            # A confirmation step, for the same reason the Telegram path has
            # one: a mis-tap should not end a nine-hour print. The buttons
            # make it one more tap rather than a typed word.
            paused = self._printer.get_state_id() == "PAUSED"
            action = "resume" if (command == "pause" and paused) else command
            # No offer for a printer that cannot honour it: a Yes/No over
            # an idle printer is a live Stop button waiting for the NEXT
            # print to start inside its TTL.
            problem = self._action_state_problem(action)
            if problem is not None:
                reply(problem)
                return
            token = self._issue_confirm(action, "discord")
            reply("Confirm: %s the print? (valid for %ds)"
                  % (action, self.CONFIRM_TTL),
                  components=discord_confirm_buttons(
                      self.printer_id, action, token))

    def _discord_status(self):
        """Return the Discord status-chip state."""
        if not self.enable_discord:
            return "OFF"
        if self.alerts_muted:
            return "MUTED"
        bot = self.discord_bot
        if bot is None:
            # Configured but not set up YET is not the same as not
            # configured -- see channel_setup_running().
            if (self.discord_bot_token and self.discord_channel_id
                    and self.channel_setup_running("discord")):
                return "CONNECTING"
            return "OFF"
        if bot.connected:
            return "ON"
        return "FAILED" if bot.last_error else "CONNECTING"

    def check_reply(self):
        """Build the shared Telegram/Discord camera and printer reply."""
        title, state, progress, nozzle_temp, bed_temp, file_metadata = (
            self.get_printer_status())
        caption = (f"Printer: {title}\nStatus: {state}\n"
                   f"Progress: {progress}\nNozzle Temp: {nozzle_temp}°C\n"
                   f"Bed Temp: {bed_temp}°C")
        if file_metadata:
            caption += f"\nFile: {file_metadata.get('name', 'Unknown')}"
        # The user-facing view is unmasked; camera failures use a placeholder.
        try:
            image = self.current_view_image()
        except Exception as exc:                        # noqa: BLE001
            self._logger.warning("Check could not get a frame: %s",
                                 self.redact(str(exc)))
            image = None
        if image is None:
            caption += ("\n⚠️ No camera connected -- failure detection is "
                        "blind, but the print above is unaffected.")
            image = self.create_no_camera_image()
        return image, caption

    def printer_label(self):
        """Return the appearance name or a stable short instance label."""
        name = self._settings.global_get(["appearance", "name"])
        if name and str(name).strip():
            return str(name).strip()
        if self.printer_id:
            return "printer-%s" % self.printer_id[:8]
        # No name and no id yet: only reachable before the first settings
        # load, and "Unnamed" is at least not the word None.
        return "Unnamed printer"

    def get_printer_status(self):
        """Return job progress, temperatures and current-file metadata."""

        title = self.printer_label()

        # Get the current printer data
        printer_data = self._printer.get_current_data()

        # Get the printer state
        state_id = self._printer.get_state_id()

        # A printer whose USB has dropped is one of the two independent
        # things that can go wrong here -- the other being the camera -- and
        # it changes what the buttons can do: Pause and Stop have nothing to
        # command. "❓ Unknown" for that left the user with no idea which of
        # the two had failed, on the one message they went looking for an
        # answer in.
        if state_id == "PRINTING":
            state = "🖨️ Printing"
            # OctoPrint reports completion as a percentage from 0 to 100.
            progress = printer_data['progress']['completion']
        elif state_id == "PAUSED":
            state = "⏸️ Paused"
            progress = printer_data['progress']['completion']
        elif state_id == "OPERATIONAL":
            state = "⏹️ Idle"
            progress = 0
        elif state_id in ("OFFLINE", "CLOSED"):
            state = "🔌 Printer disconnected"
            progress = 0
        elif state_id in ("CLOSED_WITH_ERROR", "OFFLINE_AFTER_ERROR",
                          "ERROR"):
            state = "🔌 Printer disconnected (error)"
            progress = 0
        elif state_id in ("CONNECTING", "DETECT_SERIAL", "DETECT_BAUDRATE"):
            state = "🔌 Connecting to the printer"
            progress = 0
        elif state_id in ("CANCELLING", "FINISHING"):
            state = "⏹️ Finishing"
            progress = printer_data['progress']['completion']
        else:
            # Still reachable: OctoPrint can add states, and it is better to
            # show the raw id than to hide it behind a question mark.
            state = "❓ %s" % state_id
            progress = 0

        # Initialize temperature variables
        nozzle_temp = 0
        bed_temp = 0

        # Set default values if any value is None
        state = state or "Unknown"
        progress = f"{progress:.1f}%" if progress is not None else "0.0%"

        # Get temperature information
        temperatures = {}
        for k, v in self._printer.get_current_temperatures().items():
            if re.search(r'^(tool\d+|bed|chamber)$', k):
                temperatures[k] = v
        nozzle_temp = temperatures.get('tool0', {}).get('actual', nozzle_temp)
        bed_temp = temperatures.get('bed', {}).get('actual', bed_temp)

        # Get file metadata
        file_metadata = printer_data.get('job', {}).get('file', {})

        return title, state, progress, nozzle_temp, bed_temp, file_metadata
