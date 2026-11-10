"""The 1.0 compatibility contract (docs/stability.md), enforced.

Each test compares what the package offers now with a snapshot committed under
tests/fixtures/stability/. Removing, renaming or narrowing anything in a snapshot
fails: that is a breaking change. Adding is compatible but still fails until the
snapshot is updated on purpose, so every addition to the contract is a reviewed
diff. To record additions, run

    PAGELEDGER_UPDATE_STABILITY=1 uv run --frozen --extra dev --extra pdf \
        pytest tests/pageledger/test_stability.py

which rewrites a snapshot only when nothing in it was removed or narrowed.
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import difflib
import hashlib
import json
import os
import re
import shutil
import sys
import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from pageledger import (
    adapters,
    artifacts,
    checkpoint,
    cli,
    contest,
    export,
    grading,
    processing_config,
)
from pageledger import config as config_module
from pageledger import processing_policy as policy
from pageledger.document_report import _REPORT_FORMATS
from pageledger.processing import verify_job
from pageledger.verify import verify_run

REPO = Path(__file__).resolve().parents[2]
SNAPSHOTS = REPO / "tests" / "fixtures" / "stability"
RELEASES = SNAPSHOTS / "releases"
RELEASE_VERSIONS = ("0.5.2", "0.6.0", "0.7.0")
UPDATE = os.environ.get("PAGELEDGER_UPDATE_STABILITY") == "1"
HOW = (
    "Additions are compatible: record them on purpose with "
    "PAGELEDGER_UPDATE_STABILITY=1 uv run --frozen --extra dev --extra pdf "
    "pytest tests/pageledger/test_stability.py and commit the snapshot."
)
BREAKING = (
    "is a breaking change to the 1.0 contract (docs/stability.md). "
    "Restore it, or make it in a major release with a changelog migration note."
)


def _dump(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _settle(
    path: Path,
    current: Any,
    breaks: Callable[[Any, Any], list[str]],
    *,
    additions_fail: bool = True,
    hint: str = "",
) -> None:
    """Fail on anything ``breaks`` finds; otherwise demand a deliberate update for additions."""
    if not path.exists():
        if UPDATE:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_dump(current), encoding="utf-8")
            return
        pytest.fail(f"No snapshot at {path.relative_to(REPO)}. {HOW}")
    frozen = json.loads(path.read_text(encoding="utf-8"))
    problems = breaks(frozen, current)
    if problems:
        pytest.fail(
            f"{path.relative_to(REPO)}: this {BREAKING}{hint}\n  " + "\n  ".join(problems),
            pytrace=False,
        )
    if current == frozen or not (additions_fail or UPDATE):
        return
    if UPDATE:
        path.write_text(_dump(current), encoding="utf-8")
        return
    diff = "".join(
        difflib.unified_diff(
            _dump(frozen).splitlines(keepends=True),
            _dump(current).splitlines(keepends=True),
            "snapshot",
            "current",
        )
    )
    pytest.fail(f"{path.relative_to(REPO)} has additions. {HOW}\n{diff}", pytrace=False)


def _missing_members(frozen: dict[str, list], current: dict[str, list]) -> list[str]:
    problems = []
    for name, members in frozen.items():
        if name not in current:
            problems.append(f"{name}: removed")
            continue
        gone = sorted(set(map(str, members)) - set(map(str, current[name])))
        if gone:
            problems.append(f"{name}: removed {gone}")
    return problems


# -- CLI surface -------------------------------------------------------------


def _cli_surface() -> dict[str, Any]:
    def describe(parser: argparse.ArgumentParser) -> dict[str, Any]:
        positionals, options, exclusive = [], {}, []
        for action in parser._actions:
            if isinstance(action, (argparse._HelpAction, argparse._SubParsersAction)):
                continue
            if not action.option_strings:
                positionals.append({"dest": action.dest, "nargs": action.nargs})
                continue
            for option in action.option_strings:
                options[option] = {
                    "required": action.required,
                    "choices": sorted(action.choices) if action.choices else None,
                    "takes_value": action.nargs != 0,
                }
        for group in parser._mutually_exclusive_groups:
            members = sorted(o for a in group._group_actions for o in a.option_strings)
            exclusive.append({"options": members, "required": group.required})
        return {
            "positionals": positionals,
            "options": options,
            "exclusive": sorted(exclusive, key=lambda g: g["options"]),
        }

    parser = cli.build_parser()
    (subparsers,) = (a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return {
        "global": describe(parser),
        "commands": {name: describe(sub) for name, sub in sorted(subparsers.choices.items())},
    }


def _command_breaks(name: str, old: dict, new: dict) -> list[str]:
    problems = []
    if old["positionals"] != new["positionals"]:
        problems.append(f"{name}: positional arguments changed")
    for option, after in new["options"].items():
        if option not in old["options"] and after["required"]:
            problems.append(f"{name} {option}: new and required")
    for option, before in old["options"].items():
        after = new["options"].get(option)
        if after is None:
            problems.append(f"{name} {option}: removed")
            continue
        if after["required"] and not before["required"]:
            problems.append(f"{name} {option}: now required")
        if after["takes_value"] != before["takes_value"]:
            problems.append(
                f"{name} {option}: now {'takes' if after['takes_value'] else 'no'} value"
            )
        if after["choices"] is not None and (
            before["choices"] is None or set(before["choices"]) - set(after["choices"])
        ):
            problems.append(f"{name} {option}: choices narrowed to {after['choices']}")
    together = {
        frozenset((a, b))
        for group in old["exclusive"]
        for a in group["options"]
        for b in group["options"]
    }
    for group in new["exclusive"]:
        known = [o for o in group["options"] if o in old["options"]]
        for a in known:
            for b in known:
                if a < b and frozenset((a, b)) not in together:
                    problems.append(f"{name}: {a} and {b} are now mutually exclusive")
        if group["required"] and not any(
            g["required"] and set(g["options"]) <= set(group["options"]) for g in old["exclusive"]
        ):
            problems.append(f"{name}: one of {group['options']} is now required")
    return problems


def _cli_breaks(old: dict, new: dict) -> list[str]:
    problems = _command_breaks("pageledger", old["global"], new["global"])
    for name, command in old["commands"].items():
        if name not in new["commands"]:
            problems.append(f"{name}: command removed")
        else:
            problems += _command_breaks(name, command, new["commands"][name])
    return problems


def test_cli_commands_and_options_are_a_superset_of_the_snapshot() -> None:
    _settle(SNAPSHOTS / "cli.json", _cli_surface(), _cli_breaks)


def test_cli_snapshot_check_catches_removals_and_tightening() -> None:
    old = _cli_surface()
    assert _cli_breaks(old, json.loads(json.dumps(old))) == []
    for mutate, expected in (
        (lambda s: s["commands"].pop("verify-run"), "verify-run: command removed"),
        (lambda s: s["commands"]["run"]["options"].pop("--pages"), "run --pages: removed"),
        (
            lambda s: s["commands"]["run"]["options"]["--pages"].update(required=True),
            "now required",
        ),
        (
            lambda s: s["commands"]["run"]["options"]["--adapter"].update(choices=["text"]),
            "choices narrowed",
        ),
        (
            lambda s: s["commands"]["run"]["options"].update(
                {"--source-kind": {"required": True, "choices": None, "takes_value": True}}
            ),
            "run --source-kind: new and required",
        ),
        (
            lambda s: s["commands"]["run"]["positionals"].append({"dest": "extra", "nargs": None}),
            "positional arguments changed",
        ),
        (
            lambda s: s["commands"]["run"]["exclusive"].append(
                {"options": ["--dry-run", "--resumable"], "required": False}
            ),
            "mutually exclusive",
        ),
    ):
        new = json.loads(json.dumps(old))
        mutate(new)
        assert any(expected in p for p in _cli_breaks(old, new)), expected
    added = json.loads(json.dumps(old))
    added["commands"]["run"]["options"]["--new-flag"] = {
        "required": False,
        "choices": None,
        "takes_value": False,
    }
    added["commands"]["new-command"] = added["commands"]["doctor"]
    assert _cli_breaks(old, added) == []


def test_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """0 success, 1 runtime error or failed result, 2 usage error (docs/cli.md)."""
    with pytest.raises(SystemExit) as version:
        cli.main(["--version"])
    assert version.value.code == 0
    with pytest.raises(SystemExit) as usage:
        cli.main(["verify-run"])
    assert usage.value.code == 2
    with pytest.raises(SystemExit) as unknown:
        cli.main(["no-such-command"])
    assert unknown.value.code == 2
    assert cli.main(["verify-run", str(tmp_path / "missing")]) == 1
    run = _copy_release("0.7.0", "run", tmp_path)
    assert cli.main(["verify-run", str(run)]) == 0
    capsys.readouterr()


# -- Schemas -----------------------------------------------------------------

_ANNOTATIONS = {"description", "title", "$comment", "examples", "$schema", "$id"}
# Keywords this comparison understands structurally; any other keyword (pattern,
# minimum, const, if/then/else, allOf, $ref ...) may be dropped but not added or changed.
_STRUCTURAL = {
    "type",
    "enum",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "propertyNames",
    "anyOf",
    "oneOf",
    "$defs",
}


def _types(schema: dict) -> set[str] | None:
    value = schema.get("type")
    if value is None:
        return None
    return {value} if isinstance(value, str) else set(value)


def _const_as_enum(schema: dict) -> dict:
    """0.6 wrote hold_policy as const "0.6"; 0.8 lists three. That is a widening."""
    if "const" not in schema or "enum" in schema:
        return schema
    widened = {key: value for key, value in schema.items() if key != "const"}
    widened["enum"] = [schema["const"]]
    return widened


def _optional_subschema(old: dict, new: dict, key: str, path: str) -> list[str]:
    if key not in old:
        return [f"{path}: {key} added"] if key in new else []
    if key not in new:
        return []
    return schema_breaks(old[key], new[key], f"{path}/{key}")


def schema_breaks(old: Any, new: Any, path: str = "$") -> list[str]:
    """What an old artifact valid under ``old`` could fail under ``new``.

    The new schema must accept everything the old one did, so a reader at 1.0 can
    read every artifact written since 0.8:

    - A property may not be removed, and its schema must stay compatible.
    - A new property may be declared on any object. Under a closed object
      (``additionalProperties: false``) no older artifact holds it; under an open
      one, PageLedger never wrote keys its schema did not name. When the old
      object constrained extra keys with a schema, the new property must accept it.
    - ``required`` may lose names, never gain them.
    - ``additionalProperties: false`` may open; an open object may not close.
    - ``type`` may widen (``integer`` to ``number`` counts), never narrow.
    - ``enum`` may gain values or be dropped; a new ``enum`` is a narrowing.
      ``const`` counts as a one-value ``enum``, so it may widen to an ``enum``.
    - ``items`` and ``propertyNames`` are compared recursively; adding one narrows.
    - ``anyOf``/``oneOf``: each old branch must be accepted by some new branch.
      oneOf exclusivity is not modelled.
    - ``$defs`` entries may not be removed and must stay compatible.
    - Every other validation keyword is compared exactly: it may be dropped
      (a widening) but not added or changed.
    - Annotations (description, title, $id, $schema ...) are ignored.
    """
    if old is False or new is True or old == new:
        return []
    if old is True:
        return [f"{path}: now constrained"]
    if new is False:
        return [f"{path}: now rejects everything"]
    old, new = _const_as_enum(old), _const_as_enum(new)
    problems: list[str] = []
    old_types, new_types = _types(old), _types(new)
    if new_types is not None:
        if old_types is None:
            problems.append(f"{path}: type {sorted(new_types)} added")
        else:
            lost = {
                t
                for t in old_types
                if t not in new_types and not (t == "integer" and "number" in new_types)
            }
            if lost:
                problems.append(f"{path}: type {sorted(lost)} removed")
    if "enum" in new:
        if "enum" not in old:
            problems.append(f"{path}: enum added")
        else:
            gone = [v for v in old["enum"] if v not in new["enum"]]
            if gone:
                problems.append(f"{path}: enum values {gone} removed")
    old_props, new_props = old.get("properties", {}), new.get("properties", {})
    for name, sub in old_props.items():
        if name not in new_props:
            problems.append(f"{path}.{name}: property removed")
        else:
            problems += schema_breaks(sub, new_props[name], f"{path}.{name}")
    old_extra = old.get("additionalProperties", True)
    new_extra = new.get("additionalProperties", True)
    if isinstance(old_extra, dict):
        for name in set(new_props) - set(old_props):
            problems += schema_breaks(old_extra, new_props[name], f"{path}.{name}")
    if old_extra is not False and new_extra is not True:
        if new_extra is False or old_extra is True:
            problems.append(f"{path}: additionalProperties closed")
        else:
            problems += schema_breaks(old_extra, new_extra, f"{path}/additionalProperties")
    added = sorted(set(new.get("required", [])) - set(old.get("required", [])))
    if added:
        problems.append(f"{path}: newly required {added}")
    for key in ("items", "propertyNames"):
        problems += _optional_subschema(old, new, key, path)
    for key in ("anyOf", "oneOf"):
        if key not in new:
            continue
        if key not in old:
            problems.append(f"{path}: {key} added")
            continue
        for index, branch in enumerate(old[key]):
            if all(schema_breaks(branch, candidate, path) for candidate in new[key]):
                problems.append(f"{path}/{key}[{index}]: no compatible branch")
    for name, sub in old.get("$defs", {}).items():
        if name not in new.get("$defs", {}):
            problems.append(f"{path}/$defs/{name}: removed")
        else:
            problems += schema_breaks(sub, new["$defs"][name], f"{path}/$defs/{name}")
    for key in set(old) | set(new):
        if key in _STRUCTURAL or key in _ANNOTATIONS or key not in new:
            continue
        if key not in old:
            problems.append(f"{path}: {key} added")
        elif old[key] != new[key]:
            problems.append(f"{path}: {key} changed")
    return problems


def _schema_files() -> dict[str, Any]:
    return {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((REPO / "schemas").glob("*.json"))
    }


@pytest.mark.parametrize("name", sorted(p.name for p in (SNAPSHOTS / "schemas").glob("*.json")))
def test_schema_accepts_everything_its_snapshot_accepted(name: str) -> None:
    """Superset only: a compatible schema edit passes without touching the snapshot."""
    current = _schema_files().get(name)
    assert current is not None, f"schemas/{name} was removed; this {BREAKING}"
    _settle(
        SNAPSHOTS / "schemas" / name,
        current,
        schema_breaks,
        additions_fail=False,
        hint=(
            " Keywords this check does not model (pattern, minimum, if/then/else, allOf, "
            "$ref ...) are compared exactly; if your change to one of them only widens "
            "the schema, edit the snapshot by hand and say why in the commit."
        ),
    )


def test_every_schema_has_a_snapshot() -> None:
    """A new artifact schema enters the contract when its snapshot is committed."""
    schemas = _schema_files()
    missing = sorted(set(schemas) - {p.name for p in (SNAPSHOTS / "schemas").iterdir()})
    if missing and UPDATE:
        for name in missing:
            (SNAPSHOTS / "schemas" / name).write_text(_dump(schemas[name]), encoding="utf-8")
        return
    assert not missing, f"No snapshot for {missing}. {HOW}"


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ({"properties": {"a": {}}}, {"properties": {}}, "property removed"),
        ({"type": ["string", "null"]}, {"type": "string"}, "type ['null'] removed"),
        ({"enum": ["a", "b"]}, {"enum": ["a"]}, "enum values ['b'] removed"),
        ({"type": "string"}, {"type": "string", "enum": ["a"]}, "enum added"),
        ({"required": ["a"]}, {"required": ["a", "b"]}, "newly required ['b']"),
        ({"type": "object"}, {"type": "object", "additionalProperties": False}, "closed"),
        ({"type": "string"}, {"type": "string", "pattern": "^a"}, "pattern added"),
        ({"minimum": 0}, {"minimum": 1}, "minimum changed"),
        ({"enum": ["0.6", "0.8"]}, {"const": "0.8"}, "enum values ['0.6'] removed"),
        ({"type": "string"}, {"type": "string", "const": "a"}, "enum added"),
        ({"type": "array"}, {"type": "array", "items": {"type": "string"}}, "items added"),
        (
            {"items": {"properties": {"a": {"type": "string"}}}},
            {"items": {"properties": {"a": {"type": "integer"}}}},
            "type ['string'] removed",
        ),
        (
            {"anyOf": [{"type": "string"}, {"type": "null"}]},
            {"anyOf": [{"type": "string"}]},
            "no compatible branch",
        ),
        (
            {"additionalProperties": {"type": "string"}},
            {"properties": {"x": {"type": "integer"}}, "additionalProperties": {"type": "string"}},
            "type ['string'] removed",
        ),
    ],
)
def test_schema_comparison_catches_each_kind_of_break(old: dict, new: dict, expected: str) -> None:
    assert any(expected in p for p in schema_breaks(old, new)), schema_breaks(old, new)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            {"properties": {"a": {}}, "additionalProperties": False},
            {"properties": {"a": {}, "b": {"type": "string"}}, "additionalProperties": False},
        ),
        ({"enum": ["a"]}, {"enum": ["a", "b"]}),
        ({"const": "0.6"}, {"enum": ["0.6", "0.6.1", "0.8"]}),
        ({"type": "integer"}, {"type": ["number", "null"]}),
        ({"required": ["a", "b"]}, {"required": ["a"]}),
        ({"additionalProperties": False}, {}),
        ({"type": "string", "pattern": "^a"}, {"type": "string"}),
        ({"anyOf": [{"type": "string"}]}, {"anyOf": [{"type": "string"}, {"type": "null"}]}),
        ({"type": "string", "description": "old"}, {"type": "string", "description": "new"}),
    ],
)
def test_schema_comparison_accepts_additions_and_widening(old: dict, new: dict) -> None:
    assert schema_breaks(old, new) == []


# -- Code taxonomies ---------------------------------------------------------


def _documented_warning_codes() -> list[str]:
    text = (REPO / "docs" / "warnings.md").read_text(encoding="utf-8")
    table = text.split("## Quality warnings", 1)[1].split("\n## ", 1)[0]
    codes = set()
    for line in table.splitlines():
        if line.startswith("| `"):
            codes |= set(re.findall(r"`([a-z_]+)`", line.split("|")[1]))
    return sorted(codes)


def _taxonomies() -> dict[str, Any]:
    return {
        "sets": {
            "warning_codes": _documented_warning_codes(),
            "hold_reasons": sorted(policy._HOLD_ORDER),
            "review_dispositions": sorted(policy.REVIEW_DISPOSITIONS),
            "hold_policies": sorted(policy.HOLD_POLICIES),
            "page_states": sorted(checkpoint.STATES),
            "processing_stages": list(processing_config.STAGES),
            "escalation_triggers": list(processing_config.TRIGGERS),
            "lexicon_providers": list(processing_config.LEXICON_PROVIDERS),
            "contest_rule_sets": sorted(contest.RULE_SETS),
            "export_formats": list(export.FORMATS),
            "document_report_formats": sorted(_REPORT_FORMATS),
            "grades": list(grading.GRADES),
            "adapter_failure_codes": sorted(adapters.ADAPTER_FAILURE_CODES),
            "diagnostic_codes": sorted(adapters.PageLedgerDiagnostic.CODES),
        },
        "mappings": {
            "run_artifacts": dict(artifacts.ARTIFACT_PATHS),
            # A warning keeps the hold it files; a hold reason is never repurposed.
            "warning_holds": dict(sorted(policy._WARNING_HOLDS.items())),
            # A rule set's name versions its whole procedure: its rules never change.
            "contest_rule_sets": json.loads(json.dumps(contest.RULE_SETS, sort_keys=True)),
        },
    }


def _taxonomy_breaks(old: dict, new: dict) -> list[str]:
    problems = _missing_members(old["sets"], new["sets"])
    for name, mapping in old["mappings"].items():
        current = new["mappings"].get(name, {})
        for key, value in mapping.items():
            if key not in current:
                problems.append(f"{name}.{key}: removed")
            elif current[key] != value:
                problems.append(f"{name}.{key}: changed from {value!r} to {current[key]!r}")
    return problems


def test_code_taxonomies_are_a_superset_of_the_snapshot() -> None:
    _settle(SNAPSHOTS / "taxonomies.json", _taxonomies(), _taxonomy_breaks)


def test_taxonomy_check_catches_rename_and_repurpose() -> None:
    old = _taxonomies()
    renamed = json.loads(json.dumps(old))
    renamed["sets"]["warning_codes"].remove("short_text")
    assert _taxonomy_breaks(old, renamed) == ["warning_codes: removed ['short_text']"]
    repurposed = json.loads(json.dumps(old))
    repurposed["mappings"]["warning_holds"]["empty_text"] = "coverage_defect"
    assert "changed from" in _taxonomy_breaks(old, repurposed)[0]
    retuned = json.loads(json.dumps(old))
    retuned["mappings"]["contest_rule_sets"]["ru-print-0.1"]["glyph_pairs"]["rapidocr"].pop()
    assert _taxonomy_breaks(old, retuned)[0].startswith("contest_rule_sets.ru-print-0.1")


def test_every_held_warning_is_documented() -> None:
    assert set(policy._WARNING_HOLDS) <= set(_documented_warning_codes())


def test_snapshotted_warning_codes_are_still_in_the_package() -> None:
    """Warnings are emitted from many modules, so look for each code as a string literal.

    This catches a rename in code that left docs/warnings.md behind; it cannot tell
    whether the code is still emitted.
    """
    literals = {
        node.value
        for path in (REPO / "pageledger").glob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    frozen = json.loads((SNAPSHOTS / "taxonomies.json").read_text(encoding="utf-8"))
    missing = sorted(set(frozen["sets"]["warning_codes"]) - literals)
    assert not missing, f"warning codes {missing} no longer appear in pageledger/: {BREAKING}"


BUILTIN_ADAPTERS = ("text", "pdf_text", "pdf", "pdf_ocr", "vision", "rapidocr", "indra")


@pytest.mark.parametrize("name", BUILTIN_ADAPTERS)
def test_builtin_adapter_names_still_resolve(name: str) -> None:
    """The names live in load_adapter's dispatch, not a constant: probe each one.

    Optional engines may be missing here, or need options; the name must still be known.
    """
    try:
        adapters.load_adapter(name)
    except ValueError as exc:
        assert not str(exc).startswith("Unsupported adapter"), f"{name}: {BREAKING}"


# -- Adapter protocol --------------------------------------------------------

STUB_ADAPTER = textwrap.dedent(
    """\
    from pathlib import Path

    from pageledger.adapters import ExtractionResult

    CALLS = []


    class StrictAdapter:
        # Exactly the 0.8 protocol: no **kwargs, so a new argument is a TypeError.
        name = "strict"
        version = "1"
        deterministic = True
        input_types = ("text",)
        output_types = ("text",)
        capabilities = ("embedded_text", "local")

        def supports(self, action):
            return action == "transcribe_text"

        def extract(self, source, *, page_id, page_number, action, prompt=None):
            CALLS.append(page_id)
            text = Path(source).read_text(encoding="utf-8").split("\\f")[page_number - 1]
            return ExtractionResult(
                content=text,
                format="text",
                confidence=None,
                model="strict-1",
                warnings=[],
                usage={"pages": 1},
            )
    """
)


def _protocol() -> dict[str, Any]:
    fields = dataclasses.fields(adapters.ExtractionResult)
    no_default = (dataclasses.MISSING,)
    return {
        "required_attributes": [attr for attr, _, _ in adapters._ADAPTER_META_CHECKS],
        "extraction_result_required": [
            f.name for f in fields if f.default in no_default and f.default_factory in no_default
        ],
        "extraction_result_optional": [
            f.name
            for f in fields
            if f.default not in no_default or f.default_factory not in no_default
        ],
    }


def _protocol_breaks(old: dict, new: dict) -> list[str]:
    problems = []
    added = sorted(set(new["required_attributes"]) - set(old["required_attributes"]))
    if added:
        problems.append(f"adapters must now declare {added}")
    known = set(old["extraction_result_required"]) | set(old["extraction_result_optional"])
    for name in sorted(
        known - set(new["extraction_result_required"]) - set(new["extraction_result_optional"])
    ):
        problems.append(f"ExtractionResult.{name}: removed")
    for name in sorted(
        set(new["extraction_result_required"]) - set(old["extraction_result_required"])
    ):
        problems.append(f"ExtractionResult.{name}: now required")
    return problems


def test_adapter_protocol_is_a_superset_of_the_snapshot() -> None:
    _settle(SNAPSHOTS / "adapter-protocol.json", _protocol(), _protocol_breaks)


def test_the_0_8_adapter_shape_still_conforms(tmp_path: Path) -> None:
    namespace: dict[str, Any] = {}
    exec(compile(STUB_ADAPTER, "strict_adapter", "exec"), namespace)
    assert adapters.adapter_conformance_check(namespace["StrictAdapter"]()) == []


def _strict_module(tmp_path: Path, name: str) -> Path:
    folder = tmp_path / "adapters"
    folder.mkdir()
    (folder / f"{name}.py").write_text(STUB_ADAPTER, encoding="utf-8")
    return folder


def test_run_resume_process_and_classify_call_adapters_as_0_8_did(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = f"strict_adapter_{os.getpid()}"
    folder = _strict_module(tmp_path, module)
    monkeypatch.syspath_prepend(str(folder))
    spec = f"{module}:StrictAdapter"
    source = tmp_path / "sample.txt"
    source.write_text("one page of text for the strict adapter\ftwo pages", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text(
        "schema_version: '0.1'\n"
        "taxonomy:\n  page_types:\n    prose: {default_action: transcribe_text}\n"
        f"run:\n  adapter: '{spec}'\n",
        encoding="utf-8",
    )
    jobs = tmp_path / "processing.yml"
    jobs.write_text(
        f"schema_version: '0.1'\nprocessing:\n  local_text:\n    adapter: '{spec}'\n",
        encoding="utf-8",
    )
    path = ["--adapter-path", str(folder)]
    routes = tmp_path / "routes.yml"
    assert cli.main(["classify", str(source), "--out", str(routes), "--adapter", spec, *path]) == 0
    for out, extra in (("plain", []), ("resumable", ["--resumable"])):
        argv = ["run", str(source), "--config", str(config), "--out", str(tmp_path / out)]
        assert cli.main([*argv, *extra, *path]) == 0, out
        assert verify_run(tmp_path / out)["status"] == "pass"
    job = tmp_path / "job"
    argv = ["process", str(source), "--config", str(jobs), "--out", str(job), *path]
    assert cli.main(argv) == 0
    assert verify_job(job)["status"] == "pass"
    # Two pages each for the classify probe, the run, the resumable run and the job.
    assert len(sys.modules[module].CALLS) == 8
    capsys.readouterr()


def _example_adapters() -> list[tuple[str, ast.ClassDef]]:
    found = []
    for path in sorted((REPO / "examples").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and any(
                isinstance(item, ast.FunctionDef) and item.name == "extract" for item in node.body
            ):
                found.append((path.name, node))
    return found


@pytest.mark.parametrize("example", _example_adapters(), ids=lambda e: f"{e[0]}:{e[1].name}")
def test_example_adapters_accept_the_protocol_call(example: tuple[str, ast.ClassDef]) -> None:
    """A static check: example engines are not installed in the test environment."""
    _, node = example
    methods = {item.name: item for item in node.body if isinstance(item, ast.FunctionDef)}
    assert "supports" in methods
    args = methods["extract"].args
    names = {a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]}
    assert args.kwarg is not None or {"page_id", "page_number", "action", "prompt"} <= names
    positional = [*args.posonlyargs, *args.args]
    assert len(positional) >= 2  # self, source
    defined = set(methods)
    for item in node.body:
        if isinstance(item, (ast.Assign, ast.AnnAssign)):
            targets = item.targets if isinstance(item, ast.Assign) else [item.target]
            defined |= {t.id for t in targets if isinstance(t, ast.Name)}
    for item in ast.walk(node):
        if isinstance(item, ast.Attribute) and isinstance(item.ctx, ast.Store):
            if isinstance(item.value, ast.Name) and item.value.id == "self":
                defined.add(item.attr)
    missing = [a for a, _, _ in adapters._ADAPTER_META_CHECKS if a not in defined]
    assert not missing, f"{node.name} does not set {missing}"


# -- Configuration -----------------------------------------------------------

PROCESSING_KEYS = (
    "local_text",
    "local_ocr",
    "second_ocr",
    "local_image",
    "image",
    "second_opinion",
    "limits",
    "links",
    "benchmark",
    "batch",
    "escalate_on",
    "lexicon",
    "contest",
)


def _config_keys() -> dict[str, list[str]]:
    return {
        "top_level": sorted(config_module._KNOWN_TOP_LEVEL),
        "run": sorted(config_module._KNOWN_RUN_KEYS),
        "processing_stages": list(processing_config.STAGES),
    }


def test_config_keys_are_a_superset_of_the_snapshot() -> None:
    _settle(SNAPSHOTS / "config-keys.json", _config_keys(), _missing_members)


@pytest.mark.parametrize("key", PROCESSING_KEYS)
def test_processing_keys_are_still_known(key: str) -> None:
    """processing_config checks its keys inline, so probe each 0.8 key by name."""
    try:
        processing_config.processing_config({"processing": {key: None}}, pdf=False)
    except ValueError as exc:
        assert not str(exc).startswith("Unknown processing key"), f"processing.{key}: {BREAKING}"


CONFIG_PROPERTIES = (
    "dataset_citation",
    "language",
    "has_page_types",
    "default_action",
    "default_prompt",
    "classify_adapter",
    "classify_adapter_options",
    "classify_hook",
    "classify_hook_options",
    "classify_min_confidence",
    "adapter_name",
    "adapter_options",
    "adapter_order",
    "budget_max_pages",
    "budget_max_tokens",
    "budget_max_usd",
    "budget_warn_pages",
    "budget_warn_tokens",
    "budget_warn_usd",
    "budget_warn_at_percent",
    "cost_per_page",
    "cost_per_1k_tokens",
    "max_retries",
    "retry_backoff",
    "on_page_error",
    "max_consecutive_failures",
    "max_rerun_depth",
    "review_below_grade",
    "rerun_rules",
    "quarantine_rules",
)


def _effective(path: Path) -> dict[str, Any]:
    loaded = config_module.load_config(path, validate_adapter=False)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    properties = {name: json.loads(json.dumps(getattr(loaded, name))) for name in CONFIG_PROPERTIES}
    # Only the thresholds the file sets: built-in defaults are grading, not contract.
    properties["classify_thresholds.table_column_line_ratio"] = loaded.classify_thresholds[
        "table_column_line_ratio"
    ]
    properties["grading_thresholds.confidence.A"] = loaded.grading_thresholds["confidence"]["A"]
    return {
        "warnings": loaded.warnings,
        "properties": properties,
        "review_after_extraction": {
            name: loaded.review_after_extraction(name) for name in data["taxonomy"]["page_types"]
        },
        "processing": processing_config.processing_config(data, pdf=True),
    }


def test_a_0_8_config_means_what_it_meant() -> None:
    """Every documented key, set to a non-default value, read the way 0.8 read it.

    The compiled processing policy must match exactly: a job records it, so an old
    config has to compile to the policy its jobs recorded.
    """
    current = _effective(SNAPSHOTS / "config-0.8.yml")
    assert current["warnings"] == []

    def breaks(old: dict, new: dict) -> list[str]:
        problems = [
            f"{name}: {old['properties'][name]!r} became {new['properties'][name]!r}"
            for name in old["properties"]
            if old["properties"][name] != new["properties"].get(name)
        ]
        if old["review_after_extraction"] != new["review_after_extraction"]:
            problems.append("taxonomy review flags read differently")
        if old["processing"] != new["processing"]:
            problems.append("processing compiles to a different policy")
        return problems

    _settle(SNAPSHOTS / "config-0.8.effective.json", current, breaks)


# -- Runs and jobs written by published releases -----------------------------


def _copy_release(version: str, kind: str, tmp_path: Path) -> Path:
    """Copy a golden run or job as Git checked it out: its empty directories are gone."""
    recorded = json.loads((RELEASES / version / "run" / "manifest.json").read_text())
    if Path(recorded["inputs"][0]["path"]).exists():
        pytest.skip("the releases' recorded source path exists on this machine")
    target = tmp_path / f"{kind}-{version}"
    shutil.copytree(RELEASES / version / kind, target)
    return target.resolve()


def _issues(report: dict) -> tuple[list[str], list[str]]:
    return [e["code"] for e in report["errors"]], [w["code"] for w in report["warnings"]]


def _index_sample(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    source = tmp_path / "moved.txt"
    shutil.copyfile(RELEASES / "sample.txt", source)
    index = tmp_path / "index.tsv"
    index.write_text(f"{hashlib.sha256(source.read_bytes()).hexdigest()}\t{source}\n")
    monkeypatch.setenv("PAGELEDGER_SOURCE_INDEX", str(index))
    return source


@pytest.mark.parametrize("version", RELEASE_VERSIONS)
def test_old_runs_verify_after_moving_without_their_source(version: str, tmp_path: Path) -> None:
    run = _copy_release(version, "run", tmp_path)
    assert not (run / "normalized").exists()
    report = verify_run(run)
    assert report["status"] == "pass", report["errors"]
    assert _issues(report) == ([], ["source_missing"])


@pytest.mark.parametrize("version", RELEASE_VERSIONS)
def test_old_runs_find_their_moved_source_by_hash(
    version: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _index_sample(tmp_path, monkeypatch)
    report = verify_run(_copy_release(version, "run", tmp_path))
    assert report["status"] == "pass", report["errors"]
    assert _issues(report) == ([], ["source_relocated"])


@pytest.mark.parametrize("version", RELEASE_VERSIONS)
def test_old_job_stage_runs_verify(version: str, tmp_path: Path) -> None:
    job = _copy_release(version, "job", tmp_path)
    report = verify_run(job / "attempts" / "local_text")
    assert report["status"] == "pass", report["errors"]


@pytest.mark.parametrize("version", RELEASE_VERSIONS)
def test_old_jobs_verify_after_moving(
    version: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``root`` in job.json records where a job was written; a copy verifies anywhere."""
    job = _copy_release(version, "job", tmp_path)
    recorded = checkpoint.read_record(job / "job.json")["root"]
    result = verify_job(job)
    assert result["status"] == "pass", result
    assert [w["code"] for w in result["warnings"]] == ["job_relocated", "source_missing"]
    assert result["warnings"][0]["recorded_root"] == recorded
    assert result["warnings"][0]["root"] == str(job)

    _index_sample(tmp_path, monkeypatch)
    result = verify_job(job)
    assert [w["code"] for w in result["warnings"]] == ["job_relocated", "source_relocated"]


def _change_raw_page(job: Path) -> None:
    (job / "attempts" / "local_text" / "raw" / "doc_0001_page_0001.txt").write_text("changed")


def _change_report(job: Path) -> None:
    report = job / "report.md"
    report.write_text(report.read_text(encoding="utf-8").replace("Status:", "Status: ok,", 1))


def _remove_attempt(job: Path) -> None:
    shutil.rmtree(job / "attempts" / "local_text")


@pytest.mark.parametrize("version", RELEASE_VERSIONS)
@pytest.mark.parametrize(
    "tamper",
    [_change_raw_page, _change_report, _remove_attempt],
    ids=["raw page", "report", "attempt"],
)
def test_a_tampered_copy_of_an_old_job_fails(
    version: str, tamper: Callable[[Path], None], tmp_path: Path
) -> None:
    job = _copy_release(version, "job", tmp_path)
    tamper(job)
    assert verify_job(job)["status"] == "fail"


@pytest.mark.parametrize("version", RELEASE_VERSIONS)
def test_a_copy_of_an_old_job_can_be_reviewed_and_its_receipt_is_bound(
    version: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pageledger.processing import review_job

    _index_sample(tmp_path, monkeypatch)
    job = _copy_release(version, "job", tmp_path)
    page = checkpoint.read_record(job / "job.json")["pages"][0]
    attempt = page["attempts"][0]
    review = tmp_path / "review.json"
    review.write_text(
        json.dumps(
            {
                "schema_version": "0.1",
                "source_sha256": page["source_sha256"],
                "decisions": [
                    {
                        "page_id": page["page_id"],
                        "page_number": page["page_number"],
                        "disposition": "reviewed_text",
                        "selected_attempt": attempt["attempt_id"],
                        "output_sha256": attempt["raw_sha256"],
                        "reviewer": "person:colleague",
                        "reason": "Read against the page",
                        "reviewed_at": "2026-11-10T12:00:00Z",
                    }
                ],
            }
        )
    )
    assert review_job(job, review)["status"] == "completed"
    record = checkpoint.read_record(job / "job.json")
    assert record.get("hold_policy") == checkpoint.read_record(
        RELEASES / version / "job" / "job.json"
    ).get("hold_policy")
    assert [item["command"] for item in record["relocations"]] == ["review-job"]
    result = verify_job(job)
    assert result["status"] == "pass", result
    assert [w["code"] for w in result["warnings"]] == [
        "job_relocated",
        "job_changed_elsewhere",
        "source_relocated",
    ]

    record["pages"][0]["review"]["decisions"][0]["reason"] = "changed"
    checkpoint.write_record(job / "job.json", record)
    assert verify_job(job)["status"] == "fail"


def test_a_current_job_verifies_after_moving(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "sample.txt"
    shutil.copyfile(RELEASES / "sample.txt", source)
    job = tmp_path / "job"
    argv = ["process", str(source), "--config", str(RELEASES / "processing.yml"), "--out", str(job)]
    assert cli.main(argv) == 0
    assert verify_job(job)["status"] == "pass"
    for name in ("job.json", "document.json", "report.md", "transcript.md"):
        assert (job / name).is_file(), f"{name}: {BREAKING}"
    shutil.copytree(job, tmp_path / "moved")
    result = verify_job(tmp_path / "moved")
    assert result["status"] == "pass", result
    assert [w["code"] for w in result["warnings"]] == ["job_relocated"]
    capsys.readouterr()


_RUN_SCHEMAS = {
    "manifest.json": "manifest.schema.json",
    "audit.json": "audit.schema.json",
    "cost.json": "cost.schema.json",
    "provenance.jsonl": "provenance-line.schema.json",
    "quality.jsonl": "quality-line.schema.json",
    "run.log": "run-log-line.schema.json",
}
_JOB_SCHEMAS = {
    "job.json": "job.schema.json",
    "document.json": "document.schema.json",
    "attempts/local_text/checkpoint.json": "checkpoint.schema.json",
    **{f"attempts/local_text/{name}": schema for name, schema in _RUN_SCHEMAS.items()},
}


def _records(path: Path) -> list[Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        return [json.loads(text)]
    return [json.loads(line) for line in text.splitlines() if line.strip()]


@pytest.mark.parametrize("version", RELEASE_VERSIONS)
@pytest.mark.parametrize("kind", ["run", "job"])
def test_old_artifacts_validate_against_current_schemas(version: str, kind: str) -> None:
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    root = RELEASES / version / kind
    files = dict(_RUN_SCHEMAS if kind == "run" else _JOB_SCHEMAS)
    if kind == "job":
        pages = root / "attempts" / "local_text" / ".checkpoint" / "pages"
        files.update(
            {str(p.relative_to(root)): "checkpoint-page.schema.json" for p in pages.glob("*")}
        )
    schemas = _schema_files()
    registry = Registry().with_resources(
        (name, Resource.from_contents(schema)) for name, schema in schemas.items()
    )
    for name, schema in files.items():
        validator = Draft202012Validator(schemas[schema], registry=registry)
        for record in _records(root / name):
            validator.validate(record)


def test_release_fixtures_record_their_versions_and_hold_policies() -> None:
    expected_policy = {"0.5.2": None, "0.6.0": "0.6", "0.7.0": "0.6.1"}
    for version in RELEASE_VERSIONS:
        manifest = json.loads((RELEASES / version / "run" / "manifest.json").read_text())
        assert manifest["pageledger_version"] == version
        job = checkpoint.read_record(RELEASES / version / "job" / "job.json")
        assert job.get("hold_policy") == expected_policy[version]
        if job.get("hold_policy") is not None:
            assert job["hold_policy"] in policy.HOLD_POLICIES
