#!/usr/bin/env python3

import argparse
import json
import math
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from statistics import median

from PIL import Image, ImageDraw, ImageStat

from video_common import atomic_write_json, atomic_write_text, ffconcat_quote, resolve_project_path


ROOT = Path(__file__).resolve().parent.parent
DETECT_FPS = 10
MASK_FPS = 30
MASK_SIZE = (960, 540)
FULL_BLUR_PADDING_FRAMES = round(1.5 * DETECT_FPS)


@dataclass
class Box:
    x: float
    y: float
    width: float
    height: float
    luma: float = 0.0
    contrast: float = 0.0

    @property
    def center(self) -> float:
        return self.x + self.width / 2


def run(command: list[str]) -> None:
    environment = os.environ.copy()
    module_cache = str(Path(tempfile.gettempdir()) / "privacy-review-swift-cache")
    environment["CLANG_MODULE_CACHE_PATH"] = module_cache
    environment["SWIFT_MODULECACHE_PATH"] = module_cache
    subprocess.run(command, check=True, env=environment)


def parse_boxes(encoded: str) -> list[Box]:
    boxes = []
    for item in encoded.split(";"):
        if item:
            boxes.append(Box(*map(float, item.split(","))))
    return [box for box in boxes if box.height >= 0.12]


def read_detections(path: Path) -> list[tuple[float, list[Box]]]:
    rows = []
    for row in path.read_text(encoding="utf-8").splitlines():
        timestamp, encoded = (row.split("\t", 1) + [""])[:2]
        rows.append((float(timestamp), parse_boxes(encoded)))
    return rows


def privacy_action(speaker: Box | None, others: list[Box], margin: float = 0.035) -> str:
    if speaker is None:
        return "full-blur"
    for other in others:
        horizontal_gap = max(0.0, max(speaker.x, other.x) - min(speaker.x + speaker.width, other.x + other.width))
        vertical_overlap = min(speaker.y + speaker.height, other.y + other.height) - max(speaker.y, other.y)
        if horizontal_gap <= margin and vertical_overlap > 0.05:
            return "full-blur"
    return "blur-others"


def problem_windows(path: Path, duration: float, padding: float) -> list[tuple[float, float]]:
    times = [timestamp for timestamp, boxes in read_detections(path) if len(boxes) > 1]
    groups: list[list[float]] = []
    for timestamp in times:
        if not groups or timestamp - groups[-1][-1] > 1.01:
            groups.append([timestamp])
        else:
            groups[-1].append(timestamp)
    return [
        (max(0, group[0] - padding), min(duration, group[-1] + 1 + padding))
        for group in groups
    ]


def speaker_and_others(
    detections: list[tuple[float, list[Box]]],
    reference: tuple[float, float],
) -> tuple[list[Box | None], list[list[Box]]]:
    singles = [(index, boxes[0]) for index, (_time, boxes) in enumerate(detections) if len(boxes) == 1]
    if not singles:
        raise SystemExit("privacy window has no single-person anchor")
    first_index, first = singles[0]
    last_index, last = singles[-1]
    reference_luma, reference_contrast = reference
    speakers: list[Box | None] = []
    others: list[list[Box]] = []

    for index, (_time, boxes) in enumerate(detections):
        span = max(1, last_index - first_index)
        ratio = max(0.0, min(1.0, (index - first_index) / span))
        expected = first.center + ratio * (last.center - first.center)
        ranked = sorted((
            (
                abs(box.center - expected)
                + 1.50 * abs(box.luma - reference_luma) / 255
                + 0.20 * abs(box.contrast - reference_contrast) / 128,
                box,
            )
            for box in boxes
        ), key=lambda item: item[0])
        speaker = ranked[0][1] if ranked else None
        if ranked and (ranked[0][0] > 0.24 or (len(ranked) > 1 and ranked[1][0] - ranked[0][0] < 0.06)):
            speaker = None
        speakers.append(speaker)
        others.append([box for box in boxes if box is not speaker])
    return speakers, others


def interpolate(left: list[Box], right: list[Box], ratio: float) -> list[Box]:
    if not left:
        return right
    if not right:
        return left
    left = sorted(left, key=lambda box: box.center)
    right = sorted(right, key=lambda box: box.center)
    if len(left) != len(right):
        return left if ratio < 0.5 else right
    return [
        Box(
            a.x + (b.x - a.x) * ratio,
            a.y + (b.y - a.y) * ratio,
            a.width + (b.width - a.width) * ratio,
            a.height + (b.height - a.height) * ratio,
        )
        for a, b in zip(left, right, strict=True)
    ]


def add_appearance(frames: list[Path], detections: list[tuple[float, list[Box]]]) -> None:
    for frame, (_time, boxes) in zip(frames, detections, strict=True):
        with Image.open(frame) as image:
            gray = image.convert("L")
            width, height = gray.size
            for box in boxes:
                left = round((box.x + box.width * 0.20) * width)
                right = round((box.x + box.width * 0.80) * width)
                top = round((1 - box.y - box.height * 0.72) * height)
                bottom = round((1 - box.y - box.height * 0.28) * height)
                stats = ImageStat.Stat(gray.crop((left, top, right, bottom)))
                box.luma = stats.mean[0]
                box.contrast = stats.stddev[0]


def speaker_reference(detections: Path, samples: Path) -> tuple[float, float]:
    rows = [row for row in read_detections(detections) if len(row[1]) == 1]
    rows = rows[::max(1, len(rows) // 100)]
    pairs = [
        (row, samples / f"frame-{round(row[0]) + 1:05d}.jpg")
        for row in rows
        if (samples / f"frame-{round(row[0]) + 1:05d}.jpg").exists()
    ]
    if not pairs:
        raise SystemExit("privacy review has no usable single-person reference samples")
    selected_rows = [row for row, _frame in pairs]
    add_appearance([frame for _row, frame in pairs], selected_rows)
    return (
        median(row[1][0].luma for row in selected_rows),
        median(row[1][0].contrast for row in selected_rows),
    )


def draw_mask(boxes: list[Box], path: Path) -> None:
    width, height = MASK_SIZE
    image = Image.new("L", MASK_SIZE, 0)
    draw = ImageDraw.Draw(image)
    for box in boxes:
        x1 = max(0.0, box.x - box.width * 0.18 - 0.01)
        x2 = min(1.0, box.x + box.width * 1.18 + 0.01)
        top = max(0.0, 1.0 - box.y - box.height * 1.10)
        bottom = min(1.0, top + box.height * 2.05)
        draw.rounded_rectangle(
            (round(x1 * width), round(top * height), round(x2 * width), round(bottom * height)),
            radius=max(8, round(box.width * width * 0.18)),
            fill=255,
        )
    image.save(path, compress_level=1)


def hold_unsafe(unsafe: list[bool]) -> list[bool]:
    held = unsafe[:]
    for index, value in enumerate(unsafe):
        if value:
            start = max(0, index - FULL_BLUR_PADDING_FRAMES)
            end = min(len(held), index + FULL_BLUR_PADDING_FRAMES + 1)
            held[start:end] = [True] * (end - start)
    return held


def make_mask(
    window_dir: Path, duration: float, speakers: list[Box | None], others: list[list[Box]]
) -> tuple[Path, Path]:
    masks = window_dir / "masks"
    cuts = window_dir / "cuts"
    masks.mkdir()
    cuts.mkdir()
    unsafe = [privacy_action(speaker, people) == "full-blur" for speaker, people in zip(speakers, others, strict=True)]
    held = hold_unsafe(unsafe)
    frame_count = math.ceil(duration * MASK_FPS)
    for frame in range(frame_count):
        sample = frame * DETECT_FPS / MASK_FPS
        left = min(len(others) - 1, int(sample))
        right = min(len(others) - 1, left + 1)
        draw_mask(interpolate(others[left], others[right], sample - left), masks / f"frame-{frame + 1:05d}.png")
        Image.new("L", MASK_SIZE, 255 if held[left] or held[right] else 0).save(
            cuts / f"frame-{frame + 1:05d}.png", compress_level=1
        )

    output = window_dir / "mask.mp4"
    cut_output = window_dir / "full-blur.mp4"
    for source, target in ((masks, output), (cuts, cut_output)):
        temporary = target.with_name(f".{target.stem}.tmp{target.suffix}")
        try:
            run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-framerate", str(MASK_FPS), "-i", str(source / "frame-%05d.png"),
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "0", "-pix_fmt", "yuv420p",
                str(temporary),
            ])
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    shutil.rmtree(masks)
    shutil.rmtree(cuts)
    return output, cut_output


def extract_and_detect(
    video: Path,
    window_dir: Path,
    start: float,
    duration: float,
    detector_command: list[str],
) -> list[tuple[float, list[Box]]]:
    frames = window_dir / "frames"
    frames.mkdir(parents=True)
    run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}", "-i", str(video),
        "-t", f"{duration:.3f}", "-vf", f"fps={DETECT_FPS},scale={MASK_SIZE[0]}:{MASK_SIZE[1]}",
        "-q:v", "3", str(frames / "frame-%05d.jpg"),
    ])
    frame_paths = sorted(frames.glob("frame-*.jpg"))
    inputs = window_dir / "inputs.tsv"
    atomic_write_text(
        inputs,
        "".join(f"{index / DETECT_FPS:.3f}\t{path}\n" for index, path in enumerate(frame_paths)),
    )
    results = window_dir / "detections.tsv"
    run([part.format(inputs=inputs, output=results) for part in detector_command])
    detections = read_detections(results)
    add_appearance(frame_paths, detections)
    shutil.rmtree(frames)
    return detections


def corrected_track(
    base_track: Path,
    replacements: list[tuple[float, list[tuple[float, Box | None]]]],
    crop_width: int,
    source_width: int,
    output: Path,
) -> None:
    track = json.loads(base_track.read_text(encoding="utf-8"))
    for start, samples in replacements:
        sample_times = [start + time for time, _box in samples]
        for item in track:
            timestamp = float(item["time"])
            if not sample_times or timestamp < sample_times[0] or timestamp > sample_times[-1]:
                continue
            index = min(range(len(sample_times)), key=lambda i: abs(sample_times[i] - timestamp))
            nearby = [
                box.center for time, box in samples
                if box is not None and abs(start + time - timestamp) <= 0.45
            ]
            if nearby:
                center = sum(nearby) / len(nearby)
                item["x"] = round(
                    max(0, min(source_width - crop_width, center * source_width - crop_width / 2)),
                    2,
                )
    atomic_write_json(output, track)


def format_time(seconds: float) -> str:
    minutes, second = divmod(round(seconds), 60)
    hours, minute = divmod(minutes, 60)
    return f"{hours:02d}:{minute:02d}:{second:02d}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Render only multi-person privacy review passages.")
    parser.add_argument("--project", type=Path)
    parser.add_argument("--video", type=Path)
    parser.add_argument("--coarse-detections", type=Path)
    parser.add_argument("--samples", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--padding", type=float, default=1.25)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return

    project = {}
    if args.project:
        project = json.loads(args.project.read_text(encoding="utf-8"))
        project["_project_dir"] = str(args.project.resolve().parent)
    base = Path(project.get("_project_dir", ROOT))
    args.video = args.video or (
        resolve_project_path(project, project["video"]) if project.get("video") else None
    )
    if not args.video:
        raise SystemExit("--video or a project with video is required")
    args.coarse_detections = args.coarse_detections or resolve_project_path(
        project, project.get("coarse_detections", "tmp/people-results-upper-1s.tsv")
    )
    args.samples = args.samples or resolve_project_path(
        project, project.get("coarse_detection_samples", "tmp/people-samples-1s")
    )
    args.output = args.output or resolve_project_path(
        project,
        project.get(
            "privacy_review_output",
            "output/debug/privacy/problemstellen-privacy-test-1080p.mp4",
        ),
    )
    configured_detector = project.get("privacy_detector_command", project.get("people_detector"))
    detector_command = (
        configured_detector
        if isinstance(configured_detector, list)
        else shlex.split(configured_detector)
        if configured_detector
        else [
            str(ROOT / "scripts/vision-people.swift"),
            "--list",
            "{inputs}",
            "--output",
            "{output}",
        ]
    )
    detector_path = Path(detector_command[0])
    if not detector_path.is_absolute() and (base / detector_path).exists():
        detector_command[0] = str((base / detector_path).resolve())

    timeline_path = resolve_project_path(project, project.get("timeline", "timeline.json"))
    timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
    windows = problem_windows(args.coarse_detections, float(timeline["duration"]), args.padding)
    if not windows:
        raise SystemExit("no multi-person privacy windows found")
    reference = speaker_reference(args.coarse_detections, args.samples)
    build = base / "build/privacy-review"
    build.mkdir(parents=True, exist_ok=True)
    clips: list[Path] = []
    replacements: list[tuple[float, list[tuple[float, Box | None]]]] = []
    masks: list[Path] = []
    cuts: list[Path] = []

    for index, (start, end) in enumerate(windows, 1):
        window_dir = build / f"window-{index:02d}"
        if window_dir.exists():
            shutil.rmtree(window_dir)
        window_dir.mkdir(parents=True)
        detections = extract_and_detect(
            args.video, window_dir, start, end - start, detector_command
        )
        speakers, others = speaker_and_others(detections, reference)
        mask, cut = make_mask(window_dir, end - start, speakers, others)
        masks.append(mask)
        cuts.append(cut)
        replacements.append((start, [(time, box) for (time, _boxes), box in zip(detections, speakers, strict=True)]))

    track = build / "speaker-track.json"
    corrected_track(
        resolve_project_path(project, timeline["speaker_track"]),
        replacements,
        int(timeline["speaker_crop"]["width"]),
        int(timeline.get("source_width", timeline["screen_crop"]["width"])),
        track,
    )
    timeline["speaker_track"] = str(track)
    review_timeline = build / "timeline.json"
    atomic_write_json(review_timeline, timeline)

    for index, ((start, end), mask, cut) in enumerate(zip(windows, masks, cuts, strict=True), 1):
        clip = build / f"clip-{index:02d}.mp4"
        command = [
            sys.executable, str(ROOT / "scripts/render-video.py"), "--video", str(args.video),
            "--project-dir", str(base),
            "--timeline", str(review_timeline), "--start", f"{start:.3f}", "--duration", f"{end - start:.3f}",
            "--privacy-mask", str(mask), "--resolution", "1920x1080", "--encoder", "libx264",
            "--full-blur-mask", str(cut), "--privacy-mask-start", f"{start:.3f}",
            "--preset", "ultrafast", "--output", str(clip),
        ]
        if project.get("background"):
            command.extend(("--background", str(resolve_project_path(project, project["background"]))))
        if project.get("slides"):
            command.extend(("--slides", str(resolve_project_path(project, project["slides"]))))
        run(command)
        clips.append(clip)

    concat = build / "clips.ffconcat"
    atomic_write_text(
        concat,
        "ffconcat version 1.0\n"
        + "".join(f"file {ffconcat_quote(clip.resolve())}\n" for clip in clips),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-safe", "0", "-f", "concat", "-i", str(concat),
        "-c", "copy", "-movflags", "+faststart", str(args.output),
    ])
    print(f"{len(windows)} windows -> {args.output}")
    for start, end in windows:
        print(f"{format_time(start)}-{format_time(end)}")


def self_test() -> None:
    assert parse_boxes("0,0,1,1")[0].center == 0.5
    assert privacy_action(None, []) == "full-blur"


if __name__ == "__main__":
    main()
