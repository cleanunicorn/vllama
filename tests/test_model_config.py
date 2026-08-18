"""Tests for per-model config."""

from __future__ import annotations

from pathlib import Path

import pytest

from drove.model_config import (
    ModelConfig,
    load_model_config,
    save_model_config,
    set_model_config_key,
)


def fake_model(tmp_path: Path, name: str = "mymodel") -> Path:
    model_dir = tmp_path / name
    model_dir.mkdir(parents=True, exist_ok=True)
    p = model_dir / f"{name}.gguf"
    p.write_bytes(b"")  # empty placeholder
    return p


def test_defaults_when_no_sidecar(tmp_path: Path) -> None:
    model = fake_model(tmp_path)
    cfg = load_model_config(model)
    assert cfg.ctx_size is None
    assert cfg.n_gpu_layers is None


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    model = fake_model(tmp_path)
    cfg = ModelConfig(ctx_size=4096, n_gpu_layers=32, flash_attn="on")
    save_model_config(model, cfg)

    loaded = load_model_config(model)
    assert loaded.ctx_size == 4096
    assert loaded.n_gpu_layers == 32
    assert loaded.flash_attn == "on"


def test_to_llama_args(tmp_path: Path) -> None:
    cfg = ModelConfig(ctx_size=8192, n_gpu_layers=-1, flash_attn="on", temp=0.7)
    args = cfg.to_llama_args()
    assert "--ctx-size" in args
    assert "8192" in args
    assert "--flash-attn" in args
    assert "on" in args
    assert "--temp" in args
    assert "0.7" in args


def test_set_model_config_key_int(tmp_path: Path) -> None:
    model = fake_model(tmp_path)
    updated = set_model_config_key(model, "ctx_size", "8192")
    assert updated.ctx_size == 8192
    assert load_model_config(model).ctx_size == 8192


def test_set_model_config_key_bool(tmp_path: Path) -> None:
    model = fake_model(tmp_path)
    updated = set_model_config_key(model, "flash_attn", "on")
    assert updated.flash_attn == "on"


def test_set_model_config_key_invalid(tmp_path: Path) -> None:
    model = fake_model(tmp_path)
    with pytest.raises(ValueError, match="Unknown config key"):
        set_model_config_key(model, "nonexistent_key", "value")


def test_drove_only_fields_excluded_from_llama_args(tmp_path: Path) -> None:
    cfg = ModelConfig(
        backend="asr",
        asr_model="nemo-parakeet-tdt-0.6b-v3",
        asr_quantization="int8",
        ctx_size=4096,
    )
    args = cfg.to_llama_args()
    assert "--backend" not in args
    assert "--asr-model" not in args
    assert "--asr-quantization" not in args
    assert "--ctx-size" in args


def test_set_model_config_key_accepts_asr_fields(tmp_path: Path) -> None:
    model = fake_model(tmp_path)
    updated = set_model_config_key(model, "asr_model", "nemo-parakeet-tdt-0.6b-v3")
    assert updated.asr_model == "nemo-parakeet-tdt-0.6b-v3"
    assert load_model_config(model).asr_model == "nemo-parakeet-tdt-0.6b-v3"


def test_set_model_config_key_rejects_malformed_asr_model(tmp_path: Path) -> None:
    model = fake_model(tmp_path)
    with pytest.raises(ValueError, match="Invalid value for 'asr_model'"):
        set_model_config_key(model, "asr_model", "bad value; rm -rf /")
    assert load_model_config(model).asr_model is None


def test_set_model_config_key_rejects_malformed_asr_quantization(tmp_path: Path) -> None:
    model = fake_model(tmp_path)
    with pytest.raises(ValueError, match="Invalid value for 'asr_quantization'"):
        set_model_config_key(model, "asr_quantization", "int8 --extra-flag")
    assert load_model_config(model).asr_quantization is None


def test_set_model_config_key_accepts_valid_asr_quantization(tmp_path: Path) -> None:
    model = fake_model(tmp_path)
    updated = set_model_config_key(model, "asr_quantization", "int8")
    assert updated.asr_quantization == "int8"


def test_set_model_config_key_rejects_unknown_backend(tmp_path: Path) -> None:
    model = fake_model(tmp_path)
    with pytest.raises(ValueError, match="Unknown backend"):
        set_model_config_key(model, "backend", "whisper")
    assert load_model_config(model).backend is None


def test_set_model_config_key_accepts_valid_backend(tmp_path: Path) -> None:
    model = fake_model(tmp_path)
    updated = set_model_config_key(model, "backend", "asr")
    assert updated.backend == "asr"


# ── llama-server argument mapping ─────────────────────────────────────────────


def test_prompt_cache_flags_map_to_llama_args() -> None:
    args = ModelConfig(cache_reuse=256, cache_ram=16384, ctx_checkpoints=8).to_llama_args()
    assert "--cache-reuse" in args
    assert args[args.index("--cache-reuse") + 1] == "256"
    assert args[args.index("--cache-ram") + 1] == "16384"
    assert args[args.index("--ctx-checkpoints") + 1] == "8"


def test_negatable_bools_emit_the_no_variant_when_false() -> None:
    args = ModelConfig(cache_prompt=False, context_shift=False).to_llama_args()
    assert "--no-cache-prompt" in args
    assert "--no-context-shift" in args
    assert "--cache-prompt" not in args


def test_negatable_bools_emit_the_plain_flag_when_true() -> None:
    args = ModelConfig(cache_prompt=True, context_shift=True).to_llama_args()
    assert args.count("--cache-prompt") == 1
    assert args.count("--context-shift") == 1
    assert not any(a.startswith("--no-") for a in args)


def test_plain_bool_is_omitted_when_false() -> None:
    assert ModelConfig(swa_full=False).to_llama_args() == []
    assert ModelConfig(swa_full=True).to_llama_args() == ["--swa-full"]


def test_n_parallel_uses_the_parallel_flag() -> None:
    """llama-server rejects --n-parallel outright, so the key must be remapped."""
    args = ModelConfig(n_parallel=4).to_llama_args()
    assert args == ["--parallel", "4"]


def test_extra_args_are_appended_last() -> None:
    args = ModelConfig(ctx_size=4096, extra_args=["--cache-reuse", "512"]).to_llama_args()
    assert args[:2] == ["--ctx-size", "4096"]
    assert args[-2:] == ["--cache-reuse", "512"]


def test_extra_args_is_never_emitted_as_a_flag() -> None:
    assert "--extra-args" not in ModelConfig(extra_args=["--verbose"]).to_llama_args()


def test_set_extra_args_splits_shell_style(tmp_path: Path) -> None:
    model = fake_model(tmp_path)
    cfg = set_model_config_key(model, "extra_args", "--lora /models/a.gguf")
    assert cfg.extra_args == ["--lora", "/models/a.gguf"]
    assert load_model_config(model).extra_args == ["--lora", "/models/a.gguf"]


def test_set_int_and_bool_cache_keys(tmp_path: Path) -> None:
    model = fake_model(tmp_path)
    assert set_model_config_key(model, "cache_reuse", "256").cache_reuse == 256
    assert set_model_config_key(model, "cache_prompt", "false").cache_prompt is False
