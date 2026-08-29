#!/usr/bin/env python3

import contextlib
import importlib.util
import io
import json
import math
import os
import shlex
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

import numpy as np
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
        "-vf",
        r"drawbox=x=40:y=120:w=360:h=840:color=blue:t=fill,"
        r"drawbox=x=1520:y=120:w=360:h=840:color=yellow:t=fill:enable='not(between(t\,1.5\,2.5))',"
        r"settb=expr=1/90000,setpts=floor(N/2)*6000+mod(N\,2)*900",
        "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-fps_mode", "passthrough", "-enc_time_base:v", "demux",
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


def sample_pixel(path: Path, x: int, y: int, timestamp: float = 0.1) -> tuple[int, int, int]:
    frame = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(timestamp), "-i", str(path),
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
    Image.new("RGB", (1920, 1080), "#7020df").save(directory / "end-card.png")
    Image.new("RGBA", (240, 240), "#ffffff").save(directory / "shorts-logo.png")
    Image.new("RGB", (1080, 1920), "#20df20").save(directory / "shorts-end-card.png")
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
        directory / "build/host-a-track.json",
        [
            {"time": 0.0, "x": 0.0, "visible": True, "box": [0.02, 0.1, 0.19, 0.78]},
            {"time": DURATION, "x": 0.0, "visible": True, "box": [0.02, 0.1, 0.19, 0.78]},
        ],
    )
    atomic_write_json(
        directory / "build/host-b-track.json",
        [
            {"time": 0.0, "x": 1520.0, "visible": True, "box": [0.79, 0.1, 0.19, 0.78]},
            {"time": 1.5, "x": 1520.0, "visible": False, "box": None},
            {"time": 2.5, "x": 1520.0, "visible": True, "box": [0.79, 0.1, 0.19, 0.78]},
            {"time": DURATION, "x": 1520.0, "visible": True, "box": [0.79, 0.1, 0.19, 0.78]},
        ],
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
            "participants": {
                "host_a": {
                    "track": "build/host-a-track.json",
                    "crop": {"width": 400, "height": 1080, "y": 0},
                    "audio_channel": 1,
                },
                "host_b": {
                    "track": "build/host-b-track.json",
                    "crop": {"width": 400, "height": 1080, "y": 0},
                    "audio_channel": 2,
                },
            },
            "layout_sections": [
                {
                    "source_start": 0.0,
                    "source_end": 2.0,
                    "kind": "intro",
                    "layout": "dual_speaker",
                    "left": "host_a",
                    "right": "host_b",
                    "active": "left",
                },
                {
                    "source_start": 2.0,
                    "source_end": 2.5,
                    "kind": "talk",
                    "layout": "standard",
                    "audio_channel": 1,
                },
                {
                    "source_start": 2.5,
                    "source_end": DURATION,
                    "kind": "discussion",
                    "layout": "dual_speaker",
                    "left": "host_a",
                    "right": "host_b",
                    "active": "right",
                },
            ],
            "mix_mapped_microphones": True,
            "microphone_mix": {"inactive_gain": 0.18, "both_gain": 0.5, "fade_seconds": 0.12},
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
        "end_card": "end-card.png",
        "end_card_duration": 0.25,
        "shorts_logo": "shorts-logo.png",
        "shorts_end_card": "shorts-end-card.png",
        "shorts_end_card_duration": 0.25,
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
        raw_video = temporary_path / "raw-video.mp4"
        raw_slides = temporary_path / "raw-slides.pdf"
        raw_video.write_bytes(b"video")
        raw_slides.write_bytes(b"slides")
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
            "--template",
            "own-your-ai",
            "--video",
            str(raw_video),
            "--slides-pdf",
            str(raw_slides),
        )
        initialized_project = json.loads(initialized.read_text(encoding="utf-8"))
        assert initialized_project["event_url"] == "https://example.com/events/meetup"
        assert initialized_project["organization_template"] == "own-your-ai"
        assert initialized_project["organization"] == "Own Your AI"
        assert initialized_project["organization_url"] == "https://luma.com/ownyourai"
        assert initialized_project["announcement_label"] == "Community announcement"
        for key in ("background", "shorts_logo", "end_card", "shorts_end_card"):
            assert initialized_project[key].startswith("assets/")
            assert (initialized.parent / initialized_project[key]).is_file()
        assert initialized_project["end_card_duration"] == 6.0
        assert initialized_project["shorts_end_card_duration"] == 2.5
        assert (initialized.parent / initialized_project["video"]).resolve() == raw_video
        assert (initialized.parent / initialized_project["slides_pdf"]).resolve() == raw_slides
        for artifact in ("manual-edits.json", "final-edits.json", "faq-timeline.json"):
            assert json.loads((initialized.parent / artifact).read_text(encoding="utf-8"))["source"] == {
                "path": initialized_project["video"]
            }
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
        invalid_template = temporary_path / "invalid-template/project.json"
        rejected = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/meetup-video.py"),
                "--project",
                str(invalid_template),
                "init",
                "--name",
                "Invalid template",
                "--template",
                "../own-your-ai",
            ],
            capture_output=True,
            text=True,
        )
        assert rejected.returncode and "invalid organization template" in rejected.stderr
        assert not invalid_template.parent.exists()
        missing = temporary_path / "missing/project.json"
        rejected = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/meetup-video.py"),
                "--project",
                str(missing),
                "init",
                "--name",
                "Missing source",
                "--video",
                str(temporary_path / "does-not-exist.mp4"),
            ],
            capture_output=True,
            text=True,
        )
        assert rejected.returncode and "does not exist" in rejected.stderr
        assert not missing.parent.exists()
        project = fixture(temporary_path)
        command = [sys.executable, str(ROOT / "scripts/meetup-video.py"), "--project", str(project)]
        spec = importlib.util.spec_from_file_location("meetup_video", ROOT / "scripts/meetup-video.py")
        assert spec and spec.loader
        meetup_video = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(meetup_video)
        assert meetup_video.append_publisher_attribution(
            {"organization": "Own Your AI", "organization_url": "https://luma.com/ownyourai"},
            "Description",
        ) == "Description\n\nOwn Your AI: https://luma.com/ownyourai"
        assert meetup_video.append_publisher_attribution(
            {"organization": "Own Your AI", "organization_url": "https://luma.com/ownyourai"},
            "Description https://luma.com/ownyourai",
        ) == "Description https://luma.com/ownyourai"
        assert meetup_video.decorate_publishing_description(
            {
                "organization": "Own Your AI",
                "organization_url": "https://luma.com/ownyourai",
                "publishing_intro": "Join us: https://luma.com/ownyourai",
            },
            "Description",
        ) == "Join us: https://luma.com/ownyourai\n\nDescription"
        assert meetup_video.audio_render_policy(
            {
                "channel_analysis": {
                    "classification": "dual_mono",
                    "render_policy": "process_once_then_duplicate_to_stereo",
                    "analysis_channels": [1],
                }
            }
        ) == "process_once_then_duplicate_to_stereo"
        try:
            meetup_video.audio_render_policy({"channel_analysis": {}})
        except SystemExit:
            pass
        else:
            raise AssertionError("missing audio channel policy must fail closed")
        dead_lock = temporary_path / "dead.lock"
        dead_lock.mkdir()
        atomic_write_json(dead_lock / "owner.json", {"pid": 999_999_999})
        dead_handle = meetup_video.acquire_render_lock(dead_lock)
        assert json.loads((dead_lock / "owner.json").read_text(encoding="utf-8"))["pid"] == os.getpid()
        meetup_video.release_render_lock(dead_lock, dead_handle)
        contended_lock = temporary_path / "contended.lock"
        contended_handle = meetup_video.acquire_render_lock(contended_lock)
        try:
            meetup_video.acquire_render_lock(contended_lock)
        except SystemExit:
            pass
        else:
            raise AssertionError("OS-held render lock must reject a second contender")
        meetup_video.release_render_lock(contended_lock, contended_handle)
        current_identity = {
            "sha256": "current",
            "render": {"contract": 1},
            "files": {"renderer": {"sha256": "same"}},
        }
        legacy_identity = {
            "sha256": "legacy",
            "files": {
                "renderer": {"sha256": "same"},
                "controller": {"sha256": "orchestration-only"},
                "common": {"sha256": "legacy-whole-file-hash"},
            },
        }
        assert meetup_video.comparable_render_identity(
            current_identity
        ) == meetup_video.comparable_render_identity(legacy_identity)
        debug_file = temporary_path / "output/debug/preview.mp4"
        debug_file.parent.mkdir(parents=True, exist_ok=True)
        debug_file.write_bytes(b"review")
        legacy_guard = temporary_path / "output/final/final.mp4.lock.guard"
        legacy_guard.parent.mkdir(parents=True, exist_ok=True)
        legacy_guard.touch()
        finder_metadata = temporary_path / "output/.DS_Store"
        finder_metadata.touch()
        active_lock = temporary_path / "output/final/active.mp4.lock"
        active_lock.mkdir()
        active_guard = Path(f"{active_lock}.guard")
        active_guard.touch()
        meetup_video.clean_debug({"_project_dir": str(temporary_path)})
        assert (
            not debug_file.parent.exists()
            and not legacy_guard.exists()
            and not finder_metadata.exists()
            and active_guard.exists()
        )
        active_guard.unlink()
        active_lock.rmdir()
        for owner in ({"pid": os.getpid()}, None):
            blocked_lock = temporary_path / f"blocked-{owner is None}.lock"
            blocked_lock.mkdir()
            if owner is not None:
                atomic_write_json(blocked_lock / "owner.json", owner)
            try:
                meetup_video.acquire_render_lock(blocked_lock)
            except SystemExit:
                pass
            else:
                raise AssertionError("live and malformed render locks must fail closed")
            if owner is not None:
                (blocked_lock / "owner.json").unlink()
            blocked_lock.rmdir()
        assert meetup_video.representative_frame_samples(
            [index / 30 for index in range(8)]
        ) == [3 / 30, 4 / 30]
        guarded = meetup_video.representative_frame_samples(
            [index / 30 for index in range(46)]
        )
        assert min(guarded) >= 0.375 and max(guarded) <= 1.125
        loaded_project = json.loads(project.read_text(encoding="utf-8"))
        loaded_project["_project_dir"] = str(project.parent)
        transition_samples = meetup_video.full_blur_sample_groups(
            loaded_project,
            [[1.8, 1.9, 1.967, 2.0, 2.033, 2.1, 2.2]],
        )
        assert all(abs(sample - 2.0) >= 0.1 for group in transition_samples for sample in group)
        profile = meetup_video.host_capabilities(loaded_project, refresh=True)
        assert not profile["privacy_detector"]["qualified"]
        test_profile = json.loads(json.dumps(profile))
        test_profile["privacy_detector"].update(
            {"available": True, "qualified": True, "reason": "synthetic test injection"}
        )
        with patch.object(meetup_video, "host_capabilities", return_value=test_profile):
            meetup_video.seal_privacy(loaded_project, "synthetic test")
        timeline_path = project.parent / "build/timeline.json"
        timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
        atomic_write_json(timeline_path, {**timeline, "duration": DURATION - 0.5})
        with patch.object(meetup_video, "host_capabilities", return_value=test_profile):
            try:
                meetup_video.check(loaded_project, project)
            except SystemExit as error:
                assert "timeline" in str(error) or "layout section" in str(error)
            else:
                raise AssertionError("stale timeline duration must fail")
        atomic_write_json(timeline_path, timeline)
        with patch.object(meetup_video, "host_capabilities", return_value=test_profile):
            meetup_video.check(loaded_project, project)
        run(*command, "preview", "--duration", str(DURATION))
        preview = project.parent / "output/debug/previews/preview-1080p.mp4"
        run(
            *command,
            "validate",
            "--input",
            str(preview),
        )
        left = sample_pixel(preview, 170, 540, 0.8)
        center = sample_pixel(preview, 960, 540, 0.8)
        right = sample_pixel(preview, 1730, 540, 0.8)
        assert left[2] > left[0] + 30 and left[2] > left[1] + 30, left
        assert center[0] > center[1] + 100 and center[0] > center[2] + 100, center
        assert min(right[:2]) > right[2] + 40, right
        absent = sample_pixel(preview, 1730, 540, 2.05)
        assert max(absent) - min(absent) < 20 and max(absent) < 80, absent
        returned = sample_pixel(preview, 1730, 540, 3.05)
        assert min(returned[:2]) > returned[2] + 40, returned
        late_preview = project.parent / "output/debug/previews/late-slide.mp4"
        relative_late_preview = os.path.relpath(late_preview, Path.cwd())
        run(
            *command, "preview", "--start", "2.5", "--duration", "1.5",
            "--output", relative_late_preview,
        )
        red, green, blue = sample_pixel(late_preview, 700, 400)
        assert green > 180 and red < 80 and blue < 80, (red, green, blue)
        unapproved = subprocess.run([*command, "final"], capture_output=True, text=True)
        assert unapproved.returncode and "not explicitly approved" in unapproved.stderr
        run(*command, "approve")
        participant_track = project.parent / "build/host-b-track.json"
        approved_track = participant_track.read_bytes()
        participant_track.write_bytes(approved_track + b" ")
        stale = subprocess.run([*command, "final"], capture_output=True, text=True)
        assert stale.returncode and "preview approval is missing or stale" in stale.stderr
        with patch.object(meetup_video, "host_capabilities", return_value=test_profile):
            try:
                meetup_video.check(loaded_project, project)
            except SystemExit as error:
                assert "privacy provenance is stale" in str(error)
            else:
                raise AssertionError("participant-track changes must invalidate privacy review")
        participant_track.write_bytes(approved_track)
        edl = project.parent / "final-edits.json"
        approved_edl = edl.read_bytes()
        edl.write_bytes(approved_edl + b" ")
        stale = subprocess.run([*command, "final"], capture_output=True, text=True)
        assert stale.returncode and "preview approval is missing or stale" in stale.stderr
        edl.write_bytes(approved_edl)
        with patch.object(meetup_video, "host_capabilities", return_value=test_profile):
            meetup_video.final_render(loaded_project, project)
        with (
            patch.object(meetup_video, "host_capabilities", return_value=test_profile),
            patch.object(meetup_video, "render", side_effect=AssertionError("final cache missed")),
        ):
            meetup_video.final_render(loaded_project, project)
        assert not list((project.parent / "output/final").glob("*.lock.guard"))
        assert list((project.parent / "build/locks").glob("*.lock.guard"))
        preflight = json.loads(
            (project.parent / "build/privacy-preflight.json").read_text(encoding="utf-8")
        )
        assert preflight["status"] == "passed" and preflight["clips"]
        with patch.object(meetup_video, "render", side_effect=AssertionError("cache missed")):
            meetup_video.privacy_preflight(
                loaded_project,
                project,
                identity={"sha256": preflight["identity_sha256"]},
            )
        run(*command, "validate", "--resolution", "1920x1080")
        final = project.parent / "output/final/final.mp4"
        decoded_audio = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(final),
                "-map", "0:a:0", "-f", "s16le", "-acodec", "pcm_s16le", "-",
            ],
            check=True,
            capture_output=True,
        ).stdout
        samples = np.frombuffer(decoded_audio, dtype=np.int16).reshape(-1, 2)
        assert np.corrcoef(samples[:, 0], samples[:, 1])[0, 1] > 0.999
        peak_db = 20 * math.log10(np.max(np.abs(samples.astype(np.int32))) / 32768)
        assert peak_db <= -1.5, peak_db
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
        shorts_spec = importlib.util.spec_from_file_location(
            "render_shorts", ROOT / "scripts/render-shorts.py"
        )
        assert shorts_spec and shorts_spec.loader
        render_shorts = importlib.util.module_from_spec(shorts_spec)
        shorts_spec.loader.exec_module(render_shorts)
        short_output = project.parent / "output/shorts/cta-test.mp4"
        short_output.parent.mkdir(parents=True, exist_ok=True)
        short_project = {**loaded_project, "_video_encoder": "libx264", "_render_threads": 2}

        def fake_subtitles(_transcript, path, _replacements=None):
            path.write_text("1\n00:00:00,000 --> 00:00:00,500\nTest\n", encoding="utf-8")

        with (
            patch.object(render_shorts, "transcribe", return_value=[]),
            patch.object(render_shorts, "make_subtitles", side_effect=fake_subtitles),
        ):
            render_shorts.render_clip(
                project.parent / "source/video.mp4",
                final,
                project.parent / "shorts-logo.png",
                project.parent / "shorts-end-card.png",
                0.25,
                project.parent / "build/privacy/mask.mp4",
                project.parent / "build/privacy/full-blur.mp4",
                {"id": "cta-test", "source_start": 2.6, "duration": 0.5},
                short_project,
                short_output,
                project.parent / "build/shorts-test",
                threading.Semaphore(1),
            )
        short_green = sample_pixel(short_output, 540, 960, 0.65)
        assert short_green[1] > short_green[0] + 100 and short_green[1] > short_green[2] + 100
        packet_times = [
            float(value)
            for value in subprocess.run(
                [
                    "ffprobe", "-v", "error", "-select_streams", "v:0",
                    "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(final),
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
            if value
        ]
        assert len(packet_times) > 100
        assert all(right > left for left, right in zip(packet_times, packet_times[1:]))
        make_mask(
            project.parent / "build/privacy/full-blur.mp4",
            "black",
            (1.0, 1.19),
        )
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
