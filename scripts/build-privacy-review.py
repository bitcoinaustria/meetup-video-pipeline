#!/usr/bin/env python3

import argparse
import itertools
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from statistics import median

from PIL import Image, ImageDraw, ImageStat

from video_common import (
    atomic_write_json,
    atomic_write_text,
    ffconcat_quote,
    host_capabilities,
    parse_detection_coordinates,
    privacy_detector_command,
    resolve_project_path,
    validate_speaker_track,
    validate_timeline,
)


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
    return [Box(*coordinates) for coordinates in parse_detection_coordinates(encoded)]


def read_detections(path: Path) -> list[tuple[float, list[Box]]]:
    rows = []
    for row in path.read_text(encoding="utf-8").splitlines():
        timestamp, encoded = (row.split("\t", 1) + [""])[:2]
        rows.append((float(timestamp), parse_boxes(encoded)))
    return rows


def boxes_overlap(left: Box, right: Box, margin: float = 0.035) -> bool:
    horizontal_gap = max(
        0.0,
        max(left.x, right.x) - min(left.x + left.width, right.x + right.width),
    )
    vertical_overlap = min(left.y + left.height, right.y + right.height) - max(left.y, right.y)
    return horizontal_gap <= margin and vertical_overlap > 0.05


def privacy_action(
    speakers: Box | list[Box] | None, others: list[Box], margin: float = 0.035
) -> str:
    if isinstance(speakers, Box):
        speakers = [speakers]
    if not speakers:
        return "full-blur"
    for speaker, other in itertools.combinations([*speakers, *others], 2):
        if boxes_overlap(speaker, other, margin):
            return "full-blur"
    return "blur-others"


def track_sample(track: list[dict], timestamp: float) -> dict:
    return next(
        (item for item in reversed(track) if float(item["time"]) <= timestamp),
        track[0],
    )


def dual_speakers_and_others(
    boxes: list[Box],
    timestamp: float,
    section: dict,
    participants: dict,
    tracks: dict[str, list[dict]],
    references: dict[str, tuple[float, float]],
) -> tuple[list[Box] | None, list[Box]]:
    names = [section["left"], section["right"]]
    expected = []
    for name in names:
        sample = track_sample(tracks[name], timestamp)
        if not sample["visible"]:
            continue
        reviewed_box = sample["box"]
        expected.append(
            (
                name,
                float(reviewed_box[0]) + float(reviewed_box[2]) / 2,
                float(reviewed_box[2]),
                float(reviewed_box[3]),
            )
        )
    if not expected or len(boxes) < len(expected):
        return None, boxes

    ranked = []
    for assignment in itertools.permutations(boxes, len(expected)):
        distances = [
            abs(box.center - center)
            + 0.35 * abs(box.width - width)
            + 0.10 * abs(box.height - height)
            + 1.50 * abs(box.luma - references[name][0]) / 255
            + 0.20 * abs(box.contrast - references[name][1]) / 128
            for box, (name, center, width, height) in zip(assignment, expected)
        ]
        ranked.append((sum(distances), max(distances), assignment))
    ranked.sort(key=lambda item: item[:2])
    best = ranked[0]
    ambiguous = len(ranked) > 1 and ranked[1][0] - best[0] < 0.06
    if best[1] > 0.18 or ambiguous:
        return None, boxes
    speakers = list(best[2])
    return speakers, [box for box in boxes if box not in speakers]


def problem_windows(
    path: Path,
    duration: float,
    padding: float,
    required: list[tuple[float, float]] | None = None,
) -> list[tuple[float, float]]:
    times = [timestamp for timestamp, boxes in read_detections(path) if len(boxes) > 1]
    windows = [
        (max(0, start - padding), min(duration, end + padding))
        for start, end in (required or [])
    ]
    groups: list[list[float]] = []
    for timestamp in times:
        if not groups or timestamp - groups[-1][-1] > 1.01:
            groups.append([timestamp])
        else:
            groups[-1].append(timestamp)
    windows.extend(
        (max(0, group[0] - padding), min(duration, group[-1] + 1 + padding))
        for group in groups
    )
    merged = []
    for start, end in sorted(windows):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def speaker_and_others(
    detections: list[tuple[float, list[Box]]],
    reference: tuple[float, float] | None,
) -> tuple[list[Box | None], list[list[Box]]]:
    singles = [(index, boxes[0]) for index, (_time, boxes) in enumerate(detections) if len(boxes) == 1]
    if not singles or reference is None:
        return [None] * len(detections), [boxes for _time, boxes in detections]
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


def speaker_reference(detections: Path, samples: Path) -> tuple[float, float] | None:
    rows = [row for row in read_detections(detections) if len(row[1]) == 1]
    rows = rows[::max(1, len(rows) // 100)]
    pairs = [
        (row, samples / f"frame-{round(row[0]) + 1:05d}.jpg")
        for row in rows
        if (samples / f"frame-{round(row[0]) + 1:05d}.jpg").exists()
    ]
    if not pairs:
        return None
    selected_rows = [row for row, _frame in pairs]
    add_appearance([frame for _row, frame in pairs], selected_rows)
    return (
        median(row[1][0].luma for row in selected_rows),
        median(row[1][0].contrast for row in selected_rows),
    )


def participant_references(
    tracks: dict[str, list[dict]], samples: Path
) -> dict[str, tuple[float, float]]:
    references = {}
    for name, track in tracks.items():
        rows = [
            ((float(item["time"]), [Box(*map(float, item["box"]))]),
             samples / f"frame-{round(float(item['time'])) + 1:05d}.jpg")
            for item in track
            if item.get("visible") and item.get("box")
        ]
        rows = [(row, frame) for row, frame in rows if frame.exists()]
        rows = rows[::max(1, len(rows) // 100)]
        if not rows:
            raise SystemExit(f"privacy review has no appearance samples for participant {name!r}")
        detections = [row for row, _frame in rows]
        add_appearance([frame for _row, frame in rows], detections)
        references[name] = (
            median(row[1][0].luma for row in detections),
            median(row[1][0].contrast for row in detections),
        )
    return references


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
    window_dir: Path,
    duration: float,
    speakers: list[list[Box] | None],
    others: list[list[Box]],
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
    detector_command = privacy_detector_command(project)
    detector_path = Path(detector_command[0])
    if not detector_path.is_absolute() and (base / detector_path).exists():
        detector_command[0] = str((base / detector_path).resolve())

    timeline_path = resolve_project_path(project, project.get("timeline", "timeline.json"))
    timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
    validate_timeline(timeline)
    dual_sections = [
        section
        for section in timeline.get("layout_sections", [])
        if section["layout"] == "dual_speaker"
    ]
    windows = problem_windows(
        args.coarse_detections,
        float(timeline["duration"]),
        args.padding,
        [
            (float(section["source_start"]), float(section["source_end"]))
            for section in dual_sections
        ],
    )
    if not windows:
        raise SystemExit("no multi-person privacy windows found")
    reference = speaker_reference(args.coarse_detections, args.samples)
    participants = timeline.get("participants", {})
    participant_tracks = {
        name: json.loads(
            resolve_project_path(project, participant["track"]).read_text(encoding="utf-8")
        )
        for name, participant in participants.items()
    }
    dual_names = {
        section[side]
        for section in dual_sections
        for side in ("left", "right")
    }
    participant_appearance = participant_references(
        {name: participant_tracks[name] for name in dual_names}, args.samples
    )
    source_width = float(timeline.get("source_width", 3840))
    for name, track_data in participant_tracks.items():
        validate_speaker_track(
            track_data,
            float(timeline["duration"]),
            participants[name]["crop"],
            source_width,
            visibility=True,
        )
    build = base / "build/privacy-review"
    build.mkdir(parents=True, exist_ok=True)
    clips: list[Path] = []
    replacements: list[tuple[float, list[tuple[float, Box | None]]]] = []
    masks: list[Path] = []
    cuts: list[Path] = []
    encoder = host_capabilities(project)["video_encoder"]["name"]

    for index, (start, end) in enumerate(windows, 1):
        window_dir = build / f"window-{index:02d}"
        if window_dir.exists():
            shutil.rmtree(window_dir)
        window_dir.mkdir(parents=True)
        detections = extract_and_detect(
            args.video, window_dir, start, end - start, detector_command
        )
        primary_speakers, primary_others = speaker_and_others(detections, reference)
        speakers: list[list[Box] | None] = [
            [speaker] if speaker is not None else None for speaker in primary_speakers
        ]
        others = primary_others
        for sample_index, (relative_time, boxes) in enumerate(detections):
            source_time = start + relative_time
            section = next(
                (
                    item
                    for item in dual_sections
                    if float(item["source_start"]) <= source_time < float(item["source_end"])
                ),
                None,
            )
            if section:
                speakers[sample_index], others[sample_index] = dual_speakers_and_others(
                    boxes,
                    source_time,
                    section,
                    participants,
                    participant_tracks,
                    participant_appearance,
                )
        mask, cut = make_mask(window_dir, end - start, speakers, others)
        masks.append(mask)
        cuts.append(cut)
        replacements.append(
            (
                start,
                [
                    (time, box)
                    for (time, _boxes), box in zip(detections, primary_speakers, strict=True)
                ],
            )
        )

    track = build / "speaker-track.json"
    corrected_track(
        resolve_project_path(project, timeline["speaker_track"]),
        replacements,
        int(timeline["speaker_crop"]["width"]),
        int(timeline.get("source_width", 3840)),
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
            "--privacy-mask", str(mask), "--resolution", "1920x1080", "--encoder", encoder,
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
