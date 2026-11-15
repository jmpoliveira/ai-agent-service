from __future__ import annotations

from pathlib import Path

import pytest

from evals.dataset import DatasetError, load_dataset
from tests.evals.helpers import case, dataset, expect

ROOT = Path(__file__).resolve().parents[2]

_MINIMAL = """
name = "demo"
version = 1
[[cases]]
id = "one"
prompt = "Find Alice"
[cases.expect]
tools = ["search_customers"]
arguments.search_customers.query = "alice"
"""


def test_the_shipped_dataset_is_valid():
    # Act
    loaded = load_dataset(ROOT / "evals" / "datasets" / "tool_use.toml")

    # Assert
    assert loaded.name == "tool_use"
    assert len(loaded.cases) >= 20
    assert len(loaded.fingerprint) == 12


def test_plain_argument_value_is_shorthand_for_equals(tmp_path: Path):
    path = tmp_path / "d.toml"
    path.write_text(_MINIMAL, encoding="utf-8")

    loaded = load_dataset(path)

    matcher = loaded.cases[0].expect.arguments["search_customers"]["query"]
    assert matcher.equals == "alice"


def test_fingerprint_changes_when_the_file_changes(tmp_path: Path):
    path = tmp_path / "d.toml"
    path.write_text(_MINIMAL, encoding="utf-8")
    first = load_dataset(path).fingerprint

    path.write_text(_MINIMAL.replace("Find Alice", "Find Bruno"), encoding="utf-8")

    assert load_dataset(path).fingerprint != first


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("name = ", "not valid TOML"),
        ('name = "x"\nversion = 1\ncases = []', "invalid"),
        (_MINIMAL.replace("tools =", "toolz ="), "invalid"),  # typo in a field name
        (_MINIMAL + '\n[[cases]]\nid = "one"\nprompt = "again"\n', "duplicate case id"),
        (_MINIMAL.replace('id = "one"', 'id = "Has Spaces"'), "invalid"),
    ],
)
def test_bad_datasets_are_rejected_with_a_clear_message(tmp_path: Path, content: str, message: str):
    path = tmp_path / "d.toml"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(DatasetError, match=message):
        load_dataset(path)


def test_missing_file_is_reported(tmp_path: Path):
    with pytest.raises(DatasetError, match="cannot read dataset"):
        load_dataset(tmp_path / "missing.toml")


@pytest.mark.parametrize(
    "fields",
    [
        {"no_tools": True, "tools": ["search_customers"]},
        {"tools": ["create_ticket"], "forbidden_tools": ["create_ticket"]},
        {"arguments": {"t": {"a": {"equals": 1, "contains": "x"}}}},
        {"arguments": {"t": {"a": {}}}},
    ],
)
def test_contradictory_expectations_are_rejected(fields: dict[str, object]):
    with pytest.raises(ValueError, match=r"no_tools|both expected|exactly one"):
        expect(**fields)


def test_defaults_are_merged_and_an_expected_tool_lifts_its_own_ban():
    # Arrange
    read = case("read")
    write = case("write", expect={"tools": ["create_ticket"]})
    data = dataset(
        read,
        write,
        defaults={"forbidden_tools": ["create_ticket"], "max_steps": 4},
    )

    # Act
    read_expect = data.expectation_for(read)
    write_expect = data.expectation_for(write)

    # Assert
    assert read_expect.forbidden_tools == ("create_ticket",)
    assert read_expect.max_steps == 4
    assert write_expect.forbidden_tools == ()


def test_case_stubs_override_dataset_stubs():
    plain = case("plain")
    custom = case("custom", stub_tools={"create_ticket": "custom", "search_customers": "s"})
    data = dataset(plain, custom, stub_tools={"create_ticket": "default"})

    assert data.stubs_for(plain) == {"create_ticket": "default"}
    assert data.stubs_for(custom) == {"create_ticket": "custom", "search_customers": "s"}


def test_select_by_case_id_or_tag_and_reject_unknown_ids():
    data = dataset(case("a", tags=["x"]), case("b", tags=["y"]), case("c"))

    assert [c.id for c in data.select(set(), set())] == ["a", "b", "c"]
    assert [c.id for c in data.select({"c"}, {"x"})] == ["a", "c"]
    with pytest.raises(ValueError, match="unknown case ids"):
        data.select({"nope"}, set())


def test_a_case_can_override_the_default_tool_error_budget_even_with_zero():
    strict = case("strict", expect={"max_tool_errors": 0})
    lenient = case("lenient", expect={"max_tool_errors": 1})
    inherits = case("inherits")
    data = dataset(strict, lenient, inherits, defaults={"max_tool_errors": 2})

    assert data.expectation_for(strict).max_tool_errors == 0
    assert data.expectation_for(lenient).max_tool_errors == 1
    assert data.expectation_for(inherits).max_tool_errors == 2


def test_fingerprint_ignores_comments_and_formatting(tmp_path: Path):
    path = tmp_path / "d.toml"
    path.write_text(_MINIMAL, encoding="utf-8")
    first = load_dataset(path).fingerprint

    path.write_text(
        "# changelog: v1 first cases\n" + _MINIMAL.replace(" = ", "="), encoding="utf-8"
    )

    assert load_dataset(path).fingerprint == first
