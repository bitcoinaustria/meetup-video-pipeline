#!/usr/bin/env python3

import argparse
import json
from pathlib import Path

from video_common import atomic_write_json, atomic_write_text


SMOOTH_WEIGHTS = (1, 4, 6, 4, 1)


def clipped_box(box: list[float]) -> list[float]:
    x, y, width, height = box
    left, top = min(1.0, max(0.0, x)), min(1.0, max(0.0, y))
    right, bottom = min(1.0, max(0.0, x + width)), min(1.0, max(0.0, y + height))
    return [left, top, right - left, bottom - top]


def smooth_positions(values: list[float]) -> list[float]:
    radius = len(SMOOTH_WEIGHTS) // 2
    divisor = sum(SMOOTH_WEIGHTS)
    return [
        sum(
            weight * values[max(0, min(len(values) - 1, index + offset))]
            for offset, weight in enumerate(SMOOTH_WEIGHTS, start=-radius)
        )
        / divisor
        for index in range(len(values))
    ]


def stable_visibility(
    times: list[float], detected: list[bool], dropout_hold: float, reacquire: float
) -> list[bool]:
    visible = detected[:]
    index = 0
    while index < len(visible):
        if visible[index]:
            index += 1
            continue
        end = index
        while end < len(visible) and not visible[end]:
            end += 1
        if index and end < len(visible) and times[end] - times[index - 1] <= dropout_hold:
            visible[index:end] = [True] * (end - index)
        index = end
    detected_after_hold = visible[:]
    index = 1
    while index < len(visible):
        if not detected_after_hold[index] or detected_after_hold[index - 1]:
            index += 1
            continue
        end = index
        while end < len(visible) and detected_after_hold[end]:
            if times[end] - times[index] < reacquire:
                visible[end] = False
            end += 1
        index = end
    return visible


def fill_visible_boxes(positions: list[dict], visibility: list[bool]) -> None:
    for index, (item, visible) in enumerate(zip(positions, visibility, strict=True)):
        if not visible or item["box"] is not None:
            continue
        left = next(
            (positions[candidate] for candidate in range(index - 1, -1, -1) if positions[candidate]["box"]),
            None,
        )
        right = next(
            (
                positions[candidate]
                for candidate in range(index + 1, len(positions))
                if positions[candidate]["box"]
            ),
            None,
        )
        if not left or not right:
            raise SystemExit("held participant visibility has no detections on both sides")
        span = float(right["time"]) - float(left["time"])
        ratio = (float(item["time"]) - float(left["time"])) / span
        item["box"] = [
            round(float(a) + (float(b) - float(a)) * ratio, 6)
            for a, b in zip(left["box"], right["box"], strict=True)
        ]
        item["box_source"] = "interpolated_short_dropout"


def prepare(samples: Path, output: Path, step: float) -> None:
    frames = sorted(samples.glob("frame-*.jpg"))
    if not frames:
        raise SystemExit(f"no frames found in {samples}")
    atomic_write_text(
        output,
        "".join(f"{index * step:.3f}\t{path.resolve()}\n" for index, path in enumerate(frames)),
    )
    print(f"{len(frames)} frames -> {output}")


def build(
    detections: Path,
    output: Path,
    source_width: int,
    crop_width: int,
    track_visibility: bool = False,
    dropout_hold: float = 1.0,
    reacquire: float = 1.0,
) -> None:
    positions: list[dict[str, float]] = []
    visibility_limits: list[tuple[float, float] | None] = []
    previous_center: float | None = None
    previous_width: float | None = None

    for row in detections.read_text(encoding="utf-8").splitlines():
        timestamp, encoded = (row.split("\t", 1) + [""])[:2]
        boxes = []
        for item in encoded.split(";"):
            if not item:
                continue
            x, _y, width, height = clipped_box(list(map(float, item.split(","))))
            if width > 0 and height >= 0.12:
                boxes.append(
                    (x + width / 2, width, height / max(width, 0.01), [x, _y, width, height])
                )

        selected_box = None
        if boxes:
            if previous_center is None:
                center, width, _, selected_box = max(boxes, key=lambda item: item[2])
            else:
                center, width, _, selected_box = min(
                    boxes,
                    key=lambda item: abs(item[0] - previous_center)
                    + 0.35 * abs(item[1] - (previous_width or item[1]))
                    - 0.025 * item[2],
                )
            previous_center, previous_width = center, width
            person_left = (center - width / 2) * source_width
            person_right = (center + width / 2) * source_width
            framing_margin = min(100.0, max(0.0, (crop_width - width * source_width) / 4))
            visibility_limits.append(
                (
                    max(0.0, person_right + framing_margin - crop_width),
                    min(source_width - crop_width, person_left - framing_margin),
                )
            )
        else:
            visibility_limits.append(None)

        if previous_center is None:
            previous_center = 0.5
        target = previous_center * source_width - crop_width / 2
        target = max(0.0, min(source_width - crop_width, target))
        positions.append({"time": float(timestamp), "x": target, "box": selected_box})

    raw = [item["x"] for item in positions]
    cleaned = raw[:]
    for index in range(1, len(raw) - 1):
        neighbors = (raw[index - 1] + raw[index + 1]) / 2
        if abs(raw[index] - neighbors) > 450 and abs(raw[index - 1] - raw[index + 1]) < 300:
            cleaned[index] = neighbors

    for item, value, limits in zip(
        positions, smooth_positions(cleaned), visibility_limits, strict=True
    ):
        if limits is not None:
            lower, upper = limits
            if lower <= upper:
                value = max(lower, min(upper, value))
        item["x"] = round(value, 2)

    if track_visibility:
        times = [float(item["time"]) for item in positions]
        visibility = stable_visibility(
            times, [item["box"] is not None for item in positions], dropout_hold, reacquire
        )
        fill_visible_boxes(positions, visibility)
        for item, visible in zip(positions, visibility, strict=True):
            item["visible"] = visible
    else:
        for item in positions:
            item.pop("box", None)

    atomic_write_json(output, positions)
    print(f"{len(positions)} tracked positions -> {output}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare and smooth local speaker tracking data.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prep = subparsers.add_parser("prepare")
    prep.add_argument("samples", type=Path)
    prep.add_argument("output", type=Path)
    prep.add_argument("--step", type=float, default=4.0)

    track = subparsers.add_parser("build")
    track.add_argument("detections", type=Path)
    track.add_argument("output", type=Path)
    track.add_argument("--source-width", type=int, default=3840)
    track.add_argument("--crop-width", type=int, default=1080)
    track.add_argument("--track-visibility", action="store_true")
    track.add_argument("--dropout-hold", type=float, default=1.0)
    track.add_argument("--reacquire", type=float, default=1.0)

    subparsers.add_parser("self-test")

    args = parser.parse_args()
    if args.command == "self-test":
        assert [round(value, 2) for value in clipped_box([0.94, -0.01, 0.07, 0.5])] == [
            0.94, 0.0, 0.06, 0.49
        ]
        assert clipped_box([1.1, 0.0, 0.2, 0.5])[2] == 0
        assert smooth_positions([10.0] * 9) == [10.0] * 9
        assert stable_visibility([0, 0.5, 1.0], [True, False, True], 1.1, 0.0) == [True] * 3
        assert stable_visibility([0, 0.5, 1.0], [False, True, True], 0.0, 0.5) == [False, False, True]
        held = [
            {"time": 0.0, "box": [0.0, 0.0, 0.1, 0.5]},
            {"time": 0.5, "box": None},
            {"time": 1.0, "box": [0.2, 0.0, 0.1, 0.5]},
        ]
        fill_visible_boxes(held, [True, True, True])
        assert held[1]["box"] == [0.1, 0.0, 0.1, 0.5]
    elif args.command == "prepare":
        prepare(args.samples, args.output, args.step)
    else:
        build(
            args.detections,
            args.output,
            args.source_width,
            args.crop_width,
            args.track_visibility,
            args.dropout_hold,
            args.reacquire,
        )


if __name__ == "__main__":
    main()
