"""Single-use confirmation tokens, shared by Telegram and Discord.

A destructive action -- pause, resume, stop -- is only carried out for a
nonce this process issued, in the current print, within the last
CONFIRM_TTL seconds, and that nobody has used yet. Both chat backends go
through this one mechanism so neither can evict the other's offer.
"""

import secrets
import time


class ConfirmMixin:
    """Mixed into PinozcamPlugin; see the module docstring."""

    CONFIRM_TTL = 60

    def _new_confirm_scope(self):
        """Invalidate every outstanding confirmation. Called per print.

        Bumping the scope is belt and braces next to clearing the dict:
        a reply that was already in flight when the print ended carries
        the old scope and is refused on arrival.
        """
        with self.confirm_lock:
            self.confirm_scope += 1
            self.confirm_tokens.clear()

    def _issue_confirm(self, action, channel):
        """Mint a single-use nonce for `action` on `channel`, and return it.

        action, a Stop offered on Discord overwrote a Stop offered on
        Telegram, so the Telegram Yes the user was looking at became stale
        the moment anyone touched the other channel -- and _confirm_pending
        would additionally silence Telegram alerts for a minute because of a
        pending Discord question. The two channels are independent
        conversations and must not evict each other.

        Within one channel, a second offer for the same action does replace
        the first: pressing Stop twice should leave one live token, not two.
        """
        nonce = secrets.token_urlsafe(8)
        with self.confirm_lock:
            self.confirm_tokens[(channel, action)] = (
                nonce, time.monotonic(), self.confirm_scope)
        return nonce

    def _consume_confirm(self, action, nonce, channel):
        """Atomically claim a confirmation. Returns "ok"/"stale"/"expired".

        The check and the removal are one critical section on purpose.
        Splitting them -- which is what the code before this did -- let
        two taps that arrive together both see a valid token and both
        cancel the print.
        """
        key = (channel, action)
        with self.confirm_lock:
            held = self.confirm_tokens.get(key)
            if (not held or not nonce or held[0] != nonce
                    or held[2] != self.confirm_scope):
                return "stale"
            if time.monotonic() - held[1] > self.CONFIRM_TTL:
                del self.confirm_tokens[key]
                return "expired"
            del self.confirm_tokens[key]
            return "ok"

    def _confirm_pending(self, channel):
        """True while a confirmation on `channel` is still answerable.

        Scoped to one channel so a pending Discord question does not
        suppress Telegram alerts. Expired entries are dropped as they are
        seen, so a question the user ignored stops suppressing alerts once
        its TTL passes rather than silencing them until the print ends.
        """
        now = time.monotonic()
        with self.confirm_lock:
            for key, held in list(self.confirm_tokens.items()):
                if now - held[1] > self.CONFIRM_TTL:
                    del self.confirm_tokens[key]
            return any(k[0] == channel for k in self.confirm_tokens)

    def _drop_confirm(self, action, nonce=None, channel=None):
        """Withdraw an offer, so "No" actually revokes the "Yes".

        Without this, declining only sent a polite reply and left the
        Yes button live for the rest of its TTL -- the user thought they
        had cancelled and the print could still be stopped by a stray
        tap. Passing a nonce makes it idempotent and stops one message's
        No from revoking a newer offer.
        """
        with self.confirm_lock:
            held = self.confirm_tokens.get((channel, action))
            if held and (nonce is None or held[0] == nonce):
                del self.confirm_tokens[(channel, action)]
                return True
        return False
