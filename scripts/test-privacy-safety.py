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

anchor = privacy.Box(0.40, 0.20, 0.10, 0.40, 70, 38)
twin_left = privacy.Box(0.35, 0.20, 0.10, 0.40, 70, 38)
twin_right = privacy.Box(0.45, 0.20, 0.10, 0.40, 70, 38)
tracked, _others = privacy.speaker_and_others(
    [(0.0, [anchor]), (0.1, [twin_left, twin_right]), (0.2, [anchor])],
    (70, 38),
)
assert tracked[1] is None

# Apple Vision may briefly merge two overlapping bodies into one detection.
# The full-camera fallback must therefore start well before the first unsafe frame.
held = privacy.hold_unsafe([False] * 15 + [True])
assert all(held)
print("privacy fail-closed checks passed")
