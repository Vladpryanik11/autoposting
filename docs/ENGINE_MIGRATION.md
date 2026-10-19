# Video engine migration (working branch)

## Scope

- The **only** pipeline video encoder is `app.video_engine.render`.
- Legacy `app/uniq.py` and its geometry patch are removed.
- Docker checks out **all upstream source code** at pinned commit
  `2d967b15d182af6ac4d5c303c3adce625eba98b0` and installs it editable.
- Upstream license is retained under `vendor/video-uniquifier/LICENSE` after build
  and reproduced in `THIRD_PARTY_NOTICES.md`.

This is NOT a standalone GitHub fork owned by Vladpryanik11. Source is checked
out during image build. Transplanting upstream Git history into a new independent
repository requires a repository-level fork/import operation, not yet done.

## Adapter behavior

`Pipeline.prepare` → `app.video_engine.render` → upstream `video-uniq run`
→ full FFmpeg decode → stream geometry/audio/subtitle checks → upstream
`video-uniq qa` → SSIM/VMAF thresholds → atomic promotion of rendered output.
Any failure keeps the publication from READY; the existing scheduler marks a
retry or failure. No alternate renderer / fallback.

Config through environment:

- `VIDEO_UNIQ_PROFILE`: upstream profile path or `auto`, default `auto`.
- `VIDEO_UNIQ_BIN`: location of upstream CLI, default `video-uniq`.
- `VIDEO_UNIQ_MIN_VMAF`: default 75.
- `VIDEO_UNIQ_MIN_SSIM`: default 0.80.
- `VIDEO_UNIQ_TIMEOUT_SECONDS`: default 1800.

VMAF and SSIM are *quality metrics*, not platform originality signals.
For radical reframing/speed changes raw comparisons are not always meaningful,
and strict thresholds can reject valid edits. Tune using real licensed footage.

## Outstanding blockers for production

1. Build the Docker image and verify the FFmpeg binary exposes `libvmaf`.
   Debian's FFmpeg packages differ by distribution; adapter deliberately fails
   closed rather than silently skipping missing VMAF.
2. Execute the full suite and video E2E tests on licensed sample MP4 with audio,
   baked captions and selectable subtitle streams.
3. Verify audio loudness / true peak from upstream QA report against policy.
   The CLI measures loudness, but the adapter does not yet gate by LUFS/peak.
4. Verify source/destination media stream contracts and subtitle burn-in profiles.
   Strict stream preservation is intentional; burnt subtitles need an explicit
   policy.
5. Rework metadata persistence: quality JSON is stored next to the variant
   and is not yet copied to the SQLite audit tables.
6. Create a new owned GitHub repository from upstream preserving full Git
   history, then retarget the pinned checkout URL to that repository.
7. Add a nightly smoke test which actually builds the Docker image and runs
   a 5-second encode/QA clip. Contract tests alone are insufficient.
8. Add a startup health check for presence of libvmaf and version alignment.

The Docker image has **not** been built in this session. The connector cannot
run the remote code locally and GitHub Actions needs a configured runner.
No claim of end-to-end readiness is made.
