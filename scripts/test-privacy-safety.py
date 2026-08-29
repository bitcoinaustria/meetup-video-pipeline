#!/usr/bin/env python3

import importlib.util
from pathlib import Path


script = Path(__file__).with_name("build-privacy-review.py")
spec = importlib.util.spec_from_file_location("privacy_review", script)
privacy = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(privacy)

speaker = privacy.Box(0.24085, 0.20376, 0.08121, 0.34832)
overlapping_person = privacy.Box(0.26717, 0.19006, 0.16221, 0.38158)
separate_person = privacy.Box(0.70, 0.10, 0.15, 0.50)

assert privacy.privacy_action(speaker, [overlapping_person]) == "full-blur"
assert privacy.privacy_action(speaker, [separate_person]) == "blur-others"
assert privacy.privacy_action(None, [separate_person]) == "full-blur"
assert privacy.privacy_action(None, []) == "full-blur"

crop_timeline = {
    "source_width": 1000,
    "source_height": 500,
    "speaker_crop": {"width": 400, "height": 500, "y": 0},
}
crop_track = [{"time": 0, "x": 100}, {"time": 1, "x": 100}]
inside = privacy.Box(0.20, 0.10, 0.10, 0.50)
outside = privacy.Box(0.70, 0.10, 0.10, 0.50)
cropped = privacy.crop_detections(
    [(0.0, [inside, outside])], 0.0, crop_timeline, crop_track, {}
)
assert cropped == [(0.0, [inside])]
assert privacy.problem_windows(cropped, 1.0, 0.0) == []
assert privacy.problem_windows([(0.0, [inside, inside])], 1.0, 0.0) == [(0, 1.0)]

participants = {
    "host_a": {"crop": {"width": 100}},
    "host_b": {"crop": {"width": 100}},
}
tracks = {
    "host_a": [{"time": 0, "x": 100, "visible": True, "box": [0.10, 0.20, 0.10, 0.40]}],
    "host_b": [{"time": 0, "x": 700, "visible": True, "box": [0.70, 0.20, 0.10, 0.40]}],
}
references = {"host_a": (0, 0), "host_b": (0, 0)}
section = {"left": "host_a", "right": "host_b"}
host_a = privacy.Box(0.10, 0.20, 0.10, 0.40)
host_b = privacy.Box(0.70, 0.20, 0.10, 0.40)
bystander = privacy.Box(0.88, 0.20, 0.08, 0.35)
authorized, others = privacy.dual_speakers_and_others(
    [host_a, host_b, bystander], 0, section, participants, tracks, references
)
assert authorized == [host_a, host_b] and others == [bystander]
assert privacy.privacy_action(authorized, others) == "blur-others"
missing, _others = privacy.dual_speakers_and_others(
    [host_a], 0, section, participants, tracks, references
)
assert missing is None
ambiguous_tracks = {
    "host_a": [{"time": 0, "x": 450, "visible": True, "box": [0.45, 0.20, 0.10, 0.40]}],
    "host_b": [{"time": 0, "x": 450, "visible": True, "box": [0.45, 0.20, 0.10, 0.40]}],
}
ambiguous, _others = privacy.dual_speakers_and_others(
    [privacy.Box(0.40, 0.2, 0.1, 0.4), privacy.Box(0.50, 0.2, 0.1, 0.4)],
    0,
    section,
    participants,
    ambiguous_tracks,
    references,
)
assert ambiguous is None
substitute = privacy.Box(0.70, 0.20, 0.10, 0.40, 180, 60)
replaced, _others = privacy.dual_speakers_and_others(
    [host_a, substitute], 0, section, participants, tracks, references
)
assert replaced is None
assert privacy.privacy_action(
    [host_a, privacy.Box(0.18, 0.19, 0.12, 0.42)], []
) == "full-blur"

anchor = privacy.Box(0.40, 0.20, 0.10, 0.40, 70, 38)
twin_left = privacy.Box(0.35, 0.20, 0.10, 0.40, 70, 38)
twin_right = privacy.Box(0.45, 0.20, 0.10, 0.40, 70, 38)
tracked, _others = privacy.speaker_and_others(
    [(0.0, [anchor]), (0.1, [twin_left, twin_right]), (0.2, [anchor])],
    (70, 38),
)
assert tracked[1] is None
unanchored, unanchored_others = privacy.speaker_and_others(
    [(0.0, [host_a, host_b])], None
)
assert unanchored == [None] and unanchored_others == [[host_a, host_b]]

# Apple Vision may briefly merge two overlapping bodies into one detection.
# The full-camera fallback must therefore start well before the first unsafe frame.
held = privacy.hold_unsafe([False] * 15 + [True])
assert all(held)
print("privacy fail-closed checks passed")
