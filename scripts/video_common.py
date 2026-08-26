#!/usr/bin/env python3

import bisect
import functools
import hashlib
import json
import os
import platform
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DETECTOR_TRUST = ROOT / "privacy-detector-trust.json"
_HOST_CAPABILITY_CACHE: dict[str, dict] = {}


def resource_budget(
    jobs: int | None = None,
    gpu_jobs: int | None = None,
    render_jobs: int | None = None,
) -> dict[str, int]:
    cpus = max(1, os.cpu_count() or 1)
    workers = jobs if jobs is not None else min(4, max(1, cpus // 2))
    gpu_workers = gpu_jobs if gpu_jobs is not None else 1
    render_workers = render_jobs if render_jobs is not None else 1
    if min(workers, gpu_workers, render_workers) < 1:
        raise SystemExit("jobs, gpu-jobs, and render-jobs must be positive")
    return {
        "cpus": cpus,
        "jobs": workers,
        "gpu_jobs": gpu_workers,
        "render_jobs": render_workers,
        "threads_per_job": max(1, cpus // render_workers),
    }


def encoder_candidates(system: str | None = None) -> list[str]:
    system = system or platform.system()
    if system == "Darwin":
        return ["h264_videotoolbox"]
    if system == "Windows":
        return ["h264_nvenc", "h264_qsv", "h264_amf"]
    if system == "Linux":
        return ["h264_nvenc", "h264_qsv", "h264_amf"]
    return []


def encoder_options(encoder: str, preset: str | None = None) -> list[str]:
    options = ["-c:v", encoder]
    if preset and encoder in {"libx264", "libx265"}:
        options.extend(("-preset", preset))
    return options


def _ffmpeg_version(ffmpeg: str) -> str:
    result = subprocess.run(
        [ffmpeg, "-version"], check=True, capture_output=True, text=True, timeout=10
    )
    return result.stdout.splitlines()[0]


def _encoder_works(ffmpeg: str, encoder: str, resolution: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"color=c=black:s={resolution}:r=1:d=1",
                "-frames:v",
                "1",
                "-an",
                "-c:v",
                encoder,
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, str(error)
    detail = (result.stderr or "").strip().splitlines()
    return result.returncode == 0, detail[-1] if detail else ""


def _command_status(
    command: object, default: Path | None = None, base: Path = ROOT
) -> dict[str, object]:
    parts = list(command) if isinstance(command, list) else shlex.split(str(command or ""))
    configured = parts[0] if parts else None
    executable = str(configured or default or "")
    if not executable:
        return {"available": False, "command": ""}
    path = Path(executable)
    local = path if path.is_absolute() else base / path
    resolved = str(local.resolve()) if local.exists() else shutil.which(executable)
    return {"available": bool(resolved), "command": resolved or executable}


def normalize_command(command: object, base: Path = ROOT) -> list[str]:
    parts = list(command) if isinstance(command, list) else shlex.split(str(command or ""))
    if not parts:
        return []
    executable = Path(parts[0])
    local = executable if executable.is_absolute() else base / executable
    resolved = str(local.resolve()) if local.exists() else shutil.which(parts[0])
    if resolved:
        parts[0] = resolved
    return parts


def command_identity(command: object, base: Path = ROOT) -> dict:
    parts = normalize_command(command, base)
    files = {}
    for index, part in enumerate(parts):
        if "{" in part:
            continue
        candidate = Path(part)
        if not candidate.is_absolute():
            candidate = base / candidate
        if candidate.is_file():
            files[str(index)] = {
                "size": candidate.stat().st_size,
                "sha256": file_sha256(candidate),
            }
    return {"command": parts, "files": files}


def detector_command_identity(
    command: object, base: Path = ROOT, artifacts: list[str] | None = None
) -> dict:
    if not isinstance(artifacts, list) or not artifacts or not all(
        isinstance(value, str) and value for value in artifacts
    ):
        raise ValueError("privacy_detector_artifacts must list detector code and model files")
    identity = command_identity(command, base)
    bound = []
    for value in artifacts:
        path = resolve_project_path({"_project_dir": str(base)}, value)
        if not path.is_file():
            raise ValueError(f"detector artifact is unavailable: {path}")
        bound.append(
            {
                "path": str(path.resolve()),
                "size": path.stat().st_size,
                "sha256": file_sha256(path),
            }
        )
    return {**identity, "artifacts": bound}


def parse_detection_coordinates(encoded: str, minimum_height: float = 0.12) -> list[tuple[float, ...]]:
    boxes = [tuple(map(float, item.split(","))) for item in encoded.split(";") if item]
    return [box for box in boxes if len(box) == 4 and box[3] >= minimum_height]


def expected_detection_counts(path: Path) -> dict[float, int]:
    rows = (
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    return {
        float(timestamp): int(count)
        for timestamp, count in (line.split("\t", 1) for line in rows)
    }


def actual_detection_counts(path: Path) -> dict[float, int]:
    counts = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        timestamp, encoded = (line.split("\t", 1) + [""])[:2]
        counts[float(timestamp)] = len(parse_detection_coordinates(encoded))
    return counts


def score_detection_counts(labels: dict[float, int], detections: dict[float, int]) -> dict[str, float]:
    people = [time for time, count in labels.items() if count > 0]
    overlaps = [time for time, count in labels.items() if count > 1]
    exact = sum(detections.get(time, 0) == count for time, count in labels.items())
    return {
        "any_person_recall": (
            sum(detections.get(time, 0) > 0 for time in people) / len(people)
            if people
            else 1.0
        ),
        "overlap_recall": (
            sum(detections.get(time, 0) > 1 for time in overlaps) / len(overlaps)
            if overlaps
            else 1.0
        ),
        "exact_count_accuracy": exact / max(1, len(labels)),
    }


def detector_qualification_is_trusted(artifact: dict, detector_identity: dict) -> bool:
    try:
        trust = json.loads(DETECTOR_TRUST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    artifact_hashes = sorted(item["sha256"] for item in detector_identity.get("artifacts", []))
    return trust.get("version") == 1 and any(
        entry.get("labels_sha256") == artifact.get("labels_sha256")
        and entry.get("inputs_sha256") == artifact.get("inputs_sha256")
        and sorted(entry.get("detector_artifact_sha256s", [])) == artifact_hashes
        for entry in trust.get("approved_qualifications", [])
    )


def detector_qualification(project: dict, command: object) -> dict[str, object]:
    configured = project.get("privacy_detector_command", project.get("people_detector"))
    if not configured and platform.system() == "Darwin":
        return {"qualified": True, "source": "apple-vision-default"}
    value = project.get("privacy_detector_qualification")
    if not value:
        return {"qualified": False, "reason": "missing privacy_detector_qualification"}
    path = resolve_project_path(project, str(value))
    try:
        artifact = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return {"qualified": False, "reason": f"invalid qualification artifact: {error}"}
    try:
        detector_identity = detector_command_identity(
            command,
            Path(project.get("_project_dir", ROOT)),
            project.get("privacy_detector_artifacts"),
        )
        trusted = detector_qualification_is_trusted(artifact, detector_identity)
        files = artifact.get("files", {})
        labels = Path(files["labels"])
        inputs = Path(files["inputs"])
        detections = Path(files["detections"])
        if (
            file_sha256(labels) != artifact.get("labels_sha256")
            or file_sha256(inputs) != artifact.get("inputs_sha256")
            or file_sha256(detections) != artifact.get("detections_sha256")
        ):
            raise ValueError("qualification files do not match their recorded hashes")
        metrics = score_detection_counts(
            expected_detection_counts(labels), actual_detection_counts(detections)
        )
        qualified = bool(
            artifact.get("version") == 1
            and artifact.get("parser_policy") == "minimum-height-0.12-v1"
            and artifact.get("detector")
            == detector_identity
            and artifact.get("labels_sha256")
            and artifact.get("inputs_sha256")
            and artifact.get("detections_sha256")
            and artifact.get("metrics") == metrics
            and float(metrics.get("any_person_recall", 0)) >= 0.99
            and float(metrics.get("overlap_recall", 0)) >= 0.90
            and trusted
        )
    except (KeyError, OSError, TypeError, ValueError) as error:
        qualified = False
        reason = str(error)
    else:
        reason = (
            "qualification dataset is not approved by the repository trust store"
            if not trusted
            else "qualification does not match detector or recall contract"
        )
    return {
        "qualified": qualified,
        "source": str(path),
        "reason": "" if qualified else reason,
    }


def host_capabilities(project: dict, *, refresh: bool = False) -> dict:
    project_dir = Path(project.get("_project_dir", ROOT))
    output = project_dir / "build/host-capabilities.json"
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SystemExit("ffmpeg is unavailable")
    system = platform.system()
    default_people = ROOT / "scripts/vision-people.swift" if system == "Darwin" else None
    detector_command = project.get("privacy_detector_command", project.get("people_detector"))
    qualification = optional_project_path(project, "privacy_detector_qualification")
    try:
        detector_identity = (
            detector_command_identity(
                detector_command,
                project_dir,
                project.get("privacy_detector_artifacts"),
            )
            if detector_command
            else command_identity(default_people, project_dir)
        )
    except ValueError as error:
        detector_identity = {
            "command": command_identity(detector_command, project_dir),
            "error": str(error),
        }
    signature = {
        "system": system,
        "machine": platform.machine(),
        "ffmpeg": ffmpeg,
        "ffmpeg_version": _ffmpeg_version(ffmpeg),
        "acceleration": str(project.get("acceleration", "auto")),
        "requested_encoder": str(project.get("encoder", "auto")),
        "final_resolution": str(project.get("final_resolution", "3840x2160")),
        "privacy_detector_command": project.get(
            "privacy_detector_command", project.get("people_detector")
        ),
        "privacy_detector_qualification": project.get("privacy_detector_qualification"),
        "privacy_detector_identity": detector_identity,
        "privacy_detector_qualification_identity": (
            content_fingerprint(qualification) if qualification and qualification.exists() else None
        ),
        "privacy_detector_trust_identity": content_fingerprint(DETECTOR_TRUST),
        "ocr_command": project.get("ocr_command"),
    }
    cache_key = json.dumps(signature, ensure_ascii=False, sort_keys=True)
    if not refresh and cache_key in _HOST_CAPABILITY_CACHE:
        return _HOST_CAPABILITY_CACHE[cache_key]

    policy = signature["acceleration"]
    if policy not in {"auto", "off", "required"}:
        raise SystemExit("acceleration must be auto, off, or required")
    requested = signature["requested_encoder"]
    hardware_encoders = {
        encoder
        for system in ("Darwin", "Linux", "Windows")
        for encoder in encoder_candidates(system)
    }
    if policy == "off" and requested in hardware_encoders:
        raise SystemExit("hardware acceleration is disabled but a hardware encoder was requested")
    resolution = signature["final_resolution"]
    if resolution not in {"1920x1080", "3840x2160"}:
        raise SystemExit("final_resolution must be 1920x1080 or 3840x2160")
    candidates = (
        [requested]
        if requested != "auto"
        else ([] if policy == "off" else encoder_candidates(signature["system"])) + ["libx264"]
    )
    probes = []
    selected = None
    for encoder in candidates:
        works, detail = _encoder_works(ffmpeg, encoder, resolution)
        probes.append({"encoder": encoder, "available": works, "detail": detail})
        if works:
            selected = encoder
            break
    if not selected:
        raise SystemExit(f"no usable FFmpeg H.264 encoder found: {', '.join(candidates)}")
    hardware = selected in hardware_encoders
    if policy == "required" and not hardware:
        raise SystemExit("hardware acceleration is required but no supported encoder passed its smoke test")
    if requested != "auto" and selected != requested:
        raise SystemExit(f"requested encoder is unavailable: {requested}")

    default_ocr = ROOT / "scripts/vision-ocr.swift" if signature["system"] == "Darwin" else None
    detector_status = _command_status(detector_command, default_people, project_dir)
    detector_status.update(detector_qualification(project, detector_command or default_people))
    profile = {
        "version": 1,
        "signature": signature,
        "video_encoder": {"name": selected, "hardware": hardware, "probes": probes},
        "privacy_detector": detector_status,
        "ocr": _command_status(project.get("ocr_command"), default_ocr, project_dir),
    }
    atomic_write_json(output, profile)
    _HOST_CAPABILITY_CACHE[cache_key] = profile
    return profile


def privacy_detector_command(project: dict) -> list[str]:
    configured = project.get("privacy_detector_command", project.get("people_detector"))
    if configured:
        command = list(configured) if isinstance(configured, list) else shlex.split(str(configured))
    elif platform.system() == "Darwin":
        command = [str(ROOT / "scripts/vision-people.swift"), "--list", "{inputs}", "--output", "{output}"]
    else:
        raise SystemExit(
            "no qualified privacy detector is configured on this platform; "
            "set privacy_detector_command to a detector that implements the TSV contract"
        )
    if not any("{inputs}" in item for item in command) or not any(
        "{output}" in item for item in command
    ):
        raise SystemExit("privacy_detector_command must contain {inputs} and {output} placeholders")
    executable = Path(command[0])
    local = executable if executable.is_absolute() else Path(project.get("_project_dir", ROOT)) / executable
    if local.exists():
        command[0] = str(local.resolve())
    elif not shutil.which(command[0]):
        raise SystemExit(f"privacy detector is unavailable: {command[0]}")
    qualification = detector_qualification(project, command)
    if not qualification["qualified"]:
        raise SystemExit(f"privacy detector is not qualified: {qualification['reason']}")
    return command


def resolve_project_path(project: dict, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else Path(project.get("_project_dir", ROOT)) / path


def project_path(project: dict, key: str) -> Path:
    return resolve_project_path(project, project[key])


def optional_project_path(project: dict, key: str) -> Path | None:
    value = project.get(key)
    return resolve_project_path(project, value) if value else None


def ensure_slides_text(project: dict) -> Path:
    slides_text = project_path(project, "slides_text")
    if slides_text.exists():
        return slides_text
    slides_pdf = optional_project_path(project, "slides_pdf")
    if not slides_pdf or not slides_pdf.exists():
        raise SystemExit("slides_text is missing; extract it from the supplied screen recording")
    slides_text.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["pdftotext", "-layout", str(slides_pdf), str(slides_text)], check=True)
    return slides_text


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def atomic_write_json(path: Path, value: object) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def canonical_sha256(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def content_fingerprint(path: Path, cache: Path | None = None) -> dict:
    stat = path.stat()
    cached = None
    if cache and cache.exists():
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    key = {"path": str(path.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    digest = file_sha256(path)
    if cache and (not cached or cached.get("sha256") != digest or any(cached.get(k) != v for k, v in key.items())):
        atomic_write_json(cache, {**key, "sha256": digest})
    return {"size": stat.st_size, "sha256": digest}


def ffconcat_quote(path: Path) -> str:
    return "'" + str(path).replace("'", "'\\''") + "'"


def source_to_output(
    source_time: float,
    presentation_start: float,
    edits: list[dict],
    faq_entries: list[dict] = (),
) -> float:
    if source_time < presentation_start:
        raise ValueError("source time precedes the presentation")
    removed = sum(
        max(
            0.0,
            min(source_time, float(edit["source_end"]))
            - max(presentation_start, float(edit["source_start"])),
        )
        for edit in edits
        if float(edit["source_start"]) < source_time
    )
    inserted = sum(
        float(entry.get("duration", 4.0))
        for entry in faq_entries
        if presentation_start <= float(entry["source_start"]) <= source_time
    )
    return max(0.0, source_time - presentation_start - removed + inserted)


def presentation_bounds(project: dict, source_duration: float) -> tuple[float, float]:
    start = float(project.get("presentation_start", 0))
    configured_end = project.get("presentation_end")
    end = source_duration if configured_end is None else float(configured_end)
    if not 0 <= start < end <= source_duration + 1 / 30:
        raise SystemExit(
            f"invalid presentation range {start:.3f}-{end:.3f} for {source_duration:.3f}s source"
        )
    return start, min(end, source_duration)


def source_range_output_duration(
    start: float,
    duration: float,
    edits: list[dict],
    faq_entries: list[dict],
) -> float:
    end = start + duration
    removed = sum(
        max(0.0, min(end, float(edit["source_end"])) - max(start, float(edit["source_start"])))
        for edit in edits
    )
    inserted = sum(
        float(entry.get("duration", 4.0))
        for entry in faq_entries
        if start <= float(entry["source_start"]) < end
    )
    return duration - removed + inserted


def timeline_events_in_range(
    start: float, duration: float, edits: list[dict], faq_entries: list[dict]
) -> list[str]:
    end = start + duration
    events = [
        f"cut {float(edit['source_start']):.3f}-{float(edit['source_end']):.3f}"
        for edit in edits
        if float(edit["source_start"]) < end and float(edit["source_end"]) > start
    ]
    events.extend(
        f"FAQ insertion {float(entry['source_start']):.3f}"
        for entry in faq_entries
        if start < float(entry["source_start"]) < end
    )
    return events


def build_time_map(duration: float, edits: list[dict]) -> dict:
    kept = []
    source_cursor = output_cursor = 0.0
    for edit in edits:
        start = float(edit["source_start"])
        end = float(edit["source_end"])
        if not 0 <= source_cursor <= start < end <= duration:
            raise SystemExit(f"invalid automatic edit: {edit}")
        if start > source_cursor:
            length = start - source_cursor
            kept.append(
                {
                    "source_start": round(source_cursor, 6),
                    "source_end": round(start, 6),
                    "output_start": round(output_cursor, 6),
                    "output_end": round(output_cursor + length, 6),
                }
            )
            output_cursor += length
        source_cursor = end
    if source_cursor < duration:
        kept.append(
            {
                "source_start": round(source_cursor, 6),
                "source_end": round(duration, 6),
                "output_start": round(output_cursor, 6),
                "output_end": round(output_cursor + duration - source_cursor, 6),
            }
        )
        output_cursor += duration - source_cursor
    return {
        "version": 1,
        "source_duration": round(duration, 6),
        "output_duration": round(output_cursor, 6),
        "removed_duration": round(duration - output_cursor, 6),
        "kept_segments": kept,
        "cuts": edits,
    }


def whisper_tokens(data: dict):
    for segment in data.get("transcription", []):
        for token in segment.get("tokens", []):
            text = token.get("text", "")
            if not text or text.startswith("["):
                continue
            offsets = token.get("offsets", {})
            yield (
                text,
                float(offsets.get("from", 0)) / 1000,
                float(offsets.get("to", 0)) / 1000,
                float(token.get("p", 0)),
            )


def read_prompt_source(path: Path, maximum_bytes: int = 2_000_000) -> str:
    data = path.read_bytes()
    if len(data) > maximum_bytes:
        raise SystemExit(f"model input is too large ({len(data)} bytes): {path}")
    return data.decode("utf-8")


def configured_analyzer(project: dict, stage: str, override: str | None = None) -> str:
    provider = override or project.get(f"{stage}_analyzer") or project.get("analyzer")
    if not provider:
        raise SystemExit(
            "no analyzer selected; set ANALYZER=<agent> for this run or "
            f"configure analyzer/{stage}_analyzer in the project"
        )
    return str(provider)


def event_context(project: dict) -> dict[str, str]:
    def text(key: str) -> str:
        value = project.get(key)
        return "" if value is None else str(value).strip()

    return {
        "announcement_url": text("event_url"),
        "background": text("event_context"),
    }


@functools.lru_cache(maxsize=1)
def require_claude_safe_mode() -> None:
    try:
        result = subprocess.run(
            ["claude", "--help"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise SystemExit(f"Claude CLI capability check failed: {error}") from error
    if "--safe-mode" not in f"{result.stdout}\n{result.stderr}":
        raise SystemExit("Claude CLI does not support the required --safe-mode isolation flag")


def run_structured_model(provider: str, schema: dict, prompt: str, timeout: float = 300) -> dict:
    with tempfile.TemporaryDirectory(prefix="meetup-analysis-") as directory:
        workspace = Path(directory)
        if provider == "claude":
            require_claude_safe_mode()
            command = [
                "claude", "-p", "--safe-mode", "--setting-sources", "user",
                "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--tools", "",
                "--permission-mode", "dontAsk", "--no-session-persistence",
                "--output-format", "json", "--json-schema", json.dumps(schema),
            ]
        elif provider == "codex":
            schema_path = workspace / "schema.json"
            atomic_write_json(schema_path, schema)
            command = [
                "codex", "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral",
                "--skip-git-repo-check", "--sandbox", "read-only", "-C", str(workspace),
                "-c", "project_doc_max_bytes=0", "-c", "features.shell_snapshot=false",
                "-c", "features.hooks=false", "-c", "features.plugins=false",
                "-c", "skills.include_instructions=false", "-c", "skills.config=[]",
                "-c", 'default_permissions="meetup_analysis"',
                "-c", 'permissions.meetup_analysis.filesystem={":minimal"="read",":workspace_roots"="read"}',
                "--output-schema", str(schema_path), "-",
            ]
        else:
            raise SystemExit(f"unsupported analyzer: {provider}")
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            input=prompt,
            text=True,
            timeout=timeout,
        )
    payload = json.loads(result.stdout)
    if provider == "claude":
        structured = payload.get("structured_output")
        if structured is None:
            raw = payload.get("result", "")
            structured = json.loads(raw) if isinstance(raw, str) else raw
        payload = structured
    if not isinstance(payload, dict):
        raise SystemExit(f"{provider} returned no structured analysis")
    return payload


def stabilize_camera_positions(values: list[float], deadband: float = 80.0) -> list[float]:
    if not values:
        return []
    targets = values[:]
    for index in range(1, len(values) - 1):
        neighbors = (values[index - 1] + values[index + 1]) / 2
        if abs(values[index] - neighbors) > 180 and abs(values[index - 1] - values[index + 1]) < 120:
            targets[index] = neighbors
    camera = targets[0]
    held = [camera]
    for target in targets[1:]:
        if target > camera + deadband:
            camera = target - deadband
        elif target < camera - deadband:
            camera = target + deadband
        held.append(camera)
    return held


def monotone_slopes(times: list[float], values: list[float]) -> list[float]:
    if len(values) < 2:
        return [0.0] * len(values)
    secants = [
        (right - left) / (times[index + 1] - times[index])
        for index, (left, right) in enumerate(zip(values, values[1:], strict=False))
    ]
    slopes = [secants[0]]
    for left, right in zip(secants, secants[1:], strict=False):
        slopes.append(0.0 if left * right <= 0 else 2 * left * right / (left + right))
    return [*slopes, secants[-1]]


def speaker_position(track: list[dict], timestamp: float) -> tuple[float, float]:
    positions = stabilize_camera_positions([float(item["x"]) for item in track])
    times = [float(item["time"]) for item in track]
    slopes = monotone_slopes(times, positions)
    index = bisect.bisect_right(times, timestamp)
    if index == 0:
        return positions[0], 0.0
    if index == len(track):
        return positions[-1], 0.0
    segment = times[index] - times[index - 1]
    ratio = (timestamp - times[index - 1]) / segment
    left_x, right_x = positions[index - 1], positions[index]
    left_slope, right_slope = slopes[index - 1], slopes[index]
    position = (
        (2 * ratio**3 - 3 * ratio**2 + 1) * left_x
        + (ratio**3 - 2 * ratio**2 + ratio) * segment * left_slope
        + (-2 * ratio**3 + 3 * ratio**2) * right_x
        + (ratio**3 - ratio**2) * segment * right_slope
    )
    velocity = (
        (6 * ratio**2 - 6 * ratio) * left_x / segment
        + (3 * ratio**2 - 4 * ratio + 1) * left_slope
        + (-6 * ratio**2 + 6 * ratio) * right_x / segment
        + (3 * ratio**2 - 2 * ratio) * right_slope
    )
    return position, velocity
