"""Minimal GGUF metadata reader.

Only enough of the format to answer questions drove needs before starting a
backend. Adding the full ``gguf`` package as a dependency would pull in numpy
for a handful of header fields.

Format: https://github.com/ggml-org/ggml/blob/master/docs/gguf.md
"""

from __future__ import annotations

import logging
import mmap
import struct
from pathlib import Path

logger = logging.getLogger(__name__)

_MAGIC = b"GGUF"
_MIN_VERSION = 2  # v1 sized its counts differently and predates every model we serve

# Metadata value types, mapped to their fixed width in bytes.
_FIXED_WIDTHS = {
    0: 1,  # uint8
    1: 1,  # int8
    2: 2,  # uint16
    3: 2,  # int16
    4: 4,  # uint32
    5: 4,  # int32
    6: 4,  # float32
    7: 1,  # bool
    10: 8,  # uint64
    11: 8,  # int64
    12: 8,  # float64
}
_TYPE_STRING = 8
_TYPE_ARRAY = 9

#: Key suffix every sliding-window architecture sets (gemma3.attention.sliding_window,
#: phi3.attention.sliding_window, …).
_SLIDING_WINDOW_SUFFIX = ".attention.sliding_window"


class _MalformedGGUF(Exception):
    """The file is not a GGUF file, or its header does not parse."""


class _Cursor:
    """Sequential reader over a memory-mapped GGUF header."""

    def __init__(self, buf: mmap.mmap | bytes) -> None:
        self._buf = buf
        self._off = 0
        self._len = len(buf)

    def _take(self, n: int) -> int:
        start = self._off
        if start + n > self._len:
            raise _MalformedGGUF("truncated header")
        self._off += n
        return start

    def u32(self) -> int:
        off = self._take(4)
        return int(struct.unpack_from("<I", self._buf, off)[0])

    def u64(self) -> int:
        off = self._take(8)
        return int(struct.unpack_from("<Q", self._buf, off)[0])

    def string(self) -> str:
        length = self.u64()
        off = self._take(length)
        return bytes(self._buf[off : off + length]).decode("utf-8", errors="replace")

    def skip(self, n: int) -> None:
        self._take(n)

    def read_int(self, value_type: int) -> int | None:
        """Read an integer value, or None if this type is not an integer."""
        formats = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 10: "<Q", 11: "<q"}
        fmt = formats.get(value_type)
        if fmt is None:
            self.skip_value(value_type)
            return None
        off = self._take(_FIXED_WIDTHS[value_type])
        return int(struct.unpack_from(fmt, self._buf, off)[0])

    def skip_value(self, value_type: int) -> None:
        width = _FIXED_WIDTHS.get(value_type)
        if width is not None:
            self.skip(width)
            return
        if value_type == _TYPE_STRING:
            self.skip(self.u64())
            return
        if value_type == _TYPE_ARRAY:
            item_type = self.u32()
            count = self.u64()
            item_width = _FIXED_WIDTHS.get(item_type)
            if item_width is not None:
                self.skip(item_width * count)
                return
            if item_type == _TYPE_STRING:
                # Tokenizer vocabularies live here; skipping each length is the
                # only way past them, but the keys we want come earlier.
                for _ in range(count):
                    self.skip(self.u64())
                return
            raise _MalformedGGUF(f"unsupported array element type {item_type}")
        raise _MalformedGGUF(f"unsupported value type {value_type}")


def uses_sliding_window_attention(model_path: Path) -> bool | None:
    """Return whether a GGUF model uses sliding-window attention.

    Returns None when the file cannot be inspected (missing, not GGUF, or a
    header this reader does not understand), so callers can tell "no" apart
    from "unknown".
    """
    try:
        with model_path.open("rb") as f:
            with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as buf:
                return _scan_for_sliding_window(buf)
    except (OSError, ValueError, _MalformedGGUF) as e:
        logger.debug("Could not read GGUF metadata from %s: %s", model_path, e)
        return None


def _scan_for_sliding_window(buf: mmap.mmap | bytes) -> bool | None:
    cur = _Cursor(buf)
    if bytes(buf[:4]) != _MAGIC:
        raise _MalformedGGUF("missing GGUF magic")
    cur.skip(4)
    version = cur.u32()
    if version < _MIN_VERSION:
        raise _MalformedGGUF(f"unsupported GGUF version {version}")
    cur.u64()  # tensor count
    kv_count = cur.u64()

    for _ in range(kv_count):
        key = cur.string()
        value_type = cur.u32()
        if key.endswith(_SLIDING_WINDOW_SUFFIX):
            window = cur.read_int(value_type)
            # A declared window of 0 means the architecture has the key but
            # keeps full attention.
            return bool(window)
        cur.skip_value(value_type)
    return False
