"""Tests for the minimal GGUF metadata reader."""

from __future__ import annotations

import struct
from pathlib import Path

from drove.gguf import uses_sliding_window_attention

# Metadata value type ids from the GGUF spec.
T_UINT32 = 4
T_STRING = 8
T_ARRAY = 9


def _kv_string(key: str, value: str) -> bytes:
    return _key(key) + struct.pack("<I", T_STRING) + _str(value)


def _kv_uint32(key: str, value: int) -> bytes:
    return _key(key) + struct.pack("<I", T_UINT32) + struct.pack("<I", value)


def _kv_string_array(key: str, values: list[str]) -> bytes:
    body = struct.pack("<I", T_STRING) + struct.pack("<Q", len(values))
    body += b"".join(_str(v) for v in values)
    return _key(key) + struct.pack("<I", T_ARRAY) + body


def _kv_uint32_array(key: str, values: list[int]) -> bytes:
    body = struct.pack("<I", T_UINT32) + struct.pack("<Q", len(values))
    body += b"".join(struct.pack("<I", v) for v in values)
    return _key(key) + struct.pack("<I", T_ARRAY) + body


def _key(key: str) -> bytes:
    return _str(key)


def _str(value: str) -> bytes:
    raw = value.encode()
    return struct.pack("<Q", len(raw)) + raw


def write_gguf(
    path: Path, entries: list[bytes], *, version: int = 3, magic: bytes = b"GGUF"
) -> Path:
    counts = struct.pack("<Q", 0) + struct.pack("<Q", len(entries))  # tensors, metadata pairs
    header = magic + struct.pack("<I", version) + counts
    path.write_bytes(header + b"".join(entries) + b"\x00" * 32)  # trailing tensor data
    return path


def test_detects_a_sliding_window_model(tmp_path: Path) -> None:
    model = write_gguf(
        tmp_path / "m.gguf",
        [
            _kv_string("general.architecture", "gemma3"),
            _kv_uint32("gemma3.context_length", 131072),
            _kv_uint32("gemma3.attention.sliding_window", 1024),
        ],
    )
    assert uses_sliding_window_attention(model) is True


def test_model_without_the_key_is_not_sliding_window(tmp_path: Path) -> None:
    model = write_gguf(
        tmp_path / "m.gguf",
        [
            _kv_string("general.architecture", "qwen3"),
            _kv_uint32("qwen3.context_length", 32768),
        ],
    )
    assert uses_sliding_window_attention(model) is False


def test_declared_window_of_zero_means_full_attention(tmp_path: Path) -> None:
    model = write_gguf(
        tmp_path / "m.gguf",
        [_kv_uint32("someearch.attention.sliding_window", 0)],
    )
    assert uses_sliding_window_attention(model) is False


def test_scan_walks_past_arrays_to_reach_later_keys(tmp_path: Path) -> None:
    """Tokenizer vocabularies sit between the keys we read; skipping must be exact."""
    model = write_gguf(
        tmp_path / "m.gguf",
        [
            _kv_string("general.architecture", "gemma3"),
            _kv_string_array("tokenizer.ggml.tokens", [f"tok{i}" for i in range(500)]),
            _kv_uint32_array("tokenizer.ggml.token_type", list(range(500))),
            _kv_uint32("gemma3.attention.sliding_window", 512),
        ],
    )
    assert uses_sliding_window_attention(model) is True


def test_missing_file_is_unknown(tmp_path: Path) -> None:
    assert uses_sliding_window_attention(tmp_path / "nope.gguf") is None


def test_non_gguf_file_is_unknown(tmp_path: Path) -> None:
    path = tmp_path / "m.gguf"
    path.write_bytes(b"this is not a model" * 8)
    assert uses_sliding_window_attention(path) is None


def test_truncated_header_is_unknown(tmp_path: Path) -> None:
    path = tmp_path / "m.gguf"
    path.write_bytes(b"GGUF" + struct.pack("<I", 3) + struct.pack("<Q", 0))
    assert uses_sliding_window_attention(path) is None


def test_unsupported_version_is_unknown(tmp_path: Path) -> None:
    entries = [_kv_uint32("x.attention.sliding_window", 8)]
    model = write_gguf(tmp_path / "m.gguf", entries, version=1)
    assert uses_sliding_window_attention(model) is None


def test_empty_file_is_unknown(tmp_path: Path) -> None:
    path = tmp_path / "m.gguf"
    path.write_bytes(b"")
    assert uses_sliding_window_attention(path) is None
