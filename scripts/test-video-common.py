#!/usr/bin/env python3

import platform
import shutil
import sys
import tempfile
from pathlib import Path

from video_common import (
    build_time_map,
    configured_analyzer,
    content_fingerprint,
    encoder_candidates,
    encoder_options,
    event_context,
    ffconcat_quote,
    privacy_detector_command,
    resource_budget,
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
assert encoder_candidates("Darwin") == ["h264_videotoolbox"]
assert encoder_candidates("Linux")[0] == "h264_nvenc"
assert encoder_options("h264_nvenc", "ultrafast") == ["-c:v", "h264_nvenc"]
assert encoder_options("libx264", "slow") == ["-c:v", "libx264", "-preset", "slow"]
assert resource_budget(4, 1, 1)["threads_per_job"] >= 1
detector = [sys.executable, "detector.py", "{inputs}", "{output}"]
resolved_detector = privacy_detector_command({"privacy_detector_command": detector})
assert Path(resolved_detector[0]).samefile(sys.executable) and resolved_detector[1:] == detector[1:]
if platform.system() != "Darwin":
    try:
        privacy_detector_command({})
    except SystemExit as error:
        assert "no qualified privacy detector" in str(error)
    else:
        raise AssertionError("non-macOS privacy detection must fail closed")
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
assert event_context({"event_url": " https://example.com/event ", "event_context": " Talk "}) == {
    "announcement_url": "https://example.com/event",
    "background": "Talk",
}
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
