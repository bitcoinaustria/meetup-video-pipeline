---
name: meetup-video-production
description: Run a meetup video project from local source media through preview, privacy and edit checks, 4K final render, Shorts, chapters, and publishing copy.
---

# Meetup video production

Use this procedure when creating or rerendering a meetup project. Replace `<project>` with the manifest path in every command; never silently fall back to another event.

1. For a new event, run `make init NAME=<slug>`. Put the immutable recording and slide PDF in the new project's `source/` directory and complete `project.json`, especially the presentation start, language, transcription prompt, camera geometry, and output resolution.
2. Build or import the project-local slide images, timeline, speaker track, privacy mask, and full-blur mask referenced by the manifest. Do not reuse another camera's geometry without validating it against sampled frames.
3. Run `make check PROJECT=<project>`. Any missing audience turn, stale review identity, ambiguous privacy interval, or mismatched audio source is a blocker.
4. Run `make preview PROJECT=<project> START=<seconds> DURATION=60`. Inspect the opening, a tracking movement, a privacy overlap, an FAQ card, and a hard speech cut. Preview remains 1080p.
5. Run `make final PROJECT=<project>`. The command rebuilds audio and FAQ decisions, rejects any drift from the approved preview, renders the configured production resolution, validates duration and known full-blur intervals, and only then replaces the previous final atomically.
6. Run `make shorts PROJECT=<project>` when the Shorts manifest contains approved clips. The final-render sidecar must match the final file and current EDL/FAQ; clips crossing cuts or FAQ insertions are rejected. Subtitles remain separate SRT files.
7. Run `make release PROJECT=<project>` only when publishing copy should also be regenerated. Run `make chapters PROJECT=<project>` after any later EDL or FAQ timing change.
8. Finish with `make validate PROJECT=<project>` and record the final path, resolution, duration, size, audio channel layout, and passed checks.

Never weaken full-camera blur to rescue aesthetics. Never cut overlapping speech or low-confidence content. Keep the previous final until the replacement passes validation.
