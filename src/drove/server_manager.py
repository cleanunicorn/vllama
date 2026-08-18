"""Manages the llama-server subprocess lifecycle."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import logging
import re
import shutil
import signal
import socket
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import psutil

if TYPE_CHECKING:
    from drove.model_config import ModelConfig

from drove.backend import BACKEND_ASR, BACKEND_LLAMA, detect_backend, infer_asr_model_type
from drove.config import Config
from drove.gguf import uses_sliding_window_attention
from drove.model_config import (
    config_path_for_model,
    global_config_path,
    load_download_info,
    load_global_model_config,
    load_model_config,
)
from drove.model_store import ModelStore

logger = logging.getLogger(__name__)

HEALTH_CHECK_INTERVAL = 0.5  # seconds between health poll attempts


_STDERR_MAX_BYTES = 256 * 1024  # keep last 256 KB of stderr

_GGUF_SHARD_RE = re.compile(r"^(.+)-\d{5}-of-\d{5}\.gguf$", re.IGNORECASE)

_PROMPT_CACHE_GLOB = "slot-*.bin"
_PROMPT_CACHE_FILE_RE = re.compile(r"^slot-(\d+)\.bin$")
_PROMPT_CACHE_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _prompt_cache_slug(model_name: str) -> str:
    """Return a filesystem-safe directory name for a model's prompt cache.

    Model names contain path separators ("unsloth/Qwen3-8B-GGUF"), so they are
    sanitized and suffixed with a short digest of the original name to keep
    two models that sanitize to the same string apart.
    """
    safe = _PROMPT_CACHE_UNSAFE_RE.sub("_", model_name).strip("_") or "model"
    digest = hashlib.sha256(model_name.encode()).hexdigest()[:8]
    return f"{safe}-{digest}"


def _estimate_model_memory(model_path: Path) -> int:
    """Estimate the memory needed to serve a model, in bytes.

    Uses on-disk file size as a proxy for resident weight memory: all shards
    for sharded GGUF models, all .onnx files in the directory for ASR models,
    or the single model file otherwise. KV cache and runtime overhead are not
    counted, so budgets should leave headroom.
    """
    try:
        if model_path.suffix.lower() == ".onnx":
            return sum(f.stat().st_size for f in model_path.parent.glob("*.onnx") if f.is_file())
        shard = _GGUF_SHARD_RE.match(model_path.name)
        if shard:
            prefix = shard.group(1)
            return sum(
                f.stat().st_size
                for f in model_path.parent.iterdir()
                if f.is_file() and (m := _GGUF_SHARD_RE.match(f.name)) and m.group(1) == prefix
            )
        return model_path.stat().st_size
    except OSError:
        return 0


class _ModelInstance:
    """State for a single running llama-server process."""

    def __init__(
        self,
        model_name: str,
        process: asyncio.subprocess.Process,
        port: int,
        model_path: Path,
        config_mtimes: tuple[float, float],
        backend: str = BACKEND_LLAMA,
    ) -> None:
        self.model_name = model_name
        self.process = process
        self.port = port
        self.model_path = model_path
        self.backend = backend
        self.config_mtimes = config_mtimes  # (per-model mtime, global mtime) at startup
        self.needs_restart: bool = False
        self.est_memory_bytes: int = 0
        self.loaded_at: float = time.time()
        self.last_request_time: float = time.monotonic()
        self.active_requests: int = 0
        self._stderr_buf: bytearray = bytearray()
        self._stderr_task: asyncio.Task[None] | None = None

    def start_stderr_reader(self) -> None:
        """Start a background task that continuously drains stderr into a buffer."""
        if self.process.stderr is not None:
            self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        assert self.process.stderr is not None
        try:
            while True:
                chunk = await self.process.stderr.read(8192)
                if not chunk:
                    break
                self._stderr_buf.extend(chunk)
                # Trim to keep only the tail
                if len(self._stderr_buf) > _STDERR_MAX_BYTES:
                    self._stderr_buf = self._stderr_buf[-_STDERR_MAX_BYTES:]
        except Exception:
            pass

    @property
    def stderr_text(self) -> str:
        return bytes(self._stderr_buf).decode(errors="replace").strip()

    @property
    def is_running(self) -> bool:
        return self.process.returncode is None

    @property
    def idle_seconds(self) -> float:
        return time.monotonic() - self.last_request_time

    def get_process_stats(self) -> dict[str, object] | None:
        if not self.is_running:
            return None
        try:
            proc = psutil.Process(self.process.pid)
            mem = proc.memory_info()
            cpu_times = proc.cpu_times()
            elapsed = time.time() - proc.create_time()
            cpu_pct = (cpu_times.user + cpu_times.system) / elapsed * 100 if elapsed > 0 else 0
            return {
                "memory_rss_bytes": mem.rss,
                "cpu_percent": round(cpu_pct, 1),
            }
        except psutil.NoSuchProcess, psutil.AccessDenied:
            return None


class ServerManager:
    """Manages multiple llama-server subprocesses, one per model.

    Each model gets its own llama-server on a dynamically assigned port.
    Models are started lazily on first request and stopped after idle timeout.
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._instances: dict[str, _ModelInstance] = {}
        self._idle_tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()
        # (path, mtime) → whether that GGUF uses sliding-window attention.
        self._swa_cache: dict[tuple[str, float], bool | None] = {}

    @property
    def is_running(self) -> bool:
        return any(inst.is_running for inst in self._instances.values())

    @property
    def loaded_models(self) -> list[str]:
        return [name for name, inst in self._instances.items() if inst.is_running]

    @property
    def current_model(self) -> str | None:
        """For backwards compatibility — returns the first loaded model, or None."""
        models = self.loaded_models
        return models[0] if models else None

    @property
    def model_loaded_at(self) -> float | None:
        """For backwards compatibility — returns loaded_at of the first model."""
        models = self.loaded_models
        if models:
            return self._instances[models[0]].loaded_at
        return None

    @property
    def idle_seconds(self) -> float:
        """For backwards compatibility — returns minimum idle across all models."""
        running = [inst for inst in self._instances.values() if inst.is_running]
        if not running:
            return 0.0
        return min(inst.idle_seconds for inst in running)

    def _get_config_mtimes(self, model_path: Path) -> tuple[float, float]:
        """Return modification times of the per-model and global config files.

        Returns 0.0 for files that do not exist yet.
        """
        model_cfg_path = config_path_for_model(model_path)
        global_cfg_path = global_config_path(self._config.models_dir)
        model_mtime = model_cfg_path.stat().st_mtime if model_cfg_path.exists() else 0.0
        global_mtime = global_cfg_path.stat().st_mtime if global_cfg_path.exists() else 0.0
        return (model_mtime, global_mtime)

    def base_url_for(self, model_name: str) -> str:
        inst = self._instances.get(model_name)
        port = inst.port if inst else 0
        return f"http://{self._config.llama_server_host}:{port}"

    @property
    def base_url(self) -> str:
        """For backwards compatibility — returns base_url of the first loaded model."""
        model = self.current_model
        if model:
            return self.base_url_for(model)
        return f"http://{self._config.llama_server_host}:0"

    def get_process_stats(self) -> dict[str, object] | None:
        """Return aggregated stats, or per-model stats if multiple models loaded."""
        running = {name: inst for name, inst in self._instances.items() if inst.is_running}
        if not running:
            return None
        if len(running) == 1:
            return next(iter(running.values())).get_process_stats()
        return {name: inst.get_process_stats() for name, inst in running.items()}

    def get_all_model_info(self) -> list[dict[str, object]]:
        """Return status info for all loaded models."""
        now = time.time()
        result = []
        for name, inst in self._instances.items():
            if not inst.is_running:
                continue
            result.append(
                {
                    "name": name,
                    "loaded_seconds": round(now - inst.loaded_at, 1),
                    "idle_seconds": round(inst.idle_seconds, 1),
                    "idle_timeout_seconds": self._config.idle_timeout_seconds,
                    "active_requests": inst.active_requests,
                    "port": inst.port,
                    "est_memory_bytes": inst.est_memory_bytes,
                }
            )
        return result

    def record_request(self, model_name: str) -> None:
        """Call on each proxied request to reset the idle timer for a model."""
        inst = self._instances.get(model_name)
        if inst:
            inst.last_request_time = time.monotonic()

    def request_started(self, model_name: str) -> None:
        """Mark a request as in-flight for a model."""
        inst = self._instances.get(model_name)
        if inst:
            inst.active_requests += 1
            inst.last_request_time = time.monotonic()

    def request_finished(self, model_name: str) -> None:
        """Mark a request as complete and reset the idle timer for a model."""
        inst = self._instances.get(model_name)
        if inst:
            inst.active_requests = max(0, inst.active_requests - 1)
            inst.last_request_time = time.monotonic()

    async def ensure_running(self, model_name: str, *, claim: bool = False) -> None:
        """Ensure llama-server is running for the requested model.

        If the model is already loaded with an up-to-date config, this is a no-op.
        When the per-model or global config file has been modified since startup,
        the instance is restarted if idle; otherwise it is flagged for restart once
        all in-flight requests finish (the idle watcher handles that case).
        When ``max_loaded_models`` or the ``max_memory`` budget would be
        exceeded, least-recently-used models are drained (wait for their active
        requests to finish) and evicted before starting the new one.

        If *claim* is True, atomically increment ``active_requests`` before
        releasing the lock.  This prevents a race where a concurrent
        ``ensure_running`` call for a different model could evict and kill
        this server between the time ``ensure_running`` returns and the
        caller records the request as in-flight.  Callers passing
        ``claim=True`` must pair it with a later ``request_finished`` call.
        """
        async with self._lock:
            inst = self._instances.get(model_name)
            if inst is not None and inst.is_running:
                # Check whether the config changed since this instance was started.
                current_mtimes = self._get_config_mtimes(inst.model_path)
                if current_mtimes != inst.config_mtimes:
                    if inst.active_requests == 0:
                        logger.info(
                            "Config changed for model=%s, restarting with new config",
                            model_name,
                        )
                        await self._stop_instance(model_name)
                        # Fall through to _evict_if_needed + _start
                    else:
                        logger.info(
                            "Config changed for model=%s, will restart when requests finish",
                            model_name,
                        )
                        inst.needs_restart = True
                        if claim:
                            self._claim_slot(model_name)
                        return
                else:
                    if claim:
                        self._claim_slot(model_name)
                    return  # running with current config, nothing to do
            elif inst is not None:
                # Clean up stale instance if process died
                await self._stop_instance(model_name)
            # Resolve before evicting so an unknown model cannot evict anything.
            incoming_bytes = _estimate_model_memory(self._resolve_model(model_name))
            await self._evict_if_needed(incoming_bytes)
            await self._start(model_name)
            if claim:
                self._claim_slot(model_name)

    def _claim_slot(self, model_name: str) -> None:
        """Increment active_requests for a model (caller must hold the lock)."""
        inst = self._instances.get(model_name)
        if inst is not None:
            inst.active_requests += 1
            inst.last_request_time = time.monotonic()

    async def _evict_if_needed(self, incoming_bytes: int = 0) -> None:
        """Evict loaded models until count and memory budgets allow one more.

        Two independent limits apply: ``max_loaded_models`` caps the number of
        loaded models, and ``max_memory`` caps the combined memory estimate of
        the loaded models plus *incoming_bytes* (the estimate for the model
        about to start). Either limit set to 0 means unlimited.

        Prefers evicting a model with no active connections: models that are
        still serving in-flight requests are left running. Among the idle
        models the least-recently-used is chosen. Only when every loaded model
        is busy do we fall back to draining the least-recently-used one (wait
        for its active requests to finish before stopping it).

        Must be called while holding ``self._lock``.
        """
        max_models = self._config.max_loaded_models
        max_memory = self._config.max_memory_bytes
        if max_models <= 0 and max_memory <= 0:
            return  # unlimited

        if max_memory > 0 and incoming_bytes > max_memory:
            logger.warning(
                "Model memory estimate (%d bytes) alone exceeds max_memory=%s; "
                "starting it anyway after evicting all other models",
                incoming_bytes,
                self._config.max_memory,
            )

        # Loop: draining the busy fallback releases the lock, so the situation
        # may change while we wait. Re-evaluate from scratch after every drain
        # or eviction — memory pressure can require evicting more than one model.
        while True:
            running = {n: i for n, i in self._instances.items() if i.is_running}
            over_count = max_models > 0 and len(running) >= max_models
            used_bytes = sum(i.est_memory_bytes for i in running.values())
            over_memory = (
                max_memory > 0 and bool(running) and used_bytes + incoming_bytes > max_memory
            )
            if not over_count and not over_memory:
                return

            # Prefer an idle model (no active connections); pick LRU among those.
            # Fall back to the LRU of all running models only when all are busy.
            idle = {n: i for n, i in running.items() if i.active_requests == 0}
            candidates = idle or running
            victim_name = min(candidates, key=lambda n: candidates[n].last_request_time)
            victim = running[victim_name]

            if victim.active_requests == 0:
                reason = (
                    f"max_loaded_models={max_models}"
                    if over_count
                    else f"max_memory={self._config.max_memory}"
                )
                logger.info("Evicting model=%s to make room (%s)", victim_name, reason)
                await self._stop_instance(victim_name)
                continue

            # Every running model is busy: wait for the LRU to drain. Release
            # the lock while waiting so its in-flight requests can complete.
            logger.info(
                "Waiting for %d active request(s) on model=%s before evicting",
                victim.active_requests,
                victim_name,
            )
            self._lock.release()
            try:
                while victim.active_requests > 0:
                    await asyncio.sleep(0.5)
            finally:
                await self._lock.acquire()
            # The victim may have been re-claimed (or capacity freed) while the
            # lock was released; loop back to re-evaluate before stopping anything.

    async def stop(self) -> None:
        """Gracefully stop all running llama-server processes."""
        async with self._lock:
            names = list(self._instances.keys())
            for name in names:
                await self._stop_instance(name)

    async def stop_model(self, model_name: str) -> None:
        """Gracefully stop a specific model's llama-server."""
        async with self._lock:
            await self._stop_instance(model_name)

    async def _start(self, model_name: str) -> None:
        model_path = self._resolve_model(model_name)
        model_cfg = load_model_config(model_path)
        backend = detect_backend(model_path, model_cfg)

        port = _find_free_port()
        slot_save_path: Path | None = None
        if backend == BACKEND_ASR:
            cmd = self._build_asr_command(model_name, model_path, model_cfg, port)
        else:
            binary = self._config.llama_server_bin
            if not shutil.which(binary):
                raise FileNotFoundError(
                    f"llama-server binary '{binary}' not found on PATH. "
                    "Install llama.cpp or set 'llama_server_bin' in config."
                )
            slot_save_path = self._prepare_prompt_cache_dir(model_name)
            uses_swa = False
            if slot_save_path is not None:
                uses_swa = bool(await self._detect_sliding_window(model_path))
            cmd = [
                binary,
                *self._build_args(model_path, model_cfg, port, slot_save_path, uses_swa),
            ]

        logger.info("Starting %s backend: %s", backend, " ".join(cmd))
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        config_mtimes = self._get_config_mtimes(model_path)
        inst = _ModelInstance(model_name, process, port, model_path, config_mtimes, backend)
        inst.est_memory_bytes = _estimate_model_memory(model_path)
        inst.start_stderr_reader()
        self._instances[model_name] = inst

        try:
            await self._wait_for_health(inst)
        except (RuntimeError, TimeoutError) as e:
            logger.error("Backend failed to start for model=%s: %s", model_name, e)
            await self._stop_instance(model_name)
            raise

        if slot_save_path is not None:
            await self._restore_prompt_cache(inst, slot_save_path)

        self._start_idle_watcher(model_name)
        logger.info("Backend ready (model=%s, backend=%s, port=%d)", model_name, backend, port)

    async def _stop_instance(self, model_name: str) -> None:
        task = self._idle_tasks.pop(model_name, None)
        # The idle watcher calls this from inside its own task: cancelling it
        # here would raise CancelledError at the next await and leave the
        # process running. It exits on its own right after this returns.
        if task is not None and task is not asyncio.current_task():
            task.cancel()

        inst = self._instances.pop(model_name, None)
        if inst is None:
            return

        if not inst.is_running:
            return

        await self._save_prompt_cache(inst)

        logger.info("Stopping llama-server (model=%s, pid=%d)", model_name, inst.process.pid)
        try:
            inst.process.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(inst.process.wait(), timeout=10.0)
            except TimeoutError:
                logger.warning("llama-server did not stop in time, sending SIGKILL")
                inst.process.kill()
                await inst.process.wait()
        except ProcessLookupError:
            pass  # already gone

    async def _wait_for_health(self, inst: _ModelInstance) -> None:
        url = f"http://{self._config.llama_server_host}:{inst.port}/health"
        timeout = self._config.startup_timeout_seconds
        deadline = time.monotonic() + timeout
        async with httpx.AsyncClient() as client:
            while time.monotonic() < deadline:
                if not inst.is_running:
                    # Give the stderr reader a moment to finish draining
                    await asyncio.sleep(0.2)
                    msg = "Model server exited unexpectedly during startup"
                    stderr = inst.stderr_text
                    if stderr:
                        msg += f"\nstderr: {stderr}"
                    raise RuntimeError(msg)
                try:
                    resp = await client.get(url, timeout=2.0)
                    if resp.status_code == 200:
                        return
                except httpx.TransportError:
                    pass
                await asyncio.sleep(HEALTH_CHECK_INTERVAL)
        stderr = inst.stderr_text
        msg = f"Model server did not become healthy within {timeout}s"
        if stderr:
            msg += f"\nstderr (last lines):\n{_tail(stderr, 30)}"
        raise TimeoutError(msg)

    def _build_args(
        self,
        model_path: Path,
        model_cfg: ModelConfig,
        port: int,
        slot_save_path: Path | None = None,
        uses_swa: bool = False,
    ) -> list[str]:
        from drove.model_config import ModelConfig  # local import to avoid circular

        # Start with global defaults from config.toml [llama_server]
        base_cfg = ModelConfig(
            n_gpu_layers=self._config.llama_server.n_gpu_layers,
            threads=self._config.llama_server.threads,
        )
        # Layer on global model config from _global.toml in models dir
        global_model_cfg = load_global_model_config(self._config.models_dir)
        merged = base_cfg.model_copy(update={k: v for k, v in global_model_cfg.to_dict().items()})
        # Model-specific overrides take precedence
        merged = merged.model_copy(update={k: v for k, v in model_cfg.to_dict().items()})

        # Resolve relative mmproj paths against the model directory
        if merged.mmproj and not Path(merged.mmproj).is_absolute():
            merged = merged.model_copy(update={"mmproj": str(model_path.parent / merged.mmproj)})

        args = [
            "--model",
            str(model_path),
            "--host",
            self._config.llama_server_host,
            "--port",
            str(port),
        ]
        if slot_save_path is not None:
            args.extend(["--slot-save-path", f"{slot_save_path}/"])
            # A restored KV cache is only reusable if the whole cache was
            # serialized, and sliding-window models keep only the window unless
            # the full SWA cache is enabled. Measured on gemma-4-E4B: without
            # --swa-full a restored cache produced zero reuse (23 tokens
            # re-processed); with it, 22 of 23 tokens came from the cache. It
            # costs memory (5.8 GB → 7.0 GB at 32k context on that model), so it
            # is only added for models that actually use SWA. An explicit
            # swa_full in the model config still wins.
            if uses_swa and merged.swa_full is None:
                merged = merged.model_copy(update={"swa_full": True})
        args.extend(merged.to_llama_args())
        return args

    def _build_asr_command(
        self, model_name: str, model_path: Path, model_cfg: ModelConfig, port: int
    ) -> list[str]:
        """Build the command for the built-in ASR worker subprocess."""
        if not _onnx_asr_available():
            raise RuntimeError(
                f"Model '{model_name}' is a speech-to-text model, but the 'onnx-asr' "
                "package is not installed. Install drove with the asr extra: "
                "pip install 'drove[asr]'"
            )

        model_type = model_cfg.asr_model or self._infer_asr_model_type(model_name, model_path)
        cmd = [
            sys.executable,
            "-m",
            "drove.workers.asr",
            "--model-dir",
            str(model_path.parent),
            "--model-type",
            model_type,
            "--host",
            self._config.llama_server_host,
            "--port",
            str(port),
        ]
        if model_cfg.asr_quantization:
            cmd.extend(["--quantization", model_cfg.asr_quantization])
        return cmd

    def _infer_asr_model_type(self, model_name: str, model_path: Path) -> str:
        """Infer the onnx-asr model type from download metadata or the model name."""
        download = load_download_info(model_path)
        candidates = [model_name]
        if download is not None:
            candidates.insert(0, download.repo_id)
        for ref in candidates:
            inferred = infer_asr_model_type(ref)
            if inferred:
                return inferred
        raise RuntimeError(
            f"Cannot determine the ASR model type for '{model_name}'. "
            f"Set it explicitly with: drove models config '{model_name}' asr_model <type> "
            "(e.g. nemo-parakeet-tdt-0.6b-v3)"
        )

    # ── Prompt cache persistence ──────────────────────────────────────────────
    #
    # llama-server keeps a prompt (KV) cache in RAM, which dies with the
    # process — and drove stops that process on every idle timeout. With
    # ``prompt_cache`` enabled, each slot's KV cache is written to disk before
    # the process is stopped and restored right after the next one is healthy,
    # so a woken model does not re-process a prompt it already saw. Cache files
    # older than ``prompt_cache_ttl_seconds`` are discarded before startup.
    #
    # None of this is load-bearing: every failure path degrades to "no cache".

    def _prompt_cache_dir_for(self, model_name: str) -> Path:
        """Return the directory holding a model's saved slot caches."""
        return self._config.prompt_cache_dir / _prompt_cache_slug(model_name)

    def _prepare_prompt_cache_dir(self, model_name: str) -> Path | None:
        """Create the model's slot-cache directory and drop expired files.

        Returns None when the prompt cache is disabled or the directory cannot
        be created, in which case ``--slot-save-path`` is not passed at all.
        """
        if not self._config.prompt_cache:
            return None
        cache_dir = self._prompt_cache_dir_for(model_name)
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("Prompt cache disabled for model=%s: %s", model_name, e)
            return None
        self._prune_expired(cache_dir)
        return cache_dir

    def _prune_expired(self, cache_dir: Path) -> None:
        """Delete slot cache files older than the configured TTL (0 = never)."""
        ttl = self._config.prompt_cache_ttl_seconds
        if ttl <= 0:
            return
        cutoff = time.time() - ttl
        for path in cache_dir.glob(_PROMPT_CACHE_GLOB):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    logger.info("Discarded expired prompt cache %s", path)
            except OSError:
                continue

    def prune_prompt_cache(self) -> None:
        """Drop expired slot caches for every model. Safe to call at any time."""
        if not self._config.prompt_cache:
            return
        root = self._config.prompt_cache_dir
        if not root.is_dir():
            return
        for cache_dir in root.iterdir():
            if cache_dir.is_dir():
                self._prune_expired(cache_dir)

    async def _detect_sliding_window(self, model_path: Path) -> bool | None:
        """Whether this GGUF uses sliding-window attention (None = could not tell).

        Reading the header walks the metadata block — cheap for a model that
        declares a window early, but a model without one is only proven negative
        after the tokenizer vocabulary, so this runs off the event loop and the
        answer is cached per file revision.
        """
        try:
            key = (str(model_path), model_path.stat().st_mtime)
        except OSError:
            return None
        if key not in self._swa_cache:
            self._swa_cache[key] = await asyncio.to_thread(
                uses_sliding_window_attention, model_path
            )
            if self._swa_cache[key] is None:
                logger.info(
                    "Could not read GGUF metadata for %s; not enabling --swa-full, "
                    "which may leave the prompt cache unusable if this model uses SWA",
                    model_path.name,
                )
        return self._swa_cache[key]

    async def _save_prompt_cache(self, inst: _ModelInstance) -> None:
        """Write each idle slot's KV cache to disk before the process is stopped."""
        if not self._config.prompt_cache or inst.backend != BACKEND_LLAMA:
            return
        base = f"http://{self._config.llama_server_host}:{inst.port}"
        try:
            async with httpx.AsyncClient(
                timeout=self._config.prompt_cache_timeout_seconds
            ) as client:
                resp = await client.get(f"{base}/slots")
                resp.raise_for_status()
                slots = resp.json()
                if not isinstance(slots, list):
                    return
                for slot in slots:
                    if isinstance(slot, dict):
                        await self._save_slot(client, base, inst.model_name, slot)
        except (httpx.HTTPError, OSError, ValueError) as e:
            logger.warning("Could not save prompt cache for model=%s: %s", inst.model_name, e)

    async def _save_slot(
        self,
        client: httpx.AsyncClient,
        base: str,
        model_name: str,
        slot: dict[str, object],
    ) -> None:
        """Save one slot's KV cache, dropping the file when the slot was empty."""
        slot_id = slot.get("id")
        if not isinstance(slot_id, int) or slot.get("is_processing"):
            return
        filename = f"slot-{slot_id}.bin"
        resp = await client.post(
            f"{base}/slots/{slot_id}",
            params={"action": "save"},
            json={"filename": filename},
        )
        if resp.status_code != 200:
            logger.warning(
                "Prompt cache save failed for model=%s slot=%d: HTTP %d",
                model_name,
                slot_id,
                resp.status_code,
            )
            return
        body = resp.json()
        n_saved = body.get("n_saved", 0) if isinstance(body, dict) else 0
        if not n_saved:
            # Empty slot: drop the file so the next start skips a pointless restore.
            (self._prompt_cache_dir_for(model_name) / filename).unlink(missing_ok=True)
            return
        logger.info(
            "Saved prompt cache for model=%s (slot=%d, %s tokens)", model_name, slot_id, n_saved
        )

    async def _restore_prompt_cache(self, inst: _ModelInstance, cache_dir: Path) -> None:
        """Load previously saved slot caches into a freshly started server."""
        files = sorted(cache_dir.glob(_PROMPT_CACHE_GLOB))
        if not files:
            return
        base = f"http://{self._config.llama_server_host}:{inst.port}"
        try:
            async with httpx.AsyncClient(
                timeout=self._config.prompt_cache_timeout_seconds
            ) as client:
                for path in files:
                    match = _PROMPT_CACHE_FILE_RE.match(path.name)
                    if match is None:
                        continue
                    await self._restore_slot(client, base, inst.model_name, path, int(match[1]))
        except (httpx.HTTPError, OSError, ValueError) as e:
            logger.warning("Could not restore prompt cache for model=%s: %s", inst.model_name, e)

    async def _restore_slot(
        self,
        client: httpx.AsyncClient,
        base: str,
        model_name: str,
        path: Path,
        slot_id: int,
    ) -> None:
        """Restore one slot cache, deleting the file when the server rejects it.

        A rejection means the file no longer matches the server (context size,
        KV cache type or slot count changed), so it is dead weight from here on.
        """
        resp = await client.post(
            f"{base}/slots/{slot_id}",
            params={"action": "restore"},
            json={"filename": path.name},
        )
        if resp.status_code != 200:
            logger.info(
                "Discarding unusable prompt cache %s (HTTP %d)", path.name, resp.status_code
            )
            path.unlink(missing_ok=True)
            return
        body = resp.json()
        n_restored = body.get("n_restored", 0) if isinstance(body, dict) else 0
        logger.info(
            "Restored prompt cache for model=%s (slot=%d, %s tokens)",
            model_name,
            slot_id,
            n_restored,
        )

    def _resolve_model(self, model_name: str) -> Path:
        return ModelStore(self._config.models_dir).resolve(model_name)

    def _start_idle_watcher(self, model_name: str) -> None:
        self._idle_tasks[model_name] = asyncio.create_task(self._idle_watcher(model_name))

    async def _idle_watcher(self, model_name: str) -> None:
        while True:
            await asyncio.sleep(30)  # check every 30 seconds
            inst = self._instances.get(model_name)
            if inst is None or not inst.is_running:
                return
            if inst.active_requests > 0:
                continue  # never shut down while requests are in-flight
            # Detect config changes even when idle (no incoming requests)
            current_mtimes = self._get_config_mtimes(inst.model_path)
            if current_mtimes != inst.config_mtimes:
                inst.needs_restart = True
            # Stop immediately when a config change was detected
            if inst.needs_restart:
                logger.info(
                    "Stopping model=%s to apply config changes (will restart on next request)",
                    model_name,
                )
                async with self._lock:
                    await self._stop_instance(model_name)
                return
            idle = time.monotonic() - inst.last_request_time
            if idle >= self._config.idle_timeout_seconds:
                logger.info("Idle timeout reached for model=%s (%.0fs), stopping", model_name, idle)
                async with self._lock:
                    await self._stop_instance(model_name)
                return


def _onnx_asr_available() -> bool:
    return importlib.util.find_spec("onnx_asr") is not None


def _find_free_port() -> int:
    """Bind to port 0 to let the OS assign an available port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        port: int = s.getsockname()[1]
        return port


def _tail(text: str, n: int) -> str:
    """Return the last *n* lines of *text*."""
    lines = text.splitlines()
    return "\n".join(lines[-n:])
