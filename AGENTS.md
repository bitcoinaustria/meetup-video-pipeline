# AGENTS.md — AI Agent Contributor Guide

This file contains only the repo-specific rules for changing the generator. Read [README.md](README.md) for setup and commands and [VISION.md](VISION.md) for product direction.

## Quality Gates

- Run `make check` after changes to tracking, privacy, timelines, or rendering.
- Run `python3 -m py_compile scripts/*.py` after Python changes.
- Render a short 1080p passage before any full production run; full renders are slow and consume substantial disk space.
- Validate the final artifact with `make validate` before calling it complete.
- Run `make test` before shipping generator changes.

Privacy is fail-closed: ambiguous speaker identity or overlapping people must blur the complete camera panel. Never weaken this fallback to improve aesthetics.

Audio and video edits share one source-time EDL. Any time cut must remap speaker tracking, slide timing, FAQ insertion, and Shorts consistently.

Source media is immutable and stays outside Git. Within each local project folder, generated files belong in `build/`, temporary analysis in `tmp/`, and deliverables in `output/` so caches can be deleted without touching originals or releases.
Keep release artifacts in `output/final`, `output/shorts`, `output/thumbnails`, and `output/metadata`; previews, review renders, and logs belong in deletable `output/debug` subdirectories.

## Working Agreements

- Keep commits small and self-contained; do not mix a design change, tracking change, and production manifest edit without a reason.
- Resolve relative paths from the project manifest so a complete project folder remains portable.
- Put event terminology, source geometry, timings, and review decisions in project JSON; generator code and prompts must remain presentation-neutral.

## Common Gotchas

1. Stereo may contain two independent mono microphone feeds. Inspect channel content before downmixing or normalizing.
2. Brief detector merges are expected when people overlap. The full-camera blur mask intentionally starts early and holds through uncertainty.
3. macOS Spatial Audio with head tracking can make a correct stereo render sound as if its balance moves. Verify with head tracking disabled before changing the mix.
4. Whisper often drops question punctuation in weak room audio. FAQ detection must combine wording, relative microphone level, and answer context, and every suspect segment must be accounted for.
