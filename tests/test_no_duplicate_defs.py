"""Guard against silently shadowed method definitions in agent_core.

A 9000-line class body makes it easy to paste a second copy of a method: Python
keeps only the last definition, so the earlier one plus everything reachable
only from it becomes dead code that still reads as live.  This is how an earlier
DLC contract pipeline ended up unreachable behind the live one.
"""
import ast
import collections
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ["agent_core.py", "app.py"]


def _class_defs(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            names = [
                child.name
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            yield node.name, names


@pytest.mark.parametrize("filename", SOURCES)
def test_no_duplicate_method_definitions(filename):
    path = ROOT / filename
    if not path.exists():
        pytest.skip(f"{filename} not present")
    offenders = {}
    for cls_name, names in _class_defs(path):
        dupes = {n: c for n, c in collections.Counter(names).items() if c > 1}
        if dupes:
            offenders[cls_name] = dupes
    assert not offenders, (
        f"{filename} has shadowed method definitions (only the last one runs): {offenders}"
    )


@pytest.mark.parametrize("filename", SOURCES)
def test_no_duplicate_module_level_defs(filename):
    path = ROOT / filename
    if not path.exists():
        pytest.skip(f"{filename} not present")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    dupes = {n: c for n, c in collections.Counter(names).items() if c > 1}
    assert not dupes, f"{filename} has shadowed module-level functions: {dupes}"


def test_removed_dlc_contract_pipeline_stays_removed():
    """The removed DLC contract helpers must not creep back in."""
    src = (ROOT / "agent_core.py").read_text(encoding="utf-8")
    for name in (
        "_dlc_contract_prompt",
        "_dlc_contract_with_repair",
        "_dlc_fallback_contract",
        "_dlc_normalize_contract",
        "_dlc_review_candidate",
        "_dlc_source_context",
    ):
        assert f"def {name}" not in src, f"{name} was removed as dead code; do not reintroduce"


def test_live_dlc_helpers_still_present():
    """These are shared by the surviving Grok path; removing them breaks DLC."""
    src = (ROOT / "agent_core.py").read_text(encoding="utf-8")
    for name in ("_dlc_json_list", "_dlc_normalize_review", "_dlc_repetition_violations"):
        assert f"def {name}" in src, f"{name} is still called by the live DLC path"
