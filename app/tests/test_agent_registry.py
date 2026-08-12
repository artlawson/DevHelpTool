import inspect

from app.agent.registry import TOOL_REGISTRY
from app.agent.schemas import TOOL_SCHEMAS


def test_tool_schemas_have_empty_properties():
    assert len(TOOL_SCHEMAS) == 4
    for schema in TOOL_SCHEMAS:
        assert schema["input_schema"] == {"type": "object", "properties": {}}


def test_registry_keys_match_schema_names():
    schema_names = {schema["name"] for schema in TOOL_SCHEMAS}
    assert set(TOOL_REGISTRY) == schema_names


def test_registry_values_are_zero_arg_async_callables():
    for name, fn in TOOL_REGISTRY.items():
        assert inspect.iscoroutinefunction(fn), f"{name} must be an async function"
        sig = inspect.signature(fn)
        assert len(sig.parameters) == 0, f"{name} must take zero arguments"
