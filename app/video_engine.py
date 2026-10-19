"""Single upstream video-uniquifier v2 adapter with fail-closed technical QA.

Source and license: Hostlife22/video-uniquifier (MIT), pinned at v2.1.2.
This module does not implement an alternative video transformation engine.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


class RenderQualityError(RuntimeError):
    pass


def _cli() -> str:
    return os.environ.get("VIDEO_UNIQ_BIN", "video-uniq")


def _run(args: list[str], timeout: int) -> None:
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RenderQualityError(f"video-uniquifier command failed: {exc}") from exc
    if proc.returncode:
        raise RenderQualityError(
            f"video-uniquifier exit {proc.returncode}: {(proc.stderr + proc.stdout)[-1800:]}"
        )


def _probe(path: Path) -> dict:
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
            capture_output=True, text=True, check=True, timeout=60,
        )
        return json.loads(proc.stdout)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise RenderQualityError(f"ffprobe failed for {path}: {exc}") from exc


def _streams(meta: dict, kind: str) -> list[dict]:
    return [s for s in meta.get("streams", []) if s.get("codec_type") == kind]


def _validate_structure(source: Path, result: Path) -> dict:
    src, out = _probe(source), _probe(result)
    sv, ov = _streams(src, "video"), _streams(out, "video")
    if not ov or not sv:
        raise RenderQualityError("missing primary video stream")
    if _streams(src, "audio") and not _streams(out, "audio"):
        raise RenderQualityError("output lost its audio stream")
    if len(_streams(src, "subtitle")) > len(_streams(out, "subtitle")):
        raise RenderQualityError("output lost a subtitle stream")
    w, h = int(ov[0].get("width") or 0), int(ov[0].get("height") or 0)
    if not w or not h or w % 2 or h % 2:
        raise RenderQualityError(f"invalid output dimensions: {w}x{h}")
    duration = float(out.get("format", {}).get("duration") or 0)
    if duration <= 0:
        raise RenderQualityError("empty output duration")
    return {"width": w, "height": h, "duration": duration,
            "audio_streams": len(_streams(out, "audio")),
            "subtitle_streams": len(_streams(out, "subtitle"))}


def _decode_full(path: Path, timeout: int) -> None:
    try:
        p = subprocess.run(
            ["ffmpeg", "-v", "error", "-xerror", "-i", str(path),
             "-map", "0:v:0", "-map", "0:a?", "-f", "null", "-"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RenderQualityError(f"full decode failed: {exc}") from exc
    if p.returncode:
        raise RenderQualityError(f"full decode error: {p.stderr[-1200:]}")


def _quality_check(report: dict, min_vmaf: float, min_ssim: float) -> tuple[float, float]:
    vmaf, ssim = report.get("vmaf_mean"), report.get("ssim_mean")
    # Metric availability is mandatory: no silent skip when libvmaf is absent.
    if vmaf is None or ssim is None:
        raise RenderQualityError("VMAF/SSIM unavailable; verify FFmpeg libvmaf and source comparability")
    try:
        vmaf, ssim = float(vmaf), float(ssim)
    except (ValueError, TypeError) as exc:
        raise RenderQualityError("invalid VMAF/SSIM values") from exc
    import math
    if not (math.isfinite(vmaf) and math.isfinite(ssim)):
        raise RenderQualityError("nonfinite VMAF/SSIM values")
    if not (0 <= vmaf <= 100 and -1 <= ssim <= 1):
        raise RenderQualityError("VMAF/SSIM outside valid ranges")
    if vmaf < min_vmaf or ssim < min_ssim:
        raise RenderQualityError(
            f"quality below minimum: VMAF={vmaf:.2f}/{min_vmaf}; SSIM={ssim:.4f}/{min_ssim}"
        )
    return vmaf, ssim


def render(
    source: Path, destination: Path, *,
    profile: str = "auto", encoder: str | None = None,
    min_vmaf: float = 75.0, min_ssim: float = 0.80,
    timeout: int = 1800,
) -> dict:
    """Render via upstream and evaluate; publication is forbidden on any QA failure.

    Use a profile path to select any upstream transform; profile='auto' chooses
    an upstream shipped preset. Source must be owned/licensed.
    """
    source, destination = Path(source), Path(destination)
    if not source.is_file():
        raise RenderQualityError(f"source file missing: {source}")
    if source.resolve() == destination.resolve():
        raise RenderQualityError("source and destination must differ")
    destination.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="video_uniq_v2_"))
    candidate = work / "encoded.mp4"
    try:
        args = [_cli(), "run", str(source), "--profile", profile, "--out", str(candidate),
                "--work-dir", str(work / "state"), "--no-progress", "--no-qa"]
        if encoder:
            args.extend(["--encoder", encoder])
        _run(args, timeout)
        if not candidate.is_file() or candidate.stat().st_size == 0:
            raise RenderQualityError("encoder did not produce a valid output")
        dimensions = _validate_structure(source, candidate)
        _decode_full(candidate, timeout)
        report_path = work / "quality.json"
        _run([_cli(), "qa", str(source), str(candidate), "--json",
              str(report_path), "--html", str(work / "quality.html"),
              "--loudness", "--no-cid-predict"], timeout)
        if not report_path.is_file():
            raise RenderQualityError("upstream QA report not generated")
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise RenderQualityError("invalid upstream QA report") from exc
        vmaf, ssim = _quality_check(report, min_vmaf, min_ssim)
        os.replace(candidate, destination)
        diagnostic = {
            "engine": "video-uniquifier", "version": "v2.1.2",
            "profile": profile, "vmaf": vmaf, "ssim": ssim,
            "structure": dimensions,
            "upstream_report": report,
        }
        (destination.parent / (destination.name + ".quality.json")).write_text(
            json.dumps(diagnostic, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return {"similarity": report.get("phash_similarity"),
                "intensity": "upstream-v2", "vmaf": vmaf, "ssim": ssim}
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
