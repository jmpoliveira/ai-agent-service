"""The eval dataset: cases written in TOML, validated into typed models.

TOML because the standard library reads it (no extra dependency) and it is already the
format of ``pyproject.toml``. ``extra="forbid"`` everywhere: a typo in a field name must be
an error, not an expectation that is silently never checked.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from pathlib import Path
from typing import Any, Self, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ArgMatcher(_Strict):
    """How to compare one tool argument. Text comparisons ignore case and outer spaces."""

    equals: Any = None
    contains: str | None = None
    one_of: tuple[Any, ...] | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        given = [self.equals is not None, self.contains is not None, self.one_of is not None]
        if sum(given) != 1:
            raise ValueError("an argument matcher needs exactly one of: equals, contains, one_of")
        return self


class Expectation(_Strict):
    """What a good run looks like. Every field is optional; only the given ones are checked."""

    # Tools that must be called, in this relative order (other calls may happen in between).
    tools: tuple[str, ...] = ()
    # The run must not call any tool at all.
    no_tools: bool = False
    # Tools that must never be called. Merged with the dataset defaults.
    forbidden_tools: tuple[str, ...] = ()
    # tool name -> argument name -> matcher. Satisfied if ANY call to that tool matches.
    arguments: dict[str, dict[str, ArgMatcher]] = Field(default_factory=dict)
    # Final answer checks, case-insensitive.
    answer_contains: tuple[str, ...] = ()
    answer_contains_any: tuple[str, ...] = ()
    answer_not_contains: tuple[str, ...] = ()
    max_steps: int | None = Field(default=None, ge=1)
    # Most tool calls that may return an error. Failed calls usually mean the model is guessing
    # (an invented id, the wrong kind of id), even when it later stumbles on the right answer.
    max_tool_errors: int | None = Field(default=None, ge=0)
    # Regex of an identifier (for example ^[A-Z]-\d+$). Every argument that is an identifier must
    # have appeared before the call, in the prompt or an earlier tool result: otherwise the model
    # guessed it, even when the guess happened to be right. Empty string disables the check.
    id_pattern: str | None = None
    stop_reason: str = "finished"

    @field_validator("arguments", mode="before")
    @classmethod
    def _plain_value_means_equals(cls, value: Any) -> Any:
        # Shorthand: `ticket_id = "T-1001"` is the same as `ticket_id = { equals = "T-1001" }`.
        if not isinstance(value, dict):
            return value
        expanded: dict[str, Any] = {}
        for tool, args in cast(dict[str, Any], value).items():
            if isinstance(args, dict):
                expanded[tool] = {
                    name: matcher if isinstance(matcher, dict) else {"equals": matcher}
                    for name, matcher in cast(dict[str, Any], args).items()
                }
            else:
                expanded[tool] = args
        return expanded

    @field_validator("id_pattern")
    @classmethod
    def _valid_regex(cls, value: str | None) -> str | None:
        return _check_regex(value)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.no_tools and (self.tools or self.arguments):
            raise ValueError("no_tools cannot be combined with tools or arguments")
        clash = set(self.tools) & set(self.forbidden_tools)
        if clash:
            raise ValueError(f"tools both expected and forbidden: {sorted(clash)}")
        return self


def _check_regex(value: str | None) -> str | None:
    if value:
        try:
            re.compile(value)
        except re.error as err:
            raise ValueError(f"id_pattern is not a valid regular expression: {err}") from err
    return value


class Defaults(_Strict):
    forbidden_tools: tuple[str, ...] = ()
    max_steps: int | None = Field(default=None, ge=1)
    max_tool_errors: int | None = Field(default=None, ge=0)
    id_pattern: str | None = None

    @field_validator("id_pattern")
    @classmethod
    def _valid_regex(cls, value: str | None) -> str | None:
        return _check_regex(value)


class Case(_Strict):
    id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9-]*$")
    prompt: str = Field(min_length=1)
    tags: tuple[str, ...] = ()
    # Tool name -> canned result text. Overrides the dataset's stub_tools for this case.
    stub_tools: dict[str, str] = Field(default_factory=dict)
    expect: Expectation = Expectation()


class Dataset(_Strict):
    name: str = Field(min_length=1)
    version: int = Field(ge=1)
    description: str = ""
    defaults: Defaults = Defaults()
    # Tools that are never sent to the real server during evals (for example write tools),
    # mapped to the canned result the model receives instead.
    stub_tools: dict[str, str] = Field(default_factory=dict)
    cases: tuple[Case, ...] = Field(min_length=1)
    # Hash of the graded content, so a report always says exactly which rules scored it.
    fingerprint: str = ""

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        seen: set[str] = set()
        for case in self.cases:
            if case.id in seen:
                raise ValueError(f"duplicate case id: {case.id}")
            seen.add(case.id)
        return self

    def expectation_for(self, case: Case) -> Expectation:
        """The case's expectation with the dataset defaults applied."""
        forbidden = tuple(
            name
            for name in dict.fromkeys(self.defaults.forbidden_tools + case.expect.forbidden_tools)
            if name not in case.expect.tools  # a case may expect a tool the defaults forbid
        )
        max_steps = case.expect.max_steps or self.defaults.max_steps
        # `is None`, not `or`: a case may set 0, which is falsy but meaningful.
        max_tool_errors = (
            self.defaults.max_tool_errors
            if case.expect.max_tool_errors is None
            else case.expect.max_tool_errors
        )
        id_pattern = (
            self.defaults.id_pattern if case.expect.id_pattern is None else case.expect.id_pattern
        )
        return case.expect.model_copy(
            update={
                "forbidden_tools": forbidden,
                "max_steps": max_steps,
                "max_tool_errors": max_tool_errors,
                "id_pattern": id_pattern,
            }
        )

    def stubs_for(self, case: Case) -> dict[str, str]:
        return {**self.stub_tools, **case.stub_tools}

    def select(self, case_ids: set[str], tags: set[str]) -> tuple[Case, ...]:
        """Cases matching any of ``case_ids`` or ``tags``; all cases when both are empty."""
        if not case_ids and not tags:
            return self.cases
        unknown = case_ids - {case.id for case in self.cases}
        if unknown:
            raise ValueError(f"unknown case ids: {sorted(unknown)}")
        return tuple(
            case for case in self.cases if case.id in case_ids or tags.intersection(case.tags)
        )


class DatasetError(ValueError):
    """The dataset file is missing, is not valid TOML, or does not match the schema."""


def load_dataset(path: Path) -> Dataset:
    try:
        raw = path.read_bytes()
    except OSError as err:
        raise DatasetError(f"cannot read dataset {path}: {err.strerror}") from err
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as err:
        raise DatasetError(f"dataset {path} is not valid TOML: {err}") from err
    try:
        dataset = Dataset.model_validate(data)
    except ValidationError as err:
        raise DatasetError(f"dataset {path} is invalid:\n{err}") from err
    return dataset.model_copy(update={"fingerprint": _fingerprint(dataset)})


def _fingerprint(dataset: Dataset) -> str:
    """A hash of what is graded (cases, expectations, defaults, stubs), not of the file bytes:
    comments, spacing and key order do not change the results, so they must not change it."""
    content = dataset.model_dump(mode="json", exclude={"fingerprint"})
    canonical = json.dumps(content, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
