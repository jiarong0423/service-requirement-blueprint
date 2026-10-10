"""Application package for the Service Requirement Blueprint PoC."""
from __future__ import annotations

import stat
import uuid
from pathlib import Path

from flask import Flask, abort, g, redirect, render_template, request
from werkzeug.exceptions import HTTPException

from app.config import SUPPORTED_LOCALES, build_config
from app.db import close_db, connect_database
from app.i18n import load_catalogs, translate
from app.presentation import (
    delivery_task,
    invitation_state_label,
    invitation_task,
    issue_kind_label,
    issue_state_label,
    screen_copy,
    stage_label,
    state_label,
    value_label,
)
from app.routes.localized import register_localized_routes
from app.security.auth import load_current_user
from app.security.csrf import ensure_nonce, validate_csrf
from app.services.case_workflow import collect_open_items
from app.services.candidate_workflow import CandidateWorkflowError, reconcile_candidate_storage
from app.services.document_integrity import load_and_validate_document_assets
from app.services.delivery_workflow import DeliveryWorkflowError, reconcile_delivery_storage
from app.services.evidence_export import EvidenceExportError, reconcile_evidence_export_storage
from app.services.json_b_workflow import JsonBWorkflowError, reconcile_json_artifact_storage


def create_app(test_config: dict[str, object] | None = None) -> Flask:
    project_root = Path(__file__).resolve().parents[1]
    app = Flask(__name__, instance_relative_config=True)
    if test_config is None:
        app.config.from_mapping(build_config(project_root))
    else:
        app.config.from_mapping(test_config)
        app.config.setdefault("INSTANCE_ROOT", Path(app.instance_path))
        app.config.setdefault("DATABASE", Path(app.config["INSTANCE_ROOT"]) / "app.sqlite3")
        app.config.setdefault("DATA_ROOT", Path(app.config["INSTANCE_ROOT"]) / "data")
        app.config.setdefault("MAX_CONTENT_LENGTH", 2_097_152)
        app.config.setdefault("SESSION_COOKIE_HTTPONLY", True)
        app.config.setdefault("SESSION_COOKIE_SAMESITE", "Lax")
        app.config.setdefault("SESSION_COOKIE_SECURE", False)
        app.config.setdefault("SESSION_REFRESH_EACH_REQUEST", False)

    app.config["PROJECT_ROOT"] = project_root
    app.config["SCHEMA_PATH"] = project_root / "app/schemas/json-a.v1.schema.json"
    app.config["JSON_B_SCHEMA_PATH"] = project_root / "app/schemas/json-b.v1.schema.json"
    app.config["CANDIDATE_MANIFEST_SCHEMA_PATH"] = project_root / "app/schemas/candidate-manifest.v1.schema.json"
    app.config["EVIDENCE_EXPORT_SCHEMA_PATH"] = project_root / "app/schemas/evidence-export-manifest.v1.schema.json"
    app.config["EVIDENCE_EXPORT_RECORD_SCHEMA_PATH"] = project_root / "app/schemas/evidence-export-records.v1.schema.json"
    app.config["DOCUMENT_ASSETS"] = load_and_validate_document_assets(project_root)
    app.config["CATALOGS"] = load_catalogs(project_root / "app/i18n")
    if app.config.get("TESTING"):
        Path(app.config["DATA_ROOT"]).mkdir(parents=True, mode=0o700, exist_ok=True)
    else:
        if not Path(app.config["DATABASE"]).is_file():
            raise RuntimeError("database_missing")
        if not Path(app.config["DATA_ROOT"]).is_dir():
            raise RuntimeError("data_root_missing")
        if stat.S_IMODE(Path(app.config["DATA_ROOT"]).stat().st_mode) != 0o700:
            raise RuntimeError("data_root_permissions_invalid")
        connection = connect_database(Path(app.config["DATABASE"]))
        try:
            applied = {
                row[0]
                for row in connection.execute("SELECT version FROM schema_migrations")
            }
            expected = {path.stem for path in (project_root / "migrations").glob("*.sql")}
            if applied != expected:
                raise RuntimeError("migration_missing")
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("database_integrity_invalid")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("database_foreign_key_invalid")
            try:
                reconcile_candidate_storage(
                    connection,
                    Path(app.config["DATA_ROOT"]),
                    app.config["DOCUMENT_ASSETS"],
                )
            except CandidateWorkflowError as error:
                raise RuntimeError("candidate_storage_invalid") from error
            try:
                reconcile_delivery_storage(connection, Path(app.config["DATA_ROOT"]))
            except DeliveryWorkflowError as error:
                raise RuntimeError("delivery_storage_invalid") from error
            try:
                reconcile_evidence_export_storage(
                    connection,
                    Path(app.config["DATA_ROOT"]),
                    Path(app.config["EVIDENCE_EXPORT_SCHEMA_PATH"]),
                )
            except EvidenceExportError as error:
                raise RuntimeError("evidence_export_storage_invalid") from error
            try:
                reconcile_json_artifact_storage(connection, Path(app.config["DATA_ROOT"]))
            except JsonBWorkflowError as error:
                raise RuntimeError("json_artifact_storage_invalid") from error
        finally:
            connection.close()
        for required in (
            project_root / "assets/fonts/NotoSansTC-Regular.ttf",
            project_root / "assets/fonts/NotoSansTC-Bold.ttf",
            Path(app.config["SCHEMA_PATH"]),
            Path(app.config["JSON_B_SCHEMA_PATH"]),
            Path(app.config["CANDIDATE_MANIFEST_SCHEMA_PATH"]),
            Path(app.config["EVIDENCE_EXPORT_SCHEMA_PATH"]),
            Path(app.config["EVIDENCE_EXPORT_RECORD_SCHEMA_PATH"]),
            project_root / "app/documents/candidate-labels.v1.json",
        ):
            if not required.is_file():
                raise RuntimeError("startup_asset_missing")

    app.teardown_appcontext(close_db)

    @app.before_request
    def enforce_request_contract() -> None:
        load_current_user()
        ensure_nonce()
        if request.method == "POST":
            handler = app.view_functions.get(request.endpoint or "")
            if handler is not None and getattr(handler, "requires_authenticated_user", False) and g.user is None:
                abort(401, description="error.session_expired")
            validate_csrf()

    @app.context_processor
    def template_contract() -> dict[str, object]:
        locale = request.view_args.get("locale", "en") if request.view_args else "en"
        catalogs = app.config["CATALOGS"]
        return {
            "locale": locale,
            "csrf_token": ensure_nonce(),
            "collect_open_items": collect_open_items,
            "tr": lambda key, **values: translate(catalogs, locale, key, **values),
            "ui": lambda screen, slot, **values: screen_copy(catalogs, locale, screen, slot, **values),
            "ui_state": lambda state: state_label(catalogs, locale, state),
            "ui_stage": lambda state: stage_label(catalogs, locale, state),
            "ui_issue_state": lambda state: issue_state_label(catalogs, locale, state),
            "ui_issue_kind": lambda kind: issue_kind_label(catalogs, locale, kind),
            "ui_value": lambda field_name, value, contractor_authored=False: value_label(
                catalogs, locale, field_name, value, contractor_authored
            ),
            "ui_invitation_state": lambda state: invitation_state_label(catalogs, locale, state),
            "ui_invitation_task": lambda state, role: invitation_task(catalogs, locale, state, role),
            "ui_delivery_task": lambda state, role, delivery_limit_reached=False: delivery_task(
                catalogs, locale, state, role, delivery_limit_reached=delivery_limit_reached
            ),
        }

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("Content-Security-Policy", "default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        return response

    @app.get("/")
    def root_redirect():
        return redirect("/en/", code=302)

    register_localized_routes(app)

    @app.errorhandler(HTTPException)
    def localized_http_error(error: HTTPException):
        path_parts = request.path.split("/")
        locale = path_parts[1] if len(path_parts) > 1 and path_parts[1] in SUPPORTED_LOCALES else None
        if locale is None:
            return "Not Found", error.code or 500
        description = error.description if isinstance(error.description, str) else "error.access_denied"
        key = description if description in app.config["CATALOGS"][locale] else "error.access_denied"
        status = error.code or 500
        title_key = {
            400: "error.title_input",
            401: "error.title_sign_in",
            403: "error.title_access",
            404: "error.title_not_found",
            409: "error.title_changed",
            422: "error.title_input",
        }.get(status, "error.title_problem")
        return render_template(
            "error.html",
            error_title=translate(app.config["CATALOGS"], locale, title_key),
            message=translate(app.config["CATALOGS"], locale, key),
        ), status

    @app.errorhandler(Exception)
    def sanitized_internal_error(_error: Exception):
        correlation_id = str(uuid.uuid4())
        app.logger.error(
            "request_failed correlation_id=%s method=%s endpoint=%s",
            correlation_id,
            request.method,
            request.endpoint or "unknown",
        )
        path_parts = request.path.split("/")
        locale = path_parts[1] if len(path_parts) > 1 and path_parts[1] in SUPPORTED_LOCALES else None
        if locale is None:
            return "Internal Server Error", 500
        message = f"{translate(app.config['CATALOGS'], locale, 'error.internal')} Reference: {correlation_id}"
        return render_template(
            "error.html",
            error_title=translate(app.config["CATALOGS"], locale, "error.title_problem"),
            message=message,
        ), 500

    return app
