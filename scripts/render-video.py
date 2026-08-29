#!/usr/bin/env python3

import argparse
import bisect
import json
import math
import subprocess
from pathlib import Path

from video_common import (
    atomic_write_text,
    decoder_options,
    encoder_options,
    ffconcat_quote,
    monotone_slopes,
    resolve_project_path,
    source_to_output,
    stabilize_camera_positions,
    validate_speaker_track,
    validate_timeline,
)


ROOT = Path(__file__).resolve().parent.parent


def project_source(path: str, project_dir: Path = ROOT) -> Path:
    return resolve_project_path({"_project_dir": str(project_dir)}, path).resolve()


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


def audio_edit_filter(source: str, cuts: list[tuple[float, float]], duration: float) -> str:
    kept = kept_intervals(cuts, duration)

    if len(kept) == 1:
        start, end = kept[0]
        return f"[{source}]atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS[editeda]"

    inputs = "".join(f"[audio{i}]" for i in range(len(kept)))
    filters = [f"[{source}]asplit={len(kept)}{inputs};"]
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
        lines.extend((f"file {ffconcat_quote(image)}", f"duration {seconds:.6f}"))
    lines.append(f"file {ffconcat_quote(image)}")
    atomic_write_text(output, "\n".join(lines) + "\n")


def make_speaker_commands(
    track_file: Path,
    output: Path,
    start: float,
    duration: float,
    position_scale: float = 1.0,
    instance: str = "speaker",
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
        commands.append(f"{local:.3f} crop@{instance} x {expression};")
    atomic_write_text(output, "\n".join(commands) + "\n")
    return first


def intersect_intervals(
    left: list[tuple[float, float]], right: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    intersections = []
    for left_start, left_end in left:
        for right_start, right_end in right:
            start, end = max(left_start, right_start), min(left_end, right_end)
            if end > start:
                intersections.append((start, end))
    return intersections


def visible_intervals(track: list[dict], duration: float) -> list[tuple[float, float]]:
    intervals = []
    for index, item in enumerate(track):
        start = float(item["time"])
        end = float(track[index + 1]["time"]) if index + 1 < len(track) else duration
        if item.get("visible", True) and end > start:
            if intervals and start <= intervals[-1][1] + 1e-6:
                intervals[-1] = (intervals[-1][0], end)
            else:
                intervals.append((start, end))
    return intervals


def enable_expression(
    intervals: list[tuple[float, float]], start: float, duration: float
) -> str:
    end = start + duration
    local = [
        (max(left, start) - start, min(right, end) - start)
        for left, right in intervals
        if left < end and right > start
    ]
    if not local:
        return "0"
    return "+".join(f"between(t,{left:.6f},{right:.6f})" for left, right in local)


def microphone_gain_segments(
    timeline: dict, start: float, duration: float
) -> dict[int, list[tuple[float, float, float]]]:
    mix = timeline.get("microphone_mix", {})
    inactive = float(mix.get("inactive_gain", 0.18))
    both = float(mix.get("both_gain", 0.5))
    end = start + duration
    gains = {1: [], 2: []}
    for section in timeline.get("layout_sections", []):
        left = max(start, float(section["source_start"]))
        right = min(end, float(section["source_end"]))
        if right <= left:
            continue
        levels = {1: inactive, 2: inactive}
        if section["layout"] == "dual_speaker":
            participants = timeline["participants"]
            left_channel = int(participants[section["left"]]["audio_channel"])
            right_channel = int(participants[section["right"]]["audio_channel"])
            if section["active"] in {"left", "both"}:
                levels[left_channel] = both if section["active"] == "both" else 1.0
            if section["active"] in {"right", "both"}:
                levels[right_channel] = both if section["active"] == "both" else 1.0
        else:
            channel = int(section["audio_channel"])
            if channel not in gains:
                raise SystemExit("reviewed microphone mix supports only stereo source channels 1 and 2")
            levels[channel] = 1.0
        for channel in gains:
            gains[channel].append((left - start, right - start, levels[channel]))
    if any(not segments or abs(segments[0][0]) > 1e-6 or abs(segments[-1][1] - duration) > 1e-6 for segments in gains.values()):
        raise SystemExit("reviewed microphone mix does not cover the render range")
    return gains


def gain_expression(segments: list[tuple[float, float, float]], fade: float) -> str:
    def expression(index: int) -> str:
        gain = segments[index][2]
        if index + 1 == len(segments):
            return f"{gain:.6f}"
        boundary = segments[index][1]
        next_gain = segments[index + 1][2]
        left, right = max(0.0, boundary - fade), boundary + fade
        ramp = f"{gain:.6f}+({next_gain:.6f}-{gain:.6f})*(t-{left:.6f})/{right - left:.6f}"
        return (
            f"if(lt(t,{left:.6f}),{gain:.6f},"
            f"if(lt(t,{right:.6f}),{ramp},{expression(index + 1)}))"
        )

    return expression(0)


def microphone_mix_filter(timeline: dict, start: float, duration: float) -> str:
    mix = timeline.get("microphone_mix", {})
    fade = float(mix.get("fade_seconds", 0.12))
    normalize = mix.get("normalize", True)
    integrated = float(mix.get("integrated_lufs", -18.0))
    true_peak = float(mix.get("true_peak_db", -2.0))
    gains = microphone_gain_segments(timeline, start, duration)
    filters = ["[0:a:0]channelsplit=channel_layout=stereo[mic1][mic2]"]
    for channel in (1, 2):
        chain = f"[mic{channel}]"
        if normalize:
            chain += (
                f"loudnorm=I={integrated:.2f}:LRA=11:TP={true_peak:.2f},"
                "aresample=48000:async=1:first_pts=0,"
            )
        chain += (
            f"volume='{gain_expression(gains[channel], fade)}':eval=frame,"
            "pan=stereo|c0=0.70710678*c0|c1=0.70710678*c0"
            f"[mixed-mic{channel}]"
        )
        filters.append(chain)
    ceiling = 10 ** (true_peak / 20)
    filters.append(
        "[mixed-mic1][mixed-mic2]amix=inputs=2:normalize=0:dropout_transition=0,"
        f"alimiter=limit={ceiling:.8f}:level=0:latency=1[prepareda]"
    )
    return ";".join(filters)


def main() -> None:
    parser = argparse.ArgumentParser(description="Render a branded meetup presentation layout.")
    parser.add_argument("--video", type=Path)
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
    parser.add_argument("--end-card", type=Path)
    parser.add_argument("--end-card-duration", type=float, default=0.0)
    parser.add_argument("--encoder", default="libx264")
    parser.add_argument("--preset")
    parser.add_argument("--resolution", choices=("3840x2160", "1920x1080"), default="3840x2160")
    parser.add_argument(
        "--audio-policy",
        choices=(
            "process_once_then_duplicate_to_stereo",
            "process_and_preserve_each_channel_separately",
            "mix_reviewed_microphones_to_stereo",
        ),
        default="process_and_preserve_each_channel_separately",
    )
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    if not args.video:
        raise SystemExit("--video is required")
    for name in (
        "video", "project_dir", "timeline", "background", "slides", "output",
        "privacy_mask", "full_blur_mask", "edl", "faq_timeline", "end_card",
    ):
        value = getattr(args, name)
        if value is not None:
            setattr(args, name, value.resolve())
    if args.full_blur_mask and not args.privacy_mask:
        raise SystemExit("--full-blur-mask requires --privacy-mask")
    if args.privacy_mask and args.start < args.privacy_mask_start:
        raise SystemExit("render starts before the privacy mask")
    if args.end_card_duration < 0 or bool(args.end_card) != (args.end_card_duration > 0):
        raise SystemExit("--end-card and a positive --end-card-duration are required together")
    if args.end_card and not args.end_card.is_file():
        raise SystemExit(f"end card is missing: {args.end_card}")

    timeline = json.loads(args.timeline.read_text(encoding="utf-8"))
    validate_timeline(timeline)

    build = args.timeline.resolve().parent / "render"
    build.mkdir(parents=True, exist_ok=True)
    concat = build / "slides.ffconcat"
    make_concat(timeline, args.slides, concat)

    person = timeline["speaker_crop"]
    screen = timeline["screen_crop"]
    output_width, output_height = map(int, args.resolution.split("x"))
    source_width = float(timeline.get("source_width", 3840))
    if source_width <= 0:
        raise SystemExit("timeline source_width must be positive")
    layout_scale = output_width / 3840
    source_scale = output_width / source_width

    def layout_scaled(value: int) -> int:
        return round(value * layout_scale)

    def source_scaled(value: int) -> int:
        return round(value * source_scale)

    render_duration = args.duration if args.duration is not None else float(timeline["duration"]) - args.start
    primary_track = json.loads(
        project_source(timeline["speaker_track"], args.project_dir).read_text(encoding="utf-8")
    )
    validate_speaker_track(
        primary_track, float(timeline["duration"]), person, source_width
    )
    participants = timeline.get("participants", {})
    participant_tracks = {
        name: json.loads(
            project_source(participant["track"], args.project_dir).read_text(encoding="utf-8")
        )
        for name, participant in participants.items()
    }
    for name, track in participant_tracks.items():
        validate_speaker_track(
            track,
            float(timeline["duration"]),
            participants[name]["crop"],
            source_width,
            visibility=True,
        )
    dual_sections = [
        section
        for section in timeline.get("layout_sections", [])
        if section["layout"] == "dual_speaker"
        and float(section["source_start"]) < args.start + render_duration
        and float(section["source_end"]) > args.start
    ]
    render_window = [(args.start, args.start + render_duration)]
    render_participants = {}
    for name, participant in participants.items():
        sections = [
            (float(section["source_start"]), float(section["source_end"]))
            for section in dual_sections
            if name in {section["left"], section["right"]}
        ]
        visible = visible_intervals(participant_tracks[name], float(timeline["duration"]))
        if intersect_intervals(intersect_intervals(sections, visible), render_window):
            render_participants[name] = participant
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
        source_scale,
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

    participant_labels = [f"participant-source-{index}" for index in range(len(render_participants))]
    split_labels = ["person0", "screen0", "clock0", *participant_labels]
    stage = privacy + f"{source}split={len(split_labels)}" + "".join(
        f"[{label}]" for label in split_labels
    ) + ";"
    stage += (
        f"[person0]sendcmd=f={commands.name},crop@speaker={source_scaled(person['width'])}:{source_scaled(person['height'])}:{initial_x:.2f}:{source_scaled(person['y'])},"
        f"scale={layout_scaled(864)}:{layout_scaled(1536)}:flags=lanczos[person];"
        f"[screen0]crop={source_scaled(screen['width'])}:{source_scaled(screen['height'])}:{source_scaled(screen['x'])}:{source_scaled(screen['y'])}[screen-base];"
    )
    if dual_sections:
        stage += (
            f"[screen-base]split=2[screen-standard0][screen-dual0];"
            f"[1:v]scale={output_width}:{output_height},format=yuv420p,split=2[background-standard][background-dual];"
            f"[clock0]split=2[clock-standard][clock-dual];"
            f"[2:v]split=2[slides-standard0][slides-dual0];"
        )
    else:
        stage += (
            f"[screen-base]null[screen-standard0];"
            f"[1:v]scale={output_width}:{output_height},format=yuv420p[background-standard];"
            f"[clock0]null[clock-standard];"
            f"[2:v]null[slides-standard0];"
        )
    # Keep the camera's frame clock. A looped 30 fps background as the
    # overlay main duplicates frames when phone timestamps briefly drift.
    stage += (
        f"[screen-standard0]scale={layout_scaled(2730)}:{layout_scaled(1536)}:flags=lanczos[screen];"
        f"[clock-standard][background-standard]blend=all_expr=B[canvas];"
        f"[canvas][person]overlay={layout_scaled(91)}:{layout_scaled(296)}[stage1];"
        f"[stage1][screen]overlay={layout_scaled(1019)}:{layout_scaled(296)}[stage2];"
        f"[slides-standard0]scale={layout_scaled(2730)}:{layout_scaled(1536)}:flags=lanczos,setpts=PTS+{slide_offset:.6f}/TB[slides-standard];"
        f"[stage2][slides-standard]overlay={layout_scaled(1019)}:{layout_scaled(296)}:eof_action=pass:enable='gte(t,{slide_enable:.6f})'[standard-out]"
    )
    filter_graph = stage
    video_label = "standard-out"
    if dual_sections:
        dual_intervals = [
            (float(section["source_start"]), float(section["source_end"]))
            for section in dual_sections
        ]
        dual_enable = enable_expression(dual_intervals, args.start, render_duration)
        filter_graph += (
            f";[screen-dual0]scale={layout_scaled(2400)}:{layout_scaled(1350)}:flags=lanczos[screen-dual]"
            f";[clock-dual][background-dual]blend=all_expr=B[dual-canvas]"
            f";[dual-canvas][screen-dual]overlay={layout_scaled(720)}:{layout_scaled(405)}[dual-stage0]"
            f";[slides-dual0]scale={layout_scaled(2400)}:{layout_scaled(1350)}:flags=lanczos,setpts=PTS+{slide_offset:.6f}/TB[slides-dual]"
            f";[dual-stage0][slides-dual]overlay={layout_scaled(720)}:{layout_scaled(405)}:eof_action=pass:enable='gte(t,{slide_enable:.6f})'[dual-stage1]"
        )
        dual_label = "dual-stage1"
        for index, (name, participant) in enumerate(render_participants.items()):
            track = participant_tracks[name]
            command_file = build / f"participant-{index}.ffcmd"
            instance = f"participant{index}"
            initial = make_speaker_commands(
                project_source(participant["track"], args.project_dir),
                command_file,
                args.start,
                render_duration,
                source_scale,
                instance,
            )
            crop = participant["crop"]
            visible = visible_intervals(track, float(timeline["duration"]))
            left_sections = [
                (float(section["source_start"]), float(section["source_end"]))
                for section in dual_sections
                if section["left"] == name
            ]
            right_sections = [
                (float(section["source_start"]), float(section["source_end"]))
                for section in dual_sections
                if section["right"] == name
            ]
            filter_graph += (
                f";[{participant_labels[index]}]sendcmd=f={command_file.name},"
                f"crop@{instance}={source_scaled(crop['width'])}:{source_scaled(crop['height'])}:{initial:.2f}:{source_scaled(crop['y'])},"
                f"scale={layout_scaled(640)}:{layout_scaled(1138)}:force_original_aspect_ratio=increase:flags=lanczos,"
                f"crop={layout_scaled(640)}:{layout_scaled(1138)}[participant-{index}]"
            )
            for side, sections, x in (
                ("left", left_sections, 60),
                ("right", right_sections, 3140),
            ):
                expression = enable_expression(
                    intersect_intervals(sections, visible), args.start, render_duration
                )
                if expression == "0":
                    continue
                next_label = f"dual-{side}-{index}"
                filter_graph += (
                    f";[{dual_label}][participant-{index}]overlay={layout_scaled(x)}:{layout_scaled(511)}:"
                    f"eof_action=pass:enable='{expression}'[{next_label}]"
                )
                dual_label = next_label
        for side, x in (("left", 60), ("right", 3140)):
            active = []
            for section in dual_sections:
                if section.get("active") not in {side, "both"}:
                    continue
                name = section[side]
                active.extend(
                    intersect_intervals(
                        [(float(section["source_start"]), float(section["source_end"]))],
                        visible_intervals(
                            participant_tracks[name], float(timeline["duration"])
                        ),
                    )
                )
            expression = enable_expression(active, args.start, render_duration)
            if expression != "0":
                next_label = f"dual-active-{side}"
                filter_graph += (
                    f";[{dual_label}]drawbox=x={layout_scaled(x)}:y={layout_scaled(511)}:"
                    f"w={layout_scaled(640)}:h={layout_scaled(1138)}:color=#eb0028@0.75:"
                    f"t={max(2, layout_scaled(8))}:enable='{expression}'[{next_label}]"
                )
                dual_label = next_label
        filter_graph += f";[standard-out][{dual_label}]overlay=0:0:enable='{dual_enable}'[outv]"
        video_label = "outv"
    audio_label: str | None = None
    audio_source = "0:a:0"
    if args.audio_policy == "mix_reviewed_microphones_to_stereo":
        if not timeline.get("mix_mapped_microphones"):
            raise SystemExit("reviewed microphone mixing is not configured in the timeline")
        filter_graph += ";" + microphone_mix_filter(timeline, args.start, render_duration)
        audio_source = "prepareda"
    elif args.audio_policy == "process_once_then_duplicate_to_stereo":
        filter_graph += ";[0:a:0]pan=mono|c0=c0,pan=stereo|c0=c0|c1=c0[prepareda]"
        audio_source = "prepareda"
    if cuts:
        filter_graph += ";" + video_edit_filter(video_label, cuts, render_duration) + ";"
        video_label = "editedv0"
        filter_graph += audio_edit_filter(audio_source, cuts, render_duration)
        audio_label = "editeda"
    elif faq_entries:
        filter_graph += (
            f";[{audio_source}]atrim=start=0:end={render_duration:.6f},"
            "asetpts=PTS-STARTPTS[editeda]"
        )
        audio_label = "editeda"
    elif audio_source != "0:a:0":
        audio_label = audio_source

    if faq_entries:
        assert audio_label is not None
        filter_graph += f";[{audio_label}]aresample=48000,aformat=channel_layouts=stereo[faqbasea]"
        audio_label = "faqbasea"

        def edited_time(source_timestamp: float) -> float:
            local_edits = [
                {"source_start": start + args.start, "source_end": end + args.start}
                for start, end in cuts
            ]
            return min(base_duration, source_to_output(source_timestamp, args.start, local_edits))

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

    if not audio_label:
        filter_graph += (
            f";[0:a:0]atrim=start=0:end={render_duration:.6f},"
            "asetpts=PTS-STARTPTS[basea]"
        )
        audio_label = "basea"
    filter_graph += (
        f";[{video_label}]tpad=stop_mode=clone:stop_duration=1,"
        f"trim=duration={output_duration:.6f},setpts=PTS-STARTPTS[syncv]"
        f";[{audio_label}]apad,atrim=duration={output_duration:.6f},"
        "asetpts=PTS-STARTPTS[synca]"
    )
    video_label, audio_label = "syncv", "synca"

    final_duration = output_duration
    if args.end_card:
        end_card_input = 3 + int(bool(args.privacy_mask)) + int(bool(args.full_blur_mask)) + len(faq_entries)
        filter_graph += (
            f";[{audio_label}]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[contenta]"
            f";[{end_card_input}:v]scale={output_width}:{output_height}:force_original_aspect_ratio=decrease,"
            f"pad={output_width}:{output_height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p,"
            f"trim=duration={args.end_card_duration:.6f},setpts=PTS-STARTPTS[endcardv]"
            f";anullsrc=r=48000:cl=stereo,atrim=duration={args.end_card_duration:.6f},"
            "aformat=sample_fmts=fltp,asetpts=PTS-STARTPTS[endcarda]"
            f";[{video_label}][contenta][endcardv][endcarda]concat=n=2:v=1:a=1[withcardv][withcarda]"
        )
        video_label, audio_label = "withcardv", "withcarda"
        final_duration += args.end_card_duration

    video_map = f"[{video_label}]"
    audio_map = f"[{audio_label}]" if audio_label else "0:a:0"

    bitrate, maxrate, bufsize = ("8M", "12M", "16M") if output_width == 1920 else ("24M", "32M", "48M")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    command = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y", *decoder_options()]
    if args.start:
        command.extend(("-ss", f"{args.start:.6f}"))
    command.extend((
        "-i", str(args.video),
        "-loop", "1", "-framerate", "30", "-i", str(args.background),
    ))
    command.extend(("-safe", "0", "-f", "concat", "-i", str(concat)))
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
    if args.end_card:
        command.extend(("-loop", "1", "-framerate", "30", "-i", str(args.end_card)))
    command.extend((
        "-filter_complex", filter_graph,
        "-map", video_map, "-map", audio_map,
    ))
    command.extend(encoder_options(args.encoder, args.preset))
    command.extend(("-enc_time_base:v", "demux"))
    command.extend(("-b:v", bitrate, "-maxrate", maxrate, "-bufsize", bufsize))
    command.extend((
        "-pix_fmt", "yuv420p", "-fps_mode", "passthrough",
        "-c:a", "aac" if cuts or faq_entries or audio_label else "copy",
    ))
    if cuts or faq_entries or audio_label:
        command.extend(("-b:a", "192k"))
    command.extend(("-movflags", "+faststart", "-t", f"{final_duration:.6f}"))
    command.append(str(args.output))
    subprocess.run(command, check=True, cwd=build)


def self_test() -> None:
    assert stabilize_camera_positions([0, 0, 300, 0, 0]) == [0, 0, 0, 0, 0]
    followed = stabilize_camera_positions([0] * 4 + [400] * 10)
    assert max(abs(target - camera) for target, camera in zip([0] * 4 + [400] * 10, followed)) <= 80
    assert monotone_slopes([0, 1, 2], [0, 1, 0]) == [1.0, 0.0, -1.0]
    assert kept_intervals([(1.0, 2.0)], 3.0) == [(0.0, 1.0), (2.0, 3.0)]
    assert visible_intervals(
        [
            {"time": 0, "visible": True},
            {"time": 2, "visible": False},
            {"time": 4, "visible": True},
            {"time": 6, "visible": True},
        ],
        6,
    ) == [(0.0, 2.0), (4.0, 6.0)]
    assert enable_expression([(4, 8)], 5, 2) == "between(t,0.000000,2.000000)"
    assert "(1.000000-0.180000)" in gain_expression(
        [(0, 1, 0.18), (1, 2, 1.0)], 0.1
    )
    mix_filter = microphone_mix_filter(
        {
            "participants": {
                "left": {"audio_channel": 1},
                "right": {"audio_channel": 2},
            },
            "layout_sections": [
                {
                    "source_start": 0,
                    "source_end": 2,
                    "layout": "dual_speaker",
                    "left": "left",
                    "right": "right",
                    "active": "left",
                }
            ],
            "microphone_mix": {"true_peak_db": -2},
        },
        0,
        2,
    )
    assert "async=1:first_pts=0" in mix_filter
    assert "limit=0.79432823:level=0:latency=1" in mix_filter
    print("render-video self-test: ok")


if __name__ == "__main__":
    main()
