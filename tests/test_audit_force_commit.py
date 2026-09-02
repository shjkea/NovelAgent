"""Contracts for manual and forced audit-repair submission.

The repair panel has three intentionally different paths:

* ordinary submission is limited to candidates that passed every automatic gate;
* manual submission lets an operator choose a generated candidate whose own
  candidate gate passed, even when the evidence/joint gate needs human review;
* forced submission is a per-chapter escape hatch for a failed quality gate.

The last path is deliberately narrow.  It may bypass the quality verdict only
after an explicit ``FORCE`` confirmation and reason, but it must still verify
the source hash, archive the old project state, and rebuild the Canon bundle
(including Summary/Memory/Handoff).  These tests are source contracts because
importing the full desktop app would start provider/database infrastructure.
"""

from __future__ import annotations

import ast
import copy
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AGENT_PATH = ROOT / "agent_core.py"
APP_PATH = ROOT / "app.py"
JS_PATH = ROOT / "static" / "app_3_1.js"
HTML_PATH = ROOT / "static" / "index.html"
CSS_PATH = ROOT / "static" / "app_3_1.css"

AGENT_SOURCE = AGENT_PATH.read_text(encoding="utf-8")
APP_SOURCE = APP_PATH.read_text(encoding="utf-8")
JS_SOURCE = JS_PATH.read_text(encoding="utf-8")
HTML_SOURCE = HTML_PATH.read_text(encoding="utf-8")
CSS_SOURCE = CSS_PATH.read_text(encoding="utf-8")


def _module_tree(source: str, path: Path):
    return ast.parse(source, filename=str(path))


def _class_node(source: str, path: Path, name: str):
    return next(
        node
        for node in _module_tree(source, path).body
        if isinstance(node, ast.ClassDef) and node.name == name
    )


def _method_node(source: str, path: Path, class_name: str, name: str):
    cls = _class_node(source, path, class_name)
    return next(
        node
        for node in cls.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    )


def _function_node(source: str, path: Path, name: str):
    return next(
        node
        for node in _module_tree(source, path).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    )


def _segment(source: str, node: ast.AST) -> str:
    text = ast.get_source_segment(source, node)
    assert text, f"无法提取 {type(node).__name__} 的源码"
    return text


def _field_names(cls: ast.ClassDef):
    names = set()
    for stmt in cls.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            names.add(stmt.target.id)
        elif isinstance(stmt, ast.Assign):
            names.update(
                target.id
                for target in stmt.targets
                if isinstance(target, ast.Name)
            )
    return names


def _call_nodes(node: ast.AST, attribute: str):
    return [
        item
        for item in ast.walk(node)
        if isinstance(item, ast.Call)
        and isinstance(item.func, ast.Attribute)
        and item.func.attr == attribute
    ]


def _compile_method_class(name: str, method: ast.AST):
    """Compile one dependency-free NovelAgent method without importing app code."""
    method = copy.deepcopy(method)
    cls = ast.ClassDef(
        name=name, bases=[], keywords=[], body=[method], decorator_list=[]
    )
    module = ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[]))
    namespace = {}
    exec(compile(module, str(AGENT_PATH), "exec"), namespace)
    return namespace[name]


class TestCommitRequestContract:
    def test_request_exposes_manual_and_force_controls_with_safe_defaults(self):
        cls = _class_node(APP_SOURCE, APP_PATH, "AuditRepairCommitPatch")
        fields = _field_names(cls)
        assert {"manual", "force", "confirm", "force_reason"} <= fields

        defaults = {}
        for stmt in cls.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                if stmt.value is not None:
                    defaults[stmt.target.id] = ast.unparse(stmt.value)
        assert defaults.get("manual", "False").lower() in {"false", "none"}
        assert defaults.get("force", "False").lower() in {"false", "none"}
        assert defaults.get("confirm", "''") in {"''", '""'}
        assert defaults.get("force_reason", "''") in {"''", '""'}

    def test_endpoint_forwards_every_control_to_core(self):
        endpoint = _function_node(APP_SOURCE, APP_PATH, "audit_repair_commit")
        calls = _call_nodes(endpoint, "commit_audit_repair")
        assert calls, "API endpoint 没有调用 NovelAgent.commit_audit_repair"
        forwarded = {
            kw.arg
            for call in calls
            for kw in call.keywords
            if kw.arg is not None
        }
        assert {"manual", "force", "confirm", "force_reason"} <= forwarded


class TestCommitCoreContract:
    @classmethod
    def setup_class(cls):
        cls.node = _method_node(
            AGENT_SOURCE, AGENT_PATH, "NovelAgent", "commit_audit_repair"
        )
        cls.source = _segment(AGENT_SOURCE, cls.node)
        cls.confirm_node = _method_node(
            AGENT_SOURCE, AGENT_PATH, "NovelAgent", "_repair_force_confirmation_valid"
        )
        cls.confirm_source = _segment(AGENT_SOURCE, cls.confirm_node)

    def test_method_accepts_manual_and_force_arguments(self):
        args = self.node.args
        names = {
            item.arg for item in [*args.posonlyargs, *args.args, *args.kwonlyargs]
        }
        assert {"manual", "force", "confirm", "force_reason"} <= names

    def test_force_requires_explicit_confirmation_and_reason(self):
        # Keep this check intentionally semantic rather than tied to one exact
        # spelling (``confirm.strip().upper()`` and equivalent forms are fine).
        combined = self.source + "\n" + self.confirm_source
        assert re.search(r"confirm[\s\S]{0,240}FORCE|FORCE[\s\S]{0,240}confirm", combined, re.I)
        assert re.search(r"force_reason", combined)
        assert re.search(r"force_reason[\s\S]{0,500}(空|必|缺|reason|strip|len|not)", combined, re.I)

    def test_force_confirmation_helper_rejects_implicit_acknowledgements(self):
        helper = _compile_method_class("ForceConfirmation", self.confirm_node)
        checker = helper._repair_force_confirmation_valid
        assert checker("FORCE") is True
        assert checker("confirm force") is True
        assert checker("强制提交") is True
        for value in ("", "yes", "确认", "FORCED", "force it"):
            assert checker(value) is False

    def test_manual_path_is_distinct_from_force_path(self):
        args = self.node.args
        arg_names = {
            item.arg for item in [*args.posonlyargs, *args.args, *args.kwonlyargs]
        }
        names = {item.id for item in ast.walk(self.node) if isinstance(item, ast.Name)}
        assert {"manual", "force"} <= (arg_names | names)
        # A manual review must not be implemented as an alias for force.
        assert not re.search(r"manual\s*(?:or|\|)\s*force|force\s*(?:or|\|)\s*manual", self.source, re.I)
        assert re.search(r"manual[\s\S]{0,500}(approved|candidate|safe)|(?:approved|candidate|safe)[\s\S]{0,500}manual", self.source, re.I)

    def test_source_hash_check_is_unconditional_and_precedes_publish(self):
        hash_checks = [
            node
            for node in ast.walk(self.node)
            if isinstance(node, ast.If)
            and "_repair_hash" in ast.unparse(node.test)
            and "original_sha256" in ast.unparse(node.test)
        ]
        assert hash_checks, "提交路径缺少候选生成后的原文 SHA256 校验"
        assert all("force" not in ast.unparse(node.test).lower() for node in hash_checks)
        first_hash = self.source.find("_repair_hash(current)")
        first_publish = self.source.find("_commit_canon_bundle(")
        assert first_hash >= 0 and first_publish >= 0
        assert first_hash < first_publish

    def test_old_state_is_archived_before_any_publish(self):
        archive_markers = (
            "archive.mkdir",
            "_repair_archive_sidecars",
            "_repair_archive_project_files",
            "shutil.copy2",
        )
        archive_positions = [self.source.find(marker) for marker in archive_markers]
        archive_positions = [pos for pos in archive_positions if pos >= 0]
        publish = self.source.find("_commit_canon_bundle(")
        assert archive_positions and publish >= 0
        assert min(archive_positions) < publish

    def test_quality_gate_has_a_force_escape_but_hash_does_not(self):
        quality_terms = ("meta.get(\"safe\")", "meta.get('safe')", "needs_revision", "final_review")
        assert any(term in self.source for term in quality_terms)
        # The quality rejection must be conditional on force (directly or via
        # a nearby ``if not force`` block); otherwise force=true cannot do what
        # the UI promises.  Conversely the test above ensures the hash check is
        # outside that escape hatch.
        assert any(word in self.source.lower() for word in (
            "force", "forced", "bypass", "override", "绕过", "强制",
        ))
        force_guard_nodes = []
        for candidate in ast.walk(self.node):
            if not isinstance(candidate, ast.If):
                continue
            test = ast.unparse(candidate.test).lower()
            body = ast.unparse(ast.Module(body=candidate.body, type_ignores=[])).lower()
            if "force" in test and "raise" in body:
                force_guard_nodes.append((test, body))
        assert force_guard_nodes, "质量门失败分支没有 force 逃生通道"

    def test_publish_receives_rebuilt_summary_memory_and_handoff(self):
        publish = self.source.find("_commit_canon_bundle(")
        assert publish >= 0
        before = self.source[:publish]
        call_tail = self.source[publish:]
        assert "summarize_and_extract_memories" in before or "memory" in before.lower()
        assert "summary=summary" in call_tail
        assert re.search(r"memories\s*=\s*memory_records", call_tail)
        assert re.search(r"handoff\s*=\s*handoff", call_tail)

    def test_force_audit_trail_records_reason_and_gate_state(self):
        assert re.search(r"[\"']forced[\"']", self.source)
        assert "force_reason" in self.source
        assert re.search(r"quality|gate|review", self.source, re.I)


class TestIncrementalCommitAndRollbackContract:
    """A repair batch remains usable after one chapter is committed.

    Forced submission is intentionally per chapter, so treating the first
    force as a terminal commit for the whole batch would strand every other
    candidate.  The same applies when automatic and manually approved rows are
    submitted in separate UI actions.  These checks stay source-only, but pin
    the persistence rules needed for a later cumulative rollback.
    """

    @classmethod
    def setup_class(cls):
        cls.commit_node = _method_node(
            AGENT_SOURCE, AGENT_PATH, "NovelAgent", "commit_audit_repair"
        )
        cls.commit_source = _segment(AGENT_SOURCE, cls.commit_node)
        cls.detail_node = _method_node(
            AGENT_SOURCE, AGENT_PATH, "NovelAgent", "audit_repair_batch_detail"
        )
        cls.detail_source = _segment(AGENT_SOURCE, cls.detail_node)
        cls.rollback_node = _method_node(
            AGENT_SOURCE, AGENT_PATH, "NovelAgent", "rollback_audit_repair"
        )
        cls.rollback_source = _segment(AGENT_SOURCE, cls.rollback_node)

    def test_existing_manifest_does_not_globally_reject_a_new_chapter(self):
        global_guards = []
        for candidate in ast.walk(self.commit_node):
            if not isinstance(candidate, ast.If):
                continue
            test = ast.unparse(candidate.test)
            body = ast.unparse(ast.Module(body=candidate.body, type_ignores=[]))
            if (
                "existing_rows" in test
                and "selected" not in test
                and "requested" not in test
                and "raise" in body.lower()
            ):
                global_guards.append(test)
        assert not global_guards, (
            "已有任意 manifest row 就拒绝整批后续提交；逐章强制后无法继续提交其他章"
        )

    def test_batch_detail_disables_only_the_committed_chapter(self):
        assert not re.search(
            r"(?:manual_selectable|force_selectable)[^\n]{0,240}"
            r"manifest\.get\([\"']committed[\"']\)",
            self.detail_source,
        ), "首章提交后，后端把同批次所有未提交候选也标成不可选"
        assert "committed_rows" in self.detail_source
        option_start = self.detail_source.find('row["commit_options"]')
        option_tail = self.detail_source[option_start: option_start + 1400]
        assert re.search(
            r"(?:n\s+not\s+in\s+committed_rows|not\s+\w*committed\w*)",
            option_tail,
            re.I,
        ), "候选可选状态没有按章排除已提交记录"

    def test_manifest_accumulates_prior_rows_and_binds_each_row_to_its_archive(self):
        append_at = self.commit_source.find("manifest_rows.append")
        assert append_at >= 0
        later = self.commit_source[append_at:]
        assert "existing_rows" in later or "existing_manifest" in later, (
            "新一轮提交清单会覆盖旧 rows，普通/人工/强制分次提交历史无法累积"
        )
        row_window = self.commit_source[append_at: append_at + 2200]
        assert re.search(r"[\"']archive_dir[\"']\s*:", row_window), (
            "累计清单的每章 row 必须记录本轮 archive_dir，不能只依赖会变化的顶层归档"
        )

    def test_rollback_uses_each_manifest_rows_own_archive(self):
        calls = _call_nodes(self.rollback_node, "_archived_canon_bundle")
        assert calls, "累计回滚没有读取提交前 Canon 归档"
        assert re.search(r"row\.get\([\"']archive_dir[\"']\)", self.rollback_source)
        assert all(
            call.args and (
                "row" in ast.unparse(call.args[0]).lower()
                or ast.unparse(call.args[0]) == "row_archive"
            )
            for call in calls
        ), "累计回滚仍使用顶层单一 archive_dir，会找不到较早提交轮次的备份"

    def test_failed_later_round_does_not_overwrite_prior_manifest_rows(self):
        handlers = [
            node for node in ast.walk(self.commit_node)
            if isinstance(node, ast.ExceptHandler)
            and "commit_exc" in ast.unparse(node)
            and "failure_manifest" in ast.unparse(node)
        ]
        assert handlers
        for handler in handlers:
            source = ast.unparse(handler)
            assert "existing_rows" in source or "existing_manifest" in source, (
                "后续提交轮次若回滚不完整，会用 failure_manifest 抹掉此前成功提交的 rows"
            )


class TestAuditRepairFrontendContract:
    def test_manual_and_force_controls_are_visible(self):
        for dom_id in (
            "auditRepairManualBtn",
            "chapterCompareForceBtn",
            "chapterCompareChangedOnly",
        ):
            assert f'id="{dom_id}"' in HTML_SOURCE

    def test_manual_candidates_can_be_selected_without_auto_gate(self):
        assert "data-repair-select" in JS_SOURCE
        # The old bug disabled a checkbox whenever auto_commit_allowed was
        # false.  A separate manual/selectable predicate must now exist.
        assert re.search(r"manual(?:_?eligible|_?allowed)|selectable|canManual", JS_SOURCE, re.I)
        old = re.compile(
            r"can\s*&&\s*isApproved\s*&&\s*!isCommitted[^\n]{0,180}disabled"
        )
        assert not old.search(JS_SOURCE), "人工复核仍被旧的 auto_commit_allowed 条件禁用"

    def test_manual_and_force_requests_carry_explicit_controls(self):
        for field in ("manual", "force", "confirm", "force_reason"):
            assert re.search(rf"\b{re.escape(field)}\s*:", JS_SOURCE), f"前端请求缺少 {field}"
        assert re.search(r"confirm\s*:\s*['\"]FORCE['\"]", JS_SOURCE, re.I)
        assert re.search(r"force_reason\s*:", JS_SOURCE)

    def test_manual_button_and_force_button_are_wired(self):
        for dom_id in ("auditRepairManualBtn", "chapterCompareForceBtn"):
            assert JS_SOURCE.count(dom_id) >= 2, f"{dom_id} 只声明未绑定事件"
        assert re.search(r"auditRepairManualBtn[^\n]{0,500}(onclick|commit|post)", JS_SOURCE, re.I | re.S)
        assert re.search(r"chapterCompareForceBtn[\s\S]{0,1200}(force|commit)", JS_SOURCE, re.I)

    def test_compare_dialog_renders_colored_diff_and_changed_only_filter(self):
        assert "chapterCompareChangedOnly" in JS_SOURCE
        compare_start = JS_SOURCE.find("async function openAuditRepairCompare")
        assert compare_start >= 0
        compare_window = JS_SOURCE[compare_start: compare_start + 7000]
        assert "innerHTML" in compare_window, "章节对比仍使用纯 textContent，无法显示彩色 Diff"
        assert re.search(r"diff-(?:add|added|insert)", compare_window, re.I)
        assert re.search(r"diff-(?:del|delete|removed)", compare_window, re.I)
        assert re.search(
            r"diff-(?:change|modify|replace)|type\s*:\s*['\"]change",
            compare_window, re.I,
        )

    def test_diff_classes_have_visual_colors(self):
        assert "diff-add" in CSS_SOURCE
        assert any(token in CSS_SOURCE for token in ("diff-del", "diff-remove"))
        assert any(token in CSS_SOURCE for token in (
            "diff-change", "diff-char-add", "diff-char-remove",
        ))
        # At least one foreground/background declaration must accompany the
        # semantic classes; otherwise the markup is colored only in name.
        style_start = min(CSS_SOURCE.find(".diff-add"), CSS_SOURCE.find(".diff-del"))
        assert style_start >= 0
        style_window = CSS_SOURCE[style_start: style_start + 1800]
        assert re.search(r"(?:background|color)\s*:", style_window)

    def test_force_submission_is_single_chapter(self):
        start = JS_SOURCE.find("async function forceAuditRepairChapter")
        assert start >= 0
        end = JS_SOURCE.find("\n  async function ", start + 10)
        window = JS_SOURCE[start: end if end >= 0 else start + 3000]
        assert "chapterNo" in window or "chapter_no" in window
        assert "force" in window.lower()
        assert re.search(r"chapters\s*:\s*\[\s*n\s*\]", window)
