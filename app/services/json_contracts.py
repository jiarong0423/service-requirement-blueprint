from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from app.config import MAX_JSON_BYTES


class JsonContractError(ValueError):
    pass


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise JsonContractError("duplicate_key")
        result[key] = value
    return result


def decode_strict_json(raw: bytes) -> Any:
    if not raw or len(raw) > MAX_JSON_BYTES:
        raise JsonContractError("file_size_invalid")
    if raw.startswith(b"\xef\xbb\xbf"):
        raise JsonContractError("bom_forbidden")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise JsonContractError("invalid_utf8") from error
    if "\ufffd" in text:
        raise JsonContractError("replacement_character_forbidden")
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicates)
    except json.JSONDecodeError as error:
        raise JsonContractError("invalid_json") from error


def load_schema(path: Path) -> dict[str, Any]:
    schema = decode_strict_json(path.read_bytes())
    if not isinstance(schema, dict):
        raise JsonContractError("schema_invalid")
    Draft202012Validator.check_schema(schema)
    return schema


def validate_json_a(document: dict[str, Any], schema_path: Path) -> None:
    validator = Draft202012Validator(load_schema(schema_path), format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(document), key=lambda error: list(error.absolute_path))
    if errors:
        raise JsonContractError("schema_validation_failed")


def validate_json_b(document: dict[str, Any], schema_path: Path) -> None:
    validator = Draft202012Validator(load_schema(schema_path), format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(document), key=lambda error: list(error.absolute_path))
    if errors:
        raise JsonContractError("schema_validation_failed")


def encode_json_once(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()
