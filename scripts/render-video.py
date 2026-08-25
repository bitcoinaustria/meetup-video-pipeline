#!/usr/bin/env python3

import argparse
import bisect
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def project_source(path: str, project_dir: Path = ROOT) -> Path:
    source = Path(path)
    return (source if source.is_absolute() else project_dir / source).resolve()


def load_edits(path: Path | None, video: Path, project_dir: Path = ROOT) -> list[dict]:
    if path is None:
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    source = data.get("source", {}).get("path")
    if source and project_source(source, project_dir) != video.resolve():
        raise SystemExit(f"edit source does not match video: {source}")
    edits = data.get("edits")
    if edits is None:
        edits = [
            decision
            for decision in data.get("decisions", [])
            if decision.get("status") == "approved"
        ]
    edits = sorted(edits, key=lambda item: float(item["source_start"]))
    previous_end = 0.0
    for edit in edits:
        start = float(edit["source_start"])
        end = float(edit["source_end"])
        if start < previous_end or end <= start:
            raise SystemExit(f"invalid or overlapping edit: {edit}")
        previous_end = end
    return edits


def load_faq(path: Path | None, video: Path, project_dir: Path = ROOT) -> list[dict]:
    if path is None:
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    source = data.get("source", {}).get("path")
    if source and project_source(source, project_dir) != video.resolve():
        raise SystemExit(f"FAQ source does not match video: {source}")
    entries = []
    for entry in data.get("entries", []):
        image = Path(entry["image"])
        if not image.is_absolute():
            image = project_dir / image
        start = float(entry["source_start"])
        duration = float(entry.get("duration", 4.0))
        if start < 0 or duration <= 0 or not image.exists():
            raise SystemExit(f"invalid FAQ entry: {entry}")
        entries.append({**entry, "source_start": start, "duration": duration, "image": image})
    return sorted(entries, key=lambda item: item["source_start"])


def local_cuts(edits: list[dict], start: float, duration: float) -> list[tuple[float, float]]:
    end = start + duration
    cuts = []
    for edit in edits:
        cut_start = max(start, float(edit["source_start"]))
        cut_end = min(end, float(edit["source_end"]))
        if cut_end > cut_start:
            cuts.append((cut_start - start, cut_end - start))
    return cuts


def kept_intervals(cuts: list[tuple[float, float]], duration: float) -> list[tuple[float, float]]:
    kept = []
    cursor = 0.0
    for start, end in cuts:
        if start > cursor:
            kept.append((cursor, start))
        cursor = end
    if cursor < duration:
        kept.append((cursor, duration))
    if not kept:
        raise SystemExit("automatic edits remove the entire render range")
    return kept


def audio_edit_filter(cuts: list[tuple[float, float]], duration: float) -> str:
    kept = kept_intervals(cuts, duration)

    if len(kept) == 1:
        start, end = kept[0]
        return f"[0:a:0]atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS[editeda]"

    inputs = "".join(f"[audio{i}]" for i in range(len(kept)))
    filters = [f"[0:a:0]asplit={len(kept)}{inputs};"]
    for index, (start, end) in enumerate(kept):
        segment_duration = end - start
        fade = min(0.03, segment_duration / 3)
        chain = f"[audio{index}]atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS"
        if index:
            chain += f",afade=t=in:st=0:d={fade:.6f}"
        if index + 1 < len(kept):
            chain += f",afade=t=out:st={segment_duration - fade:.6f}:d={fade:.6f}"
        filters.append(chain + f"[audio-trimmed{index}];")
    filters.append(
        "".join(f"[audio-trimmed{index}]" for index in range(len(kept)))
        + f"concat=n={len(kept)}:v=0:a=1[editeda]"
    )
    return "".join(filters)


def video_edit_filter(label: str, cuts: list[tuple[float, float]], duration: float) -> str:
    kept = kept_intervals(cuts, duration)
    if len(kept) == 1:
        start, end = kept[0]
        return (
            f"[{label}]trim=start={start:.6f}:end={end:.6f},"
            "setpts=PTS-STARTPTS[editedv0]"
        )
    inputs = "".join(f"[video{i}]" for i in range(len(kept)))
    filters = [f"[{label}]split={len(kept)}{inputs};"]
    for index, (start, end) in enumerate(kept):
        filters.append(
            f"[video{index}]trim=start={start:.6f}:end={end:.6f},"
            f"setpts=PTS-STARTPTS[video-trimmed{index}];"
        )
    filters.append(
        "".join(f"[video-trimmed{index}]" for index in range(len(kept)))
        + f"concat=n={len(kept)}:v=1:a=0[editedv0]"
    )
    return "".join(filters)


def stabilize_camera_positions(
    values: list[float], deadband: float = 80.0
) -> list[float]:
    """Keep a quiet dead zone while immediately following sustained travel."""
    if not values:
        return []
    targets = values[:]
    for index in range(1, len(values) - 1):
        neighbors = (values[index - 1] + values[index + 1]) / 2
        if abs(values[index] - neighbors) > 180 and abs(values[index - 1] - values[index + 1]) < 120:
            targets[index] = neighbors
    camera = targets[0]
    held = [camera]
    for target in targets[1:]:
        if target > camera + deadband:
            camera = target - deadband
        elif target < camera - deadband:
            camera = target + deadband
        held.append(camera)
    return held


def monotone_slopes(times: list[float], values: list[float]) -> list[float]:
    """C1-continuous slopes without overshoot between tracked positions."""
    if len(values) < 2:
        return [0.0] * len(values)
    secants = [
        (right - left) / (times[index + 1] - times[index])
        for index, (left, right) in enumerate(zip(values, values[1:], strict=False))
    ]
    slopes = [secants[0]]
    for left, right in zip(secants, secants[1:], strict=False):
        if left * right <= 0:
            slopes.append(0.0)
        else:
            slopes.append(2 * left * right / (left + right))
    slopes.append(secants[-1])
    return slopes


def make_concat(timeline: dict, slides_dir: Path, output: Path) -> None:
    slides = timeline["slides"]
    duration = float(timeline["duration"])
    lines = ["ffconcat version 1.0"]
    for index, slide in enumerate(slides):
        end = float(slides[index + 1]["time"]) if index + 1 < len(slides) else duration
        seconds = end - float(slide["time"])
        image = (slides_dir / f"page-{int(slide['page']):02d}.jpg").resolve()
        if seconds <= 0 or not image.exists():
            raise SystemExit(f"invalid slide segment: {slide}")
        lines.extend((f"file '{image}'", f"duration {seconds:.6f}"))
    lines.append(f"file '{image}'")
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_speaker_commands(
    track_file: Path, output: Path, start: float, duration: float, position_scale: float = 1.0
) -> float:
    track = json.loads(track_file.read_text(encoding="utf-8"))
    positions = stabilize_camera_positions([float(item["x"]) for item in track])
    track = [{**item, "x": position} for item, position in zip(track, positions, strict=True)]
    times = [float(item["time"]) for item in track]
    slopes = monotone_slopes(times, positions)

    def position_and_velocity(timestamp: float) -> tuple[float, float]:
        index = bisect.bisect_right(times, timestamp)
        if index == 0:
            return float(track[0]["x"]), 0.0
        if index == len(track):
            return float(track[-1]["x"]), 0.0
        left, right = track[index - 1], track[index]
        segment = float(right["time"]) - float(left["time"])
        ratio = (timestamp - float(left["time"])) / segment
        left_x, right_x = float(left["x"]), float(right["x"])
        left_slope, right_slope = slopes[index - 1], slopes[index]
        position = (
            (2 * ratio**3 - 3 * ratio**2 + 1) * left_x
            + (ratio**3 - 2 * ratio**2 + ratio) * segment * left_slope
            + (-2 * ratio**3 + 3 * ratio**2) * right_x
            + (ratio**3 - ratio**2) * segment * right_slope
        )
        velocity = (
            (6 * ratio**2 - 6 * ratio) * left_x / segment
            + (3 * ratio**2 - 4 * ratio + 1) * left_slope
            + (-6 * ratio**2 + 6 * ratio) * right_x / segment
            + (3 * ratio**2 - 2 * ratio) * right_slope
        )
        return position, velocity

    first = position_and_velocity(start)[0] * position_scale
    commands = []
    boundaries = [0.0]
    boundaries.extend(time - start for time in times if start < time < start + duration)
    boundaries.append(duration)
    for local, end in zip(boundaries, boundaries[1:], strict=False):
        step = end - local
        current, velocity = position_and_velocity(start + local)
        following, next_velocity = position_and_velocity(start + end)
        current *= position_scale
        following *= position_scale
        velocity *= position_scale
        next_velocity *= position_scale
        quadratic = 3 * (following - current) / step**2 - (2 * velocity + next_velocity) / step
        cubic = 2 * (current - following) / step**3 + (velocity + next_velocity) / step**2
        delta = f"(t-{local:.6f})"
        expression = (
            f"{current:.4f}+{velocity:.4f}*{delta}+"
            f"{quadratic:.4f}*{delta}^2+{cubic:.4f}*{delta}^3"
        )
        commands.append(f"{local:.3f} crop@speaker x {expression};")
    output.write_text("\n".join(commands) + "\n", encoding="utf-8")
    return first


def main() -> None:
    parser = argparse.ArgumentParser(description="Render a branded meetup presentation layout.")
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--project-dir", type=Path, default=ROOT)
    parser.add_argument("--timeline", type=Path, default=ROOT / "timeline.json")
    parser.add_argument("--background", type=Path, default=ROOT / "Background.png")
    parser.add_argument("--slides", type=Path, default=ROOT / "build/slides")
    parser.add_argument("--output", type=Path, default=ROOT / "output/final/presentation.mp4")
    parser.add_argument("--start", type=float, default=0.0)
    parser.add_argument("--duration", type=float)
    parser.add_argument("--privacy-mask", type=Path)
    parser.add_argument(
        "--privacy-mask-start",
        type=float,
        default=0.0,
        help="Source timestamp represented by the first privacy-mask frame.",
    )
    parser.add_argument("--full-blur-mask", type=Path)
    parser.add_argument("--edl", type=Path, help="Approved automatic audio/video edits.")
    parser.add_argument("--faq-timeline", type=Path, help="Full-cover FAQ cards in source time.")
    parser.add_argument("--encoder", default="h264_videotoolbox")
    parser.add_argument("--preset")
    parser.add_argument("--resolution", choices=("3840x2160", "1920x1080"), default="3840x2160")
    args = parser.parse_args()
    if args.full_blur_mask and not args.privacy_mask:
        raise SystemExit("--full-blur-mask requires --privacy-mask")
    if args.privacy_mask and args.start < args.privacy_mask_start:
        raise SystemExit("render starts before the privacy mask")

    timeline = json.loads(args.timeline.read_text(encoding="utf-8"))
    slides = timeline["slides"]
    times = [float(item["time"]) for item in slides]
    if times != sorted(times) or times[0] != float(timeline["website_until"]):
        raise SystemExit("timeline must be ordered and start at website_until")

    build = args.timeline.resolve().parent / "render"
    build.mkdir(parents=True, exist_ok=True)
    concat = build / "slides.ffconcat"
    make_concat(timeline, args.slides, concat)

    person = timeline["speaker_crop"]
    screen = timeline["screen_crop"]
    output_width, output_height = map(int, args.resolution.split("x"))
    scale = output_width / 3840

    def scaled(value: int) -> int:
        return round(value * scale)

    render_duration = args.duration if args.duration is not None else float(timeline["duration"]) - args.start
    edits = load_edits(args.edl, args.video, args.project_dir)
    faq_entries = [
        entry
        for entry in load_faq(args.faq_timeline, args.video, args.project_dir)
        if args.start <= entry["source_start"] < args.start + render_duration
    ]
    cuts = local_cuts(edits, args.start, render_duration)
    base_duration = render_duration - sum(end - start for start, end in cuts)
    output_duration = base_duration + sum(entry["duration"] for entry in faq_entries)
    commands = build / "speaker-track.ffcmd"
    initial_x = make_speaker_commands(
        project_source(timeline["speaker_track"], args.project_dir),
        commands,
        args.start,
        render_duration,
        scale,
    )
    slide_offset = float(timeline["website_until"]) - args.start
    slide_enable = max(0.0, slide_offset)
    privacy = f"[0:v]scale={output_width}:{output_height},format=yuv420p[private];"
    source = "[private]"
    if args.privacy_mask:
        blur_result = "[blurred]"
        full_blur = ""
        if args.full_blur_mask:
            blur_result = "[blurred0]"
            full_blur = (
                f"[blurred0]split=2[blurred_people][blurred_full];"
                f"[blurred_people][mask]alphamerge[blurred_people_alpha];"
                f"[clean][blurred_people_alpha]overlay[partially_private];"
                f"[4:v]format=gray,scale={output_width}:{output_height}[full_blur_mask];"
                f"[blurred_full][full_blur_mask]alphamerge[blurred_full_alpha];"
                f"[partially_private][blurred_full_alpha]overlay[private];"
            )
        else:
            full_blur = (
                f"{blur_result}[mask]alphamerge[blurred_alpha];"
                f"[clean][blurred_alpha]overlay[private];"
            )
        privacy = (
            f"[0:v]scale={output_width}:{output_height},format=yuv420p,split=2[clean][blurbase];"
            f"[blurbase]scale=960:540,boxblur=24:2,scale={output_width}:{output_height}:flags=bilinear{blur_result};"
            f"[3:v]format=gray,gblur=sigma=8,scale={output_width}:{output_height}[mask];"
            + full_blur
        )
        source = "[private]"

    stage = (
        privacy
        + f"{source}split=3[person0][screen0][clock0];"
        f"[person0]sendcmd=f={commands},crop@speaker={scaled(person['width'])}:{scaled(person['height'])}:{initial_x:.2f}:{scaled(person['y'])},"
        f"scale={scaled(864)}:{scaled(1536)}:flags=lanczos[person];"
        f"[screen0]crop={scaled(screen['width'])}:{scaled(screen['height'])}:{scaled(screen['x'])}:{scaled(screen['y'])},"
        f"scale={scaled(2730)}:{scaled(1536)}:flags=lanczos[screen];"
        f"[1:v]scale={output_width}:{output_height},format=yuv420p[background];"
        # Keep the camera's frame clock. A looped 30 fps background as the
        # overlay main duplicates frames when phone timestamps briefly drift.
        f"[clock0][background]blend=all_expr=B[canvas];"
        f"[canvas][person]overlay={scaled(91)}:{scaled(296)}[stage1];"
        f"[stage1][screen]overlay={scaled(1019)}:{scaled(296)}[stage2];"
    )
    slides_filter = (
        f"[2:v]scale={scaled(2730)}:{scaled(1536)}:flags=lanczos,setpts=PTS+{slide_offset:.6f}/TB[slides];"
        f"[stage2][slides]overlay={scaled(1019)}:{scaled(296)}:eof_action=pass:enable='gte(t,{slide_enable:.6f})'[outv]"
    )
    filter_graph = stage + slides_filter
    video_label = "outv"
    audio_label: str | None = None
    if cuts:
        filter_graph += ";" + video_edit_filter(video_label, cuts, render_duration) + ";"
        video_label = "editedv0"
        filter_graph += audio_edit_filter(cuts, render_duration)
        audio_label = "editeda"
    elif faq_entries:
        filter_graph += (
            f";[0:a:0]atrim=start=0:end={render_duration:.6f},"
            "asetpts=PTS-STARTPTS[editeda]"
        )
        audio_label = "editeda"

    if faq_entries:
        assert audio_label is not None

        def edited_time(source_timestamp: float) -> float:
            local = source_timestamp - args.start
            removed = sum(max(0.0, min(local, end) - start) for start, end in cuts if start < local)
            return max(0.0, min(base_duration, local - removed))

        insertions = [(edited_time(entry["source_start"]), entry) for entry in faq_entries]
        intervals: list[tuple[float, float]] = []
        cursor = 0.0
        for point, _entry in insertions:
            if point > cursor + 1e-6:
                intervals.append((cursor, point))
            cursor = max(cursor, point)
        if base_duration > cursor + 1e-6:
            intervals.append((cursor, base_duration))

        if len(intervals) == 1:
            video_sources = [video_label]
            audio_sources = [audio_label]
        else:
            video_sources = [f"faq-base-v{index}" for index in range(len(intervals))]
            audio_sources = [f"faq-base-a{index}" for index in range(len(intervals))]
            filter_graph += (
                f";[{video_label}]split={len(intervals)}"
                + "".join(f"[{label}]" for label in video_sources)
                + f";[{audio_label}]asplit={len(intervals)}"
                + "".join(f"[{label}]" for label in audio_sources)
            )

        interval_labels: dict[tuple[float, float], tuple[str, str]] = {}
        for index, ((begin, end), video_source, audio_source) in enumerate(
            zip(intervals, video_sources, audio_sources, strict=True)
        ):
            video_part, audio_part = f"faq-segment-v{index}", f"faq-segment-a{index}"
            filter_graph += (
                f";[{video_source}]trim=start={begin:.6f}:end={end:.6f},"
                f"setpts=PTS-STARTPTS[{video_part}]"
                f";[{audio_source}]atrim=start={begin:.6f}:end={end:.6f},"
                f"asetpts=PTS-STARTPTS[{audio_part}]"
            )
            interval_labels[(begin, end)] = (video_part, audio_part)

        faq_input = 3 + int(bool(args.privacy_mask)) + int(bool(args.full_blur_mask))
        pieces: list[tuple[str, str]] = []
        cursor = 0.0
        for index, (point, entry) in enumerate(insertions):
            if point > cursor + 1e-6:
                pieces.append(interval_labels[(cursor, point)])
            faq_video, faq_audio = f"faq-card-v{index}", f"faq-card-a{index}"
            filter_graph += (
                f";[{faq_input + index}:v]scale={output_width}:{output_height},"
                f"format=yuv420p,trim=duration={entry['duration']:.6f},"
                f"setpts=PTS-STARTPTS[{faq_video}]"
                f";anullsrc=r=48000:cl=stereo,atrim=duration={entry['duration']:.6f},"
                f"asetpts=PTS-STARTPTS[{faq_audio}]"
            )
            pieces.append((faq_video, faq_audio))
            cursor = max(cursor, point)
        if base_duration > cursor + 1e-6:
            pieces.append(interval_labels[(cursor, base_duration)])

        filter_graph += (
            ";"
            + "".join(f"[{video}][{audio}]" for video, audio in pieces)
            + f"concat=n={len(pieces)}:v=1:a=1[finalv][finala]"
        )
        video_label, audio_label = "finalv", "finala"

    video_map = f"[{video_label}]"
    audio_map = f"[{audio_label}]" if audio_label else "0:a:0"

    bitrate, maxrate, bufsize = ("8M", "12M", "16M") if output_width == 1920 else ("24M", "32M", "48M")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    command = ["ffmpeg", "-hide_banner", "-y"]
    if args.start:
        command.extend(("-ss", f"{args.start:.6f}"))
    command.extend((
        "-i", str(args.video),
        "-loop", "1", "-framerate", "30", "-i", str(args.background),
        "-safe", "0", "-f", "concat", "-i", str(concat),
    ))
    if args.privacy_mask:
        mask_offset = args.start - args.privacy_mask_start
        if mask_offset:
            command.extend(("-ss", f"{mask_offset:.6f}"))
        command.extend(("-i", str(args.privacy_mask)))
    if args.full_blur_mask:
        if mask_offset:
            command.extend(("-ss", f"{mask_offset:.6f}"))
        command.extend(("-i", str(args.full_blur_mask)))
    for entry in faq_entries:
        command.extend(("-loop", "1", "-framerate", "30", "-i", str(entry["image"])))
    command.extend((
        "-filter_complex", filter_graph,
        "-map", video_map, "-map", audio_map,
        "-c:v", args.encoder,
    ))
    if args.encoder.endswith("_videotoolbox"):
        command.extend(("-allow_sw", "1"))
    command.extend(("-b:v", bitrate, "-maxrate", maxrate, "-bufsize", bufsize))
    if args.preset:
        command.extend(("-preset", args.preset))
    command.extend((
        "-pix_fmt", "yuv420p", "-fps_mode", "passthrough",
        "-c:a", "aac" if cuts or faq_entries else "copy",
    ))
    if cuts or faq_entries:
        command.extend(("-b:a", "192k"))
    command.extend(("-movflags", "+faststart", "-t", f"{output_duration:.6f}"))
    command.append(str(args.output))
    subprocess.run(command, check=True)


if __name__ == "__main__":
    assert stabilize_camera_positions([0, 0, 300, 0, 0]) == [0, 0, 0, 0, 0]
    followed = stabilize_camera_positions([0] * 4 + [400] * 10)
    assert max(abs(target - camera) for target, camera in zip([0] * 4 + [400] * 10, followed)) <= 80
    assert monotone_slopes([0, 1, 2], [0, 1, 0]) == [1.0, 0.0, -1.0]
    assert kept_intervals([(1.0, 2.0)], 3.0) == [(0.0, 1.0), (2.0, 3.0)]
    main()
