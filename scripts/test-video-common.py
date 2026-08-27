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
    analysis_range_matches,
    build_time_map,
    detector_command_identity,
    detector_command_sha256,
    configured_analyzer,
    content_fingerprint,
    encoder_candidates,
    encoder_options,
    event_context,
    ffconcat_quote,
    file_sha256,
    host_capabilities,
    privacy_detector_command,
    presentation_bounds,
    resource_budget,
    require_claude_safe_mode,
    run_structured_model,
    source_range_output_duration,
    source_to_output,
    timeline_events_in_range,
    validate_timeline,
    validate_speaker_track,
    whisper_tokens,
)


ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DETECTOR = ROOT / "scripts/test-people-detector.py"
FIXTURE_LABELS = ROOT / "tests/privacy-detector/labels.tsv"
FIXTURE_INPUTS = ROOT / "tests/privacy-detector/inputs.tsv"
edits = [{"source_start": 12, "source_end": 13}]
faq = [{"source_start": 14, "duration": 5}]
assert source_to_output(12, 10, edits, faq) == 2
assert source_to_output(13, 10, edits, faq) == 2
assert source_to_output(14, 10, edits, faq) == 8
assert source_range_output_duration(10, 10, edits, faq) == 14
assert presentation_bounds({"presentation_start": 10, "presentation_end": 20}, 30) == (10, 20)
assert analysis_range_matches({"range": {"start": 10, "duration": 10}}, 10, 20)
assert not analysis_range_matches({"range": {"start": 0, "duration": 30}}, 10, 20)
assert timeline_events_in_range(11.5, 1, edits, faq) == ["cut 12.000-13.000"]
assert timeline_events_in_range(14, 1, edits, faq) == []
assert build_time_map(3, [{"source_start": 1, "source_end": 2}])["output_duration"] == 2
valid_timeline = {"duration": 10, "website_until": 1, "slides": [{"time": 1}, {"time": 5}]}
validate_timeline(valid_timeline)
dual_timeline = {
    **valid_timeline,
    "source_width": 1920,
    "source_height": 1080,
    "participants": {
        "host_a": {"track": "host-a.json", "crop": {"width": 400, "height": 900, "y": 90}},
        "host_b": {"track": "host-b.json", "crop": {"width": 400, "height": 900, "y": 90}},
    },
    "layout_sections": [
        {
            "source_start": 0,
            "source_end": 4,
            "kind": "intro",
            "layout": "dual_speaker",
            "left": "host_a",
            "right": "host_b",
            "active": "left",
        },
        {"source_start": 4, "source_end": 10, "kind": "talk", "layout": "standard"},
    ],
}
validate_timeline(dual_timeline)
validate_speaker_track(
    [
        {"time": 0, "x": 0, "visible": True, "box": [0.05, 0.1, 0.2, 0.8]},
        {"time": 10, "x": 100, "visible": False},
    ],
    10,
    dual_timeline["participants"]["host_a"]["crop"],
    1920,
    visibility=True,
)
validate_speaker_track(
    [{"time": 0, "x": 0}, {"time": 1, "x": 0}],
    10,
    {"width": 400},
    1920,
)
try:
    validate_speaker_track(
        [
            {"time": 0, "x": 0, "visible": True, "box": [0.05, 0.1, 0.2, 0.8]},
            {"time": 1, "x": 0, "visible": True, "box": [0.05, 0.1, 0.2, 0.8]},
        ],
        10,
        {"width": 400},
        1920,
        visibility=True,
    )
except SystemExit:
    pass
else:
    raise AssertionError("participant visibility tracks must review the timeline tail")
for invalid_timeline in (
    {**valid_timeline, "slides": [{"time": 5}, {"time": 1}]},
    {**valid_timeline, "slides": [{"time": 1}, {"time": 1}]},
    {**valid_timeline, "website_until": 2},
    {**valid_timeline, "slides": [{"time": 1}, {"time": 10}]},
    {**valid_timeline, "slides": [{"time": 1}, {"time": 11}]},
    {**valid_timeline, "slides": [{"time": float("nan")}]},
    {
        **dual_timeline,
        "layout_sections": [
            dual_timeline["layout_sections"][0],
            {"source_start": 3, "source_end": 5, "kind": "talk", "layout": "standard"},
        ],
    },
    {
        **dual_timeline,
        "layout_sections": [
            {
                **dual_timeline["layout_sections"][0],
                "right": "host_a",
            }
        ],
    },
    {
        **valid_timeline,
        "mix_mapped_microphones": True,
        "layout_sections": [
            {
                "source_start": 0,
                "source_end": 10,
                "kind": "talk",
                "layout": "standard",
                "audio_channel": 3,
            }
        ],
    },
):
    try:
        validate_timeline(invalid_timeline)
    except SystemExit:
        pass
    else:
        raise AssertionError("invalid timeline must fail before rendering")
assert configured_analyzer({"analyzer": "codex"}, "audio") == "codex"
assert encoder_candidates("Darwin") == ["h264_videotoolbox"]
assert encoder_candidates("Linux")[0] == "h264_nvenc"
assert encoder_options("h264_nvenc", "ultrafast") == ["-c:v", "h264_nvenc"]
assert encoder_options("libx264", "slow") == ["-c:v", "libx264", "-preset", "slow"]
assert resource_budget(4, 1, 1)["threads_per_job"] >= 1
assert resource_budget(1, 1, 4)["threads_per_job"] == max(1, (os.cpu_count() or 1) // 4)
detector = [sys.executable, str(FIXTURE_DETECTOR), "{inputs}", "{output}"]
detector_string = f'"{sys.executable}" "{FIXTURE_DETECTOR}" {{inputs}} {{output}}'
detector_status = _command_status(detector_string)
assert detector_status["available"] and Path(detector_status["command"]).samefile(sys.executable)
try:
    detector_command_identity(detector)
except ValueError as error:
    assert "privacy_detector_artifacts" in str(error)
else:
    raise AssertionError("configured detectors must bind their implementation artifacts")
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
    require_claude_safe_mode.cache_clear()
    model_run.side_effect = [
        SimpleNamespace(stdout="--safe-mode", stderr=""),
        SimpleNamespace(stdout=json.dumps({"structured_output": {"ok": True}})),
    ]
    assert run_structured_model("claude", schema, large_prompt) == {"ok": True}
    assert model_run.call_args.kwargs["input"] == large_prompt
    assert large_prompt not in model_run.call_args.args[0]
    claude_command = model_run.call_args.args[0]
    assert claude_command[claude_command.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert claude_command[claude_command.index("--tools") + 1] == ""
    require_claude_safe_mode.cache_clear()
with patch("video_common.subprocess.run") as model_run:
    model_run.return_value = SimpleNamespace(stdout="", stderr="")
    try:
        require_claude_safe_mode()
    except SystemExit as error:
        assert "--safe-mode" in str(error)
    else:
        raise AssertionError("unsafe Claude CLI must fail closed")
    require_claude_safe_mode.cache_clear()
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
    detector_artifacts = [str(FIXTURE_DETECTOR)]
    qualification = Path(directory) / "qualification.json"
    qualification_detections = Path(directory) / "qualification.detections.tsv"
    qualification_detections.write_text(
        "0\t\n1\t0,0,1,1\n2\t0,0,1,1;0,0,1,1\n",
        encoding="utf-8",
    )
    detector_identity = detector_command_identity(detector, Path(directory), detector_artifacts)
    atomic_qualification = {
        "version": 1,
        "parser_policy": "minimum-height-0.12-v1",
        "detector": detector_identity,
        "command_sha256": detector_command_sha256(detector_identity),
        "labels_sha256": file_sha256(FIXTURE_LABELS),
        "inputs_sha256": file_sha256(FIXTURE_INPUTS),
        "detections_sha256": file_sha256(qualification_detections),
        "files": {
            "labels": str(FIXTURE_LABELS),
            "inputs": str(FIXTURE_INPUTS),
            "detections": str(qualification_detections),
        },
        "metrics": {
            "any_person_recall": 1.0,
            "overlap_recall": 1.0,
            "exact_count_accuracy": 1.0,
        },
    }
    qualification.write_text(json.dumps(atomic_qualification), encoding="utf-8")
    trusted_store = Path(directory) / "trust.json"
    trusted_store.write_text(
        json.dumps(
            {
                "version": 1,
                "approved_qualifications": [
                    {
                        "labels_sha256": atomic_qualification["labels_sha256"],
                        "inputs_sha256": atomic_qualification["inputs_sha256"],
                        "detections_sha256": atomic_qualification["detections_sha256"],
                        "command_sha256": atomic_qualification["command_sha256"],
                        "detector_artifact_sha256s": [file_sha256(FIXTURE_DETECTOR)],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    trust_patch = patch("video_common.DETECTOR_TRUST", trusted_store)
    trust_patch.start()
    for configured in (detector, detector_string):
        resolved_detector = privacy_detector_command(
            {
                "_project_dir": directory,
                "privacy_detector_command": configured,
                "privacy_detector_artifacts": detector_artifacts,
                "privacy_detector_qualification": str(qualification),
            }
        )
        assert Path(resolved_detector[0]).samefile(sys.executable)
    capability_project = {
        "_project_dir": directory,
        "final_resolution": "1920x1080",
        "encoder": "libx265",
        "privacy_detector_command": detector,
        "privacy_detector_artifacts": detector_artifacts,
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
    if platform.system() == "Darwin":
        capability_project["encoder"] = "auto"
        with (
            patch("video_common.shutil.which", return_value=sys.executable),
            patch("video_common._ffmpeg_version", return_value="ffmpeg test"),
            patch(
                "video_common._encoder_works",
                side_effect=[(False, "temporarily busy"), (True, "")],
            ) as encoder_probe,
            patch("video_common.time.sleep"),
        ):
            recovered = host_capabilities(capability_project, refresh=True)
        assert recovered["video_encoder"]["name"] == "h264_videotoolbox"
        assert encoder_probe.call_count == 2
    trust_patch.stop()

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
