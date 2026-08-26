#!/usr/bin/env python3

import json
import os
import platform
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from video_common import (
    _command_status,
    build_time_map,
    command_identity,
    configured_analyzer,
    content_fingerprint,
    encoder_candidates,
    encoder_options,
    event_context,
    ffconcat_quote,
    host_capabilities,
    privacy_detector_command,
    presentation_bounds,
    resource_budget,
    run_structured_model,
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
assert presentation_bounds({"presentation_start": 10, "presentation_end": 20}, 30) == (10, 20)
assert timeline_events_in_range(11.5, 1, edits, faq) == ["cut 12.000-13.000"]
assert timeline_events_in_range(14, 1, edits, faq) == []
assert build_time_map(3, [{"source_start": 1, "source_end": 2}])["output_duration"] == 2
assert configured_analyzer({"analyzer": "codex"}, "audio") == "codex"
assert encoder_candidates("Darwin") == ["h264_videotoolbox"]
assert encoder_candidates("Linux")[0] == "h264_nvenc"
assert encoder_options("h264_nvenc", "ultrafast") == ["-c:v", "h264_nvenc"]
assert encoder_options("libx264", "slow") == ["-c:v", "libx264", "-preset", "slow"]
assert resource_budget(4, 1, 1)["threads_per_job"] >= 1
assert resource_budget(1, 1, 4)["threads_per_job"] == max(1, (os.cpu_count() or 1) // 4)
detector = [sys.executable, "detector.py", "{inputs}", "{output}"]
detector_string = f'"{sys.executable}" detector.py {{inputs}} {{output}}'
detector_status = _command_status(detector_string)
assert detector_status["available"] and Path(detector_status["command"]).samefile(sys.executable)
if platform.system() != "Darwin":
    try:
        privacy_detector_command({})
    except SystemExit as error:
        assert "no qualified privacy detector" in str(error)
    else:
        raise AssertionError("non-macOS privacy detection must fail closed")

large_prompt = "x" * 200_000
schema = {"type": "object"}
with patch("video_common.subprocess.run") as model_run:
    model_run.return_value = SimpleNamespace(
        stdout=json.dumps({"structured_output": {"ok": True}})
    )
    assert run_structured_model("claude", schema, large_prompt) == {"ok": True}
    assert model_run.call_args.kwargs["input"] == large_prompt
    assert large_prompt not in model_run.call_args.args[0]
    claude_command = model_run.call_args.args[0]
    assert claude_command[claude_command.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert claude_command[claude_command.index("--tools") + 1] == ""
with patch("video_common.subprocess.run") as model_run:
    model_run.return_value = SimpleNamespace(stdout=json.dumps({"ok": True}))
    assert run_structured_model("codex", schema, large_prompt) == {"ok": True}
    assert model_run.call_args.kwargs["input"] == large_prompt
    assert model_run.call_args.args[0][-1] == "-"
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
    qualification = Path(directory) / "qualification.json"
    atomic_qualification = {
        "version": 1,
        "parser_policy": "minimum-height-0.12-v1",
        "detector": command_identity(detector, Path(directory)),
        "labels_sha256": "labels",
        "inputs_sha256": "inputs",
        "detections_sha256": "detections",
        "metrics": {"any_person_recall": 1.0, "overlap_recall": 1.0},
    }
    qualification.write_text(json.dumps(atomic_qualification), encoding="utf-8")
    for configured in (detector, detector_string):
        resolved_detector = privacy_detector_command(
            {
                "_project_dir": directory,
                "privacy_detector_command": configured,
                "privacy_detector_qualification": str(qualification),
            }
        )
        assert Path(resolved_detector[0]).samefile(sys.executable)
    capability_project = {
        "_project_dir": directory,
        "final_resolution": "1920x1080",
        "encoder": "libx265",
        "privacy_detector_command": detector,
        "privacy_detector_qualification": str(qualification),
    }
    with (
        patch("video_common.shutil.which", return_value=sys.executable),
        patch("video_common._ffmpeg_version", return_value="ffmpeg test"),
        patch("video_common._encoder_works", return_value=(True, "")),
    ):
        profile = host_capabilities(capability_project, refresh=True)
        assert profile["video_encoder"] == {
            "name": "libx265",
            "hardware": False,
            "probes": [{"encoder": "libx265", "available": True, "detail": ""}],
        }
        assert profile["privacy_detector"]["qualified"]
        qualification.write_text(
            json.dumps(
                {
                    **atomic_qualification,
                    "metrics": {"any_person_recall": 0.5, "overlap_recall": 1.0},
                }
            ),
            encoding="utf-8",
        )
        assert not host_capabilities(capability_project)["privacy_detector"]["qualified"]
        qualification.write_text(json.dumps(atomic_qualification), encoding="utf-8")
        for project, message in (
            ({**capability_project, "acceleration": "required"}, "hardware acceleration is required"),
            (
                {
                    **capability_project,
                    "encoder": encoder_candidates()[0],
                    "acceleration": "off",
                },
                "hardware acceleration is disabled",
            ),
        ):
            try:
                host_capabilities(project, refresh=True)
            except SystemExit as error:
                assert message in str(error)
            else:
                raise AssertionError(message)

    source = Path(directory) / "source"
    copy = Path(directory) / "copy"
    source.write_bytes(b"portable")
    first = content_fingerprint(source, Path(directory) / "fingerprint.json")
    shutil.copy(source, copy)
    assert first == content_fingerprint(copy)
    timestamps = source.stat()
    source.write_bytes(b"tampered")
    os.utime(source, ns=(timestamps.st_atime_ns, timestamps.st_mtime_ns))
    assert content_fingerprint(source, Path(directory) / "fingerprint.json") != first

print("video-common checks passed")
