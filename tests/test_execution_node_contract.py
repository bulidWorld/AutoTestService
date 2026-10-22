"""Validate the new public node shapes without requiring the unfinished executor."""

import copy

import pytest
from jsonschema import RefResolver, ValidationError
from openapi_schema_validator import OAS30Validator

from app.api.contract import load_contract
from app.config.settings import ROOT


@pytest.fixture(scope="module")
def contract():
    return load_contract(ROOT / "docs/ rest-api/rest-task.yaml")


def validate(contract, name, value):
    validator = OAS30Validator(
        contract["components"]["schemas"][name], resolver=RefResolver.from_schema(contract)
    )
    validator.validate(value)


@pytest.mark.parametrize(
    "name",
    ["RestStepInput", "WaitStepInput", "ParallelStepInput", "RestStep", "WaitStep", "ParallelStep"],
)
def test_node_examples_match_exactly_one_variant(contract, name):
    schema = contract["components"]["schemas"][name]
    validate(contract, "StepInput" if name.endswith("Input") else "Step", schema["example"])


@pytest.mark.parametrize(
    "value",
    [
        {"interface_id": 1, "file_id": 1, "version": 1, "bindings": []},
        {"step_type": "wait", "execution_scope": "main", "position": 1},
        {"step_type": "wait", "execution_scope": "main", "position": 0, "wait_milliseconds": 1},
        {"step_type": "wait", "execution_scope": "main", "position": 1, "wait_milliseconds": 2592000001},
        {"step_type": "wait", "execution_scope": "main", "position": 1, "wait_milliseconds": 1,
         "raw_body": {"text": ""}},
        {"step_type": "parallel", "execution_scope": "main", "position": 1},
        {"step_type": "parallel", "execution_scope": "finally", "position": 1, "max_concurrency": 2},
        {"step_type": "parallel", "execution_scope": "main", "position": 1, "max_concurrency": 9},
        {"step_type": "parallel", "execution_scope": "main", "position": 1, "max_concurrency": 1,
         "headers": []},
    ],
)
def test_old_or_invalid_nodes_are_rejected(contract, value):
    with pytest.raises(ValidationError):
        validate(contract, "StepInput", value)


def test_headers_and_raw_text_have_separate_shapes(contract):
    node = copy.deepcopy(contract["components"]["schemas"]["RestStepInput"]["example"])
    node.update(
        headers=[{"name": "X-Client", "binding": {"kind": "global", "parameter_name": "client"}}],
        raw_body={"text": ' \n{"name":中文 {token} '},
    )
    validate(contract, "StepInput", node)
    for invalid in (
        {"bindings": [{"location": "header", "name": "X-Client",
                       "binding": {"kind": "literal", "value": "demo"}}]},
        {"headers": [{"name": "X-Client", "location": "header",
                      "binding": {"kind": "literal", "value": "demo"}}]},
        {"raw_body": {"text": "", "extra": "invalid"}},
    ):
        with pytest.raises(ValidationError):
            validate(contract, "StepInput", node | invalid)
    

@pytest.mark.parametrize("kind", ["Rest", "Wait", "Parallel"])
def test_read_nodes_never_return_temporary_parent_indices(contract, kind):
    value = copy.deepcopy(contract["components"]["schemas"][kind + "Step"]["example"])
    value["parent_step_index"] = 0
    with pytest.raises(ValidationError):
        validate(contract, "Step", value)


@pytest.mark.parametrize("kind", ["Rest", "Wait", "Parallel"])
def test_visible_run_node_examples(contract, kind):
    schema = contract["components"]["schemas"][kind + "RunStep"]
    validate(contract, "RunStep", schema["example"])


def test_empty_parallel_stats_and_published_parameter(contract):
    stats = contract["components"]["schemas"]["ParallelStats"]["example"]
    validate(contract, "ParallelStats", stats)
    assert stats["response_count"] == stats["response_time_sum_ms"] == 0
    assert all(stats[key] is None for key in (
        "avg_response_time_ms", "min_response_time_ms", "max_response_time_ms"
    ))
    parameter = contract["components"]["schemas"]["rest_parameter_RunParameter"]
    validate(contract, "rest_parameter_RunParameter", parameter["example"])
    assert "published" in parameter["required"]


def test_runtime_null_is_allowed_without_allowing_null_fixed_inputs(contract):
    parameter = copy.deepcopy(
        contract["components"]["schemas"]["rest_parameter_RunParameter"]["example"]
    )
    parameter.update(value=None, published=True, status="succeeded", redacted=False)
    validate(contract, "rest_parameter_RunParameter", parameter)
    parameter_contract = load_contract(ROOT / "docs/ rest-api/rest-parameter.yaml")
    validate(parameter_contract, "PreviewParameterResult", {
        "parameter_name": "missing", "value_type": "integer", "redacted": False, "value": None,
    })
    with pytest.raises(ValidationError):
        validate(parameter_contract, "Value", None)
