PRAGMA defer_foreign_keys = ON;

DROP TRIGGER IF EXISTS cases_candidate_pointer_insert_guard;
DROP TRIGGER IF EXISTS cases_candidate_pointer_update_guard;
DROP TRIGGER IF EXISTS candidate_versions_pointed_delete_guard;
DROP TRIGGER IF EXISTS candidate_versions_pointed_update_guard;

CREATE TABLE cases_slice6_new (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES users(id),
    contractor_id TEXT REFERENCES users(id),
    parent_case_id TEXT REFERENCES cases_slice6_new(id),
    title TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'REQUIREMENT_DRAFT',
        'INVITATION_PENDING',
        'WAITING_JSON_A_IMPORT',
        'CASE_ACTIVE',
        'JSON_B_PRIVATE_DRAFT',
        'JSON_B_REVIEW',
        'CANDIDATE_REVIEW',
        'FORMAL'
    )),
    content_revision INTEGER NOT NULL DEFAULT 1 CHECK (content_revision >= 1),
    current_json_a_artifact_id TEXT,
    current_json_b_artifact_id TEXT,
    active_candidate_id TEXT,
    formal_candidate_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (contractor_id IS NULL OR contractor_id <> owner_id),
    CHECK (
        (state = 'CANDIDATE_REVIEW' AND active_candidate_id IS NOT NULL AND formal_candidate_id IS NULL)
        OR (state = 'FORMAL' AND active_candidate_id IS NULL AND formal_candidate_id IS NOT NULL)
        OR (state NOT IN ('CANDIDATE_REVIEW', 'FORMAL') AND active_candidate_id IS NULL AND formal_candidate_id IS NULL)
    )
);

CREATE TABLE candidate_versions_slice6_new (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases_slice6_new(id),
    version_no INTEGER NOT NULL CHECK (version_no >= 1),
    created_by_id TEXT NOT NULL REFERENCES users(id),
    state TEXT NOT NULL CHECK (state IN ('CURRENT', 'SUPERSEDED', 'INVALIDATED', 'FORMAL')),
    review_revision INTEGER NOT NULL DEFAULT 1 CHECK (review_revision >= 1),
    json_a_artifact_id TEXT NOT NULL REFERENCES artifacts(id),
    json_a_sha256 TEXT NOT NULL CHECK (
        length(json_a_sha256) = 64
        AND json_a_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    json_a_byte_length INTEGER NOT NULL CHECK (json_a_byte_length > 0),
    json_b_artifact_id TEXT NOT NULL REFERENCES artifacts(id),
    json_b_sha256 TEXT NOT NULL CHECK (
        length(json_b_sha256) = 64
        AND json_b_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    json_b_byte_length INTEGER NOT NULL CHECK (json_b_byte_length > 0),
    manifest_artifact_id TEXT NOT NULL UNIQUE REFERENCES artifacts(id),
    pdf_artifact_id TEXT NOT NULL UNIQUE REFERENCES artifacts(id),
    svg_artifact_id TEXT NOT NULL UNIQUE REFERENCES artifacts(id),
    manifest_schema_version TEXT NOT NULL CHECK (length(manifest_schema_version) BETWEEN 1 AND 100),
    template_version TEXT NOT NULL CHECK (length(template_version) BETWEEN 1 AND 100),
    catalog_sha256 TEXT NOT NULL CHECK (
        length(catalog_sha256) = 64
        AND catalog_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    label_map_sha256 TEXT NOT NULL CHECK (
        length(label_map_sha256) = 64
        AND label_map_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL,
    superseded_at TEXT,
    invalidated_at TEXT,
    formalized_at TEXT,
    invalidation_reason TEXT CHECK (invalidation_reason IS NULL OR length(invalidation_reason) BETWEEN 1 AND 200),
    UNIQUE(case_id, version_no),
    CHECK (
        json_a_artifact_id <> json_b_artifact_id
        AND manifest_artifact_id <> json_a_artifact_id
        AND manifest_artifact_id <> json_b_artifact_id
        AND pdf_artifact_id <> json_a_artifact_id
        AND pdf_artifact_id <> json_b_artifact_id
        AND svg_artifact_id <> json_a_artifact_id
        AND svg_artifact_id <> json_b_artifact_id
        AND manifest_artifact_id <> pdf_artifact_id
        AND manifest_artifact_id <> svg_artifact_id
        AND pdf_artifact_id <> svg_artifact_id
    ),
    CHECK (
        (state = 'CURRENT' AND superseded_at IS NULL AND invalidated_at IS NULL AND formalized_at IS NULL AND invalidation_reason IS NULL)
        OR (state = 'SUPERSEDED' AND superseded_at IS NOT NULL AND invalidated_at IS NULL AND formalized_at IS NULL AND invalidation_reason IS NULL)
        OR (state = 'INVALIDATED' AND superseded_at IS NULL AND invalidated_at IS NOT NULL AND formalized_at IS NULL AND invalidation_reason IS NOT NULL)
        OR (state = 'FORMAL' AND superseded_at IS NULL AND invalidated_at IS NULL AND formalized_at IS NOT NULL AND invalidation_reason IS NULL)
    )
);

INSERT INTO cases_slice6_new (
    id, owner_id, contractor_id, parent_case_id, title, state, content_revision,
    current_json_a_artifact_id, current_json_b_artifact_id, active_candidate_id,
    formal_candidate_id, created_at, updated_at
)
SELECT
    id, owner_id, contractor_id, parent_case_id, title,
    CASE WHEN state = 'CANDIDATE_REVIEW' THEN 'JSON_B_REVIEW' ELSE state END,
    content_revision,
    current_json_a_artifact_id, current_json_b_artifact_id,
    CASE WHEN state = 'CANDIDATE_REVIEW' THEN NULL ELSE active_candidate_id END,
    formal_candidate_id, created_at, updated_at
FROM cases;

INSERT INTO candidate_versions_slice6_new (
    id, case_id, version_no, created_by_id, state, review_revision,
    json_a_artifact_id, json_a_sha256, json_a_byte_length,
    json_b_artifact_id, json_b_sha256, json_b_byte_length,
    manifest_artifact_id, pdf_artifact_id, svg_artifact_id,
    manifest_schema_version, template_version, catalog_sha256, label_map_sha256,
    created_at, superseded_at, invalidated_at, formalized_at, invalidation_reason
)
SELECT
    id, case_id, version_no, created_by_id,
    CASE WHEN state = 'CURRENT' THEN 'INVALIDATED' ELSE state END,
    1,
    json_a_artifact_id, json_a_sha256, json_a_byte_length,
    json_b_artifact_id, json_b_sha256, json_b_byte_length,
    manifest_artifact_id, pdf_artifact_id, svg_artifact_id,
    manifest_schema_version, template_version, catalog_sha256, label_map_sha256,
    created_at,
    superseded_at,
    CASE WHEN state = 'CURRENT' THEN strftime('%Y-%m-%dT%H:%M:%fZ','now') ELSE NULL END,
    NULL,
    CASE WHEN state = 'CURRENT' THEN 'fixed_document_lifecycle_wording_superseded' ELSE NULL END
FROM candidate_versions;

UPDATE artifacts
SET lifecycle = 'invalidated'
WHERE id IN (
    SELECT manifest_artifact_id FROM candidate_versions WHERE state = 'CURRENT'
    UNION
    SELECT pdf_artifact_id FROM candidate_versions WHERE state = 'CURRENT'
    UNION
    SELECT svg_artifact_id FROM candidate_versions WHERE state = 'CURRENT'
);

DROP TABLE candidate_versions;
DROP TABLE cases;

ALTER TABLE cases_slice6_new RENAME TO cases;
ALTER TABLE candidate_versions_slice6_new RENAME TO candidate_versions;

CREATE INDEX idx_cases_owner ON cases(owner_id);
CREATE INDEX idx_cases_contractor ON cases(contractor_id);

CREATE UNIQUE INDEX idx_candidate_versions_one_current
ON candidate_versions(case_id)
WHERE state = 'CURRENT';

CREATE UNIQUE INDEX idx_candidate_versions_one_formal
ON candidate_versions(case_id)
WHERE state = 'FORMAL';

CREATE INDEX idx_candidate_versions_case_version
ON candidate_versions(case_id, version_no);

CREATE TABLE candidate_review_rounds (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    candidate_id TEXT NOT NULL UNIQUE REFERENCES candidate_versions(id),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    state TEXT NOT NULL CHECK (state IN ('OPEN', 'INVALIDATED', 'FORMALIZED')),
    manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64 AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
    pdf_sha256 TEXT NOT NULL CHECK (length(pdf_sha256) = 64 AND pdf_sha256 NOT GLOB '*[^0-9a-f]*'),
    svg_sha256 TEXT NOT NULL CHECK (length(svg_sha256) = 64 AND svg_sha256 NOT GLOB '*[^0-9a-f]*'),
    json_a_sha256 TEXT NOT NULL CHECK (length(json_a_sha256) = 64 AND json_a_sha256 NOT GLOB '*[^0-9a-f]*'),
    json_b_sha256 TEXT NOT NULL CHECK (length(json_b_sha256) = 64 AND json_b_sha256 NOT GLOB '*[^0-9a-f]*'),
    created_at TEXT NOT NULL,
    invalidated_at TEXT,
    invalidated_by_id TEXT REFERENCES users(id),
    revision_reason TEXT CHECK (revision_reason IS NULL OR length(revision_reason) BETWEEN 1 AND 4000),
    formalized_at TEXT,
    CHECK (
        (state = 'OPEN' AND invalidated_at IS NULL AND invalidated_by_id IS NULL AND revision_reason IS NULL AND formalized_at IS NULL)
        OR (state = 'INVALIDATED' AND invalidated_at IS NOT NULL AND invalidated_by_id IS NOT NULL AND revision_reason IS NOT NULL AND formalized_at IS NULL)
        OR (state = 'FORMALIZED' AND invalidated_at IS NULL AND invalidated_by_id IS NULL AND revision_reason IS NULL AND formalized_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX idx_candidate_rounds_one_open
ON candidate_review_rounds(case_id)
WHERE state = 'OPEN';

CREATE UNIQUE INDEX idx_candidate_rounds_one_formal
ON candidate_review_rounds(case_id)
WHERE state = 'FORMALIZED';

INSERT INTO candidate_review_rounds (
    id, case_id, candidate_id, revision, state,
    manifest_sha256, pdf_sha256, svg_sha256, json_a_sha256, json_b_sha256,
    created_at
)
SELECT
    candidate.id, candidate.case_id, candidate.id, candidate.review_revision, 'OPEN',
    manifest.sha256, pdf.sha256, svg.sha256, candidate.json_a_sha256, candidate.json_b_sha256,
    candidate.created_at
FROM candidate_versions AS candidate
JOIN artifacts AS manifest ON manifest.id = candidate.manifest_artifact_id
JOIN artifacts AS pdf ON pdf.id = candidate.pdf_artifact_id
JOIN artifacts AS svg ON svg.id = candidate.svg_artifact_id
WHERE candidate.state = 'CURRENT';

CREATE TABLE candidate_acknowledgements (
    id TEXT PRIMARY KEY,
    round_id TEXT NOT NULL REFERENCES candidate_review_rounds(id),
    case_id TEXT NOT NULL REFERENCES cases(id),
    candidate_id TEXT NOT NULL REFERENCES candidate_versions(id),
    actor_id TEXT NOT NULL REFERENCES users(id),
    actor_role TEXT NOT NULL CHECK (actor_role IN ('client', 'contractor')),
    manifest_artifact_id TEXT NOT NULL REFERENCES artifacts(id),
    manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64 AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
    manifest_byte_length INTEGER NOT NULL CHECK (manifest_byte_length > 0),
    pdf_artifact_id TEXT NOT NULL REFERENCES artifacts(id),
    pdf_sha256 TEXT NOT NULL CHECK (length(pdf_sha256) = 64 AND pdf_sha256 NOT GLOB '*[^0-9a-f]*'),
    pdf_byte_length INTEGER NOT NULL CHECK (pdf_byte_length > 0),
    svg_artifact_id TEXT NOT NULL REFERENCES artifacts(id),
    svg_sha256 TEXT NOT NULL CHECK (length(svg_sha256) = 64 AND svg_sha256 NOT GLOB '*[^0-9a-f]*'),
    svg_byte_length INTEGER NOT NULL CHECK (svg_byte_length > 0),
    json_a_artifact_id TEXT NOT NULL REFERENCES artifacts(id),
    json_a_sha256 TEXT NOT NULL CHECK (length(json_a_sha256) = 64 AND json_a_sha256 NOT GLOB '*[^0-9a-f]*'),
    json_a_byte_length INTEGER NOT NULL CHECK (json_a_byte_length > 0),
    json_b_artifact_id TEXT NOT NULL REFERENCES artifacts(id),
    json_b_sha256 TEXT NOT NULL CHECK (length(json_b_sha256) = 64 AND json_b_sha256 NOT GLOB '*[^0-9a-f]*'),
    json_b_byte_length INTEGER NOT NULL CHECK (json_b_byte_length > 0),
    acknowledged_at TEXT NOT NULL,
    UNIQUE(round_id, actor_role),
    UNIQUE(round_id, actor_id)
);

CREATE TABLE candidate_confirmations (
    id TEXT PRIMARY KEY,
    round_id TEXT NOT NULL REFERENCES candidate_review_rounds(id),
    case_id TEXT NOT NULL REFERENCES cases(id),
    candidate_id TEXT NOT NULL REFERENCES candidate_versions(id),
    actor_id TEXT NOT NULL REFERENCES users(id),
    actor_role TEXT NOT NULL CHECK (actor_role IN ('client', 'contractor')),
    manifest_artifact_id TEXT NOT NULL REFERENCES artifacts(id),
    manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64 AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
    manifest_byte_length INTEGER NOT NULL CHECK (manifest_byte_length > 0),
    pdf_artifact_id TEXT NOT NULL REFERENCES artifacts(id),
    pdf_sha256 TEXT NOT NULL CHECK (length(pdf_sha256) = 64 AND pdf_sha256 NOT GLOB '*[^0-9a-f]*'),
    pdf_byte_length INTEGER NOT NULL CHECK (pdf_byte_length > 0),
    svg_artifact_id TEXT NOT NULL REFERENCES artifacts(id),
    svg_sha256 TEXT NOT NULL CHECK (length(svg_sha256) = 64 AND svg_sha256 NOT GLOB '*[^0-9a-f]*'),
    svg_byte_length INTEGER NOT NULL CHECK (svg_byte_length > 0),
    json_a_artifact_id TEXT NOT NULL REFERENCES artifacts(id),
    json_a_sha256 TEXT NOT NULL CHECK (length(json_a_sha256) = 64 AND json_a_sha256 NOT GLOB '*[^0-9a-f]*'),
    json_a_byte_length INTEGER NOT NULL CHECK (json_a_byte_length > 0),
    json_b_artifact_id TEXT NOT NULL REFERENCES artifacts(id),
    json_b_sha256 TEXT NOT NULL CHECK (length(json_b_sha256) = 64 AND json_b_sha256 NOT GLOB '*[^0-9a-f]*'),
    json_b_byte_length INTEGER NOT NULL CHECK (json_b_byte_length > 0),
    state TEXT NOT NULL CHECK (state IN ('CURRENT', 'WITHDRAWN', 'INVALIDATED', 'FORMALIZED')),
    confirmed_at TEXT NOT NULL,
    withdrawn_at TEXT,
    invalidated_at TEXT,
    formalized_at TEXT,
    CHECK (
        (state = 'CURRENT' AND withdrawn_at IS NULL AND invalidated_at IS NULL AND formalized_at IS NULL)
        OR (state = 'WITHDRAWN' AND withdrawn_at IS NOT NULL AND invalidated_at IS NULL AND formalized_at IS NULL)
        OR (state = 'INVALIDATED' AND withdrawn_at IS NULL AND invalidated_at IS NOT NULL AND formalized_at IS NULL)
        OR (state = 'FORMALIZED' AND withdrawn_at IS NULL AND invalidated_at IS NULL AND formalized_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX idx_candidate_confirmations_current_role
ON candidate_confirmations(round_id, actor_role)
WHERE state = 'CURRENT';

CREATE UNIQUE INDEX idx_candidate_confirmations_current_actor
ON candidate_confirmations(round_id, actor_id)
WHERE state = 'CURRENT';

CREATE INDEX idx_candidate_confirmations_round_history
ON candidate_confirmations(round_id, confirmed_at, id);

CREATE UNIQUE INDEX idx_candidate_confirmations_formal_role
ON candidate_confirmations(round_id, actor_role)
WHERE state = 'FORMALIZED';

CREATE TABLE separate_proposal_requests (
    id TEXT PRIMARY KEY,
    parent_case_id TEXT NOT NULL REFERENCES cases(id),
    child_case_id TEXT NOT NULL UNIQUE REFERENCES cases(id),
    created_by_id TEXT NOT NULL REFERENCES users(id),
    idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) BETWEEN 16 AND 200),
    created_at TEXT NOT NULL,
    UNIQUE(parent_case_id, idempotency_key),
    CHECK (parent_case_id <> child_case_id)
);

CREATE TRIGGER cases_candidate_pointer_insert_guard
BEFORE INSERT ON cases
BEGIN
    SELECT CASE
        WHEN NEW.state = 'CANDIDATE_REVIEW' AND (
            NEW.active_candidate_id IS NULL OR NEW.formal_candidate_id IS NOT NULL
            OR NOT EXISTS (
                SELECT 1 FROM candidate_versions AS candidate
                WHERE candidate.id = NEW.active_candidate_id
                  AND candidate.case_id = NEW.id
                  AND candidate.state = 'CURRENT'
            )
        ) THEN RAISE(ABORT, 'candidate_pointer_invalid')
        WHEN NEW.state = 'FORMAL' AND (
            NEW.active_candidate_id IS NOT NULL OR NEW.formal_candidate_id IS NULL
            OR NOT EXISTS (
                SELECT 1 FROM candidate_versions AS candidate
                WHERE candidate.id = NEW.formal_candidate_id
                  AND candidate.case_id = NEW.id
                  AND candidate.state = 'FORMAL'
            )
        ) THEN RAISE(ABORT, 'formal_pointer_invalid')
    END;
END;

CREATE TRIGGER cases_candidate_pointer_update_guard
BEFORE UPDATE OF state, active_candidate_id, formal_candidate_id ON cases
BEGIN
    SELECT CASE
        WHEN NEW.state = 'CANDIDATE_REVIEW' AND (
            NEW.active_candidate_id IS NULL OR NEW.formal_candidate_id IS NOT NULL
            OR NOT EXISTS (
                SELECT 1 FROM candidate_versions AS candidate
                WHERE candidate.id = NEW.active_candidate_id
                  AND candidate.case_id = NEW.id
                  AND candidate.state = 'CURRENT'
            )
        ) THEN RAISE(ABORT, 'candidate_pointer_invalid')
        WHEN NEW.state = 'FORMAL' AND (
            NEW.active_candidate_id IS NOT NULL OR NEW.formal_candidate_id IS NULL
            OR NOT EXISTS (
                SELECT 1 FROM candidate_versions AS candidate
                WHERE candidate.id = NEW.formal_candidate_id
                  AND candidate.case_id = NEW.id
                  AND candidate.state = 'FORMAL'
            )
        ) THEN RAISE(ABORT, 'formal_pointer_invalid')
    END;
END;

CREATE TRIGGER candidate_versions_pointed_delete_guard
BEFORE DELETE ON candidate_versions
WHEN EXISTS (
    SELECT 1 FROM cases
    WHERE active_candidate_id = OLD.id OR formal_candidate_id = OLD.id
)
BEGIN
    SELECT RAISE(ABORT, 'candidate_pointer_in_use');
END;

CREATE TRIGGER candidate_versions_pointed_update_guard
BEFORE UPDATE OF id, case_id, state ON candidate_versions
WHEN EXISTS (
    SELECT 1 FROM cases
    WHERE (active_candidate_id = OLD.id AND (NEW.id <> OLD.id OR NEW.case_id <> OLD.case_id OR NEW.state <> 'CURRENT'))
       OR (formal_candidate_id = OLD.id AND (NEW.id <> OLD.id OR NEW.case_id <> OLD.case_id OR NEW.state <> 'FORMAL'))
)
BEGIN
    SELECT RAISE(ABORT, 'candidate_pointer_in_use');
END;

CREATE TRIGGER cases_fixed_sources_guard
BEFORE UPDATE OF current_json_a_artifact_id, current_json_b_artifact_id, content_revision ON cases
WHEN (OLD.active_candidate_id IS NOT NULL OR OLD.formal_candidate_id IS NOT NULL)
  AND (
      NEW.current_json_a_artifact_id IS NOT OLD.current_json_a_artifact_id
      OR NEW.current_json_b_artifact_id IS NOT OLD.current_json_b_artifact_id
      OR NEW.content_revision <> OLD.content_revision
  )
BEGIN
    SELECT RAISE(ABORT, 'fixed_candidate_sources_locked');
END;

CREATE TRIGGER cases_formal_identity_guard
BEFORE UPDATE OF owner_id, contractor_id, state, content_revision,
    current_json_a_artifact_id, current_json_b_artifact_id,
    active_candidate_id, formal_candidate_id ON cases
WHEN OLD.state = 'FORMAL'
BEGIN
    SELECT RAISE(ABORT, 'formal_case_immutable');
END;

CREATE TRIGGER candidate_versions_formal_immutable
BEFORE UPDATE ON candidate_versions
WHEN OLD.state = 'FORMAL'
BEGIN
    SELECT RAISE(ABORT, 'formal_candidate_immutable');
END;

CREATE TRIGGER artifacts_formal_immutable_update
BEFORE UPDATE ON artifacts
WHEN OLD.lifecycle = 'formal'
BEGIN
    SELECT RAISE(ABORT, 'formal_artifact_immutable');
END;

CREATE TRIGGER artifacts_formal_immutable_delete
BEFORE DELETE ON artifacts
WHEN OLD.lifecycle = 'formal'
BEGIN
    SELECT RAISE(ABORT, 'formal_artifact_immutable');
END;

CREATE TRIGGER candidate_acknowledgements_append_only_update
BEFORE UPDATE ON candidate_acknowledgements
BEGIN
    SELECT RAISE(ABORT, 'candidate_acknowledgements_append_only');
END;

CREATE TRIGGER candidate_acknowledgements_append_only_delete
BEFORE DELETE ON candidate_acknowledgements
BEGIN
    SELECT RAISE(ABORT, 'candidate_acknowledgements_append_only');
END;

CREATE TRIGGER candidate_confirmations_no_delete
BEFORE DELETE ON candidate_confirmations
BEGIN
    SELECT RAISE(ABORT, 'candidate_confirmations_no_delete');
END;

CREATE TRIGGER candidate_confirmations_state_only_update
BEFORE UPDATE ON candidate_confirmations
WHEN NOT (
    OLD.state = 'CURRENT'
    AND NEW.id = OLD.id
    AND NEW.round_id = OLD.round_id
    AND NEW.case_id = OLD.case_id
    AND NEW.candidate_id = OLD.candidate_id
    AND NEW.actor_id = OLD.actor_id
    AND NEW.actor_role = OLD.actor_role
    AND NEW.manifest_artifact_id = OLD.manifest_artifact_id
    AND NEW.manifest_sha256 = OLD.manifest_sha256
    AND NEW.manifest_byte_length = OLD.manifest_byte_length
    AND NEW.pdf_artifact_id = OLD.pdf_artifact_id
    AND NEW.pdf_sha256 = OLD.pdf_sha256
    AND NEW.pdf_byte_length = OLD.pdf_byte_length
    AND NEW.svg_artifact_id = OLD.svg_artifact_id
    AND NEW.svg_sha256 = OLD.svg_sha256
    AND NEW.svg_byte_length = OLD.svg_byte_length
    AND NEW.json_a_artifact_id = OLD.json_a_artifact_id
    AND NEW.json_a_sha256 = OLD.json_a_sha256
    AND NEW.json_a_byte_length = OLD.json_a_byte_length
    AND NEW.json_b_artifact_id = OLD.json_b_artifact_id
    AND NEW.json_b_sha256 = OLD.json_b_sha256
    AND NEW.json_b_byte_length = OLD.json_b_byte_length
    AND NEW.confirmed_at = OLD.confirmed_at
    AND (
        (NEW.state = 'WITHDRAWN' AND NEW.withdrawn_at IS NOT NULL AND NEW.invalidated_at IS NULL AND NEW.formalized_at IS NULL)
        OR (NEW.state = 'INVALIDATED' AND NEW.withdrawn_at IS NULL AND NEW.invalidated_at IS NOT NULL AND NEW.formalized_at IS NULL)
        OR (NEW.state = 'FORMALIZED' AND NEW.withdrawn_at IS NULL AND NEW.invalidated_at IS NULL AND NEW.formalized_at IS NOT NULL)
    )
)
BEGIN
    SELECT RAISE(ABORT, 'candidate_confirmation_transition_invalid');
END;

CREATE TRIGGER candidate_review_rounds_transition_guard
BEFORE UPDATE ON candidate_review_rounds
WHEN NOT (
    OLD.state = 'OPEN'
    AND NEW.id = OLD.id
    AND NEW.case_id = OLD.case_id
    AND NEW.candidate_id = OLD.candidate_id
    AND NEW.revision = OLD.revision
    AND NEW.manifest_sha256 = OLD.manifest_sha256
    AND NEW.pdf_sha256 = OLD.pdf_sha256
    AND NEW.svg_sha256 = OLD.svg_sha256
    AND NEW.json_a_sha256 = OLD.json_a_sha256
    AND NEW.json_b_sha256 = OLD.json_b_sha256
    AND NEW.created_at = OLD.created_at
    AND (
        (NEW.state = 'INVALIDATED' AND NEW.invalidated_at IS NOT NULL AND NEW.invalidated_by_id IS NOT NULL AND NEW.revision_reason IS NOT NULL AND NEW.formalized_at IS NULL)
        OR (NEW.state = 'FORMALIZED' AND NEW.invalidated_at IS NULL AND NEW.invalidated_by_id IS NULL AND NEW.revision_reason IS NULL AND NEW.formalized_at IS NOT NULL)
    )
)
BEGIN
    SELECT RAISE(ABORT, 'candidate_round_transition_invalid');
END;

CREATE TRIGGER candidate_review_rounds_no_delete
BEFORE DELETE ON candidate_review_rounds
BEGIN
    SELECT RAISE(ABORT, 'candidate_review_rounds_no_delete');
END;

CREATE TRIGGER separate_proposal_requests_append_only_update
BEFORE UPDATE ON separate_proposal_requests
BEGIN
    SELECT RAISE(ABORT, 'separate_proposal_requests_append_only');
END;

CREATE TRIGGER separate_proposal_requests_append_only_delete
BEFORE DELETE ON separate_proposal_requests
BEGIN
    SELECT RAISE(ABORT, 'separate_proposal_requests_append_only');
END;

PRAGMA defer_foreign_keys = OFF;
