"""Tests for trimming the patch prompt down to the located paragraphs.

The patch generator used to paste the entire chapter into every request, so a
one-word date fix in a 9,000-character chapter cost the same as a substantial
one. Since the locator now resolves each unit to exact offsets, the prompt can
carry just the offending paragraphs plus their neighbours.

The risk in trimming is silent loss of coverage: an excerpt that omits a unit
would make the model patch something it cannot see, or make an `old` string look
unique when it is not unique in the full chapter. These tests pin the savings
and both failure modes.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import LOCATOR_BLOCK, build_class

Agent = build_class("Agent", [LOCATOR_BLOCK])

PARAS = [f"这是第{i}段的正文内容，用来把章节撑到足够长度。" * 4 for i in range(12)]
BODY = "\n\n".join(PARAS)


def anchor_for(para_index, needle=None):
    """Return an anchor pointing at a paragraph, or at a needle inside it."""
    start = BODY.index(PARAS[para_index])
    if needle is None:
        return {"start": start, "end": start + len(PARAS[para_index])}
    offset = PARAS[para_index].index(needle)
    return {"start": start + offset, "end": start + offset + len(needle)}


class TestWindowedMode:
    def test_single_unit_is_trimmed(self):
        text, meta = Agent._repair_patch_context(BODY, [anchor_for(5)])
        assert meta["mode"] == "windowed"
        assert meta["chars"] < meta["full_chars"]

    def test_target_paragraph_is_present_verbatim(self):
        """Trimming is only safe if the model can still copy `old` exactly."""
        text, _ = Agent._repair_patch_context(BODY, [anchor_for(5)])
        assert PARAS[5] in text

    def test_neighbours_are_included(self):
        text, _ = Agent._repair_patch_context(BODY, [anchor_for(5)])
        assert PARAS[4] in text
        assert PARAS[6] in text

    def test_distant_paragraphs_are_excluded(self):
        text, _ = Agent._repair_patch_context(BODY, [anchor_for(5)])
        assert PARAS[0] not in text
        assert PARAS[11] not in text

    def test_elision_is_marked_so_the_model_knows_text_was_removed(self):
        """Without a marker the excerpt reads like the whole chapter, which
        invites the model to 'fix' a discontinuity that is not really there."""
        text, _ = Agent._repair_patch_context(BODY, [anchor_for(5)])
        assert "略" in text

    def test_offsets_are_reported_for_each_window(self):
        text, meta = Agent._repair_patch_context(BODY, [anchor_for(5)])
        w = meta["windows"][0]
        assert BODY[w["start"]:w["end"]] in text

    def test_sub_paragraph_anchor_still_sends_whole_paragraph(self):
        """A short quote is not enough to patch against; the model needs the
        surrounding sentence boundaries to build a unique `old`."""
        text, meta = Agent._repair_patch_context(BODY, [anchor_for(5, "第5段")])
        assert meta["mode"] == "windowed"
        assert PARAS[5] in text


class TestMultipleUnits:
    def test_every_unit_is_covered(self):
        """The headline guarantee: no unit may be trimmed out of the prompt."""
        anchors = [anchor_for(1), anchor_for(6), anchor_for(10)]
        text, _ = Agent._repair_patch_context(BODY, anchors)
        for i in (1, 6, 10):
            assert PARAS[i] in text

    def test_distant_units_stay_separate(self):
        anchors = [anchor_for(1), anchor_for(10)]
        _, meta = Agent._repair_patch_context(BODY, anchors)
        assert meta["window_count"] == 2

    def test_adjacent_units_are_merged(self):
        """Neighbouring windows overlap; emitting both would duplicate text and
        make the excerpt contradict itself."""
        anchors = [anchor_for(5), anchor_for(6)]
        text, meta = Agent._repair_patch_context(BODY, anchors)
        assert meta["window_count"] == 1
        assert text.count(PARAS[5]) == 1

    def test_windows_are_emitted_in_reading_order(self):
        anchors = [anchor_for(10), anchor_for(1)]
        text, _ = Agent._repair_patch_context(BODY, anchors)
        assert text.index(PARAS[1]) < text.index(PARAS[10])


class TestFullFallback:
    def test_unlocated_unit_forces_full_chapter(self):
        """A unit with no offsets has no defensible excerpt, so trimming any of
        the chapter could hide exactly the text it needs."""
        anchors = [anchor_for(5), {"start": -1, "end": -1}]
        text, meta = Agent._repair_patch_context(BODY, anchors)
        assert meta["mode"] == "full"
        assert text == BODY

    def test_missing_offsets_force_full_chapter(self):
        text, meta = Agent._repair_patch_context(BODY, [{"instruction": "改日期"}])
        assert meta["mode"] == "full"
        assert text == BODY

    def test_no_anchors_force_full_chapter(self):
        text, meta = Agent._repair_patch_context(BODY, [])
        assert meta["mode"] == "full"
        assert text == BODY

    def test_out_of_range_offsets_force_full_chapter(self):
        """Offsets past the end mean the chapter changed under us; trusting them
        would slice at the wrong place."""
        text, meta = Agent._repair_patch_context(
            BODY, [{"start": 10, "end": len(BODY) + 500}],
        )
        assert meta["mode"] == "full"
        assert text == BODY

    def test_non_numeric_offsets_force_full_chapter(self):
        text, meta = Agent._repair_patch_context(
            BODY, [{"start": "第五段", "end": None}],
        )
        assert meta["mode"] == "full"
        assert text == BODY

    def test_broad_coverage_sends_plain_chapter(self):
        """When windows already cover nearly everything, excerpt markers add
        confusion for no token saving."""
        anchors = [anchor_for(i) for i in range(0, 12, 2)]
        text, meta = Agent._repair_patch_context(BODY, anchors)
        assert meta["mode"] == "full"
        assert text == BODY

    def test_short_chapter_is_not_carved_up(self):
        short = "只有一段的短章节正文。"
        text, meta = Agent._repair_patch_context(
            short, [{"start": 0, "end": len(short)}],
        )
        assert meta["mode"] == "full"
        assert text == short

    def test_empty_chapter_is_handled(self):
        text, meta = Agent._repair_patch_context("", [{"start": 0, "end": 0}])
        assert text == ""
        assert meta["mode"] == "full"

    def test_full_mode_reports_a_reason(self):
        _, meta = Agent._repair_patch_context(BODY, [])
        assert meta["reason"]


class TestSavings:
    def test_trimming_a_long_chapter_is_a_large_saving(self):
        _, meta = Agent._repair_patch_context(BODY, [anchor_for(5)])
        assert meta["chars"] < meta["full_chars"] * 0.5

    def test_full_chars_is_recorded_in_both_modes(self):
        """Cost attribution needs the baseline even when nothing was trimmed."""
        for anchors in ([anchor_for(5)], []):
            _, meta = Agent._repair_patch_context(BODY, anchors)
            assert meta["full_chars"] == len(BODY)
