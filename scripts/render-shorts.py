#!/usr/bin/env python3

import argparse
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WIDTH, HEIGHT = 2160, 3840
WHISPER = ROOT / "build/whisper.cpp/build/bin/whisper-cli"
WHISPER_MODEL = Path.home() / ".cache/openwhispr/whisper-models/ggml-large-v3.bin"


def project_path(project: dict, key: str) -> Path:
    path = Path(project[key])
    return path if path.is_absolute() else Path(project.get("_project_dir", ROOT)) / path


def run(command: list[str]) -> None:
    print("+", " ".join(command))
    subprocess.run(command, check=True)


def source_to_final(source_time: float, project: dict) -> float:
    start = float(project["presentation_start"])
    if source_time < start:
        raise SystemExit("short starts before the presentation")
    edits = json.loads(project_path(project, "edl").read_text(encoding="utf-8")).get("edits", [])
    faq = json.loads(project_path(project, "faq").read_text(encoding="utf-8")).get("entries", [])
    for edit in edits:
        if float(edit["source_start"]) < source_time < float(edit["source_end"]):
            raise SystemExit("short starts inside a removed passage")
    removed = sum(
        float(edit["source_end"]) - float(edit["source_start"])
        for edit in edits
        if float(edit["source_end"]) <= source_time
    )
    inserted = sum(float(entry.get("duration", 4.0)) for entry in faq if float(entry["source_start"]) <= source_time)
    return source_time - start - removed + inserted


def transcribe(video: Path, start: float, duration: float, work: Path, project: dict) -> Path:
    wav = work / "audio.wav"
    transcript = work / "transcript.json"
    identity_file = work / "source.json"
    stat = video.stat()
    language = str(project.get("language", "de"))
    prompt = str(project.get("transcription_prompt", project.get("presentation_title", "")))
    identity = {
        "video": str(video.resolve()),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "start": round(start, 3),
        "duration": round(duration, 3),
        "language": language,
        "prompt": prompt,
    }
    cached = json.loads(identity_file.read_text(encoding="utf-8")) if identity_file.exists() else None
    if transcript.exists() and cached == identity:
        return transcript
    transcript.unlink(missing_ok=True)
    run([
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y", "-ss", f"{start:.3f}",
        "-i", str(video), "-t", f"{duration:.3f}", "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le", str(wav),
    ])
    prefix = transcript.with_suffix("")
    command = [
        str(WHISPER), "--threads", "8", "--beam-size", "1", "--best-of", "1",
        "--model", str(WHISPER_MODEL), "--file", str(wav),
        "--language", language,
        "--prompt", prompt,
        "--split-on-word", "--max-len", "34", "--output-json-full", "--output-file", str(prefix),
    ]
    try:
        run(command)
    except subprocess.CalledProcessError:
        transcript.unlink(missing_ok=True)
        run([command[0], "--no-gpu", *command[1:]])
    identity_file.write_text(json.dumps(identity, indent=2) + "\n", encoding="utf-8")
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
    for segment in data.get("transcription", []):
        for token in segment.get("tokens", []):
            piece = token.get("text", "")
            if not piece or piece.startswith("["):
                continue
            start = float(token["offsets"]["from"]) / 1000
            end = float(token["offsets"]["to"]) / 1000
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

    srt.write_text(
        "\n\n".join(
            f"{index}\n{timestamp(start)} --> {timestamp(end)}\n{text}"
            for index, (start, end, text) in enumerate(cues, 1)
        ) + "\n",
        encoding="utf-8",
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
    assert (video["width"], video["height"]) == (WIDTH, HEIGHT)
    assert audio["channels"] == 2
    assert audio["sample_rate"] == "48000"
    assert abs(actual - expected_duration) < 0.2
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
) -> None:
    clip_build = build / clip["id"]
    clip_build.mkdir(parents=True, exist_ok=True)
    duration = float(clip["duration"])
    source_start = float(clip["source_start"])
    final_start = source_to_final(source_start, project)
    transcript = transcribe(final_video, final_start, duration, clip_build, project)
    srt = output.with_suffix(".srt")
    make_subtitles(transcript, srt, project.get("caption_replacements"))

    crop_x = pan_expression(clip["pan"])
    crop_y = int(clip.get("crop_y", 480))
    mask_x = f"({crop_x})/4"
    privacy_graph = (
        f"[0:v]crop=w=900:h=1600:x='{crop_x}':y={crop_y},"
        "setsar=1,format=yuv420p,split=2[clean][blurbase];"
        "[blurbase]scale=450:800,boxblur=24:2,scale=900:1600:flags=bilinear,"
        "split=2[blurred_people][blurred_full];"
        f"[1:v]format=gray,crop=w=225:h=400:x='{mask_x}':y={crop_y / 4:.3f},"
        "gblur=sigma=8,scale=900:1600[mask];"
        "[blurred_people][mask]alphamerge[blurred_people_alpha];"
        "[clean][blurred_people_alpha]overlay[partially_private];"
        "[2:v]format=gray,scale=900:1600[full_blur_mask];"
        "[blurred_full][full_blur_mask]alphamerge[blurred_full_alpha];"
        "[partially_private][blurred_full_alpha]overlay[private_native];"
        f"[private_native]scale={WIDTH}:{HEIGHT}:flags=lanczos,format=yuv420p[outv]"
    )
    mask_offset = source_start - float(project["presentation_start"])
    private_video = clip_build / "private.mp4"
    normalized_audio = clip_build / "normalized.m4a"
    run([
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
        "-ss", f"{source_start:.3f}", "-i", str(raw_video),
        "-ss", f"{mask_offset:.3f}", "-i", str(privacy_mask),
        "-ss", f"{mask_offset:.3f}", "-i", str(full_blur_mask),
        "-filter_complex", privacy_graph, "-map", "[outv]", "-t", f"{duration:.3f}", "-an",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-fps_mode", "passthrough",
        "-movflags", "+faststart", str(private_video),
    ])
    run([
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
        "-ss", f"{final_start:.3f}", "-i", str(final_video), "-t", f"{duration:.3f}", "-vn",
        "-af", "loudnorm=I=-14:TP=-1.5:LRA=7,aresample=48000",
        "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-ar", "48000", str(normalized_audio),
    ])
    run([
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
        "-i", str(private_video), "-i", str(normalized_audio),
        "-loop", "1", "-framerate", "30", "-i", str(logo_source),
        "-filter_complex", f"[2:v]scale={int(project.get('shorts_logo_width', 360))}:-1,format=rgba,"
        f"colorchannelmixer=aa={float(project.get('shorts_logo_opacity', 0.55)):.3f}[mark];"
        "[0:v][mark]overlay=96:96:eof_action=pass[outv]",
        "-map", "[outv]", "-map", "1:a:0", "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-fps_mode", "passthrough",
        "-c:a", "copy", "-movflags", "+faststart", str(output),
    ])
    validate(output, duration)


def main() -> None:
    parser = argparse.ArgumentParser(description="Render clean vertical 4K Shorts from the original meetup video.")
    parser.add_argument("--project", type=Path, default=ROOT / "video-project.json")
    parser.add_argument("--manifest", type=Path, default=ROOT / "shorts.json")
    parser.add_argument("--only", action="append", help="Render only the named clip; may be repeated.")
    args = parser.parse_args()

    project = json.loads(args.project.read_text(encoding="utf-8"))
    project["_project_dir"] = str(args.project.resolve().parent)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    raw_video = project_path(project, "video")
    final_video = project_path(project, "final_output")
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
    outputs = []
    for clip in clips:
        output = output_dir / f"{clip['id']}.mp4"
        render_clip(
            raw_video, final_video, logo_source, privacy_mask, full_blur_mask,
            clip, project, output, build,
        )
        outputs.append(output)

    concat = build / "review.ffconcat"
    concat.write_text("ffconcat version 1.0\n" + "".join(f"file '{path.resolve()}'\n" for path in outputs), encoding="utf-8")
    review = output_dir / "shorts-review.mp4"
    run([
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y", "-safe", "0", "-f", "concat", "-i", str(concat),
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart", str(review),
    ])
    print(review)


if __name__ == "__main__":
    assert clean_caption("  Ein   kurzer Satz ") == "Ein kurzer Satz"
    assert [text for _start, _end, text in caption_cues([
        {"text": word, "start": index * 0.2, "end": index * 0.2 + 0.1}
        for index, word in enumerate("Das Ganze ist Open Source und man braucht keine Lizenz.".split())
    ])] == ["Das Ganze ist Open Source", "und man braucht keine Lizenz."]
    main()
