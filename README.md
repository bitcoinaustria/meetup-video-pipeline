# Meetup Video Pipeline

Local-first tooling for turning meetup camera footage plus a slide deck, screen recording, or both into branded long-form video and vertical clips. The pipeline combines clean presentation visuals, smooth speaker tracking, fail-closed privacy blurring, conservative speech edits, full-cover FAQ cards, subtitles, chapters, thumbnails, and publishing copy without a traditional video editor.

The included templates use Bitcoin Austria branding as a working example. Replace the background, logo, publishing voice, and organization fields for another organizer.

The generator is presentation-neutral and host-portable: event facts, source paths, terminology, timing, review decisions, and output names live in a project manifest and its project folder. FFmpeg encoding is selected automatically for the current machine. Apple Vision is the qualified macOS privacy/OCR backend; other platforms must configure a detector that passes the same TSV recall gates.

## Requirements

- Python 3.11+
- FFmpeg and ffprobe
- Poppler (`pdftotext` and `pdftoppm`)
- Xcode Command Line Tools on macOS for Apple Vision detection and OCR, or a qualified detector command on other platforms
- [whisper.cpp](https://github.com/ggml-org/whisper.cpp) plus a local Large-v3 model
- TypeWhisper with Parakeet TDT v3 when the secondary audio pass is required
- The CLI for the analyzer selected by the invoking agent (`codex` or a Claude CLI that supports `--safe-mode`)

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
├── camera.mp4
├── screen.mp4       # optional
└── slides.pdf       # optional when screen.mp4 exists
```

Then ask a production agent to handle that folder. No filenames, manifest, or `make`
commands are required from the user. An announcement link can be included immediately:

> Produce the meetup video from `projects/my-talk/source`. Event information:
> `https://example.com/events/my-talk`

The agent inventories all recordings, creates or completes the project manifest, and uses the
announcement page for the event title, speaker, abstract, terminology, date, venue, and
organizer context. If no Meetup, Luma, or website link was supplied in the request, manifest,
or supporting files, the agent asks for one; the user can confirm that no page exists. The
agent also resolves camera/screen roles, sync, and talk boundaries from the media. It asks one
consolidated question only when those facts or speaker mapping remain ambiguous. Face and voice
changes are useful boundary cues, not identity proof. A continuous recording with several talks
becomes one project per talk, each referencing the same source with its own
`presentation_start` and `presentation_end`.

When a PDF and screen recording both exist, the PDF supplies clean images/text and the screen
recording supplies timings. With only a screen recording, the agent extracts stable frames and
OCR text before the normal render gates. It captures event facts in `event_context`, uses its own
analyzer adapter, requests preview approval, and reports the artifacts; render scripts never
browse or choose an agent independently.

Paths in a manifest resolve relative to that manifest, so a complete project folder can be
archived to shared storage and restored on another checkout.

The initial camera/slide calibration produces the timeline, slide images, speaker track, and two privacy masks referenced by the manifest. These physical-camera values stay project-specific; the render and edit pipeline never assumes one event's crop or speaker position applies to another. See the [agent production runbook](.agents/skills/meetup-video-production/SKILL.md) for the exact gates and artifact contract.

### Multiple programme speakers

The timeline may opt into reviewed programme sections and a three-panel dual-speaker layout.
Slides remain centered, participant identities stay on fixed left/right sides, and optional `active`
metadata adds a restrained border without moving either person. Participant tracks use the normal
source-time `time`/`x` samples plus reviewed `visible` booleans; an invisible participant is not
rendered or chased beyond the frame. Use an explicit standard section at a reviewed sentence
boundary when a sustained absence should return to the larger single-speaker layout.

```json
{
  "participants": {
    "host_a": {
      "track": "build/host-a-track.json",
      "crop": {"width": 720, "height": 1920, "y": 120},
      "audio_channel": 1
    },
    "host_b": {
      "track": "build/host-b-track.json",
      "crop": {"width": 720, "height": 1920, "y": 120},
      "audio_channel": 2
    }
  },
  "layout_sections": [
    {
      "source_start": 0.0,
      "source_end": 75.0,
      "kind": "intro",
      "layout": "dual_speaker",
      "left": "host_a",
      "right": "host_b",
      "active": "left"
    },
    {
      "source_start": 75.0,
      "source_end": 3600.0,
      "kind": "talk",
      "layout": "standard",
      "audio_channel": 1
    }
  ],
  "mix_mapped_microphones": true,
  "microphone_mix": {
    "inactive_gain": 0.18,
    "both_gain": 0.5,
    "fade_seconds": 0.12,
    "integrated_lufs": -18.0,
    "true_peak_db": -2.0
  }
}
```

Reviewed microphone mixing is opt-in and requires contiguous sections covering the recording.
Mapped DJI channels are normalized independently, centered, crossfaded at reviewed speaker turns,
and limited after mixing; inactive microphones remain audible at a reduced gain so overlap is never
discarded. Unmapped stereo is preserved unchanged. Every participant track is bound into the
privacy seal and preview/final identity. An ambiguous or overlapping participant still activates
the existing full-camera blur. Dual-speaker privacy review covers the complete reviewed sections
and matches participant geometry plus appearance; a missing, substituted, or ambiguous identity
fails closed to full-camera blur.

Audio post-production also identifies German and English moderator time/wrap-up cues as
`production_interruption` candidates. Wording, locally relative level, shared quiet boundaries,
section role, transcript reconnection, and semantic review must agree before a non-overlapping cue
from a mapped non-presenter microphone inside a standard talk can be cut. Same-channel cues, plus
cues in introductions, news, discussions, Q&A, missing section context, or overlapping channels,
remain review-only.

## Pipeline contract

The production agent uses these commands as a reproducible internal interface. They remain
available to maintainers for debugging and automation; ordinary users should not need them.
Every command accepts the same manifest through `PROJECT`:

```sh
make check PROJECT=projects/my-talk/project.json
make capabilities PROJECT=projects/my-talk/project.json
make privacy-seal PROJECT=projects/my-talk/project.json ANALYZER=codex
make privacy-preflight PROJECT=projects/my-talk/project.json
make preview PROJECT=projects/my-talk/project.json START=260 DURATION=60
make approve PROJECT=projects/my-talk/project.json
make audio PROJECT=projects/my-talk/project.json ANALYZER=codex
make faq PROJECT=projects/my-talk/project.json ANALYZER=codex
make final PROJECT=projects/my-talk/project.json ANALYZER=codex
make shorts PROJECT=projects/my-talk/project.json
make clean-debug PROJECT=projects/my-talk/project.json
make copy PROJECT=projects/my-talk/project.json ANALYZER=codex
make chapters PROJECT=projects/my-talk/project.json
make validate PROJECT=projects/my-talk/project.json
```

`capabilities` smoke-tests the installed FFmpeg encoders, records the selected backend and
platform detector/OCR availability in `build/host-capabilities.json`, and reports the resource budget.
Selection is automatic: VideoToolbox on supported Macs, NVENC/QSV/AMF where their FFmpeg
encoder really initializes, and `libx264` otherwise. The project manifest stores only
`acceleration: auto|off|required`; machine-specific results stay out of the portable manifest
and are recorded in build data and the final-render sidecar. Production commands reuse a successful
capability result while its host, FFmpeg, encoder, resolution, and detector signature remain unchanged;
`make capabilities` always performs a fresh probe.

Analysis uses an automatic run-wide budget. Maintainers can reproduce constrained runs with
`JOBS=`, `GPU_JOBS=`, and `RENDER_JOBS=`; ordinary users should leave them unset. GPU inference
and 4K rendering default to one concurrent job, while independent semantic batches use up to
four workers. Shorts honor the render budget and are safe to resume from their per-clip caches.

The manifest's `analyzer` selects the default agent for every semantic stage. The production
agent selects or overrides it with `ANALYZER=codex` (or `claude`); optional
`audio_analyzer`, `faq_analyzer`, and `publishing_analyzer` settings override individual
stages. The generator has no vendor-specific default.

`make final` rebuilds context-gated speech edits and audience FAQ coverage, reuses an already-current validated final, or renders at `final_resolution` and atomically replaces the configured output. `make release` additionally regenerates grounded publishing copy and renders approved Shorts. An empty Shorts manifest is recorded explicitly in `output/metadata/shorts.json`. A short 1080p preview followed by explicit `make approve` is the required approval gate before an expensive production render.

Privacy approval is content-addressed: `make privacy-seal` binds both masks to the source hash, talk range, geometry, every participant track, qualified detector artifacts, and repository-approved labeled dataset. Preview approval then binds the complete render inputs. Any drift stops final rendering or validation until the affected review is repeated. Final validation samples known full-blur intervals in every active camera panel, and the final-render sidecar binds Shorts to the exact final file and current EDL/FAQ hashes.

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
`make clean-debug PROJECT=...` removes the deletable review tree and legacy render-lock guards after approval; active locks live under `build/locks/`.

Each approved Short needs a stable lowercase `id`, a publishable `title`, `source_start`, and `duration`. The renderer creates numbered readable 1080×1920 files, a matching SRT, and `output/metadata/shorts.json`. Titles remain in filenames and publishing metadata; no title or subtitles are burned into the video. Crop and pan default to the reviewed speaker track; `crop_y` and `pan` remain optional manual overrides.

## Safety and edit contract

- One source-time EDL drives audio, video, tracking, slides, FAQ insertion, chapters, and Shorts.
- Multi-pass speech editing cuts only when deterministic, acoustic, speaker-context, and semantic checks agree.
- Mono input is duplicated to the required stereo output. Stereo input is inspected before processing because its channels can contain two independent mono microphones.
- Independent microphones are mixed only when reviewed sections explicitly map every channel and turn; true stereo is never inferred as a two-mic mix.
- Ambiguous speaker identity or overlapping people trigger full-camera blur; privacy fails closed.
- Camera timestamps remain the render clock, preventing duplicate catch-up frames around hard cuts.
- Source media is immutable. Caches live in `build/` and `tmp/`; release artifacts live in categorized `output/` subdirectories.

`final-edits.json` is generated by the FAQ stage. Put hand edits in the manifest's `base_edits` file; the generated EDL records its inputs and should not be edited directly. FAQ labels, accent color, optional font, Whisper paths, analyzer choice, and chapter opt-out are manifest settings. Person detectors can be replaced with `privacy_detector_command`, whose command receives `{inputs}` and `{output}` placeholders for the existing TSV contract. A non-macOS privacy analysis without such a qualified command stops instead of selecting an untested model.

Contributor checks, including a generated project in a path containing an apostrophe, run with:

```sh
make test
```

Use `scripts/score-detections.py` against labeled count TSVs before adopting a detector replacement; its default gates prioritize any-person and overlapping-person recall. Bind the passing result to the exact command used by the manifest:

```sh
python3 scripts/score-detections.py labels.tsv --inputs inputs.tsv \
  --detector-command '/usr/bin/python3 /opt/people-detector.py {inputs} {output}' \
  --detector-artifact /opt/people-detector.py \
  --qualification-output build/detector-qualification.json
```

Set `privacy_detector_qualification` to that artifact and list every implementation and model file in `privacy_detector_artifacts`. The command and file paths must match the manifest exactly; changing any bound file invalidates qualification.
Qualification output preserves the detector TSV so recall is recomputed at every gate. A maintainer must also approve the exact label/input/detection hashes, normalized command identity, and detector-artifact hashes in `privacy-detector-trust.json`; project files cannot extend that trust store. The shipped store has no production detector entries, so non-macOS production fails closed until a real backend and representative labeled dataset are reviewed.

`video-project.example.json` documents all reusable settings. Local project folders and review decisions are ignored by Git; branded layout templates and generator code are versioned.

See [VISION.md](VISION.md) for product direction and [AGENTS.md](AGENTS.md) for contributor rules.

## License

The generator and templates are licensed under [MIT](LICENSE). The Bitcoin Austria name, marks, and listed [brand assets](BRAND_ASSETS.md) are expressly excluded from that license.
