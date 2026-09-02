"""External Canon range discovery, package validation, and manifest checks.

The module has no provider or database dependency.  It treats externally
written chapters as ordinary Canon files while keeping generation ownership
and range-completion gates explicit and testable offline.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

from continuity import extract_source_tail


EXTERNAL_CANON_SCHEMA_VERSION = 1


class ExternalCanonError(ValueError):
    pass


def canonical_chapter_text(text: str) -> str:
    return str(text or "").lstrip("\ufeff").strip() + "\n"


def sha256_text(text: str) -> str:
    return hashlib.sha256(canonical_chapter_text(text).encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _positive_int(value, field: str) -> int:
    try:
        number = int(value)
    except Exception as exc:
        raise ExternalCanonError(f"{field} must be an integer") from exc
    if number < 1:
        raise ExternalCanonError(f"{field} must be positive")
    return number


def _range_record(start, end, label="", source="configured") -> dict:
    start = _positive_int(start, "external Canon start")
    end = _positive_int(end, "external Canon end")
    if end < start:
        raise ExternalCanonError(f"external Canon range is reversed: {start}-{end}")
    return {
        "start": start,
        "end": end,
        "label": str(label or f"External Canon {start}-{end}").strip()[:200],
        "source": str(source or "configured"),
    }


_OUTLINE_MARKERS = (
    re.compile(
        r"NOVELAGENT(?:\s*[:_-]\s*|_)(?:EXTERNAL[_ -]?CANON)\b[^\r\n>]*?"
        r"start\s*=\s*(\d+)[^\r\n>]*?end\s*=\s*(\d+)"
        r"(?:[^\r\n>]*?label\s*=\s*[\"']([^\"']+)[\"'])?",
        re.I,
    ),
    re.compile(
        r"(?:外部正史|EXTERNAL\s+CANON)(?:范围|章节|卷)?\s*[:：]?\s*"
        r"(?:第\s*)?(\d+)\s*[—–~-]\s*(\d+)\s*章?"
        r"(?:\s*[:：|]\s*([^\r\n<]+))?",
        re.I,
    ),
)


def outline_external_ranges(outline_text: str) -> list[dict]:
    """Read explicit generic markers without guessing from ordinary plot prose."""
    text = str(outline_text or "")
    found = []
    for pattern in _OUTLINE_MARKERS:
        for match in pattern.finditer(text):
            label = (match.group(3) or "").strip(" #*-\t")
            found.append(_range_record(match.group(1), match.group(2), label, "outline"))
    return found


def external_canon_ranges(config: dict | None, outline_text: str = "") -> list[dict]:
    cfg = (config or {}).get("external_canon", {}) or {}
    if cfg.get("enabled", True) is False:
        return []
    rows = []
    for raw in cfg.get("ranges", []) or []:
        if not isinstance(raw, dict):
            raise ExternalCanonError("external_canon.ranges entries must be objects")
        rows.append(_range_record(raw.get("start"), raw.get("end"), raw.get("label"), "configured"))
    if cfg.get("read_outline_markers", True):
        rows.extend(outline_external_ranges(outline_text))

    merged = {}
    for row in rows:
        key = (row["start"], row["end"])
        if key in merged:
            old = merged[key]
            if old["source"] != row["source"]:
                old["source"] = "configured+outline"
            if row.get("label") and row["label"] != f"External Canon {row['start']}-{row['end']}":
                old["label"] = row["label"]
        else:
            merged[key] = dict(row)
    ordered = sorted(merged.values(), key=lambda item: (item["start"], item["end"]))
    for previous, current in zip(ordered, ordered[1:]):
        if current["start"] <= previous["end"]:
            raise ExternalCanonError(
                f"external Canon ranges overlap: {previous['start']}-{previous['end']} and "
                f"{current['start']}-{current['end']}"
            )
    return ordered


def range_key(spec: dict) -> str:
    return f"{int(spec['start']):04d}-{int(spec['end']):04d}"


def find_range(ranges: list[dict], chapter_no: int) -> dict | None:
    chapter_no = int(chapter_no)
    return next((row for row in ranges if row["start"] <= chapter_no <= row["end"]), None)


def range_digest(chapter_hashes: dict) -> str:
    rows = [f"{int(number):04d}:{chapter_hashes[number]}" for number in sorted(chapter_hashes, key=int)]
    return hashlib.sha256("\n".join(rows).encode("ascii")).hexdigest()


def _safe_zip_name(raw_name: str) -> tuple[str, str]:
    name = str(raw_name or "").replace("\\", "/")
    path = PurePosixPath(name)
    if not name or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise ExternalCanonError(f"unsafe ZIP path: {raw_name!r}")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ExternalCanonError(f"unsafe ZIP path: {raw_name!r}")
    parts = list(path.parts)
    if len(parts) == 2 and parts[0] == "chapters":
        parts = parts[1:]
    elif len(parts) == 2 and parts[0] == "metadata":
        return name, parts[-1]
    if len(parts) != 1:
        raise ExternalCanonError(f"chapter files must be at ZIP root or chapters/: {raw_name!r}")
    return name, parts[0]


_NUMBERED_TITLE = re.compile(r"(?m)^\s{0,3}#{1,6}\s*第\s*(\d+)\s*章(?:\s|[:：]|$)")


def validate_chapter_package(payload: bytes, spec: dict, *, max_zip_bytes=128 * 1024 * 1024,
                             max_chapter_bytes=2 * 1024 * 1024,
                             max_total_bytes=256 * 1024 * 1024) -> dict:
    """Validate a complete ZIP before any chapter or staging file is written."""
    if not isinstance(payload, (bytes, bytearray)) or not payload:
        raise ExternalCanonError("ZIP package is empty")
    if len(payload) > int(max_zip_bytes):
        raise ExternalCanonError("ZIP package exceeds configured size limit")
    start, end = int(spec["start"]), int(spec["end"])
    expected = set(range(start, end + 1))
    texts = {}
    metadata = {}
    names = {}
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(payload), "r") as archive:
            infos = archive.infolist()
            if not infos:
                raise ExternalCanonError("ZIP package has no files")
            for info in infos:
                if info.is_dir():
                    continue
                unix_mode = (int(info.external_attr) >> 16) & 0xFFFF
                if unix_mode and stat.S_ISLNK(unix_mode):
                    raise ExternalCanonError(f"symbolic links are not allowed: {info.filename}")
                normalized, filename = _safe_zip_name(info.filename)
                match = re.fullmatch(r"(\d{4})\.md", filename, re.I)
                metadata_match = re.fullmatch(r"metadata/(\d{4})\.json", normalized, re.I)
                exit_match = re.fullmatch(r"metadata/(?:exit_state|canon_exit_state)\.json", normalized, re.I)
                if not match and not metadata_match and not exit_match:
                    raise ExternalCanonError(
                        f"invalid ZIP entry {info.filename!r}; expected NNNN.md or metadata/NNNN.json"
                    )
                if metadata_match or exit_match:
                    if info.file_size > 512 * 1024:
                        raise ExternalCanonError(f"metadata entry exceeds size limit: {info.filename}")
                    total += int(info.file_size)
                    if total > int(max_total_bytes):
                        raise ExternalCanonError("uncompressed ZIP content exceeds configured size limit")
                    try:
                        data = json.loads(archive.read(info).decode("utf-8-sig"))
                    except Exception as exc:
                        raise ExternalCanonError(f"invalid metadata JSON: {info.filename}") from exc
                    if not isinstance(data, dict):
                        raise ExternalCanonError(f"metadata must be an object: {info.filename}")
                    if metadata_match:
                        chapter = int(metadata_match.group(1))
                        if chapter not in expected:
                            raise ExternalCanonError(f"metadata chapter {chapter} is outside external Canon range")
                        if str(chapter) in metadata:
                            raise ExternalCanonError(f"duplicate metadata for chapter {chapter}")
                        metadata[str(chapter)] = data
                    else:
                        if "exit_state" in metadata:
                            raise ExternalCanonError("duplicate metadata/exit_state.json")
                        metadata["exit_state"] = data
                    continue
                chapter = int(match.group(1))
                if chapter not in expected:
                    raise ExternalCanonError(
                        f"chapter {chapter} is outside external Canon range {start}-{end}"
                    )
                if chapter in texts:
                    raise ExternalCanonError(
                        f"duplicate chapter {chapter}: {names[chapter]!r} and {info.filename!r}"
                    )
                if info.flag_bits & 0x1:
                    raise ExternalCanonError(f"encrypted ZIP entries are not supported: {info.filename}")
                if info.file_size > int(max_chapter_bytes):
                    raise ExternalCanonError(f"chapter {chapter} exceeds configured size limit")
                total += int(info.file_size)
                if total > int(max_total_bytes):
                    raise ExternalCanonError("uncompressed ZIP content exceeds configured size limit")
                raw = archive.read(info)
                try:
                    text = raw.decode("utf-8-sig")
                except UnicodeDecodeError as exc:
                    raise ExternalCanonError(f"chapter {chapter} is not UTF-8") from exc
                if "\x00" in text or not text.strip():
                    raise ExternalCanonError(f"chapter {chapter} is empty or contains NUL bytes")
                heading = _NUMBERED_TITLE.search(text[:3000])
                if heading and int(heading.group(1)) != chapter:
                    raise ExternalCanonError(
                        f"chapter title number {heading.group(1)} does not match filename {filename}"
                    )
                texts[chapter] = canonical_chapter_text(text)
                names[chapter] = info.filename
    except zipfile.BadZipFile as exc:
        raise ExternalCanonError("uploaded file is not a valid ZIP package") from exc

    missing = sorted(expected.difference(texts))
    if missing:
        preview = ", ".join(str(x) for x in missing[:12])
        suffix = " ..." if len(missing) > 12 else ""
        raise ExternalCanonError(f"external Canon package is incomplete; missing chapters: {preview}{suffix}")
    hashes = {chapter: sha256_text(text) for chapter, text in texts.items()}
    if "exit_state" in metadata and str(end) in metadata:
        raise ExternalCanonError(f"chapter {end} has both metadata/{end:04d}.json and exit_state metadata")
    for key, data in metadata.items():
        chapter = end if key == "exit_state" else int(key)
        try:
            declared_chapter = int(data.get("chapter_no"))
        except Exception as exc:
            raise ExternalCanonError(f"metadata for chapter {chapter} has no valid chapter_no") from exc
        if declared_chapter != chapter:
            raise ExternalCanonError(
                f"metadata chapter_no {declared_chapter} does not match expected chapter {chapter}"
            )
        if str(data.get("content_sha256") or data.get("canon_sha256") or "") != hashes[chapter]:
            raise ExternalCanonError(f"metadata for chapter {chapter} is not bound to its正文 hash")
        if key != "exit_state":
            if not isinstance(data.get("summary"), str) or not data.get("summary", "").strip():
                raise ExternalCanonError(f"metadata for chapter {chapter} has no Summary")
            if not isinstance(data.get("memories"), list):
                raise ExternalCanonError(f"metadata for chapter {chapter} memories must be a list")
            if not isinstance(data.get("handoff"), dict):
                raise ExternalCanonError(f"metadata for chapter {chapter} has no Handoff object")
    return {
        "schema_version": EXTERNAL_CANON_SCHEMA_VERSION,
        "range": dict(spec),
        "texts": texts,
        "metadata": metadata,
        "chapter_hashes": hashes,
        "range_digest": range_digest(hashes),
        "package_sha256": hashlib.sha256(bytes(payload)).hexdigest(),
        "compressed_bytes": len(payload),
        "uncompressed_bytes": total,
    }


def atomic_write_json(path: Path, value: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".pending")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    os.replace(temp, path)


def new_manifest(spec: dict, package: dict, completed_entries=None) -> dict:
    now = datetime.now().isoformat(timespec="seconds")
    return {
        "schema_version": EXTERNAL_CANON_SCHEMA_VERSION,
        "kind": "external_canon_import",
        "source": "external_canon",
        "range": {key: spec[key] for key in ("start", "end", "label", "source") if key in spec},
        "status": "importing",
        "package_sha256": package["package_sha256"],
        "expected_range_digest": package["range_digest"],
        "expected_chapter_hashes": {str(k): v for k, v in package["chapter_hashes"].items()},
        "entries": dict(completed_entries or {}),
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
        "boundary_validation": None,
        "exit_state": None,
        "last_error": "",
    }


def load_manifest(path: Path) -> dict | None:
    path = Path(path)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ExternalCanonError(f"invalid external Canon manifest: {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise ExternalCanonError(f"invalid external Canon manifest: {path.name}")
    return value


def verify_manifest_files(root: Path, spec: dict, manifest: dict | None,
                          source_tail_chars=2600) -> list[str]:
    """Deep file/Handoff verification used before generation can cross a range."""
    root = Path(root)
    errors = []
    if not isinstance(manifest, dict):
        return ["missing import manifest"]
    if manifest.get("status") != "complete":
        errors.append(f"manifest status is {manifest.get('status') or 'unknown'}, not complete")
    recorded = manifest.get("range") or {}
    if int(recorded.get("start", 0) or 0) != int(spec["start"]) or int(recorded.get("end", 0) or 0) != int(spec["end"]):
        errors.append("manifest range does not match configured external Canon range")
    expected_hashes = manifest.get("expected_chapter_hashes") or {}
    entries = manifest.get("entries") or {}
    actual_hashes = {}
    for chapter in range(int(spec["start"]), int(spec["end"]) + 1):
        key = str(chapter)
        expected = str(expected_hashes.get(key) or "")
        entry = entries.get(key) if isinstance(entries, dict) else None
        chapter_path = root / "chapters" / f"{chapter:04d}.md"
        summary_path = root / "summaries" / f"{chapter:04d}.md"
        handoff_path = root / "handoffs" / f"{chapter:04d}.json"
        if not chapter_path.exists():
            errors.append(f"missing Canon chapter {chapter}")
            continue
        actual = sha256_file(chapter_path)
        actual_hashes[chapter] = actual
        if not expected or actual != expected:
            errors.append(f"chapter {chapter} hash does not match import manifest")
        if not isinstance(entry, dict) or str(entry.get("canon_sha256") or "") != actual:
            errors.append(f"chapter {chapter} completion entry is missing or stale")
        if not summary_path.exists() or not summary_path.read_text(encoding="utf-8").strip():
            errors.append(f"missing Summary for chapter {chapter}")
        try:
            handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
            if int(handoff.get("chapter_no", 0) or 0) != chapter or handoff.get("status") != "complete":
                errors.append(f"chapter {chapter} Handoff is not complete")
            canon = chapter_path.read_text(encoding="utf-8")
            expected_tail = extract_source_tail(canon, source_tail_chars)
            if str(handoff.get("source_tail") or "") != expected_tail:
                errors.append(f"chapter {chapter} Handoff source tail is stale")
        except Exception:
            errors.append(f"missing or invalid Handoff for chapter {chapter}")
        if len(errors) >= 40:
            errors.append("additional manifest errors omitted")
            break
    if len(actual_hashes) == int(spec["end"]) - int(spec["start"]) + 1:
        digest = range_digest(actual_hashes)
        if digest != str(manifest.get("expected_range_digest") or ""):
            errors.append("external Canon range digest does not match manifest")
    exit_meta = manifest.get("exit_state") or {}
    exit_path = root / "runtime" / "external_canon" / range_key(spec) / "exit_state.json"
    if not exit_path.exists():
        errors.append("missing verified Canon exit state")
    else:
        exit_sha = sha256_file(exit_path)
        if exit_sha != str(exit_meta.get("sha256") or ""):
            errors.append("Canon exit state hash does not match manifest")
        else:
            try:
                exit_state = json.loads(exit_path.read_text(encoding="utf-8"))
                end = int(spec["end"])
                if int(exit_state.get("chapter_no", 0) or 0) != end:
                    errors.append("Canon exit state chapter number is invalid")
                if str(exit_state.get("canon_sha256") or "") != actual_hashes.get(end, ""):
                    errors.append("Canon exit state is not bound to the final chapter body")
                final_handoff = root / "handoffs" / f"{end:04d}.json"
                if not final_handoff.exists() or str(exit_state.get("handoff_sha256") or "") != sha256_file(final_handoff):
                    errors.append("Canon exit state is not bound to the final Handoff")
            except Exception:
                errors.append("Canon exit state JSON is invalid")
    return errors
