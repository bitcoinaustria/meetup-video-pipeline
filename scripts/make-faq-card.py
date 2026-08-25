#!/usr/bin/env python3

import argparse
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parent.parent
FONT_BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a clean 1080p FAQ full-cover card.")
    parser.add_argument("question")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    with Image.open(ROOT / "Background.png") as background:
        image = background.resize((1920, 1080), Image.Resampling.LANCZOS).convert("RGBA")
    draw = ImageDraw.Draw(image)

    label_font = ImageFont.truetype(FONT_BOLD, 30)
    question_font = ImageFont.truetype(FONT_BOLD, 72)
    draw.rounded_rectangle((900, 345, 1020, 352), radius=3, fill=(235, 0, 40, 255))
    draw.text((960, 405), "FRAGE AUS DEM PUBLIKUM", font=label_font, fill=(235, 0, 40, 255), anchor="mm")

    lines = textwrap.wrap(args.question, width=36)
    if len(lines) > 2:
        raise SystemExit("question must fit on two lines")
    draw.multiline_text(
        (960, 550), "\n".join(lines), font=question_font, fill="white", spacing=20, anchor="mm", align="center"
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    image.save(args.output)
    with Image.open(args.output) as check:
        assert check.size == (1920, 1080) and check.mode == "RGBA"


if __name__ == "__main__":
    main()
