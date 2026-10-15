"""Group dependency selection must never bring outside requests into a run."""

import copy

import pytest

from app.service.errors import BusinessError
from app.service.task_groups import group_configuration


def step(sid, parameter=None, assertion=None):
    return {
        "id": sid,
        "position": sid,
        "bindings": []
        if parameter is None
        else [
            {
                "location": "header",
                "name": "X-Value",
                "binding": {"kind": "literal", "value": "{" + parameter + "}"},
            }
        ],
        "assertion_code": assertion,
        "interface": {"method": "GET", "path": "/ok", "parameters": [], "request_body": None},
        "document": {},
    }


def parameter(name, sid):
    return {
        "id": sid,
        "name": name,
        "position": sid,
        "source_type": "rest",
        "value_type": "string",
        "rest_source": {"step_id": sid, "extract_expression": "data.value"},
    }


def config():
    return {
        "base_url": "http://localhost",
        "steps": [step(1), step(2, "a"), step(3, "b"), step(4)],
        "global_parameters": [parameter("a", 1), parameter("b", 2), parameter("unused", 4)],
    }


def group(ids):
    return {"id": 1, "name": "group", "step_ids": ids}


def test_group_transitive_dependencies_and_snapshot_independence():
    original = config()
    unchanged = copy.deepcopy(original)
    selected = group_configuration(original, group([1, 2, 3]))
    assert [p["name"] for p in selected["global_parameters"]] == ["a", "b"]
    assert [s["id"] for s in selected["steps"]] == [1, 2, 3]
    assert original == unchanged
    selected["steps"][0]["bindings"].append({})
    assert original == unchanged


@pytest.mark.parametrize("ids", [[], [2, 3], [2, 1, 3], [1, 3], [1, 1], [999]])
def test_group_rejects_missing_or_out_of_order_dependencies(ids):
    with pytest.raises(BusinessError) as exc:
        group_configuration(config(), group(ids))
    assert exc.value.status == 400


def test_group_assertion_only_dependency_is_kept():
    original = config()
    original["steps"][1] = step(
        2,
        assertion='def assert_response(responseBody, globalParam):\n    return globalParam.get("a") == "x"',
    )
    selected = group_configuration(original, group([1, 2]))
    assert [p["name"] for p in selected["global_parameters"]] == ["a"]
    with pytest.raises(BusinessError):
        group_configuration(original, group([2]))
    with pytest.raises(BusinessError):
        group_configuration(original, group([2, 1]))


def test_group_dynamic_assertion_dictionary_requires_all_sources():
    original = config()
    original["steps"][2] = step(
        3,
        assertion="def assert_response(responseBody, globalParam):\n    return bool(dict(globalParam))",
    )
    with pytest.raises(BusinessError):
        group_configuration(original, group([1, 2, 3]))
    selected = group_configuration(original, group([4, 1, 2, 3]))
    assert [p["name"] for p in selected["global_parameters"]] == ["a", "b", "unused"]
    assert [s["position"] for s in selected["steps"]] == [1, 2, 3, 4]


def test_group_assertion_cannot_use_current_step_response_parameter():
    original = config()
    original["steps"][0]["assertion_code"] = (
        'def assert_response(responseBody, globalParam):\n    return globalParam["a"] == "x"'
    )
    with pytest.raises(BusinessError):
        group_configuration(original, group([1]))
