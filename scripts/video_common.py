#!/usr/bin/env python3

import bisect
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def resolve_project_path(project: dict, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else Path(project.get("_project_dir", ROOT)) / path


def project_path(project: dict, key: str) -> Path:
    return resolve_project_path(project, project[key])


def optional_project_path(project: dict, key: str) -> Path | None:
    value = project.get(key)
    return resolve_project_path(project, value) if value else None


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
    digest = cached.get("sha256") if cached and all(cached.get(k) == v for k, v in key.items()) else file_sha256(path)
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


def run_structured_model(provider: str, schema: dict, prompt: str, timeout: float = 300) -> dict:
    with tempfile.TemporaryDirectory(prefix="meetup-analysis-") as directory:
        workspace = Path(directory)
        if provider == "claude":
            command = [
                "claude", "-p", "--safe-mode", "--setting-sources", "user",
                "--strict-mcp-config", "--mcp-config", "{}", "--tools", "",
                "--permission-mode", "dontAsk", "--no-session-persistence",
                "--output-format", "json", "--json-schema", json.dumps(schema), prompt,
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
                "--output-schema", str(schema_path), prompt,
            ]
        else:
            raise SystemExit(f"unsupported analyzer: {provider}")
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)
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
