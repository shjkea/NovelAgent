# -*- coding: utf-8 -*-
"""
NovelAgent V5.0 - story Markdown manager core.

Accepted route line (recommended):
NOVELAGENT_MD file=characters_seed.md; target=角色名; section=家庭背景; action=replace

Legacy HTML-comment route is also accepted:
<!-- NOVELAGENT_MD file=...; ... -->

The route line is never written to the canonical .md file.
"""
from __future__ import annotations

import difflib
import hashlib
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


ALLOWED_FILES = {
    "characters_seed.md",
    "outline.md",
    "world.md",
    "style.md",
    "premise.md",
    "author_notes.md",
}
ACTIONS = {"add", "append", "replace", "insert_after"}
CHAR_CATEGORIES = {"主角", "主要角色", "NPC", "反派角色"}

HTML_TAG_RE = re.compile(
    r"^\s*<!--\s*NOVELAGENT_MD\s+(.*?)\s*-->\s*(?:\r?\n)?",
    re.I | re.S,
)
VISIBLE_TAG_RE = re.compile(
    r"^\s*NOVELAGENT_MD\s+([^\r\n]*?)\s*(?:\r?\n|$)",
    re.I,
)
ANY_ROUTE_RE = re.compile(
    r"(?im)^\s*(?:<!--\s*)?NOVELAGENT_MD\b",
)


class MdManagerError(RuntimeError):
    pass


@dataclass
class RouteInfo:
    file: str
    target: str = ""
    section: str = ""
    action: str = "add"
    content: str = ""


def _meta(body: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in re.split(r"[;；]\s*", str(body or "").strip()):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip().lower()] = v.strip()

    # Legacy v1.x compatibility.
    if "action" not in out and "mode" in out:
        mode = out["mode"].strip().lower()
        if mode in ACTIONS:
            out["action"] = mode
        elif mode in {"chapter", "auto"}:
            out["action"] = "add"
    return out


def parse_route(text: str) -> RouteInfo:
    raw = str(text or "")
    m = HTML_TAG_RE.match(raw)
    if m:
        meta = _meta(m.group(1))
        content = raw[m.end():].lstrip()
    else:
        m = VISIBLE_TAG_RE.match(raw)
        if not m:
            raise MdManagerError(
                "没有检测到 NOVELAGENT_MD 路由首行。请复制包含第一行路由的完整文本。"
            )
        meta = _meta(m.group(1))
        content = raw[m.end():].lstrip()

    if ANY_ROUTE_RE.search(content):
        raise MdManagerError(
            "检测到第二个 NOVELAGENT_MD 路由标签。每次只能粘贴并提交一个代码块。"
        )

    filename = meta.get("file", "").strip()
    target = meta.get("target", "").strip()
    section = meta.get("section", "").strip()
    action = meta.get("action", "add").strip().lower()

    if not filename:
        raise MdManagerError("路由缺少 file。")
    if filename not in ALLOWED_FILES:
        raise MdManagerError(f"不允许修改文件：{filename}")
    if action not in ACTIONS:
        raise MdManagerError(f"不支持的 action：{action}")
    if action != "add" and not target:
        raise MdManagerError(f"{action} 操作必须指定 target。")
    if filename == "characters_seed.md" and action == "add" and section not in CHAR_CATEGORIES:
        raise MdManagerError(
            "新增人物时 section 必须是：主角 / 主要角色 / NPC / 反派角色。"
        )
    if not content.strip():
        raise MdManagerError("路由首行之后没有可写入内容。")

    return RouteInfo(
        file=filename,
        target=target,
        section=section,
        action=action,
        content=content.strip(),
    )


def route_dict(route: RouteInfo) -> dict:
    return {
        "file": route.file,
        "target": route.target,
        "section": route.section,
        "action": route.action,
        "content": route.content,
    }


def safe_story_path(story_dir: Path, filename: str) -> Path:
    story = Path(story_dir).resolve()
    path = (story / filename).resolve()
    if path.parent != story or path.name not in ALLOWED_FILES:
        raise MdManagerError("非法目标路径。")
    return path


def read_utf8(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def text_hash(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def normalize_title(value: str) -> str:
    s = re.sub(r"^#{1,6}\s+", "", str(value or "").strip())
    s = s.replace("：", ":")
    return re.sub(r"\s+", "", s).casefold()


def all_headings(text: str):
    rows = []
    pos = 0
    for line in text.splitlines(True):
        m = re.match(r"^(#{1,6})\s+(.+?)\s*(?:\r?\n)?$", line)
        if m:
            rows.append((pos, len(m.group(1)), m.group(2).strip()))
        pos += len(line)
    return rows


def first_heading(text: str):
    rows = all_headings(text)
    if not rows:
        return None, None
    _, level, title = rows[0]
    return level, title


def first_h2_name(text: str):
    for _, level, title in all_headings(text):
        if level == 2 and not re.search(r"第\s*\d+", title):
            return title
    return None


CHAPTER_RANGE_RE = re.compile(
    r"第?\s*(\d{1,6})\s*(?:[—–\-~～至到]\s*第?\s*(\d{1,6}))?\s*章"
)
CHAPTER_HEADING_RE = re.compile(
    r"^\s*第?\s*(\d{1,6})\s*(?:[—–\-~～至到]\s*第?\s*(\d{1,6}))?\s*章"
)


def chapter_range(value: str):
    m = CHAPTER_RANGE_RE.search(str(value or ""))
    if not m:
        return None
    a = int(m.group(1))
    b = int(m.group(2) or a)
    if b < a:
        a, b = b, a
    return a, b


def heading_chapter_range(value: str):
    """Parse a chapter token only when it starts the heading text."""
    m = CHAPTER_HEADING_RE.match(str(value or ""))
    if not m:
        return None
    a = int(m.group(1))
    b = int(m.group(2) or a)
    if b < a:
        a, b = b, a
    return a, b


def find_heading(text: str, target: str, preferred_level=None):
    wanted = normalize_title(target)

    # Exact title first.
    for pos, level, title in all_headings(text):
        if preferred_level and level != preferred_level:
            continue
        if normalize_title(title) == wanted:
            return pos, level, title

    # Chapter/range token: target "第357章" can match "第357章：标题";
    # target "第351—375章" can match a range heading with a suffix.
    tr = heading_chapter_range(target)
    if tr:
        matches = []
        for pos, level, title in all_headings(text):
            if preferred_level and level != preferred_level:
                continue
            hr = heading_chapter_range(title)
            if hr == tr:
                matches.append((pos, level, title))
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise MdManagerError(f"目标“{target}”匹配到多个章节标题，已阻止自动修改。")

    return None


def block_bounds(text: str, pos: int, level: int):
    line_end = text.find("\n", pos)
    if line_end < 0:
        line_end = len(text)
    after_start = min(len(text), line_end + 1)
    after = text[after_start:]
    m = re.search(rf"(?m)^#{{1,{level}}}\s+.+$", after)
    end = after_start + m.start() if m else len(text)
    return pos, end


def find_block(text: str, target: str, preferred_level=None):
    hit = find_heading(text, target, preferred_level=preferred_level)
    if not hit:
        return None
    pos, level, actual = hit
    a, b = block_bounds(text, pos, level)
    return a, b, level, actual


def find_subsection(text: str, parent, section: str):
    a, b, parent_level, _ = parent
    wanted = normalize_title(section)
    chunk = text[a:b]
    hits = []
    for rel, level, title in all_headings(chunk):
        if level <= parent_level:
            continue
        if normalize_title(title) == wanted:
            abs_pos = a + rel
            sa, sb = block_bounds(text[:b], abs_pos, level)
            hits.append((sa, sb, level, title))
    if len(hits) > 1:
        raise MdManagerError(
            f"目标内部存在多个同名小节“{section}”，已阻止自动修改。"
        )
    return hits[0] if hits else None


def heading_suggestions(text: str, target: str, preferred_level=None, limit=3):
    """Suggest likely headings without ever selecting one automatically."""
    choices = []
    normalized_to_titles = {}
    for _, level, title in all_headings(text):
        if preferred_level and level != preferred_level:
            continue
        key = normalize_title(title)
        if not key:
            continue
        normalized_to_titles.setdefault(key, []).append(title)
        choices.append(key)
    close = difflib.get_close_matches(
        normalize_title(target), list(dict.fromkeys(choices)),
        n=max(1, int(limit or 3)), cutoff=0.28,
    )
    out = []
    for key in close:
        for title in normalized_to_titles.get(key, []):
            if title not in out:
                out.append(title)
            if len(out) >= limit:
                return out
    return out


def leading_heading(fragment: str):
    m = re.match(
        r"^\s*(#{1,6})\s+(.+?)\s*(?:\r?\n|$)",
        str(fragment or ""),
    )
    if not m:
        return None
    return len(m.group(1)), m.group(2).strip(), m.end()


def ensure_heading(fragment: str, title: str, level: int) -> str:
    fragment = str(fragment or "").strip()
    leading = leading_heading(fragment)
    if leading:
        first_level, first_title, first_end = leading
        if normalize_title(first_title) == normalize_title(title):
            body = fragment[first_end:].lstrip()
            return (
                f'{"#" * level} {title.strip()}\n\n{body}'.rstrip()
                if body else f'{"#" * level} {title.strip()}'
            )
        if first_level <= level:
            raise MdManagerError(
                f"正文首标题“{first_title}”与实际目标“{title}”不一致。"
                "修改已有目标时请删除正文中的重复标题，只粘贴标题下的内容。"
            )
    return f'{"#" * level} {title.strip()}\n\n{fragment}'


def validate_append_fragment(fragment: str, actual_title: str, level: int):
    """Prevent an append payload from escaping its target block."""
    leading = leading_heading(fragment)
    if not leading:
        return
    first_level, first_title, _ = leading
    if first_level <= level:
        raise MdManagerError(
            f"追加正文以“{first_title}”标题开头，会越出目标“{actual_title}”的结构范围。"
            "请删除重复/改写后的目标标题；如需新增独立区块，请使用 add 或 insert_after。"
        )


def insert_at(base: str, pos: int, fragment: str) -> str:
    left = base[:pos].rstrip()
    right = base[pos:].lstrip("\n")
    parts = []
    if left:
        parts.append(left)
    parts.append(fragment.strip())
    if right:
        parts.append(right.rstrip())
    return "\n\n".join(parts).rstrip() + "\n"


def replace_range(base: str, start: int, end: int, fragment: str) -> str:
    left = base[:start].rstrip()
    right = base[end:].lstrip("\n")
    parts = []
    if left:
        parts.append(left)
    parts.append(fragment.strip())
    if right:
        parts.append(right.rstrip())
    return "\n\n".join(parts).rstrip() + "\n"


def append_to_block(base: str, block, fragment: str) -> str:
    return insert_at(base, block[1], fragment)


def category_end(text: str, category: str):
    hit = find_heading(text, category, preferred_level=1)
    if not hit:
        return None
    pos, level, _ = hit
    return block_bounds(text, pos, level)[1]


def add_character(base: str, route: RouteInfo) -> str:
    name = first_h2_name(route.content)
    if not name:
        raise MdManagerError("新增人物内容必须包含“## 人物名”二级标题。")
    if find_block(base, name, preferred_level=2):
        raise MdManagerError(
            f"人物“{name}”已经存在；新增已阻止。修改请使用 replace，追加请使用 append。"
        )

    pos = category_end(base, route.section)
    if pos is None:
        # Existing projects normally already have these sections. Creating a
        # missing category is safer than silently writing under a wrong one.
        return base.rstrip() + f"\n\n# {route.section}\n\n{route.content.strip()}\n"
    return insert_at(base, pos, route.content)


def add_outline(base: str, route: RouteInfo) -> str:
    level, title = first_heading(route.content)
    if level and title and find_block(base, title, preferred_level=level):
        raise MdManagerError(
            f"大纲标题“{title}”已经存在；新增已阻止。修改请使用 replace。"
        )

    _, new_title = first_heading(route.content)
    new_range = heading_chapter_range(new_title or "")
    if new_range:
        new_start = new_range[0]
        later = []
        for pos, _, title2 in all_headings(base):
            old_range = heading_chapter_range(title2)
            if old_range and old_range[0] > new_start:
                later.append((old_range[0], pos))
        if later:
            _, pos = min(later, key=lambda x: x[0])
            return insert_at(base, pos, route.content)

    return base.rstrip() + "\n\n" + route.content.strip() + "\n"


def apply_operation(base: str, route: RouteInfo) -> tuple[str, str]:
    """
    Returns (new_text, note).
    note is used by the Web UI to explain non-obvious behavior.
    """
    action = route.action
    filename = route.file
    target = route.target
    section = route.section
    fragment = route.content.strip()
    note = ""

    if action == "add":
        if filename == "characters_seed.md":
            return add_character(base, route), f"将在“# {section}”分类中新增人物。"
        if filename == "outline.md":
            return add_outline(base, route), "将按章节编号尝试放到合适位置；无法排序时追加到文件末尾。"

        level, title = first_heading(fragment)
        if level and title and find_block(base, title, preferred_level=level):
            raise MdManagerError(f"标题“{title}”已经存在；新增已阻止。")
        return base.rstrip() + "\n\n" + fragment + "\n", "将追加为新的文件级区块。"

    preferred = 2 if filename == "characters_seed.md" else None
    parent = find_block(base, target, preferred_level=preferred)
    if not parent:
        suggestions = heading_suggestions(base, target, preferred_level=preferred)
        hint = (
            "；最接近的现有标题：" + " / ".join(f"“{x}”" for x in suggestions)
            if suggestions else ""
        )
        raise MdManagerError(
            f"找不到目标：{target}{hint}。target 必须使用原 MD 中的现有标题，不能使用改写后的新标题。"
        )

    if action in {"replace", "append"} and section:
        sub = find_subsection(base, parent, section)
        if sub:
            if action == "replace":
                a, b, level, actual = sub
                wrapped = ensure_heading(fragment, actual, level)
                return replace_range(base, a, b, wrapped), f"只替换目标内部小节“{actual}”。"
            validate_append_fragment(fragment, sub[3], sub[2])
            return append_to_block(base, sub, fragment), f"追加到目标内部小节“{sub[3]}”末尾。"

        if action == "replace":
            parent_chunk = base[parent[0]:parent[1]]
            suggestions = heading_suggestions(parent_chunk, section)
            hint = (
                "；最接近的现有小节：" + " / ".join(f"“{x}”" for x in suggestions)
                if suggestions else ""
            )
            raise MdManagerError(
                f"目标“{parent[3]}”内找不到小节“{section}”{hint}。"
                "replace 不会新建小节；若确实要新增小节，请使用 action=append。"
            )

        # Only append may deliberately create a missing subsection.  This
        # prevents a misspelled replace target from silently creating a near-
        # duplicate heading.
        level = parent[2] + 1
        wrapped = ensure_heading(fragment, section, level)
        return append_to_block(base, parent, wrapped), f"未找到小节“{section}”，将新建该小节。"

    if action == "replace":
        a, b, level, actual = parent
        wrapped = ensure_heading(fragment, actual, level)
        return replace_range(base, a, b, wrapped), f"替换整个目标块“{actual}”。"

    if action == "append":
        validate_append_fragment(fragment, parent[3], parent[2])
        return append_to_block(base, parent, fragment), f"追加到目标块“{parent[3]}”末尾。"

    if action == "insert_after":
        return insert_at(base, parent[1], fragment), f"插入到目标块“{parent[3]}”之后。"

    raise MdManagerError(f"未实现操作：{action}")


def make_diff(before: str, after: str, filename: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(True),
            after.splitlines(True),
            fromfile=f"{filename}（修改前）",
            tofile=f"{filename}（修改后）",
            n=4,
        )
    )


def preview(story_dir: Path, route: RouteInfo) -> dict:
    path = safe_story_path(story_dir, route.file)
    before = read_utf8(path)
    after, note = apply_operation(before, route)
    if after == before:
        raise MdManagerError("计算后的文件与原文件完全相同，没有可提交的变化。")

    preferred = 2 if route.file == "characters_seed.md" else None
    target_exists = (
        bool(find_block(before, route.target, preferred_level=preferred))
        if route.target else None
    )
    return {
        "file": route.file,
        "target": route.target,
        "section": route.section,
        "action": route.action,
        "target_exists": target_exists,
        "note": note,
        "before_hash": text_hash(before),
        "before_chars": len(before),
        "after_chars": len(after),
        "diff": make_diff(before, after, route.file),
    }


def commit(story_dir: Path, route: RouteInfo, expected_before_hash: str) -> dict:
    story_dir = Path(story_dir)
    path = safe_story_path(story_dir, route.file)
    before = read_utf8(path)
    if text_hash(before) != str(expected_before_hash or ""):
        raise MdManagerError(
            "文件在预览之后已经发生变化，已阻止覆盖。请重新点击“检查修改”后再提交。"
        )

    after, note = apply_operation(before, route)
    if after == before:
        raise MdManagerError("没有检测到实际变化。")

    backup = None
    if path.exists():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        backup_dir = story_dir / "_md_manager_backups" / stamp
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / path.name
        shutil.copy2(path, backup)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(after, encoding="utf-8")

    return {
        "ok": True,
        "file": route.file,
        "note": note,
        "backup": (
            str(backup.relative_to(story_dir.parent)).replace("\\", "/")
            if backup else ""
        ),
        "after_hash": text_hash(after),
    }
