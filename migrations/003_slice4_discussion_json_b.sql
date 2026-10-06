PRAGMA defer_foreign_keys = ON;

CREATE TABLE cases_slice4_new (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES users(id),
    contractor_id TEXT REFERENCES users(id),
    parent_case_id TEXT REFERENCES cases_slice4_new(id),
    title TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'REQUIREMENT_DRAFT',
        'INVITATION_PENDING',
        'WAITING_JSON_A_IMPORT',
        'CASE_ACTIVE',
        'JSON_B_PRIVATE_DRAFT',
        'JSON_B_REVIEW'
    )),
    content_revision INTEGER NOT NULL DEFAULT 1 CHECK (content_revision >= 1),
    current_json_a_artifact_id TEXT,
    current_json_b_artifact_id TEXT,
    active_candidate_id TEXT,
    formal_candidate_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (contractor_id IS NULL OR contractor_id <> owner_id)
);

CREATE TABLE requirement_drafts_slice4_new (
    case_id TEXT PRIMARY KEY REFERENCES cases_slice4_new(id),
    data_json TEXT NOT NULL,
    completed_sections_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE artifacts_slice4_new (
    id TEXT PRIMARY KEY,
    case_id TEXT REFERENCES cases_slice4_new(id),
    author_id TEXT NOT NULL REFERENCES users(id),
    kind TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    visibility TEXT NOT NULL CHECK (visibility IN ('private', 'submitted', 'party_only')),
    lifecycle TEXT NOT NULL CHECK (lifecycle IN ('active', 'superseded', 'invalidated', 'formal', 'failed')),
    relative_path TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL,
    byte_length INTEGER NOT NULL,
    schema_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(case_id, kind, version_no)
);

CREATE TABLE invitations_slice4_new (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases_slice4_new(id),
    owner_id TEXT NOT NULL REFERENCES users(id),
    designated_user_id TEXT NOT NULL REFERENCES users(id),
    source_artifact_id TEXT NOT NULL REFERENCES artifacts_slice4_new(id),
    source_version_no INTEGER NOT NULL CHECK (source_version_no >= 1),
    source_sha256 TEXT NOT NULL CHECK (length(source_sha256) = 64 AND source_sha256 NOT GLOB '*[^0-9a-f]*'),
    source_byte_length INTEGER NOT NULL CHECK (source_byte_length > 0),
    source_schema_version TEXT NOT NULL,
    source_document_id TEXT NOT NULL,
    source_content_revision INTEGER NOT NULL CHECK (source_content_revision >= 1),
    token_hash TEXT NOT NULL UNIQUE CHECK (length(token_hash) = 64 AND token_hash NOT GLOB '*[^0-9a-f]*'),
    state TEXT NOT NULL CHECK (state IN ('pending', 'accepted', 'rejected', 'revoked', 'expired')),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    accepted_at TEXT,
    rejected_at TEXT,
    revoked_at TEXT,
    expired_at TEXT,
    source_invalidated_at TEXT,
    source_invalidation_reason TEXT,
    updated_at TEXT NOT NULL,
    CHECK (designated_user_id <> owner_id),
    CHECK (
        (state = 'pending' AND accepted_at IS NULL AND rejected_at IS NULL AND revoked_at IS NULL AND expired_at IS NULL)
        OR (state = 'accepted' AND accepted_at IS NOT NULL AND rejected_at IS NULL AND revoked_at IS NULL AND expired_at IS NULL)
        OR (state = 'rejected' AND accepted_at IS NULL AND rejected_at IS NOT NULL AND revoked_at IS NULL AND expired_at IS NULL)
        OR (state = 'revoked' AND accepted_at IS NULL AND rejected_at IS NULL AND revoked_at IS NOT NULL AND expired_at IS NULL)
        OR (state = 'expired' AND accepted_at IS NULL AND rejected_at IS NULL AND revoked_at IS NULL AND expired_at IS NOT NULL)
    )
);

CREATE TABLE json_a_source_verifications_slice4_new (
    id TEXT PRIMARY KEY,
    invitation_id TEXT NOT NULL REFERENCES invitations_slice4_new(id),
    case_id TEXT NOT NULL REFERENCES cases_slice4_new(id),
    actor_id TEXT NOT NULL REFERENCES users(id),
    actor_role TEXT NOT NULL CHECK (actor_role IN ('owner', 'contractor')),
    artifact_id TEXT NOT NULL REFERENCES artifacts_slice4_new(id),
    supplied_sha256 TEXT NOT NULL CHECK (length(supplied_sha256) = 64 AND supplied_sha256 NOT GLOB '*[^0-9a-f]*'),
    supplied_byte_length INTEGER NOT NULL CHECK (supplied_byte_length > 0),
    schema_version TEXT NOT NULL,
    document_id TEXT NOT NULL,
    document_version INTEGER NOT NULL CHECK (document_version >= 1),
    verified_at TEXT NOT NULL,
    UNIQUE(invitation_id, actor_role)
);

INSERT INTO cases_slice4_new (
    id,
    owner_id,
    contractor_id,
    parent_case_id,
    title,
    state,
    content_revision,
    current_json_a_artifact_id,
    current_json_b_artifact_id,
    active_candidate_id,
    formal_candidate_id,
    created_at,
    updated_at
)
SELECT
    id,
    owner_id,
    contractor_id,
    parent_case_id,
    title,
    state,
    content_revision,
    current_json_a_artifact_id,
    current_json_b_artifact_id,
    active_candidate_id,
    formal_candidate_id,
    created_at,
    updated_at
FROM cases;

INSERT INTO requirement_drafts_slice4_new (
    case_id,
    data_json,
    completed_sections_json,
    updated_at
)
SELECT
    case_id,
    data_json,
    completed_sections_json,
    updated_at
FROM requirement_drafts;

INSERT INTO artifacts_slice4_new (
    id,
    case_id,
    author_id,
    kind,
    version_no,
    visibility,
    lifecycle,
    relative_path,
    sha256,
    byte_length,
    schema_version,
    created_at
)
SELECT
    id,
    case_id,
    author_id,
    kind,
    version_no,
    visibility,
    lifecycle,
    relative_path,
    sha256,
    byte_length,
    schema_version,
    created_at
FROM artifacts;

INSERT INTO invitations_slice4_new (
    id,
    case_id,
    owner_id,
    designated_user_id,
    source_artifact_id,
    source_version_no,
    source_sha256,
    source_byte_length,
    source_schema_version,
    source_document_id,
    source_content_revision,
    token_hash,
    state,
    created_at,
    expires_at,
    accepted_at,
    rejected_at,
    revoked_at,
    expired_at,
    source_invalidated_at,
    source_invalidation_reason,
    updated_at
)
SELECT
    id,
    case_id,
    owner_id,
    designated_user_id,
    source_artifact_id,
    source_version_no,
    source_sha256,
    source_byte_length,
    source_schema_version,
    source_document_id,
    source_content_revision,
    token_hash,
    state,
    created_at,
    expires_at,
    accepted_at,
    rejected_at,
    revoked_at,
    expired_at,
    source_invalidated_at,
    source_invalidation_reason,
    updated_at
FROM invitations;

INSERT INTO json_a_source_verifications_slice4_new (
    id,
    invitation_id,
    case_id,
    actor_id,
    actor_role,
    artifact_id,
    supplied_sha256,
    supplied_byte_length,
    schema_version,
    document_id,
    document_version,
    verified_at
)
SELECT
    id,
    invitation_id,
    case_id,
    actor_id,
    actor_role,
    artifact_id,
    supplied_sha256,
    supplied_byte_length,
    schema_version,
    document_id,
    document_version,
    verified_at
FROM json_a_source_verifications;

DROP TABLE json_a_source_verifications;

DROP TABLE invitations;

DROP TABLE requirement_drafts;

DROP TABLE artifacts;

DROP TABLE cases;

ALTER TABLE cases_slice4_new RENAME TO cases;

ALTER TABLE requirement_drafts_slice4_new RENAME TO requirement_drafts;

ALTER TABLE artifacts_slice4_new RENAME TO artifacts;

ALTER TABLE invitations_slice4_new RENAME TO invitations;

ALTER TABLE json_a_source_verifications_slice4_new RENAME TO json_a_source_verifications;

CREATE INDEX idx_cases_owner ON cases(owner_id);

CREATE INDEX idx_cases_contractor ON cases(contractor_id);

CREATE INDEX idx_artifacts_case ON artifacts(case_id);

CREATE UNIQUE INDEX idx_invitations_one_pending_per_case
ON invitations(case_id)
WHERE state = 'pending';

CREATE INDEX idx_invitations_designated_user
ON invitations(designated_user_id, state);

CREATE INDEX idx_invitations_case
ON invitations(case_id, created_at);

CREATE INDEX idx_json_a_source_verifications_case
ON json_a_source_verifications(case_id, verified_at);

CREATE TABLE case_issues (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    kind TEXT NOT NULL CHECK (kind IN ('CLARIFICATION', 'JSON_B_OBJECTION')),
    source_json_b_artifact_id TEXT REFERENCES artifacts(id),
    field_pointer TEXT,
    summary TEXT NOT NULL CHECK (length(summary) BETWEEN 1 AND 4000),
    opened_by_id TEXT NOT NULL REFERENCES users(id),
    opened_by_role TEXT NOT NULL CHECK (opened_by_role IN ('client', 'contractor')),
    state TEXT NOT NULL CHECK (state IN (
        'OPEN',
        'PROPOSED',
        'REVISION_REQUESTED',
        'RESOLVED',
        'OUT_OF_SCOPE_PROPOSED',
        'OUT_OF_SCOPE'
    )),
    proposal_role TEXT CHECK (proposal_role IS NULL OR proposal_role IN ('client', 'contractor')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (kind = 'CLARIFICATION' AND source_json_b_artifact_id IS NULL)
        OR (
            kind = 'JSON_B_OBJECTION'
            AND source_json_b_artifact_id IS NOT NULL
            AND field_pointer IS NOT NULL
            AND substr(field_pointer, 1, 1) = '/'
        )
    )
);

CREATE INDEX idx_case_issues_case_state
ON case_issues(case_id, state, created_at);

CREATE TABLE case_messages (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    issue_id TEXT REFERENCES case_issues(id),
    author_id TEXT NOT NULL REFERENCES users(id),
    author_role TEXT NOT NULL CHECK (author_role IN ('client', 'contractor')),
    correction_of_id TEXT REFERENCES case_messages(id),
    idempotency_key TEXT NOT NULL,
    body TEXT NOT NULL CHECK (length(body) BETWEEN 1 AND 4000),
    server_created_at TEXT NOT NULL,
    UNIQUE(case_id, author_id, idempotency_key),
    CHECK (correction_of_id IS NULL OR correction_of_id <> id)
);

CREATE INDEX idx_case_messages_case_created
ON case_messages(case_id, server_created_at, id);

CREATE INDEX idx_case_messages_issue
ON case_messages(issue_id, server_created_at, id);

CREATE TABLE issue_events (
    id TEXT PRIMARY KEY,
    issue_id TEXT NOT NULL REFERENCES case_issues(id),
    actor_id TEXT NOT NULL REFERENCES users(id),
    actor_role TEXT NOT NULL CHECK (actor_role IN ('client', 'contractor')),
    action TEXT NOT NULL CHECK (action IN (
        'OPENED',
        'PROPOSED',
        'REVISION_REQUESTED',
        'RESOLVED',
        'OUT_OF_SCOPE_PROPOSED',
        'OUT_OF_SCOPE'
    )),
    from_state TEXT CHECK (from_state IS NULL OR from_state IN (
        'OPEN',
        'PROPOSED',
        'REVISION_REQUESTED',
        'RESOLVED',
        'OUT_OF_SCOPE_PROPOSED',
        'OUT_OF_SCOPE'
    )),
    to_state TEXT NOT NULL CHECK (to_state IN (
        'OPEN',
        'PROPOSED',
        'REVISION_REQUESTED',
        'RESOLVED',
        'OUT_OF_SCOPE_PROPOSED',
        'OUT_OF_SCOPE'
    )),
    note TEXT NOT NULL CHECK (length(note) <= 4000),
    related_json_b_artifact_id TEXT REFERENCES artifacts(id),
    issue_revision INTEGER NOT NULL CHECK (issue_revision >= 1),
    server_created_at TEXT NOT NULL,
    UNIQUE(issue_id, issue_revision)
);

CREATE INDEX idx_issue_events_issue_revision
ON issue_events(issue_id, issue_revision);

CREATE TABLE json_b_drafts (
    case_id TEXT PRIMARY KEY REFERENCES cases(id),
    contractor_id TEXT NOT NULL REFERENCES users(id),
    document_id TEXT NOT NULL UNIQUE,
    target_version INTEGER NOT NULL CHECK (target_version >= 1),
    base_review_artifact_id TEXT REFERENCES artifacts(id),
    source_artifact_id TEXT NOT NULL REFERENCES artifacts(id),
    source_document_id TEXT NOT NULL,
    source_version_no INTEGER NOT NULL CHECK (source_version_no >= 1),
    source_schema_version TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK (length(source_sha256) = 64 AND source_sha256 NOT GLOB '*[^0-9a-f]*'),
    source_byte_length INTEGER NOT NULL CHECK (source_byte_length > 0),
    data_json TEXT NOT NULL,
    content_revision INTEGER NOT NULL DEFAULT 1 CHECK (content_revision >= 1),
    current_export_artifact_id TEXT REFERENCES artifacts(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE json_b_private_exports (
    artifact_id TEXT PRIMARY KEY REFERENCES artifacts(id),
    case_id TEXT NOT NULL REFERENCES cases(id),
    contractor_id TEXT NOT NULL REFERENCES users(id),
    draft_revision INTEGER NOT NULL CHECK (draft_revision >= 1),
    target_version INTEGER NOT NULL CHECK (target_version >= 1),
    source_artifact_id TEXT NOT NULL REFERENCES artifacts(id),
    source_document_id TEXT NOT NULL,
    source_version_no INTEGER NOT NULL CHECK (source_version_no >= 1),
    source_schema_version TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK (length(source_sha256) = 64 AND source_sha256 NOT GLOB '*[^0-9a-f]*'),
    source_byte_length INTEGER NOT NULL CHECK (source_byte_length > 0),
    state TEXT NOT NULL CHECK (state IN ('ACTIVE', 'SUBMITTED', 'INVALIDATED')),
    created_at TEXT NOT NULL,
    submitted_at TEXT,
    invalidated_at TEXT,
    CHECK (
        (state = 'ACTIVE' AND submitted_at IS NULL AND invalidated_at IS NULL)
        OR (state = 'SUBMITTED' AND submitted_at IS NOT NULL AND invalidated_at IS NULL)
        OR (state = 'INVALIDATED' AND submitted_at IS NULL AND invalidated_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX idx_json_b_private_exports_one_active
ON json_b_private_exports(case_id)
WHERE state = 'ACTIVE';

CREATE INDEX idx_json_b_private_exports_case_created
ON json_b_private_exports(case_id, created_at);

CREATE TABLE json_b_versions (
    artifact_id TEXT PRIMARY KEY REFERENCES json_b_private_exports(artifact_id),
    case_id TEXT NOT NULL REFERENCES cases(id),
    contractor_id TEXT NOT NULL REFERENCES users(id),
    document_id TEXT NOT NULL,
    version_no INTEGER NOT NULL CHECK (version_no >= 1),
    source_artifact_id TEXT NOT NULL REFERENCES artifacts(id),
    source_document_id TEXT NOT NULL,
    source_version_no INTEGER NOT NULL CHECK (source_version_no >= 1),
    source_schema_version TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK (length(source_sha256) = 64 AND source_sha256 NOT GLOB '*[^0-9a-f]*'),
    source_byte_length INTEGER NOT NULL CHECK (source_byte_length > 0),
    state TEXT NOT NULL CHECK (state IN ('CURRENT', 'SUPERSEDED')),
    quote_expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    superseded_at TEXT,
    UNIQUE(case_id, version_no),
    CHECK (
        (state = 'CURRENT' AND superseded_at IS NULL)
        OR (state = 'SUPERSEDED' AND superseded_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX idx_json_b_versions_one_current
ON json_b_versions(case_id)
WHERE state = 'CURRENT';

CREATE INDEX idx_json_b_versions_case_version
ON json_b_versions(case_id, version_no);

CREATE TRIGGER case_messages_append_only_update
BEFORE UPDATE ON case_messages
BEGIN
    SELECT RAISE(ABORT, 'case_messages_append_only');
END;

CREATE TRIGGER case_messages_append_only_delete
BEFORE DELETE ON case_messages
BEGIN
    SELECT RAISE(ABORT, 'case_messages_append_only');
END;

CREATE TRIGGER issue_events_append_only_update
BEFORE UPDATE ON issue_events
BEGIN
    SELECT RAISE(ABORT, 'issue_events_append_only');
END;

CREATE TRIGGER issue_events_append_only_delete
BEFORE DELETE ON issue_events
BEGIN
    SELECT RAISE(ABORT, 'issue_events_append_only');
END;
