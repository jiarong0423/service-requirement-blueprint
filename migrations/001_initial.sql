CREATE TABLE users (
    id TEXT PRIMARY KEY,
    email_normalized TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    system_role TEXT NOT NULL CHECK (system_role IN ('member', 'admin')),
    status TEXT NOT NULL CHECK (status IN ('active', 'disabled')),
    session_version INTEGER NOT NULL DEFAULT 1 CHECK (session_version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE cases (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES users(id),
    contractor_id TEXT REFERENCES users(id),
    parent_case_id TEXT REFERENCES cases(id),
    title TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('REQUIREMENT_DRAFT')),
    content_revision INTEGER NOT NULL DEFAULT 1,
    current_json_a_artifact_id TEXT,
    current_json_b_artifact_id TEXT,
    active_candidate_id TEXT,
    formal_candidate_id TEXT,
    invitation_token_hash TEXT,
    invitation_email_normalized TEXT,
    invitation_state TEXT,
    invitation_expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (contractor_id IS NULL OR contractor_id <> owner_id)
);

CREATE TABLE requirement_drafts (
    case_id TEXT PRIMARY KEY REFERENCES cases(id),
    data_json TEXT NOT NULL,
    completed_sections_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE artifacts (
    id TEXT PRIMARY KEY,
    case_id TEXT REFERENCES cases(id),
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

CREATE TABLE audit_events (
    id TEXT PRIMARY KEY,
    actor_id TEXT,
    case_id TEXT,
    entity_id TEXT,
    action TEXT NOT NULL,
    result TEXT NOT NULL,
    server_created_at TEXT NOT NULL,
    request_correlation_id TEXT NOT NULL,
    locale_code TEXT
);

CREATE INDEX idx_cases_owner ON cases(owner_id);
CREATE INDEX idx_artifacts_case ON artifacts(case_id);
