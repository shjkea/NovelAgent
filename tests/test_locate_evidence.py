"""Tests for the deterministic evidence locator and span windowing.

These cover the zero-LLM foundation of audit-driven repair: if the locator is
wrong, every downstream stage either edits the wrong sentence or falls back to
whole-chapter prompts. The fuzzy tier especially must not produce confident
matches on unrelated text.
"""
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_locator():
    """Load the locator methods off NovelAgent without importing heavy deps.

    agent_core imports provider_router and other runtime modules that are not
    needed here, so the class body is extracted and exec'd in isolation.
    """
    src = (ROOT / "agent_core.py").read_text(encoding="utf-8")

    start = src.index("    # ---------------- deterministic evidence locator (zero LLM) ----------------")
    end = src.index("    def _repair_chat_json(")
    body = src[start:end]

    # Dedent one level so the block can live in a standalone class.
    body = re.sub(r"^    ", "", body, flags=re.M)

    ns = {}
    exec(
        "import difflib\n"
        "class Locator:\n"
        + re.sub(r"^", "    ", body, flags=re.M),
        ns,
    )
    return ns["Locator"]


Locator = _load_locator()

CHAPTER = """第七章 夜行

沈砚推开窗，寒气涌进来。他记得那是三月十二日的夜里，师父第一次教他握剑。

远处的灯火明明灭灭，像谁在数着不肯说出口的心事。他把手放在窗棂上，指节发白。

“你还没睡？”门外传来阿禾的声音，很轻，怕惊动了什么。

沈砚没有回答。他在想那柄剑，想它为什么会在祠堂里。三月十二日，他记得很清楚。

天快亮的时候，雨落了下来。"""


class TestExactTier:
    def test_unique_exact_match(self):
        r = Locator._locate_evidence(CHAPTER, "寒气涌进来")
        assert r["ok"] is True
        assert r["method"] == "exact"
        assert r["confidence"] == 1.0
        assert CHAPTER[r["start"]:r["end"]] == "寒气涌进来"

    def test_matched_text_is_verbatim_slice(self):
        quote = "远处的灯火明明灭灭"
        r = Locator._locate_evidence(CHAPTER, quote)
        assert r["matched_text"] == CHAPTER[r["start"]:r["end"]]

    def test_ambiguous_exact_match_is_rejected(self):
        # This phrase appears twice; committing to either one would be a guess.
        r = Locator._locate_evidence(CHAPTER, "三月十二日")
        assert r["ok"] is False
        assert r["method"] == "exact_multi"
        assert r["occurrences"] == 2
        assert "无法唯一定位" in r["reason"]

    def test_longer_quote_disambiguates(self):
        r = Locator._locate_evidence(CHAPTER, "那是三月十二日的夜里")
        assert r["ok"] is True
        assert r["method"] == "exact"


class TestFoldedTier:
    def test_whitespace_differences_are_tolerated(self):
        r = Locator._locate_evidence(CHAPTER, "寒气涌进来\n\n  ")
        assert r["ok"] is True
        # Trailing whitespace is stripped, so this still resolves exactly.
        assert r["method"] in {"exact", "folded"}

    def test_internal_whitespace_insertion_is_folded(self):
        r = Locator._locate_evidence(CHAPTER, "远处的灯火 明明灭灭")
        assert r["ok"] is True
        assert r["method"] == "folded"
        assert r["matched_text"] == "远处的灯火明明灭灭"

    def test_punctuation_variant_is_folded(self):
        # Model quoted with an ASCII comma instead of the full-width one.
        r = Locator._locate_evidence(CHAPTER, "沈砚推开窗,寒气涌进来")
        assert r["ok"] is True
        assert r["method"] == "folded"
        assert r["matched_text"] == "沈砚推开窗，寒气涌进来"

    def test_curly_quote_variant_is_folded(self):
        r = Locator._locate_evidence(CHAPTER, '"你还没睡？"')
        assert r["ok"] is True
        assert r["method"] == "folded"

    def test_folded_span_maps_back_to_exact_original(self):
        r = Locator._locate_evidence(CHAPTER, "他 把手放在窗棂上, 指节发白")
        assert r["ok"] is True
        assert r["matched_text"] == CHAPTER[r["start"]:r["end"]]
        assert "指节发白" in r["matched_text"]


class TestFuzzyTier:
    def test_small_paraphrase_still_locates(self):
        # One character dropped and one substituted: still clearly the same line.
        r = Locator._locate_evidence(CHAPTER, "天快亮的时侯，雨落下来了")
        assert r["ok"] is True
        assert r["method"] == "fuzzy"
        assert "雨落" in r["matched_text"]

    def test_unrelated_text_is_rejected(self):
        r = Locator._locate_evidence(
            CHAPTER,
            "这是一段完全不存在于本章的文字，讲的是别的事情，与本章毫无关系",
        )
        assert r["ok"] is False
        assert r["method"] == "fuzzy_failed"

    def test_short_quote_refuses_fuzzy_matching(self):
        # Too short to fuzzy-match safely: a false positive would edit the wrong spot.
        r = Locator._locate_evidence(CHAPTER, "灯火阑")
        assert r["ok"] is False
        assert "过短" in r["reason"]

    def test_threshold_is_respected(self):
        quote = "天快亮的时侯，雨落下来了"
        loose = Locator._locate_evidence(CHAPTER, quote, min_ratio=0.5)
        strict = Locator._locate_evidence(CHAPTER, quote, min_ratio=0.999)
        assert loose["ok"] is True
        assert strict["ok"] is False


class TestDegenerateInput:
    @pytest.mark.parametrize("original,quote", [
        ("", "something"),
        (CHAPTER, ""),
        ("", ""),
        (CHAPTER, "   \n  "),
    ])
    def test_empty_inputs_fail_cleanly(self, original, quote):
        r = Locator._locate_evidence(original, quote)
        assert r["ok"] is False
        assert r["start"] == -1

    def test_punctuation_only_quote_fails_cleanly(self):
        r = Locator._locate_evidence(CHAPTER, "，。、")
        assert r["ok"] is False


class TestSpanWindow:
    def test_window_includes_neighbouring_paragraphs(self):
        r = Locator._locate_evidence(CHAPTER, "门外传来阿禾的声音")
        assert r["ok"] is True
        window, w_start, w_end = Locator._repair_span_window(
            CHAPTER, r["start"], r["end"], before=1, after=1
        )
        assert "阿禾" in window
        assert "指节发白" in window       # previous paragraph
        assert "他在想那柄剑" in window   # next paragraph
        assert window == CHAPTER[w_start:w_end]

    def test_window_is_smaller_than_whole_chapter(self):
        r = Locator._locate_evidence(CHAPTER, "门外传来阿禾的声音")
        window, _, _ = Locator._repair_span_window(CHAPTER, r["start"], r["end"])
        assert len(window) < len(CHAPTER)

    def test_zero_context_yields_only_hit_paragraph(self):
        r = Locator._locate_evidence(CHAPTER, "门外传来阿禾的声音")
        window, _, _ = Locator._repair_span_window(
            CHAPTER, r["start"], r["end"], before=0, after=0
        )
        assert "阿禾" in window
        assert "指节发白" not in window

    def test_window_at_chapter_start_does_not_underflow(self):
        r = Locator._locate_evidence(CHAPTER, "寒气涌进来")
        window, w_start, w_end = Locator._repair_span_window(
            CHAPTER, r["start"], r["end"], before=5, after=0
        )
        assert w_start == 0
        assert window == CHAPTER[w_start:w_end]

    def test_window_at_chapter_end_does_not_overflow(self):
        r = Locator._locate_evidence(CHAPTER, "雨落了下来")
        window, w_start, w_end = Locator._repair_span_window(
            CHAPTER, r["start"], r["end"], before=0, after=5
        )
        assert w_end == len(CHAPTER)
        assert window == CHAPTER[w_start:w_end]

    def test_empty_original_is_safe(self):
        window, s, e = Locator._repair_span_window("", 0, 0)
        assert (window, s, e) == ("", 0, 0)


class TestFoldHelper:
    def test_index_map_length_matches_folded_length(self):
        folded, index_map = Locator._locate_fold(CHAPTER)
        assert len(folded) == len(index_map)

    def test_index_map_points_at_source_characters(self):
        folded, index_map = Locator._locate_fold("你好，世界")
        # Every folded char must trace back to a real position in the source.
        for i, ch in enumerate(folded):
            assert 0 <= index_map[i] < len("你好，世界")

    def test_whitespace_is_dropped(self):
        folded, _ = Locator._locate_fold("你  好\n世\t界")
        assert folded == "你好世界"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
