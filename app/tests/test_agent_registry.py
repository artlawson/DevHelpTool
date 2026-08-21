import inspect

from app.agent.registry import TOOL_REGISTRY
from app.agent.schemas import TOOL_SCHEMAS

_PARAMETERIZED_TOOLS = {"jira_get_persons_open_issues", "jira_draft_comment"}


def test_tool_schemas_have_empty_properties():
    assert len(TOOL_SCHEMAS) == 9
    zero_arg_schemas = [
        s for s in TOOL_SCHEMAS if s["name"] not in _PARAMETERIZED_TOOLS
    ]
    assert len(zero_arg_schemas) == 7
    for schema in zero_arg_schemas:
        assert schema["input_schema"] == {"type": "object", "properties": {}}


def test_parameterized_tool_schemas_declare_properties():
    parameterized_schemas = {
        s["name"]: s for s in TOOL_SCHEMAS if s["name"] in _PARAMETERIZED_TOOLS
    }
    assert set(parameterized_schemas) == _PARAMETERIZED_TOOLS
    for schema in parameterized_schemas.values():
        properties = schema["input_schema"]["properties"]
        assert properties, f"{schema['name']} should declare at least one property"
        assert set(schema["input_schema"]["required"]) == set(properties)


def test_registry_keys_match_schema_names():
    schema_names = {schema["name"] for schema in TOOL_SCHEMAS}
    assert set(TOOL_REGISTRY) == schema_names


def test_registry_function_signatures_match_schema_properties():
    # Runtime replacement for the mypy arity/param-name checking lost when
    # TOOL_REGISTRY's value type was loosened to Callable[..., ...] to
    # accommodate mixed-arity tools (see registry.py's comment).
    schemas_by_name = {schema["name"]: schema for schema in TOOL_SCHEMAS}
    for name, fn in TOOL_REGISTRY.items():
        assert inspect.iscoroutinefunction(fn), f"{name} must be an async function"
        sig = inspect.signature(fn)
        expected_params = set(schemas_by_name[name]["input_schema"]["properties"])
        assert set(sig.parameters) == expected_params, (
            f"{name}'s parameters {set(sig.parameters)} don't match its "
            f"schema's declared properties {expected_params}"
        )
