"""Archive the current Canon and prepare a clean chapter-1 rewrite.

The default command is read-only. Destructive reset requires both --execute
and an exact confirmation token. The NovelAgent process must be stopped first.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


CHAPTER_DIRS = (
    "chapters",
    "plans",
    "reviews",
    "summaries",
    "handoffs",
    "runtime/state_snapshots",
)
PROJECT_FILES = ("state.json", "current_state.json")
CONFIRM_TEMPLATE = "REWRITE-0001-{end:04d}"


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _chapter_prefix(path):
    match = re.match(r"^(\d{4})(?=\D|$)", Path(path).name)
    return int(match.group(1)) if match else None


def _owned_artifacts(root, end):
    root = Path(root).resolve()
    rows = []
    for folder in CHAPTER_DIRS:
        directory = root / folder
        if not directory.exists():
            continue
        for path in sorted(directory.iterdir()):
            chapter = _chapter_prefix(path)
            if path.is_file() and chapter is not None and 1 <= chapter <= int(end):
                rows.append(path.resolve())
    return rows


def _canonical_chapters(root):
    directory = Path(root).resolve() / "chapters"
    if not directory.exists():
        return []
    out = []
    for path in directory.glob("[0-9][0-9][0-9][0-9].md"):
        chapter = _chapter_prefix(path)
        if chapter is not None:
            out.append(chapter)
    return sorted(set(out))


def _db_last_chapter(db_path):
    db_path = Path(db_path)
    if not db_path.exists():
        return 0
    with sqlite3.connect(str(db_path), timeout=10) as con:
        row = con.execute(
            "SELECT COALESCE(MAX(chapter_no), 0) FROM chapters "
            "WHERE final IS NOT NULL AND length(final) > 0"
        ).fetchone()
    return int(row[0] or 0)


def inspect_rewrite(root, end=80):
    root = Path(root).resolve()
    end = int(end)
    if end < 1 or end > 9999:
        raise ValueError("end must be between 1 and 9999")
    pending = sorted((root / "runtime" / "canon_transactions").glob("*.json"))
    chapters = _canonical_chapters(root)
    expected = list(range(1, end + 1))
    extra = [chapter for chapter in chapters if chapter > end]
    db_path = root / "novel_memory.sqlite3"
    result = {
        "root": str(root),
        "end": end,
        "confirmation": CONFIRM_TEMPLATE.format(end=end),
        "canonical_chapters": chapters,
        "missing_chapters": [chapter for chapter in expected if chapter not in chapters],
        "chapters_after_end": extra,
        "db_last_chapter": _db_last_chapter(db_path),
        "pending_canon_transactions": [str(path.relative_to(root)).replace("\\", "/") for path in pending],
        "artifact_count": len(_owned_artifacts(root, end)),
    }
    result["ready"] = not (
        result["missing_chapters"]
        or result["chapters_after_end"]
        or result["db_last_chapter"] > end
        or result["pending_canon_transactions"]
        or not db_path.exists()
    )
    return result


def _backup_database(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(source), timeout=30)
    dst = sqlite3.connect(str(destination), timeout=30)
    try:
        src.backup(dst)
        check = dst.execute("PRAGMA integrity_check").fetchone()
        if not check or str(check[0]).lower() != "ok":
            raise RuntimeError(f"archived database integrity check failed: {check}")
    finally:
        dst.close()
        src.close()


def _reset_database(db_path):
    con = sqlite3.connect(str(db_path), timeout=30)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute("DELETE FROM memories WHERE chapter_no >= 1")
        con.execute("DELETE FROM chapters WHERE chapter_no >= 1")
        con.execute("DELETE FROM llm_usage WHERE chapter_no >= 1")
        con.execute(
            "UPDATE memories SET active=CASE "
            "WHEN status IN ('resolved','obsolete','inactive') THEN 0 ELSE 1 END"
        )
        con.commit()
        remaining = con.execute(
            "SELECT COUNT(*) FROM chapters WHERE chapter_no >= 1"
        ).fetchone()[0]
        if remaining:
            raise RuntimeError("database reset verification failed")
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def execute_rewrite_reset(root, end=80, confirmation=""):
    root = Path(root).resolve()
    end = int(end)
    expected_confirmation = CONFIRM_TEMPLATE.format(end=end)
    if confirmation != expected_confirmation:
        raise ValueError(f"confirmation must equal {expected_confirmation}")
    preflight = inspect_rewrite(root, end)
    if not preflight["ready"]:
        raise RuntimeError("rewrite preflight failed: " + json.dumps(preflight, ensure_ascii=False))

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    archive = root / "archive" / f"full_rewrite_0001_{end:04d}_{stamp}"
    archive.mkdir(parents=True, exist_ok=False)
    manifest_rows = []
    for source in _owned_artifacts(root, end):
        relative = source.relative_to(root)
        destination = archive / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        source_hash = _sha256(source)
        archive_hash = _sha256(destination)
        if source_hash != archive_hash:
            raise RuntimeError(f"archive hash mismatch: {relative}")
        manifest_rows.append({
            "source": str(relative).replace("\\", "/"),
            "archive": str(destination.relative_to(root)).replace("\\", "/"),
            "sha256": source_hash,
            "bytes": source.stat().st_size,
        })

    for name in PROJECT_FILES:
        source = root / name
        if not source.exists():
            continue
        destination = archive / name
        shutil.copy2(source, destination)
        if _sha256(source) != _sha256(destination):
            raise RuntimeError(f"archive hash mismatch: {name}")
        manifest_rows.append({
            "source": name,
            "archive": str(destination.relative_to(root)).replace("\\", "/"),
            "sha256": _sha256(source),
            "bytes": source.stat().st_size,
        })

    db_path = root / "novel_memory.sqlite3"
    archived_db = archive / "novel_memory.sqlite3"
    _backup_database(db_path, archived_db)
    manifest_rows.append({
        "source": "novel_memory.sqlite3",
        "archive": str(archived_db.relative_to(root)).replace("\\", "/"),
        "sha256": _sha256(archived_db),
        "bytes": archived_db.stat().st_size,
        "sqlite_backup": True,
    })

    cache_dir = root / "runtime" / "plan_stage_contracts"
    cache_rows = []
    if cache_dir.exists():
        for source in sorted(path for path in cache_dir.iterdir() if path.is_file()):
            destination = archive / "runtime" / "plan_stage_contracts" / source.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            if _sha256(source) != _sha256(destination):
                raise RuntimeError(f"archive hash mismatch: {source.relative_to(root)}")
            cache_rows.append(source)

    manifest = {
        "schema_version": 1,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "range": [1, end],
        "preflight": preflight,
        "archive_complete": True,
        "files": manifest_rows,
        "stage_contract_cache_files": [
            str(path.relative_to(root)).replace("\\", "/") for path in cache_rows
        ],
    }
    manifest_path = archive / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Mutation starts only after every archive copy and the SQLite backup have
    # passed verification.
    for source in _owned_artifacts(root, end):
        source.unlink()
    for source in cache_rows:
        source.unlink()
    _reset_database(db_path)

    state_path = root / "state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    except (ValueError, TypeError):
        state = {}
    state["next_chapter"] = 1
    state["last_canon_chapter"] = 0
    state.pop("last_canon_hash", None)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    current_state = {
        "states": [],
        "hooks": [],
        "facts_events": [],
        "as_of_chapter": 0,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "rewrite_archive": str(archive.relative_to(root)).replace("\\", "/"),
    }
    (root / "current_state.json").write_text(
        json.dumps(current_state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    postflight = inspect_rewrite(root, end)
    postflight["archive"] = str(archive)
    postflight["reset_complete"] = (
        not postflight["canonical_chapters"] and postflight["db_last_chapter"] == 0
    )
    if not postflight["reset_complete"]:
        raise RuntimeError("rewrite reset postflight failed: " + json.dumps(postflight, ensure_ascii=False))
    return postflight


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--end", type=int, default=80)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args(argv)
    result = (
        execute_rewrite_reset(args.root, args.end, args.confirm)
        if args.execute
        else inspect_rewrite(args.root, args.end)
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
