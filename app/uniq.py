"""Уникализатор видео на базе Hostlife22/yt_uniquifier (vendor/yt_uniquifier).

Для каждой копии генерируется свой YAML-профиль со случайными параметрами
в безопасных диапазонах, затем запускается `yt-uniq run`. После кодирования
yt-uniq пишет QA-отчёт (<out>.qa.json) с pHash-похожестью на оригинал; если
копия слишком похожа, она переделывается с более сильным пресетом.
"""
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile

import yaml

# Диапазоны случайных изменений по силе.
INTENSITY = {
    "light":  {"crop": (0.015, 0.025), "rotate": (0.1, 0.3), "speed": 0.01, "pitch": 0.006,
               "noise": (2, 4), "color": 0.6},
    "medium": {"crop": (0.025, 0.04), "rotate": (0.2, 0.6), "speed": 0.02, "pitch": 0.012,
               "noise": (3, 6), "color": 1.0},
    "strong": {"crop": (0.04, 0.06), "rotate": (0.5, 1.0), "speed": 0.035, "pitch": 0.02,
               "noise": (5, 9), "color": 1.6},
}
LEVELS = list(INTENSITY)

# Итоговый формат: соотношение сторон + целевая громкость площадки (LUFS).
FORMATS = {
    "reels":  {"aspect": "9:16", "size": (1080, 1920), "lufs": -14.0},
    "square": {"aspect": "1:1", "size": (1080, 1080), "lufs": -14.0},
    "wide":   {"aspect": "16:9", "size": (1920, 1080), "lufs": -14.0},
}


def build_profile(rng: random.Random, intensity: str = "medium",
                  fmt: str | None = "reels", flip: bool = False) -> dict:
    b = INTENSITY[intensity]
    c = b["color"]
    speed = round(1 + rng.uniform(-b["speed"], b["speed"]), 4)
    t = []
    if fmt:
        f = FORMATS[fmt]
        t.append({"id": "video.fit_aspect", "enabled": True, "params": {
            "target_aspect": f["aspect"], "target_width": f["size"][0],
            "target_height": f["size"][1], "mode": "pad_blur", "allow_upscale": True,
            "blur_sigma": round(rng.uniform(20, 35), 1)}})
    t += [
        {"id": "video.crop_resize", "enabled": True,
         "params": {"max_strength": round(rng.uniform(*b["crop"]), 4),
                    "rng_seed": rng.randint(1, 2**31)}},
        {"id": "video.rotate", "enabled": True,
         "params": {"degrees": round(rng.uniform(*b["rotate"]) * rng.choice([-1, 1]), 3)}},
        {"id": "video.color_eq", "enabled": True, "params": {
            "brightness": round(rng.uniform(-0.02, 0.02) * c, 4),
            "contrast": round(1 + rng.uniform(-0.01, 0.03) * c, 4),
            "gamma": round(1 + rng.uniform(-0.02, 0.02) * c, 4),
            "saturation": round(1 + rng.uniform(-0.03, 0.06) * c, 4)}},
        {"id": "video.noise", "enabled": True, "params": {"strength": rng.randint(*b["noise"])}},
    ]
    if flip and rng.random() < 0.5:
        t.append({"id": "video.mirror", "enabled": True})
    if speed != 1.0:
        t.append({"id": "video.speed", "enabled": True, "params": {"rate": speed}})
    t += [
        {"id": "audio.pitch_tempo", "enabled": True, "params": {
            "pitch": round(1 + rng.uniform(-b["pitch"], b["pitch"]), 4),
            "tempo": speed, "method": "rubberband"}},
        {"id": "audio.eq", "enabled": True, "params": {"randomize_bands": True, "jitter_db": 1.0 * c}},
        {"id": "audio.loudnorm", "enabled": True, "params": {"target_jitter_lufs": 1.0}},
    ]
    return {
        "name": f"auto_{intensity}",
        "description": "Сгенерировано uniq.py",
        "transforms": t,
        "audio_tracks": "first",
        "keep_hdr": False,
        "output_container": "mp4",
        "target_codec": "h264",
        "target_loudness_lufs": FORMATS[fmt]["lufs"] if fmt else -14.0,
        "seed_strategy": "divergent",
    }


def _yt_uniq(*args: str) -> None:
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    r = subprocess.run([sys.executable, "-m", "yt_uniquifier", *args],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    if r.returncode != 0:
        raise RuntimeError(f"yt-uniq упал ({r.returncode}):\n{(r.stdout + r.stderr)[-3000:]}")


def uniqualize(src: str, dst: str, intensity: str = "medium", fmt: str | None = "reels",
               flip: bool = False, qa: bool = True, encoder: str | None = None,
               seed: int | None = None) -> dict:
    """Одна уникальная копия. Возвращает {'profile': ..., 'similarity': float|None}."""
    rng = random.Random(seed)
    profile = build_profile(rng, intensity, fmt, flip)
    work = tempfile.mkdtemp(prefix="ytuniq_")
    try:
        prof_path = os.path.join(work, "profile.yaml")
        with open(prof_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(profile, f, allow_unicode=True, sort_keys=False)
        args = ["run", src, "--profile", prof_path, "--out", dst,
                "--work-dir", os.path.join(work, "state"), "--new-variant", "--no-progress",
                "--accept-watermark-risk"]
        args += ["--fast-qa"] if qa else ["--no-qa"]
        if encoder:
            args += ["--encoder", encoder]
        _yt_uniq(*args)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    sim = None
    qa_json = dst + ".qa.json"
    if os.path.exists(qa_json):
        with open(qa_json, encoding="utf-8") as f:
            sim = json.load(f).get("phash_similarity")
    return {"profile": profile, "similarity": sim}


def uniqualize_checked(src: str, dst: str, max_similarity: float | None = None,
                       intensity: str = "medium", **kw) -> dict:
    """Если pHash-похожесть выше порога — переделать с более сильным пресетом."""
    for level in LEVELS[LEVELS.index(intensity):]:
        res = uniqualize(src, dst, intensity=level, qa=True, **kw)
        res["intensity"] = level
        if max_similarity is None or res["similarity"] is None or res["similarity"] <= max_similarity:
            break
    return res


def cleanup_reports(dst: str) -> None:
    for ext in (".qa.json", ".qa.html"):
        if os.path.exists(dst + ext):
            os.remove(dst + ext)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Сделать N уникальных копий видео (yt_uniquifier)")
    ap.add_argument("src")
    ap.add_argument("-n", "--copies", type=int, default=1)
    ap.add_argument("-o", "--out", default="output")
    ap.add_argument("--intensity", choices=LEVELS, default="medium")
    ap.add_argument("--format", choices=[*FORMATS, "keep"], default="reels",
                    help="итоговый формат с размытым фоном; keep — не менять")
    ap.add_argument("--flip", action="store_true", help="разрешить зеркалирование")
    ap.add_argument("--encoder", help="например h264_nvenc; по умолчанию автовыбор")
    ap.add_argument("--max-similarity", type=float, help="порог pHash-похожести 0..1")
    a = ap.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")
    os.makedirs(a.out, exist_ok=True)
    stem = os.path.splitext(os.path.basename(a.src))[0]
    for i in range(a.copies):
        dst = os.path.join(a.out, f"{stem}_u{i + 1}.mp4")
        res = uniqualize_checked(a.src, dst, a.max_similarity, a.intensity,
                                 fmt=None if a.format == "keep" else a.format,
                                 flip=a.flip, encoder=a.encoder)
        print(f"{dst}  пресет={res['intensity']}  похожесть={res['similarity']}")
