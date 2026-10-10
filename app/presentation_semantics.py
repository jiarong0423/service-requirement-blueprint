from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EnumPresentation:
    catalog_key: str
    english_label: str


ENUM_PRESENTATIONS: dict[str, dict[object, EnumPresentation]] = {
    "requirement_level": {
        "required": EnumPresentation("ui.value.required", "Required"),
        "desired": EnumPresentation("ui.value.desired", "Desired"),
        "excluded": EnumPresentation("ui.value.excluded", "Excluded"),
        "undecided": EnumPresentation("ui.value.undecided", "Undecided"),
    },
    "status": {
        "existing": EnumPresentation("ui.value.existing", "Already available"),
        "to_select": EnumPresentation("ui.value.to_select", "To be selected"),
        "contractor_proposal": EnumPresentation(
            "ui.value.contractor_proposal", "Contractor to propose"
        ),
        "not_needed": EnumPresentation("ui.value.not_needed", "Not needed"),
    },
    "budget_status": {
        "known": EnumPresentation("ui.value.known", "Known"),
        "range": EnumPresentation("ui.value.range", "Range"),
        "undecided": EnumPresentation("ui.value.undecided", "Undecided"),
        "contractor_proposal_requested": EnumPresentation(
            "ui.value.contractor_proposal_requested", "Contractor estimate requested"
        ),
    },
    "resolution": {
        "undecided": EnumPresentation("ui.value.undecided", "Undecided"),
        "contractor_proposal_requested": EnumPresentation(
            "ui.value.contractor_proposal_requested", "Contractor proposal requested"
        ),
    },
    "responsible_party": {
        "client": EnumPresentation("ui.value.client", "Client"),
        "contractor": EnumPresentation("ui.value.contractor", "Contractor"),
    },
    "difficulty": {
        "low": EnumPresentation("ui.value.low", "Low"),
        "medium": EnumPresentation("ui.value.medium", "Medium"),
        "high": EnumPresentation("ui.value.high", "High"),
    },
    "billing_kind": {
        "one_time": EnumPresentation("ui.value.one_time", "One-time"),
        "recurring": EnumPresentation("ui.value.recurring", "Recurring"),
    },
    "cost_source": {
        "contractor": EnumPresentation("ui.value.contractor", "Contractor"),
        "third_party": EnumPresentation("ui.value.third_party", "Third party"),
    },
    "billing_period": {
        "monthly": EnumPresentation("ui.value.monthly", "Monthly"),
        "quarterly": EnumPresentation("ui.value.quarterly", "Quarterly"),
        "yearly": EnumPresentation("ui.value.yearly", "Yearly"),
    },
}

HUMANIZED_CODE_FIELDS = frozenset(
    {
        "id",
        "feature_code",
        "role_code",
        "service_code",
        "asset_code",
        "section_code",
    }
)

CONTRACTOR_VERBATIM_CODE_FIELDS = frozenset({"role_code", "service_code"})

HIDDEN_PRESENTATION_FIELDS = frozenset({"field_pointer"})


def enum_presentation(field_name: str | None, value: object) -> EnumPresentation | None:
    if field_name is None:
        return None
    return ENUM_PRESENTATIONS.get(field_name, {}).get(value)


def is_known_enum_field(field_name: str | None) -> bool:
    return field_name in ENUM_PRESENTATIONS


def humanize_code(value: object) -> str:
    return str(value).replace("_", " ").replace("-", " ").strip().title()


def human_scalar(
    field_name: str | None,
    value: object,
    *,
    contractor_authored: bool = False,
) -> str:
    presentation = enum_presentation(field_name, value)
    if presentation is not None:
        return presentation.english_label
    if is_known_enum_field(field_name):
        return "Unavailable"
    if contractor_authored and field_name in CONTRACTOR_VERBATIM_CODE_FIELDS:
        return str(value)
    if field_name in HUMANIZED_CODE_FIELDS:
        return humanize_code(value)
    return str(value)
