#!/usr/bin/env python3

import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import threading
from pathlib import Path

from video_common import (
    atomic_write_json,
    atomic_write_text,
    content_fingerprint,
    encoder_options,
    ffconcat_quote,
    file_sha256,
    host_capabilities,
    presentation_bounds,
    project_path,
    source_to_output,
    timeline_events_in_range,
    whisper_tokens,
)

ROOT = Path(__file__).resolve().parent.parent
WIDTH, HEIGHT = 2160, 3840
WHISPER = ROOT / "build/whisper.cpp/build/bin/whisper-cli"
WHISPER_MODEL = Path.home() / ".cache/openwhispr/whisper-models/ggml-large-v3.bin"


def run(command: list[str]) -> None:
    print("+", " ".join(command))
    subprocess.run(command, check=True)


def source_to_final(source_time: float, project: dict) -> float:
    start = float(project["presentation_start"])
    if source_time < start:
        raise SystemExit("short starts before the presentation")
    edits = json.loads(project_path(project, "edl").read_text(encoding="utf-8")).get("edits", [])
    faq = json.loads(project_path(project, "faq").read_text(encoding="utf-8")).get("entries", [])
    try:
        return source_to_output(source_time, start, edits, faq)
    except ValueError as error:
        raise SystemExit(str(error)) from error


def presentation_range_matches(project: dict, metadata: dict) -> bool:
    try:
        identity = metadata["identity"]
        return (
            float(identity["presentation_start"]) == float(project["presentation_start"])
            and identity.get("presentation_end") == project.get("presentation_end")
        )
    except (KeyError, TypeError, ValueError):
        return False


def transcribe(video: Path, start: float, duration: float, work: Path, project: dict) -> Path:
    wav = work / "audio.wav"
    transcript = work / "transcript.json"
    identity_file = work / "source.json"
    language = str(project.get("language", "de"))
    prompt = str(project.get("transcription_prompt", project.get("presentation_title", "")))
    identity = {
        "final_render": project["_final_render_identity"],
        "start": round(start, 3),
        "duration": round(duration, 3),
        "language": language,
        "prompt": prompt,
    }
    cached = json.loads(identity_file.read_text(encoding="utf-8")) if identity_file.exists() else None
    if transcript.exists() and cached == identity:
        return transcript
    wav_temporary = wav.with_name(f".{wav.stem}.tmp{wav.suffix}")
    run([
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y", "-ss", f"{start:.3f}",
        "-i", str(video), "-t", f"{duration:.3f}", "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le", str(wav_temporary),
    ])
    wav_temporary.replace(wav)
    temporary = transcript.with_name(f".{transcript.stem}.tmp{transcript.suffix}")
    prefix = temporary.with_suffix("")
    command = [
        str(WHISPER), "--threads", str(project.get("audio_threads", project["_audio_threads"])),
        "--beam-size", "1", "--best-of", "1",
        "--model", str(WHISPER_MODEL), "--file", str(wav),
        "--language", language,
        "--prompt", prompt,
        "--split-on-word", "--max-len", "34", "--output-json-full", "--output-file", str(prefix),
    ]
    try:
        run(command)
    except subprocess.CalledProcessError:
        temporary.unlink(missing_ok=True)
        run([command[0], "--no-gpu", *command[1:]])
    temporary.replace(transcript)
    atomic_write_json(identity_file, identity)
    return transcript


def timestamp(seconds: float, separator: str = ",") -> str:
    milliseconds = round(seconds * 1000)
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    secs, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{milliseconds:03d}"


def clean_caption(text: str, replacements: dict[str, str] | None = None) -> str:
    text = " ".join(text.strip().split())
    for pattern, replacement in (replacements or {}).items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text


def transcript_words(data: dict) -> list[dict]:
    words = []
    current = None
    for piece, start, end, _probability in whisper_tokens(data):
        if piece.startswith(" "):
            if current:
                words.append(current)
            current = {"text": piece.strip(), "start": start, "end": end}
        elif current:
            current["text"] += piece
            current["end"] = max(current["end"], end)
        elif piece.strip():
            current = {"text": piece.strip(), "start": start, "end": end}
    if current:
        words.append(current)
    return words


def caption_cues(
    words: list[dict], replacements: dict[str, str] | None = None
) -> list[tuple[float, float, str]]:
    sentences = []
    sentence = []
    for word in words:
        if sentence and word["start"] - sentence[-1]["end"] > 0.75:
            sentences.append(sentence)
            sentence = []
        sentence.append(word)
        if word["text"].endswith((".", "?", "!")):
            sentences.append(sentence)
            sentence = []
    if sentence:
        sentences.append(sentence)

    cues = []
    for sentence in sentences:
        total_chars = sum(len(word["text"]) + 1 for word in sentence)
        chunk_count = max(1, (len(sentence) + 6) // 7, (total_chars + 43) // 44)
        remaining = sentence[:]
        for index in range(chunk_count):
            chunks_left = chunk_count - index
            take = round(len(remaining) / chunks_left)
            take = max(1, min(7, take))
            chunk, remaining = remaining[:take], remaining[take:]
            text = clean_caption(" ".join(word["text"] for word in chunk), replacements)
            cues.append((chunk[0]["start"], chunk[-1]["end"], text))
    return cues


def make_subtitles(transcript: Path, srt: Path, replacements: dict[str, str] | None = None) -> None:
    data = json.loads(transcript.read_text(encoding="utf-8"))
    cues = caption_cues(transcript_words(data), replacements)
    if not cues:
        raise SystemExit(f"no subtitle cues in {transcript}")

    atomic_write_text(
        srt,
        "\n\n".join(
            f"{index}\n{timestamp(start)} --> {timestamp(end)}\n{text}"
            for index, (start, end, text) in enumerate(cues, 1)
        ) + "\n",
    )


def validate(path: Path, expected_duration: float) -> None:
    result = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height,channels,sample_rate:format=duration",
        "-of", "json", str(path),
    ], check=True, capture_output=True, text=True)
    probe = json.loads(result.stdout)
    video = next(stream for stream in probe["streams"] if stream["codec_type"] == "video")
    audio = next(stream for stream in probe["streams"] if stream["codec_type"] == "audio")
    actual = float(probe["format"]["duration"])
    if (video["width"], video["height"]) != (WIDTH, HEIGHT):
        raise SystemExit(f"invalid Short resolution: {video['width']}x{video['height']}")
    if audio["channels"] != 2 or audio["sample_rate"] != "48000":
        raise SystemExit("invalid Short audio: expected 48 kHz stereo")
    if abs(actual - expected_duration) >= 0.2:
        raise SystemExit(
            f"invalid Short duration: {actual:.3f}s, expected {expected_duration:.3f}s"
        )
    run(["ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"])


def pan_expression(points: list[list[float]]) -> str:
    expression = f"{float(points[0][1]):.3f}"
    for (start, previous), (end, current) in zip(points, points[1:], strict=False):
        duration = float(end) - float(start)
        if duration <= 0:
            raise SystemExit("pan keyframes must have increasing timestamps")
        delta = float(current) - float(previous)
        if not delta:
            continue
        progress = f"max(0,min(1,(t-{float(start):.3f})/{duration:.3f}))"
        expression += f"+({delta:.3f})*({progress})*({progress})*(3-2*({progress}))"
    return expression


def short_crop_geometry(source_width: float, source_height: float) -> tuple[float, ...]:
    if source_width <= 0 or source_height <= 0:
        raise SystemExit("timeline source geometry must be positive")
    scale = source_width / 3840
    crop_width, crop_height = 900 * scale, 1600 * scale
    if crop_width > source_width or crop_height > source_height:
        raise SystemExit("Short crop exceeds source geometry")
    return (
        scale,
        crop_width,
        crop_height,
        crop_width * 960 / source_width,
        crop_height * 540 / source_height,
    )


def render_clip(
    raw_video: Path,
    final_video: Path,
    logo_source: Path,
    privacy_mask: Path,
    full_blur_mask: Path,
    clip: dict,
    project: dict,
    output: Path,
    build: Path,
    gpu_slots: threading.Semaphore,
) -> None:
    clip_build = build / clip["id"]
    clip_build.mkdir(parents=True, exist_ok=True)
    duration = float(clip["duration"])
    source_start = float(clip["source_start"])
    timeline = json.loads(project_path(project, "timeline").read_text(encoding="utf-8"))
    presentation_start, presentation_end = presentation_bounds(project, float(timeline["duration"]))
    if not presentation_start <= source_start < source_start + duration <= presentation_end:
        raise SystemExit(f"short {clip['id']} must stay inside the presentation range")
    edits = json.loads(project_path(project, "edl").read_text(encoding="utf-8")).get("edits", [])
    faq = json.loads(project_path(project, "faq").read_text(encoding="utf-8")).get("entries", [])
    events = timeline_events_in_range(source_start, duration, edits, faq)
    if events:
        raise SystemExit(f"short {clip['id']} crosses timeline change: {', '.join(events)}")
    final_start = source_to_final(source_start, project)
    with gpu_slots:
        transcript = transcribe(final_video, final_start, duration, clip_build, project)
    srt = output.with_suffix(".srt")
    temporary_srt = srt.with_name(f".{srt.stem}.tmp{srt.suffix}")
    make_subtitles(transcript, temporary_srt, project.get("caption_replacements"))

    source_width = float(timeline.get("source_width", 3840))
    source_height = float(timeline.get("source_height", source_width * 9 / 16))
    source_scale, crop_width, crop_height, mask_width, mask_height = short_crop_geometry(
        source_width, source_height
    )
    crop_x = f"({pan_expression(clip['pan'])})*{source_scale:.9f}"
    crop_y = float(clip.get("crop_y", 480)) * source_scale
    if crop_width > source_width or crop_height > source_height or crop_y + crop_height > source_height:
        raise SystemExit(f"short {clip['id']} crop exceeds source geometry")
    mask_x = f"({crop_x})*960/{source_width:.9f}"
    mask_y = crop_y * 540 / source_height
    privacy_graph = (
        f"[0:v]crop=w={crop_width:.3f}:h={crop_height:.3f}:x='{crop_x}':y={crop_y:.3f},"
        "setsar=1,format=yuv420p,split=2[clean][blurbase];"
        f"[blurbase]scale={crop_width / 2:.3f}:{crop_height / 2:.3f},boxblur=24:2,"
        f"scale={crop_width:.3f}:{crop_height:.3f}:flags=bilinear,"
        "split=2[blurred_people][blurred_full];"
        f"[1:v]format=gray,crop=w={mask_width:.3f}:h={mask_height:.3f}:x='{mask_x}':y={mask_y:.3f},"
        f"gblur=sigma=8,scale={crop_width:.3f}:{crop_height:.3f}[mask];"
        "[blurred_people][mask]alphamerge[blurred_people_alpha];"
        "[clean][blurred_people_alpha]overlay[partially_private];"
        f"[2:v]format=gray,scale={crop_width:.3f}:{crop_height:.3f}[full_blur_mask];"
        "[blurred_full][full_blur_mask]alphamerge[blurred_full_alpha];"
        "[partially_private][blurred_full_alpha]overlay[private_native];"
        f"[private_native]scale={WIDTH}:{HEIGHT}:flags=lanczos,format=yuv420p[outv]"
    )
    mask_offset = source_start - float(project["presentation_start"])
    private_video = clip_build / "private.mp4"
    normalized_audio = clip_build / "normalized.m4a"
    encoder = project["_video_encoder"]
    render_threads = str(project["_render_threads"])
    encoding = encoder_options(encoder, "ultrafast")
    encoding.extend(("-crf", "18") if encoder == "libx264" else ("-b:v", "20M"))
    run([
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
        "-ss", f"{source_start:.3f}", "-i", str(raw_video),
        "-ss", f"{mask_offset:.3f}", "-i", str(privacy_mask),
        "-ss", f"{mask_offset:.3f}", "-i", str(full_blur_mask),
        "-filter_complex_threads", render_threads,
        "-filter_complex", privacy_graph, "-map", "[outv]", "-t", f"{duration:.3f}", "-an",
        *encoding,
        "-threads", render_threads,
        "-enc_time_base", "1:30",
        "-pix_fmt", "yuv420p", "-fps_mode", "passthrough",
        "-movflags", "+faststart", str(private_video),
    ])
    run([
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
        "-ss", f"{final_start:.3f}", "-i", str(final_video), "-t", f"{duration:.3f}", "-vn",
        "-af", "loudnorm=I=-14:TP=-1.5:LRA=7,aresample=48000",
        "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-ar", "48000",
        "-threads", render_threads, str(normalized_audio),
    ])
    temporary_output = output.with_name(f".{output.stem}.tmp{output.suffix}")
    try:
        run([
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
            "-i", str(private_video), "-i", str(normalized_audio),
            "-loop", "1", "-framerate", "30", "-i", str(logo_source),
            "-filter_complex_threads", render_threads,
            "-filter_complex", f"[2:v]scale={int(project.get('shorts_logo_width', 360))}:-1,format=rgba,"
            f"colorchannelmixer=aa={float(project.get('shorts_logo_opacity', 0.55)):.3f}[mark];"
            "[0:v][mark]overlay=96:96:eof_action=pass[outv]",
            "-map", "[outv]", "-map", "1:a:0", "-t", f"{duration:.3f}",
            *encoding,
            "-threads", render_threads,
            "-enc_time_base", "1:30",
            "-pix_fmt", "yuv420p", "-fps_mode", "passthrough",
            "-c:a", "copy", "-movflags", "+faststart", str(temporary_output),
        ])
        validate(temporary_output, duration)
        temporary_output.replace(output)
        temporary_srt.replace(srt)
    finally:
        temporary_output.unlink(missing_ok=True)
        temporary_srt.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Render clean vertical 4K Shorts from the original meetup video.")
    parser.add_argument("--project", type=Path, default=ROOT / "video-project.json")
    parser.add_argument("--manifest", type=Path, default=ROOT / "shorts.json")
    parser.add_argument("--only", action="append", help="Render only the named clip; may be repeated.")
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--gpu-jobs", type=int, default=1)
    parser.add_argument("--threads", type=int, default=max(1, os.cpu_count() or 1))
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return

    project = json.loads(args.project.read_text(encoding="utf-8"))
    project["_project_dir"] = str(args.project.resolve().parent)
    if min(args.jobs, args.gpu_jobs, args.threads) < 1:
        raise SystemExit("jobs, gpu-jobs, and threads must be positive")
    project["_audio_threads"] = args.threads
    project["_render_threads"] = args.threads
    project["_video_encoder"] = host_capabilities(project)["video_encoder"]["name"]
    global WHISPER, WHISPER_MODEL
    if project.get("whisper_binary"):
        WHISPER = project_path(project, "whisper_binary")
    if project.get("whisper_model"):
        WHISPER_MODEL = project_path(project, "whisper_model")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    raw_video = project_path(project, "video")
    final_video = project_path(project, "final_output")
    if not raw_video.exists() or not final_video.exists():
        raise SystemExit("source video or final render is missing")
    metadata_path = (
        project_path(project, "final_metadata")
        if project.get("final_metadata")
        else Path(project["_project_dir"]) / "output/metadata/final-render.json"
    )
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit("final render metadata is missing or invalid; rerun final") from error
    if not presentation_range_matches(project, metadata):
        raise SystemExit("final render is stale for current presentation range; rerun final")
    inputs = metadata.get("identity", {}).get("files", {})
    for key in ("edl", "faq", "privacy_mask", "full_blur_mask"):
        if inputs.get(key, {}).get("sha256") != file_sha256(project_path(project, key)):
            raise SystemExit(f"final render is stale for current {key}; rerun final")
    if inputs.get("video", {}) != content_fingerprint(
        raw_video, Path(project["_project_dir"]) / "build/source-fingerprint.json"
    ):
        raise SystemExit("final render is stale for the current source video; rerun final")
    artifact = metadata.get("artifact", {})
    final_fingerprint = content_fingerprint(
        final_video, Path(project["_project_dir"]) / "build/final-output-fingerprint.json"
    )
    if (
        int(artifact.get("size", -1)) != final_fingerprint["size"]
        or artifact.get("sha256") != final_fingerprint["sha256"]
    ):
        raise SystemExit("final render does not match its metadata; rerun final")
    project["_final_render_identity"] = metadata.get("identity", {}).get("sha256")
    if not project["_final_render_identity"]:
        raise SystemExit("final render metadata has no identity; rerun final")
    logo_source = project_path(project, "shorts_logo")
    privacy_mask = project_path(project, "privacy_mask")
    full_blur_mask = project_path(project, "full_blur_mask")
    required = (raw_video, final_video, logo_source, privacy_mask, full_blur_mask, WHISPER, WHISPER_MODEL)
    if not all(path.exists() for path in required):
        raise SystemExit("source video, final audio, logo source, privacy masks, or Whisper Large-v3 is missing")

    project_dir = Path(project["_project_dir"])
    build = project_dir / "build/shorts"
    output_dir = project_dir / "output/shorts"
    build.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    clips = [clip for clip in manifest["clips"] if not args.only or clip["id"] in args.only]
    if not clips:
        raise SystemExit("no matching clips")
    gpu_slots = threading.Semaphore(args.gpu_jobs)
    def render_one(clip: dict) -> Path:
        output = output_dir / f"{clip['id']}.mp4"
        render_clip(
            raw_video, final_video, logo_source, privacy_mask, full_blur_mask,
            clip, project, output, build, gpu_slots,
        )
        return output

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(args.jobs, len(clips))) as executor:
        outputs = list(executor.map(render_one, clips))

    concat = build / "review.ffconcat"
    atomic_write_text(
        concat,
        "ffconcat version 1.0\n" + "".join(f"file {ffconcat_quote(path.resolve())}\n" for path in outputs),
    )
    review = project_dir / "output/debug/shorts/shorts-review.mp4"
    review.parent.mkdir(parents=True, exist_ok=True)
    run([
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y", "-safe", "0", "-f", "concat", "-i", str(concat),
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart", str(review),
    ])
    print(review)


def self_test() -> None:
    assert short_crop_geometry(3840, 2160) == (1, 900, 1600, 225, 400)
    assert short_crop_geometry(1920, 1080) == (0.5, 450, 800, 225, 400)
    assert presentation_range_matches(
        {"presentation_start": 12.5, "presentation_end": 20.0},
        {"identity": {"presentation_start": 12.5, "presentation_end": 20.0}},
    )
    assert not presentation_range_matches(
        {"presentation_start": 12.5}, {"identity": {"presentation_start": 13}}
    )
    assert not presentation_range_matches({"presentation_start": 12.5}, {})
    assert clean_caption("  Ein   kurzer Satz ") == "Ein kurzer Satz"
    assert [text for _start, _end, text in caption_cues([
        {"text": word, "start": index * 0.2, "end": index * 0.2 + 0.1}
        for index, word in enumerate("Das Ganze ist Open Source und man braucht keine Lizenz.".split())
    ])] == ["Das Ganze ist Open Source", "und man braucht keine Lizenz."]
    print("render-shorts self-test: ok")


if __name__ == "__main__":
    main()
