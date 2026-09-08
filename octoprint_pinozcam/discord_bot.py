"""Discord REST delivery and outbound Gateway interaction handling."""

import json
import queue
import random
import threading
import time

import requests

from . import credentials

API = "https://discord.com/api/v10"

redact = credentials.redact


# Gateway opcodes.
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

# Interaction callback types.
CB_DEFERRED_UPDATE = 6          # acknowledge a button without editing yet

# Message flags. SUPPRESS_NOTIFICATIONS is Discord's equivalent of Telegram's
# disable_notification: the message is delivered and stays in the channel, but
# triggers no push and no sound -- what the Discord client calls sending
# "@silent". Used for everything that is a REPLY or a confirmation; a real
# failure alert must buzz, so those are sent without it.
FLAG_SUPPRESS_NOTIFICATIONS = 1 << 12

# Gateway intents. GUILD_MESSAGES | MESSAGE_CONTENT, wanted only for typed
# commands; MESSAGE_CONTENT is privileged and the connection is retried
# without it if Discord refuses, because buttons need no intent at all.
INTENTS_WITH_TEXT = (1 << 9) | (1 << 15)
INTENTS_BUTTONS_ONLY = 0

# Close code Discord sends when an app asks for a privileged intent it has
# not been granted.
CLOSE_DISALLOWED_INTENTS = 4014
# Close codes where reconnecting cannot help: fix the config instead.
FATAL_CLOSE_CODES = (4004, 4010, 4011, 4012, 4013, 4014)


# Discord's fixed component style values.
STYLE_PRIMARY = 1     # blurple
STYLE_SECONDARY = 2   # grey
STYLE_DANGER = 4      # red


# The prefix every custom_id carries, so a press can be attributed to the
# instance that minted it.
ID_PREFIX = "pnc"


def tag(printer_id, command):
    """Tag a command with its printer instance for shared-channel routing."""
    if not printer_id:
        return command
    return f"{ID_PREFIX}:{printer_id}:{command}"


def untag(custom_id):
    """Return printer ID and command from a tagged component ID."""
    parts = (custom_id or "").split(":", 2)
    if len(parts) == 3 and parts[0] == ID_PREFIX:
        return parts[1], parts[2]
    return None, custom_id or ""


def buttons(printer_id, paused=False, muted=False):
    """Build alert controls, matching Telegram labels and layout."""
    return [
        {
            "type": 1,
            "components": [
                {"type": 2, "style": STYLE_PRIMARY, "label": "Check",
                 "emoji": {"name": "\U0001F50D"},        # magnifying glass
                 "custom_id": tag(printer_id, "check")},
                # Target-state commands make stale buttons idempotent.
                {"type": 2, "style": STYLE_SECONDARY,
                 "label": "Unmute" if muted else "Mute",
                 "emoji": {"name": "\U0001F50A" if muted else "\U0001F507"},
                 "custom_id": tag(printer_id,
                                  "unmute" if muted else "mute")},
            ],
        },
        {
            "type": 1,
            "components": [
                {"type": 2, "style": STYLE_SECONDARY,
                 "label": "Resume" if paused else "Pause",
                 "emoji": {"name": "\u25B6\uFE0F" if paused
                                   else "\u23F8\uFE0F"},
                 "custom_id": tag(printer_id, "pause")},
                {"type": 2, "style": STYLE_DANGER, "label": "Stop",
                 "emoji": {"name": "\U0001F6D1"},        # octagonal sign
                 "custom_id": tag(printer_id, "stop")},
            ],
        },
    ]


def confirm_buttons(printer_id, action, token):
    """Build confirmation controls bound to one action nonce."""
    return [{
        "type": 1,
        "components": [
            {"type": 2, "style": 4, "label": f"Yes, {action}",
             "custom_id": tag(printer_id,
                              f"confirm:{action}:{token}")},
            {"type": 2, "style": 2, "label": "Cancel",
             "custom_id": tag(printer_id,
                              f"cancel:{action}:{token}")},
        ],
    }]


class DiscordBot:
    """Single-threaded Gateway client with independent REST delivery."""

    def __init__(self, token, channel_id, logger, on_command,
                 printer_id=""):
        """Store credentials, printer identity and command callback."""
        self.token = token
        self.channel_id = str(channel_id)
        self.printer_id = str(printer_id or "")
        self._logger = logger
        self.on_command = on_command        # (name, interaction) -> str | None

        self.running = False
        self.connected = False
        self.thread = None
        self.stop_event = threading.Event()
        self.last_error = None
        # Keep blocking callbacks off the Gateway heartbeat thread. Printer
        # controls have a separate, higher-priority bounded queue.
        self._controls = queue.Queue(maxsize=8)
        self._commands = queue.Queue(maxsize=2)
        self._cmd_worker = None

        self._ws = None
        self._session_id = None
        self._resume_url = None
        self._sequence = None
        self._intents = INTENTS_WITH_TEXT
        self.text_commands = False          # set once the gateway accepts them
        # Track whether a close followed a privileged-intent IDENTIFY.
        self._reached_ready = False
        self._identified_privileged = False
        self._identify_rejections = 0

    # ---- REST ---------------------------------------------------------

    def _headers(self):
        """Authorization and User-Agent for every REST call."""
        return {
            "Authorization": f"Bot {self.token}",
            "User-Agent": "PiNozCam (https://github.com/DrAlexLiu, 1.1)",
        }

    # Stamped at the top of every connection ATTEMPT (_session), read by
    # the READY handler, so its figure is this handshake and not uptime.
    _started_at = None

    def _timed(self, what, fn):
        """Run `fn` and log how long it took. See NotifyMixin._timed.

        Duplicated rather than shared because DiscordBot is deliberately
        standalone -- it takes a logger and a callback and knows nothing
        about the plugin.
        """
        started = time.monotonic()
        try:
            return fn()
        finally:
            self._logger.info("[timing] %s took %.0f ms", what,
                              (time.monotonic() - started) * 1000.0)

    def verify(self):
        """Check the token can see the channel before starting a thread."""
        try:
            response = self._timed(
                "discord verify",
                lambda: requests.get(f"{API}/channels/{self.channel_id}",
                                     headers=self._headers(),
                                     timeout=(5, 15)))
        except requests.exceptions.RequestException as exc:
            # last_error is shown in the UI and logged, and a gateway error
            # can quote the URL it was given.
            self.last_error = f"cannot reach Discord: {redact(exc)}"
            return False
        if response.status_code == 200:
            return True
        self.last_error = (
            f"Discord refused the credentials "
            f"(HTTP {response.status_code}). Check the bot token, that the "
            "bot was invited to the server, and that it can see the "
            "channel.")
        return False

    def send(self, content="", image=None, components=None, silent=False):
        """Post as the bot, so the message can carry real buttons.

        silent=True suppresses the push notification, matching what the
        Telegram side passes disable_notification=True for: replies the user
        asked for by pressing a button, and confirmations. An ALERT is the
        one thing that must interrupt, so it is sent without this.
        """
        # FILE NAME, and a file called "@everyone bracket.gcode" would
        # otherwise ping a whole server. Discord resolves mentions from the
        # message text unless this says not to, and suppressing it here is
        # the only place that covers every caller.
        payload = {"content": content,
                   "allowed_mentions": {"parse": []}}
        if components:
            payload["components"] = components
        if silent:
            payload["flags"] = FLAG_SUPPRESS_NOTIFICATIONS
        try:
            if image is not None:
                response = self._timed(
                    "discord send (with image)",
                    lambda: requests.post(
                        f"{API}/channels/{self.channel_id}/messages",
                        headers=self._headers(),
                        data={"payload_json": json.dumps(payload)},
                        files={
                            "files[0]": (
                                "nozcam.jpg", image, "image/jpeg")
                        },
                        timeout=(5, 20),
                    ),
                )
            else:
                headers = dict(self._headers())
                headers["Content-Type"] = "application/json"
                response = self._timed("discord send (text)", lambda: requests.post(
                    f"{API}/channels/{self.channel_id}/messages",
                    headers=headers, data=json.dumps(payload),
                    timeout=(5, 20)))
            if response.status_code in (200, 201):
                return True
            self._logger.error("Discord send failed: HTTP %s %s",
                               response.status_code, response.text[:200])
        except requests.exceptions.RequestException as exc:
            self._logger.error("Discord send failed: %s", redact(exc))
        return False

    def ack(self, interaction):
        """Acknowledge a click inside the three-second deadline.

        A deferred ACK is what buys the time to actually do the work: the
        initial response must land within 3 s or the token is invalidated,
        but after deferring, the token stays usable for about 15 minutes --
        long enough to grab a snapshot or pause the printer.
        """
        try:
            # Tight timeouts: the whole acknowledgement has to land inside
            # Discord's 3 second window, so waiting 5 s for a read is
            # waiting past the point where it can still help.
            response = self._timed("discord ack (3s deadline)", lambda: requests.post(
                "{}/interactions/{}/{}/callback".format(
                    API, interaction["id"], interaction["token"]),
                json={"type": CB_DEFERRED_UPDATE}, timeout=(1.5, 1.5)))
            if response.status_code not in (200, 204):
                self._logger.warning(
                    "Discord rejected the acknowledgement: HTTP %s %s",
                    response.status_code, response.text[:120])
                return False
            return True
        except requests.exceptions.RequestException as exc:
            self._logger.warning("Could not acknowledge interaction: %s",
                                 redact(exc))
            return False

    def followup(self, interaction, content, components=None, image=None,
                 silent=True):
        """Reply after a deferred ACK, using the interaction token.

        Silent by DEFAULT, unlike send(): a followup only exists because the
        user just pressed a button, so they are already looking at Discord
        and a push notification for their own action is pure noise.
        """
        # See send(): a followup carries the same interpolated text.
        payload = {"content": content,
                   "allowed_mentions": {"parse": []}}
        if components is not None:
            payload["components"] = components
        if silent:
            payload["flags"] = FLAG_SUPPRESS_NOTIFICATIONS
        url = "{}/webhooks/{}/{}".format(API, interaction["application_id"],
                                     interaction["token"])
        try:
            if image is not None:
                requests.post(url, data={"payload_json": json.dumps(payload)},
                              files={"files[0]": ("nozcam.jpg", image,
                                                  "image/jpeg")},
                              timeout=(5, 20))
            else:
                requests.post(url, json=payload, timeout=(5, 15))
        except requests.exceptions.RequestException as exc:
            self._logger.warning("Could not send follow-up: %s", redact(exc))

    # ---- Gateway ------------------------------------------------------

    def start(self):
        """Begin connecting, in a background thread. Safe to call twice."""
        if self.running:
            return
        self.running = True
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="pinozcam-discord")
        self.thread.daemon = True
        self.thread.start()
        self._cmd_worker = threading.Thread(target=self._command_worker,
                                            name="pinozcam-discord-cmd")
        self._cmd_worker.daemon = True
        self._cmd_worker.start()

    def stop(self, timeout=8.0):
        """Close the connection and wait up to `timeout` for the thread.

        `timeout` is a parameter, not a constant, because the shutdown path
        has one overall budget to divide between several components; a fixed
        8 s here made the total unbounded in aggregate.
        """
        self.running = False
        self.stop_event.set()
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        thread, self.thread = self.thread, None
        worker, self._cmd_worker = self._cmd_worker, None
        if timeout > 0:
            deadline = time.monotonic() + timeout
            if thread is not None and thread.is_alive():
                thread.join(timeout=timeout)
            if worker is not None and worker.is_alive():
                worker.join(timeout=max(0.0, deadline - time.monotonic()))
        self.connected = False

    def _run(self):
        # websocket-client ships with OctoPrint, but importing at module
        # scope would make this file unimportable if that ever changed.
        """Connect, and keep reconnecting, until stopped.

        Backoff is exponential with full jitter so a shared outage does
        not have every printer retrying on the same tick. A close code
        that reconnecting cannot fix ends the loop instead."""
        try:
            import websocket
        except ImportError:
            self.last_error = ("websocket-client is not installed; buttons "
                               "need it. Notifications still work.")
            self._logger.error(self.last_error)
            self.running = False
            return

        backoff = 1.0
        while self.running and not self.stop_event.is_set():
            try:
                started = time.monotonic()
                fatal = self._session(websocket)
                if fatal:
                    self._logger.error(
                        "Discord closed the connection permanently: %s",
                        self.last_error)
                    # after a fatal exit left the object looking live while
                    # its thread was gone, and start() checks `running` --
                    # so saving the settings again could not revive the same
                    # instance. setup_discord_bot builds a fresh one, but a
                    # half-dead object is a trap for anything that does not.
                    self.running = False
                    break
                # Reset the backoff only if the session actually lasted.
                # Resetting on every return made the backoff meaningless:
                # a connection that fails immediately, over and over, would
                # retry every second forever.
                if time.monotonic() - started > 60:
                    backoff = 1.0
            except Exception as exc:
                self.last_error = redact(str(exc))
                self._logger.warning("Discord gateway error: %s", redact(exc))
            finally:
                self.connected = False
            if not self.running:
                break
            # Full jitter: a shared outage must not have every printer
            # reconnecting on the same tick.
            wait = backoff * (0.5 + random.random() * 0.5)
            self.stop_event.wait(wait)
            backoff = min(60.0, backoff * 2)
        self._logger.info("Discord gateway thread ended.")

    def _session(self, websocket):
        """One connection. Returns True if reconnecting cannot help."""
        # READY timing measured time since the bot object was first started
        # -- after an overnight outage it read "READY 21064051 ms after
        # start()", i.e. 5.85 hours, and was meaningless in exactly the case
        # the number exists for: how long did THIS reconnect take.
        self._started_at = time.monotonic()
        url = self._resume_url or "wss://gateway.discord.gg"
        ws = websocket.create_connection(
            "{}/?v=10&encoding=json".format(url.rstrip("/")), timeout=30)
        # A short recv timeout is what lets a single loop also send
        # heartbeats, avoiding a second thread writing to the same socket.
        ws.settimeout(1.0)
        self._ws = ws
        interval = None
        next_beat = None
        awaiting_ack = False
        self._reached_ready = False
        self._identified_privileged = False

        try:
            while self.running and not self.stop_event.is_set():
                now = time.monotonic()
                # `is not None`, not truthiness: a zero interval would be
                # nonsense from Discord, but silently skipping every
                # heartbeat is a worse way to handle it than beating madly
                # and being disconnected for it.
                if interval is not None and now >= next_beat:
                    if awaiting_ack:
                        # Discord never acknowledged the last beat: the
                        # connection is a zombie. Drop it and resume.
                        self._logger.info(
                            "Discord heartbeat unacknowledged; reconnecting.")
                        return False
                    ws.send(json.dumps({"op": OP_HEARTBEAT,
                                        "d": self._sequence}))
                    awaiting_ack = True
                    next_beat = now + interval
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                except websocket.WebSocketConnectionClosedException:
                    return self._closed(ws, "connection closed")
                # CLOSE frame -- it does NOT raise. Treating it as "nothing
                # to read" and continuing was the bug: the loop spun until
                # the next heartbeat was due, returned False, and reconnected
                # about 78 times a minute against Discord's 1000 IDENTIFY per
                # day. A bot whose Message Content Intent is not enabled gets
                # closed with 4014 every single time, so this was not a rare
                # path -- and _handle_fatal_close, which exists precisely to
                # fall back to buttons-only, was unreachable.
                if not raw:
                    return self._closed(ws, "empty frame (close)")
                message = json.loads(raw)
                op = message.get("op")
                if message.get("s") is not None:
                    self._sequence = message["s"]

                if op == OP_HELLO:
                    interval = message["d"]["heartbeat_interval"] / 1000.0
                    # First beat is jittered, as the docs require.
                    next_beat = time.monotonic() + interval * random.random()
                    awaiting_ack = False
                    if self._session_id:
                        ws.send(json.dumps({"op": OP_RESUME, "d": {
                            "token": self.token,
                            "session_id": self._session_id,
                            "seq": self._sequence}}))
                    else:
                        ws.send(json.dumps({"op": OP_IDENTIFY, "d": {
                            "token": self.token,
                            "intents": self._intents,
                            "properties": {"os": "linux",
                                           "browser": "pinozcam",
                                           "device": "pinozcam"}}}))
                        # Record that we asked for a privileged intent on a
                        # connection that had already got this far. _closed
                        # uses exactly this to tell a refused intent from a
                        # network problem.
                        if self._intents != INTENTS_BUTTONS_ONLY:
                            self._identified_privileged = True
                elif op == OP_HEARTBEAT:
                    ws.send(json.dumps({"op": OP_HEARTBEAT,
                                        "d": self._sequence}))
                    next_beat = time.monotonic() + interval
                elif op == OP_HEARTBEAT_ACK:
                    awaiting_ack = False
                elif op == OP_RECONNECT:
                    return False
                elif op == OP_INVALID_SESSION:
                    # The payload says whether the session may be resumed.
                    # Discarding it unconditionally forced a full IDENTIFY
                    # every time, and IDENTIFY is rate limited.
                    if not message.get("d"):
                        self._session_id = None
                        self._resume_url = None
                    return False
                elif op == OP_DISPATCH:
                    self._dispatch(message)
        except websocket.WebSocketException as exc:
            code = getattr(ws, "status", None)
            if code in FATAL_CLOSE_CODES:
                return self._handle_fatal_close(code, exc)
            raise
        finally:
            try:
                ws.close()
            except Exception:
                pass
            self._ws = None
        return False

    def _closed(self, ws, reason):
        """Handle closed websocket sessions and decide reconnect policy."""
        self.connected = False
        code = getattr(ws, "close_code", None)
        if code in FATAL_CLOSE_CODES:
            return self._handle_fatal_close(code, reason)

        if self._reached_ready:
            # A healthy session that ended: ordinary reconnect.
            self._logger.info("Discord gateway closed (%s); reconnecting.",
                              reason)
            self._identify_rejections = 0
            return False

        if not self._identified_privileged:
            # We never even got as far as asking for the privileged intent,
            # so this says nothing about intents. Reconnect with backoff.
            self._logger.info(
                "Discord connection dropped before the handshake completed "
                "(%s); reconnecting.", reason)
            return False

        # HELLO received, privileged IDENTIFY sent, closed before READY.
        self._identify_rejections += 1
        if self._identify_rejections == 1:
            self._logger.warning(
                "Discord accepted the connection but closed it after an "
                "IDENTIFY asking for the MESSAGE_CONTENT intent (%s). "
                "Turning typed commands off and retrying with no privileged "
                "intent; buttons still work. Enable Message Content Intent "
                "in the Discord developer portal if you want !commands too.",
                reason)
            self._intents = INTENTS_BUTTONS_ONLY
            self._session_id = None
            self._resume_url = None
            return False

        self.last_error = (
            f"Discord rejected the connection after IDENTIFY "
            f"{self._identify_rejections} times ({reason}). Check that the "
            "bot token is correct and the bot is invited to the channel.")
        self._logger.error("%s Giving up; fix the setting and save again "
                           "to retry.", self.last_error)
        return True

    def _handle_fatal_close(self, code, exc):
        """Decide whether a close code is worth retrying.

        A refused privileged intent is not fatal: buttons need no intent,
        so it drops to buttons-only and reconnects. A bad token is fatal."""
        if code == CLOSE_DISALLOWED_INTENTS and self._intents != INTENTS_BUTTONS_ONLY:
            # Text commands need the privileged MESSAGE_CONTENT intent, and
            # the user has not enabled it. Buttons need no intent, so drop
            # back rather than giving up the whole integration.
            self._logger.warning(
                "Discord refused the MESSAGE_CONTENT intent, so typed "
                "commands are off. Buttons still work. Enable Message "
                "Content Intent in the Discord developer portal if you want "
                "!commands as well.")
            self._intents = INTENTS_BUTTONS_ONLY
            self._session_id = None
            return False
        self.last_error = f"gateway close code {code}: {redact(exc)}"
        return True

    def _dispatch(self, message):
        """Route one gateway event.

        READY and RESUMED update the session; INTERACTION_CREATE is a
        button press; MESSAGE_CREATE is a typed command."""
        event = message.get("t")
        data = message.get("d") or {}
        if event == "READY":
            self._session_id = data.get("session_id")
            self._resume_url = data.get("resume_gateway_url")
            self.connected = True
            self._reached_ready = True
            self._identify_rejections = 0
            self.text_commands = self._intents != INTENTS_BUTTONS_ONLY
            self.last_error = None
            if self._started_at is not None:
                self._logger.info(
                    "[timing] discord gateway READY %.0f ms into this "
                    "connection attempt",
                    (time.monotonic() - self._started_at) * 1000.0)
            self._logger.info(
                "Discord gateway connected as %s (typed commands %s).",
                (data.get("user") or {}).get("username", "?"),
                "on" if self.text_commands else "off -- buttons only")
        elif event == "RESUMED":
            self.connected = True
            self._reached_ready = True
            self._identify_rejections = 0
        elif event == "INTERACTION_CREATE":
            self._on_interaction(data)
        elif event == "MESSAGE_CREATE":
            self._on_message(data)

    def _on_interaction(self, interaction):
        # Component interactions only; slash commands are not registered.
        """Acknowledge a button press, then run it off this thread.

        The acknowledgement must land within 3 seconds, and this thread
        also sends heartbeats -- so the work cannot run here."""
        if interaction.get("type") != 3:
            return
        custom_id = (interaction.get("data") or {}).get("custom_id")
        if not custom_id:
            return
        #
        # Discord delivers INTERACTION_CREATE to EVERY gateway session on the
        # token, and the channel id is identical for every printer sharing a
        # channel. So without this, one press of Check answered from all of
        # them and one press of Stop opened a confirmation on all of them.
        # (Nothing was cancelled twice -- the nonce is per instance -- but
        # which instance owned the live offer was unpredictable.)
        #
        # Not acknowledged, on purpose: the instance that DOES own the button
        # is the one that must answer within the 3 second deadline, and an
        # ACK from a bystander would consume the interaction.
        who, custom_id = untag(custom_id)
        if who is not None and who != self.printer_id:
            self._logger.debug(
                "Ignoring button %r for printer %s; this is %s.",
                custom_id, who, self.printer_id)
            return
        # instance carried an id. It cannot be attributed to anyone, so it is
        # still honoured: refusing would break every button in every message
        # a single-printer user already has, which is the overwhelmingly
        # common case and not the one the id exists for. In a shared channel
        # those old messages keep the old behaviour; new ones are correct.
        if who is None:
            self._logger.info(
                "Button %r carries no printer id (message predates this "
                "version); honouring it. Press a button on a NEWER alert if "
                "several printers share this channel.", custom_id)
        # Typed commands always had this check; buttons did not -- so a
        # PiNozCam message left behind in a previously-configured channel
        # kept live Pause/Stop buttons for whoever could see that channel.
        # A missing channel_id is refused too (str(None) never matches).
        # Not acknowledging is deliberate: the press visibly fails in the
        # old channel instead of silently doing nothing.
        if str(interaction.get("channel_id")) != self.channel_id:
            self._logger.warning(
                "Ignoring button %r from channel %s; the configured "
                "channel is %s.", custom_id,
                interaction.get("channel_id"), self.channel_id)
            return
        # Acknowledge FIRST, on this thread, because that is what the 3
        # second deadline applies to.
        self.ack(interaction)
        # Then hand the work off. Running it here would block the receive
        # loop, and that loop is also what sends heartbeats -- grabbing a
        # camera frame takes longer than a heartbeat interval on a slow
        # board, so doing it inline would drop the connection.
        if not self._enqueue_command(custom_id, interaction):
            # reads as "it worked" -- and for a Stop that is the worst
            # possible thing to get wrong. Sent through the plain REST
            # send, not a followup, because it must not depend on the
            # queue that just refused the work.
            self._busy_notice(custom_id)

    def _on_message(self, message):
        """Typed commands, for users who enabled the message content intent.

        Bot messages are skipped, which also skips our own posts -- otherwise
        an alert caption containing the word "stop" would act on the printer.
        """
        if message.get("author", {}).get("bot"):
            return
        if str(message.get("channel_id")) != self.channel_id:
            return
        text = (message.get("content") or "").strip().lower()
        if not text.startswith("!"):
            return
        # JPEG and posts it -- seconds to ~20 s on a slow board -- and this
        # thread is also the one sending heartbeats. Buttons already ran
        # elsewhere; typed commands ran inline and could drop the gateway.
        name = text.split()[0][1:]
        if not self._enqueue_command(name, None):
            self._busy_notice(name)

    def _busy_notice(self, name):
        """Post a short notice that a command was dropped because busy."""
        try:
            self.send(content="PiNozCam is busy running an earlier "
                              f"command; `{name}` was not queued. Try again in "
                              "a moment.")
        except Exception as exc:                        # noqa: BLE001
            self._logger.error("Could not post the busy notice: %s",
                               redact(str(exc)))

    def _is_control(self, name):
        """Does this command touch the PRINTER?

        Everything that pauses, resumes, cancels or answers a Yes/No about
        doing so. These must not queue behind a Check.
        """
        base = (name or "").split(":")[0]
        return base in ("pause", "resume", "stop", "confirm", "cancel")

    def _enqueue_command(self, name, interaction):
        """Queue a command with control commands drained first."""
        control = self._is_control(name)
        work = self._controls if control else self._commands
        try:
            work.put_nowait((name, interaction))
            return True
        except queue.Full:
            self._logger.warning(
                "Discord %s queue is full; dropping %r.",
                "control" if control else "command", name)
            return False

    def _command_worker(self):
        """Run button and typed commands in priority order."""
        while not self.stop_event.is_set():
            work = None
            try:
                # Controls first, always. Only when none is waiting does a
                # Check get a turn -- and then with a short timeout, so a
                # control arriving during the wait is picked up promptly.
                try:
                    item = self._controls.get_nowait()
                    work = self._controls
                except queue.Empty:
                    item = self._commands.get(timeout=0.25)
                    work = self._commands
            except queue.Empty:
                continue
            name, interaction = item
            try:
                self.on_command(name, interaction)
            except Exception as exc:
                self._logger.error("Discord command %s failed: %s",
                                   name, exc, exc_info=True)
            finally:
                work.task_done()
