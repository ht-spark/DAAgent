"""运行执行管理：内存态（取消/预算/澄清）+ 统一 store 持久化。

本地单机单人：用 asyncio.to_thread 在后台线程跑同步的 run_graph，
进度事件写入 tool 消息的 metadata，供前端 SSE 与历史回看。
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 确保项目根目录在 sys.path，便于 import scr.math_modeling_agent
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scr.math_modeling_agent.graph import run_graph  # noqa: E402
from scr.math_modeling_agent.llm import create_llm  # noqa: E402
from scr.runtime.budget import BudgetType  # noqa: E402
from server import store  # noqa: E402

_MAX_PROGRESS_EVENTS = 500
_RUN_KIND = "modeling"

_lock = threading.Lock()

# 取消意图标志：run_id -> True。仅存在于进程内存；
# 服务重启后后台任务本就不会存活，无需持久化。
_cancel_flags: dict[str, bool] = {}

# 正在执行的任务预算管理器。仅用于向前端暴露实时 token / LLM 耗时，
# 任务结束后仍以 progress 中持久化的 budget_summary 为准。
_active_budget_managers: dict[str, Any] = {}
_active_budget_lock = threading.Lock()


def get_run_monitor(run_id: str) -> dict[str, int] | None:
    """返回运行中任务的实时预算快照；非运行中任务返回 ``None``。"""
    with _active_budget_lock:
        manager = _active_budget_managers.get(run_id)
    if manager is None:
        return None
    totals = manager.get_total_usage()
    return {
        "time_total": int(totals.get(BudgetType.TIME, 0)),
        "token_total": int(totals.get(BudgetType.TOKEN, 0)),
    }

# ---------------------------------------------------------------------------
# 预算确认（人工介入）：run 跑到 configure_question_budget 节点时暂停，
# 等待前端弹窗确认该问预算覆盖后再继续。仅进程内存。
# ---------------------------------------------------------------------------
_budget_lock = threading.Lock()
# run_id -> {"event": Event, "question_id": str, "proposed": dict}
_pending_budget: dict[str, dict] = {}
# run_id -> 用户决定（dict[BudgetType值, int] 或 None=用默认）
_budget_decisions: dict[str, dict | None] = {}


def request_budget_confirmation(
    run_id: str, question_id: str, proposed: dict
) -> threading.Event:
    """注册一个待确认的预算请求，返回用于阻塞等待的 Event。"""
    ev = threading.Event()
    with _budget_lock:
        _pending_budget[run_id] = {
            "event": ev,
            "question_id": question_id,
            "proposed": proposed,
        }
        _budget_decisions.pop(run_id, None)
    return ev


def get_pending_budget(run_id: str) -> dict | None:
    """查询某个 run 当前是否有待确认的预算请求（供前端轮询/判断）。"""
    with _budget_lock:
        p = _pending_budget.get(run_id)
        return dict(p) if p else None


def submit_budget_decision(run_id: str, decision: dict | None) -> bool:
    """前端提交预算决定；写入并唤醒阻塞中的回调。无 pending 返回 False。"""
    with _budget_lock:
        p = _pending_budget.get(run_id)
        if not p:
            return False
        _budget_decisions[run_id] = decision
        p["event"].set()
    return True


def take_budget_decision(run_id: str) -> dict | None:
    """回调取出决定（并移除）。"""
    with _budget_lock:
        return _budget_decisions.pop(run_id, None)


def clear_budget_pending(run_id: str) -> None:
    with _budget_lock:
        _pending_budget.pop(run_id, None)
        _budget_decisions.pop(run_id, None)


# ---------------------------------------------------------------------------
# 人工介入（G0 硬失败澄清）：run 跑到 g0_clarification 节点时暂停，
# 等待前端弹窗选择"终止"或"上传补充材料继续"后再继续。仅进程内存。
# ---------------------------------------------------------------------------
_clarification_lock = threading.Lock()
# run_id -> {"event": Event, "failed_checks": list[str]}
_pending_clarification: dict[str, dict] = {}
# run_id -> {"action": "terminate"|"continue", "new_data_paths": list[str]}
_clarification_decisions: dict[str, dict] = {}


def request_clarification(
    run_id: str, failed_checks: list[str]
) -> threading.Event:
    """注册一个待确认的 G0 澄清请求，返回用于阻塞等待的 Event。"""
    ev = threading.Event()
    with _clarification_lock:
        _pending_clarification[run_id] = {
            "event": ev,
            "failed_checks": failed_checks,
        }
        _clarification_decisions.pop(run_id, None)
    return ev


def get_pending_clarification(run_id: str) -> dict | None:
    """查询某个 run 当前是否有待确认的 G0 澄清请求。"""
    with _clarification_lock:
        p = _pending_clarification.get(run_id)
        return dict(p) if p else None


def submit_clarification_decision(run_id: str, decision: dict) -> bool:
    """前端提交澄清决定；写入并唤醒阻塞中的回调。无 pending 返回 False。"""
    with _clarification_lock:
        p = _pending_clarification.get(run_id)
        if not p:
            return False
        _clarification_decisions[run_id] = decision
        p["event"].set()
    return True


def take_clarification_decision(run_id: str) -> dict | None:
    """回调取出决定（并移除）。"""
    with _clarification_lock:
        return _clarification_decisions.pop(run_id, None)


def clear_clarification_pending(run_id: str) -> None:
    with _clarification_lock:
        _pending_clarification.pop(run_id, None)
        _clarification_decisions.pop(run_id, None)


# ---------------------------------------------------------------------------
# 持久化：tool 消息（id = run_id）+ 元数据
# ---------------------------------------------------------------------------


def _load_run_meta(run_id: str) -> dict[str, Any] | None:
    msg = store.get_message(run_id)
    if msg is None or msg.get("role") != store.ROLE_TOOL:
        return None
    meta = msg.get("metadata") or {}
    if meta.get("kind") and meta.get("kind") != _RUN_KIND:
        return None
    return {
        "run_id": run_id,
        "session_id": msg["session_id"],
        "created_at": msg["created_at"],
        "updated_at": msg["updated_at"],
        "problem_preview": meta.get("problem_preview"),
        "task_name": meta.get("task_name"),
        "model_config": meta.get("model_config") or {},
        "status": meta.get("status", "queued"),
        "workflow_status": meta.get("workflow_status"),
        "current_question_id": meta.get("current_question_id"),
        "results_count": meta.get("results_count"),
        "paper_title": meta.get("paper_title"),
        "review_status": meta.get("review_status"),
        "error": meta.get("error"),
        "progress": meta.get("progress") or [],
        "artifacts": meta.get("artifacts") or [],
        "output_dir": meta.get("output_dir"),
    }


def _update_run_meta(run_id: str, **fields: Any) -> None:
    store.update_message_metadata(run_id, metadata=fields, merge=True)


def create_run(
    run_id: str,
    problem_preview: str,
    model_config: dict,
    task_name: str | None = None,
    session_id: str | None = None,
) -> str:
    """登记一次新运行：建会话（若无）并写入 tool 消息。返回 session_id。"""
    safe_cfg = {k: v for k, v in (model_config or {}).items() if k != "api_key"}
    title = (task_name or problem_preview or "建模任务").strip()[:80] or "建模任务"
    sid = session_id or store.create_session(title)
    store.append_message(
        sid,
        store.ROLE_TOOL,
        content=problem_preview or title,
        message_id=run_id,
        metadata={
            "kind": _RUN_KIND,
            "status": "queued",
            "problem_preview": problem_preview,
            "task_name": task_name,
            "model_config": safe_cfg,
            "progress": [],
            "artifacts": [],
            "error": None,
        },
    )
    return sid


def rename_run(run_id: str, task_name: str) -> bool:
    """更新任务名称（同时同步会话标题）。"""
    msg = store.get_message(run_id)
    if msg is None:
        return False
    name = (task_name or "").strip()[:80]
    if not name:
        return False
    _update_run_meta(run_id, task_name=name)
    store.rename_session(msg["session_id"], name)
    return True


def mark_running(run_id: str) -> None:
    _update_run_meta(run_id, status="running")


def mark_failed(run_id: str, error: str) -> None:
    _update_run_meta(run_id, status="failed", error=error)


def mark_cancelled(run_id: str) -> None:
    _update_run_meta(run_id, status="cancelled", error="用户已中断任务")


def set_result(run_id: str, summary: dict, artifacts: list[str]) -> None:
    _update_run_meta(
        run_id,
        status="succeeded",
        workflow_status=summary.get("workflow_status"),
        paper_title=summary.get("paper_title"),
        review_status=summary.get("review_status"),
        results_count=summary.get("results_count"),
        artifacts=artifacts,
        error=None,
    )


def append_progress(run_id: str, event: dict) -> None:
    """记录一个进度事件，并同步更新当前状态快照。"""
    with _lock:
        row = _load_run_meta(run_id)
        if row is None:
            return
        events = list(row.get("progress") or [])
        events.append(event)
        if len(events) > _MAX_PROGRESS_EVENTS:
            events = events[-_MAX_PROGRESS_EVENTS:]
        wf = event.get("workflow_status") or row.get("workflow_status")
        cq = event.get("current_question_id") or row.get("current_question_id")
        rc = event.get("results_count")
        if rc is None:
            rc = row.get("results_count")
        _update_run_meta(
            run_id,
            progress=events,
            workflow_status=wf,
            current_question_id=cq,
            results_count=rc,
        )


def get_run(run_id: str) -> dict | None:
    with _lock:
        return _load_run_meta(run_id)


def list_runs(limit: int = 50) -> list[dict]:
    items = store.list_tool_messages(kind=_RUN_KIND, limit=limit)
    out: list[dict] = []
    for msg in items:
        meta = msg.get("metadata") or {}
        out.append(
            {
                "run_id": msg["id"],
                "session_id": msg["session_id"],
                "created_at": msg["created_at"],
                "updated_at": msg["updated_at"],
                "status": meta.get("status", "queued"),
                "workflow_status": meta.get("workflow_status"),
                "current_question_id": meta.get("current_question_id"),
                "results_count": meta.get("results_count"),
                "paper_title": meta.get("paper_title"),
                "review_status": meta.get("review_status"),
                "error": meta.get("error"),
                "problem_preview": meta.get("problem_preview"),
                "task_name": meta.get("task_name"),
                "progress": meta.get("progress") or [],
                "artifacts": meta.get("artifacts") or [],
                "model_config": meta.get("model_config") or {},
            }
        )
    return out


def cleanup_stale_runs(max_age_seconds: int = 300) -> int:
    """把处于 queued/running 但已超过 max_age_seconds 没更新的运行标记为 failed。"""
    now = datetime.now(timezone.utc)
    cleaned = 0
    for row in list_runs(limit=500):
        if row.get("status") not in ("queued", "running"):
            continue
        try:
            ts = datetime.fromisoformat(row["updated_at"])
        except (ValueError, TypeError):
            ts = now
        age = (now - ts).total_seconds()
        if age >= max_age_seconds:
            _update_run_meta(
                row["run_id"],
                status="failed",
                error=f"服务重启后未完成（已停止 {int(age)}s）",
            )
            cleaned += 1
    return cleaned


def cancel_run(run_id: str) -> dict:
    """请求中断一个正在执行（queued/running）的任务。"""
    row = get_run(run_id)
    if row is None:
        return {"cancelled": False, "blocked": False, "reason": "not_found"}
    status = row["status"]
    if status in ("succeeded", "failed", "cancelled"):
        return {
            "cancelled": False,
            "blocked": True,
            "reason": f"任务已结束（{status}），无需中断",
        }
    _cancel_flags[run_id] = True
    if status == "queued":
        mark_cancelled(run_id)
    return {"cancelled": True, "blocked": False, "reason": "ok"}


def delete_run(run_id: str) -> dict:
    """删除一条运行记录（tool 消息 + 产物目录）。

    出于安全考虑：正在执行（queued/running）的任务不允许删除。
    """
    row = get_run(run_id)
    if row is None:
        return {"deleted": False, "blocked": False, "reason": "not_found"}
    status = row["status"]
    if status in ("queued", "running"):
        return {
            "deleted": False,
            "blocked": True,
            "reason": f"任务仍在执行（{status}），请等待完成后再删除",
        }

    store.delete_message(run_id)

    art_dir = PROJECT_ROOT / "artifacts" / run_id
    try:
        if art_dir.exists():
            shutil.rmtree(art_dir, ignore_errors=True)
    except Exception:
        pass
    return {"deleted": True, "blocked": False, "reason": "ok"}


# ---------------------------------------------------------------------------
# 后台执行
# ---------------------------------------------------------------------------


def _summarize_final(state: dict) -> dict:
    paper = state.get("paper_draft")
    review = state.get("review_report")
    return {
        "workflow_status": state.get("workflow_status"),
        "paper_title": getattr(paper, "title", None) if paper else None,
        "review_status": getattr(review, "overall_status", None) if review else None,
        "results_count": len(state.get("question_results") or {}),
    }


def _collect_artifacts(output_dir: str) -> list[str]:
    """列出产物目录下相对路径（供前端下载与展示）。"""
    base = Path(output_dir)
    if not base.exists():
        return []
    allowed = {"paper.md", "paper.docx", "review_report.json"}
    out: list[str] = []
    for p in sorted(base.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(base).as_posix()
        top = rel.split("/")[0]
        if rel in allowed or top in ("figures", "questions", "context", "input"):
            out.append(rel)
    return out


async def execute_run(
    run_id: str,
    problem_text: str,
    data_paths: list[str],
    output_dir: str,
    model_config: dict,
) -> None:
    """后台执行一次完整解题流程。"""
    mark_running(run_id)
    llm = create_llm(
        provider=model_config.get("provider"),
        api_key=model_config.get("api_key"),
        base_url=model_config.get("base_url"),
        model=model_config.get("model"),
        max_tokens=model_config.get("max_tokens"),
        temperature=model_config.get("temperature"),
    )

    def _callback(event: dict) -> None:
        append_progress(run_id, event)

    def _cancel_check() -> bool:
        return _cancel_flags.get(run_id, False)

    def _budget_callback(state: dict) -> dict | None:
        """在每个子任务开始前请求搜索、验证和代码修复预算。

        返回 {BudgetType: int} 作为该问覆盖；返回 None 表示沿用默认。
        本函数在后台线程（asyncio.to_thread）中执行，可安全阻塞。
        """
        qid = state.get("current_question_id", "") or "current"
        bm = state.get("budget_manager")
        proposed: dict = {}
        if bm is not None:
            try:
                proposed = {
                    BudgetType.SEARCH.value: bm.get_record(BudgetType.SEARCH).limit,
                    BudgetType.VALIDATION_ITERATION.value:
                    bm.get_record(BudgetType.VALIDATION_ITERATION).limit,
                    BudgetType.CODE_REPAIR.value:
                    bm.get_record(BudgetType.CODE_REPAIR).limit,
                }
            except Exception:  # noqa: BLE001
                proposed = {}
        ev = request_budget_confirmation(run_id, qid, proposed)
        append_progress(
            run_id,
            {
                "type": "budget_request",
                "phase": "question",
                "node": "configure_question_budget",
                "question_id": qid,
                "proposed": proposed,
                "timestamp": time.time(),
            },
        )
        # 阻塞等待用户决定；每秒检查一次取消意图，便于中断时及时退出
        while not ev.wait(timeout=1.0):
            if _cancel_flags.get(run_id, False):
                clear_budget_pending(run_id)
                append_progress(
                    run_id,
                    {
                        "type": "budget_confirmed",
                        "phase": "question",
                        "question_id": qid,
                        "action": "cancelled",
                        "timestamp": time.time(),
                    },
                )
                return None
        decision = take_budget_decision(run_id)
        clear_budget_pending(run_id)
        if not decision:
            append_progress(
                run_id,
                {
                    "type": "budget_confirmed",
                    "phase": "question",
                    "question_id": qid,
                    "action": "default",
                    "timestamp": time.time(),
                },
            )
            return None
        append_progress(
            run_id,
            {
                "type": "budget_confirmed",
                "phase": "question",
                "question_id": qid,
                "action": "override",
                "limits": decision,
                "timestamp": time.time(),
            },
        )
        return {
            BudgetType(key): int(value)
            for key, value in decision.items()
            if key in {
                BudgetType.SEARCH.value,
                BudgetType.VALIDATION_ITERATION.value,
                BudgetType.CODE_REPAIR.value,
            }
        }

    def _request_initial_budget() -> dict | None:
        """任务启动前请求用户配置 G0 输入质量门重试预算。

        在后台线程中阻塞等待用户确认。返回 {BudgetType: int} 或 None（沿用默认）。
        """
        from scr.runtime.budget import DEFAULT_BUDGETS
        proposed = {
            BudgetType.INTAKE_RETRY.value:
            DEFAULT_BUDGETS[BudgetType.INTAKE_RETRY],
        }
        ev = request_budget_confirmation(run_id, "", proposed)
        append_progress(
            run_id,
            {
                "type": "budget_request",
                "phase": "initial",
                "node": "initial_budget",
                "question_id": "",
                "proposed": proposed,
                "timestamp": time.time(),
            },
        )
        while not ev.wait(timeout=1.0):
            if _cancel_flags.get(run_id, False):
                clear_budget_pending(run_id)
                append_progress(
                    run_id,
                    {
                        "type": "budget_confirmed",
                        "phase": "initial",
                        "action": "cancelled",
                        "timestamp": time.time(),
                    },
                )
                return None
        decision = take_budget_decision(run_id)
        clear_budget_pending(run_id)
        if not decision:
            append_progress(
                run_id,
                {
                    "type": "budget_confirmed",
                    "phase": "initial",
                    "action": "default",
                    "timestamp": time.time(),
                },
            )
            return None
        append_progress(
            run_id,
            {
                "type": "budget_confirmed",
                "phase": "initial",
                "action": "override",
                "limits": decision,
                "timestamp": time.time(),
            },
        )
        return {
            BudgetType(key): int(value)
            for key, value in decision.items()
            if key == BudgetType.INTAKE_RETRY.value
        }

    def _delivery_budget_callback(state: dict) -> dict | None:
        """所有子任务完成后请求用户配置 GF 交付修订预算。"""
        from scr.runtime.budget import DEFAULT_BUDGETS

        proposed = {
            BudgetType.PAPER_REVISION.value:
            DEFAULT_BUDGETS[BudgetType.PAPER_REVISION],
        }
        ev = request_budget_confirmation(run_id, "", proposed)
        append_progress(
            run_id,
            {
                "type": "budget_request",
                "phase": "delivery",
                "node": "configure_delivery_budget",
                "question_id": "",
                "proposed": proposed,
                "timestamp": time.time(),
            },
        )
        while not ev.wait(timeout=1.0):
            if _cancel_flags.get(run_id, False):
                clear_budget_pending(run_id)
                return None
        decision = take_budget_decision(run_id)
        clear_budget_pending(run_id)
        append_progress(
            run_id,
            {
                "type": "budget_confirmed",
                "phase": "delivery",
                "question_id": "",
                "action": "override" if decision else "default",
                "limits": decision or {},
                "timestamp": time.time(),
            },
        )
        return {
            BudgetType(key): int(value)
            for key, value in (decision or {}).items()
            if key == BudgetType.PAPER_REVISION.value
        }

    def _clarification_callback(state: dict) -> dict | None:
        """G0 硬失败时暂停等用户选择终止或补充材料继续。

        返回 {"action": "terminate"} 或 {"action": "continue", "new_data_paths": [...]}。
        本函数在后台线程（asyncio.to_thread）中执行，可安全阻塞。
        """
        failed_checks = state.get("_g0_failed_checks", [])
        run_id_val = state.get("run_id", "") or run_id
        ev = request_clarification(run_id_val, failed_checks)
        append_progress(
            run_id_val,
            {
                "type": "clarification_request",
                "failed_checks": failed_checks,
                "timestamp": time.time(),
            },
        )
        # 阻塞等待用户决定；每秒检查一次取消意图
        while not ev.wait(timeout=1.0):
            if _cancel_flags.get(run_id_val, False):
                clear_clarification_pending(run_id_val)
                append_progress(
                    run_id_val,
                    {
                        "type": "clarification_resolved",
                        "action": "cancelled",
                        "timestamp": time.time(),
                    },
                )
                return {"action": "terminate"}
        decision = take_clarification_decision(run_id_val)
        clear_clarification_pending(run_id_val)
        action = decision.get("action", "terminate") if decision else "terminate"
        append_progress(
            run_id_val,
            {
                "type": "clarification_resolved",
                "action": action,
                "timestamp": time.time(),
            },
        )
        return decision or {"action": "terminate"}

    bm = None
    try:
        # 任务启动前：请求用户配置 G0 输入质量门预算。
        from scr.runtime.budget import BudgetManager
        bm = BudgetManager()
        with _active_budget_lock:
            _active_budget_managers[run_id] = bm
        initial_limits = await asyncio.to_thread(_request_initial_budget)
        if initial_limits:
            bm.update_run_limits(initial_limits)

        final_state = await asyncio.to_thread(
            run_graph,
            problem_text=problem_text,
            data_paths=data_paths,
            output_dir=output_dir,
            llm=llm,
            run_id=run_id,
            progress_callback=_callback,
            console=False,
            cancel_check=_cancel_check,
            budget_manager=bm,
            budget_config_callback=_budget_callback,
            delivery_budget_config_callback=_delivery_budget_callback,
            clarification_callback=_clarification_callback,
        )
        # 任务完成后推送预算统计（time/token），前端在聊天中展示
        bm = final_state.get("budget_manager")
        if bm is not None:
            try:
                totals = bm.get_total_usage()
                append_progress(
                    run_id,
                    {
                        "type": "budget_summary",
                        "time_total": totals.get(BudgetType.TIME, 0),
                        "token_total": totals.get(BudgetType.TOKEN, 0),
                        "timestamp": time.time(),
                    },
                )
            except Exception:  # noqa: BLE001
                pass
        set_result(run_id, _summarize_final(final_state), _collect_artifacts(output_dir))
    except Exception as exc:  # noqa: BLE001
        # 记录完整堆栈，便于排查 NoneType.get 等深层 bug
        import traceback
        print(f"[run {run_id}] 执行失败: {type(exc).__name__}: {exc}", flush=True)
        traceback.print_exc()
        if _cancel_flags.get(run_id, False) or "cancelled" in str(exc).lower():
            mark_cancelled(run_id)
        else:
            mark_failed(run_id, f"{type(exc).__name__}: {exc}")
    finally:
        with _active_budget_lock:
            _active_budget_managers.pop(run_id, None)
