# Configuration

`drove` reads global settings from:

- `~/.config/drove/config.toml`
- or a custom path set with `DROVE_CONFIG`

Environment variables with the `DROVE_` prefix override file values.

## Example config

```toml
models_dir = "~/.local/share/drove/models"
listen_host = "0.0.0.0"
listen_port = 8080
llama_server_bin = "llama-server"
idle_timeout_seconds = 1800
max_loaded_models = 1
max_memory = "24GB"
prompt_cache = true
prompt_cache_ttl_seconds = 3600

[llama_server]
n_gpu_layers = -1
```

## Model eviction

Two independent limits control when loaded models are stopped to make room
for a newly requested one (in both cases the least-recently-used idle model
is evicted first; models with in-flight requests are drained before being
stopped):

- `max_loaded_models` — how many models may be loaded at once (`0` = unlimited, default `1`).
- `max_memory` — combined memory budget for all loaded models (`"0"` = unlimited, the default).
  Accepts decimal (`"24GB"`, `"512MB"`) and binary (`"16GiB"`) units, or a plain
  number of bytes.

The memory used by a model is estimated from its on-disk file size (all shards
for sharded GGUF models, all `.onnx` files for speech-to-text models). Context
(KV cache) and runtime overhead are not counted, so leave some headroom below
your real RAM/VRAM limit. A model whose estimate alone exceeds `max_memory` is
still started (after evicting everything else) rather than refused.

## Prompt cache

llama-server keeps a prompt (KV) cache in RAM so a follow-up request only
processes the tokens that changed. That cache dies with the process — and drove
stops the process on every idle timeout, so the first request after a model
wakes up re-processes the whole prompt.

drove carries it across sleep/wake: each slot's KV cache is written to disk
before a model is stopped and restored as soon as the model is healthy again.
This is on by default — set `prompt_cache = false` to turn it off.

```toml
prompt_cache = true                  # default: true
prompt_cache_dir = "~/.local/share/drove/prompt-cache"
prompt_cache_ttl_seconds = 3600      # discard cache files older than this; 0 = never expire
prompt_cache_timeout_seconds = 60    # max wait for a single save or restore
```

Sliding-window models (Gemma and friends) only keep the attention window, so
their saved cache is unusable unless the full SWA cache is kept. drove reads the
GGUF header at startup and passes `--swa-full` only for models that actually
declare a sliding window; everything else is started unchanged. Setting
`swa_full` in a model's config overrides the decision either way.

For those models the flag is the one real cost of the prompt cache: a full-size
SWA cache needs more memory than a windowed one (measured on `gemma-4-E4B` at
32k context: 5.8 GB → 7.0 GB resident). Set `swa_full = false` on a model to
keep the smaller cache and give up the reuse. What it buys, measured on the same
model at 2048 context with a 20-token prompt:

| | first request | after idle shutdown + wake |
|---|---|---|
| prompt cache off | 20 tokens processed | 20 tokens processed |
| prompt cache on | 20 tokens processed | 1 processed, 19 from cache |

Notes:

- Cache files are large — roughly the KV size of the cached prompt (tens of MB
  for a short conversation, hundreds for a long one), one file per busy slot per
  model. The TTL is what keeps the directory bounded; expired files are deleted
  when drove starts and before each model start.
- Restoring only helps when the next prompt shares a prefix with the cached one.
- A cache file the server refuses — because `ctx_size`, `cache_type_k/v` or the
  slot count changed since it was written — is deleted rather than retried.
- The cache is an optimization: every failure to save or restore is logged and
  otherwise ignored, and speech-to-text models skip it entirely.

## Per-model config

Each model can have a sidecar config file in the models directory:

- model file: `~/.local/share/drove/models/<name>.gguf`
- config file: `~/.local/share/drove/models/<name>.toml`

Example:

```toml
ctx_size = 4096
n_gpu_layers = -1
```

Only keys declared in `ModelConfig` are accepted; unknown keys are silently ignored.

Supported keys:

- Context and memory: `ctx_size`, `n_gpu_layers`, `main_gpu`, `tensor_split`, `load_mode`
- Batching: `batch_size`, `ubatch_size`, `n_parallel` (→ `--parallel`)
- Sampling defaults: `temp`, `top_p`, `top_k`
- Performance: `threads`, `threads_batch`, `flash_attn`
- Prompt cache: `cache_prompt`, `cache_reuse`, `cache_ram`, `ctx_checkpoints`,
  `context_shift`, `swa_full`
- KV quantization: `cache_type_k`, `cache_type_v`
- Rope scaling: `rope_freq_base`, `rope_freq_scale`
- Multimodal: `mmproj`
- Escape hatch: `extra_args`

### Speeding up prompt processing

llama-server caches prompts by default (`cache_prompt`) and keeps up to
`cache_ram` MiB of them in memory. The flag worth adding by hand is
`cache_reuse`: it lets the server reuse cached blocks even when the prompt
changed in the middle, which is the common case for agents and chat clients that
rewrite earlier turns.

```toml
cache_reuse = 256    # min chunk size to reuse via KV shifting; 0 (default) = off
cache_ram = 16384    # MiB of prompt cache to keep in RAM (-1 = no limit, 0 = off)
```

### Raw llama-server arguments

`extra_args` is passed through verbatim, after every argument drove generated —
llama-server takes the last occurrence of a flag, so these win:

```toml
extra_args = ["--no-webui", "--lora", "/models/adapter.gguf"]
```

From the CLI the value is split shell-style:

```bash
drove models config mymodel extra_args "--lora /models/adapter.gguf"
```

Drove-specific keys (never passed to llama-server): `backend` (`llama` or `asr`),
`asr_model`, `asr_quantization` — see [Speech-to-text](./speech-to-text.md).
