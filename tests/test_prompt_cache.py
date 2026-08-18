"""Tests for the persistent prompt (KV) cache across model sleep/wake."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from drove.backend import BACKEND_ASR, BACKEND_LLAMA
from drove.config import Config
from drove.model_config import ModelConfig
from drove.server_manager import ServerManager, _ModelInstance, _prompt_cache_slug


def make_config(tmp_path: Path, **overrides: Any) -> Config:
    models_dir = tmp_path / "models"
    models_dir.mkdir(exist_ok=True)
    defaults: dict[str, Any] = {
        "models_dir": models_dir,
        "prompt_cache": True,
        "prompt_cache_dir": tmp_path / "prompt-cache",
        "prompt_cache_ttl_seconds": 3600,
    }
    defaults.update(overrides)
    return Config(**defaults)


def make_instance(
    tmp_path: Path, backend: str = BACKEND_LLAMA, model_name: str = "org/mymodel"
) -> _ModelInstance:
    process = MagicMock()
    process.returncode = None
    process.pid = 4242
    model_path = tmp_path / "models" / "mymodel.gguf"
    return _ModelInstance(model_name, process, 9999, model_path, (0.0, 0.0), backend)


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self._payload: dict[str, Any] = payload if payload is not None else {}

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPError(f"HTTP {self.status_code}")


def fake_client(get: Any = None, post: Any = None) -> tuple[Any, MagicMock]:
    """Return (patcher, client_mock) for httpx.AsyncClient used as a context manager."""
    client = MagicMock()
    client.get = AsyncMock(return_value=get)
    client.post = AsyncMock(return_value=post)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return patch("drove.server_manager.httpx.AsyncClient", return_value=ctx), client


def write_cache_file(cache_dir: Path, name: str, age_seconds: float = 0.0) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / name
    path.write_bytes(b"kv")
    if age_seconds:
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
    return path


# ── Directory naming ──────────────────────────────────────────────────────────


def test_slug_is_filesystem_safe() -> None:
    slug = _prompt_cache_slug("unsloth/Qwen3-8B-GGUF")
    assert "/" not in slug
    assert slug.startswith("unsloth_Qwen3-8B-GGUF-")


def test_slug_distinguishes_names_that_sanitize_alike() -> None:
    assert _prompt_cache_slug("a/b") != _prompt_cache_slug("a:b")


# ── --slot-save-path wiring ───────────────────────────────────────────────────


def test_slot_save_path_not_passed_when_disabled(tmp_path: Path) -> None:
    manager = ServerManager(make_config(tmp_path, prompt_cache=False))
    assert manager._prepare_prompt_cache_dir("mymodel") is None

    args = manager._build_args(tmp_path / "m.gguf", ModelConfig(), 1234, None)
    assert "--slot-save-path" not in args


def test_slot_save_path_passed_and_directory_created(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    manager = ServerManager(config)

    cache_dir = manager._prepare_prompt_cache_dir("org/mymodel")
    assert cache_dir is not None
    assert cache_dir.is_dir()
    assert cache_dir.parent == config.prompt_cache_dir

    args = manager._build_args(tmp_path / "m.gguf", ModelConfig(), 1234, cache_dir)
    # llama-server joins the path with the filename verbatim, so it needs a
    # trailing separator.
    assert args[args.index("--slot-save-path") + 1] == f"{cache_dir}/"


# ── TTL expiry ────────────────────────────────────────────────────────────────


def test_expired_cache_files_are_dropped_before_start(tmp_path: Path) -> None:
    manager = ServerManager(make_config(tmp_path, prompt_cache_ttl_seconds=3600))
    cache_dir = manager._prompt_cache_dir_for("org/mymodel")
    stale = write_cache_file(cache_dir, "slot-0.bin", age_seconds=7200)
    fresh = write_cache_file(cache_dir, "slot-1.bin")

    manager._prepare_prompt_cache_dir("org/mymodel")

    assert not stale.exists()
    assert fresh.exists()


def test_ttl_zero_keeps_cache_forever(tmp_path: Path) -> None:
    manager = ServerManager(make_config(tmp_path, prompt_cache_ttl_seconds=0))
    cache_dir = manager._prompt_cache_dir_for("org/mymodel")
    ancient = write_cache_file(cache_dir, "slot-0.bin", age_seconds=10_000_000)

    manager._prepare_prompt_cache_dir("org/mymodel")

    assert ancient.exists()


def test_prune_prompt_cache_sweeps_every_model(tmp_path: Path) -> None:
    manager = ServerManager(make_config(tmp_path, prompt_cache_ttl_seconds=60))
    stale_a = write_cache_file(manager._prompt_cache_dir_for("a"), "slot-0.bin", age_seconds=600)
    stale_b = write_cache_file(manager._prompt_cache_dir_for("b"), "slot-0.bin", age_seconds=600)
    fresh = write_cache_file(manager._prompt_cache_dir_for("b"), "slot-1.bin")

    manager.prune_prompt_cache()

    assert not stale_a.exists()
    assert not stale_b.exists()
    assert fresh.exists()


def test_prune_prompt_cache_noop_when_disabled(tmp_path: Path) -> None:
    manager = ServerManager(make_config(tmp_path, prompt_cache=False, prompt_cache_ttl_seconds=60))
    stale = write_cache_file(manager._prompt_cache_dir_for("a"), "slot-0.bin", age_seconds=600)

    manager.prune_prompt_cache()

    assert stale.exists()


# ── Saving ────────────────────────────────────────────────────────────────────


async def test_save_posts_one_request_per_idle_slot(tmp_path: Path) -> None:
    manager = ServerManager(make_config(tmp_path))
    inst = make_instance(tmp_path)
    slots = FakeResponse(payload=[{"id": 0, "is_processing": False}])  # type: ignore[arg-type]
    saved = FakeResponse(payload={"n_saved": 1745})

    patcher, client = fake_client(get=slots, post=saved)
    with patcher:
        await manager._save_prompt_cache(inst)

    client.post.assert_awaited_once()
    url, kwargs = client.post.await_args[0][0], client.post.await_args[1]
    assert url.endswith("/slots/0")
    assert kwargs["params"] == {"action": "save"}
    assert kwargs["json"] == {"filename": "slot-0.bin"}


async def test_save_skips_busy_slots(tmp_path: Path) -> None:
    manager = ServerManager(make_config(tmp_path))
    inst = make_instance(tmp_path)
    slots = FakeResponse(payload=[{"id": 0, "is_processing": True}])  # type: ignore[arg-type]

    patcher, client = fake_client(get=slots, post=FakeResponse(payload={"n_saved": 1}))
    with patcher:
        await manager._save_prompt_cache(inst)

    client.post.assert_not_awaited()


async def test_save_skipped_for_asr_backend(tmp_path: Path) -> None:
    manager = ServerManager(make_config(tmp_path))
    inst = make_instance(tmp_path, backend=BACKEND_ASR)

    patcher, client = fake_client(get=FakeResponse(payload=[]))  # type: ignore[arg-type]
    with patcher:
        await manager._save_prompt_cache(inst)

    client.get.assert_not_awaited()


async def test_save_skipped_when_disabled(tmp_path: Path) -> None:
    manager = ServerManager(make_config(tmp_path, prompt_cache=False))
    inst = make_instance(tmp_path)

    patcher, client = fake_client(get=FakeResponse(payload=[]))  # type: ignore[arg-type]
    with patcher:
        await manager._save_prompt_cache(inst)

    client.get.assert_not_awaited()


async def test_empty_slot_leaves_no_cache_file(tmp_path: Path) -> None:
    manager = ServerManager(make_config(tmp_path))
    cache_dir = manager._prompt_cache_dir_for("org/mymodel")
    leftover = write_cache_file(cache_dir, "slot-0.bin")

    client = MagicMock()
    client.post = AsyncMock(return_value=FakeResponse(payload={"n_saved": 0}))
    await manager._save_slot(client, "http://x", "org/mymodel", {"id": 0})

    assert not leftover.exists()


async def test_save_failure_does_not_raise(tmp_path: Path) -> None:
    manager = ServerManager(make_config(tmp_path))
    inst = make_instance(tmp_path)

    patcher, _ = fake_client(get=FakeResponse(status_code=503))
    with patcher:
        await manager._save_prompt_cache(inst)  # must not raise


async def test_stop_saves_cache_before_terminating(tmp_path: Path) -> None:
    manager = ServerManager(make_config(tmp_path))
    inst = make_instance(tmp_path, model_name="mymodel")
    inst.process.wait = AsyncMock(return_value=0)
    manager._instances["mymodel"] = inst

    order: list[str] = []
    inst.process.send_signal.side_effect = lambda _sig: order.append("terminate")

    async def record_save(_inst: _ModelInstance) -> None:
        order.append("save")

    with patch.object(manager, "_save_prompt_cache", side_effect=record_save) as save:
        await manager._stop_instance("mymodel")

    save.assert_awaited_once()
    assert save.await_args[0][0] is inst
    # Saving after SIGTERM would race the dying process out of its own cache.
    assert order == ["save", "terminate"]


# ── Restoring ─────────────────────────────────────────────────────────────────


async def test_restore_posts_for_each_saved_slot(tmp_path: Path) -> None:
    manager = ServerManager(make_config(tmp_path))
    inst = make_instance(tmp_path)
    cache_dir = manager._prompt_cache_dir_for(inst.model_name)
    write_cache_file(cache_dir, "slot-0.bin")
    write_cache_file(cache_dir, "slot-1.bin")

    patcher, client = fake_client(post=FakeResponse(payload={"n_restored": 1745}))
    with patcher:
        await manager._restore_prompt_cache(inst, cache_dir)

    assert client.post.await_count == 2
    first = client.post.await_args_list[0]
    assert first[0][0].endswith("/slots/0")
    assert first[1]["params"] == {"action": "restore"}
    assert first[1]["json"] == {"filename": "slot-0.bin"}


async def test_restore_is_a_noop_without_cache_files(tmp_path: Path) -> None:
    manager = ServerManager(make_config(tmp_path))
    inst = make_instance(tmp_path)
    cache_dir = manager._prompt_cache_dir_for(inst.model_name)
    cache_dir.mkdir(parents=True)

    patcher, client = fake_client(post=FakeResponse())
    with patcher:
        await manager._restore_prompt_cache(inst, cache_dir)

    client.post.assert_not_awaited()


async def test_rejected_cache_file_is_deleted(tmp_path: Path) -> None:
    """A file the server refuses (ctx_size or slot count changed) is dead weight."""
    manager = ServerManager(make_config(tmp_path))
    cache_dir = manager._prompt_cache_dir_for("org/mymodel")
    path = write_cache_file(cache_dir, "slot-0.bin")

    client = MagicMock()
    client.post = AsyncMock(return_value=FakeResponse(status_code=400))
    await manager._restore_slot(client, "http://x", "org/mymodel", path, 0)

    assert not path.exists()


async def test_restore_failure_does_not_raise(tmp_path: Path) -> None:
    manager = ServerManager(make_config(tmp_path))
    inst = make_instance(tmp_path)
    cache_dir = manager._prompt_cache_dir_for(inst.model_name)
    write_cache_file(cache_dir, "slot-0.bin")

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(side_effect=httpx.ConnectError("refused"))
    ctx.__aexit__ = AsyncMock(return_value=False)
    with patch("drove.server_manager.httpx.AsyncClient", return_value=ctx):
        await manager._restore_prompt_cache(inst, cache_dir)  # must not raise


@pytest.mark.parametrize("name", ["notes.txt", "slot-x.bin", "slot.bin"])
async def test_unrecognized_files_are_ignored(tmp_path: Path, name: str) -> None:
    manager = ServerManager(make_config(tmp_path))
    inst = make_instance(tmp_path)
    cache_dir = manager._prompt_cache_dir_for(inst.model_name)
    write_cache_file(cache_dir, name)

    patcher, client = fake_client(post=FakeResponse())
    with patcher:
        await manager._restore_prompt_cache(inst, cache_dir)

    client.post.assert_not_awaited()


# ── Sliding-window models ─────────────────────────────────────────────────────


def test_swa_model_gets_full_swa_cache(tmp_path: Path) -> None:
    """Without --swa-full a restored cache is unusable on sliding-window models."""
    manager = ServerManager(make_config(tmp_path))
    args = manager._build_args(
        tmp_path / "m.gguf", ModelConfig(), 1234, tmp_path / "pc", uses_swa=True
    )
    assert "--swa-full" in args


def test_non_swa_model_does_not_pay_for_a_full_swa_cache(tmp_path: Path) -> None:
    manager = ServerManager(make_config(tmp_path))
    args = manager._build_args(
        tmp_path / "m.gguf", ModelConfig(), 1234, tmp_path / "pc", uses_swa=False
    )
    assert "--swa-full" not in args


def test_explicit_swa_full_setting_is_respected(tmp_path: Path) -> None:
    manager = ServerManager(make_config(tmp_path))
    args = manager._build_args(
        tmp_path / "m.gguf", ModelConfig(swa_full=False), 1234, tmp_path / "pc", uses_swa=True
    )
    assert "--swa-full" not in args


def test_no_swa_full_without_prompt_cache(tmp_path: Path) -> None:
    manager = ServerManager(make_config(tmp_path, prompt_cache=False))
    args = manager._build_args(tmp_path / "m.gguf", ModelConfig(), 1234, None)
    assert "--swa-full" not in args


async def test_sliding_window_detection_is_cached_per_file(tmp_path: Path) -> None:
    manager = ServerManager(make_config(tmp_path))
    model = tmp_path / "m.gguf"
    model.write_bytes(b"not a gguf")

    with patch("drove.server_manager.uses_sliding_window_attention", return_value=True) as detect:
        assert await manager._detect_sliding_window(model) is True
        assert await manager._detect_sliding_window(model) is True

    detect.assert_called_once()


async def test_unreadable_model_reports_unknown(tmp_path: Path) -> None:
    manager = ServerManager(make_config(tmp_path))
    assert await manager._detect_sliding_window(tmp_path / "missing.gguf") is None


async def test_idle_shutdown_does_not_cancel_itself(tmp_path: Path) -> None:
    """The idle watcher must survive its own _stop_instance call.

    Cancelling the calling task there raises CancelledError at the first await
    and the llama-server process is never signalled — an orphaned model.
    """
    import asyncio

    manager = ServerManager(make_config(tmp_path, idle_timeout_seconds=0))
    inst = make_instance(tmp_path, model_name="mymodel")
    inst.process.wait = AsyncMock(return_value=0)
    manager._instances["mymodel"] = inst

    reached: list[str] = []

    async def slow_save(_inst: _ModelInstance) -> None:
        await asyncio.sleep(0)  # a cancel scheduled on this task would fire here
        reached.append("saved")

    async def caller() -> None:
        manager._idle_tasks["mymodel"] = asyncio.current_task()  # type: ignore[assignment]
        with patch.object(manager, "_save_prompt_cache", side_effect=slow_save):
            await manager._stop_instance("mymodel")
        reached.append("stopped")

    await asyncio.create_task(caller())

    assert reached == ["saved", "stopped"]
    inst.process.send_signal.assert_called_once()


def test_prompt_cache_is_on_by_default(tmp_path: Path) -> None:
    config = Config(models_dir=tmp_path)
    assert config.prompt_cache is True
    assert config.prompt_cache_ttl_seconds == 3600
