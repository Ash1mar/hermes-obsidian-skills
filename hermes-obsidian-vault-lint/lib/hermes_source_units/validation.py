"""Offline P0 shape/relationship checks, not source-integrity or authorization checks."""

from __future__ import annotations

import hashlib
from importlib.resources import files
import json
import re
from typing import Any, Iterable, Mapping

class ContractError(ValueError):
    def __init__(self, code: str, path: str, message: str) -> None:
        self.code, self.path, self.message = code, path, message
        super().__init__(f"{code} at {path}: {message}")


def _fail(code: str, path: str, message: str) -> None:
    raise ContractError(code, path, message)


def canonical_json(value: Any) -> bytes:
    """Hermes canonical-json/v1: sorted keys, UTF-8, compact, no floats/NaN.

    No Unicode or newline normalization here: callers hash the declared canonical
    source. This is a project-local profile, not a claim of RFC 8785 compliance.
    """
    def check(item: Any) -> None:
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is list:
            for child in item:
                check(child)
            return
        if type(item) is dict and all(type(k) is str for k in item):
            for child in item.values():
                check(child)
            return
        _fail("INVALID_CANONICAL_VALUE", "$", "only JSON strings, integers, booleans, null, arrays and string-keyed objects are allowed")
    check(value)
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except UnicodeError as exc:
        raise ContractError("INVALID_CANONICAL_VALUE", "$", "unpaired Unicode surrogate") from exc


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def load_schema() -> dict[str, Any]:
    return json.loads(files("hermes_source_units").joinpath("schemas/contracts.json").read_text(encoding="utf-8"))


def default_config() -> dict[str, Any]:
    return json.loads(files("hermes_source_units").joinpath("defaults/config.json").read_text(encoding="utf-8"))


def valid_relative_path(value: Any) -> bool:
    if not isinstance(value, str):
        return True  # JSON Schema's type validator reports this separately
    if not value or re.search(r'[\\:\x00-\x1f\x7f]', value):
        return False
    parts = value.split("/")
    return all(p and p not in (".", "..") and not p.endswith((" ", ".")) for p in parts)


_SHAPE_KEYS = {"$ref", "type", "properties", "required", "additionalProperties",
               "const", "enum", "oneOf", "anyOf", "items", "minItems", "uniqueItems",
               "minimum", "minLength", "maxLength", "pattern", "format"}
_JSON_TYPES = {"object": dict, "array": list, "string": str, "integer": int,
               "boolean": bool, "null": type(None)}


def check_schema(schema: dict[str, Any]) -> None:
    """Audit our fixed contract vocabulary, NOT a general JSON Schema validator.

    New keywords/formats, remote references and recursive definitions fail closed.
    Schema remains the single description of fields; business checks follow below.
    """
    if set(schema) != {"$schema", "$id", "$defs"} or not isinstance(schema["$defs"], dict):
        _fail("UNSUPPORTED_SCHEMA", "$", "expected the bundled schema envelope")
    if schema["$schema"] != "https://json-schema.org/draft/2020-12/schema":
        _fail("UNSUPPORTED_SCHEMA", "$", "unexpected schema dialect")
    done: set[str] = set()

    def audit(node: Any, stack: tuple[str, ...] = ()) -> None:
        if not isinstance(node, dict) or not node or set(node) - _SHAPE_KEYS:
            _fail("UNSUPPORTED_SCHEMA", "$", "unknown schema keyword or empty shape")
        if "$ref" in node:
            target = node["$ref"]
            if set(node) != {"$ref"} or not isinstance(target, str) or not target.startswith("#/$defs/"):
                _fail("UNSUPPORTED_SCHEMA", "$", "only local, standalone definition references are supported")
            name = target.removeprefix("#/$defs/")
            if name not in schema["$defs"] or name in stack:
                _fail("UNSUPPORTED_SCHEMA", "$", "unknown or recursive definition")
            if name not in done:
                audit(schema["$defs"][name], (*stack, name))
                done.add(name)
            return
        for union in ("oneOf", "anyOf"):
            if union in node:
                if set(node) != {union} or not isinstance(node[union], list) or not node[union]:
                    _fail("UNSUPPORTED_SCHEMA", "$", "invalid union shape")
                for branch in node[union]:
                    audit(branch, stack)
                return
        if "type" in node and node["type"] not in _JSON_TYPES:
            _fail("UNSUPPORTED_SCHEMA", "$", "unsupported JSON type")
        allowed = {
            "object": {"properties", "required", "additionalProperties"},
            "array": {"items", "minItems", "uniqueItems"},
            "string": {"minLength", "maxLength", "pattern", "format"},
            "integer": {"minimum"}, "boolean": set(), "null": set(),
            None: set(),
        }[node.get("type")] | {"type", "const", "enum"}
        if set(node) - allowed:
            _fail("UNSUPPORTED_SCHEMA", "$", "keyword does not apply to this contract shape")
        for bound in ("minimum", "minLength", "maxLength", "minItems"):
            if bound in node and (type(node[bound]) is not int or node[bound] < 0):
                _fail("UNSUPPORTED_SCHEMA", "$", "bounds must be nonnegative integers")
        if "uniqueItems" in node and type(node["uniqueItems"]) is not bool:
            _fail("UNSUPPORTED_SCHEMA", "$", "uniqueItems must be boolean")
        if "enum" in node and (not isinstance(node["enum"], list) or not node["enum"]):
            _fail("UNSUPPORTED_SCHEMA", "$", "enum must be a nonempty list")
        if "format" in node and node["format"] != "vault-relative-path":
            _fail("UNSUPPORTED_SCHEMA", "$", "unsupported format")
        if node.get("type") == "object":
            if node.get("additionalProperties") is not False or not isinstance(node.get("properties"), dict):
                _fail("UNSUPPORTED_SCHEMA", "$", "objects must be closed")
            required = node.get("required", [])
            if not isinstance(required, list) or any(k not in node["properties"] for k in required):
                _fail("UNSUPPORTED_SCHEMA", "$", "invalid required fields")
            for child in node["properties"].values():
                audit(child, stack)
        if node.get("type") == "array":
            audit(node.get("items"), stack)
        if "pattern" in node:
            try:
                re.compile(node["pattern"])
            except (TypeError, re.error) as exc:
                raise ContractError("UNSUPPORTED_SCHEMA", "$", "invalid pattern") from exc

    for name in schema["$defs"]:
        audit({"$ref": "#/$defs/" + name})


def _check_shape(node: dict[str, Any], value: Any, defs: dict[str, Any], path: str) -> None:
    if "$ref" in node:
        _check_shape(defs[node["$ref"].removeprefix("#/$defs/")], value, defs, path)
        return
    for union in ("oneOf", "anyOf"):
        if union in node:
            matches = 0
            for branch in node[union]:
                try:
                    _check_shape(branch, value, defs, path)
                    matches += 1
                except ContractError as exc:
                    if exc.code != "INVALID_SCHEMA":
                        raise
            if matches == 0 or (union == "oneOf" and matches != 1):
                _fail("INVALID_SCHEMA", path, "value does not match the declared union")
            return
    if "type" in node and type(value) is not _JSON_TYPES[node["type"]]:
        _fail("INVALID_SCHEMA", path, "expected " + node["type"])
    # Type-sensitive equality: true must not pass a numeric const/enum as 1.
    if "const" in node and (type(value) is not type(node["const"]) or value != node["const"]):
        _fail("INVALID_SCHEMA", path, "unexpected constant/contract version")
    if "enum" in node and not any(type(value) is type(v) and value == v for v in node["enum"]):
        _fail("INVALID_SCHEMA", path, "value is not in the declared enum")
    if type(value) is dict:
        props = node.get("properties", {})
        if any(k not in value for k in node.get("required", [])):
            _fail("INVALID_SCHEMA", path, "missing required field")
        if node.get("additionalProperties") is False and set(value) - set(props):
            _fail("INVALID_SCHEMA", path, "unexpected field")
        for key, child in value.items():
            if key in props:
                _check_shape(props[key], child, defs, f"{path}.{key}")
    if type(value) is list:
        if len(value) < node.get("minItems", 0):
            _fail("INVALID_SCHEMA", path, "too few items")
        if node.get("uniqueItems") and len({canonical_json(v) for v in value}) != len(value):
            _fail("INVALID_SCHEMA", path, "items must be unique")
        if "items" in node:
            for i, child in enumerate(value):
                _check_shape(node["items"], child, defs, f"{path}[{i}]")
    if type(value) is str:
        if len(value) < node.get("minLength", 0) or len(value) > node.get("maxLength", len(value)):
            _fail("INVALID_SCHEMA", path, "invalid string length")
        if "pattern" in node and re.search(node["pattern"], value) is None:
            _fail("INVALID_SCHEMA", path, "string does not match required pattern")
        if node.get("format") == "vault-relative-path" and not valid_relative_path(value):
            _fail("INVALID_SCHEMA", path, "expected a safe relative path")
    if type(value) is int and "minimum" in node and value < node["minimum"]:
        _fail("INVALID_SCHEMA", path, "number below minimum")


def _walk(value: Any, path: str = "$") -> Iterable[tuple[str, Any]]:
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, f"{path}[{index}]")


def _unique(values: list[Any], path: str) -> None:
    encoded = [canonical_json(item) for item in values]
    if len(encoded) != len(set(encoded)):
        _fail("DUPLICATE", path, "duplicate record/reference")


def validate_record(kind: str, record: Mapping[str, Any]) -> None:
    """Validate closed shape plus local invariants. No filesystem/model access."""
    schema = load_schema()
    if kind not in schema["$defs"]:
        _fail("UNKNOWN_KIND", "$", kind)
    check_schema(schema)
    _check_shape(schema["$defs"][kind], record, schema["$defs"], "$")
    for path, item in _walk(record):
        if isinstance(item, float):
            _fail("INVALID_SCHEMA", path, "this contract requires integer JSON numbers")
        if not isinstance(item, dict):
            continue
        if set(item) == {"start", "end"} and item["start"] >= item["end"]:
            _fail("INVALID_RANGE", path, "requires start < end")
        if "line_start" in item and item["line_start"] > item["line_end"]:
            _fail("INVALID_RANGE", path, "requires line_start <= line_end")
        if "qa" in item and item["qa"] == "needs-qa" and not item["qa_note"].strip():
            _fail("QA_NOTE_REQUIRED", path, "needs-qa requires a specific note")
        if "pages" in item:
            _unique(item["pages"], path + ".pages")
        for field in ("core_refs", "context_refs", "omitted_refs", "target_refs", "support_refs", "source_unit_refs"):
            if field in item:
                _unique(item[field], path + "." + field)
    if kind == "config":
        source, retrieval = record["source"], record["retrieval"]
        if source["target_codepoints"] > source["max_codepoints"]:
            _fail("INVALID_BUDGET", "$.source", "target exceeds maximum")
        if not retrieval["overlap_tokens"] < retrieval["target_tokens"] <= retrieval["max_tokens"]:
            _fail("INVALID_BUDGET", "$.retrieval", "requires overlap < target <= maximum")
    if kind == "unit":
        asset = record["locator"]["kind"] == "asset"
        if (record["content_type"] == "source_asset") != asset:
            _fail("TYPE_LOCATOR_MISMATCH", "$.locator", "source_asset requires an asset locator; other types require text")
        if asset and record["content_sha256"] != record["locator"]["sha256"]:
            _fail("HASH_MISMATCH", "$.content_sha256", "asset hashes disagree")
        if record["content_type"] in ("ocr_transcription", "model_derived") and not record["derived_from"]:
            _fail("PROVENANCE_REQUIRED", "$.derived_from", "derived content requires a source")
        if any(item["unit_ref"] == record["ref"] for item in record["derived_from"]):
            _fail("SELF_REFERENCE", "$.derived_from", "a unit cannot derive from itself")
        for field in ("prev_ref", "next_ref"):
            other = record[field]
            if other is not None and (other == record["ref"] or any(other[k] != record["ref"][k] for k in ("vault_id", "resource_id", "artifact_revision", "unit_set_id"))):
                _fail("INVALID_NEIGHBOR", "$.'" + field + "'", "neighbor must be a different unit in the same set")
    if kind == "section":
        cursor = record["scope"]["start"]
        for span in record["owned_ranges"]:
            if span["start"] < cursor or span["end"] > record["scope"]["end"]:
                _fail("INVALID_OWNERSHIP", "$.owned_ranges", "ranges must be ordered, disjoint and inside scope")
            cursor = span["end"]
        if record["parent_id"] == record["section_id"]:
            _fail("SELF_REFERENCE", "$.parent_id", "section cannot parent itself")
    if kind in ("reading_window", "retrieval_window"):
        if record["truncated"] != bool(record["omitted_refs"]) or (record["truncated"] and not record["reason"].strip()):
            _fail("INVALID_TRUNCATION", "$", "truncation requires omitted refs and reason, and vice versa")
        refs = record["core_refs"] + record["context_refs"] + record["omitted_refs"]
        if len({r["unit_ref"]["vault_id"] for r in refs}) != 1:
            _fail("MIXED_VAULT", "$", "windows cannot span vaults")
        if kind == "retrieval_window":
            if record["input_tokens"] > record["max_tokens"]:
                _fail("INVALID_BUDGET", "$.input_tokens", "index input exceeds declared tokenizer budget")
            scopes = {tuple(r["unit_ref"][k] for k in ("vault_id", "resource_id", "artifact_revision", "unit_set_id")) for r in refs}
            if len(scopes) != 1:
                _fail("MIXED_SOURCE", "$", "retrieval windows must use one source version/set")
    if kind == "work":
        expected_contract = {"knowledge_build": "hermes-knowledge-build/v4", "entity_extraction": "hermes-entity-extraction/v1", "table_qa": "hermes-table-qa/v1"}[record["task_type"]]
        if record["task_contract"] != expected_contract:
            _fail("TASK_CONTRACT_MISMATCH", "$.task_contract", "task type requires its declared contract version")
        if record["status"] in ("blocked", "failed", "skipped") and not record["reason"].strip():
            _fail("REASON_REQUIRED", "$.reason", "non-success terminal/blocked states need a reason")
        if record["status"] == "completed" and (not record["inspections"] or record["deferred"]):
            _fail("INVALID_COMPLETION", "$", "completed work needs inspections and no unresolved deferred range")
        if record["status"] == "completed" and not record["outputs"] and not record["reason"].strip():
            _fail("REASON_REQUIRED", "$.reason", "zero-output completion requires a finding")
    if kind == "knowledge_build":
        _unique([c["id"] for c in record["candidates"]], "$.candidates")
        _unique([o["path"] for o in record["outputs"]], "$.outputs")
        paths = {o["path"] for o in record["outputs"]}
        linked = set()
        for candidate in record["candidates"]:
            if candidate["decision"] in ("skip", "defer") and candidate["outputs"]:
                _fail("INVALID_DECISION", "$.candidates", "skip/defer cannot claim outputs")
            if candidate["decision"] in ("create", "update", "reuse") and not candidate["outputs"]:
                _fail("OUTPUT_REQUIRED", "$.candidates", "write/reuse decisions require outputs")
            linked.update(candidate["outputs"])
        if paths != linked:
            _fail("OUTPUT_MISMATCH", "$.outputs", "candidate and output paths must agree")
        for output in record["outputs"]:
            if output["path"].split("/")[0] not in ("20_Notes", "30_Cards", "40_Concepts", "50_Projects"):
                _fail("INVALID_OUTPUT_PATH", "$.outputs", "only governed knowledge directories allowed")
        if record["execution_status"] == "completed" and not record["outputs"] and not record["reason"].strip():
            _fail("REASON_REQUIRED", "$.reason", "zero-output completion requires a finding")


def validate_references(kind: str, record: Mapping[str, Any], units: list[dict[str, Any]]) -> None:
    """Resolve exact references against supplied records; no live source/ACL check.

    Reading and knowledge records may cite multiple documents/versions. Retrieval
    windows may not. Matching text or unit_id alone is never enough to resolve.
    """
    validate_record(kind, record)
    index: dict[bytes, dict[str, Any]] = {}
    for unit in units:
        validate_record("unit", unit)
        key = canonical_json(unit["ref"])
        if key in index:
            _fail("DUPLICATE", "$.units", "duplicate full unit_ref")
        index[key] = unit

    def interval(source: dict[str, Any]) -> tuple[bytes, int, int]:
        key = canonical_json(source["unit_ref"])
        unit = index.get(key)
        if unit is None:
            _fail("UNRESOLVED_REFERENCE", "$", "exact source version/unit not present")
        loc = unit["locator"]
        if loc["kind"] == "asset":
            if source["span"] is not None:
                _fail("INVALID_RANGE", "$", "whole-asset references cannot carry text spans")
            return key, 0, 1
        core = loc["span"]
        selected = source["span"] or core
        if selected["start"] < core["start"] or selected["end"] > core["end"]:
            _fail("OUTSIDE_UNIT", "$", "reference span exceeds source unit core")
        return key, selected["start"], selected["end"]

    for _, item in _walk(record):
        if isinstance(item, dict) and set(item) == {"unit_ref", "span"}:
            interval(item)
    all_refs = [item["unit_ref"] for _, item in _walk(record) if isinstance(item, dict) and set(item) == {"unit_ref", "span"}]
    if len({item["vault_id"] for item in all_refs}) > 1:
        _fail("MIXED_VAULT", "$", "a single record cannot span vaults")
    if kind == "provider_extension":
        if len({tuple(r[k] for k in ("vault_id", "resource_id", "artifact_revision", "unit_set_id")) for r in all_refs}) != 1:
            _fail("MIXED_SOURCE", "$", "a candidate extension must identify one source version/set")
    if kind == "unit_set":
        if set(record["unit_ids"]) != {u["ref"]["unit_id"] for u in units}:
            _fail("UNIT_SET_MISMATCH", "$", "manifest and supplied unit IDs differ")
        for unit in units:
            identity = record["identity"]
            expected = {"vault_id": identity["vault_id"], "resource_id": identity["resource_id"], "artifact_revision": record["artifact_revision"], "unit_set_id": record["unit_set_id"]}
            if any(unit["ref"][k] != v for k, v in expected.items()) or any(unit[k] != identity[k] for k in ("document_id", "version_id")):
                _fail("UNIT_SET_MISMATCH", "$", "unit belongs to a different source/version/set")
    if kind == "unit":
        for field in ("prev_ref", "next_ref"):
            if record[field] is not None and canonical_json(record[field]) not in index:
                _fail("UNRESOLVED_REFERENCE", "$." + field, "neighbor not present")

    def covered(source: dict[str, Any], support: list[dict[str, Any]]) -> bool:
        key, start, end = interval(source)
        ranges = sorted((lo, hi) for k, lo, hi in map(interval, support) if k == key)
        cursor = start
        for lo, hi in ranges:
            if lo > cursor:
                break
            cursor = max(cursor, hi)
            if cursor >= end:
                return True
        return False

    if kind == "work":
        inspected = [item["source_ref"] for item in record["inspections"]]
        for item in inspected + [d["source_ref"] for d in record["deferred"]]:
            if not covered(item, record["target_refs"]):
                _fail("OUTSIDE_TASK", "$", "inspection/defer outside target range")
        if record["status"] == "completed" and not all(covered(r, inspected) for r in record["target_refs"]):
            _fail("INCOMPLETE_COVERAGE", "$", "context or partial reading cannot complete the target")
    if kind == "knowledge_build":
        inspected = [item["source_ref"] for item in record["inspections"]]
        for candidate in record["candidates"]:
            if not all(covered(r, inspected) for r in candidate["support_refs"]):
                _fail("UNINSPECTED_SUPPORT", "$", "candidate uses unread evidence")
        for output in record["outputs"]:
            candidate_refs = [r for c in record["candidates"] if output["path"] in c["outputs"] for r in c["support_refs"]]
            if not all(covered(r, output["support_refs"]) for r in candidate_refs) or not all(covered(r, candidate_refs) for r in output["support_refs"]):
                _fail("PROVENANCE_MISMATCH", "$", "output support must equal union of its candidates' support")
            for inspection in record["inspections"]:
                if inspection["qa"] != "needs-qa":
                    continue
                key, lo, hi = interval(inspection["source_ref"])
                affected = any(k == key and a < hi and lo < b for k, a, b in map(interval, output["support_refs"]))
                if affected and output["status"] != "draft":
                    _fail("QA_REQUIRES_DRAFT", "$", "affected output must retain draft status")
