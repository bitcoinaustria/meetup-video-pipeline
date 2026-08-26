# Meetup Video Pipeline

Local-first tooling for turning a single-camera meetup recording and a slide PDF into branded long-form video and vertical clips. The pipeline combines digital slides, smooth speaker tracking, fail-closed privacy blurring, conservative speech edits, full-cover FAQ cards, subtitles, chapters, thumbnails, and publishing copy without a traditional video editor.

The included templates use Bitcoin Austria branding as a working example. Replace the background, logo, publishing voice, and organization fields for another organizer.

The generator is presentation-neutral and host-portable: event facts, source paths, terminology, timing, review decisions, and output names live in a project manifest and its project folder. FFmpeg encoding is selected automatically for the current machine. Apple Vision is the qualified macOS privacy/OCR backend; other platforms must configure a detector that passes the same TSV recall gates.

## Requirements

- Python 3.11+
- FFmpeg and ffprobe
- Poppler (`pdftotext` and `pdftoppm`)
- Xcode Command Line Tools on macOS for Apple Vision detection and OCR, or a qualified detector command on other platforms
- [whisper.cpp](https://github.com/ggml-org/whisper.cpp) plus a local Large-v3 model
- TypeWhisper with Parakeet TDT v3 when the secondary audio pass is required
- The CLI for the analyzer selected by the invoking agent (`codex` or `claude`)

On macOS:

```sh
brew install ffmpeg poppler
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

On Debian or Ubuntu:

```sh
sudo apt install ffmpeg poppler-utils
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Linux and macOS run the complete portable pipeline. Windows runs the portable contracts natively and the complete pipeline through WSL2 until a native privacy detector passes the labeled recall suite.

## Agent-first workflow

Put the recording, slide PDF, and any supporting files in an event folder, for example:

```text
projects/my-talk/source/
├── recording.mp4
└── slides.pdf
```

Then ask a production agent to handle that folder. No filenames, manifest, or `make`
commands are required from the user. An announcement link can be included immediately:

> Produce the meetup video from `projects/my-talk/source`. Event information:
> `https://example.com/events/my-talk`

The agent inventories the files, creates or completes the project manifest, and uses the
announcement page for the event title, speaker, abstract, terminology, date, venue, and
organizer context. If no Meetup, Luma, or website link was supplied in the request, manifest,
or supporting files, the agent asks for one; the user can confirm that no page exists. The
agent captures supported facts in `event_context`, selects its own analyzer adapter, runs the
pipeline gates, requests preview approval, and reports the finished artifacts. Render scripts
never browse independently.

Paths in a manifest resolve relative to that manifest, so a complete project folder can be
archived to shared storage and restored on another checkout.

The initial camera/slide calibration produces the timeline, slide images, speaker track, and two privacy masks referenced by the manifest. These physical-camera values stay project-specific; the render and edit pipeline never assumes one event's crop or speaker position applies to another. See the [agent production runbook](.agents/skills/meetup-video-production/SKILL.md) for the exact gates and artifact contract.

## Pipeline contract

The production agent uses these commands as a reproducible internal interface. They remain
available to maintainers for debugging and automation; ordinary users should not need them.
Every command accepts the same manifest through `PROJECT`:

```sh
make check PROJECT=projects/my-talk/project.json
make capabilities PROJECT=projects/my-talk/project.json
make preview PROJECT=projects/my-talk/project.json START=260 DURATION=60
make audio PROJECT=projects/my-talk/project.json ANALYZER=codex
make faq PROJECT=projects/my-talk/project.json ANALYZER=codex
make final PROJECT=projects/my-talk/project.json ANALYZER=codex
make shorts PROJECT=projects/my-talk/project.json
make copy PROJECT=projects/my-talk/project.json ANALYZER=codex
make chapters PROJECT=projects/my-talk/project.json
make validate PROJECT=projects/my-talk/project.json
```

`capabilities` smoke-tests the installed FFmpeg encoders, records the selected backend and
platform detector/OCR availability in `build/host-capabilities.json`, and reports the resource budget.
Selection is automatic: VideoToolbox on supported Macs, NVENC/QSV/AMF where their FFmpeg
encoder really initializes, and `libx264` otherwise. The project manifest stores only
`acceleration: auto|off|required`; machine-specific results stay out of the portable manifest
and are recorded in build data and the final-render sidecar.

Analysis uses an automatic run-wide budget. Maintainers can reproduce constrained runs with
`JOBS=`, `GPU_JOBS=`, and `RENDER_JOBS=`; ordinary users should leave them unset. GPU inference
and 4K rendering default to one concurrent job, while independent semantic batches use up to
four workers. Shorts honor the render budget and are safe to resume from their per-clip caches.

The manifest's `analyzer` selects the default agent for every semantic stage. The production
agent selects or overrides it with `ANALYZER=codex` (or `claude`); optional
`audio_analyzer`, `faq_analyzer`, and `publishing_analyzer` settings override individual
stages. The generator has no vendor-specific default.

`make final` rebuilds context-gated speech edits and audience FAQ coverage, renders at `final_resolution`, validates stereo audio and visible frames, then atomically replaces the configured final output. `make release` additionally regenerates grounded publishing copy. A short 1080p preview is the required approval gate before an expensive production render.

Preview approval is content-addressed: if the source, EDL, FAQ timeline, slides, masks, speaker track, or renderer changes, `make final` stops until a new preview is inspected. Final validation samples known full-blur intervals in the artifact, and the final-render sidecar binds Shorts to the exact final file and current EDL/FAQ hashes.

## Output layout

The output root contains categories instead of loose artifacts:

```text
output/
├── final/       publishable long-form renders
├── shorts/      publishable vertical clips and SRT subtitles
├── thumbnails/  approved thumbnail variants
├── metadata/    publishing copy and YouTube chapters
└── debug/       previews, review renders, and logs; safe to delete
```

Source media remains in the project's `source/` directory. Intermediate assets belong in `build/` or `tmp/`, not in `output/`.

## Safety and edit contract

- One source-time EDL drives audio, video, tracking, slides, FAQ insertion, chapters, and Shorts.
- Multi-pass speech editing cuts only when deterministic, acoustic, speaker-context, and semantic checks agree.
- Mono input is duplicated to the required stereo output. Stereo input is inspected before processing because its channels can contain two independent mono microphones.
- Ambiguous speaker identity or overlapping people trigger full-camera blur; privacy fails closed.
- Camera timestamps remain the render clock, preventing duplicate catch-up frames around hard cuts.
- Source media is immutable. Caches live in `build/` and `tmp/`; release artifacts live in categorized `output/` subdirectories.

`final-edits.json` is generated by the FAQ stage. Put hand edits in the manifest's `base_edits` file; the generated EDL records its inputs and should not be edited directly. FAQ labels, accent color, optional font, Whisper paths, analyzer choice, and chapter opt-out are manifest settings. Person detectors can be replaced with `privacy_detector_command`, whose command receives `{inputs}` and `{output}` placeholders for the existing TSV contract. A non-macOS privacy analysis without such a qualified command stops instead of selecting an untested model.

Contributor checks, including a generated project in a path containing an apostrophe, run with:

```sh
make test
```

Use `scripts/score-detections.py` against labeled count TSVs before adopting a detector replacement; its default gates prioritize any-person and overlapping-person recall.

`video-project.example.json` documents all reusable settings. Local project folders and review decisions are ignored by Git; branded layout templates and generator code are versioned.

See [VISION.md](VISION.md) for product direction and [AGENTS.md](AGENTS.md) for contributor rules.

## License

The generator and templates are licensed under [MIT](LICENSE). The Bitcoin Austria name, marks, and listed [brand assets](BRAND_ASSETS.md) are expressly excluded from that license.
