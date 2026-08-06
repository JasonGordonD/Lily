"""WS-16 (WO-LILY-OMNIBUS-003, AMENDMENT-002) — pre-STT dereverb node.

Contract under test:
  - flag off (the default) => no processor, no heavy imports on the boot path
  - wpe mode => 1:1 frame geometry (sample count, rate, channels) so stream
    timestamps are untouched; only a constant in-stream delay is added
  - any failure => passthrough, never a dead STT stream
"""

import asyncio
import importlib
import math
import struct
import sys

import pytest

import lily_config
import lily_dereverb


class _Frame:
    def __init__(self, data, sample_rate, num_channels, samples_per_channel):
        self.data = data
        self.sample_rate = sample_rate
        self.num_channels = num_channels
        self.samples_per_channel = samples_per_channel


def _tone_frame(sr=16000, n=160, freq=440.0, phase0=0):
    pcm = struct.pack(
        f"<{n}h",
        *[
            int(12000 * math.sin(2 * math.pi * freq * (phase0 + i) / sr))
            for i in range(n)
        ],
    )
    return _Frame(pcm, sr, 1, n)


# --- flag / factory ---------------------------------------------------------

def test_mode_default_off(monkeypatch):
    monkeypatch.delenv("LILY_DEREVERB_NODE", raising=False)
    assert lily_config.dereverb_node_mode() == "off"


def test_mode_unknown_resolves_off(monkeypatch):
    monkeypatch.setenv("LILY_DEREVERB_NODE", "quail-supreme")
    assert lily_config.dereverb_node_mode() == "off"


def test_mode_values(monkeypatch):
    for v, want in (("wpe", "wpe"), ("AIC", "aic"), ("off", "off")):
        monkeypatch.setenv("LILY_DEREVERB_NODE", v)
        assert lily_config.dereverb_node_mode() == want


def test_factory_off_returns_none(monkeypatch):
    monkeypatch.delenv("LILY_DEREVERB_NODE", raising=False)
    assert lily_dereverb.lily_create_dereverb_processor() is None


def test_boot_path_imports_no_dereverb_deps():
    # The module import itself (what lily_agent pulls at boot) must not
    # drag in the dereverb stacks — they load lazily inside mode!=off.
    importlib.reload(lily_dereverb)
    assert "nara_wpe" not in sys.modules
    assert "livekit.plugins.ai_coustics" not in sys.modules


# --- wpe processor ----------------------------------------------------------

def test_wpe_preserves_frame_geometry():
    pytest.importorskip("nara_wpe")
    proc = lily_dereverb._WpeFrameProcessor()
    total_in = total_out = 0
    for i in range(50):  # 500 ms
        f = _tone_frame(phase0=i * 160)
        out = proc.process(f)
        assert out.sample_rate == f.sample_rate
        assert out.num_channels == f.num_channels
        assert out.samples_per_channel == f.samples_per_channel
        assert len(out.data) == len(f.data)
        total_in += f.samples_per_channel
        total_out += out.samples_per_channel
    assert total_in == total_out
    assert not proc._broken


def test_wpe_output_is_processed_not_identity():
    pytest.importorskip("nara_wpe")
    proc = lily_dereverb._WpeFrameProcessor()
    outs, ins = [], []
    for i in range(50):
        f = _tone_frame(phase0=i * 160)
        ins.append(f.data)
        outs.append(proc.process(f).data)
    # constant one-window in-stream delay => streams differ
    assert b"".join(outs) != b"".join(ins)
    # ...but the signal is not silence after the priming window
    assert any(b != 0 for b in b"".join(outs[10:]))


def test_wpe_multichannel_passthrough():
    pytest.importorskip("nara_wpe")
    proc = lily_dereverb._WpeFrameProcessor()
    f = _Frame(b"\x00\x00" * 320, 16000, 2, 160)
    assert proc.process(f) is f


def test_wpe_failure_degrades_to_passthrough(monkeypatch):
    proc = lily_dereverb._WpeFrameProcessor()

    def _boom(*a, **k):
        raise RuntimeError("no wpe here")

    monkeypatch.setattr(lily_dereverb, "LilyWpeDereverb", _boom)
    f = _tone_frame()
    assert proc.process(f) is f
    assert proc._broken
    # subsequent frames stay passthrough without retrying the import
    assert proc.process(f) is f


def test_wpe_latency_reported():
    pytest.importorskip("nara_wpe")
    wpe = lily_dereverb.LilyWpeDereverb(sample_rate=16000)
    assert 0.02 < wpe.latency_seconds() < 0.05


# --- aic processor ----------------------------------------------------------

def test_aic_init_failure_is_passthrough(monkeypatch):
    proc = lily_dereverb._AicFrameProcessor()
    monkeypatch.setattr(
        lily_dereverb._AicFrameProcessor,
        "_ensure",
        lambda self: False,
    )
    f = _tone_frame()
    assert proc.process(f) is f


# --- stream wrapper ---------------------------------------------------------

def test_dereverb_frames_wraps_one_to_one():
    pytest.importorskip("nara_wpe")

    async def _run():
        frames = [_tone_frame(phase0=i * 160) for i in range(10)]

        async def _gen():
            for f in frames:
                yield f

        proc = lily_dereverb._WpeFrameProcessor()
        out = [f async for f in lily_dereverb.lily_dereverb_frames(_gen(), proc)]
        assert len(out) == len(frames)
        assert all(
            o.samples_per_channel == f.samples_per_channel
            for o, f in zip(out, frames)
        )

    asyncio.run(_run())
