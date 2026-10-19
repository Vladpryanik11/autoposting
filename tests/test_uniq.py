"""Контрактные тесты QA уникализации без запуска FFmpeg."""
import json
import subprocess

import pytest

from app import uniq


def test_profile_has_consistent_audio_speed():
    profile = uniq.build_profile(__import__("random").Random(42), "medium")
    transforms = {entry["id"]: entry for entry in profile["transforms"]}
    assert transforms["audio.pitch_tempo"]["params"]["tempo"] == transforms["video.speed"]["params"]["rate"]
    assert profile["transforms"][-1]["id"] == "audio.loudnorm"


def test_qa_requires_valid_number(tmp_path, monkeypatch):
    src, dst = tmp_path / "src.mp4", tmp_path / "out.mp4"
    src.write_bytes(b"video")

    def fake_run(*args):
        dst.write_bytes(b"rendered")
        (tmp_path / "out.mp4.qa.json").write_text(json.dumps({"phash_similarity": None}))

    monkeypatch.setattr(uniq, "_yt_uniq", fake_run)
    result = uniq.uniqualize(str(src), str(dst))
    assert result["similarity"] is None
    with pytest.raises(RuntimeError, match="Не пройден QA"):
        uniq.uniqualize_checked(str(src), str(dst), 0.9)


def test_retries_then_fails_closed(monkeypatch):
    values = iter([0.98, 0.93])
    seen = []

    def fake(src, dst, intensity, **kwargs):
        seen.append(intensity)
        return {"profile": {}, "similarity": next(values)}

    monkeypatch.setattr(uniq, "uniqualize", fake)
    with pytest.raises(RuntimeError, match="Не пройден QA"):
        uniq.uniqualize_checked("src", "dst", 0.9, "medium")
    assert seen == ["medium", "strong"]


def test_passes_after_stronger_profile(monkeypatch):
    values = iter([0.96, 0.86])
    monkeypatch.setattr(uniq, "uniqualize",
                        lambda *args, **kwargs: {"profile": {}, "similarity": next(values)})
    assert uniq.uniqualize_checked("src", "dst", 0.9)["intensity"] == "strong"


def test_rejects_nan_and_bad_ranges(tmp_path, monkeypatch):
    src, dst = tmp_path / "src.mp4", tmp_path / "out.mp4"
    src.write_bytes(b"video")
    monkeypatch.setattr(uniq, "_yt_uniq", lambda *args: (
        dst.write_bytes(b"rendered"),
        (tmp_path / "out.mp4.qa.json").write_text('{"phash_similarity": NaN}')))
    with pytest.raises(RuntimeError, match="вне диапазона"):
        uniq.uniqualize(str(src), str(dst))
    with pytest.raises(ValueError, match="диапазоне"):
        uniq.uniqualize_checked(str(src), str(dst), 2.0)


def test_encoder_process_has_timeout(monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(uniq.subprocess, "run", fake_run)
    uniq._yt_uniq("--help")
    assert captured["timeout"] == 900
