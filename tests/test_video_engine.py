"""Contract tests for the only active video engine adapter."""
import json
from pathlib import Path

import pytest

from app import video_engine as v


def test_quality_requires_both_metrics():
    with pytest.raises(v.RenderQualityError, match="unavailable"):
        v._quality_check({"ssim_mean": 0.99}, 75, 0.8)
    with pytest.raises(v.RenderQualityError, match="below minimum"):
        v._quality_check({"vmaf_mean": 60, "ssim_mean": 0.99}, 75, 0.8)
    assert v._quality_check({"vmaf_mean": 91, "ssim_mean": 0.96}, 75, 0.8) == (91.0, 0.96)


def test_structure_detects_missing_audio(monkeypatch, tmp_path):
    video = {"codec_type": "video", "width": 1080, "height": 1920}
    audio = {"codec_type": "audio"}
    probes = iter([
        {"streams": [video, audio], "format": {"duration": "3.0"}},
        {"streams": [video], "format": {"duration": "3.0"}},
    ])
    monkeypatch.setattr(v, "_probe", lambda _: next(probes))
    with pytest.raises(v.RenderQualityError, match="audio"):
        v._validate_structure(tmp_path / "src.mp4", tmp_path / "result.mp4")


def test_structure_detects_lost_subtitles(monkeypatch, tmp_path):
    video = {"codec_type": "video", "width": 1080, "height": 1920}
    subtitles = {"codec_type": "subtitle"}
    probes = iter([
        {"streams": [video, subtitles], "format": {"duration": "3"}},
        {"streams": [video], "format": {"duration": "3"}},
    ])
    monkeypatch.setattr(v, "_probe", lambda _: next(probes))
    with pytest.raises(v.RenderQualityError, match="subtitle"):
        v._validate_structure(tmp_path / "src.mp4", tmp_path / "result.mp4")


def test_render_calls_one_upstream_binary_and_qa(monkeypatch, tmp_path):
    src = tmp_path / "source.mp4"
    src.write_bytes(b"source")
    dst = tmp_path / "output.mp4"
    commands = []

    def upstream(args, timeout):
        commands.append(args)
        if args[1] == "run":
            Path(args[args.index("--out") + 1]).write_bytes(b"encoded")
        if args[1] == "qa":
            Path(args[args.index("--json") + 1]).write_text(
                json.dumps({"vmaf_mean": 91.3, "ssim_mean": 0.97,
                            "phash_similarity": 0.98, "duration_match": True,
                            "correctness": {"status": "passed", "full_decode_status": "passed"}}), encoding="utf-8")

    monkeypatch.setattr(v, "_run", upstream)
    monkeypatch.setattr(v, "_validate_structure", lambda *a: {"width": 1080, "height": 1920, "audio_streams": 0})
    monkeypatch.setattr(v, "_decode_full", lambda *a: None)
    result = v.render(src, dst)
    assert [x[1] for x in commands] == ["run", "qa"]
    assert dst.read_bytes() == b"encoded"
    assert result["vmaf"] == 91.3
    assert (tmp_path / "output.mp4.quality.json").exists()


def test_render_fails_closed_on_bad_metrics(monkeypatch, tmp_path):
    src = tmp_path / "source.mp4"
    src.write_bytes(b"source")
    dst = tmp_path / "output.mp4"
    def upstream(args, timeout):
        if args[1] == "run":
            Path(args[args.index("--out") + 1]).write_bytes(b"encoded")
        if args[1] == "qa":
            Path(args[args.index("--json") + 1]).write_text(
                json.dumps({"vmaf_mean": None, "ssim_mean": 0.96}), encoding="utf-8")
    monkeypatch.setattr(v, "_run", upstream)
    monkeypatch.setattr(v, "_validate_structure", lambda *a: {"audio_streams": 0})
    monkeypatch.setattr(v, "_decode_full", lambda *a: None)
    with pytest.raises(v.RenderQualityError, match="unavailable"):
        v.render(src, dst)
    assert not dst.exists()


def test_upstream_correctness_rejected():
    with pytest.raises(v.RenderQualityError, match="correctness"):
        v._check_report_contract({"correctness": {"status": "failed"}}, False)


def test_audio_quality_rejected():
    report = {"duration_match": True,
              "correctness": {"status": "passed", "full_decode_status": "passed"},
              "loudness": {"status": "passed", "streams": [
                  {"status": "passed", "integrated_lufs": -12.0, "true_peak_dbtp": 0.0}]}}
    with pytest.raises(v.RenderQualityError, match="audio out of range"):
        v._check_report_contract(report, True)
