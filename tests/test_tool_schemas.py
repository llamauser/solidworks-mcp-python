"""Guards the 'small model friendly' rules for every registered tool."""

from __future__ import annotations

import asyncio
import json
import re

import pytest
from mcp import Client

from sw_mcp.server import create_server

PRIMITIVES = {"string", "number", "integer", "boolean"}
MAX_PARAMS = 8  # geometry needs up to 8 plain numbers (two 3D points + diameter); still flat
MAX_DESCRIPTION_CHARS = 900  # per tool docstring
LONG_DESCRIPTION_TOOLS = {"build_part": 1900}  # carries the whole plan format
MAX_TOTAL_SCHEMA_CHARS = 21000  # the whole tools/list payload the model must read each turn


async def _list_tools():
    async with Client(create_server()) as client:
        return (await client.list_tools()).tools


@pytest.fixture(scope="module")
def tools():
    return asyncio.run(_list_tools())


def test_all_tools_present(tools):
    assert {t.name for t in tools} == {
        "get_status", "open_document", "get_selection_context", "set_dimension", "save_document",
        "new_part", "make_box", "make_cylinder", "make_prism", "finish_edges", "undo_last_feature",
        "repeat_last_shape", "repeat_last_shape_around", "build_part", "make_assembly", "list_project",
        "manage_documents", "connect_parts", "move_mechanism", "make_motion_study",
        "get_model_summary",
    }


def test_no_auto_titles(tools):
    for t in tools:
        assert "title" not in t.input_schema
        for prop in t.input_schema.get("properties", {}).values():
            assert "title" not in prop, t.name


def test_names_are_plain_snake_case_without_prefix(tools):
    for t in tools:
        assert re.fullmatch(r"[a-z][a-z_]{2,40}", t.name), t.name
        assert not t.name.startswith("sw_"), "OpenCode already prefixes tools with the server name"


def test_parameters_are_flat_and_few(tools):
    for t in tools:
        schema = t.input_schema
        props = schema.get("properties", {})
        assert len(props) <= MAX_PARAMS, t.name
        assert "$defs" not in schema and "definitions" not in schema, t.name
        for name, prop in props.items():
            kind = prop.get("type")
            assert kind in PRIMITIVES, f"{t.name}.{name} has non-flat type {prop}"
            assert prop.get("description"), f"{t.name}.{name} needs a description"


def test_docstrings_follow_the_template(tools):
    for t in tools:
        desc = t.description or ""
        assert "Use when" in desc, t.name
        assert "Example" in desc, t.name
        limit = LONG_DESCRIPTION_TOOLS.get(t.name, MAX_DESCRIPTION_CHARS)
        assert len(desc) <= limit, f"{t.name} description is {len(desc)} chars"


def test_whole_tool_list_fits_the_budget(tools):
    total = sum(len(json.dumps(t.model_dump(exclude_none=True))) for t in tools)
    assert total <= MAX_TOTAL_SCHEMA_CHARS, total


def test_no_output_schema_duplication(tools):
    for t in tools:
        assert t.output_schema is None, f"{t.name} should use structured_output=False"
