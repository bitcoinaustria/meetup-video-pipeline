# Architecture audit

Full review of the generator as of `6703c71` (initial public release). Scope: every script,
the Makefile, the manifest contract, the docs, and the stated invariants in AGENTS.md and
VISION.md. Line references point at the audited revision.

## Verdict

The core architecture is sound and unusually disciplined for a media pipeline of this size:
one source-time EDL, fail-closed privacy, identity-fingerprinted caches, schema-validated
LLM calls that fail closed, an atomic final replace behind a lock, and a single-pass ffmpeg
composition that avoids generational quality loss. Those decisions are correct and worth
keeping.

The real risks are not in the ideas but in the duplication around them: the source→output
time mapping is independently re-implemented four times, project-path resolution and
transcript parsing are copy-pasted across scripts with visible drift, `layout.json` is
dead configuration, and several stated invariants (privacy in the final artifact, stereo
input, preview/final equivalence, Shorts A/V alignment) are not actually verified by any
gate. Cross-platform is closer than the docs suggest because the Apple Vision tools are
already isolated behind a one-line TSV contract.

## What is right about the current design

- **One source-time EDL** driving audio, video, tracking, slides, FAQ insertion, chapters,
  and Shorts is the correct backbone. `check` re-verifying that every approved base and
  audio edit is represented in the final EDL (`scripts/meetup-video.py:300-333`) is a
  strong consistency gate.
- **Fail-closed privacy** is genuinely fail-closed: ambiguous speaker ranking becomes
  `speaker = None` (`scripts/build-privacy-review.py:114`), any `None` speaker forces
  full-blur, and `hold_unsafe` pads 1.5 s on both sides. `test-privacy-safety.py` pins the
  exact merge-detection regression the docs warn about.
- **Cache identity everywhere** (path + size + mtime, plus policy/prompt versions and
  content hashes for LLM inputs) makes expensive stages resumable and makes stale
  artifacts a hard error instead of a silent wrong render.
- **LLM usage is well contained**: read-only tools, JSON schema enforcement, every
  candidate ID accounted for exactly once, invalid or missing responses fail closed
  (`audio-post.py:valid_semantic_decisions`, `build-faq.py:validate_analysis`).
- **Single ffmpeg invocation** for the final render keeps camera timestamps as the render
  clock and avoids intermediate re-encodes; the blend-with-background trick to preserve
  the camera clock is documented in place.
- **Atomic final replace** with a lock directory and validate-before-replace
  (`meetup-video.py:417-441`) honors "keep the previous final until the replacement
  passes".

## Findings

Ordered by how much they threaten the pipeline's own stated invariants.

### 1. The final artifact is never checked for privacy blur (high)

`validate_render` checks resolution, stereo, duration, and three non-black frames
(`meetup-video.py:222-276`). Nothing verifies that the privacy mask actually landed in the
output. A wrong `--privacy-mask-start`, a mask that encodes black where it should be
white, or a filter-graph regression that drops the overlay would produce a validated,
publishable video with bystanders visible — the exact failure the project treats as worst
case. `check` only verifies mask *duration*.

Fix: derive a handful of known full-blur timestamps from the mask (or store them alongside
it when the mask is built), sample those frames in the rendered output, and assert low
high-frequency energy inside the blur region. Cheap, and it turns the strongest invariant
from "constructed correctly" into "verified in the artifact".

### 2. Shorts can silently desync audio from video (high)

`render-clip` takes video from the raw source (continuous from `source_start`) and audio
from the final render via `source_to_final` (`render-shorts.py:200-268`).
`source_to_final` only rejects a clip that *starts* inside a removed passage
(`render-shorts.py:31-33`). Any EDL cut or FAQ insertion that falls *inside* the clip's
duration shifts the final-render audio relative to the raw video, and lip sync drifts
mid-clip with no error. Separately, nothing ties `final_output` to the current EDL: render
Shorts after editing `final-edits.json` but before re-rendering the final, and every clip
is misaligned.

Fix: refuse any clip whose `[source_start, source_start + duration)` intersects an edit or
FAQ insertion, and record the EDL/FAQ hash in the final render (sidecar JSON) so Shorts
can verify freshness.

### 3. Four independent implementations of source→output time mapping (high)

- `chapter_entries.output_time` (`meetup-video.py:133-148`)
- `build_time_map` (`audio-post.py:886-923`)
- `edited_time` inside the render (`render-video.py:381-384`)
- `source_to_final` (`render-shorts.py:25-40`)

They differ in boundary conditions today (e.g. which comparison is strict, how
`presentation_start` clamps, whether FAQ insertions before the query point count). Any
future edit-model change must be found and fixed in all four places or chapters, Shorts,
and the render disagree — precisely the class of bug the "one EDL" rule exists to prevent.
`audio-post` already writes `time-map.json`; nothing consumes it.

Fix: one shared `timemap` module (or make the render emit the authoritative
source→output map including FAQ insertions, and have chapters and Shorts consume that
artifact instead of recomputing).

### 4. `layout.json` is dead configuration (medium)

No script reads it. `render-video.py:349-361` hardcodes the same geometry (864×1536 at
91,296; 2730×1536 at 1019,296), and there's a small existing divergence
(`layout.json` says slide width 2730.6667; the render uses 2730). Anyone retheming the
canvas by editing `layout.json` — the obvious move, and what BRAND_ASSETS.md implies —
changes nothing. Either the render should consume `layout.json`, or the file should be
deleted and the geometry documented where it actually lives.

### 5. Preview approval is not tied to what `final` renders (medium)

The runbook makes a 1080p preview the approval gate, but `make final` re-runs audio
analysis and FAQ analysis first (`meetup-video.py:625-629`). Caches usually make that a
no-op, but any drift — a whisper model or binary update changes transcripts, a source
mtime bump after archiving, a prompt-version bump — regenerates candidates, re-runs the
semantic review, and can approve a *different* cut than the one previewed. Nothing
detects that the EDL changed between preview and final.

Fix: at preview time, record a hash over (EDL, FAQ timeline, timeline, masks, speaker
track); have `final` fail (or loudly warn) when the hash no longer matches.

### 6. mtime-based identity contradicts the portability promise (medium)

README promises a project folder can be archived to shared storage and restored. Almost
every identity check compares `mtime_ns` strictly (`meetup-video.py:315-322`,
`audio-post.py:52-54`, `build-faq.py:122-135`, `render-shorts.py:50-61`). A plain `cp`
back from archive changes mtimes: approved audio edits become "stale" and block `final`,
and hours of transcription cache is discarded. The failure is at least closed, not silent,
but the two goals conflict.

Fix: content-based identity for the source video (size + sha256, or size + hash of first
and last 64 MiB for speed), mtime only as a fast-path shortcut. `file_sha256` already
exists in `audio-post.py`.

### 7. Late failures `check` could catch up front (medium)

- **Audio channel count**: a mono source renders for hours, then `validate_render`
  rejects "expected two audio channels" (`meetup-video.py:246-247`). `check` never probes
  the source's audio stream, even though `audio-post` computes a full channel analysis.
- **Sample rate with FAQ cards**: FAQ card audio is `anullsrc=r=48000:cl=stereo`
  (`render-video.py:433`) concatenated with source audio at its native rate. A 44.1 kHz
  or mono source makes the `concat` filter's inputs mismatch — an ffmpeg error (or wrong
  negotiation) deep into the final render. An explicit `aresample=48000` +
  channel-layout normalization on the edited chain would make card insertion
  source-independent.
- **Disk check on the wrong volume**: `shutil.disk_usage(ROOT)` (`meetup-video.py:354`)
  measures the repo's filesystem, but `final_output` lives under the project directory,
  which the docs encourage keeping on shared/external storage. Measure at
  `output.parent`.
- **`render_policy` is computed but never consumed**: `analyze_channels` classifies
  dual-mono vs independent channels and prescribes a render policy
  (`audio-post.py:212-235`), but no renderer reads it. An independent-two-mic source
  ships with one voice per ear and no loudness treatment (long-form has no `loudnorm` at
  all; only Shorts normalize). At minimum `check` should surface the classification;
  ideally the final render honors the policy.

### 8. Copy-paste drift across scripts (medium)

`project_path`/path resolution exists in five variants, including two module-global
`PROJECT_DIR` mutations (`build-faq.py:799`, `test-faq-coverage.py:21`) alongside the
`_project_dir` dict-key style. Two different `run()` wrappers, three `timestamp()`
formatters, and two near-identical whisper-JSON→words parsers (`audio-post.py:930` vs
`render-shorts.py:100`) with different edge-case handling (`[_` vs `[` prefix skip). The
claude/codex invocation + envelope parsing is duplicated four times
(`meetup-video.py:492`, `audio-post.py:1511`, `build-faq.py:424`). This is where the next
subtle inconsistency will come from. A small `scripts/lib/` package (path resolution,
ffprobe wrappers, fingerprints, transcript parsing, LLM invocation, time map) removes the
whole class.

### 9. Filter-graph scale wall (medium, latent)

Cuts are implemented as `split=N` + per-segment `trim` + `concat`
(`render-video.py:113-132`). Every kept segment adds a full-rate branch, so per-frame
work grows linearly with cut count. Filler/stutter editing of a 60–90 minute talk can
plausibly approve hundreds of micro-cuts; at that point the graph gets very slow and
memory-hungry or hits ffmpeg limits. Same pattern in the audio path with per-segment
`afade`. Either document a tested ceiling, or switch to
`select`/`aselect` with `between()` expressions (O(1) branches, loses the per-cut audio
crossfade) — or render kept segments separately and join with the concat demuxer.

### 10. Presentation-neutrality leaks (low, but it's a stated working agreement)

- `make-faq-card.py` hardcodes German label text ("FRAGE AUS DEM PUBLIKUM"), brand red,
  layout coordinates, and a macOS-only font path (`make-faq-card.py:11,26-27`).
  The label at minimum belongs in the project manifest; font/colors belong with the
  brand assets.
- `vision-ocr.swift:27` pins `recognitionLanguages = ["en-US"]` while the example project
  is German.
- `detect-changes.py:12` hardcodes camera-specific screen geometry (`image[30:]`,
  `[30:, 75:]`).
- `build-privacy-review.py` predates the manifest: it reads `ROOT / "timeline.json"`,
  `ROOT/tmp` defaults, and hardcodes source width 3840 (`build-privacy-review.py:274,296`),
  so it only works for a project living at the repo root. It also shells out to literal
  `python3` (`:327`) instead of `sys.executable`, bypassing the venv.
- `audio-post.py` defaults point at one machine: `~/.cache/openwhispr/...`,
  `/Applications/TypeWhisper.app/...`, `--threads 10`.

### 11. Smaller correctness notes (low)

- `make validate` with `--input` always asserts 1920×1080 (`meetup-video.py:620-623`), so
  validating the 4K final by explicit path fails wrongly.
- The chapter refresh regex `(?ms)(^Kapitel\n).*?(?=\n\n## )` (`meetup-video.py:191-196`)
  silently does nothing when the Kapitel block isn't followed by another `## ` heading —
  a hand-edited publishing file keeps stale chapters with no warning.
- ffconcat writers quote paths as `file '{path}'` (`render-video.py:185`,
  `render-shorts.py:309`, `audio-post.py:1692`); a source path containing an apostrophe
  breaks the concat file. Escape or reject.
- `validate_render`'s black-frame test passes if a single 16×9 sample pixel reaches
  luma 16 — fine as a smoke test, but it will not catch a mostly-black composition.
- Chapters hard-require ≥3 entries ≥10 s apart (`meetup-video.py:165-168`) — short talks
  or sparse decks can never pass `check`; deserves a manifest opt-out.
- `merge_analyses` treats any two turns within 0.25 s overlap as duplicates
  (`build-faq.py:544-551`); two genuinely adjacent questions can merge and lose a card.
- Cache artifacts (chunk WAVs, transcripts) are written in place; an interrupted ffmpeg
  can leave a truncated file that passes the `exists()` fast path
  (`audio-post.py:329-330`). Write to a temp name and rename.
- Transcript- and slide-derived text is interpolated into LLM prompts that run with
  `--permission-mode dontAsk`. Tools are read-only and outputs are schema-checked, so
  blast radius is small, but publishing copy is an injection target for anything an
  audience member says; the human review step is the real control and should stay
  mandatory.

## What's missing entirely

- **CI.** No workflow runs `py_compile`, the self-tests (`audio-post.py self-test`,
  `render-video.py`'s import-time asserts, `test-privacy-safety.py`), or the coverage
  checks. Everything above ships on discipline alone.
- **A synthetic fixture project.** A tiny generated source (ffmpeg `testsrc` + tone +
  generated slide PDF + trivial masks) would let `check → preview → final → validate`
  run end-to-end in CI in minutes, and is also the enabler for cross-platform work.
- **A `make test` target** aggregating the self-tests that currently only run as
  side effects.
- **Unit tests for the time map** — the most consequence-dense pure logic in the repo has
  zero direct tests.
- **A detection-quality harness.** VISION.md says cross-platform is "earned when Apple
  Vision has tested replacements", but there is no labeled sample set or scoring script
  to test a replacement against.

## Unintended consequences of current decisions

- The `preview_preset`/`final_preset` example both say `ultrafast` with `libx264` — a 4K
  final at 24 Mbit ultrafast wastes most of its bitrate; the example config quietly
  encourages a low-quality release encode.
- `make final` rebuilding decisions each run (see finding 5) means "re-render the same
  approved video" is not actually guaranteed reproducible.
- Strict mtime identity (finding 6) means the documented archive/restore workflow
  degrades to "re-approve everything".
- `build-faq.py` writes the final EDL directly (`write_outputs`,
  `build-faq.py:730-776`); an automatic (unreviewed-path) FAQ analysis rewrites the
  release edit list in place. `check`'s gates make this safe-ish, but the file named
  `final-edits.json` being machine-overwritten on every `make faq` will surprise a human
  who hand-tuned it. The preserved/regenerated split (audience types are regenerated,
  everything else preserved) is subtle enough to deserve a header comment in the file
  itself.
- Import-time asserts at the bottom of `render-video.py` and `render-shorts.py` run on
  every production invocation — nice as always-on invariants, but a failed assert blocks
  a render with a stack trace rather than a message, and they only run when the script is
  the entry point, so they vanish under any future refactor to a library.

## Cross-platform path

The macOS surface is smaller than "macOS-first" implies. Concretely:

| Dependency | Where | Portable replacement |
|---|---|---|
| Apple Vision person detection | `vision-people.swift` | Any ONNX person detector (e.g. RT-DETR/YOLO-class) via `onnxruntime`, emitting the same TSV |
| Apple Vision OCR | `vision-ocr.swift` | PaddleOCR/RapidOCR or Tesseract, same TSV |
| TypeWhisper + Parakeet | `audio-post.py` secondary pass | NVIDIA NeMo/onnx Parakeet, or any CLI honoring the existing JSON contract (`engine`, `model`, `text`) |
| `h264_videotoolbox` | `render-video.py` default, review clips | Encoder probe: videotoolbox → nvenc → vaapi/qsv → libx264 |
| Arial Bold system path | `make-faq-card.py` | Ship an OFL font in the repo |
| Spatial-audio gotcha | docs only | — |

The architecture already did the hard part: both Swift tools are pure batch programs with
a `--list in.tsv --output out.tsv` contract, and the secondary transcriber output is
validated JSON. So the plan is:

1. **Make detectors manifest-configured commands.** `"people_detector": ["scripts/vision-people.swift"]`
   with the TSV contract as the spec, plus an `onnxruntime`-based reference implementation
   (`scripts/detect-people-onnx.py`) as the Linux default. Same for OCR. Whisper.cpp and
   ffmpeg/Poppler are already cross-platform.
2. **Build the quality harness first** (missing-pieces section above): a few hundred
   labeled frames from real meetups, a script that scores a detector against them, and a
   floor the ONNX detector must meet — especially on the merge-two-people failure mode
   the privacy logic depends on. This is what VISION.md's "earned" means operationally,
   and the fail-closed design tolerates a somewhat weaker detector (missed separation ⇒
   more full-blur, never less privacy) as long as *recall of any-person-present* stays
   high; that asymmetry should be an explicit acceptance criterion.
3. **Encoder auto-selection** in `render-video.py` (`ffmpeg -encoders` probe, or try/fall
   back the way review clips already do in `audio-post.py:1681-1688`), keeping the
   manifest override.
4. **Config over hardcode** for font, FAQ label, OCR language, whisper binary/model paths
   (env or manifest), which is finding 10 anyway.
5. **Target Linux, not Windows.** Everything assumes POSIX paths and `#!/usr/bin/env`;
   Linux + WSL covers the realistic organizer base for a fraction of the effort of native
   Windows support.
6. **Then CI becomes possible**: with detection pluggable and a synthetic fixture, the
   whole `check → final → validate` chain runs on a Linux runner, which retroactively
   protects the macOS path too.

## Priority order

1. Verify privacy in the rendered artifact (finding 1) — cheapest insurance on the
   highest-stakes invariant.
2. Shorts range validation + final-output freshness (finding 2) — silent published-artifact
   corruption.
3. Extract the shared time map (finding 3) and a `scripts/lib` package (finding 8) — stops
   the drift that everything else grows from.
4. Early `check` probes: audio channels, sample-rate normalization for FAQ cards, disk on
   the output volume (finding 7).
5. Preview→final consistency hash (finding 5) and content-based source identity
   (finding 6).
6. Fixture project + CI + `make test` (missing pieces) — then the cross-platform detector
   work (plan above) lands with a safety net.
