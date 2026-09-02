"""Static guards for the audit-repair frontend.

There is no JS runtime here, so these tests do what a linter would: strip the
file down to executable code and assert structural properties.  They are aimed
at the three failure modes that actually bit during this refactor:

  1. A full-width Chinese bracket typed into code position.  The UI strings are
     Chinese, so `（` sits one keystroke away from `(` and produces a file that
     parses as garbage while looking completely normal.
  2. Writing to a DOM id that does not exist in index.html, which fails silently
     because `text()` no-ops on a missing element.
  3. Adding a helper and forgetting to call it, which is how the retry counters
     stayed invisible even after the helper existed.
"""

import re
import unittest
from pathlib import Path

from _jslex import strip_js

ROOT = Path(__file__).resolve().parents[1]
JS_PATH = ROOT / "static" / "app_3_1.js"
HTML_PATH = ROOT / "static" / "index.html"

JS_SRC = JS_PATH.read_text(encoding="utf-8")
JS_CODE = strip_js(JS_SRC)
HTML_SRC = HTML_PATH.read_text(encoding="utf-8")


class TestLexerItself(unittest.TestCase):
    """The guards below are only as trustworthy as the lexer under them."""

    def test_blanks_string_contents(self):
        out = strip_js("const a='（全角）';")
        self.assertNotIn("（", out)
        self.assertIn("const a=", out)

    def test_preserves_offsets_and_lines(self):
        src = "a='x';\n// c\nb=`t${1}`;\n"
        out = strip_js(src)
        self.assertEqual(len(out), len(src))
        self.assertEqual(out.count("\n"), src.count("\n"))

    def test_keeps_template_interpolation_code(self):
        # Interpolations are real code and must stay visible to the guards.
        out = strip_js("x=`前缀${foo(bar)}后缀`;")
        self.assertIn("${foo(bar)}", out)
        self.assertNotIn("前缀", out)

    def test_regex_literal_not_treated_as_division(self):
        out = strip_js("s.replace(/[&<>'\"]/g,'x');")
        # The quote inside the character class must not open a string, which
        # would swallow the rest of the file.
        self.assertIn("s.replace(", out)
        self.assertIn(");", out)

    def test_division_not_treated_as_regex(self):
        out = strip_js("const r=a/b, s=c/d;")
        self.assertIn("const r=a/b, s=c/d;", out)

    def test_strips_both_comment_forms(self):
        out = strip_js("a;// 注释（）\nb;/* 块（）*/c;")
        self.assertNotIn("注释", out)
        self.assertNotIn("块", out)
        self.assertNotIn("（", out)


class TestNoFullWidthPunctuationInCode(unittest.TestCase):
    """Guard 1: Chinese punctuation belongs in strings, never in code."""

    # Full-width forms of characters that are syntactically meaningful in JS.
    FORBIDDEN = "（）；，：？［］｛｝＝＋－！｜＆"

    def test_code_positions_are_ascii_punctuation(self):
        offenders = []
        for lineno, line in enumerate(JS_CODE.splitlines(), 1):
            for ch in line:
                if ch in self.FORBIDDEN:
                    offenders.append((lineno, ch))
        self.assertEqual(offenders, [], f"全角标点出现在代码位置: {offenders}")


class TestBracketBalance(unittest.TestCase):
    """A cheap stand-in for a parser: brackets must nest and close."""

    PAIRS = {")": "(", "]": "[", "}": "{"}

    def test_brackets_balanced(self):
        stack = []
        for lineno, line in enumerate(JS_CODE.splitlines(), 1):
            for ch in line:
                if ch in "([{":
                    stack.append((ch, lineno))
                elif ch in self.PAIRS:
                    self.assertTrue(stack, f"第 {lineno} 行多出一个 {ch}")
                    opener, open_line = stack.pop()
                    self.assertEqual(
                        opener, self.PAIRS[ch],
                        f"第 {lineno} 行的 {ch} 与第 {open_line} 行的 {opener} 不匹配",
                    )
        self.assertEqual(stack, [], f"未闭合括号: {stack}")


class TestDomIdsExist(unittest.TestCase):
    """Guard 2: text()/html() into a missing id fails silently."""

    def _html_ids(self):
        return set(re.findall(r'id="([A-Za-z0-9_-]+)"', HTML_SRC))

    def _referenced_ids(self):
        # Only literal single-argument ids; dynamic ids are out of scope.
        pat = re.compile(r"""\b(?:text|html|\$)\(\s*'([A-Za-z0-9_-]+)'""")
        return set(pat.findall(JS_SRC))

    def test_every_referenced_id_is_declared(self):
        missing = sorted(self._referenced_ids() - self._html_ids())
        self.assertEqual(missing, [], f"JS 引用了 index.html 中不存在的 id: {missing}")

    def test_repair_panel_ids_present(self):
        # The fields added for the new pipeline stages, spelled out so a
        # renamed container cannot quietly drop them from the panel.
        for dom_id in ("chapterCompareMeta", "chapterCompareTitle"):
            self.assertIn(dom_id, self._html_ids())


class TestHelpersAreWired(unittest.TestCase):
    """Guard 3: a defined-but-uncalled helper is invisible to the operator."""

    def _call_count(self, name):
        return len(re.findall(rf"\b{name}\s*\(", JS_CODE))

    def _is_defined(self, name):
        return re.search(rf"function\s+{name}\s*\(", JS_CODE) is not None

    def test_attempt_text_helper_is_called(self):
        self.assertTrue(self._is_defined("auditRepairAttemptText"))
        # Definition plus at least one call site.
        self.assertGreater(
            self._call_count("auditRepairAttemptText"), 1,
            "auditRepairAttemptText 已定义但没有任何调用点",
        )

    def test_class_label_helper_is_called(self):
        self.assertTrue(self._is_defined("auditRepairClassLabel"))
        self.assertGreater(self._call_count("auditRepairClassLabel"), 1)


class TestClassLabelCoverage(unittest.TestCase):
    """Every repair_class the backend can emit needs a human-readable label."""

    # Mirrors AUDIT_FIX_CLASSES plus the legacy-only classes in agent_core.py.
    BACKEND_CLASSES = (
        "TEXT_ONLY", "CONTINUITY_MINOR", "REWRITE_SPAN", "REWRITE_CHAPTER",
        "DEFER_FUTURE", "NEEDS_EVIDENCE", "MANUAL_ONLY",
    )

    def test_backend_classes_have_labels(self):
        body = self._label_body()
        missing = [c for c in self.BACKEND_CLASSES if c not in body]
        self.assertEqual(missing, [], f"auditRepairClassLabel 缺少标签: {missing}")

    def _label_body(self):
        m = re.search(
            r"function auditRepairClassLabel\s*\([^)]*\)\s*\{(.*?)\n  \}",
            JS_SRC, re.S,
        )
        self.assertIsNotNone(m, "找不到 auditRepairClassLabel 定义")
        return m.group(1)

    def test_manual_only_says_chapter_file_missing(self):
        # MANUAL_ONLY no longer means "too big to fix"; it means the chapter
        # file could not be opened.  A stale label would misdirect triage.
        body = self._label_body()
        m = re.search(r"MANUAL_ONLY['\"]?\s*:\s*['\"]([^'\"]+)", body)
        self.assertIsNotNone(m, "MANUAL_ONLY 没有对应标签文案")
        self.assertIn("章节文件", m.group(1))


class TestRejectedPatchesSurfaced(unittest.TestCase):
    """Partial patch application must show why individual patches were refused."""

    def test_compare_dialog_reads_patch_rejections(self):
        self.assertIn("patch_rejections", JS_SRC)

    def test_rejection_reason_is_rendered(self):
        # Reading the array but never rendering `reason` would still leave the
        # operator guessing.
        window = JS_SRC[JS_SRC.index("patch_rejections"):]
        window = window[:1200]
        self.assertIn("reason", window)


if __name__ == "__main__":
    unittest.main()
