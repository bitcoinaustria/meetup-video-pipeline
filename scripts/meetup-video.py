#!/usr/bin/env python3

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
GIB = 1024**3


def load_project(path: Path) -> dict:
    path = path.resolve()
    project = json.loads(path.read_text(encoding="utf-8"))
    required = (
        "video",
        "slides_pdf",
        "background",
        "timeline",
        "slides",
        "edl",
        "faq",
        "privacy_mask",
        "full_blur_mask",
        "presentation_start",
        "final_output",
    )
    missing = [key for key in required if key not in project]
    if missing:
        raise SystemExit(f"project is missing: {', '.join(missing)}")
    project["_project_dir"] = str(path.parent)
    return project


def resolve_project_path(project: dict, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else Path(project.get("_project_dir", ROOT)) / path


def project_path(project: dict, key: str) -> Path:
    return resolve_project_path(project, project[key])


def init_project(project_file: Path, name: str) -> None:
    if project_file.exists():
        raise SystemExit(f"project already exists: {project_file}")
    project_dir = project_file.resolve().parent
    project_dir.mkdir(parents=True, exist_ok=True)
    for directory in ("source", "build", "tmp", "output"):
        (project_dir / directory).mkdir(exist_ok=True)
    project = json.loads((ROOT / "video-project.example.json").read_text(encoding="utf-8"))
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("-.").lower() or "presentation"
    project.update(
        {
            "name": slug,
            "presentation_title": name,
            "background": os.path.relpath(ROOT / "Background.png", project_dir),
            "shorts_logo": os.path.relpath(ROOT / "CoverLogo.png", project_dir),
        }
    )
    project_file.write_text(json.dumps(project, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    source = {"path": project["video"]}
    (project_dir / "manual-edits.json").write_text(
        json.dumps({"version": 1, "source": source, "edits": []}, indent=2) + "\n",
        encoding="utf-8",
    )
    (project_dir / "shorts.json").write_text(
        json.dumps({"version": 1, "clips": []}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(project_file)


def project_slug(project: dict, project_file: Path) -> str:
    raw = str(project.get("name", project_file.stem))
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", raw).strip("-.")
    return slug or "presentation"


def youtube_time(seconds: float) -> str:
    total = max(0, round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


def slide_titles(slides_text: Path, presentation_title: str) -> dict[int, str]:
    titles = {}
    for page, content in enumerate(slides_text.read_text(encoding="utf-8").split("\f"), start=1):
        lines = [" ".join(line.split()) for line in content.splitlines() if line.strip()]
        candidates = [
            line
            for line in lines
            if presentation_title.casefold() not in line.casefold()
            and not re.fullmatch(r"\d+", line)
            and not re.search(r"\s\d+$", line)
            and not line.startswith(("•", "▪", "→"))
            and not re.match(r"^\d+[.)]\s", line)
            and len(line) <= 90
        ]
        if candidates:
            title = candidates[0]
            if (
                len(candidates) > 1
                and title.casefold().endswith((" on", " in", " of", " and", " to", " —", " -"))
                and candidates[1][:1].islower()
                and len(candidates[1].split()) <= 4
            ):
                title += " " + candidates[1]
            titles[page] = title
    return titles


def chapter_entries(project: dict) -> list[tuple[float, str]]:
    slides_text = project_path(project, "slides_text")
    slides_text.parent.mkdir(parents=True, exist_ok=True)
    if not slides_text.exists():
        subprocess.run(
            ["pdftotext", "-layout", str(project_path(project, "slides_pdf")), str(slides_text)],
            check=True,
        )
    titles = slide_titles(slides_text, project["presentation_title"])
    timeline = json.loads(project_path(project, "timeline").read_text(encoding="utf-8"))
    edits = json.loads(project_path(project, "edl").read_text(encoding="utf-8"))["edits"]
    faq = json.loads(project_path(project, "faq").read_text(encoding="utf-8"))["entries"]
    start = float(project["presentation_start"])

    def output_time(source_time: float) -> float:
        removed = sum(
            max(
                0.0,
                min(source_time, float(edit["source_end"]))
                - max(start, float(edit["source_start"])),
            )
            for edit in edits
            if float(edit["source_start"]) < source_time
        )
        inserted = sum(
            float(entry["duration"])
            for entry in faq
            if start <= float(entry["source_start"]) <= source_time
        )
        return max(0.0, source_time - start - removed + inserted)

    minimum_gap = float(project.get("chapter_min_seconds", 180.0))
    chapters = [(0.0, project["presentation_title"])]
    used_titles = {project["presentation_title"]}
    for slide in timeline["slides"]:
        source_time = float(slide["time"])
        title = titles.get(int(slide["page"]))
        mapped = output_time(source_time)
        if (
            source_time > start
            and title
            and title not in used_titles
            and mapped - chapters[-1][0] >= minimum_gap
        ):
            chapters.append((mapped, title))
            used_titles.add(title)
    if len(chapters) < 3 or any(
        later[0] - earlier[0] < 10 for earlier, later in zip(chapters, chapters[1:])
    ):
        raise SystemExit("YouTube chapters need at least three entries, each at least 10 seconds apart")
    return chapters


def chapters_text(project: dict) -> str:
    return "\n".join(f"{youtube_time(seconds)} {title}" for seconds, title in chapter_entries(project))


def write_chapters(project: dict, project_file: Path) -> Path:
    output = Path(
        project.get(
            "chapters_output",
            f"output/metadata/{project_slug(project, project_file)}-youtube-chapters.txt",
        )
    )
    if not output.is_absolute():
        output = Path(project.get("_project_dir", ROOT)) / output
    output.parent.mkdir(parents=True, exist_ok=True)
    text = chapters_text(project)
    output.write_text(text + "\n", encoding="utf-8")
    publishing = project_path(project, "publishing_copy") if project.get("publishing_copy") else None
    if publishing and publishing.exists():
        current = publishing.read_text(encoding="utf-8")
        updated, count = re.subn(
            r"(?ms)(^Kapitel\n).*?(?=\n\n## )",
            lambda match: match.group(1) + text,
            current,
            count=1,
        )
        if count:
            publishing.write_text(updated, encoding="utf-8")
    print(output)
    return output


def duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(result.stdout.strip())


def validate_render(path: Path, expected_resolution: str = "1920x1080") -> None:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,width,height,channels:format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise SystemExit(f"invalid render: {result.stderr.strip()}")
    probe = json.loads(result.stdout)
    video = next((stream for stream in probe["streams"] if stream["codec_type"] == "video"), None)
    audio = next((stream for stream in probe["streams"] if stream["codec_type"] == "audio"), None)
    expected_width, expected_height = map(int, expected_resolution.split("x"))
    if not video or (video.get("width"), video.get("height")) != (expected_width, expected_height):
        size = f"{video.get('width')}x{video.get('height')}" if video else "missing"
        raise SystemExit(f"invalid render: video is {size}, expected {expected_resolution}")
    if not audio or audio.get("channels") != 2:
        raise SystemExit("invalid render: expected two audio channels")

    seconds = float(probe["format"]["duration"])
    visible = 0
    for timestamp in (1.0, seconds * 0.37, seconds * 0.73):
        frame = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-ss",
                f"{timestamp:.3f}",
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-vf",
                "scale=16:9,format=gray",
                "-f",
                "rawvideo",
                "-",
            ],
            capture_output=True,
        )
        if frame.returncode or len(frame.stdout) != 16 * 9:
            raise SystemExit(f"invalid render: cannot decode frame at {timestamp:.1f}s")
        visible += max(frame.stdout) >= 16
    if visible != 3:
        raise SystemExit(f"invalid render: {3 - visible} of 3 sampled frames are black")
    print(f"validated render: {expected_resolution}, stereo, {seconds:.3f}s, three visible sample frames")


def check(project: dict, project_file: Path, final: bool = False) -> None:
    keys = (
        "video",
        "slides_pdf",
        "background",
        "timeline",
        "slides",
        "edl",
        "faq",
        "privacy_mask",
        "full_blur_mask",
    )
    if project.get("audio_edits"):
        keys += ("audio_edits",)
    if project.get("base_edits"):
        keys += ("base_edits",)
    missing = [str(project_path(project, key)) for key in keys if not project_path(project, key).exists()]
    if missing:
        raise SystemExit("missing inputs:\n" + "\n".join(missing))
    final_edits = json.loads(project_path(project, "edl").read_text(encoding="utf-8")).get("edits", [])

    def represented(candidate: dict) -> bool:
        return any(
            float(edit["source_start"]) <= float(candidate["source_start"]) + 1e-6
            and float(edit["source_end"]) >= float(candidate["source_end"]) - 1e-6
            and set(candidate.get("types", [])).issubset(edit.get("types", []))
            for edit in final_edits
        )

    if project.get("audio_edits"):
        audio_edits = json.loads(project_path(project, "audio_edits").read_text(encoding="utf-8"))
        status = audio_edits.get("safety", {}).get("semantic_review_status")
        if status not in {"passed", "cached"}:
            raise SystemExit(f"audio semantic review is not approved: {status or 'missing'}")
        configured_video = project_path(project, "video")
        audio_source = audio_edits.get("source", {})
        audio_source_path = resolve_project_path(project, str(audio_source.get("path", "")))
        stat = configured_video.stat()
        if (
            audio_source_path.resolve() != configured_video.resolve()
            or int(audio_source.get("size", -1)) != stat.st_size
            or int(audio_source.get("mtime_ns", -1)) != stat.st_mtime_ns
        ):
            raise SystemExit("audio edit artifact is stale for the configured source video")
        for audio_edit in audio_edits.get("edits", []):
            if not represented(audio_edit):
                raise SystemExit(f"approved audio edit is missing from the final EDL: {audio_edit}")
    if project.get("base_edits"):
        base_edits = json.loads(project_path(project, "base_edits").read_text(encoding="utf-8"))
        base_source = resolve_project_path(project, str(base_edits.get("source", {}).get("path", "")))
        if base_source.resolve() != project_path(project, "video").resolve():
            raise SystemExit("base edits do not belong to the configured source video")
        for base_edit in base_edits.get("edits", []):
            if not represented(base_edit):
                raise SystemExit(f"base edit is missing from the final EDL: {base_edit}")

    source_seconds = duration(project_path(project, "video"))
    start = float(project["presentation_start"])
    expected = source_seconds - start
    for key in ("privacy_mask", "full_blur_mask"):
        actual = duration(project_path(project, key))
        if actual + 1 / 30 < expected:
            raise SystemExit(f"{key} is {expected - actual:.3f}s too short")

    subprocess.run([sys.executable, str(ROOT / "scripts/test-privacy-safety.py")], check=True)
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/test-faq-coverage.py"),
            "--project",
            str(project_file),
        ],
        check=True,
    )
    chapter_entries(project)
    free = shutil.disk_usage(ROOT).free
    final_resolution = project.get("final_resolution", "3840x2160")
    estimated_bitrate = 24_000_000 if final_resolution == "3840x2160" else 8_000_000
    estimated_output = expected * estimated_bitrate / 8
    required_free = max(6 * GIB, estimated_output * 1.35)
    print(f"inputs and privacy checks passed; free disk: {free / GIB:.1f} GiB")
    if final and free < required_free:
        raise SystemExit(
            f"final render needs about {required_free / GIB:.1f} GiB free; "
            f"only {free / GIB:.1f} GiB available while preserving the previous render"
        )


def render(
    project: dict,
    project_file: Path,
    start: float,
    render_duration: float | None,
    output: Path,
    final: bool,
) -> None:
    if final:
        check(project, project_file, final=True)
    resolution = project.get("final_resolution", "3840x2160") if final else "1920x1080"
    command = [
        sys.executable,
        str(ROOT / "scripts/render-video.py"),
        "--video",
        str(project_path(project, "video")),
        "--project-dir",
        str(project.get("_project_dir", ROOT)),
        "--timeline",
        str(project_path(project, "timeline")),
        "--background",
        str(project_path(project, "background")),
        "--slides",
        str(project_path(project, "slides")),
        "--edl",
        str(project_path(project, "edl")),
        "--faq-timeline",
        str(project_path(project, "faq")),
        "--privacy-mask",
        str(project_path(project, "privacy_mask")),
        "--full-blur-mask",
        str(project_path(project, "full_blur_mask")),
        "--privacy-mask-start",
        str(project["presentation_start"]),
        "--start",
        str(start),
        "--resolution",
        resolution,
        "--encoder",
        project.get("encoder", "libx264"),
        "--preset",
        project.get("final_preset" if final else "preview_preset", "ultrafast"),
        "--output",
        str(output),
    ]
    if render_duration is not None:
        command.extend(("--duration", str(render_duration)))
    subprocess.run(command, check=True)


def final_render(project: dict, project_file: Path) -> None:
    output = project_path(project, "final_output")
    output.parent.mkdir(parents=True, exist_ok=True)
    lock = output.with_suffix(output.suffix + ".lock")
    temporary = output.with_name(f"{output.stem}.rendering{output.suffix}")
    try:
        lock.mkdir()
    except FileExistsError:
        raise SystemExit(f"final render already active or stale lock exists: {lock}") from None
    try:
        temporary.unlink(missing_ok=True)
        render(
            project,
            project_file,
            float(project["presentation_start"]),
            None,
            temporary,
            final=True,
        )
        validate_render(temporary, project.get("final_resolution", "3840x2160"))
        temporary.replace(output)
        print(output)
    finally:
        temporary.unlink(missing_ok=True)
        lock.rmdir()


def publishing_copy(project: dict, project_file: Path) -> None:
    slides_text = project_path(project, "slides_text")
    slides_text.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["pdftotext", "-layout", str(project_path(project, "slides_pdf")), str(slides_text)],
        check=True,
    )
    faq_context = (
        Path(project.get("_project_dir", ROOT))
        / "build/faq-analysis"
        / project_slug(project, project_file)
        / "faq-candidates.md"
    )
    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "description": {"type": "string"},
            "x_post": {"type": "string"},
        },
        "required": ["title", "description", "x_post"],
        "additionalProperties": False,
    }
    prompt = f"""
Create publication copy for a recorded {project['organization']} meetup presentation.

Read these local sources before writing:
- slides: {slides_text}
- reconstructed Q&A context: {faq_context}

Facts:
- Presentation title: {project['presentation_title']}
- Speaker: {project['speaker']}
- Publisher: {project['organization']} ({project['x_handle']})
- Language of the talk and copy: {project.get('copy_language', project.get('language', 'de'))}

Refer to the speaker only as {project['speaker']}. Never use a surname, even if one appears in the sources.

Return exactly one YouTube title, one YouTube description, and one X/Twitter announcement post.
Voice: {project.get('publishing_voice', 'technically literate, direct, sober, confident without hype')}.
No emoji pile, no clickbait, and no invented URLs, dates, sponsors, technical claims, or event details.
Explain the subject clearly enough for someone who did not attend while retaining important limitations and tradeoffs.
Mention the Q&A only if useful. Keep the description between 1,200 and 1,800 characters.
Keep the title below 100 characters. End x_post with the literal placeholder {{YOUTUBE_URL}} and keep the complete
x_post, including that placeholder, at or below 270 characters so replacing it with a 23-character t.co URL remains safe.
Do not invent or include chapters or timecodes; they are appended deterministically.
Do not use markdown inside the three values.
""".strip()
    result = subprocess.run(
        [
            "claude",
            "-p",
            "--allowedTools",
            "Read",
            "--permission-mode",
            "dontAsk",
            "--no-session-persistence",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema),
            prompt,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    envelope = json.loads(result.stdout)
    copy = envelope.get("structured_output")
    if copy is None:
        raw = envelope.get("result", "")
        copy = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(copy, dict) or len(copy.get("x_post", "")) > 270:
        raise SystemExit("Claude returned invalid publishing copy")
    output = project_path(project, "publishing_copy")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "# Publishing copy\n\n"
        "Generated via Claude CLI using its configured default model.\n\n"
        f"## YouTube title\n\n{copy['title']}\n\n"
        f"## YouTube description\n\n{copy['description']}\n\nKapitel\n{chapters_text(project)}\n\n"
        f"## X / Twitter announcement\n\n{copy['x_post']}\n",
        encoding="utf-8",
    )
    print(output)


def build_faq(project_file: Path) -> None:
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/build-faq.py"), "--project", str(project_file)],
        check=True,
    )


def build_audio(project: dict, project_file: Path) -> None:
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/audio-post.py"),
            "analyze",
            "--project",
            str(project_file),
            "--video",
            str(project_path(project, "video")),
            "--timeline",
            str(project_path(project, "timeline")),
            "--language",
            str(project.get("language", "de")),
        ],
        check=True,
    )


def render_shorts(project: dict, project_file: Path) -> None:
    manifest = project_path(project, "shorts_manifest")
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/render-shorts.py"),
            "--project",
            str(project_file),
            "--manifest",
            str(manifest),
        ],
        check=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="One-command meetup video workflow.")
    parser.add_argument("--project", type=Path, default=ROOT / "video-project.json")
    subparsers = parser.add_subparsers(dest="command", required=True)
    init = subparsers.add_parser("init")
    init.add_argument("--name", required=True)
    subparsers.add_parser("check")
    preview = subparsers.add_parser("preview")
    preview.add_argument("--start", type=float)
    preview.add_argument("--duration", type=float, default=60.0)
    preview.add_argument("--output", type=Path)
    subparsers.add_parser("copy")
    subparsers.add_parser("chapters")
    subparsers.add_parser("audio")
    subparsers.add_parser("faq")
    subparsers.add_parser("shorts")
    validate = subparsers.add_parser("validate")
    validate.add_argument("--input", type=Path)
    subparsers.add_parser("final")
    subparsers.add_parser("release")
    args = parser.parse_args()
    if args.command == "init":
        init_project(args.project, args.name)
        return
    project = load_project(args.project)

    if args.command == "check":
        check(project, args.project)
    elif args.command == "preview":
        start = args.start if args.start is not None else float(project["presentation_start"])
        output = (
            args.output
            or Path(project["_project_dir"]) / "output/debug/previews/preview-1080p.mp4"
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        render(project, args.project, start, args.duration, output, final=False)
    elif args.command == "copy":
        publishing_copy(project, args.project)
    elif args.command == "chapters":
        write_chapters(project, args.project)
    elif args.command == "audio":
        build_audio(project, args.project)
    elif args.command == "faq":
        build_audio(project, args.project)
        build_faq(args.project)
    elif args.command == "shorts":
        render_shorts(project, args.project)
    elif args.command == "validate":
        validate_render(
            args.input or project_path(project, "final_output"),
            "1920x1080" if args.input else project.get("final_resolution", "3840x2160"),
        )
    else:
        build_audio(project, args.project)
        build_faq(args.project)
        if args.command == "release":
            publishing_copy(project, args.project)
        final_render(project, args.project)


if __name__ == "__main__":
    main()
