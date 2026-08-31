"""Credential helpers shared by transport modules.

This file contains only local validation and safe redaction helpers used by
notifications and UI validation paths.
"""

import re

# Match known credential-like text fragments so logs stay safe.
SECRET_RE = re.compile(
    r"(?:/webhooks/\d+/[\w-]+)"
    r"|(?:/interactions/\d+/[\w.-]+)"
    r"|(?:/bot\d+:[\w-]+)"
    r"|(?:\b\d{5,20}:[\w-]{30,}\b)"
    r"|(?:\b[\w-]{24,28}\.[\w-]{6}\.[\w-]{27,}\b)"
    r"|(?://[^/\s:@]+:[^/\s@]+@)")

TELEGRAM_TOKEN_RE = re.compile(r"^\d{6,}:[\w-]{30,}$")
TELEGRAM_CHAT_RE = re.compile(r"^-?[0-9]{5,}$")

DISCORD_CHANNEL_RE = re.compile(r"^[0-9]{17,20}$")
DISCORD_TOKEN_RE = re.compile(r"^[\w-]{24,28}\.[\w-]{6}\.[\w-]{27,}$")


def telegram_chat_id_error(value):
    """Validate Telegram chat-id shape.

    Empty values are valid and treated as "not configured".
    """
    value = "" if value is None else str(value).strip()
    if value and not TELEGRAM_CHAT_RE.match(value):
        return ("Telegram Chat ID must be at least 5 digits, optionally "
                "starting with a minus sign for a group.")
    return None


def discord_channel_id_error(value):
    """Validate Discord channel-id shape.

    Empty values are valid and treated as "not configured".
    """
    value = "" if value is None else str(value).strip()
    if value and not DISCORD_CHANNEL_RE.match(value):
        return "Discord Channel ID must be 17-20 digits."
    return None


def redact(text):
    """Redact credential-like fragments before writing to logs."""
    if not text:
        return text
    return SECRET_RE.sub("<redacted>", str(text))


def describe_pieces(value):
    """Return a shape summary for user-facing errors without leaking secrets."""
    pieces = value.split()
    if not pieces:
        return "nothing but whitespace"
    if len(pieces) == 1:
        return "one piece of %d characters" % len(pieces[0])

    def kind(part):
        """Name one piece's character class, without quoting it."""
        if part.isdigit():
            return "digits"
        if part.isalpha():
            return "letters"
        return "mixed"

    lengths = ", ".join(str(len(part)) for part in pieces[:-1])
    return ("%d pieces of %s and %d characters (%s)"
            % (len(pieces), lengths, len(pieces[-1]),
               ", ".join(kind(part) for part in pieces)))


def describe_telegram_problem(token, chat_id):
    """Describe why Telegram credentials cannot be used yet, or return None."""
    if not token:
        return ("the Telegram Bot Token field is empty. Get one from "
                "@BotFather with /mybots -> your bot -> API Token.")
    if not chat_id:
        return ("the Telegram Chat ID field is empty. Send your bot any "
                "message first, then read the id from "
                "api.telegram.org/bot<TOKEN>/getUpdates.")
    if ":" not in token and any(ch.isspace() for ch in token):
        repaired = re.sub(r"\s+", ":", token, count=1)
        if TELEGRAM_TOKEN_RE.match(repaired):
            return ("the Bot Token has a space where its colon should be. "
                    "Everything else is there -- replace that space with a "
                    "colon, so it reads <digits>:<the rest>.")
        return ("the Bot Token has no colon and contains whitespace. "
                "What arrived is %s. A token is one piece: 6 or more "
                "digits, a colon, then about 35 letters, digits, "
                "underscores or dashes." % describe_pieces(token))
    if any(ch.isspace() for ch in token):
        return ("the Bot Token contains a space or a line break: it "
                "arrived as %s. A token has none -- re-copy it from "
                "@BotFather in one piece." % describe_pieces(token))
    if TELEGRAM_CHAT_RE.match(token):
        return ("the Bot Token field holds what looks like a chat id "
                "(%d digits, no colon). The two fields may be swapped: "
                "a token looks like 123456789:AA... " % len(token))
    if ":" not in token:
        return ("the Bot Token has no colon. A token is the bot's numeric "
                "id, then a colon, then about 35 more characters -- this "
                "value is %d characters with no colon, so it looks like "
                "only part of one was pasted." % len(token))
    if not TELEGRAM_TOKEN_RE.match(token):
        head, _, tail = token.partition(":")
        return ("the Bot Token is not the right shape: %d digits before "
                "the colon and %d characters after it. BotFather issues 6 "
                "or more digits and about 35 characters."
                % (len(head), len(tail)))
    chat_error = telegram_chat_id_error(chat_id)
    if chat_error:
        return chat_error
    return None


def describe_discord_problem(token, channel_id):
    """Describe why Discord credentials cannot be used yet, or return None."""
    if not token and not channel_id:
        return ("both Discord fields are empty. Create an application at "
                "discord.com/developers, add a Bot, copy its token, and "
                "invite it to your server; then right-click the channel "
                "and Copy Channel ID.")
    if not token:
        return ("the Discord Bot Token field is empty. The Channel ID "
                "alone cannot send anything -- both are needed.")
    if not channel_id:
        return ("the Discord Channel ID field is empty. Turn on Developer "
                "Mode in Discord (User Settings -> Advanced), then "
                "right-click the channel and Copy Channel ID.")
    if (DISCORD_CHANNEL_RE.match(token)
            and DISCORD_TOKEN_RE.match(channel_id)):
        return ("the two Discord fields look swapped -- the Bot Token "
                "field holds a channel id and the Channel ID field holds "
                "a token.")
    channel_error = discord_channel_id_error(channel_id)
    if channel_error:
        if channel_id.startswith("http"):
            return ("the Channel ID is a URL, not an id. A channel link "
                    "ends with the id; Copy Channel ID gives just the "
                    "digits.")
        return ("%s Right-click the channel -> Copy Channel ID, with "
                "Developer Mode on." % channel_error)
    if not DISCORD_TOKEN_RE.match(token):
        if any(ch.isspace() for ch in token):
            return ("the Bot Token contains whitespace. What arrived is "
                    "%s. A token is one piece of three dot-separated "
                    "parts." % describe_pieces(token))
        return ("the Bot Token is not the expected shape. A token is three "
                "parts separated by dots; what arrived is %d characters in "
                "%d parts. Copy the token "
                "from the Bot page, not the Client Secret or the "
                "Application ID from the General Information page."
                % (len(token), len(token.split("."))))
    return None
