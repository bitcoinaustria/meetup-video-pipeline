#!/usr/bin/env python3

import argparse
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PROJECT_DIR = ROOT


def project_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_DIR / path


parser = argparse.ArgumentParser(description="Validate generated audience edits and FAQ cards.")
parser.add_argument("--project", type=Path, default=ROOT / "video-project.json")
args = parser.parse_args()
PROJECT_DIR = args.project.resolve().parent
project = json.loads(args.project.read_text(encoding="utf-8"))
assert float(project.get("faq_scan_start", project["presentation_start"])) <= float(
    project["presentation_start"]
)

raw_slug = str(project.get("name", args.project.stem))
slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", raw_slug).strip("-.") or "presentation"
analysis_path = PROJECT_DIR / "build/faq-analysis" / slug / "model-analysis.json"
reviewed = json.loads(analysis_path.read_text(encoding="utf-8"))["turns"]

expected_minimum = int(project.get("faq_expected_min_turns", 0))
assert len(reviewed) >= expected_minimum, (
    f"FAQ analysis unexpectedly shrank to {len(reviewed)} turns; expected at least {expected_minimum}"
)
early_sentinel = project.get("faq_expected_first_question_before")
if early_sentinel is not None:
    assert any(
        turn["kind"] in {"faq", "followup"}
        and float(turn["source_start"]) < float(early_sentinel)
        for turn in reviewed
    ), "early-presentation FAQ sentinel is missing"

edits = json.loads(project_path(project["edl"]).read_text(encoding="utf-8"))["edits"]
type_by_kind = {
    "faq": "audience_question",
    "followup": "audience_follow_up",
    "comment": "audience_comment",
    "incomplete": "incomplete_audience_tail",
}
missed = [
    f"{turn['kind']}@{float(turn['source_start']):.2f}"
    for turn in reviewed
    if not any(
        float(edit["source_start"]) <= float(turn["source_start"]) + 0.05
        and float(edit["source_end"]) >= float(turn["source_end"]) - 0.05
        and type_by_kind[turn["kind"]] in edit["types"]
        for edit in edits
    )
]
assert not missed, f"audience turns escaped the FAQ EDL: {', '.join(missed)}"

cards = json.loads(project_path(project["faq"]).read_text(encoding="utf-8"))["entries"]
cards_dir = (PROJECT_DIR / "build/faq-analysis" / slug / "cards").resolve()
missing_images = [card["image"] for card in cards if not project_path(card["image"]).is_file()]
assert not missing_images, f"FAQ card images are missing: {', '.join(missing_images)}"
wrong_project_images = [
    card["image"]
    for card in cards
    if project_path(card["image"]).resolve().parent != cards_dir
]
assert not wrong_project_images, (
    f"FAQ cards escaped the project namespace: {', '.join(wrong_project_images)}"
)
minimum_card_answer = float(project.get("faq_card_min_answer_seconds", 12.0))
questions = [
    turn
    for turn in reviewed
    if turn["kind"] == "faq"
    and float(turn["answer_end"]) - float(turn["answer_start"]) >= minimum_card_answer
]
assert len(cards) == len(questions), (
    f"expected {len(questions)} reviewed question cards, got {len(cards)}"
)
card_duration = float(project.get("faq_card_duration", 7.5))
missing_cards = [
    f"{turn['kind']}@{float(turn['source_start']):.2f}"
    for turn in questions
    if not any(
        abs(float(card["source_start"]) - float(turn["answer_start"])) <= 0.05
        and card["question"] == turn["question"]
        and abs(float(card["duration"]) - card_duration) <= 0.001
        for card in cards
    )
]
assert not missing_cards, f"reviewed questions missing cards: {', '.join(missing_cards)}"
print("FAQ coverage checks passed")
