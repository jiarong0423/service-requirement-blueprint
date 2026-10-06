from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.config import SUPPORTED_LOCALES

PLACEHOLDER_PATTERN = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")


class CatalogError(RuntimeError):
    pass


def _reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CatalogError("duplicate_catalog_key")
        result[key] = value
    return result


def load_catalogs(root: Path) -> dict[str, dict[str, str]]:
    expected_files = {f"catalog.{locale}.json" for locale in SUPPORTED_LOCALES}
    actual_files = {path.name for path in root.glob("catalog.*.json") if path.is_file()}
    if actual_files != expected_files:
        raise CatalogError("catalog_set_invalid")
    catalogs: dict[str, dict[str, str]] = {}
    for locale in SUPPORTED_LOCALES:
        path = root / f"catalog.{locale}.json"
        try:
            raw = path.read_bytes()
            if raw.startswith(b"\xef\xbb\xbf"):
                raise CatalogError("catalog_bom_forbidden")
            parsed = json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=_reject_duplicate)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CatalogError("catalog_invalid") from error
        if not isinstance(parsed, dict) or not all(
            isinstance(key, str) and isinstance(value, str) and value.strip() and value != key
            for key, value in parsed.items()
        ):
            raise CatalogError("catalog_value_invalid")
        catalogs[locale] = parsed
    for catalog in catalogs.values():
        for value in catalog.values():
            try:
                value.format(**{name[1:-1]: name for name in PLACEHOLDER_PATTERN.findall(value)})
            except (KeyError, ValueError) as error:
                raise CatalogError("catalog_placeholder_invalid") from error
    return catalogs


def translate(catalogs: dict[str, dict[str, str]], locale: str, key: str, **values: object) -> str:
    if locale not in SUPPORTED_LOCALES or key not in catalogs[locale]:
        raise CatalogError("translation_missing")
    try:
        return catalogs[locale][key].format(**values)
    except (KeyError, ValueError) as error:
        raise CatalogError("translation_format_invalid") from error
