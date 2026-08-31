from scr.agents.model_builder import ModelBuilder
from scr.schemas.question import CurrentQuestionContext, ProblemInterpretation


def test_model_builder_attaches_generic_formulation_ir():
    """IR 只记录建模产物字段，不套用题型模板；缺省变量给出通用占位。"""
    builder = ModelBuilder()
    context = CurrentQuestionContext(
        question_id="q1",
        question_text="给出最优方案",
        objective="优化资源配置",
    )
    interpretation = ProblemInterpretation(
        question_id="q1",
        math_task="composite",
        math_task_description="优化问题",
        objective_function="max Z",
        result_form="最优方案",
    )

    formulation = builder._build_formulation(
        "LLM 问题驱动建模",
        "composite",
        interpretation,
        context,
        "",
    )

    assert formulation["ir"]["variables"]
    assert formulation["ir"]["objective"] == "max Z"
    assert formulation["ir"]["required_outputs"] == ["model_output"]
    assert formulation["ir"]["validation_requirements"] == ["result_validation"]
    assert formulation["method_key"] == ""


def test_model_builder_does_not_invent_task_templates():
    """不同 math_task 不再触发不同的预设公式模板，表述字段保持中性。"""
    builder = ModelBuilder()
    context = CurrentQuestionContext(question_id="q1", question_text="题面")

    for task in ("evaluation", "prediction", "optimization", "composite"):
        interpretation = ProblemInterpretation(
            question_id="q1",
            math_task=task,
            math_task_description="描述",
        )
        formulation = builder._build_formulation(
            "LLM 问题驱动建模", task, interpretation, context, ""
        )
        assert formulation["description"] == ""
        assert formulation["objective_function"] == ""
        assert formulation["required_outputs"] == ["model_output"]
