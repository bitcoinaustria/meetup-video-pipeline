#!/usr/bin/env python3

import shutil
import tempfile
from pathlib import Path

from video_common import (
    build_time_map,
    configured_analyzer,
    content_fingerprint,
    ffconcat_quote,
    source_range_output_duration,
    source_to_output,
    timeline_events_in_range,
    whisper_tokens,
)


edits = [{"source_start": 12, "source_end": 13}]
faq = [{"source_start": 14, "duration": 5}]
assert source_to_output(12, 10, edits, faq) == 2
assert source_to_output(13, 10, edits, faq) == 2
assert source_to_output(14, 10, edits, faq) == 8
assert source_range_output_duration(10, 10, edits, faq) == 14
assert timeline_events_in_range(11.5, 1, edits, faq) == ["cut 12.000-13.000"]
assert timeline_events_in_range(14, 1, edits, faq) == []
assert build_time_map(3, [{"source_start": 1, "source_end": 2}])["output_duration"] == 2
assert configured_analyzer({"analyzer": "codex"}, "audio") == "codex"
assert configured_analyzer(
    {"analyzer": "codex", "audio_analyzer": "claude"}, "audio"
) == "claude"
assert configured_analyzer({"analyzer": "codex"}, "audio", "claude") == "claude"
try:
    configured_analyzer({}, "audio")
except SystemExit as error:
    assert "no analyzer selected" in str(error)
else:
    raise AssertionError("missing analyzer must fail closed")
assert ffconcat_quote(Path("it's.mp4")) == "'it'\\''s.mp4'"
assert list(
    whisper_tokens(
        {
            "transcription": [
                {
                    "tokens": [
                        {"text": "[BLANK_AUDIO]", "offsets": {"from": 0, "to": 1}},
                        {"text": " word", "offsets": {"from": 100, "to": 200}, "p": 0.9},
                    ]
                }
            ]
        }
    )
) == [(" word", 0.1, 0.2, 0.9)]

with tempfile.TemporaryDirectory() as directory:
    source = Path(directory) / "source"
    copy = Path(directory) / "copy"
    source.write_bytes(b"portable")
    first = content_fingerprint(source, Path(directory) / "fingerprint.json")
    shutil.copy(source, copy)
    assert first == content_fingerprint(copy)

print("video-common checks passed")
