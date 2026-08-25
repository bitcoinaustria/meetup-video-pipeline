#!/usr/bin/env python3

import argparse
import json
from pathlib import Path


SMOOTH_WEIGHTS = (1, 4, 6, 4, 1)


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


def prepare(samples: Path, output: Path, step: float) -> None:
    frames = sorted(samples.glob("frame-*.jpg"))
    if not frames:
        raise SystemExit(f"no frames found in {samples}")
    output.write_text(
        "".join(f"{index * step:.3f}\t{path.resolve()}\n" for index, path in enumerate(frames)),
        encoding="utf-8",
    )
    print(f"{len(frames)} frames -> {output}")


def build(detections: Path, output: Path, source_width: int, crop_width: int) -> None:
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
            x, _y, width, height = map(float, item.split(","))
            if height >= 0.12:
                boxes.append((x + width / 2, width, height / max(width, 0.01)))

        if boxes:
            if previous_center is None:
                center, width, _ = max(boxes, key=lambda item: item[2])
            else:
                center, width, _ = min(
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
        positions.append({"time": float(timestamp), "x": target})

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

    output.write_text(json.dumps(positions, indent=2) + "\n", encoding="utf-8")
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

    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.samples, args.output, args.step)
    else:
        build(args.detections, args.output, args.source_width, args.crop_width)


if __name__ == "__main__":
    assert smooth_positions([10.0] * 9) == [10.0] * 9
    main()
