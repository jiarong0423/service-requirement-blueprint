from __future__ import annotations

import json
import hmac
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, Response, abort, current_app, g, redirect, render_template, request, session, url_for

from app.config import MAX_JSON_BYTES, SUPPORTED_LOCALES
from app.db import get_db, immediate_transaction
from app.security.auth import (
    KDF_RETRY_AFTER_SECONDS,
    PasswordVerification,
    current_user_required,
    verify_password,
)
from app.security.csrf import new_nonce
from app.security.permissions import load_case_party, load_owner_case, require_admin, require_member
from app.services.artifact_store import ArtifactStoreError, MAX_JSON_ARTIFACT_BYTES, read_verified
from app.services.blueprint import blueprint_data, render_svg
from app.services.candidate_workflow import (
    CandidateWorkflowError,
    create_candidate,
    load_candidate_for_party,
    read_candidate_component,
)
from app.services.case_workflow import create_case, generate_json_a, load_draft, save_section, utc_now
from app.services.discussion_workflow import (
    DiscussionWorkflowError,
    create_issue,
    list_collaboration,
    post_message,
    transition_issue,
)
from app.services.document_integrity import DocumentIntegrityError
from app.services.delivery_workflow import (
    DeliveryWorkflowError,
    add_dispute_position,
    begin_inspection,
    load_delivery_dashboard,
    open_dispute,
    read_delivery_record,
    submit_delivery,
    submit_inspection,
)
from app.services.evidence_export import (
    EvidenceExportError,
    load_delivery_dashboard_with_export_status,
    prepare_evidence_export,
    read_evidence_export,
)
from app.services.formalization_workflow import (
    FormalizationWorkflowError,
    acknowledge_candidate,
    confirm_candidate,
    create_separate_proposal,
    load_confirmation_view,
    load_formal_version,
    read_formal_source,
    request_candidate_revision,
    withdraw_confirmation,
)
from app.services.invitation_workflow import (
    InvitationWorkflowError,
    accept_invitation,
    cancel_accepted_invitation_before_import,
    create_invitation,
    import_json_a,
    reject_invitation,
    revoke_invitation,
)
from app.services.json_contracts import JsonContractError, decode_strict_json, sha256_bytes, validate_json_a
from app.services.json_b_workflow import (
    BUSINESS_FIELDS,
    JsonBWorkflowError,
    export_private_draft,
    load_private_draft,
    load_private_export,
    load_review,
    read_review_bytes,
    save_private_draft,
    submit_private_export,
    validate_objection_pointer,
)


CLIENT_CATALOG_KEYS = ("error.conflict_retry", "error.required_field", "notice.local_only")


def _quote_has_expired(value: str) -> bool:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) <= datetime.now(timezone.utc)


def _uploaded_json_a() -> bytes:
    uploaded = request.files.get("json_a_file")
    if uploaded is None or not uploaded.filename:
        abort(422, description="error.invalid_json")
    raw = uploaded.stream.read(MAX_JSON_BYTES + 1)
    if not raw or len(raw) > MAX_JSON_BYTES:
        abort(422, description="error.invalid_json")
    return raw


def _uploaded_json_b() -> bytes:
    uploaded = request.files.get("json_b_file")
    if uploaded is None or not uploaded.filename:
        abort(422, description="error.invalid_json")
    raw = uploaded.stream.read(MAX_JSON_BYTES + 1)
    if not raw or len(raw) > MAX_JSON_BYTES:
        abort(422, description="error.invalid_json")
    return raw


def _empty_json_b_business_draft() -> str:
    draft = {
        "requirement_understanding": "",
        "included_scope": [],
        "excluded_scope": [],
        "pages": [],
        "functions": [],
        "data_and_permissions": [],
        "third_party_services": [],
        "milestones": [],
        "effort": {"estimated_hours": 0, "difficulty": "low", "assumptions": []},
        "fee_items": [],
        "responsibilities": {"client": [], "contractor": []},
        "acceptance": {
            "criteria": [],
            "review_period_days": 1,
            "silence_is_acceptance": False,
        },
        "remediation": {
            "included_corrections": [],
            "excluded_changes": [],
            "reinspection_required": True,
        },
        "maintenance": {"warranty_days": 0, "included_support": [], "excluded_support": []},
        "quote_expires_at": "",
    }
    return json.dumps(draft, ensure_ascii=False, indent=2)


def _active_json_a(case_id: str, artifact_id: str | None):
    if not artifact_id:
        abort(404)
    artifact = get_db().execute(
        "SELECT id, version_no, visibility, relative_path, sha256, byte_length, schema_version "
        "FROM artifacts WHERE id = ? AND case_id = ? AND kind = 'json_a' AND lifecycle = 'active'",
        (artifact_id, case_id),
    ).fetchone()
    if artifact is None:
        abort(404)
    return artifact


def _verify_preview_bytes(case_id: str, artifact_id: str | None, raw: bytes) -> tuple[object, dict[str, object]]:
    artifact = _active_json_a(case_id, artifact_id)
    try:
        document = decode_strict_json(raw)
        if not isinstance(document, dict):
            raise JsonContractError("schema_validation_failed")
        validate_json_a(document, Path(current_app.config["SCHEMA_PATH"]))
        stored = read_verified(
            Path(current_app.config["DATA_ROOT"]), artifact["relative_path"], artifact["sha256"],
            artifact["byte_length"], max_bytes=MAX_JSON_ARTIFACT_BYTES,
        )
    except (JsonContractError, ArtifactStoreError, OSError, ValueError):
        abort(422, description="error.invalid_json")
    if (
        len(raw) != artifact["byte_length"]
        or not hmac.compare_digest(sha256_bytes(raw), artifact["sha256"])
        or not hmac.compare_digest(raw, stored)
        or document.get("document_id") != artifact["id"]
        or document.get("version") != artifact["version_no"]
        or document.get("schema_version") != artifact["schema_version"]
    ):
        abort(422, description="error.invalid_json")
    return artifact, document


def _list(value: str) -> list[str]:
    return [line for line in value.splitlines() if line.strip()]


def _integer(value: str) -> int | None:
    if value.strip() == "":
        return None
    parsed = int(value)
    if parsed < 0 or parsed > 9_999_999_999:
        raise ValueError("amount_invalid")
    return parsed


def _section_values(section_no: int) -> dict[str, object]:
    form = request.form
    if section_no == 1:
        return {
            "working_name": form.get("working_name", ""),
            "target_audience": form.get("target_audience", ""),
            "primary_purpose": form.get("primary_purpose", ""),
            "primary_visitor_action": form.get("primary_visitor_action", ""),
            "supplemental_notes": form.get("supplemental_notes", ""),
        }
    if section_no == 2:
        return {key: _list(form.get(key, "")) for key in ("user_groups", "primary_devices", "entry_paths", "core_actions")}
    if section_no == 3:
        pages = []
        for index, line in enumerate(_list(form.get("pages", "")), start=1):
            pieces = line.split("|", 2)
            pieces += [""] * (3 - len(pieces))
            pages.append({"id": f"page-{index}", "title": pieces[0], "purpose": pieces[1], "content_items": [item for item in pieces[2].split(",") if item]})
        return {"pages": pages, "language_requirements": _list(form.get("language_requirements", ""))}
    if section_no == 4:
        items = []
        for code in ("booking", "form", "membership", "search", "cart", "admin", "notification", "other"):
            items.append({"feature_code": code, "requirement_level": form.get(f"feature_{code}", "undecided"), "rules": _list(form.get(f"rules_{code}", ""))})
        return {"items": items}
    if section_no == 5:
        return {
            "data_categories": _list(form.get("data_categories", "")),
            "contains_customer_data": form.get("contains_customer_data") == "yes",
            "roles": [{"role_code": "site_administrator", "read_rights": _list(form.get("read_rights", "")), "write_rights": _list(form.get("write_rights", ""))}],
            "retention_expectations": form.get("retention_expectations", ""),
            "deletion_expectations": form.get("deletion_expectations", ""),
        }
    if section_no == 6:
        services = [{"service_code": code, "status": form.get(f"service_{code}", "contractor_proposal"), "notes": form.get(f"service_{code}_notes", "")} for code in ("domain", "hosting", "payment", "maps", "social", "email", "api")]
        assets = [{"asset_code": code, "status": form.get(f"asset_{code}", "contractor_proposal"), "notes": form.get(f"asset_{code}_notes", "")} for code in ("existing_site", "logo", "brand_materials", "database")]
        return {"services": services, "assets": assets}
    if section_no == 7:
        sites = []
        for line in _list(form.get("reference_sites", "")):
            pieces = line.split("|", 1)
            pieces += [""] * (2 - len(pieces))
            sites.append({"url": pieces[0], "reason": pieces[1]})
        return {
            "brand_style": _list(form.get("brand_style", "")), "colors": _list(form.get("colors", "")),
            "reference_sites": sites, "mobile_requirements": _list(form.get("mobile_requirements", "")),
            "accessibility_requirements": _list(form.get("accessibility_requirements", "")), "excluded_directions": _list(form.get("excluded_directions", "")),
        }
    return {
        "deliverables": _list(form.get("deliverables", "")), "source_code_ownership": form.get("source_code_ownership", ""),
        "account_ownership": _list(form.get("account_ownership", "")), "deployment_expectation": form.get("deployment_expectation", ""),
        "maintenance_expectation": form.get("maintenance_expectation", ""), "launch_conditions": _list(form.get("launch_conditions", "")),
        "required_deadline": form.get("required_deadline") or None, "budget_status": form.get("budget_status", "undecided"),
        "budget_currency": form.get("budget_currency", "").strip().upper() or None,
        "budget_min_amount": _integer(form.get("budget_min_amount", "")), "budget_max_amount": _integer(form.get("budget_max_amount", "")),
        "acceptance_expectations": _list(form.get("acceptance_expectations", "")), "constraints": _list(form.get("constraints", "")),
    }


def register_localized_routes(app: Flask) -> None:
    def localized(rule: str, methods: tuple[str, ...] = ("GET",)):
        def decorator(handler):
            for locale in SUPPORTED_LOCALES:
                endpoint = f"{locale.replace('-', '_')}.{handler.__name__}"
                app.add_url_rule(f"/{locale}{rule}", endpoint, handler, methods=list(methods), defaults={"locale": locale})
            return handler
        return decorator

    @localized("/")
    def start(locale: str):
        return render_template("start.html")

    @localized("/i18n/client.json")
    def client_catalog(locale: str):
        catalog = current_app.config["CATALOGS"][locale]
        response = Response(json.dumps({key: catalog[key] for key in CLIENT_CATALOG_KEYS}, ensure_ascii=False), mimetype="application/json")
        response.headers["Cache-Control"] = "no-store"
        return response

    @localized("/login", ("GET", "POST"))
    def login(locale: str):
        flow = request.args.get("flow", "") if request.method == "GET" else request.form.get("flow", "")
        if flow != "start":
            flow = ""
        account = request.args.get("account", "") if request.method == "GET" else request.form.get("account", "")
        account_roles = {"client", "contractor", "admin"}
        if account not in account_roles:
            account = ""
        account_email = ""
        if request.method == "GET":
            if g.user is not None:
                endpoint = "admin_accounts" if g.user["system_role"] == "admin" else "account"
                return redirect(url_for(f"{locale.replace('-', '_')}.{endpoint}"))
            return render_template(
                "login.html",
                error=None,
                flow=flow,
                account=account,
                account_email=account_email,
            )
        email = request.form.get("email", "").strip().lower()
        candidate = request.form.get("password", "")
        user = get_db().execute("SELECT * FROM users WHERE email_normalized = ?", (email,)).fetchone()
        verification = verify_password(user["password_hash"] if user else None, candidate)
        if verification is PasswordVerification.BUSY:
            return render_template(
                "login.html",
                error="error.login_unavailable",
                flow=flow,
                account=account,
                account_email=account_email,
            ), 503, {"Retry-After": str(KDF_RETRY_AFTER_SECONDS), "Cache-Control": "no-store"}
        if verification is not PasswordVerification.VALID or user is None or user["status"] != "active":
            return render_template(
                "login.html",
                error="error.login_invalid",
                flow=flow,
                account=account,
                account_email=account_email,
            ), 401
        invitation_id = session.get("post_login_invitation_id")
        session.clear()
        session.permanent = True
        session["user_id"] = user["id"]
        session["session_version"] = user["session_version"]
        session["csrf_nonce"] = new_nonce()
        if user["system_role"] == "admin":
            endpoint = "admin_accounts"
        elif isinstance(invitation_id, str) and invitation_id:
            return redirect(
                url_for(
                    f"{locale.replace('-', '_')}.invitation_review",
                    invitation_id=invitation_id,
                )
            )
        elif flow == "start":
            endpoint = "owner_guide"
        else:
            endpoint = "account"
        return redirect(url_for(f"{locale.replace('-', '_')}.{endpoint}"))

    @localized("/logout", ("POST",))
    @current_user_required
    def logout(locale: str):
        with immediate_transaction(get_db()):
            get_db().execute(
                "UPDATE users SET session_version = session_version + 1, updated_at = ? WHERE id = ?",
                (utc_now(), g.user["id"]),
            )
        session.clear()
        session["csrf_nonce"] = new_nonce()
        return redirect(url_for(f"{locale.replace('-', '_')}.start"))

    @localized("/account")
    @current_user_required
    def account(locale: str):
        owner_cases = []
        pending_invitations = []
        contractor_cases = []
        if g.user["system_role"] != "admin":
            owner_rows = get_db().execute(
                "SELECT c.id, c.title, COALESCE(w.state, c.state) AS state, c.content_revision, "
                "d.completed_sections_json "
                "FROM cases AS c LEFT JOIN delivery_workflows AS w ON w.case_id = c.id "
                "LEFT JOIN requirement_drafts AS d ON d.case_id = c.id "
                "WHERE c.owner_id = ? ORDER BY c.created_at",
                (g.user["id"],),
            ).fetchall()
            owner_cases = []
            for row in owner_rows:
                owner_case = dict(row)
                completed = set(json.loads(owner_case["completed_sections_json"] or "[]"))
                owner_case["resume_section"] = next((step for step in range(1, 9) if step not in completed), None)
                owner_cases.append(owner_case)
            pending_invitations = get_db().execute(
                "SELECT i.id, i.state, i.expires_at, c.title "
                "FROM invitations AS i JOIN cases AS c ON c.id = i.case_id "
                "WHERE i.designated_user_id = ? AND i.state = 'pending' AND i.expires_at > ? "
                "ORDER BY i.created_at",
                (g.user["id"], utc_now()),
            ).fetchall()
            contractor_cases = get_db().execute(
                "SELECT c.id, c.title, COALESCE(w.state, c.state) AS state, c.content_revision "
                "FROM cases AS c LEFT JOIN delivery_workflows AS w ON w.case_id = c.id "
                "WHERE c.contractor_id = ? ORDER BY c.created_at",
                (g.user["id"],),
            ).fetchall()
        return render_template(
            "account.html",
            owner_cases=owner_cases,
            pending_invitations=pending_invitations,
            contractor_cases=contractor_cases,
        )

    @localized("/owner/guide")
    @require_member
    def owner_guide(locale: str):
        return render_template("owner_guide.html")

    @localized("/owner/requirements", ("POST",))
    @require_member
    def new_requirement(locale: str):
        title = request.form.get("title", "").strip()
        if not title or len(title) > 200:
            abort(400, description="error.required_field")
        case_id = create_case(get_db(), g.user["id"], title)
        return redirect(url_for(f"{locale.replace('-', '_')}.requirement_section", case_id=case_id, section_no=1))

    @localized("/owner/requirements/<case_id>/sections/<int:section_no>", ("GET", "POST"))
    @current_user_required
    def requirement_section(locale: str, case_id: str, section_no: int):
        if section_no not in range(1, 9):
            abort(404)
        case = load_owner_case(case_id)
        draft, completed = load_draft(get_db(), case_id)
        if request.method == "GET" and case["state"] != "REQUIREMENT_DRAFT":
            if case["state"] in ("INVITATION_PENDING", "WAITING_JSON_A_IMPORT"):
                destination = "publish_invitation"
            elif case["state"] == "FORMAL":
                delivery = get_db().execute(
                    "SELECT state FROM delivery_workflows WHERE case_id = ?", (case_id,)
                ).fetchone()
                destination = "delivery_dashboard" if delivery and delivery["state"] != "FORMAL" else "formal_verification"
            else:
                destination = "case_room"
            return redirect(url_for(f"{locale.replace('-', '_')}.{destination}", case_id=case_id))
        if request.method == "POST":
            try:
                expected_revision = int(request.form.get("expected_revision", ""))
                values = _section_values(section_no)
                save_section(get_db(), case_id, section_no, values, expected_revision)
            except ValueError:
                abort(422, description="error.required_field")
            except (RuntimeError, sqlite3.OperationalError):
                abort(409, description="error.conflict_retry")
            if section_no == 8:
                return redirect(url_for(f"{locale.replace('-', '_')}.requirement_review", case_id=case_id))
            return redirect(url_for(f"{locale.replace('-', '_')}.requirement_section", case_id=case_id, section_no=section_no + 1))
        values = draft.get(tuple(("project", "users_and_scenarios", "pages_and_content", "features", "data_and_permissions", "external_services_and_assets", "visual_preferences", "delivery_and_constraints"))[section_no - 1], {})
        return render_template(
            "requirement_section.html",
            case=case,
            section_no=section_no,
            values=values,
            completed=completed,
            feature_items={item["feature_code"]: item for item in values.get("items", [])},
            service_items={item["service_code"]: item for item in values.get("services", [])},
            asset_items={item["asset_code"]: item for item in values.get("assets", [])},
        )

    @localized("/owner/requirements/<case_id>/review")
    @current_user_required
    def requirement_review(locale: str, case_id: str):
        case = load_owner_case(case_id)
        draft, completed = load_draft(get_db(), case_id)
        blueprint = blueprint_data(draft)
        artifact = None
        if case["current_json_a_artifact_id"]:
            artifact = get_db().execute("SELECT id, version_no, sha256, byte_length FROM artifacts WHERE id = ?", (case["current_json_a_artifact_id"],)).fetchone()
        next_section = next((step for step in range(1, 9) if step not in completed), None)
        return render_template("review.html", case=case, draft=draft, completed=completed, blueprint=blueprint, artifact=artifact, next_section=next_section)

    @localized("/owner/requirements/<case_id>/blueprint.svg")
    @current_user_required
    def requirement_blueprint(locale: str, case_id: str):
        load_owner_case(case_id)
        draft, _ = load_draft(get_db(), case_id)
        response = Response(render_svg(blueprint_data(draft)), mimetype="image/svg+xml")
        response.headers["Cache-Control"] = "no-store, private"
        return response

    @localized("/owner/requirements/<case_id>/json-a", ("POST",))
    @current_user_required
    def requirement_json_a(locale: str, case_id: str):
        load_owner_case(case_id)
        if request.form.get("review_confirmed") != "yes":
            abort(400, description="error.required_field")
        try:
            expected_revision = int(request.form.get("expected_revision", ""))
            artifact_id, _digest, _raw = generate_json_a(
                get_db(),
                Path(current_app.config["DATA_ROOT"]),
                Path(current_app.config["SCHEMA_PATH"]),
                case_id,
                g.user["id"],
                expected_revision,
                current_app.config["DOCUMENT_ASSETS"],
            )
        except DocumentIntegrityError:
            abort(422, description="error.unsupported_character")
        except RuntimeError:
            abort(409, description="error.conflict_retry")
        except (ValueError, ArtifactStoreError):
            abort(422, description="error.invalid_json")
        return redirect(url_for(f"{locale.replace('-', '_')}.requirement_review", case_id=case_id, artifact_id=artifact_id))

    @localized("/owner/requirements/<case_id>/json-a/<artifact_id>/download")
    @current_user_required
    def download_json_a(locale: str, case_id: str, artifact_id: str):
        load_owner_case(case_id)
        artifact = get_db().execute(
            "SELECT relative_path, sha256, byte_length FROM artifacts WHERE id = ? AND case_id = ? AND kind = 'json_a' AND lifecycle IN ('active','superseded','formal')",
            (artifact_id, case_id),
        ).fetchone()
        if artifact is None:
            abort(404)
        try:
            raw = read_verified(
                Path(current_app.config["DATA_ROOT"]), artifact["relative_path"], artifact["sha256"],
                artifact["byte_length"], max_bytes=MAX_JSON_ARTIFACT_BYTES,
            )
        except (OSError, ArtifactStoreError):
            abort(409, description="error.digest_mismatch")
        response = Response(raw, mimetype="application/json")
        response.headers["Content-Disposition"] = f'attachment; filename="requirement-json-a-{artifact_id}.json"'
        response.headers["Cache-Control"] = "no-store, private"
        return response

    def publish_page_context(case_id: str):
        case = load_owner_case(case_id)
        artifact = _active_json_a(case_id, case["current_json_a_artifact_id"])
        invitation = get_db().execute(
            "SELECT i.id, i.state, i.expires_at, i.source_version_no AS json_a_version_no, "
            "i.source_invalidated_at, i.source_invalidation_reason, "
            "u.display_name AS invited_display_name, u.email_normalized AS invited_email_normalized "
            "FROM invitations AS i JOIN users AS u ON u.id = i.designated_user_id "
            "WHERE i.case_id = ? ORDER BY i.created_at DESC LIMIT 1",
            (case_id,),
        ).fetchone()
        if invitation is not None:
            invitation = dict(invitation)
            if invitation["state"] == "pending" and invitation["expires_at"] <= utc_now():
                invitation["state"] = "expired"
        return case, artifact, invitation

    @localized("/owner/requirements/<case_id>/publish")
    @current_user_required
    def publish_invitation(locale: str, case_id: str):
        case, artifact, invitation = publish_page_context(case_id)
        return render_template(
            "publish_invitation.html",
            case=case,
            artifact=artifact,
            invitation=invitation,
            preview=None,
            invitation_url=None,
            invitation_code=None,
        )

    @localized("/owner/requirements/<case_id>/publish/preview", ("POST",))
    @current_user_required
    def preview_json_a_for_invitation(locale: str, case_id: str):
        case = load_owner_case(
            case_id,
            allowed_states=("REQUIREMENT_DRAFT", "INVITATION_PENDING"),
        )
        try:
            expected_revision = int(request.form.get("expected_revision", ""))
        except ValueError:
            abort(409, description="error.conflict_retry")
        if expected_revision != case["content_revision"]:
            abort(409, description="error.conflict_retry")
        raw = _uploaded_json_a()
        artifact, document = _verify_preview_bytes(case_id, case["current_json_a_artifact_id"], raw)
        project = document.get("project", {})
        preview = {
            "project_name": project.get("working_name", ""),
            "primary_purpose": project.get("primary_purpose", ""),
            "unresolved_count": len(document.get("open_items", [])),
            "artifact_id": artifact["id"],
            "sha256": artifact["sha256"],
        }
        return render_template(
            "publish_invitation.html",
            case=case,
            artifact=artifact,
            invitation=None,
            preview=preview,
            invitation_url=None,
            invitation_code=None,
        )

    @localized("/owner/requirements/<case_id>/invitations", ("POST",))
    @current_user_required
    def issue_invitation(locale: str, case_id: str):
        case = load_owner_case(
            case_id,
            allowed_states=("REQUIREMENT_DRAFT", "INVITATION_PENDING"),
        )
        try:
            expected_revision = int(request.form.get("expected_revision", ""))
        except ValueError:
            abort(409, description="error.conflict_retry")
        if expected_revision != case["content_revision"]:
            abort(409, description="error.conflict_retry")
        email = request.form.get("contractor_email", "").strip().lower()
        designated = get_db().execute(
            "SELECT id FROM users WHERE email_normalized = ? AND status = 'active' "
            "AND system_role = 'member' AND id <> ?",
            (email, g.user["id"]),
        ).fetchone()
        if designated is None:
            abort(422, description="error.invitation_invalid")
        raw = _uploaded_json_a()
        try:
            issued = create_invitation(
                get_db(),
                Path(current_app.config["DATA_ROOT"]),
                Path(current_app.config["SCHEMA_PATH"]),
                case_id,
                g.user["id"],
                designated["id"],
                raw,
            )
        except InvitationWorkflowError as error:
            if error.code in {"json_a_invalid", "json_a_mismatch", "source_artifact_invalid"}:
                abort(422, description="error.invalid_json")
            abort(409, description="error.conflict_retry")
        case, artifact, invitation = publish_page_context(case_id)
        response = current_app.make_response(render_template(
            "publish_invitation.html",
            case=case,
            artifact=artifact,
            invitation=invitation,
            preview=None,
            invitation_url=url_for(
                f"{locale.replace('-', '_')}.invitation_review",
                invitation_id=issued.invitation_id,
                _external=True,
            ),
            invitation_code=issued.code,
        ))
        response.headers["Cache-Control"] = "no-store, private"
        return response

    @localized("/owner/requirements/<case_id>/invitation")
    @current_user_required
    def owner_invitation_status(locale: str, case_id: str):
        load_owner_case(case_id)
        return redirect(url_for(f"{locale.replace('-', '_')}.publish_invitation", case_id=case_id))

    @localized("/owner/requirements/<case_id>/invitations/<invitation_id>/revoke", ("POST",))
    @current_user_required
    def revoke_case_invitation(locale: str, case_id: str, invitation_id: str):
        load_owner_case(case_id, allowed_states=("INVITATION_PENDING",))
        try:
            revoke_invitation(get_db(), case_id, invitation_id, g.user["id"])
        except InvitationWorkflowError:
            abort(404)
        return redirect(url_for(f"{locale.replace('-', '_')}.publish_invitation", case_id=case_id))

    @localized("/owner/requirements/<case_id>/invitations/<invitation_id>/cancel-before-import", ("POST",))
    @current_user_required
    def cancel_case_invitation_before_import(locale: str, case_id: str, invitation_id: str):
        case = load_owner_case(case_id, allowed_states=("WAITING_JSON_A_IMPORT",))
        if request.form.get("expected_revision") != str(case["content_revision"]):
            abort(409, description="error.conflict_retry")
        try:
            cancel_accepted_invitation_before_import(get_db(), case_id, invitation_id, g.user["id"])
        except InvitationWorkflowError:
            abort(404)
        return redirect(url_for(f"{locale.replace('-', '_')}.publish_invitation", case_id=case_id))

    def designated_invitation(invitation_id: str):
        if g.user["system_role"] == "admin":
            abort(404)
        invitation = get_db().execute(
            "SELECT i.id, i.case_id, i.state, i.expires_at, i.source_version_no AS json_a_version_no, "
            "i.source_sha256 AS json_a_sha256, i.source_content_revision AS case_revision, "
            "i.source_invalidated_at, "
            "CASE WHEN (i.state = 'pending' AND i.source_invalidated_at IS NULL "
            "AND i.expires_at > ?) OR (i.state = 'accepted' "
            "AND i.source_invalidated_at IS NULL "
            "AND c.contractor_id = i.designated_user_id) "
            "THEN c.title END AS case_title, "
            "CASE WHEN i.state = 'accepted' AND i.source_invalidated_at IS NULL "
            "AND c.contractor_id = i.designated_user_id THEN c.state END AS case_state, "
            "CASE WHEN i.state = 'accepted' AND i.source_invalidated_at IS NULL "
            "AND c.contractor_id = i.designated_user_id "
            "THEN COALESCE(w.state, c.state) END AS workflow_state, "
            "owner.display_name AS client_display_name "
            "FROM invitations AS i JOIN cases AS c ON c.id = i.case_id "
            "LEFT JOIN delivery_workflows AS w ON w.case_id = c.id "
            "JOIN users AS owner ON owner.id = i.owner_id "
            "WHERE i.id = ? AND i.designated_user_id = ?",
            (utc_now(), invitation_id, g.user["id"]),
        ).fetchone()
        if invitation is None:
            abort(404)
        invitation = dict(invitation)
        if invitation["state"] == "pending" and invitation["expires_at"] <= utc_now():
            invitation["state"] = "expired"
        return invitation

    @localized("/invitations/<invitation_id>")
    def invitation_review(locale: str, invitation_id: str):
        if g.user is None:
            session["post_login_invitation_id"] = invitation_id
            return redirect(url_for(f"{locale.replace('-', '_')}.login"))
        invitation = designated_invitation(invitation_id)
        return render_template("invitation_review.html", invitation=invitation)

    @localized("/invitations/<invitation_id>/accept", ("POST",))
    @current_user_required
    def invitation_accept(locale: str, invitation_id: str):
        designated_invitation(invitation_id)
        code = request.form.get("invitation_code", "")
        try:
            accepted = accept_invitation(
                get_db(),
                Path(current_app.config["DATA_ROOT"]),
                invitation_id,
                code,
                g.user["id"],
            )
        except InvitationWorkflowError:
            abort(404)
        return redirect(
            url_for(f"{locale.replace('-', '_')}.case_json_a_import", case_id=accepted.case_id)
        )

    @localized("/invitations/<invitation_id>/reject", ("POST",))
    @current_user_required
    def invitation_reject(locale: str, invitation_id: str):
        designated_invitation(invitation_id)
        code = request.form.get("invitation_code", "")
        try:
            reject_invitation(get_db(), invitation_id, code, g.user["id"])
        except InvitationWorkflowError:
            abort(404)
        return redirect(url_for(f"{locale.replace('-', '_')}.account"))

    def contractor_import_context(case_id: str):
        case, _role = load_case_party(
            case_id,
            allowed_roles=("contractor",),
            allowed_states=("WAITING_JSON_A_IMPORT",),
        )
        invitation = get_db().execute(
            "SELECT id, source_version_no AS version_no, source_sha256 AS sha256, "
            "source_byte_length AS byte_length FROM invitations "
            "WHERE case_id = ? AND designated_user_id = ? AND state = 'accepted' "
            "AND source_invalidated_at IS NULL ORDER BY accepted_at DESC LIMIT 1",
            (case_id, g.user["id"]),
        ).fetchone()
        if invitation is None:
            abort(404)
        return case, invitation

    @localized("/cases/<case_id>/json-a/import", ("GET", "POST"))
    @current_user_required
    def case_json_a_import(locale: str, case_id: str):
        case, source = contractor_import_context(case_id)
        if request.method == "GET":
            return render_template("json_a_import.html", case=case, source=source)
        raw = _uploaded_json_a()
        try:
            result = import_json_a(
                get_db(),
                Path(current_app.config["DATA_ROOT"]),
                Path(current_app.config["SCHEMA_PATH"]),
                source["id"],
                g.user["id"],
                raw,
            )
        except InvitationWorkflowError as error:
            if error.code in {"json_a_invalid", "json_a_mismatch", "source_artifact_invalid"}:
                abort(422, description="error.invalid_json")
            abort(404)
        return redirect(url_for(f"{locale.replace('-', '_')}.case_room", case_id=result.case_id))

    def active_case_document(case_id: str):
        case, role = load_case_party(
            case_id,
            allowed_states=("CASE_ACTIVE", "JSON_B_PRIVATE_DRAFT", "JSON_B_REVIEW", "CANDIDATE_REVIEW", "FORMAL"),
        )
        row = get_db().execute(
            "SELECT a.id, a.version_no, a.relative_path, a.sha256, a.byte_length, a.schema_version, "
            "v.document_id, v.verified_at, owner.display_name AS client_display_name, "
            "contractor.display_name AS contractor_display_name "
            "FROM artifacts AS a "
            "JOIN cases AS c ON c.current_json_a_artifact_id = a.id "
            "JOIN users AS owner ON owner.id = c.owner_id "
            "JOIN users AS contractor ON contractor.id = c.contractor_id "
            "JOIN json_a_source_verifications AS v ON v.artifact_id = a.id "
            "AND v.case_id = c.id AND v.actor_role = 'contractor' "
            "WHERE c.id = ? AND a.visibility = 'party_only' "
            "AND ((c.state = 'FORMAL' AND a.lifecycle = 'formal') "
            "OR (c.state <> 'FORMAL' AND a.lifecycle = 'active'))",
            (case_id,),
        ).fetchone()
        if row is None:
            abort(404)
        try:
            raw = read_verified(
                Path(current_app.config["DATA_ROOT"]), row["relative_path"], row["sha256"],
                row["byte_length"], max_bytes=MAX_JSON_ARTIFACT_BYTES,
            )
            document = decode_strict_json(raw)
            if not isinstance(document, dict):
                raise JsonContractError("schema_validation_failed")
            validate_json_a(document, Path(current_app.config["SCHEMA_PATH"]))
        except (ArtifactStoreError, JsonContractError, OSError, ValueError):
            abort(409, description="error.digest_mismatch")
        return case, role, row, document

    @localized("/cases/<case_id>")
    @current_user_required
    def case_room(locale: str, case_id: str):
        case, role, source, document = active_case_document(case_id)
        project = document.get("project", {})
        collaboration = list_collaboration(get_db(), case_id, g.user["id"])

        def issue_is_actionable_for_role(issue) -> bool:
            if issue["kind"] == "JSON_B_OBJECTION":
                if issue["state"] in ("OPEN", "REVISION_REQUESTED"):
                    return role == "contractor"
                if issue["state"] in ("PROPOSED", "OUT_OF_SCOPE_PROPOSED"):
                    return role == "client"
                return False
            return (
                issue["state"] in ("OPEN", "REVISION_REQUESTED")
                or (
                    issue["state"] in ("PROPOSED", "OUT_OF_SCOPE_PROPOSED")
                    and issue["proposal_role"] != role
                )
            )

        primary_issue = next(
            (
                issue
                for issue in collaboration["issues"]
                if case["state"] not in ("JSON_B_PRIVATE_DRAFT", "CANDIDATE_REVIEW", "FORMAL")
                and issue_is_actionable_for_role(issue)
            ),
            None,
        )
        current_review = None
        if case["current_json_b_artifact_id"] is not None:
            current_review = get_db().execute(
                "SELECT v.artifact_id, v.version_no, v.state, v.quote_expires_at, a.sha256, a.byte_length "
                "FROM json_b_versions AS v JOIN artifacts AS a ON a.id = v.artifact_id "
                "WHERE v.case_id = ? AND v.artifact_id = ? AND a.visibility = 'party_only'",
                (case_id, case["current_json_b_artifact_id"]),
            ).fetchone()
        current_candidate = None
        if case["active_candidate_id"] is not None:
            current_candidate = get_db().execute(
                "SELECT id, version_no, created_at FROM candidate_versions "
                "WHERE id = ? AND case_id = ? AND state = 'CURRENT'",
                (case["active_candidate_id"], case_id),
            ).fetchone()
        formal_candidate = None
        if case["formal_candidate_id"] is not None:
            formal_candidate = get_db().execute(
                "SELECT id, version_no, formalized_at FROM candidate_versions "
                "WHERE id = ? AND case_id = ? AND state = 'FORMAL'",
                (case["formal_candidate_id"], case_id),
            ).fetchone()
        return render_template(
            "case_room.html",
            case=case,
            case_role=role,
            parties={
                "client_display_name": source["client_display_name"],
                "contractor_display_name": source["contractor_display_name"],
            },
            verification={
                "document_id": source["document_id"],
                "schema_version": source["schema_version"],
                "version_no": source["version_no"],
                "sha256": source["sha256"],
                "byte_length": source["byte_length"],
                "verified_at": source["verified_at"],
            },
            requirement_summary={
                "target_audience": project.get("target_audience", ""),
                "primary_purpose": project.get("primary_purpose", ""),
                "primary_visitor_action": project.get("primary_visitor_action", ""),
                "unresolved_count": len(document.get("open_items", [])),
            },
            json_a_download_url=url_for(
                f"{locale.replace('-', '_')}.case_json_a_download", case_id=case_id
            ),
            blueprint_url=url_for(
                f"{locale.replace('-', '_')}.case_blueprint", case_id=case_id
            ),
            messages=collaboration["messages"],
            issues=collaboration["issues"],
            issue_events=collaboration["issue_events"],
            primary_issue=primary_issue,
            current_review=current_review,
            current_candidate=current_candidate,
            formal_candidate=formal_candidate,
            message_idempotency_key=str(uuid.uuid4()),
            correction_idempotency_keys={
                message["id"]: str(uuid.uuid4()) for message in collaboration["messages"]
            },
        )

    @localized("/cases/<case_id>/json-a/download")
    @current_user_required
    def case_json_a_download(locale: str, case_id: str):
        _case, _role, source, _document = active_case_document(case_id)
        try:
            raw = read_verified(
                Path(current_app.config["DATA_ROOT"]), source["relative_path"], source["sha256"],
                source["byte_length"], max_bytes=MAX_JSON_ARTIFACT_BYTES,
            )
        except (ArtifactStoreError, OSError, ValueError):
            abort(409, description="error.digest_mismatch")
        response = Response(raw, mimetype="application/json")
        response.headers["Content-Disposition"] = f'attachment; filename="requirement-json-a-{source["id"]}.json"'
        response.headers["Cache-Control"] = "no-store, private"
        return response

    @localized("/cases/<case_id>/blueprint.svg")
    @current_user_required
    def case_blueprint(locale: str, case_id: str):
        _case, _role, _source, document = active_case_document(case_id)
        blueprint = document.get("blueprint")
        if not isinstance(blueprint, dict):
            abort(409, description="error.digest_mismatch")
        response = Response(render_svg(blueprint), mimetype="image/svg+xml")
        response.headers["Cache-Control"] = "no-store, private"
        return response

    @localized("/cases/<case_id>/messages", ("POST",))
    @current_user_required
    def case_message_create(locale: str, case_id: str):
        try:
            post_message(
                get_db(),
                case_id,
                g.user["id"],
                request.form.get("body", ""),
                request.form.get("idempotency_key", ""),
                correction_of_id=request.form.get("correction_of_id") or None,
            )
        except DiscussionWorkflowError as error:
            if error.code == "case_unavailable":
                abort(404)
            abort(409 if "conflict" in error.code else 422, description="error.conflict_retry")
        return redirect(url_for(f"{locale.replace('-', '_')}.case_room", case_id=case_id))

    @localized("/cases/<case_id>/issues", ("POST",))
    @current_user_required
    def case_issue_create(locale: str, case_id: str):
        try:
            create_issue(
                get_db(),
                case_id,
                g.user["id"],
                request.form.get("summary", ""),
            )
        except DiscussionWorkflowError as error:
            if error.code == "case_unavailable":
                abort(404)
            abort(422, description="error.required_field")
        return redirect(url_for(f"{locale.replace('-', '_')}.case_room", case_id=case_id))

    @localized("/cases/<case_id>/issues/<issue_id>/events", ("POST",))
    @current_user_required
    def case_issue_event(locale: str, case_id: str, issue_id: str):
        try:
            expected_revision = int(request.form.get("expected_revision", ""))
        except ValueError:
            abort(409, description="error.conflict_retry")
        try:
            transition_issue(
                get_db(),
                case_id,
                issue_id,
                g.user["id"],
                request.form.get("action", ""),
                expected_revision,
                request.form.get("note", ""),
            )
        except DiscussionWorkflowError as error:
            if error.code in {"case_unavailable", "issue_unavailable"}:
                abort(404)
            abort(409 if "conflict" in error.code else 422, description="error.conflict_retry")
        return redirect(url_for(f"{locale.replace('-', '_')}.case_room", case_id=case_id))

    @localized("/cases/<case_id>/json-b/draft", ("GET", "POST"))
    @current_user_required
    def json_b_draft(locale: str, case_id: str):
        try:
            draft = load_private_draft(get_db(), case_id, g.user["id"])
        except JsonBWorkflowError:
            abort(404)
        if request.method == "POST":
            try:
                expected_revision = int(request.form.get("expected_revision", "0"))
                raw = request.form.get("draft_json", "").encode("utf-8")
                save_private_draft(
                    get_db(),
                    Path(current_app.config["DATA_ROOT"]),
                    Path(current_app.config["JSON_B_SCHEMA_PATH"]),
                    case_id,
                    g.user["id"],
                    expected_revision,
                    raw,
                )
            except JsonBWorkflowError as error:
                if error.code == "draft_conflict":
                    abort(409, description="error.conflict_retry")
                abort(422, description="error.invalid_json")
            except (ValueError, UnicodeEncodeError):
                abort(422, description="error.invalid_json")
            return redirect(url_for(f"{locale.replace('-', '_')}.json_b_draft", case_id=case_id))
        draft_text = draft["data_json"] if draft is not None else _empty_json_b_business_draft()
        return render_template(
            "json_b_draft.html",
            case_id=case_id,
            draft=draft,
            draft_text=draft_text,
            draft_business=json.loads(draft_text),
            business_fields=BUSINESS_FIELDS,
        )

    @localized("/cases/<case_id>/json-b/draft/export", ("POST",))
    @current_user_required
    def json_b_draft_export(locale: str, case_id: str):
        try:
            expected_revision = int(request.form.get("expected_revision", ""))
            result = export_private_draft(
                get_db(),
                Path(current_app.config["DATA_ROOT"]),
                Path(current_app.config["JSON_B_SCHEMA_PATH"]),
                case_id,
                g.user["id"],
                expected_revision,
                current_app.config["DOCUMENT_ASSETS"],
            )
        except DocumentIntegrityError:
            abort(422, description="error.unsupported_character")
        except JsonBWorkflowError as error:
            if error.code in {"quote_expiry_invalid", "quote_expired"}:
                abort(409, description="error.quote_expired")
            abort(409, description="error.conflict_retry")
        except ValueError:
            abort(409, description="error.conflict_retry")
        return redirect(
            url_for(
                f"{locale.replace('-', '_')}.json_b_review_import",
                case_id=case_id,
                artifact_id=result.artifact_id,
            )
        )

    @localized("/cases/<case_id>/json-b/draft/exports/<artifact_id>/download")
    @current_user_required
    def json_b_private_download(locale: str, case_id: str, artifact_id: str):
        try:
            artifact = load_private_export(get_db(), case_id, g.user["id"], artifact_id)
            raw = read_verified(
                Path(current_app.config["DATA_ROOT"]),
                artifact["relative_path"],
                artifact["sha256"],
                artifact["byte_length"],
                max_bytes=MAX_JSON_ARTIFACT_BYTES,
            )
        except (JsonBWorkflowError, ArtifactStoreError, OSError, ValueError):
            abort(404)
        response = Response(raw, mimetype="application/json")
        response.headers["Content-Disposition"] = f'attachment; filename="json-b-private-{artifact_id}.json"'
        response.headers["Cache-Control"] = "no-store, private"
        return response

    @localized("/cases/<case_id>/json-b/review/import", ("GET", "POST"))
    @current_user_required
    def json_b_review_import(locale: str, case_id: str):
        artifact_id = request.values.get("artifact_id", "")
        if request.method == "POST":
            try:
                result = submit_private_export(
                    get_db(),
                    Path(current_app.config["DATA_ROOT"]),
                    Path(current_app.config["JSON_B_SCHEMA_PATH"]),
                    case_id,
                    g.user["id"],
                    artifact_id,
                    _uploaded_json_b(),
                )
            except JsonBWorkflowError as error:
                if error.code in {"case_unavailable", "export_unavailable"}:
                    abort(404)
                if error.code == "quote_expired":
                    abort(409, description="error.quote_expired")
                status = 409 if error.code in {"draft_conflict", "source_mismatch"} else 422
                abort(status, description="error.invalid_json")
            return redirect(
                url_for(
                    f"{locale.replace('-', '_')}.json_b_review",
                    case_id=case_id,
                    version_no=result.version_no,
                )
            )
        try:
            artifact = load_private_export(get_db(), case_id, g.user["id"], artifact_id)
        except JsonBWorkflowError:
            abort(404)
        return render_template("json_b_import.html", case_id=case_id, artifact=artifact)

    @localized("/cases/<case_id>/json-b/reviews/<int:version_no>")
    @current_user_required
    def json_b_review(locale: str, case_id: str, version_no: int):
        try:
            row = load_review(get_db(), case_id, g.user["id"], version_no)
            document, _raw = read_review_bytes(Path(current_app.config["DATA_ROOT"]), row)
        except JsonBWorkflowError:
            abort(404)
        case, role = load_case_party(
            case_id,
            allowed_states=("JSON_B_PRIVATE_DRAFT", "JSON_B_REVIEW", "CANDIDATE_REVIEW", "FORMAL"),
        )
        has_blocking_issues = get_db().execute(
            "SELECT 1 FROM case_issues WHERE case_id = ? "
            "AND state NOT IN ('RESOLVED', 'OUT_OF_SCOPE') LIMIT 1",
            (case_id,),
        ).fetchone() is not None
        return render_template(
            "json_b_review.html",
            case=case,
            case_role=role,
            review=row,
            document=document,
            has_blocking_issues=has_blocking_issues,
            quote_expired=_quote_has_expired(row["quote_expires_at"]),
            review_fields=[
                {
                    "key": key,
                    "label_key": f"json_b.field.{key}",
                    "pointer": f"/{key}",
                    "value": document[key],
                }
                for key in BUSINESS_FIELDS
            ],
        )

    @localized("/cases/<case_id>/candidates", ("POST",))
    @current_user_required
    def candidate_create(locale: str, case_id: str):
        _case, role = load_case_party(
            case_id,
            allowed_roles=("contractor",),
            allowed_states=("JSON_B_REVIEW", "CANDIDATE_REVIEW"),
        )
        if role != "contractor":
            abort(404)
        try:
            result = create_candidate(
                get_db(),
                Path(current_app.config["DATA_ROOT"]),
                Path(current_app.config["SCHEMA_PATH"]),
                Path(current_app.config["JSON_B_SCHEMA_PATH"]),
                Path(current_app.config["CANDIDATE_MANIFEST_SCHEMA_PATH"]),
                current_app.config["DOCUMENT_ASSETS"],
                case_id,
                g.user["id"],
            )
        except CandidateWorkflowError as error:
            if error.code in {"case_unavailable", "candidate_unavailable"}:
                abort(404)
            if error.code == "quote_expired":
                abort(409, description="error.quote_expired")
            status = 422 if error.code in {"blocking_issue", "source_mismatch"} else 409
            abort(status, description="error.candidate_build_failed")
        return redirect(
            url_for(
                f"{locale.replace('-', '_')}.candidate_review",
                case_id=case_id,
                candidate_id=result.candidate_id,
            )
        )

    @localized("/cases/<case_id>/candidates/<candidate_id>")
    @current_user_required
    def candidate_review(locale: str, case_id: str, candidate_id: str):
        try:
            view = load_confirmation_view(
                get_db(), Path(current_app.config["DATA_ROOT"]),
                current_app.config["DOCUMENT_ASSETS"], case_id, candidate_id, g.user["id"],
            )
            case, case_role = load_case_party(
                case_id,
                allowed_states=("JSON_B_REVIEW", "CANDIDATE_REVIEW", "FORMAL"),
            )
        except (CandidateWorkflowError, FormalizationWorkflowError):
            abort(404)
        acknowledgement_by_role = {row["actor_role"]: row for row in view["acknowledgements"]}
        renewal_by_withdrawal = {
            row["withdrawn_confirmation_id"]: row
            for row in view["acknowledgement_renewals"]
        }
        confirmation_by_role: dict[str, sqlite3.Row] = {}
        confirmation_priority = {
            "WITHDRAWN": 1,
            "INVALIDATED": 2,
            "CURRENT": 3,
            "FORMALIZED": 4,
        }
        for row in view["confirmations"]:
            existing = confirmation_by_role.get(row["actor_role"])
            if existing is None or confirmation_priority[row["state"]] >= confirmation_priority[existing["state"]]:
                confirmation_by_role[row["actor_role"]] = row
        renewal_required_by_role = {
            row["actor_role"]: row for row in view["unrenewed_confirmations"]
        }
        confirmation_by_role.update(renewal_required_by_role)
        own_acknowledgement = acknowledgement_by_role.get(case_role)
        own_confirmation = confirmation_by_role.get(case_role)
        proposal = get_db().execute(
            "SELECT quote_expires_at FROM json_b_versions WHERE artifact_id = ? AND case_id = ?",
            (view["candidate"]["json_b_artifact_id"], case_id),
        ).fetchone()
        if proposal is None:
            abort(404)
        return render_template(
            "candidate_review.html",
            candidate=view["candidate"],
            review_round=view["round"],
            acknowledgement_by_role=acknowledgement_by_role,
            confirmation_by_role=confirmation_by_role,
            renewal_by_withdrawal=renewal_by_withdrawal,
            renewal_required_by_role=renewal_required_by_role,
            own_acknowledgement=own_acknowledgement,
            own_confirmation=own_confirmation,
            case=case,
            case_id=case_id,
            case_role=case_role,
            quote_expired=_quote_has_expired(proposal["quote_expires_at"]),
        )

    def _formalization_error(error: FormalizationWorkflowError) -> None:
        if error.code in {"candidate_unavailable", "formal_unavailable"}:
            abort(404)
        if error.code in {"revision_reason_required", "acknowledgement_required"}:
            abort(422, description=f"error.{error.code}")
        if error.code == "quote_expired":
            abort(409, description="error.quote_expired")
        if error.code == "blocking_issue":
            abort(422, description="error.blocking_issue")
        abort(409, description="error.confirmation_stale")

    @localized("/cases/<case_id>/candidates/<candidate_id>/acknowledgements", ("POST",))
    @current_user_required
    def candidate_acknowledge(locale: str, case_id: str, candidate_id: str):
        if request.form.get("acknowledged") != "yes":
            abort(422, description="error.acknowledgement_required")
        try:
            acknowledge_candidate(
                get_db(), Path(current_app.config["DATA_ROOT"]),
                current_app.config["DOCUMENT_ASSETS"], case_id, candidate_id,
                g.user["id"], int(request.form.get("expected_revision", "")),
            )
        except (ValueError, FormalizationWorkflowError) as error:
            if isinstance(error, FormalizationWorkflowError):
                _formalization_error(error)
            abort(409, description="error.confirmation_stale")
        return redirect(url_for(
            f"{locale.replace('-', '_')}.candidate_review",
            case_id=case_id, candidate_id=candidate_id,
        ))

    @localized("/cases/<case_id>/candidates/<candidate_id>/confirmations", ("POST",))
    @current_user_required
    def candidate_confirm(locale: str, case_id: str, candidate_id: str):
        if request.form.get("confirmed") != "yes":
            abort(422, description="error.acknowledgement_required")
        try:
            result = confirm_candidate(
                get_db(), Path(current_app.config["DATA_ROOT"]),
                current_app.config["DOCUMENT_ASSETS"], case_id, candidate_id,
                g.user["id"], int(request.form.get("expected_revision", "")),
            )
        except (ValueError, FormalizationWorkflowError) as error:
            if isinstance(error, FormalizationWorkflowError):
                _formalization_error(error)
            abort(409, description="error.confirmation_stale")
        endpoint = "formal_verification" if result.formalized else "candidate_review"
        values = {"case_id": case_id}
        if not result.formalized:
            values["candidate_id"] = candidate_id
        return redirect(url_for(f"{locale.replace('-', '_')}.{endpoint}", **values))

    @localized("/cases/<case_id>/candidates/<candidate_id>/confirmations/withdraw", ("POST",))
    @current_user_required
    def candidate_confirmation_withdraw(locale: str, case_id: str, candidate_id: str):
        try:
            withdraw_confirmation(
                get_db(), Path(current_app.config["DATA_ROOT"]),
                current_app.config["DOCUMENT_ASSETS"], case_id, candidate_id,
                g.user["id"], int(request.form.get("expected_revision", "")),
            )
        except (ValueError, FormalizationWorkflowError) as error:
            if isinstance(error, FormalizationWorkflowError):
                _formalization_error(error)
            abort(409, description="error.confirmation_stale")
        return redirect(url_for(
            f"{locale.replace('-', '_')}.candidate_review",
            case_id=case_id, candidate_id=candidate_id,
        ))

    @localized("/cases/<case_id>/candidates/<candidate_id>/revision-requests", ("POST",))
    @current_user_required
    def candidate_revision_request(locale: str, case_id: str, candidate_id: str):
        try:
            request_candidate_revision(
                get_db(), Path(current_app.config["DATA_ROOT"]),
                current_app.config["DOCUMENT_ASSETS"], case_id, candidate_id,
                g.user["id"], int(request.form.get("expected_revision", "")),
                request.form.get("reason", ""),
            )
        except (ValueError, FormalizationWorkflowError) as error:
            if isinstance(error, FormalizationWorkflowError):
                _formalization_error(error)
            abort(409, description="error.confirmation_stale")
        return redirect(url_for(f"{locale.replace('-', '_')}.case_room", case_id=case_id))

    def candidate_component_response(case_id: str, candidate_id: str, filename: str):
        try:
            raw = read_candidate_component(
                get_db(), Path(current_app.config["DATA_ROOT"]), case_id, candidate_id, g.user["id"], filename,
                current_app.config["DOCUMENT_ASSETS"],
            )
        except CandidateWorkflowError:
            abort(404)
        media_types = {
            "manifest.json": "application/json",
            "document.pdf": "application/pdf",
            "diagram.svg": "image/svg+xml",
        }
        response = Response(raw, mimetype=media_types[filename])
        disposition = "attachment" if filename == "manifest.json" or request.args.get("download") == "1" else "inline"
        response.headers["Content-Disposition"] = f'{disposition}; filename="{filename}"'
        response.headers["Cache-Control"] = "no-store, private"
        return response

    @localized("/cases/<case_id>/candidates/<candidate_id>/manifest.json")
    @current_user_required
    def candidate_manifest(locale: str, case_id: str, candidate_id: str):
        return candidate_component_response(case_id, candidate_id, "manifest.json")

    @localized("/cases/<case_id>/candidates/<candidate_id>/document.pdf")
    @current_user_required
    def candidate_pdf(locale: str, case_id: str, candidate_id: str):
        return candidate_component_response(case_id, candidate_id, "document.pdf")

    @localized("/cases/<case_id>/candidates/<candidate_id>/diagram.svg")
    @current_user_required
    def candidate_svg(locale: str, case_id: str, candidate_id: str):
        return candidate_component_response(case_id, candidate_id, "diagram.svg")

    @localized("/cases/<case_id>/formal")
    @current_user_required
    def formal_verification(locale: str, case_id: str):
        try:
            formal = load_formal_version(
                get_db(), Path(current_app.config["DATA_ROOT"]),
                current_app.config["DOCUMENT_ASSETS"], case_id, g.user["id"],
            )
        except (CandidateWorkflowError, FormalizationWorkflowError):
            abort(404)
        case_role = "client" if formal["case"]["owner_id"] == g.user["id"] else "contractor"
        return render_template(
            "formal_verification.html",
            formal=formal,
            case_role=case_role,
            separate_proposal_idempotency_key=str(uuid.uuid4()),
        )

    def _delivery_view_or_404(case_id: str):
        try:
            return load_delivery_dashboard(
                get_db(), Path(current_app.config["DATA_ROOT"]),
                current_app.config["DOCUMENT_ASSETS"], case_id, g.user["id"],
            )
        except DeliveryWorkflowError as error:
            if error.code == "acceptance_integrity_invalid":
                abort(409, description="error.digest_mismatch")
            abort(404)

    def _delivery_error(error: DeliveryWorkflowError | EvidenceExportError) -> None:
        if error.code in {"case_unavailable", "delivery_unavailable", "dispute_unavailable", "export_unavailable"}:
            abort(404)
        if error.code == "export_superseded":
            abort(409, description="error.export_superseded")
        if error.code in {"workflow_stale", "delivery_state_invalid", "export_state_invalid", "position_exists", "idempotency_conflict", "delivery_version_limit"}:
            abort(409, description="error.conflict_retry")
        if error.code in {"acceptance_integrity_invalid", "delivery_integrity_invalid", "export_integrity_invalid", "formal_integrity_invalid"}:
            abort(409, description="error.digest_mismatch")
        if error.code == "export_source_changed":
            abort(409, description="error.export_source_changed")
        abort(422, description="error.required_field")

    @localized("/cases/<case_id>/delivery")
    @current_user_required
    def delivery_dashboard(locale: str, case_id: str):
        try:
            view = load_delivery_dashboard_with_export_status(
                get_db(),
                Path(current_app.config["DATA_ROOT"]),
                current_app.config["DOCUMENT_ASSETS"],
                case_id,
                g.user["id"],
                Path(current_app.config["EVIDENCE_EXPORT_SCHEMA_PATH"]),
            )
        except (DeliveryWorkflowError, EvidenceExportError) as error:
            _delivery_error(error)
        return render_template(
            "delivery_dashboard.html",
            view=view,
            delivery_idempotency_key=str(uuid.uuid4()),
            inspection_idempotency_key=str(uuid.uuid4()),
        )

    @localized("/cases/<case_id>/deliveries", ("POST",))
    @current_user_required
    def delivery_submit(locale: str, case_id: str):
        view = _delivery_view_or_404(case_id)
        item_inputs = {
            criterion["criterion_id"]: (
                request.form.get(f"note_{criterion['criterion_id']}", ""),
                request.form.get(f"evidence_{criterion['criterion_id']}", ""),
            )
            for criterion in view["criteria"]
        }
        test_inputs = {
            criterion["criterion_id"]: (
                request.form.get(f"test_outcome_{criterion['criterion_id']}", ""),
                request.form.get(f"observation_{criterion['criterion_id']}", ""),
            )
            for criterion in view["criteria"]
        }
        try:
            submit_delivery(
                get_db(), Path(current_app.config["DATA_ROOT"]), current_app.config["DOCUMENT_ASSETS"],
                case_id, g.user["id"], request.form.get("summary", ""), item_inputs, test_inputs,
                request.form.get("idempotency_key", ""),
            )
        except DeliveryWorkflowError as error:
            _delivery_error(error)
        return redirect(url_for(f"{locale.replace('-', '_')}.delivery_dashboard", case_id=case_id))

    @localized("/cases/<case_id>/deliveries/<delivery_id>/download")
    @current_user_required
    def delivery_download(locale: str, case_id: str, delivery_id: str):
        try:
            row, raw = read_delivery_record(
                get_db(), Path(current_app.config["DATA_ROOT"]), current_app.config["DOCUMENT_ASSETS"],
                case_id, delivery_id, g.user["id"],
            )
        except DeliveryWorkflowError as error:
            _delivery_error(error)
        response = Response(raw, mimetype="application/json")
        response.headers["Content-Disposition"] = f'attachment; filename="delivery-{row["version_no"]}.json"'
        response.headers["Cache-Control"] = "no-store, private"
        return response

    @localized("/cases/<case_id>/deliveries/<delivery_id>/begin-inspection", ("POST",))
    @current_user_required
    def delivery_begin_inspection(locale: str, case_id: str, delivery_id: str):
        try:
            begin_inspection(
                get_db(), Path(current_app.config["DATA_ROOT"]), current_app.config["DOCUMENT_ASSETS"],
                case_id, delivery_id, g.user["id"], int(request.form.get("expected_revision", "")),
            )
        except (ValueError, DeliveryWorkflowError) as error:
            if isinstance(error, DeliveryWorkflowError):
                _delivery_error(error)
            abort(409, description="error.conflict_retry")
        return redirect(url_for(f"{locale.replace('-', '_')}.delivery_dashboard", case_id=case_id))

    @localized("/cases/<case_id>/deliveries/<delivery_id>/inspections", ("POST",))
    @current_user_required
    def delivery_inspect(locale: str, case_id: str, delivery_id: str):
        view = _delivery_view_or_404(case_id)
        results = {
            criterion["criterion_id"]: (
                request.form.get(f"result_{criterion['criterion_id']}", ""),
                request.form.get(f"reason_{criterion['criterion_id']}", ""),
            )
            for criterion in view["criteria"]
        }
        try:
            submit_inspection(
                get_db(), Path(current_app.config["DATA_ROOT"]), current_app.config["DOCUMENT_ASSETS"],
                case_id, delivery_id, g.user["id"], int(request.form.get("expected_revision", "")),
                results, request.form.get("explicit_acceptance") == "yes",
                request.form.get("idempotency_key", ""),
            )
        except (ValueError, DeliveryWorkflowError) as error:
            if isinstance(error, DeliveryWorkflowError):
                _delivery_error(error)
            abort(409, description="error.conflict_retry")
        return redirect(url_for(f"{locale.replace('-', '_')}.delivery_dashboard", case_id=case_id))

    @localized("/cases/<case_id>/disputes", ("POST",))
    @current_user_required
    def dispute_open(locale: str, case_id: str):
        try:
            open_dispute(
                get_db(), Path(current_app.config["DATA_ROOT"]), current_app.config["DOCUMENT_ASSETS"],
                case_id, g.user["id"], int(request.form.get("expected_revision", "")), request.form.get("reason", ""),
            )
        except (ValueError, DeliveryWorkflowError) as error:
            if isinstance(error, DeliveryWorkflowError):
                _delivery_error(error)
            abort(409, description="error.conflict_retry")
        return redirect(url_for(f"{locale.replace('-', '_')}.delivery_dashboard", case_id=case_id))

    @localized("/cases/<case_id>/disputes/<dispute_id>/positions", ("POST",))
    @current_user_required
    def dispute_position(locale: str, case_id: str, dispute_id: str):
        try:
            add_dispute_position(
                get_db(), Path(current_app.config["DATA_ROOT"]), current_app.config["DOCUMENT_ASSETS"],
                case_id, dispute_id, g.user["id"], request.form.get("position_text", ""),
            )
        except DeliveryWorkflowError as error:
            _delivery_error(error)
        return redirect(url_for(f"{locale.replace('-', '_')}.delivery_dashboard", case_id=case_id))

    @localized("/cases/<case_id>/evidence-exports", ("POST",))
    @current_user_required
    def evidence_export_prepare(locale: str, case_id: str):
        try:
            result = prepare_evidence_export(
                get_db(), Path(current_app.config["DATA_ROOT"]), current_app.config["DOCUMENT_ASSETS"],
                case_id, g.user["id"], Path(current_app.config["EVIDENCE_EXPORT_SCHEMA_PATH"]),
            )
            if request.args.get("download") == "1":
                row, raw = read_evidence_export(
                    get_db(), Path(current_app.config["DATA_ROOT"]), current_app.config["DOCUMENT_ASSETS"],
                    case_id, result.export_id, g.user["id"], Path(current_app.config["EVIDENCE_EXPORT_SCHEMA_PATH"]),
                )
                response = Response(raw, mimetype="application/zip")
                response.headers["Content-Disposition"] = f'attachment; filename="evidence-{row["version_no"]}.zip"'
                response.headers["Cache-Control"] = "no-store, private"
                return response
        except EvidenceExportError as error:
            _delivery_error(error)
        return redirect(url_for(f"{locale.replace('-', '_')}.delivery_dashboard", case_id=case_id))

    @localized("/cases/<case_id>/evidence-exports/<export_id>/download")
    @current_user_required
    def evidence_export_download(locale: str, case_id: str, export_id: str):
        try:
            row, raw = read_evidence_export(
                get_db(), Path(current_app.config["DATA_ROOT"]), current_app.config["DOCUMENT_ASSETS"],
                case_id, export_id, g.user["id"], Path(current_app.config["EVIDENCE_EXPORT_SCHEMA_PATH"]),
            )
        except EvidenceExportError as error:
            _delivery_error(error)
        response = Response(raw, mimetype="application/zip")
        response.headers["Content-Disposition"] = f'attachment; filename="evidence-{row["version_no"]}.zip"'
        response.headers["Cache-Control"] = "no-store, private"
        return response

    def formal_component_response(case_id: str, filename: str):
        try:
            formal = load_formal_version(
                get_db(), Path(current_app.config["DATA_ROOT"]),
                current_app.config["DOCUMENT_ASSETS"], case_id, g.user["id"],
            )
        except (CandidateWorkflowError, FormalizationWorkflowError):
            abort(404)
        media_types = {
            "manifest.json": "application/json",
            "document.pdf": "application/pdf",
            "diagram.svg": "image/svg+xml",
        }
        raw = formal["files"][filename]
        disposition = "attachment" if filename == "manifest.json" or request.args.get("download") == "1" else "inline"
        response = Response(raw, mimetype=media_types[filename])
        response.headers["Content-Disposition"] = f'{disposition}; filename="{filename}"'
        response.headers["Cache-Control"] = "no-store, private"
        return response

    @localized("/cases/<case_id>/formal/manifest.json")
    @current_user_required
    def formal_manifest(locale: str, case_id: str):
        return formal_component_response(case_id, "manifest.json")

    @localized("/cases/<case_id>/formal/document.pdf")
    @current_user_required
    def formal_pdf(locale: str, case_id: str):
        return formal_component_response(case_id, "document.pdf")

    @localized("/cases/<case_id>/formal/diagram.svg")
    @current_user_required
    def formal_svg(locale: str, case_id: str):
        return formal_component_response(case_id, "diagram.svg")

    def formal_source_response(case_id: str, source_kind: str, download_name: str):
        try:
            _artifact, raw = read_formal_source(
                get_db(),
                Path(current_app.config["DATA_ROOT"]),
                current_app.config["DOCUMENT_ASSETS"],
                case_id,
                g.user["id"],
                source_kind,
            )
        except (CandidateWorkflowError, FormalizationWorkflowError):
            abort(404)
        response = Response(raw, mimetype="application/json")
        response.headers["Content-Disposition"] = f'attachment; filename="{download_name}"'
        response.headers["Cache-Control"] = "no-store, private"
        return response

    @localized("/cases/<case_id>/formal/requirement.json")
    @current_user_required
    def formal_json_a(locale: str, case_id: str):
        return formal_source_response(case_id, "json_a", "requirement-json-a.json")

    @localized("/cases/<case_id>/formal/proposal.json")
    @current_user_required
    def formal_json_b(locale: str, case_id: str):
        return formal_source_response(case_id, "json_b", "proposal-json-b.json")

    @localized("/cases/<case_id>/separate-proposals", ("POST",))
    @current_user_required
    def separate_proposal_create(locale: str, case_id: str):
        try:
            child_case_id = create_separate_proposal(
                get_db(), case_id, g.user["id"], request.form.get("idempotency_key", "")
            )
        except FormalizationWorkflowError as error:
            _formalization_error(error)
        return redirect(url_for(
            f"{locale.replace('-', '_')}.requirement_section",
            case_id=child_case_id, section_no=1,
        ))

    @localized("/cases/<case_id>/json-b/reviews/<int:version_no>/download")
    @current_user_required
    def json_b_review_download(locale: str, case_id: str, version_no: int):
        try:
            row = load_review(get_db(), case_id, g.user["id"], version_no)
            _document, raw = read_review_bytes(Path(current_app.config["DATA_ROOT"]), row)
        except JsonBWorkflowError:
            abort(404)
        response = Response(raw, mimetype="application/json")
        response.headers["Content-Disposition"] = f'attachment; filename="json-b-review-{version_no}.json"'
        response.headers["Cache-Control"] = "no-store, private"
        return response

    @localized("/cases/<case_id>/json-b/reviews/<int:version_no>/objections", ("POST",))
    @current_user_required
    def json_b_objection(locale: str, case_id: str, version_no: int):
        case, role = load_case_party(
            case_id,
            allowed_states=("JSON_B_REVIEW", "JSON_B_PRIVATE_DRAFT"),
        )
        if role != "client":
            abort(404)
        try:
            row = load_review(get_db(), case_id, g.user["id"], version_no)
            if row["state"] != "CURRENT":
                raise JsonBWorkflowError("review_unavailable")
            document, _raw = read_review_bytes(Path(current_app.config["DATA_ROOT"]), row)
            pointer = request.form.get("field_pointer", "")
            validate_objection_pointer(document, pointer)
            create_issue(
                get_db(),
                case_id,
                g.user["id"],
                request.form.get("summary", ""),
                kind="JSON_B_OBJECTION",
                source_json_b_artifact_id=row["artifact_id"],
                field_pointer=pointer,
            )
        except (JsonBWorkflowError, DiscussionWorkflowError):
            abort(422, description="error.required_field")
        return redirect(url_for(f"{locale.replace('-', '_')}.case_room", case_id=case["id"]))

    @localized("/cases/<case_id>/json-b/reviews/<int:version_no>/revise", ("POST",))
    @current_user_required
    def json_b_revise(locale: str, case_id: str, version_no: int):
        _case, role = load_case_party(
            case_id,
            allowed_states=("JSON_B_REVIEW", "JSON_B_PRIVATE_DRAFT"),
        )
        if role != "contractor":
            abort(404)
        try:
            row = load_review(get_db(), case_id, g.user["id"], version_no)
            if row["state"] != "CURRENT":
                raise JsonBWorkflowError("review_unavailable")
            document, _raw = read_review_bytes(Path(current_app.config["DATA_ROOT"]), row)
            current = load_private_draft(get_db(), case_id, g.user["id"])
            if current is not None and current["base_review_artifact_id"] == row["artifact_id"]:
                return redirect(url_for(f"{locale.replace('-', '_')}.json_b_draft", case_id=case_id))
            if (
                current is not None
                and (
                    current["current_export_artifact_id"] != row["artifact_id"]
                    or current["target_version"] != row["version_no"]
                )
            ):
                raise JsonBWorkflowError("draft_conflict")
            expected_revision = 0 if current is None else current["content_revision"]
            business = {key: document[key] for key in BUSINESS_FIELDS}
            save_private_draft(
                get_db(),
                Path(current_app.config["DATA_ROOT"]),
                Path(current_app.config["JSON_B_SCHEMA_PATH"]),
                case_id,
                g.user["id"],
                expected_revision,
                json.dumps(business, ensure_ascii=False).encode("utf-8"),
            )
        except (KeyError, JsonBWorkflowError):
            abort(409, description="error.conflict_retry")
        return redirect(url_for(f"{locale.replace('-', '_')}.json_b_draft", case_id=case_id))

    @localized("/admin/accounts")
    @current_user_required
    @require_admin
    def admin_accounts(locale: str):
        users = get_db().execute("SELECT id, display_name, email_normalized, status, system_role FROM users ORDER BY email_normalized").fetchall()
        return render_template("admin.html", users=users, health=None)

    @localized("/admin/accounts/<user_id>/status", ("POST",))
    @current_user_required
    @require_admin
    def admin_account_status(locale: str, user_id: str):
        status = request.form.get("status")
        if status not in ("active", "disabled"):
            abort(400)
        with immediate_transaction(get_db()):
            target = get_db().execute("SELECT system_role FROM users WHERE id = ?", (user_id,)).fetchone()
            if target is None or target["system_role"] == "admin":
                abort(400)
            get_db().execute("UPDATE users SET status = ?, session_version = session_version + 1, updated_at = ? WHERE id = ?", (status, utc_now(), user_id))
        return redirect(url_for(f"{locale.replace('-', '_')}.admin_accounts"))

    @localized("/admin/service-health")
    @current_user_required
    @require_admin
    def admin_service_health(locale: str):
        database_ok = get_db().execute("SELECT 1").fetchone()[0] == 1
        data_root = Path(current_app.config["DATA_ROOT"])
        health = {"database": database_ok, "data_directory": data_root.is_dir() and os.access(data_root, os.W_OK), "schema": Path(current_app.config["SCHEMA_PATH"]).is_file(), "catalogs": set(current_app.config["CATALOGS"]) == set(SUPPORTED_LOCALES)}
        return render_template("admin.html", users=None, health=health)
