PRAGMA defer_foreign_keys = ON;

CREATE TABLE cases_slice3_new (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES users(id),
    contractor_id TEXT REFERENCES users(id),
    parent_case_id TEXT REFERENCES cases_slice3_new(id),
    title TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('REQUIREMENT_DRAFT', 'INVITATION_PENDING', 'WAITING_JSON_A_IMPORT', 'CASE_ACTIVE')),
    content_revision INTEGER NOT NULL DEFAULT 1 CHECK (content_revision >= 1),
    current_json_a_artifact_id TEXT,
    current_json_b_artifact_id TEXT,
    active_candidate_id TEXT,
    formal_candidate_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (contractor_id IS NULL OR contractor_id <> owner_id)
);

CREATE TABLE requirement_drafts_slice3_new (
    case_id TEXT PRIMARY KEY REFERENCES cases_slice3_new(id),
    data_json TEXT NOT NULL,
    completed_sections_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE artifacts_slice3_new (
    id TEXT PRIMARY KEY,
    case_id TEXT REFERENCES cases_slice3_new(id),
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

INSERT INTO cases_slice3_new (
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

INSERT INTO requirement_drafts_slice3_new (
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

INSERT INTO artifacts_slice3_new (
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

DROP TABLE requirement_drafts;

DROP TABLE artifacts;

DROP TABLE cases;

ALTER TABLE cases_slice3_new RENAME TO cases;

ALTER TABLE requirement_drafts_slice3_new RENAME TO requirement_drafts;

ALTER TABLE artifacts_slice3_new RENAME TO artifacts;

CREATE INDEX idx_cases_owner ON cases(owner_id);

CREATE INDEX idx_cases_contractor ON cases(contractor_id);

CREATE INDEX idx_artifacts_case ON artifacts(case_id);

CREATE TABLE invitations (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    owner_id TEXT NOT NULL REFERENCES users(id),
    designated_user_id TEXT NOT NULL REFERENCES users(id),
    source_artifact_id TEXT NOT NULL REFERENCES artifacts(id),
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

CREATE UNIQUE INDEX idx_invitations_one_pending_per_case
ON invitations(case_id)
WHERE state = 'pending';

CREATE INDEX idx_invitations_designated_user
ON invitations(designated_user_id, state);

CREATE INDEX idx_invitations_case
ON invitations(case_id, created_at);

CREATE TABLE json_a_source_verifications (
    id TEXT PRIMARY KEY,
    invitation_id TEXT NOT NULL REFERENCES invitations(id),
    case_id TEXT NOT NULL REFERENCES cases(id),
    actor_id TEXT NOT NULL REFERENCES users(id),
    actor_role TEXT NOT NULL CHECK (actor_role IN ('owner', 'contractor')),
    artifact_id TEXT NOT NULL REFERENCES artifacts(id),
    supplied_sha256 TEXT NOT NULL CHECK (length(supplied_sha256) = 64 AND supplied_sha256 NOT GLOB '*[^0-9a-f]*'),
    supplied_byte_length INTEGER NOT NULL CHECK (supplied_byte_length > 0),
    schema_version TEXT NOT NULL,
    document_id TEXT NOT NULL,
    document_version INTEGER NOT NULL CHECK (document_version >= 1),
    verified_at TEXT NOT NULL,
    UNIQUE(invitation_id, actor_role)
);

CREATE INDEX idx_json_a_source_verifications_case
ON json_a_source_verifications(case_id, verified_at);
