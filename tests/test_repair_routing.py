"""Tests for tiered model routing on the small-repair path.

Two costs were being paid on every chapter regardless of how small the fix was:
generation ran on Pro whenever the caller asked for it, and CONTINUITY_MINOR
always paid for an independent Pro review.

Exact-patch application already constrains a candidate quite hard, so most of
what the reviewer confirmed was checkable from the text itself: which units got
patched, how much changed, whether edits stayed inside the located spans, and
whether the chapter tail moved. `_repair_verify_patches` makes those checks
programmatically and only escalates what it cannot settle.

The danger in a cheap path is that it passes something unsafe, so the tests here
are weighted toward escalation: each individual signal is checked in isolation
for its ability to block, and the tail check protects Canon end state.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import LOCATOR_BLOCK, build_class

Agent = build_class("Agent", [LOCATOR_BLOCK])

PARAS = [f"这是第{i}段正文，写得足够长以便改动比例不会因为章节太短而失真。" * 3
         for i in range(10)]
BODY = "\n\n".join(PARAS)


def anchor_at(i):
    start = BODY.index(PARAS[i])
    return {"start": start, "end": start + len(PARAS[i])}


def patch_inside(i, old_len=12):
    """A patch whose `old` sits inside paragraph i."""
    old = PARAS[i][:old_len]
    return {"unit": 1, "old": old, "new": old.replace("第", "某")}


def meta_for(patches, unit_count=1, covered=None, unattributed=0):
    covered = list(range(1, unit_count + 1)) if covered is None else covered
    return {
        "patch_count": len(patches),
        "unit_count": unit_count,
        "units_covered": covered,
        "units_uncovered": [u for u in range(1, unit_count + 1) if u not in covered],
        "unattributed_patches": unattributed,
        "patches": patches,
    }


def apply_patches(patches):
    out = BODY
    for p in patches:
        out = out.replace(p["old"], p["new"], 1)
    return out


def verify(patches, cls="TEXT_ONLY", anchors=None, **meta_kw):
    return Agent._repair_verify_patches(
        BODY, apply_patches(patches), meta_for(patches, **meta_kw), cls,
        anchors=anchors if anchors is not None else [anchor_at(3)],
    )


class TestProgrammaticPass:
    def test_small_in_scope_text_only_passes(self):
        assert verify([patch_inside(3)])["verdict"] == "pass"

    def test_continuity_minor_also_gets_the_cheap_path(self):
        """This is the saving: CONTINUITY_MINOR used to always pay for Pro."""
        result = verify([patch_inside(3)], cls="CONTINUITY_MINOR")
        assert result["verdict"] == "pass"

    def test_pass_reports_no_reasons(self):
        assert verify([patch_inside(3)])["reasons"] == []

    def test_change_ratio_is_reported(self):
        result = verify([patch_inside(3)])
        assert 0 < result["change_ratio"] < 1

    def test_multiple_units_all_covered_passes(self):
        patches = [
            dict(patch_inside(2), unit=1),
            dict(patch_inside(6), unit=2),
        ]
        result = Agent._repair_verify_patches(
            BODY, apply_patches(patches), meta_for(patches, unit_count=2),
            "CONTINUITY_MINOR", anchors=[anchor_at(2), anchor_at(6)],
        )
        assert result["verdict"] == "pass"


class TestEscalation:
    def test_uncovered_unit_escalates(self):
        patches = [dict(patch_inside(2), unit=1)]
        result = Agent._repair_verify_patches(
            BODY, apply_patches(patches),
            meta_for(patches, unit_count=2, covered=[1]),
            "CONTINUITY_MINOR", anchors=[anchor_at(2), anchor_at(6)],
        )
        assert result["verdict"] == "escalate"

    def test_unattributed_patch_escalates(self):
        """If we cannot tell which requirement a patch serves, we cannot claim
        the requirement was satisfied."""
        result = verify([patch_inside(3)], unattributed=1)
        assert result["verdict"] == "escalate"

    def test_no_change_escalates(self):
        result = Agent._repair_verify_patches(
            BODY, BODY, meta_for([]), "TEXT_ONLY", anchors=[anchor_at(3)],
        )
        assert result["verdict"] == "escalate"

    def test_oversized_change_escalates(self):
        """A patch large enough to rewrite a paragraph is no longer the kind of
        edit these program checks can vouch for."""
        old = PARAS[3]
        patches = [{"unit": 1, "old": old, "new": "整段都换成完全不同的新内容。" * 8}]
        result = verify(patches)
        assert result["verdict"] == "escalate"

    def test_tail_change_escalates(self):
        """The chapter tail carries Canon end state, so any movement there needs
        a semantic judgement no character count can provide."""
        result = verify([patch_inside(9)], anchors=[anchor_at(9)])
        assert result["verdict"] == "escalate"
        assert any("章末" in r for r in result["reasons"])

    def test_out_of_scope_patch_escalates(self):
        """A patch nowhere near the reported problem is touching text nobody
        complained about."""
        result = verify([patch_inside(7)], anchors=[anchor_at(3)])
        assert result["verdict"] == "escalate"
        assert any("定位范围之外" in r for r in result["reasons"])

    def test_missing_anchors_escalate(self):
        """With no spans there is no scope to verify against, so the cheap path
        must not be available."""
        result = verify([patch_inside(3)], anchors=[])
        assert result["verdict"] == "escalate"

    def test_rewrite_class_never_uses_the_cheap_path(self):
        result = verify([patch_inside(3)], cls="REWRITE_SPAN")
        assert result["verdict"] == "escalate"

    def test_escalation_always_explains_itself(self):
        """The reason list is fed to the reviewer as its focus, so an empty
        escalation would silently waste the call."""
        for kwargs in (
            {"unattributed": 1},
            {"covered": [], "unit_count": 1},
        ):
            result = verify([patch_inside(3)], **kwargs)
            assert result["verdict"] == "escalate"
            assert result["reasons"]

    def test_every_check_is_reported(self):
        result = verify([patch_inside(3)])
        for key in ("changed", "units_covered", "all_attributed",
                    "ratio_ok", "tail_intact", "patches_in_scope"):
            assert key in result["checks"]


class TestTailWindow:
    """The Canon-end-state guard uses a tail window, capped at a quarter of the
    chapter. Without the cap a short chapter is entirely 'tail' and can never
    take the cheap path; with it, edits genuinely near the end still escalate."""

    def test_edit_near_the_end_escalates_in_a_long_chapter(self):
        long_body = "\n\n".join(PARAS * 4)
        old = PARAS[9][:12]
        cand = long_body[::-1].replace(old[::-1], old.replace("第", "某")[::-1], 1)[::-1]
        start = long_body.rindex(old)
        result = Agent._repair_verify_patches(
            long_body, cand,
            meta_for([{"unit": 1, "old": old, "new": old.replace("第", "某")}]),
            "TEXT_ONLY", anchors=[{"start": start, "end": start + len(old)}],
        )
        assert result["verdict"] == "escalate"

    def test_early_edit_passes_in_a_short_chapter(self):
        short = "\n\n".join(PARAS[:4])
        old = PARAS[0][:10]
        patches = [{"unit": 1, "old": old, "new": old.replace("第", "某")}]
        start = short.index(old)
        result = Agent._repair_verify_patches(
            short, short.replace(old, patches[0]["new"], 1),
            meta_for(patches), "TEXT_ONLY",
            anchors=[{"start": start, "end": start + len(PARAS[0])}],
        )
        assert result["verdict"] == "pass"


class TestGenerationModel:
    def test_flash_is_the_default(self):
        assert Agent._repair_generation_model("TEXT_ONLY", 1) == "deepseek-v4-flash"

    def test_continuity_minor_also_starts_on_flash(self):
        model = Agent._repair_generation_model("CONTINUITY_MINOR", 1,
                                               preferred="deepseek-v4-flash")
        assert model == "deepseek-v4-flash"

    def test_second_attempt_stays_on_flash(self):
        """Escalating immediately would spend Pro on transient formatting slips."""
        model = Agent._repair_generation_model("TEXT_ONLY", 2,
                                               preferred="deepseek-v4-flash")
        assert model == "deepseek-v4-flash"

    def test_final_attempt_escalates_to_pro(self):
        model = Agent._repair_generation_model("TEXT_ONLY", 3,
                                               preferred="deepseek-v4-flash")
        assert model == "deepseek-v4-pro"

    def test_explicit_pro_request_is_honoured(self):
        """The Pro single-chapter retry path depends on this."""
        for attempt in (1, 2, 3):
            model = Agent._repair_generation_model("TEXT_ONLY", attempt,
                                                   preferred="deepseek-v4-pro")
            assert model == "deepseek-v4-pro"

    def test_unknown_preference_falls_back_to_flash(self):
        model = Agent._repair_generation_model("TEXT_ONLY", 1, preferred="gpt-9")
        assert model == "deepseek-v4-flash"
