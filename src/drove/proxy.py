"""FastAPI reverse proxy to llama-server."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from drove.config import Config, load_config
from drove.observe import ObserveContext, save_record
from drove.server_manager import ServerManager
from drove.stats import ProxyStats

logger = logging.getLogger(__name__)

# Poll interval for config file change detection
_CONFIG_POLL_SECONDS = 5

# Settings that cannot be changed without restarting the proxy process
_RESTART_REQUIRED = {"listen_host", "listen_port"}

# Strong references to fire-and-forget tasks so they are not garbage-collected.
_background_tasks: set[asyncio.Task[None]] = set()

# Header names to strip when forwarding (hop-by-hop)
_HOP_BY_HOP = frozenset(
    [
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
    ]
)


def create_app(config: Config, config_path: Path | None = None) -> FastAPI:
    manager = ServerManager(config)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        tasks: list[asyncio.Task[None]] = []

        if config_path is not None:
            tasks.append(
                asyncio.create_task(
                    _config_watcher(_app, config_path),
                    name="config-watcher",
                )
            )
            _setup_sighup(_app, config_path)

        # Drop prompt caches that outlived their TTL while drove was not running.
        manager.prune_prompt_cache()

        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            await manager.stop()
            await client.aclose()

    stats = ProxyStats()

    app = FastAPI(title="drove", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.manager = manager
    app.state.config = config
    app.state.stats = stats

    client = httpx.AsyncClient(timeout=300.0)
    app.state.client = client  # kept here so lifespan can close it

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "models": manager.loaded_models,
                "server_running": manager.is_running,
            }
        )

    @app.get("/status")
    async def status() -> JSONResponse:
        now = time.time()

        models_info = manager.get_all_model_info()

        return JSONResponse(
            {
                "server": {
                    "uptime_seconds": round(now - stats.started_at, 1),
                    "listen": f"{config.listen_host}:{config.listen_port}",
                },
                "models": models_info,
                "process": manager.get_process_stats(),
                "requests": {
                    "total": stats.request_count,
                    "active": stats.active_requests,
                    "errors": stats.error_count,
                },
                "tokens": {
                    "prompt": stats.tokens_prompt,
                    "completion": stats.tokens_completion,
                    "total": stats.tokens_prompt + stats.tokens_completion,
                    "speed": {
                        "last_tok_per_sec": (
                            round(stats.last_tokens_per_second, 1)
                            if stats.last_tokens_per_second is not None
                            else None
                        ),
                        "avg_tok_per_sec": (
                            round(stats.avg_tokens_per_second, 1)
                            if stats.avg_tokens_per_second is not None
                            else None
                        ),
                    },
                    "ttft": {
                        "last_seconds": (
                            round(stats.last_ttft, 3) if stats.last_ttft is not None else None
                        ),
                        "avg_seconds": (
                            round(stats.avg_ttft, 3) if stats.avg_ttft is not None else None
                        ),
                    },
                },
            }
        )

    @app.get("/v1/models")
    async def list_models() -> JSONResponse:
        """OpenAI-compatible model listing."""
        from drove.cli.models import _iter_models

        models = _iter_models(config.models_dir)
        model_objects = []
        for name, _path, _size in models:
            model_objects.append(
                {
                    "id": name,
                    "object": "model",
                    "created": 0,
                    "owned_by": "local",
                }
            )
        return JSONResponse({"object": "list", "data": model_objects})

    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
    )
    async def proxy(request: Request, path: str) -> StreamingResponse:
        stats.request_started()
        model_name = await _extract_model(request)

        if model_name is None:
            if not manager.is_running:
                stats.request_finished()
                stats.request_error()
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "No model is loaded. Specify a 'model' field in your request "
                        "or load a model first."
                    ),
                )
            # Use the first loaded model as fallback for the upstream URL
            resolved_model = manager.current_model
            # is_running was just checked above, so a loaded model must exist
            assert resolved_model is not None
        else:
            resolved_model = model_name
            try:
                # claim=True atomically reserves a request slot before releasing
                # the manager lock, so a concurrent ensure_running for another
                # model cannot evict this one while we're mid-request.
                await manager.ensure_running(model_name, claim=True)
            except FileNotFoundError as e:
                stats.request_finished()
                stats.request_error()
                raise HTTPException(status_code=404, detail=str(e)) from e
            except (TimeoutError, RuntimeError) as e:
                stats.request_finished()
                stats.request_error()
                raise HTTPException(status_code=503, detail=str(e)) from e

        # For the "no model_name in body" fallback path, we still need to
        # claim a slot — but without the lock since ensure_running was skipped.
        # This path is less racy because nothing competes to evict a model
        # that was picked as the current fallback.
        if model_name is None:
            manager.request_started(resolved_model)
        try:
            manager.record_request(resolved_model)

            headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
            body = await request.body()

            # Build observe context if observation is enabled
            current_config: Config = app.state.config
            obs_ctx: ObserveContext | None = None
            if current_config.observe:
                from datetime import datetime

                obs_ctx = ObserveContext(
                    timestamp=datetime.now().isoformat(timespec="seconds"),
                    model=resolved_model,
                    endpoint=path,
                    method=request.method,
                    request_headers=dict(headers),
                    request_body=body,
                )

            try:
                upstream = client.build_request(
                    method=request.method,
                    url=f"{manager.base_url_for(resolved_model)}/{path}",
                    params=request.query_params,
                    headers=headers,
                    content=body,
                )
                resp = await client.send(upstream, stream=True)
            except httpx.TransportError as e:
                # str(e) is empty for some httpx errors (e.g. ReadError);
                # include the exception class name for diagnostics.
                detail = f"Upstream error: {type(e).__name__}: {e}".rstrip(": ")
                raise HTTPException(status_code=502, detail=detail) from e

            response_headers = {
                k: v for k, v in resp.headers.items() if k.lower() not in _HOP_BY_HOP
            }

            if obs_ctx is not None:
                obs_ctx.response_status = resp.status_code
                obs_ctx.response_headers = dict(response_headers)

            return StreamingResponse(
                content=_counting_stream(
                    resp.aiter_raw(),
                    stats,
                    manager,
                    resolved_model,
                    obs_ctx,
                    current_config.observe_dir,
                ),
                status_code=resp.status_code,
                headers=response_headers,
                media_type=resp.headers.get("content-type"),
                background=None,
            )
        except Exception:
            manager.request_finished(resolved_model)
            stats.request_finished()
            stats.request_error()
            raise

    return app


async def _config_watcher(app: FastAPI, config_path: Path) -> None:
    """Background task: reload config when the file's mtime changes."""
    mtime = _mtime(config_path)
    while True:
        await asyncio.sleep(_CONFIG_POLL_SECONDS)
        new_mtime = _mtime(config_path)
        if new_mtime != mtime:
            mtime = new_mtime
            _reload_config(app, config_path)


def _reload_config(app: FastAPI, config_path: Path) -> None:
    """Load config from disk and apply hot-reloadable fields to the running app."""
    try:
        new_config = load_config(config_path)
    except Exception as e:
        logger.error("Failed to reload config: %s", e)
        return

    old_config: Config = app.state.config
    changed: list[str] = []
    skipped: list[str] = []

    new_dump = new_config.model_dump()
    old_dump = old_config.model_dump()

    for field, new_val in new_dump.items():
        if new_val == old_dump.get(field):
            continue
        if field in _RESTART_REQUIRED:
            skipped.append(f"{field} (restart required)")
        else:
            changed.append(f"{field}: {old_dump.get(field)!r} → {new_val!r}")

    if not changed and not skipped:
        return

    # Build updated config preserving non-reloadable fields from the old config
    merged = new_config.model_copy(update={f: old_dump[f] for f in _RESTART_REQUIRED})

    app.state.config = merged
    app.state.manager._config = merged

    if changed:
        logger.info("Config reloaded — changed: %s", "; ".join(changed))
    if skipped:
        logger.warning("Config changes ignored (restart required): %s", "; ".join(skipped))


def _setup_sighup(app: FastAPI, config_path: Path) -> None:
    """Reload config on SIGHUP (kill -HUP <pid>)."""
    loop = asyncio.get_event_loop()

    def _handler() -> None:
        logger.info("SIGHUP received — reloading config")
        _reload_config(app, config_path)

    try:
        loop.add_signal_handler(signal.SIGHUP, _handler)
    except OSError, NotImplementedError:
        pass  # Windows or environments without SIGHUP


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return 0.0


async def _extract_model(request: Request) -> str | None:
    """Try to read the 'model' field from the request body without consuming the stream.

    Supports JSON bodies (chat/completions) and multipart form bodies
    (OpenAI-style audio endpoints, where 'model' is a form field).
    """
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            body = await request.json()
            return body.get("model") if isinstance(body, dict) else None
        except Exception:
            return None
    if "multipart/form-data" in content_type:
        try:
            # Cache the raw body first so the multipart parser consumes the
            # cached copy and the proxy can still forward the original bytes.
            await request.body()
            form = await request.form()
            value = form.get("model")
            return value if isinstance(value, str) and value else None
        except Exception:
            return None
    return None


async def _counting_stream(
    raw_iter: AsyncIterator[bytes],
    stats: ProxyStats,
    manager: ServerManager,
    model_name: str,
    obs_ctx: ObserveContext | None = None,
    observe_dir: Path | None = None,
) -> AsyncIterator[bytes]:
    """Wrap a response stream, collecting bytes to extract token usage afterwards."""
    try:
        chunks: list[bytes] = []
        t_start = time.monotonic()
        t_first_chunk: float | None = None
        async for chunk in raw_iter:
            if t_first_chunk is None:
                t_first_chunk = time.monotonic()
            chunks.append(chunk)
            yield chunk
        t_end = time.monotonic()
        duration = t_end - t_start
        ttft = (t_first_chunk - t_start) if t_first_chunk is not None else None
        if ttft is not None:
            stats.record_ttft(ttft)
        full_body = b"".join(chunks)
        tokens = _record_usage(full_body, stats, duration)

        # Fire-and-forget observe logging
        if obs_ctx is not None and observe_dir is not None:
            obs_ctx.response_body = full_body
            obs_ctx.duration_seconds = duration
            obs_ctx.ttft_seconds = ttft
            obs_ctx.tokens_prompt = int(tokens.get("prompt") or 0)
            obs_ctx.tokens_completion = int(tokens.get("completion") or 0)
            speed = tokens.get("speed")
            obs_ctx.tokens_per_second = float(speed) if speed is not None else None
            task = asyncio.create_task(_save_observe_record(obs_ctx, observe_dir))
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)
    except Exception:
        stats.request_error()
        raise
    finally:
        manager.request_finished(model_name)
        stats.request_finished()


async def _save_observe_record(obs_ctx: ObserveContext, observe_dir: Path) -> None:
    """Write an observe record to disk (runs as a fire-and-forget task)."""
    try:
        record = obs_ctx.to_record()
        save_record(observe_dir, record)
        logger.debug("Observe record saved: %s", record.id)
    except Exception as e:
        logger.warning("Failed to save observe record: %s", e)


def _extract_tokens_from_obj(data: dict[str, object], out: dict[str, int | float | None]) -> None:
    """Extract token counts and speed from a response object (usage or timings)."""
    # OpenAI-style usage
    usage = data.get("usage")
    if isinstance(usage, dict):
        out["prompt"] = usage.get("prompt_tokens", 0) or out["prompt"]
        out["completion"] = usage.get("completion_tokens", 0) or out["completion"]

    # llama.cpp timings (always present in last streaming chunk)
    timings = data.get("timings")
    if isinstance(timings, dict):
        if not out["prompt"]:
            out["prompt"] = timings.get("prompt_n", 0)
        if not out["completion"]:
            out["completion"] = timings.get("predicted_n", 0)
        if timings.get("predicted_per_second"):
            out["speed"] = timings["predicted_per_second"]


def _record_usage(
    body: bytes, stats: ProxyStats, duration: float = 0.0
) -> dict[str, int | float | None]:
    """Best-effort extraction of token usage from a response body.

    Returns the extracted token dict with keys: prompt, completion, speed.
    """
    text = body.decode("utf-8", errors="replace")
    tokens_out: dict[str, int | float | None] = {"prompt": 0, "completion": 0, "speed": None}

    # Non-streaming: plain JSON with a top-level "usage" field
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            _extract_tokens_from_obj(data, tokens_out)
    except json.JSONDecodeError, ValueError:
        pass

    # Streaming SSE: scan backwards for the last data: line containing usage/timings
    if not tokens_out["completion"]:
        for line in reversed(text.splitlines()):
            stripped = line.strip()
            if not stripped.startswith("data: ") or stripped == "data: [DONE]":
                continue
            try:
                data = json.loads(stripped[6:])
                if not isinstance(data, dict):
                    continue
                _extract_tokens_from_obj(data, tokens_out)
                if tokens_out["completion"]:
                    break
            except json.JSONDecodeError, ValueError:
                continue

    prompt_tokens = int(tokens_out["prompt"] or 0)
    completion_tokens = int(tokens_out["completion"] or 0)
    tok_per_sec = tokens_out["speed"]

    if prompt_tokens or completion_tokens:
        stats.add_tokens(prompt_tokens, completion_tokens)

    # Record speed: prefer server-reported timing, fall back to our own measurement
    if tok_per_sec and tok_per_sec > 0:
        stats.record_completion_speed(completion_tokens, completion_tokens / tok_per_sec)
    elif completion_tokens > 0 and duration > 0:
        stats.record_completion_speed(completion_tokens, duration)

    return tokens_out
