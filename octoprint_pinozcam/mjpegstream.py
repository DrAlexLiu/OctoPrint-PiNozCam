"""Incrementally parse multipart/x-mixed-replace streams into raw parts.

This module performs no networking or image decoding. Frames end only at a
validated Content-Length or a legal MIME boundary; JPEG marker bytes are not
framing. Buffer growth is bounded and consumed in place.
"""

import re
from collections import namedtuple
from email.message import Message


# Headers are lower-cased and stripped; body bytes are returned unchanged.
Part = namedtuple("Part", "headers body")


class MultipartError(Exception):
    """The stream violated multipart framing or a configured size bound."""


# Bounded input prevents a malformed or hostile stream from growing memory.
MAX_HEADER_LINE_BYTES = 8 * 1024
MAX_HEADERS_TOTAL_BYTES = 64 * 1024
MAX_GAP_BYTES = 64 * 1024
MAX_PART_BYTES = 16 * 1024 * 1024


def _normalize_delimiter(boundary_bytes):
    """Return an on-wire delimiter, tolerating a declared leading ``--``."""
    if boundary_bytes.startswith(b"--"):
        return boundary_bytes
    return b"--" + boundary_bytes


def parse_boundary(content_type):
    """Return the normalized delimiter declared by an MJPEG Content-Type."""
    message = Message()
    message["content-type"] = content_type or ""
    media_type = message.get_content_type()
    if media_type != "multipart/x-mixed-replace":
        raise MultipartError(
            "expected multipart/x-mixed-replace, got %r" % media_type)
    boundary = message.get_param("boundary")
    if isinstance(boundary, tuple):
        # RFC 2231 extended parameters are (charset, language, value).
        boundary = boundary[2] if len(boundary) > 2 else None
    if not boundary:
        raise MultipartError(
            "multipart/x-mixed-replace with no boundary parameter")
    return _normalize_delimiter(boundary.encode("latin-1"))


# Private sentinels make parser state distinct from any valid return value.
_NEED_MORE = object()
_PROGRESS = object()
_INVALID_LENGTH = object()


class MultipartParser(object):
    """Incremental parser scoped to one connection."""

    _SEEK_BOUNDARY, _HEADERS, _BODY_KNOWN, _BODY_SCAN, _DONE = range(5)

    def __init__(self, boundary, max_header_line=MAX_HEADER_LINE_BYTES,
                 max_headers_total=MAX_HEADERS_TOTAL_BYTES,
                 max_gap_bytes=MAX_GAP_BYTES,
                 max_part_bytes=MAX_PART_BYTES):
        """Create a parser for a raw or already-normalized boundary token."""
        if isinstance(boundary, str):
            boundary = boundary.encode("latin-1")
        if not boundary:
            raise MultipartError("empty boundary")
        self._delim = _normalize_delimiter(boundary)
        self._max_header_line = max_header_line
        self._max_headers_total = max_headers_total
        self._max_gap_bytes = max_gap_bytes
        self._max_part_bytes = max_part_bytes
        self._buf = bytearray()
        self._state = self._SEEK_BOUNDARY
        self._pending_headers = None
        self._body_remaining = None

    def feed(self, data):
        """Add bytes and return every Part completed by this chunk."""
        if self._state == self._DONE:
            return []
        self._buf.extend(data)
        parts = []
        while True:
            result = self._advance()
            if result is _NEED_MORE:
                return parts
            if result is _PROGRESS:
                continue
            parts.append(result)

    def finished(self):
        """Return whether the closing boundary has been consumed."""
        return self._state == self._DONE

    # ---- state machine -------------------------------------------------

    def _advance(self):
        """Advance one state, returning a Part or a private sentinel."""
        if self._state == self._SEEK_BOUNDARY:
            return self._advance_seek_boundary()
        if self._state == self._HEADERS:
            return self._advance_headers()
        if self._state == self._BODY_KNOWN:
            return self._advance_body_known()
        if self._state == self._BODY_SCAN:
            return self._advance_body_scan()
        return _NEED_MORE           # _DONE: nothing more ever comes out

    def _advance_seek_boundary(self):
        """Discard a bounded preamble or gap and consume a boundary line."""
        idx = self._buf.find(self._delim)
        if idx == -1:
            if len(self._buf) > self._max_gap_bytes:
                raise MultipartError(
                    "no boundary found in the first %d bytes"
                    % self._max_gap_bytes)
            return _NEED_MORE
        after = idx + len(self._delim)
        if len(self._buf) < after + 2:
            return _NEED_MORE       # not enough yet to tell "--" vs a line
        if self._buf[after:after + 2] == b"--":
            self._buf.clear()
            self._state = self._DONE
            return _NEED_MORE
        line_end = self._find_line_end(after)
        if line_end is None:
            if len(self._buf) - after > self._max_header_line:
                raise MultipartError("boundary line too long")
            return _NEED_MORE
        del self._buf[:line_end]
        self._state = self._HEADERS
        return _PROGRESS

    def _advance_headers(self):
        """Consume one part's header block, decide how its body ends."""
        sep_idx, sep_len = self._find_header_terminator()
        if sep_idx is None:
            if len(self._buf) > self._max_headers_total:
                raise MultipartError(
                    "part headers exceed %d bytes"
                    % self._max_headers_total)
            return _NEED_MORE
        block = bytes(self._buf[:sep_idx])
        del self._buf[:sep_idx + sep_len]
        headers = self._parse_header_block(block)
        content_length = self._content_length(headers)
        if content_length is _INVALID_LENGTH:
            raise MultipartError(
                "illegal, negative or over-limit Content-Length")
        self._pending_headers = headers
        if content_length is None:
            self._state = self._BODY_SCAN
        else:
            self._state = self._BODY_KNOWN
            self._body_remaining = content_length
        return _PROGRESS

    def _advance_body_known(self):
        """Consume a validated Content-Length without scanning body bytes."""
        needed = self._body_remaining
        if len(self._buf) < needed:
            return _NEED_MORE
        body = bytes(self._buf[:needed])
        del self._buf[:needed]
        headers = self._pending_headers
        self._pending_headers = None
        self._body_remaining = None
        self._state = self._SEEK_BOUNDARY
        return Part(headers, body)

    def _advance_body_scan(self):
        """Find a legally line-anchored MIME boundary within the size cap.

        Without Content-Length, completion necessarily waits for the next
        boundary; an unframed final body cannot safely be emitted.
        """
        buf = self._buf
        search_from = 0
        while True:
            idx = buf.find(self._delim, search_from)
            if idx == -1:
                break
            preceded_ok = (
                idx == 0
                or (idx >= 2 and buf[idx - 2:idx] == b"\r\n")
                or (idx >= 1 and buf[idx - 1:idx] == b"\n")
            )
            if not preceded_ok:
                search_from = idx + 1
                continue
            after = idx + len(self._delim)
            if len(buf) < after + 2:
                return _NEED_MORE    # cannot tell what follows it yet
            followed_ok = (
                buf[after:after + 2] == b"--"
                or buf[after:after + 2] == b"\r\n"
                or buf[after:after + 1] == b"\n"
            )
            if not followed_ok:
                search_from = idx + 1
                continue
            body_end = idx
            if body_end >= 2 and buf[body_end - 2:body_end] == b"\r\n":
                body_end -= 2
            elif body_end >= 1 and buf[body_end - 1:body_end] == b"\n":
                body_end -= 1
            body = bytes(buf[:body_end])
            del buf[:idx]
            headers = self._pending_headers
            self._pending_headers = None
            self._state = self._SEEK_BOUNDARY
            return Part(headers, body)
        if len(buf) > self._max_part_bytes:
            raise MultipartError(
                "part exceeds %d bytes with no boundary found"
                % self._max_part_bytes)
        return _NEED_MORE

    # ---- small helpers ---------------------------------------------------

    def _find_line_end(self, pos):
        """Return the index after the next LF, or None."""
        nl = self._buf.find(b"\n", pos)
        if nl == -1:
            return None
        return nl + 1

    def _find_header_terminator(self):
        """Return the earliest CRLF or LF-only header terminator."""
        buf = self._buf
        crlf = buf.find(b"\r\n\r\n")
        lf = buf.find(b"\n\n")
        candidates = []
        if crlf != -1:
            candidates.append((crlf, 4))
        if lf != -1:
            candidates.append((lf, 2))
        if not candidates:
            return None, None
        return min(candidates, key=lambda pair: pair[0])

    def _parse_header_block(self, block):
        """Parse bounded header bytes using a byte-preserving encoding."""
        headers = {}
        for raw_line in re.split(br"\r\n|\n", block):
            if not raw_line:
                continue
            if len(raw_line) > self._max_header_line:
                raise MultipartError(
                    "header line exceeds %d bytes" % self._max_header_line)
            line = raw_line.decode("latin-1")
            if ":" not in line:
                continue
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        return headers

    def _content_length(self, headers):
        """Return a valid length, None if absent, or _INVALID_LENGTH."""
        raw = headers.get("content-length")
        if raw is None:
            return None
        try:
            value = int(raw.strip())
        except ValueError:
            return _INVALID_LENGTH
        if value < 0 or value > self._max_part_bytes:
            return _INVALID_LENGTH
        return value
