"""交付前的独立证据复核，不执行候选回答内的代码。"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..cancellation import TaskCancelledError, raise_if_cancelled
from ..observability.trace import RunTrace
from .loop import LlmClient, _project_observations, invoke_llm_with_public_progress
from .state import AgentState, AnalysisPlan, clip_text

LOGGER = logging.getLogger(__name__)
PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "verifier.md"
# 限制单次复核的候选文本与上下文大小；截断时不能把证据缺失判作事实错误。
MAX_ANSWER_CHARS = 24_000
MAX_CONTEXT_CHARS = 48_000


class VerificationIssue(BaseModel):
    """可公开的问题及证据定位，禁止输出隐藏推理。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)
    code: str = Field(min_length=1, max_length=80)
    message: str = Field(min_length=1, max_length=1200)
    evidence: str = Field(min_length=1, max_length=1600)


class Verification(BaseModel):
    """单次复核结果；最终未修复的失败会明确降为未完全验证。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal["pass", "fail", "uncertain"]
    issues: list[VerificationIssue] = Field(default_factory=list, max_length=20)
    checked_artifacts: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_issues(self) -> "Verification":
        """失败和不确定须说明原因，通过不能同时报告问题。"""
        if (self.status == "pass") == bool(self.issues):
            raise ValueError("pass 不得携带问题，fail/uncertain 必须说明问题")
        return self


class Verifier:
    """使用真实文件检查和独立提示词复核最终交付。"""

    def __init__(
        self, llm: LlmClient | None = None, *, semantic_enabled: bool = True,
        cancel_check: Callable[[], bool] | None = None, trace: RunTrace | None = None,
    ) -> None:
        """配置复核器；关闭语义检查仍保留文件检查，但不宣称完整通过。"""
        self.llm = llm
        self.semantic_enabled = semantic_enabled
        self.cancel_check = cancel_check
        self.trace = trace

    def verify(self, state: AgentState, plan: AnalysisPlan | None, answer: str) -> Verification:
        """核验候选交付，模型或文件读取异常降级，用户取消立即传播。

        Args:
            state: 原始用户目标与实际工具观察所在的运行状态。
            plan: 当前计划，直接回答时为空。
            answer: 尚未交付、尚未写入消息历史的候选回答。

        Returns:
            可公开且可持久化的结构化核验结果。
        """
        raise_if_cancelled(self.cancel_check)
        span = self.trace.begin("verify", "verify_delivery") if self.trace else None
        try:
            result = self._verify(state, plan, answer)
        except TaskCancelledError:
            if self.trace:
                self.trace.end(span, status="cancelled", error_type="TaskCancelledError")
            raise
        except Exception as exc:
            LOGGER.warning("独立验证降级：%s", type(exc).__name__)
            result = Verification(status="uncertain", issues=[VerificationIssue(
                code="verifier_unavailable", message="独立验证未完成。",
                evidence=f"验证器异常类型：{type(exc).__name__}",
            )])
        if self.trace:
            self.trace.end(span, status="error" if result.status != "pass" else "ok",
                           error_type=None if result.status == "pass" else f"Verification_{result.status}")
        return result

    def _verify(self, state: AgentState, plan: AnalysisPlan | None, answer: str) -> Verification:
        """先拦截可确定的文件错误，再进行一次语义复核。"""
        checked, issues = _check_artifacts(state, answer, self.cancel_check)
        if issues:
            status = "uncertain" if all(issue.code == "artifact_check_limit" for issue in issues) else "fail"
            return Verification(status=status, issues=issues, checked_artifacts=checked)
        if not self.semantic_enabled or self.llm is None:
            return Verification(status="uncertain", checked_artifacts=checked, issues=[VerificationIssue(
                code="semantic_disabled", message="仅完成产物检查，未进行独立语义复核。",
                evidence="语义验证已关闭或未配置模型。",
            )])
        context = {
            "user_query": clip_text(state.user_query, 8000),
            "memory_summary": clip_text(state.memory_summary or "", 4000),
            "recent_messages": [{"role": item.role, "content": clip_text(item.content, 2000)}
                                for item in state.messages[-6:]],
            "plan": plan.model_dump(mode="json") if plan else None,
            "observations": _project_observations(state.recent_observations or state.observations),
            "artifacts": [item.model_dump(mode="json") for item in state.available_artifacts[-40:]],
            "checked_artifacts": checked,
        }
        messages = [
            ("system", PROMPT_PATH.read_text(encoding="utf-8")),
            ("user", json.dumps({"evidence_context": clip_text(json.dumps(context, ensure_ascii=False), MAX_CONTEXT_CHARS),
                                  "candidate_answer": clip_text(answer, MAX_ANSWER_CHARS)}, ensure_ascii=False)),
        ]
        _, raw = invoke_llm_with_public_progress(
            self.llm, messages, summary_key="summary", stream_id="verifier",
            initial_text="正在独立核验交付…", progress_callback=None, cancel_check=self.cancel_check,
        )
        text = re.sub(r"^\s*<think>.*?</think>\s*", "", raw, count=1, flags=re.S | re.I)
        text = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip())
        result = Verification.model_validate_json(text)
        if result.status == "pass" and len(answer) > MAX_ANSWER_CHARS:
            result = Verification(status="uncertain", issues=[VerificationIssue(
                code="answer_truncated", message="候选回答超过独立验证的文本预算，未完整复核。",
                evidence=f"回答共 {len(answer)} 字符，复核预算为 {MAX_ANSWER_CHARS} 字符。",
            )])
        # 模型不得自行声称读取了文件；文件检查清单只取自确定性检查。
        return result.model_copy(update={"checked_artifacts": checked})


def _check_artifacts(
    state: AgentState, answer: str, cancel_check: Callable[[], bool] | None = None,
) -> tuple[list[str], list[VerificationIssue]]:
    """仅检查交付中引用的产物；不访问外部链接或授权目录之外的文件。"""
    if not state.output_dir:
        return [], []
    root = Path(state.output_dir).resolve()
    # 普通文字提及可能是在说明文件缺失，不能据此判成交付承诺；确定性层只处理下载链接。
    references: set[str] = set()
    for wrapped, bare in re.findall(r'\]\((?:<([^>]+)>|([^\s)]+))(?:\s+["\'][^)]*["\'])?\)', answer):
        target = wrapped or bare
        local = Path(unquote(target))
        if local.is_absolute() and root in local.resolve().parents:
            references.add(local.resolve().relative_to(root).as_posix())
            continue
        parsed = urlsplit(target)
        path = unquote(parsed.path).replace("\\", "/")
        if not path or parsed.scheme or parsed.netloc:
            continue
        api_match = re.match(r"^/api/agent/(?:discussions|runs)/[^/]+/(?:files|previews)/(.*)$", path)
        if api_match:
            references.add(api_match.group(1))
        elif path.startswith("/artifacts/"):
            # 下载地址可能包含会话前缀，只在已有引用中匹配；不猜测外部目录。
            known = next((item.relative_path for item in state.available_artifacts
                          if path.endswith("/" + item.relative_path)), None)
            references.add(known or path.removeprefix("/artifacts/"))
        elif not path.startswith(("/", "#")):
            references.add(path.removeprefix("artifacts/"))
    checked: list[str] = []
    issues: list[VerificationIssue] = []
    if len(references) > 100:
        issues.append(VerificationIssue(code="artifact_check_limit", message="引用产物超过单次检查上限，部分文件未检查。",
                                        evidence=f"共 {len(references)} 个引用，只检查前 100 个。"))
    for relative in sorted(references)[:100]:
        raise_if_cancelled(cancel_check)
        target = (root / relative).resolve()
        message = None
        if (target == root or root not in target.parents or ".." in Path(relative).parts
                or relative.split("/")[0].casefold() in {"input", ".runs", ".runtime"}):
            message = "交付引用越过允许的产物目录。"
        else:
            try:
                checked.append(relative)
                if not target.is_file():
                    message = "引用的产物文件不存在。"
                elif target.stat().st_size == 0:
                    message = "引用的产物文件为空。"
                elif target.suffix.lower() == ".json" and target.stat().st_size <= 2_000_000:
                    # 两兆以内的 JSON 做语法检查，避免对大文件进行无界读取；不执行代码。
                    json.loads(target.read_text(encoding="utf-8-sig"))
                elif target.suffix.lower() == ".py" and target.stat().st_size <= 2_000_000:
                    compile(target.read_bytes(), str(target), "exec")
            except (OSError, UnicodeError, ValueError, SyntaxError):
                message = "引用的产物无法读取或 JSON / Python 语法无效。"
        if message:
            issues.append(VerificationIssue(code="artifact_invalid", message=message, evidence=relative[:1600]))
        if len(issues) == 20:
            break
    return checked, issues
