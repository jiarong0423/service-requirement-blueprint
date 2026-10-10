from __future__ import annotations

from collections.abc import Mapping

from app.i18n import translate
from app.presentation_semantics import (
    CONTRACTOR_VERBATIM_CODE_FIELDS,
    ENUM_PRESENTATIONS,
    HUMANIZED_CODE_FIELDS,
    enum_presentation,
    humanize_code,
    is_known_enum_field,
)


class PresentationError(RuntimeError):
    pass


STATE_LABEL_KEYS: dict[str, str] = {
    "REQUIREMENT_DRAFT": "ui.state.requirement_draft",
    "INVITATION_PENDING": "ui.state.invitation_pending",
    "WAITING_JSON_A_IMPORT": "ui.state.waiting_requirement_import",
    "CASE_ACTIVE": "ui.state.case_active",
    "JSON_B_PRIVATE_DRAFT": "ui.state.proposal_draft",
    "JSON_B_REVIEW": "ui.state.proposal_review",
    "CANDIDATE_REVIEW": "ui.state.confirmation_review",
    "FORMAL": "ui.state.confirmed",
    "DELIVERED": "ui.state.delivered",
    "UNDER_ACCEPTANCE": "ui.state.under_acceptance",
    "REMEDIATION": "ui.state.remediation",
    "REINSPECTION": "ui.state.reinspection",
    "ACCEPTED": "ui.state.accepted",
    "DISPUTED": "ui.state.disputed",
}

STAGE_LABEL_KEYS: dict[str, str] = {
    "REQUIREMENT_DRAFT": "ui.stage.clarify",
    "INVITATION_PENDING": "ui.stage.clarify",
    "WAITING_JSON_A_IMPORT": "ui.stage.clarify",
    "CASE_ACTIVE": "ui.stage.clarify",
    "JSON_B_PRIVATE_DRAFT": "ui.stage.clarify",
    "JSON_B_REVIEW": "ui.stage.confirm",
    "CANDIDATE_REVIEW": "ui.stage.confirm",
    "FORMAL": "ui.stage.deliver",
    "DELIVERED": "ui.stage.deliver",
    "UNDER_ACCEPTANCE": "ui.stage.deliver",
    "REMEDIATION": "ui.stage.deliver",
    "REINSPECTION": "ui.stage.deliver",
    "ACCEPTED": "ui.stage.deliver",
    "DISPUTED": "ui.stage.deliver",
}

ISSUE_STATE_LABEL_KEYS: dict[str, str] = {
    "OPEN": "ui.issue_state.open",
    "REVISION_REQUESTED": "ui.issue_state.revision_requested",
    "PROPOSED": "ui.issue_state.proposed",
    "OUT_OF_SCOPE_PROPOSED": "ui.issue_state.proposed",
    "RESOLVED": "ui.issue_state.resolved",
    "OUT_OF_SCOPE": "ui.issue_state.resolved",
}

ISSUE_KIND_LABEL_KEYS: dict[str, str] = {
    "CLARIFICATION": "ui.issue_kind.scope_question",
    "JSON_B_OBJECTION": "ui.issue_kind.proposal_objection",
}

VALUE_LABEL_KEYS: dict[str, dict[object, str]] = {
    field_name: {
        value: presentation.catalog_key for value, presentation in presentations.items()
    }
    for field_name, presentations in ENUM_PRESENTATIONS.items()
}

INVITATION_STATE_LABEL_KEYS: dict[str, str] = {
    "PENDING": "status.invitation_pending",
    "ACCEPTED": "status.invitation_accepted",
    "REJECTED": "status.invitation_rejected",
    "REVOKED": "status.invitation_revoked",
    "EXPIRED": "status.invitation_expired",
}

INVITATION_TASK_LABEL_KEYS: dict[tuple[str, str], str] = {
    ("PENDING", "client"): "ui.invitation.task_owner_wait_response",
    ("ACCEPTED", "client"): "ui.invitation.task_owner_wait_import",
    ("REJECTED", "client"): "ui.invitation.task_verify",
    ("REVOKED", "client"): "ui.invitation.task_verify",
    ("EXPIRED", "client"): "ui.invitation.task_verify",
    ("PENDING", "contractor"): "ui.invitation.task_contractor_review",
    ("ACCEPTED", "contractor"): "ui.invitation.task_contractor_import",
    ("REJECTED", "contractor"): "ui.invitation.task_unavailable",
    ("REVOKED", "contractor"): "ui.invitation.task_unavailable",
    ("EXPIRED", "contractor"): "ui.invitation.task_unavailable",
}

DELIVERY_TASK_KEYS: dict[tuple[str, str], str] = {
    ("FORMAL", "contractor"): "ui.delivery.task_submit",
    ("FORMAL", "client"): "ui.delivery.task_wait_delivery",
    ("DELIVERED", "client"): "ui.delivery.task_begin_inspection",
    ("DELIVERED", "contractor"): "ui.delivery.task_wait_inspection",
    ("UNDER_ACCEPTANCE", "client"): "ui.delivery.task_complete_inspection",
    ("UNDER_ACCEPTANCE", "contractor"): "ui.delivery.task_wait_inspection",
    ("REINSPECTION", "client"): "ui.delivery.task_complete_inspection",
    ("REINSPECTION", "contractor"): "ui.delivery.task_wait_inspection",
    ("REMEDIATION", "contractor"): "ui.delivery.task_submit_remediation",
    ("REMEDIATION", "client"): "ui.delivery.task_wait_remediation",
    ("ACCEPTED", "client"): "ui.delivery.task_export",
    ("ACCEPTED", "contractor"): "ui.delivery.task_export",
    ("DISPUTED", "client"): "ui.delivery.task_preserve",
    ("DISPUTED", "contractor"): "ui.delivery.task_preserve",
}

SCREEN_COPY_KEYS: dict[str, dict[str, str]] = {
    "case_room": {
        "task_confirmation": "ui.case_room.task_confirmation",
        "task_formal": "ui.case_room.task_formal",
        "task_proposal": "ui.case_room.task_proposal",
        "task_prepare": "ui.case_room.task_prepare",
        "task_resolve": "ui.case_room.task_resolve",
        "task_wait_proposal": "ui.case_room.task_wait_proposal",
        "proposal_heading": "ui.case_room.proposal_heading",
        "open_confirmed": "ui.case_room.open_confirmed",
        "open_confirmation": "ui.case_room.open_confirmation",
        "review_proposal": "ui.case_room.review_proposal",
        "prepare_proposal": "ui.case_room.prepare_proposal",
        "continue_revision": "ui.case_room.continue_revision",
        "no_proposal": "ui.case_room.no_proposal",
        "reload": "ui.common.reload",
        "verify_requirement": "ui.case_room.verify_requirement",
    },
    "proposal_review": {
        "status_ready": "ui.proposal.status_ready",
        "status_review": "ui.proposal.status_review",
        "task_open_confirmation": "ui.proposal.task_open_confirmation",
        "task_previous": "ui.proposal.task_previous",
        "task_wait_revision": "ui.proposal.task_wait_revision",
        "task_client": "ui.proposal.task_client",
        "task_blocked": "ui.proposal.task_blocked",
        "task_contractor": "ui.proposal.task_contractor",
        "eyebrow": "ui.proposal.eyebrow",
        "title": "ui.proposal.title",
        "current": "ui.proposal.current",
        "previous": "ui.proposal.previous",
        "saved_notice": "ui.proposal.saved_notice",
        "read_only_notice": "ui.proposal.read_only_notice",
        "download": "ui.proposal.download",
        "create_confirmation": "ui.proposal.create_confirmation",
        "revise": "ui.proposal.revise",
        "continue_revision": "ui.proposal.continue_revision",
        "verify": "ui.proposal.verify",
    },
    "confirmation": {
        "status_confirmed": "ui.confirmation.status_confirmed",
        "status_invalidated": "ui.confirmation.status_invalidated",
        "status_review": "ui.confirmation.status_review",
        "task_delivery": "ui.confirmation.task_delivery",
        "task_invalidated": "ui.confirmation.task_invalidated",
        "task_acknowledge": "ui.confirmation.task_acknowledge",
        "task_confirm": "ui.confirmation.task_confirm",
        "task_wait": "ui.confirmation.task_wait",
        "task_review": "ui.confirmation.task_review",
        "eyebrow": "ui.confirmation.eyebrow",
        "title": "ui.confirmation.title",
        "description": "ui.confirmation.description",
        "not_final": "ui.confirmation.not_final",
        "same_saved": "ui.confirmation.same_saved",
        "invalidated": "ui.confirmation.invalidated",
        "confirmed": "ui.confirmation.confirmed",
        "acknowledge_statement": "ui.confirmation.acknowledge_statement",
        "acknowledge_action": "ui.confirmation.acknowledge_action",
        "confirm_statement": "ui.confirmation.confirm_statement",
        "waiting": "ui.confirmation.waiting",
        "next_description": "ui.confirmation.next_description",
        "invalidated_description": "ui.confirmation.invalidated_description",
        "files": "ui.confirmation.files",
        "open_pdf": "ui.confirmation.open_pdf",
        "download_pdf": "ui.confirmation.download_pdf",
        "open_diagram": "ui.confirmation.open_diagram",
        "verify": "ui.confirmation.verify",
    },
    "confirmed": {
        "status": "ui.confirmed.status",
        "task": "ui.confirmed.task",
        "eyebrow": "ui.confirmed.eyebrow",
        "title": "ui.confirmed.title",
        "description": "ui.confirmed.description",
        "banner": "ui.confirmed.banner",
        "next_title": "ui.confirmed.next_title",
        "next_description": "ui.confirmed.next_description",
        "files": "ui.confirmed.files",
        "open_pdf": "ui.confirmed.open_pdf",
        "download_pdf": "ui.confirmed.download_pdf",
        "open_diagram": "ui.confirmed.open_diagram",
        "verify": "ui.confirmed.verify",
    },
    "delivery": {
        "banner": "ui.delivery.banner",
        "reload": "ui.common.reload",
        "verify_confirmed": "ui.delivery.verify_confirmed",
        "evidence_eyebrow": "ui.delivery.evidence_eyebrow",
        "evidence_title": "ui.delivery.evidence_title",
        "evidence_description": "ui.delivery.evidence_description",
        "evidence_download": "ui.delivery.evidence_download",
        "verify_evidence": "ui.delivery.verify_evidence",
    },
    "invitation": {
        "status_ready": "ui.invitation.status_ready",
        "task_invite": "ui.invitation.task_invite",
        "task_open_case": "ui.invitation.task_open_case",
        "task_verify": "ui.invitation.task_verify",
    },
}


def _mapped_key(mapping: Mapping[str, str], value: str, fallback: str) -> str:
    return mapping.get(value, fallback)


def state_label(catalogs: dict[str, dict[str, str]], locale: str, state: str) -> str:
    return translate(catalogs, locale, _mapped_key(STATE_LABEL_KEYS, state, "ui.status.unavailable"))


def stage_label(catalogs: dict[str, dict[str, str]], locale: str, state: str) -> str:
    return translate(catalogs, locale, _mapped_key(STAGE_LABEL_KEYS, state, "ui.stage.unavailable"))


def issue_state_label(catalogs: dict[str, dict[str, str]], locale: str, state: str) -> str:
    return translate(catalogs, locale, _mapped_key(ISSUE_STATE_LABEL_KEYS, state, "ui.status.unavailable"))


def issue_kind_label(catalogs: dict[str, dict[str, str]], locale: str, kind: str) -> str:
    return translate(catalogs, locale, _mapped_key(ISSUE_KIND_LABEL_KEYS, kind, "ui.issue_kind.unavailable"))


def value_label(
    catalogs: dict[str, dict[str, str]],
    locale: str,
    field_name: str | None,
    value: str,
    contractor_authored: bool = False,
) -> str:
    presentation = enum_presentation(field_name, value)
    if presentation is not None:
        return translate(catalogs, locale, presentation.catalog_key)
    if is_known_enum_field(field_name):
        return translate(catalogs, locale, "ui.status.unavailable")
    if contractor_authored and field_name in CONTRACTOR_VERBATIM_CODE_FIELDS:
        return value
    if field_name in HUMANIZED_CODE_FIELDS:
        return humanize_code(value)
    return value


def invitation_state_label(catalogs: dict[str, dict[str, str]], locale: str, state: str) -> str:
    return translate(catalogs, locale, _mapped_key(INVITATION_STATE_LABEL_KEYS, state, "ui.status.unavailable"))


def invitation_task(catalogs: dict[str, dict[str, str]], locale: str, state: str, role: str) -> str:
    key = INVITATION_TASK_LABEL_KEYS.get((state, role), "ui.invitation.task_unavailable")
    return translate(catalogs, locale, key)


def delivery_task(
    catalogs: dict[str, dict[str, str]],
    locale: str,
    state: str,
    role: str,
    *,
    delivery_limit_reached: bool = False,
) -> str:
    if state == "REMEDIATION" and delivery_limit_reached:
        return translate(catalogs, locale, "ui.delivery.task_limit_reached")
    key = DELIVERY_TASK_KEYS.get((state, role), "ui.delivery.task_unavailable")
    return translate(catalogs, locale, key)


def screen_copy(
    catalogs: dict[str, dict[str, str]], locale: str, screen: str, slot: str, **values: object
) -> str:
    screen_map = SCREEN_COPY_KEYS.get(screen)
    if screen_map is None or slot not in screen_map:
        raise PresentationError("presentation_slot_missing")
    return translate(catalogs, locale, screen_map[slot], **values)
