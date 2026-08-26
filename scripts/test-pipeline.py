#!/usr/bin/env python3

import contextlib
import importlib.util
import io
import json
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

from video_common import atomic_write_json, atomic_write_text


ROOT = Path(__file__).resolve().parent.parent
DURATION = 4.0


def run(*command: str) -> None:
    subprocess.run(command, check=True)


def make_video(path: Path) -> None:
    run(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size=1920x1080:rate=30:duration={DURATION}",
        "-f", "lavfi", "-i",
        f"aevalsrc=0.15*sin(2*PI*440*t)|0.15*sin(2*PI*660*t):s=48000:d={DURATION}",
        "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ac", "2", str(path),
    )


def make_mask(path: Path, color: str, white_interval: tuple[float, float] | None = None) -> None:
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"color={color}:size=960x540:rate=30:duration={DURATION}",
    ]
    if white_interval:
        command.extend((
            "-vf",
            f"negate=enable=between(t\\,{white_interval[0]}\\,{white_interval[1]})",
        ))
    run(
        *command,
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "0", "-pix_fmt", "yuv420p",
        str(path),
    )


def sample_pixel(path: Path, x: int, y: int) -> tuple[int, int, int]:
    frame = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", "0.1", "-i", str(path),
            "-vf", f"crop=2:2:{x}:{y},format=rgb24", "-frames:v", "1", "-f", "rawvideo", "-",
        ],
        check=True,
        capture_output=True,
    ).stdout
    assert len(frame) == 12
    return tuple(frame[:3])


def fixture(directory: Path) -> Path:
    for name in (
        "source", "build/slides", "build/privacy", "build/faq-analysis/fixture/cards",
        "output/final", "output/metadata", "output/debug/previews", "tmp",
    ):
        (directory / name).mkdir(parents=True, exist_ok=True)

    make_video(directory / "source/video.mp4")
    make_mask(directory / "build/privacy/mask.mp4", "black")
    make_mask(directory / "build/privacy/full-blur.mp4", "white")

    background = Image.new("RGB", (1920, 1080), "#171717")
    background.save(directory / "background.png")
    slide = Image.new("RGB", (1920, 1080), "#df2020")
    draw = ImageDraw.Draw(slide)
    draw.rectangle((120, 120, 1800, 960), outline="#eb0028", width=20)
    draw.text((180, 180), "Synthetic pipeline", fill="black")
    slide.save(directory / "build/slides/page-01.jpg", quality=90)
    slide.save(directory / "source/slides.pdf", "PDF")
    Image.new("RGB", (1920, 1080), "#20df20").save(
        directory / "build/slides/page-02.jpg", quality=90
    )
    card = Image.new("RGB", (1920, 1080), "#202020")
    ImageDraw.Draw(card).text((300, 500), "AUDIENCE QUESTION", fill="white")
    card.save(directory / "build/faq-analysis/fixture/cards/faq-01-full-cover.png")
    atomic_write_text(directory / "tmp/slides.txt", "Synthetic pipeline\n")

    atomic_write_json(
        directory / "build/speaker-track.json",
        [{"time": 0.0, "x": 0.0}, {"time": DURATION, "x": 0.0}],
    )
    atomic_write_json(
        directory / "build/timeline.json",
        {
            "duration": DURATION,
            "website_until": 0.5,
            "slides": [{"time": 0.5, "page": 1}, {"time": 2.0, "page": 2}],
            "speaker_track": "build/speaker-track.json",
            "source_width": 1920,
            "source_height": 1080,
            "speaker_crop": {"width": 864, "height": 1080, "y": 0},
            "screen_crop": {"x": 0, "y": 0, "width": 1920, "height": 1080},
        },
    )
    atomic_write_json(
        directory / "final-edits.json",
        {
            "version": 1,
            "generated_by": "synthetic fixture",
            "source": {"path": "source/video.mp4"},
            "edits": [
                {
                    "source_start": 1.0,
                    "source_end": 1.2,
                    "types": ["audience_question"],
                    "transition_ms": 45,
                }
            ],
        },
    )
    atomic_write_json(
        directory / "faq-timeline.json",
        {
            "version": 1,
            "source": {"path": "source/video.mp4"},
            "entries": [
                {
                    "source_start": 1.2,
                    "duration": 0.5,
                    "image": "build/faq-analysis/fixture/cards/faq-01-full-cover.png",
                    "question": "Does the fixture pass?",
                }
            ],
        },
    )
    analysis = {
        "turns": [
            {
                "segment_ids": [1],
                "kind": "faq",
                "question": "Does the fixture pass?",
                "source_start": 1.0,
                "source_end": 1.2,
                "answer_start": 1.2,
                "answer_end": 3.8,
                "confidence": 1.0,
            }
        ],
        "ignored_candidates": [],
    }
    atomic_write_json(directory / "build/faq-analysis/fixture/model-analysis.json", analysis)
    atomic_write_json(directory / "shorts.json", {"version": 1, "clips": []})

    project = {
        "name": "fixture",
        "presentation_title": "Synthetic pipeline",
        "speaker": "Test",
        "organization": "Fixture",
        "language": "en",
        "video": "source/video.mp4",
        "slides_pdf": "source/slides.pdf",
        "slides_text": "tmp/slides.txt",
        "background": "background.png",
        "timeline": "build/timeline.json",
        "slides": "build/slides",
        "edl": "final-edits.json",
        "faq": "faq-timeline.json",
        "faq_scan_start": 0.0,
        "faq_card_duration": 0.5,
        "faq_card_min_answer_seconds": 0.1,
        "faq_expected_min_turns": 1,
        "privacy_mask": "build/privacy/mask.mp4",
        "full_blur_mask": "build/privacy/full-blur.mp4",
        "presentation_start": 0.0,
        "shorts_manifest": "shorts.json",
        "final_output": "output/final/final.mp4",
        "final_metadata": "output/metadata/final-render.json",
        "final_resolution": "1920x1080",
        "chapters_enabled": False,
        "rebuild_analysis_before_final": False,
        "encoder": "libx264",
        "preview_preset": "ultrafast",
        "final_preset": "ultrafast",
    }
    detector_command = [
        sys.executable,
        str(ROOT / "scripts/test-people-detector.py"),
        "{inputs}",
        "{output}",
    ]
    project["privacy_detector_command"] = detector_command
    project["privacy_detector_artifacts"] = [str(ROOT / "scripts/test-people-detector.py")]
    project["privacy_detector_qualification"] = "build/detector-qualification.json"
    run(
        sys.executable,
        str(ROOT / "scripts/score-detections.py"),
        str(ROOT / "tests/privacy-detector/labels.tsv"),
        "--inputs",
        str(ROOT / "tests/privacy-detector/inputs.tsv"),
        "--detector-command",
        shlex.join(detector_command),
        "--detector-artifact",
        project["privacy_detector_artifacts"][0],
        "--qualification-output",
        str(directory / project["privacy_detector_qualification"]),
    )
    project_file = directory / "project.json"
    atomic_write_json(project_file, project)
    return project_file


def main() -> None:
    (ROOT / "build").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="meetup-fixture-'", dir=ROOT / "build") as temporary:
        temporary_path = Path(temporary)
        initialized = temporary_path / "initialized/project.json"
        run(
            sys.executable,
            str(ROOT / "scripts/meetup-video.py"),
            "--project",
            str(initialized),
            "init",
            "--name",
            "Example meetup",
            "--event-url",
            "https://example.com/events/meetup",
        )
        initialized_project = json.loads(initialized.read_text(encoding="utf-8"))
        assert initialized_project["event_url"] == "https://example.com/events/meetup"
        assert initialized_project["acceleration"] == "auto" and "encoder" not in initialized_project
        capabilities = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/meetup-video.py"),
                "--project",
                str(initialized),
                "capabilities",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        selected_encoder = json.loads(capabilities.stdout)["video_encoder"]["name"]
        assert selected_encoder == "libx264" or selected_encoder.startswith("h264_")
        invalid = temporary_path / "invalid/project.json"
        rejected = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/meetup-video.py"),
                "--project",
                str(invalid),
                "init",
                "--name",
                "Invalid",
                "--event-url",
                "file:///etc/passwd",
            ],
            capture_output=True,
            text=True,
        )
        assert rejected.returncode and "absolute HTTP(S) URL" in rejected.stderr
        assert not invalid.parent.exists()
        project = fixture(temporary_path)
        command = [sys.executable, str(ROOT / "scripts/meetup-video.py"), "--project", str(project)]
        run(*command, "privacy-seal", "--reviewed-by", "synthetic test")
        timeline_path = project.parent / "build/timeline.json"
        timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
        atomic_write_json(timeline_path, {**timeline, "duration": DURATION - 0.5})
        bad_duration = subprocess.run([*command, "check"], capture_output=True, text=True)
        assert bad_duration.returncode and "timeline duration" in bad_duration.stderr
        atomic_write_json(timeline_path, timeline)
        run(*command, "check")
        run(*command, "preview", "--duration", str(DURATION))
        run(
            *command,
            "validate",
            "--input",
            str(project.parent / "output/debug/previews/preview-1080p.mp4"),
        )
        late_preview = project.parent / "output/debug/previews/late-slide.mp4"
        run(
            *command, "preview", "--start", "2.5", "--duration", "1.5",
            "--output", str(late_preview),
        )
        red, green, blue = sample_pixel(late_preview, 700, 400)
        assert green > 180 and red < 80 and blue < 80, (red, green, blue)
        unapproved = subprocess.run([*command, "final"], capture_output=True, text=True)
        assert unapproved.returncode and "not explicitly approved" in unapproved.stderr
        run(*command, "approve")
        edl = project.parent / "final-edits.json"
        approved_edl = edl.read_bytes()
        edl.write_bytes(approved_edl + b" ")
        stale = subprocess.run([*command, "final"], capture_output=True, text=True)
        assert stale.returncode and "preview approval is missing or stale" in stale.stderr
        edl.write_bytes(approved_edl)
        run(*command, "final")
        run(*command, "validate", "--resolution", "1920x1080")
        final = project.parent / "output/final/final.mp4"
        level = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                "stream=level", "-of", "default=nw=1:nk=1", str(final),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        assert int(level.stdout.strip()) <= 42, level.stdout
        make_mask(
            project.parent / "build/privacy/full-blur.mp4",
            "black",
            (1.0, 1.19),
        )
        spec = importlib.util.spec_from_file_location("meetup_video", ROOT / "scripts/meetup-video.py")
        assert spec and spec.loader
        meetup_video = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(meetup_video)
        loaded_project = json.loads(project.read_text(encoding="utf-8"))
        loaded_project["_project_dir"] = str(project.parent)
        privacy_output = io.StringIO()
        with contextlib.redirect_stdout(privacy_output):
            meetup_video.validate_privacy_render(final, loaded_project, "1920x1080")
        assert "all 1 full-blur intervals are removed by the EDL" in privacy_output.getvalue()
        stale_privacy = subprocess.run(
            [*command, "validate", "--resolution", "1920x1080"],
            capture_output=True,
            text=True,
        )
        assert stale_privacy.returncode and "privacy provenance is stale" in stale_privacy.stderr
    print("synthetic check -> preview -> final -> validate passed")


if __name__ == "__main__":
    main()
