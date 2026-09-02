"""Tests for the local audit-report noise filter.

This is the only component that deliberately discards audit content before the
model sees it, so the tests are weighted toward the expensive failure mode: a
real finding being dropped. False negatives here mean a bug never gets fixed
and the pipeline still reports success, which is exactly the behaviour we are
trying to eliminate. Keeping a little extra prompt text is the cheap mistake.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import NOISE_BLOCK, build_class

Filter = build_class("Filter", [NOISE_BLOCK])


def block(*paragraphs):
    return "\n\n".join(paragraphs)


REAL_FINDING = (
    "### 第 42 章\n"
    "时间线冲突：第 42 章称距离父亲离家已过三年，但第 17 章明确写为五年前。"
    "需要统一为五年，并检查第 40 至 44 章内的相关表述。"
)


class TestNoiseRemoval:
    @pytest.mark.parametrize("noise", [
        "### 第 10-19 章\n总体良好，人物动机连贯。",
        "本区间未发现明显矛盾。",
        "无明显问题。",
        "建议继续观察后续章节的伏笔回收情况。",
        "该段整体表现稳定，暂无需修改。",
        "保持现状即可。",
        "细节差异不影响阅读理解。",
    ])
    def test_short_noise_blocks_are_dropped(self, noise):
        out, stats = Filter._repair_filter_noise(block(REAL_FINDING, noise))
        assert noise not in out
        assert stats["dropped_blocks"] == 1

    def test_real_finding_survives(self):
        out, _ = Filter._repair_filter_noise(
            block("总体良好。", REAL_FINDING, "继续观察。")
        )
        assert "时间线冲突" in out
        assert "第 17 章" in out

    def test_stats_report_what_was_removed(self):
        src = block(REAL_FINDING, "总体良好。", "未发现明显矛盾。")
        out, stats = Filter._repair_filter_noise(src)
        assert stats["dropped_blocks"] == 2
        assert stats["dropped_chars"] > 0
        assert stats["kept_chars"] == len(out)
        assert stats["kept_chars"] < len(src)


class TestKeepBias:
    def test_long_block_with_noise_phrase_is_kept(self):
        """A 'continue observing' aside next to a real problem must not take the
        problem down with it."""
        mixed = (
            "### 第 55 章\n"
            "苏漪在本章提到自己从未去过渡口，但第 48 章她已在渡口与沈砚见面，"
            "这是一处直接的事实冲突，需要修改第 55 章的表述以保持一致。"
            "其余部分总体良好，建议继续观察后续伏笔。"
        )
        out, stats = Filter._repair_filter_noise(mixed)
        assert "事实冲突" in out
        assert stats["dropped_blocks"] == 0

    def test_all_noise_input_falls_back_to_original(self):
        """Returning an empty report would look like a clean audit, which is a
        worse lie than an unfiltered prompt."""
        src = block("总体良好。", "未发现明显问题。", "继续观察。")
        out, stats = Filter._repair_filter_noise(src)
        assert out == src
        assert stats["dropped_blocks"] == 0
        assert "fallback" in stats

    def test_unrecognized_prose_is_kept(self):
        src = "第 3 章的雨景描写与第 4 章的晴天开场之间缺少过渡。"
        out, _ = Filter._repair_filter_noise(src)
        assert out == src


class TestDegenerateInput:
    def test_empty_input(self):
        out, stats = Filter._repair_filter_noise("")
        assert out == ""
        assert stats["kept_chars"] == 0

    def test_whitespace_only_input(self):
        out, stats = Filter._repair_filter_noise("   \n\n  \t ")
        assert out == ""
        assert stats["dropped_blocks"] == 0

    def test_none_input(self):
        out, _ = Filter._repair_filter_noise(None)
        assert out == ""

    def test_headings_and_bullets_are_probed_not_counted(self):
        """Markdown decoration must not stop a noise block from being matched."""
        out, stats = Filter._repair_filter_noise(
            block(REAL_FINDING, "## 小结\n- 总体良好\n- 继续观察")
        )
        assert "小结" not in out
        assert stats["dropped_blocks"] == 1

    def test_output_stays_valid_markdown_paragraphs(self):
        out, _ = Filter._repair_filter_noise(
            block(REAL_FINDING, "总体良好。", "第 60 章的称谓前后不一致。")
        )
        assert "\n\n\n" not in out
        assert out == out.strip()
