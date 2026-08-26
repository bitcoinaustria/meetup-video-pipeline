#!/usr/bin/env python3

import argparse
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a clean 1080p FAQ full-cover card.")
    parser.add_argument("question")
    parser.add_argument("output", type=Path)
    parser.add_argument("--background", type=Path, required=True)
    parser.add_argument("--font", type=Path)
    parser.add_argument("--label", default="AUDIENCE QUESTION")
    parser.add_argument("--accent", default="#eb0028")
    args = parser.parse_args()

    with Image.open(args.background) as background:
        image = background.resize((1920, 1080), Image.Resampling.LANCZOS).convert("RGBA")
    draw = ImageDraw.Draw(image)

    label_font = ImageFont.truetype(args.font, 30) if args.font else ImageFont.load_default(size=30)
    question_font = ImageFont.truetype(args.font, 72) if args.font else ImageFont.load_default(size=72)
    draw.rounded_rectangle((900, 345, 1020, 352), radius=3, fill=args.accent)
    draw.text((960, 405), args.label, font=label_font, fill=args.accent, anchor="mm")

    lines = textwrap.wrap(args.question, width=36)
    if len(lines) > 2:
        raise SystemExit("question must fit on two lines")
    draw.multiline_text(
        (960, 550), "\n".join(lines), font=question_font, fill="white", spacing=20, anchor="mm", align="center"
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(f".{args.output.stem}.tmp{args.output.suffix}")
    try:
        image.save(temporary)
        temporary.replace(args.output)
    finally:
        temporary.unlink(missing_ok=True)
    with Image.open(args.output) as check:
        if check.size != (1920, 1080) or check.mode != "RGBA":
            raise SystemExit("invalid FAQ card output")


if __name__ == "__main__":
    main()
