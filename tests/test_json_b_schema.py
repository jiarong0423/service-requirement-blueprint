from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import uuid

from jsonschema import Draft202012Validator, FormatChecker
import pytest


SCHEMA_PATH = Path("app/schemas/json-b.v1.schema.json")


def _schema() -> dict[str, object]:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return schema


def _valid_document() -> dict[str, object]:
    identifiers = [str(uuid.uuid4()) for _ in range(13)]
    return {
        "schema_version": "json-b/1.0",
        "document_type": "contractor_proposal",
        "document_id": identifiers[0],
        "version": 1,
        "generated_at": "2026-10-06T02:00:00Z",
        "case_id": identifiers[1],
        "source_json_a": {
            "artifact_id": identifiers[2],
            "document_id": identifiers[2],
            "version": 1,
            "schema_version": "json-a/1.1",
            "sha256": "a" * 64,
            "byte_length": 128,
        },
        "requirement_understanding": "Build a Unicode-safe service site for café visitors.",
        "included_scope": [{"item_id": identifiers[3], "description": "Responsive service page"}],
        "excluded_scope": [{"item_id": identifiers[4], "description": "Payment processing"}],
        "pages": [{"page_id": identifiers[5], "title": "Home", "deliverables": ["Structured content"]}],
        "functions": [{"function_id": identifiers[6], "name": "Inquiry form", "rules": ["Validate required fields"]}],
        "data_and_permissions": [{"role_code": "site_admin", "data_categories": ["inquiries"], "permissions": ["read", "delete"]}],
        "third_party_services": [{"service_code": "hosting", "responsible_party": "client", "notes": "Selected separately"}],
        "milestones": [{"milestone_id": identifiers[7], "title": "Prototype", "deliverables": ["Local demonstration"], "estimated_working_days": 5}],
        "effort": {"estimated_hours": 40, "difficulty": "medium", "assumptions": ["Client supplies final copy"]},
        "fee_items": [
            {
                "fee_id": identifiers[8],
                "description": "Implementation",
                "billing_kind": "one_time",
                "cost_source": "contractor",
                "currency_code": "XTS",
                "amount": 5000,
                "billing_period": None,
                "included_in_quote": True,
            },
            {
                "fee_id": identifiers[9],
                "description": "Hosting",
                "billing_kind": "recurring",
                "cost_source": "third_party",
                "currency_code": "XTS",
                "amount": 100,
                "billing_period": "monthly",
                "included_in_quote": False,
            },
        ],
        "computed_fee_totals": [
            {
                "billing_kind": "one_time",
                "cost_source": "contractor",
                "currency_code": "XTS",
                "amount": 5000,
                "billing_period": None,
                "included_in_quote": True,
            },
            {
                "billing_kind": "recurring",
                "cost_source": "third_party",
                "currency_code": "XTS",
                "amount": 100,
                "billing_period": "monthly",
                "included_in_quote": False,
            },
        ],
        "responsibilities": {"client": ["Provide final copy"], "contractor": ["Implement the agreed scope"]},
        "acceptance": {
            "criteria": [{"criterion_id": identifiers[10], "description": "Form validates input", "verification_method": "Submit an incomplete form"}],
            "review_period_days": 7,
            "silence_is_acceptance": False,
        },
        "remediation": {
            "included_corrections": ["Repair deviations from accepted criteria"],
            "excluded_changes": ["New features"],
            "reinspection_required": True,
        },
        "maintenance": {
            "warranty_days": 30,
            "included_support": ["Defect correction"],
            "excluded_support": ["Content updates"],
        },
        "quote_expires_at": "2026-11-06T02:00:00Z",
    }


def _errors(document: dict[str, object]) -> list[object]:
    return list(Draft202012Validator(_schema(), format_checker=FormatChecker()).iter_errors(document))


def test_json_b_schema_accepts_global_currency_and_unicode() -> None:
    assert _errors(_valid_document()) == []


def test_runtime_registers_json_b_schema_path(demo_runtime) -> None:
    app, _instance_root, _passwords = demo_runtime
    assert Path(app.config["JSON_B_SCHEMA_PATH"]).resolve() == SCHEMA_PATH.resolve()


def test_every_json_b_object_schema_rejects_unknown_fields() -> None:
    def walk(node: object) -> None:
        if isinstance(node, dict):
            node_type = node.get("type")
            if node_type == "object" or isinstance(node_type, list) and "object" in node_type:
                assert node.get("additionalProperties") is False
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(_schema())


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("amount", 1.5),
        ("amount", "100"),
        ("amount", -1),
        ("amount", True),
        ("currency_code", "usd"),
        ("currency_code", "US"),
    ),
)
def test_json_b_fee_rejects_non_integer_or_invalid_currency(field: str, value: object) -> None:
    document = _valid_document()
    document["fee_items"][0][field] = value  # type: ignore[index]
    assert _errors(document)


@pytest.mark.parametrize(
    ("billing_kind", "billing_period"),
    (("one_time", "monthly"), ("recurring", None)),
)
def test_json_b_fee_requires_period_only_for_recurring(
    billing_kind: str, billing_period: str | None
) -> None:
    document = _valid_document()
    document["fee_items"][0]["billing_kind"] = billing_kind  # type: ignore[index]
    document["fee_items"][0]["billing_period"] = billing_period  # type: ignore[index]
    assert _errors(document)


def test_json_b_rejects_unknown_fields_at_top_and_nested_levels() -> None:
    top = _valid_document()
    top["authority"] = "client"
    assert _errors(top)

    nested = _valid_document()
    nested["source_json_a"]["actor_role"] = "owner"  # type: ignore[index]
    assert _errors(nested)

    fee = _valid_document()
    fee["fee_items"][0]["tax"] = 1  # type: ignore[index]
    assert _errors(fee)


def test_json_b_computed_total_uses_same_currency_integer_and_period_contract() -> None:
    document = _valid_document()
    total = document["computed_fee_totals"][0]  # type: ignore[index]
    total["amount"] = 2.5
    assert _errors(document)

    document = _valid_document()
    total = document["computed_fee_totals"][0]  # type: ignore[index]
    total["billing_period"] = "yearly"
    assert _errors(document)


def test_json_b_requires_fixed_identity_and_no_silence_acceptance() -> None:
    for field in ("document_id", "case_id", "source_json_a"):
        document = _valid_document()
        del document[field]
        assert _errors(document)

    document = _valid_document()
    document["acceptance"]["silence_is_acceptance"] = True  # type: ignore[index]
    assert _errors(document)


def test_json_b_example_is_not_mutated_by_validation() -> None:
    document = _valid_document()
    original = deepcopy(document)
    assert _errors(document) == []
    assert document == original
