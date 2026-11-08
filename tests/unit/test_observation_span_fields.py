"""观察模型的追踪字段必须可选且严格校验。"""

import pytest
from pydantic import ValidationError

from data_analysis.agent.state import Observation


def test_old_observation_deserializes_with_unknown_usage():
    observation = Observation.model_validate({"tool_name": "read", "success": True})
    assert observation.span_id is None
    assert observation.input_tokens is None
    assert observation.output_tokens is None
    assert observation.cost_estimate is None


@pytest.mark.parametrize("fields", [
    {"input_tokens": -1}, {"input_tokens": True}, {"output_tokens": "4"},
    {"cost_estimate": -0.1}, {"cost_estimate": float("nan")},
    {"cost_estimate": float("inf")}, {"span_id": ""}, {"unexpected": 1},
])
def test_observation_rejects_invalid_trace_fields(fields):
    with pytest.raises(ValidationError):
        Observation(tool_name="read", success=True, **fields)
