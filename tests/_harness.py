"""Load pure-logic blocks out of agent_core.py without importing runtime deps.

agent_core imports provider_router, sqlite bindings and the whole Canon runtime.
None of that is needed to test deterministic helpers, and importing it would make
these tests depend on provider credentials. Instead the relevant slices of the
class body are extracted by marker and exec'd into a purpose-built stub class.

Markers are plain source strings, so a rename that moves a helper out of the
extracted range fails loudly here instead of silently testing nothing.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "agent_core.py").read_text(encoding="utf-8")

LOCATOR_BLOCK = (
    "    # ---------------- deterministic evidence locator (zero LLM) ----------------",
    "    def _repair_chat_json(",
)

TASK_BLOCK = (
    "    # Task states used by the repair pipeline.",
    "    # Sections whose entire content is \"nothing to fix here\".",
)

NOISE_BLOCK = (
    "    # Sections whose entire content is \"nothing to fix here\".",
    "    @staticmethod\n    def _split_audit_repair_source(",
)

# Legacy prose-report path: resolves weak classes, then groups same-chapter rows
# under one chapter lock.  Starts at the class constant so the resolver that
# reads it is inside the extracted range.
NORMALIZE_BLOCK = (
    "    # Legacy extraction classes.",
    "    @staticmethod\n    def _audit_repair_review_batches(",
)

# Exact-patch applier, including per-unit attribution and scaled budgets.
PATCH_BLOCK = (
    "    @staticmethod\n    def _repair_apply_exact_patches(",
    "    def _repair_review_local_candidate(",
)

# Commit-time archiving of everything a chapter owns, plus the restore side.
# Starts at the section comment so REPAIR_SIDECAR_DIRS is in range.
ARCHIVE_BLOCK = (
    "    # ---------------- commit archive scope ----------------",
    "    # ---------------- post-commit summary rebuild ----------------",
)

# Deciding whose summary went stale once repaired prose is committed. Starts at
# the section comment so REPAIR_RESUMMARIZE_CLASSES is inside the range.
RESUMMARIZE_BLOCK = (
    "    # ---------------- post-commit summary rebuild ----------------",
    "    # ---------------- rollback safety net (pure decision layer) ----------------",
)

# Per-chapter rollback decisions. Starts at the section comment so the ROLLBACK_*
# class constants the classifier returns are inside the extracted range.
ROLLBACK_BLOCK = (
    "    # ---------------- rollback safety net (pure decision layer) ----------------",
    "    def rollback_audit_repair(",
)


def extract(block):
    """Return the source text between two markers, exclusive of the end marker."""
    start_marker, end_marker = block
    try:
        start = SOURCE.index(start_marker)
        end = SOURCE.index(end_marker, start)
    except ValueError as e:  # pragma: no cover - guards against silent drift
        raise AssertionError(
            f"agent_core.py no longer contains the expected marker: {e}"
        ) from e
    return SOURCE[start:end]


def build_class(name, blocks, header_lines=(), extra_globals=None):
    """Compose the extracted blocks into a standalone class and return it.

    `header_lines` are inserted as class-body source before the extracted blocks,
    which is how the stub gets its __init__ and any class constants the real
    NovelAgent supplies from elsewhere in the file.
    """
    body = []
    body.extend(header_lines)
    for block in blocks:
        # Dedent one level so the class-body slice can be re-indented cleanly.
        body.append(re.sub(r"^    ", "", extract(block), flags=re.M))

    src = f"class {name}:\n" + "\n".join(
        re.sub(r"^(?=.)", "    ", chunk, flags=re.M) for chunk in body
    )

    ns = {
        "difflib": __import__("difflib"),
        "re": __import__("re"),
        "Path": Path,
    }
    ns.update(extra_globals or {})
    exec(compile(src, "<agent_core-extract>", "exec"), ns)
    return ns[name]
