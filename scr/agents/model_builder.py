"""将建模决策转为可执行计算和可复现产物。

负责构建模型表述、准备数据、调用大模型生成求解代码、执行计算，
并返回数值结果、表格、图表和复现所需的代码/数据文件。
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import numpy as np

from ..schemas.context import DataProfile
from ..schemas.formulation import ConstraintIR, FormulationIR, VariableIR
from ..schemas.question import CurrentQuestionContext, ProblemInterpretation

class ModelBuilder:
    """建模计算与可视化 Agent。

    Args:
        llm: 可选的 LLM 客户端（Phase 4 暂不使用，Phase 5+ 可用于模型精调）。
        budget_manager: 可选的预算管理器，供运行监控使用。
    """

    def __init__(self, llm: Any | None = None, budget_manager: Any | None = None) -> None:
        self._llm = llm
        self._budget_manager = budget_manager

    def build(
        self,
        context: CurrentQuestionContext,
        interpretation: ProblemInterpretation,
        decision_record: dict,
        data_profile: DataProfile | None = None,
        output_dir: str | Path | None = None,
        feedback: str = "",
    ) -> dict:
        """执行完整的建模计算流程。

        Args:
            context: 当前小问上下文。
            interpretation: 问题澄清。
            decision_record: 方法决策记录（Phase 3 产出）。
            data_profile: 数据画像。
            output_dir: 产物目录（LLM 生成的解题代码将保存到其 questions/<qid>/ 下）。
            feedback: 自评/前次尝试的改进建议（作为代码生成的初始反馈）。

        Returns:
            包含 formulation, data_preparation, computation, figures, tables 的字典。
        """
        selected_method = decision_record.get("selected_method", "未知方法")
        method_key = decision_record.get("canonical_method", "")
        math_task = interpretation.math_task

        # 从决策记录中获取 required_outputs 和 validation_requirements
        # （由联网搜索+LLM生成，不再依赖预设方法目录）
        required_outputs = decision_record.get("required_outputs", [])
        validation_requirements = decision_record.get("validation_requirements", [])

        # 步骤 1: 构建模型表述
        formulation = self._build_formulation(
            selected_method, math_task, interpretation, context, method_key
        )
        # 注入决策记录中的输出和验证要求
        if required_outputs:
            formulation["required_outputs"] = required_outputs
        if validation_requirements:
            formulation["validation_requirements"] = validation_requirements

        # 步骤 2: 准备数据
        data_preparation = self._prepare_data(
            selected_method, data_profile, context
        )
        modeling_context = self._build_modeling_context(
            context, interpretation, decision_record
        )

        # 步骤 3: 执行计算
        computation = self._execute(
            selected_method, math_task, data_preparation, formulation, context,
            method_key, output_dir, feedback, modeling_context=modeling_context,
        )

        # 步骤 4: 生成输出
        tables = self._generate_tables(
            selected_method, math_task, computation, data_preparation, context
        )
        figures = computation.get("figures") or self._generate_figure_descriptions(
            selected_method, math_task, computation, context
        )

        print(f"[builder] 建模完成: {selected_method} "
              f"(computation_status={computation.get('status', 'unknown')})")

        return {
            "formulation": formulation,
            "data_preparation": data_preparation,
            "computation": computation,
            "figures": figures,
            "tables": tables,
        }

    @staticmethod
    def _build_modeling_context(
        context: CurrentQuestionContext,
        interpretation: ProblemInterpretation,
        decision_record: dict,
    ) -> str:
        """汇总 LLM 建模所需的问题上下文和方法参考材料。"""
        payload = {
            "question_id": context.question_id,
            "question_text": context.question_text[:2000],
            "objective": context.objective,
            "global_background": context.global_background[:1800],
            "global_constraints": context.global_constraints,
            "required_data": context.required_data,
            "data_quality_summary": context.data_quality_summary,
            "inherited_summaries": context.inherited_summaries[:3],
            "problem_interpretation": {
                "math_task_description": interpretation.math_task_description,
                "decision_variables": interpretation.decision_variables,
                "objective_function": interpretation.objective_function,
                "constraints": interpretation.constraints,
                "evaluation_metrics": interpretation.evaluation_metrics,
                "result_form": interpretation.result_form,
                "available_data": interpretation.available_data,
                "missing_data": interpretation.missing_data,
                "necessary_assumptions": interpretation.necessary_assumptions,
                "acceptable_simplifications": interpretation.acceptable_simplifications,
                "relation_to_previous": interpretation.relation_to_previous,
                "relation_description": interpretation.relation_description,
            },
            "modeling_instruction": "直接基于以上问题和证据建立本题专属模型，不预选候选方法。",
        }
        text = json.dumps(payload, ensure_ascii=False, default=str, indent=2)
        if len(text) > 6000:
            return text[:6000] + "\n...（建模上下文已截断）"
        return text

    # ------------------------------------------------------------------
    # 模型表述构建
    # ------------------------------------------------------------------

    def _build_formulation(
        self,
        method_name: str,
        math_task: str,
        interpretation: ProblemInterpretation,
        context: CurrentQuestionContext,
        method_key: str = "",
    ) -> dict:
        """构建数学模型表述（只记录建模产物字段，不套用题型模板）。"""
        formulation = {
            "method": method_name,
            "method_key": method_key,
            "math_task": math_task,
            "decision_variables": [],
            "objective_function": "",
            "constraints": [],
            "parameters": {},
            "description": "",
            "required_outputs": ["model_output"],
            "validation_requirements": ["result_validation"],
        }

        return self._attach_ir(formulation, method_name, math_task, interpretation, context, method_key)

    def _attach_ir(
        self,
        formulation: dict,
        method_name: str,
        math_task: str,
        interpretation: ProblemInterpretation,
        context: CurrentQuestionContext,
        method_key: str = "",
    ) -> dict:
        """Attach a generic modeling IR while preserving legacy fields.

        required_outputs 和 validation_requirements 直接从 formulation 或
        decision_record 传入（由联网搜索+LLM生成），不再依赖预设方法目录。
        """
        # 确保 required_outputs 和 validation_requirements 存在
        formulation.setdefault("required_outputs", [])
        formulation.setdefault("validation_requirements", [])
        formulation.setdefault("method_key", method_key)
        formulation.setdefault("canonical_method", method_key)

        variables = [
            self._variable_ir_from_text(v)
            for v in formulation.get("decision_variables", [])
        ]
        if not variables:
            variables = [VariableIR(symbol="z", meaning="model output")]

        constraints = [
            ConstraintIR(expression=str(c), meaning=str(c), role=self._constraint_role(str(c)))
            for c in formulation.get("constraints", [])
        ]

        objective = formulation.get("objective_function") or interpretation.objective_function
        ir = FormulationIR(
            question_id=context.question_id,
            math_task=math_task,
            method_key=method_key,
            method_name=method_name,
            sets=self._infer_sets(context, interpretation),
            indices=self._infer_indices(context, interpretation),
            parameters=formulation.get("parameters", {}),
            variables=variables,
            objective=objective,
            objective_sense=self._objective_sense(objective, math_task),
            constraints=constraints,
            assumptions=list(interpretation.necessary_assumptions),
            required_outputs=list(formulation.get("required_outputs", [])),
            validation_requirements=list(formulation.get("validation_requirements", [])),
        )
        formulation["ir"] = ir.model_dump()
        return formulation

    @staticmethod
    def _variable_ir_from_text(text: str) -> VariableIR:
        symbol, _, meaning = str(text).partition(":")
        symbol = symbol.strip() or str(text).strip()
        return VariableIR(symbol=symbol, meaning=meaning.strip(), domain="real")

    @staticmethod
    def _constraint_role(expression: str) -> str:
        text = expression.lower()
        if "≥ 0" in expression or "\\geq 0" in text or "nonnegative" in text:
            return "domain"
        if "sum" in text or "Σ" in expression or "容量" in expression or "capacity" in text:
            return "resource"
        if "概率" in expression or "probability" in text or "p(" in text:
            return "risk"
        return "generic"

    @staticmethod
    def _objective_sense(objective: str, math_task: str) -> str:
        text = (objective or "").lower()
        if "max" in text or "最大" in objective:
            return "max"
        if "min" in text or "最小" in objective:
            return "min"
        if math_task == "evaluation":
            return "score"
        if math_task == "prediction":
            return "estimate"
        if math_task == "simulation":
            return "simulate"
        return "none"

    @staticmethod
    def _infer_sets(
        context: CurrentQuestionContext,
        interpretation: ProblemInterpretation,
    ) -> list[str]:
        sets = []
        if context.required_data or interpretation.available_data:
            sets.append("data_records")
        if interpretation.decision_variables:
            sets.append("decision_options")
        if "年" in context.question_text or "time" in context.question_text.lower():
            sets.append("time_periods")
        return sets

    @staticmethod
    def _infer_indices(
        context: CurrentQuestionContext,
        interpretation: ProblemInterpretation,
    ) -> list[str]:
        indices = []
        if interpretation.decision_variables:
            indices.append("j")
        if context.required_data or interpretation.available_data:
            indices.append("i")
        if "年" in context.question_text or "time" in context.question_text.lower():
            indices.append("t")
        return indices

    # ------------------------------------------------------------------
    # 数据准备
    # ------------------------------------------------------------------

    def _prepare_data(
        self,
        method_name: str,
        data_profile: DataProfile | None,
        context: CurrentQuestionContext,
    ) -> dict:
        """准备模型所需数据。

        从 DataProfile 中查找完整文件路径（FileRecord.file_path），
        而非仅使用 source_file（文件名）。
        """
        prep = {
            "method": method_name,
            "data_available": data_profile is not None and len(data_profile.tables) > 0,
            "data_source": "",
            "n_samples": 0,
            "n_features": 0,
            "feature_names": [],
            "preprocessing": [],
            "data_matrix": None,  # 实际数据矩阵（如果有）
            "loaded_tables": [],  # 所有已加载表的摘要
        }

        if data_profile is None or not data_profile.tables:
            return prep

        # 构建 file_name → file_path 的查找映射
        file_path_map: dict[str, str] = {}
        for f in data_profile.files:
            if f.read_status == "success":
                file_path_map[f.file_name] = f.file_path

        # 尝试加载每张表的数据，优先选择有数值列的表
        best_df = None
        best_table = None

        for table in data_profile.tables:
            raw_name = table.source_file
            full_path = file_path_map.get(raw_name, "")

            try:
                import pandas as pd
                from pathlib import Path

                # 如果映射中没有，尝试用 source_file 直接作为路径
                if not full_path:
                    # source_file 可能本身就是路径（旧版本兼容）
                    p = Path(raw_name)
                    if p.exists():
                        full_path = raw_name
                    else:
                        prep["preprocessing"].append(
                            f"跳过表 {raw_name}: 无法解析完整路径"
                        )
                        continue

                # 读取数据
                ext = Path(full_path).suffix.lower()
                if ext == ".csv":
                    df = pd.read_csv(full_path)
                elif ext in (".xlsx", ".xls"):
                    df = pd.read_excel(
                        full_path,
                        sheet_name=table.sheet_name or 0,
                    )
                elif ext == ".mat":
                    from ..tools.file_tools import read_mat
                    var_name = table.sheet_name or None
                    df = read_mat(full_path, variable_name=var_name)
                elif ext in (".json", ".jsonl", ".ndjson"):
                    from ..tools.file_tools import read_json
                    df = read_json(full_path)
                else:
                    continue

                if df is None or len(df) == 0:
                    continue

                # 记录已加载表
                numeric_cols = df.select_dtypes(include=[np.number]).columns.tolist()
                prep["loaded_tables"].append({
                    "source": raw_name,
                    "sheet": table.sheet_name,
                    "n_rows": len(df),
                    "n_numeric_cols": len(numeric_cols),
                })

                # 选择数值列最多的表作为主数据
                if best_df is None or len(numeric_cols) > len(
                    best_df.select_dtypes(include=[np.number]).columns
                ):
                    best_df = df
                    best_table = table

            except Exception as e:
                prep["preprocessing"].append(
                    f"加载表 {raw_name} 失败: {str(e)[:80]}"
                )

        # 使用最佳表的数据
        if best_df is not None and best_table is not None:
            prep["data_source"] = best_table.source_file
            prep["n_samples"] = len(best_df)

            numeric_cols = best_df.select_dtypes(include=[np.number]).columns.tolist()
            if numeric_cols:
                data_matrix = best_df[numeric_cols].values
                # 清理 NaN 和 inf
                data_matrix = np.nan_to_num(data_matrix, nan=0.0, posinf=1e10, neginf=-1e10)
                prep["data_matrix"] = data_matrix.tolist()
                prep["feature_names"] = list(numeric_cols[:15])
                prep["n_features"] = len(numeric_cols)
                prep["preprocessing"].append(
                    f"从 {best_table.source_file} 选择 {len(numeric_cols)} 个数值列"
                )
            else:
                prep["preprocessing"].append("主表无数值列可用")
        else:
            prep["preprocessing"].append("未能加载任何数据表")

        return prep

    # ------------------------------------------------------------------
    # 计算执行
    # ------------------------------------------------------------------

    def _execute(
        self,
        method_name: str,
        math_task: str,
        data_prep: dict,
        formulation: dict,
        context: CurrentQuestionContext,
        method_key: str = "",
        output_dir: str | Path | None = None,
        feedback: str = "",
        modeling_context: str = "",
    ) -> dict:
        """执行模型计算。

        Args:
            method_name: 选中方法名称。
            math_task: 数学任务类型。
            data_prep: 数据准备结果。
            formulation: 模型表述。
            context: 当前小问上下文。
            method_key: 方法标识。
            output_dir: 产物目录（LLM 生成代码保存到 questions/<qid>/）。
            feedback: 自评/前次尝试的改进建议（作为代码生成的初始反馈）。
        """
        method_key = method_key or formulation.get("method_key", "")
        computation = {
            "method": method_name,
            "method_key": method_key,
            "status": "not_executed",
            "results": {},
            "metrics": {},
            "parameters_used": formulation.get("parameters", {}),
            "intermediate_values": {},
            "error": "",
        }

        data_matrix = data_prep.get("data_matrix")

        # LLM 根据完整问题上下文直接生成具体数学模型与求解代码。
        if self._llm is not None:
            try:
                code_computation = self._execute_code_based(
                    method_name, math_task, data_prep, formulation, context, output_dir,
                    feedback=feedback,
                    modeling_context=modeling_context,
                )
                if code_computation.get("status") == "success":
                    return code_computation
                print(
                    f"[builder] 任务驱动建模未成功（{code_computation.get('status')}），"
                    f"停止预设方法回退: {code_computation.get('error', '')[:120]}"
                )
                return code_computation
            except Exception as e:
                print(f"[builder] 任务驱动建模异常，停止预设方法回退: {e}")
                return {"status": "error", "error": str(e)[:200]}

        return {
            "status": "error",
            "error": "LLM 不可用，无法执行问题驱动建模",
        }

    # ------------------------------------------------------------------
    # 任务驱动建模：LLM 生成模型 + 代码沙箱执行
    # ------------------------------------------------------------------

    def _execute_code_based(
        self,
        method_name: str,
        math_task: str,
        data_prep: dict,
        formulation: dict,
        context: CurrentQuestionContext,
        output_dir: str | Path | None = None,
        feedback: str = "",
        modeling_context: str = "",
    ) -> dict:
        """任务驱动建模：分段调用 LLM（模型设计→代码生成），沙箱执行并校验。

        流程：准备数据 CSV → LLM 设计数学模型 → LLM 生成求解代码
              （各 10 分钟超时）→ 沙箱执行 → 按题型校验 → 失败反馈修复重试。

        超时策略（超时即回退）：
          - 模型设计 / 代码生成 **超时** → 立即回退预设方法（不再重试，
            因为重试大概率仍超时，避免 180s 白等后再次 180s）
          - 其他失败（解析 / 执行 / 校验）→ 反馈给 LLM 修复重试（≤2 次）

        Args:
            method_name: 选中方法名称。
            math_task: 数学任务类型。
            data_prep: 数据准备结果。
            formulation: 模型表述。
            context: 当前小问上下文。
            output_dir: 产物目录（解题代码/数据/结果保存到 questions/<qid>/）。
            feedback: 自评/前次尝试的改进建议（作为首轮代码生成的初始反馈）。
        """
        from ..agents.code_modeler import (
            CodeModeler,
            CodeModelingError,
            LLMTimeoutError,
        )
        from ..tools.code_executor import CodeExecutionError, execute_model_code

        import time

        csv_path = self._write_data_csv(data_prep)
        # 无数据文件时不 bail：让 LLM 生成自包含代码（题面参数直接写入代码常量）。
        # 仅当确实需要外部数据却拿不到时才视为失败（此处交由 LLM 判断）。
        data_summary = self._build_data_summary(data_prep)[:800]
        if csv_path is None:
            data_summary = (
                (data_summary + "\n" if data_summary else "")
                + "【重要】本题无外部数据文件。请不要读取 CSV/Excel，"
                "将任务给出的参数（如速度、距离、时间、坐标等）直接作为常量写入代码，"
                "生成完全自包含的求解代码。"
            )
            print("[builder] 无数据文件，要求 LLM 生成自包含代码（参数写入代码）")

        # 提示词瘦身（C）：问题文本 1000 字符，减少生成 token
        question_text = context.question_text[:1000]

        modeler = CodeModeler(self._llm)
        feedback = feedback or ""
        last_error = ""
        t_start = time.time()
        code = ""  # 失败归档用：模型设计阶段失败时可能尚未生成代码
        print(
            "[builder] 问题驱动建模开始"
            "（由 LLM 根据题面、数据和约束推理，模型设计/代码生成超时均为 10 分钟）"
        )

        try:
            model_json = modeler.generate_model(
                question_text=question_text,
                math_task=math_task,
                method_hint="",
                data_summary=data_summary,
                modeling_context=modeling_context,
                feedback=feedback,
            )

            code = modeler.generate_code(
                model_json,
                question_text=question_text,
                data_summary=data_summary,
                feedback=feedback,
            )

            t_exec = time.time()
            print(
                f"[builder]   ↳ 模型代码就绪（{model_json.get('model_name', '?')}，"
                f"{len(code)} 字符），开始沙箱执行..."
            )
            figure_output_dir = Path(output_dir) / "figures" if output_dir else None
            result = execute_model_code(
                code,
                data_csv_path=csv_path,
                figure_output_dir=figure_output_dir,
                figure_prefix=context.question_id,
                require_figures=figure_output_dir is not None,
            )
            print(f"[builder]   ↳ 代码执行成功（耗时 {time.time() - t_exec:.1f}s）")

            self._validate_task_results(result)
            print(f"[builder] 问题驱动建模成功（总耗时 {time.time() - t_start:.1f}s）")
            artifacts_dir = self._persist_question_artifacts(
                question_id=context.question_id,
                output_dir=output_dir,
                code=code,
                data_csv_path=csv_path,
                results=result,
                method_name=method_name,
                model_name=model_json.get("model_name", method_name),
            )
            return {
                "status": "success",
                "method": method_name,
                "method_key": "code_based",
                "model_name": model_json.get("model_name", method_name),
                "model_summary": model_json.get("model_summary", ""),
                "results": result,
                "metrics": result.get("metrics", {}) if isinstance(result, dict) else {},
                "figures": result.get("figures", []) if isinstance(result, dict) else [],
                "parameters_used": model_json.get("key_parameters", {}),
                "intermediate_values": {
                    "generation_attempts": 1,
                    "solution_code_snippet": code[:200],
                    "variables": model_json.get("variables", []),
                    "objective": model_json.get("objective", ""),
                    "constraints": model_json.get("constraints", []),
                },
                "artifacts_dir": str(artifacts_dir) if artifacts_dir else "",
                "error": "",
            }
        except LLMTimeoutError as exc:
            print(f"[builder] 问题驱动建模超时: {str(exc)[:150]}")
            self._persist_failed_attempt(context, output_dir, code, csv_path, exc, method_name)
            return {"status": "error", "error": str(exc)}
        except (CodeModelingError, CodeExecutionError, ValueError) as exc:
            repair_feedback = (
                f"上一轮模型或代码执行失败：{str(exc)[:800]}。"
                "请针对该错误重新设计并生成可执行代码。"
            )
            if self._consume_code_repair_budget(context.question_id):
                print("[builder] 消耗一次代码修复预算，使用失败反馈重新生成模型和代码")
                return self._execute_code_based(
                    method_name,
                    math_task,
                    data_prep,
                    formulation,
                    context,
                    output_dir,
                    feedback=f"{feedback}\n{repair_feedback}".strip(),
                    modeling_context=modeling_context,
                )
            print(f"[builder] 问题驱动建模失败: {str(exc)[:150]}")
            self._persist_failed_attempt(context, output_dir, code, csv_path, exc, method_name)
            return {"status": "error", "error": str(exc)}
        finally:
            if csv_path and os.path.exists(csv_path):
                try:
                    os.unlink(csv_path)
                except OSError:
                    pass

    def _consume_code_repair_budget(self, question_id: str) -> bool:
        """在可用时消耗当前子任务的一次代码修复预算。"""
        if self._budget_manager is None:
            return False
        from ..runtime.budget import BudgetType

        return self._budget_manager.consume(
            BudgetType.CODE_REPAIR,
            question_id=question_id,
        )

    def _persist_failed_attempt(
        self,
        context: CurrentQuestionContext,
        output_dir: str | Path | None,
        code: str,
        data_csv_path: str | Path | None,
        exc: Exception,
        method_name: str,
    ) -> None:
        """归档失败的求解尝试（代码 + 错误），便于 blocked 后追溯根因。

        仅在最终失败（不再重试）时调用；模型设计阶段失败时 code 为空，跳过。
        归档失败不得掩盖原始错误。
        """
        if not code or not output_dir or not context.question_id:
            return
        try:
            q_dir = self._persist_question_artifacts(
                question_id=context.question_id,
                output_dir=output_dir,
                code=code,
                data_csv_path=data_csv_path,
                results={"error": str(exc)},
                method_name=method_name,
                model_name="failed_attempt",
                status="error",
            )
            if q_dir:
                print(f"[builder] 失败尝试已归档: {q_dir}")
        except Exception as persist_err:  # noqa: BLE001 - 归档失败不影响原始错误返回
            print(f"[builder] 失败尝试归档失败（不影响错误返回）: {persist_err}")

    def _persist_question_artifacts(
        self,
        question_id: str,
        output_dir: str | Path | None,
        code: str,
        data_csv_path: str | Path | None,
        results: dict,
        method_name: str,
        model_name: str,
        status: str = "success",
    ) -> Path | None:
        """将小问的建模解题产物保存到 <output_dir>/questions/<qid>/。

        保存内容（architecture.md §6.3 每问代码/数据/结果包）：
          - solution.py：LLM 生成的完整求解代码
          - data.csv：传入沙箱执行的输入数据
          - result.json：执行结果（results + 方法信息）

        Args:
            question_id: 小问 ID。
            output_dir: 产物根目录（artifacts/<run_id>）。
            code: LLM 生成的完整求解代码。
            data_csv_path: 沙箱执行用的数据 CSV（临时文件，保存为副本）。
            results: 沙箱执行返回的结果字典。
            method_name: 选中方法名称。
            model_name: 生成的模型名称。
            status: 执行状态（success/error），写入 result.json。

        Returns:
            保存目录 Path；output_dir 为空时不保存并返回 None。
        """
        if not output_dir or not question_id:
            return None

        q_dir = Path(output_dir) / "questions" / question_id
        q_dir.mkdir(parents=True, exist_ok=True)

        # 完整求解代码
        (q_dir / "solution.py").write_text(code, encoding="utf-8")

        # 输入数据（保存副本，避免随临时文件删除）
        if data_csv_path and Path(data_csv_path).exists():
            shutil.copy2(data_csv_path, q_dir / "data.csv")

        # 执行结果
        (q_dir / "result.json").write_text(
            json.dumps(
                {
                    "status": status,
                    "method": method_name,
                    "model_name": model_name,
                    "results": results,
                    "metrics": results.get("metrics", {}) if isinstance(results, dict) else {},
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )

        print(f"[builder] 解题代码/数据/结果已保存: {q_dir}")
        return q_dir

    def _write_data_csv(self, data_prep: dict) -> str | None:
        """将数据矩阵（含表头）写入临时 CSV，供生成代码读取。"""
        import tempfile

        matrix = data_prep.get("data_matrix")
        if not matrix:
            return None

        names = list(data_prep.get("feature_names") or [])
        n_cols = len(matrix[0]) if matrix else 0
        if len(names) < n_cols:
            names = names + [f"col_{i}" for i in range(len(names), n_cols)]
        names = names[:n_cols]

        import pandas as pd

        fd, path = tempfile.mkstemp(suffix=".csv", prefix="mma_data_")
        os.close(fd)
        try:
            pd.DataFrame(matrix, columns=names).to_csv(path, index=False)
        except Exception:
            if os.path.exists(path):
                os.unlink(path)
            return None
        return path

    def _build_data_summary(self, data_prep: dict) -> str:
        """构建数据结构摘要（供 LLM 建模参考）。"""
        lines = []
        lines.append(f"- 样本量: {data_prep.get('n_samples', 0)} 行")
        lines.append(f"- 特征列: {data_prep.get('feature_names', [])}")
        tables = data_prep.get("loaded_tables", [])
        if tables:
            lines.append(f"- 已加载表: {tables[:3]}")
        for p in data_prep.get("preprocessing", [])[:5]:
            lines.append(f"- 预处理: {p}")
        return "\n".join(lines)

    def _validate_task_results(self, result: dict) -> None:
        """验证 LLM 求解代码返回了通用、可用的结果字典。"""
        from ..tools.result_keys import normalize_result_dict

        # 归一化键名：兼容 LLM 提示词契约（solution/objective/r2）与预设方法契约
        result = normalize_result_dict(result)

        if not isinstance(result, dict):
            raise ValueError("结果不是 dict")
        if not result:
            raise ValueError("结果为空")

        metrics = result.get("metrics")
        if metrics is not None and not isinstance(metrics, dict):
            raise ValueError("metrics 必须为 dict")

    # ------------------------------------------------------------------
    # 结果表格生成
    # ------------------------------------------------------------------

    def _generate_tables(
        self,
        method_name: str,
        math_task: str,
        computation: dict,
        data_prep: dict,
        context: CurrentQuestionContext,
    ) -> list[str]:
        """生成结果表格描述。"""
        tables: list[str] = []
        status = computation.get("status", "unknown")
        results = computation.get("results", {})

        if status == "success":
            tables.append(f"表: {method_name} 计算结果")
        elif status == "no_data":
            tables.append(f"表: {method_name} 结果（无数据，待填充）")
        else:
            tables.append(f"表: {method_name} 结果（{status}）")

        return tables

    # ------------------------------------------------------------------
    # 图表描述生成
    # ------------------------------------------------------------------

    def _generate_figure_descriptions(
        self,
        method_name: str,
        math_task: str,
        computation: dict,
        context: CurrentQuestionContext,
    ) -> list[str]:
        """生成图表描述（非实际图片）。"""
        figures: list[str] = []
        status = computation.get("status", "unknown")

        if status == "success":
            figures.append(f"图: {method_name} 结果可视化")

        return figures
