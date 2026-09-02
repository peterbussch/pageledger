"""Focused tests for artifact serialization helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

import pageledger.artifacts as artifacts_module

ROUTE_MAP = {
    "schema_version": "1.0",
    "pageledger_version": "0.4.1",
    "run_id": "bench-ä-5000",
    "generated_at": "2026-08-22T00:00:00Z",
    "classifier": {"adapter": None, "model": None, "prompt_hash": None},
    "documents": [
        {
            "source": "folios/échantillon.pdf",
            "pages": [
                {
                    "page_id": "doc-0001-p0001",
                    "page_number": 1,
                    "route": "extract",
                    "flags": ["ocr_required", "quality:good"],
                }
            ],
        }
    ],
}

RERUN_MANIFEST = {
    "schema_version": "1.0",
    "run_id": "run-rerun",
    "parent_run_id": "run",
    "parent_manifest": "manifest.json",
    "rerun_depth": 0,
    "max_rerun_depth": 2,
    "created_at": "2026-08-22T00:00:00Z",
    "reason": "quality_warning",
    "rerun_executable": True,
    "rerun_status": "executable",
    "items": [
        {
            "page_id": "p-1",
            "page_number": 1,
            "source": "x.pdf",
            "action": "rerun",
            "reason": "quality_warning",
            "previous_grade": "C",
        }
    ],
}

SCALAR_AND_ORDER_CASE = {
    "plain": "text",
    "unicode": "Καλημέρα 世界 café",
    "numeric_like": "00123",
    "boolean_like": "yes",
    "null_like": "null",
    "multiline": "line one\nline two\n",
    "empty": "",
    "true": True,
    "false": False,
    "nothing": None,
    "integer": -7,
    "fraction": 3.25,
    "nested": [{"z": 0, "a": ["first", "second"]}],
}

BYTE_INCOMPATIBLE_UNICODE_CASES = [
    {
        "outer": [
            {
                "supplementary \U0001f600 key": "value",
                "value": "supplementary \U0001f600 value",
            }
        ]
    },
    {
        "outer": [
            {
                "next-line \u0085 key": "value",
                "value": "next-line \u0085 value",
            }
        ]
    },
]

SAFE_DUMPER_ONLY_CASES = [
    {"outer": [{"": "empty mapping key"}]},
    {"outer": [{"carriage\rreturn": "mapping key"}]},
    {"outer": [{"value": "contains ordinary whitespace"}]},
    {"outer": [{"value": "unpaired surrogate \ud800"}]},
    {"outer": [{"value": ("non", "json", "tuple")}]},
]


@pytest.mark.parametrize(
    "data",
    [ROUTE_MAP, RERUN_MANIFEST, SCALAR_AND_ORDER_CASE],
    ids=["route-map", "rerun-manifest", "unicode-scalars-and-order"],
)
def test_write_yaml_matches_safe_dump_bytes(tmp_path: Path, data: dict[str, Any]) -> None:
    expected = yaml.safe_dump(data, allow_unicode=True, sort_keys=False).encode()
    output = tmp_path / "artifact.yml"

    artifacts_module.write_yaml(output, data)

    assert output.read_bytes() == expected


@pytest.mark.parametrize(
    "data",
    [ROUTE_MAP, RERUN_MANIFEST],
    ids=["route-map", "rerun-manifest"],
)
@pytest.mark.skipif(not hasattr(yaml, "CSafeDumper"), reason="libyaml is unavailable")
def test_write_yaml_selects_c_safe_dumper_when_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data: dict[str, Any]
) -> None:
    expected = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    selected_dumpers: list[object] = []
    original_dump = artifacts_module.yaml.dump

    def recording_dump(value: dict[str, Any], **kwargs: Any) -> str:
        selected_dumpers.append(kwargs["Dumper"])
        return original_dump(value, **kwargs)

    monkeypatch.setattr(artifacts_module.yaml, "dump", recording_dump)
    output = tmp_path / "artifact.yml"

    artifacts_module.write_yaml(output, data)

    assert selected_dumpers == [yaml.CSafeDumper]
    assert output.read_text(encoding="utf-8") == expected


def test_write_yaml_falls_back_to_safe_dumper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = SCALAR_AND_ORDER_CASE
    expected = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    selected_dumpers: list[object] = []
    original_dump = artifacts_module.yaml.dump

    def recording_dump(value: dict[str, Any], **kwargs: Any) -> str:
        selected_dumpers.append(kwargs["Dumper"])
        return original_dump(value, **kwargs)

    monkeypatch.delattr(artifacts_module.yaml, "CSafeDumper", raising=False)
    monkeypatch.setattr(artifacts_module.yaml, "dump", recording_dump)
    output = tmp_path / "artifact.yml"

    artifacts_module.write_yaml(output, data)

    assert selected_dumpers == [yaml.SafeDumper]
    assert output.read_text(encoding="utf-8") == expected


@pytest.mark.parametrize(
    "data",
    SAFE_DUMPER_ONLY_CASES,
    ids=["empty-key", "carriage-return-key", "whitespace", "surrogate", "non-json-value"],
)
def test_write_yaml_uses_safe_dumper_outside_proven_fast_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data: dict[str, Any]
) -> None:
    expected = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    selected_dumpers: list[object] = []
    original_dump = artifacts_module.yaml.dump

    def recording_dump(value: dict[str, Any], **kwargs: Any) -> str:
        selected_dumpers.append(kwargs["Dumper"])
        return original_dump(value, **kwargs)

    monkeypatch.setattr(artifacts_module.yaml, "dump", recording_dump)
    output = tmp_path / "artifact.yml"

    artifacts_module.write_yaml(output, data)

    assert selected_dumpers == [yaml.SafeDumper]
    assert output.read_text(encoding="utf-8") == expected


@pytest.mark.parametrize("cyclic", [False, True], ids=["shared-container", "cycle"])
def test_write_yaml_uses_safe_dumper_for_container_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cyclic: bool
) -> None:
    shared: list[object] = []
    if cyclic:
        shared.append(shared)
        data = {"cycle": shared}
    else:
        shared.append("value")
        data = {"first": shared, "second": shared}
    expected = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    selected_dumpers: list[object] = []
    original_dump = artifacts_module.yaml.dump

    def recording_dump(value: dict[str, Any], **kwargs: Any) -> str:
        selected_dumpers.append(kwargs["Dumper"])
        return original_dump(value, **kwargs)

    monkeypatch.setattr(artifacts_module.yaml, "dump", recording_dump)
    output = tmp_path / "artifact.yml"

    artifacts_module.write_yaml(output, data)

    assert selected_dumpers == [yaml.SafeDumper]
    assert output.read_text(encoding="utf-8") == expected


@pytest.mark.parametrize(
    "data",
    BYTE_INCOMPATIBLE_UNICODE_CASES,
    ids=["supplementary-unicode", "unicode-next-line"],
)
def test_write_yaml_uses_safe_dumper_for_byte_incompatible_unicode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data: dict[str, Any]
) -> None:
    expected = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    selected_dumpers: list[object] = []
    original_dump = artifacts_module.yaml.dump

    def recording_dump(value: dict[str, Any], **kwargs: Any) -> str:
        selected_dumpers.append(kwargs["Dumper"])
        return original_dump(value, **kwargs)

    monkeypatch.setattr(artifacts_module.yaml, "dump", recording_dump)
    output = tmp_path / "artifact.yml"

    artifacts_module.write_yaml(output, data)

    assert selected_dumpers == [yaml.SafeDumper]
    assert output.read_text(encoding="utf-8") == expected
