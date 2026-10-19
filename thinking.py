"""Optional agy 1.4 conversation DB sidechannel (internal, version-sensitive schema).

Read-only, short connections. Capture the step boundary BEFORE submitting a
request; never infer a turn boundary from the first poll after submission.
"""
from __future__ import annotations

import contextlib
import logging
import re
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)
MAX_PAYLOAD = 2 * 1024 * 1024
MAX_FIELDS = 8192
MAX_STEPS = 256


def _varint(data, pos):
    value = 0
    for shift in range(0, 70, 7):
        if pos >= len(data):
            raise ValueError("truncated varint")
        byte = data[pos]
        pos += 1
        if shift == 63 and byte > 1:
            raise ValueError("overflow varint")
        value |= (byte & 127) << shift
        if byte < 128:
            return value, pos
    raise ValueError("overflow varint")


def _fields(data):
    pos = 0
    count = 0
    while pos < len(data):
        count += 1
        if count > MAX_FIELDS:
            raise ValueError("field limit")
        tag, pos = _varint(data, pos)
        field, wire = tag >> 3, tag & 7
        if not 0 < field < (1 << 29):
            raise ValueError("invalid field")
        if wire == 0:
            value, pos = _varint(data, pos)
        elif wire in (1, 2, 5):
            if wire == 2:
                size, pos = _varint(data, pos)
            else:
                size = 8 if wire == 1 else 4
            end = pos + size
            if end > len(data):
                raise ValueError("truncated field")
            value = data[pos:end]
            pos = end
        else:
            raise ValueError("unsupported wire")
        yield field, wire, value


def decode_thinking(payload: bytes) -> str:
    """Decode only planner 20 -> RawThinking 16 (preferred) / Thinking 3."""
    if not isinstance(payload, bytes) or len(payload) > MAX_PAYLOAD:
        raise ValueError("payload limit/type")
    text = raw = ""
    for field, wire, value in _fields(payload):
        if field != 20:
            continue
        if wire != 2:
            raise ValueError("planner wire")
        for inner, inner_wire, inner_value in _fields(value):
            if inner not in (3, 16):
                continue
            if inner_wire != 2:
                raise ValueError("thinking wire")
            decoded = inner_value.decode("utf-8", errors="strict")
            if inner == 16:
                raw = decoded
            else:
                text = decoded
    return raw or text


def _connect(path):
    return contextlib.closing(sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=0.05))


class ThinkingReader:
    """One request's snapshot and append-only reasoning deltas; no held handles.

    Replacement/shrink cannot retract already-rendered OpenAI deltas. Suppress
    that step for the rest of the request instead of replaying a replacement or
    duplicating history. Other steps can continue. No text is logged.
    """
    def __init__(self, gemini_dir):
        self.directory = Path(gemini_dir) / "conversations" if gemini_dir else None
        self.baselines = {}
        self.seen = {}
        self.suppressed = set()
        self.diagnostics = set()
        self.closed = False

    def _diagnose(self, kind):
        if kind not in self.diagnostics and len(self.diagnostics) < 4:
            self.diagnostics.add(kind)
            logger.debug("Antigravity optional thinking unavailable/update suppressed (%s)", kind)

    @classmethod
    def before_request(cls, gemini_dir):
        reader = cls(gemini_dir)
        if reader.directory is None:
            reader.closed = True
            return reader
        try:
            paths = list(reader.directory.glob("*.db"))
            if len(paths) > 128:
                raise ValueError("conversation limit")
            for path in paths:
                with _connect(path) as connection:
                    row = connection.execute("SELECT MAX(idx) FROM steps").fetchone()
                reader.baselines[path.stem] = row[0] if row[0] is not None else -1
        except (OSError, sqlite3.Error, ValueError):
            # A failed baseline must NOT later turn an existing DB into a new
            # conversation and expose previous-turn reasoning.
            reader.closed = True
            reader._diagnose("baseline")
        return reader

    def close(self):
        self.closed = True
        self.seen.clear()
        self.suppressed.clear()

    def poll(self, conversation_id):
        if self.closed or self.directory is None or not isinstance(conversation_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", conversation_id):
            return []
        path = self.directory / (conversation_id + ".db")
        try:
            with _connect(path) as connection:
                # Bound allocation before retrieving arbitrary blobs. Query
                # only current-turn planner steps, in their stable order.
                rows = connection.execute(
                    "SELECT idx, CASE WHEN length(step_payload)<=? THEN step_payload ELSE NULL END "
                    "FROM steps WHERE step_type=15 AND idx>? ORDER BY idx LIMIT ?",
                    (MAX_PAYLOAD, self.baselines.get(conversation_id, -1), MAX_STEPS + 1),
                ).fetchall()
            if len(rows) > MAX_STEPS:
                self._diagnose("step limit")
                self.close()
                return []
        except (OSError, sqlite3.Error):
            self._diagnose("read/schema")
            return []
        deltas = []
        for idx, payload in rows:
            key = (conversation_id, idx)
            if key in self.suppressed:
                continue
            try:
                text = decode_thinking(payload)
            except (ValueError, UnicodeError):
                self._diagnose("payload/schema")
                continue
            previous = self.seen.get(key, "")
            if not text.startswith(previous):
                self.suppressed.add(key)
                self._diagnose("replacement")
                continue
            self.seen[key] = text
            if len(text) > len(previous):
                deltas.append(text[len(previous):])
        return deltas
