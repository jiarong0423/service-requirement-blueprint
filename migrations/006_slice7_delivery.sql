CREATE TABLE delivery_workflows (
    case_id TEXT PRIMARY KEY REFERENCES cases(id),
    formal_candidate_id TEXT NOT NULL REFERENCES candidate_versions(id),
    state TEXT NOT NULL CHECK (state IN (
        'FORMAL', 'DELIVERED', 'UNDER_ACCEPTANCE', 'REMEDIATION',
        'REINSPECTION', 'ACCEPTED', 'DISPUTED'
    )),
    current_delivery_id TEXT,
    current_inspection_id TEXT,
    active_dispute_id TEXT,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (state = 'FORMAL' AND current_delivery_id IS NULL AND current_inspection_id IS NULL AND active_dispute_id IS NULL)
        OR (state IN ('DELIVERED', 'UNDER_ACCEPTANCE', 'REMEDIATION', 'REINSPECTION')
            AND current_delivery_id IS NOT NULL AND active_dispute_id IS NULL)
        OR (state = 'ACCEPTED' AND current_delivery_id IS NOT NULL AND current_inspection_id IS NOT NULL AND active_dispute_id IS NULL)
        OR (state = 'DISPUTED' AND current_delivery_id IS NOT NULL AND active_dispute_id IS NOT NULL)
    )
);

CREATE TABLE delivery_versions (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    formal_candidate_id TEXT NOT NULL REFERENCES candidate_versions(id),
    version_no INTEGER NOT NULL CHECK (version_no BETWEEN 1 AND 20),
    kind TEXT NOT NULL CHECK (kind IN ('INITIAL', 'REMEDIATION')),
    predecessor_delivery_id TEXT REFERENCES delivery_versions(id),
    remediated_inspection_id TEXT,
    submitted_by_id TEXT NOT NULL REFERENCES users(id),
    summary TEXT NOT NULL CHECK (length(summary) BETWEEN 1 AND 4000),
    relative_path TEXT NOT NULL UNIQUE CHECK (length(relative_path) BETWEEN 1 AND 1000),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
    byte_length INTEGER NOT NULL CHECK (byte_length > 0 AND byte_length <= 1048576),
    item_count INTEGER NOT NULL CHECK (item_count BETWEEN 1 AND 200),
    criteria_sha256 TEXT NOT NULL CHECK (length(criteria_sha256) = 64 AND criteria_sha256 NOT GLOB '*[^0-9a-f]*'),
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64 AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
    submitted_at TEXT NOT NULL,
    review_due_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) BETWEEN 16 AND 200),
    UNIQUE(case_id, version_no),
    UNIQUE(case_id, idempotency_key),
    CHECK (
        (kind = 'INITIAL' AND version_no = 1 AND predecessor_delivery_id IS NULL AND remediated_inspection_id IS NULL)
        OR (kind = 'REMEDIATION' AND version_no > 1 AND predecessor_delivery_id IS NOT NULL AND remediated_inspection_id IS NOT NULL)
    )
);

CREATE TABLE delivery_items (
    id TEXT PRIMARY KEY,
    delivery_id TEXT NOT NULL REFERENCES delivery_versions(id),
    criterion_id TEXT NOT NULL,
    description TEXT NOT NULL CHECK (length(description) BETWEEN 1 AND 4000),
    verification_method TEXT NOT NULL CHECK (length(verification_method) BETWEEN 1 AND 4000),
    delivery_note TEXT NOT NULL CHECK (length(delivery_note) BETWEEN 1 AND 4000),
    evidence_reference TEXT NOT NULL CHECK (length(evidence_reference) BETWEEN 1 AND 1000),
    UNIQUE(delivery_id, criterion_id)
);

CREATE TABLE delivery_test_results (
    id TEXT PRIMARY KEY,
    delivery_id TEXT NOT NULL REFERENCES delivery_versions(id),
    criterion_id TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('PASS', 'FAIL', 'NOT_RUN')),
    observation TEXT NOT NULL CHECK (length(observation) BETWEEN 1 AND 4000),
    UNIQUE(delivery_id, criterion_id)
);

CREATE TABLE inspection_rounds (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    delivery_id TEXT NOT NULL REFERENCES delivery_versions(id),
    round_no INTEGER NOT NULL CHECK (round_no >= 1),
    kind TEXT NOT NULL CHECK (kind IN ('INITIAL', 'REINSPECTION')),
    decision TEXT NOT NULL CHECK (decision IN ('ACCEPTED', 'RETURNED')),
    explicit_acceptance INTEGER NOT NULL CHECK (explicit_acceptance IN (0, 1)),
    inspected_by_id TEXT NOT NULL REFERENCES users(id),
    inspected_at TEXT NOT NULL,
    result_count INTEGER NOT NULL CHECK (result_count BETWEEN 1 AND 200),
    criteria_sha256 TEXT NOT NULL CHECK (length(criteria_sha256) = 64 AND criteria_sha256 NOT GLOB '*[^0-9a-f]*'),
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64 AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
    idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) BETWEEN 16 AND 200),
    UNIQUE(case_id, round_no),
    UNIQUE(case_id, idempotency_key),
    CHECK ((decision = 'ACCEPTED' AND explicit_acceptance = 1) OR (decision = 'RETURNED' AND explicit_acceptance = 0))
);

CREATE TABLE inspection_item_results (
    id TEXT PRIMARY KEY,
    inspection_id TEXT NOT NULL REFERENCES inspection_rounds(id),
    delivery_item_id TEXT NOT NULL REFERENCES delivery_items(id),
    criterion_id TEXT NOT NULL,
    result TEXT NOT NULL CHECK (result IN ('PASS', 'RETURNED')),
    reason TEXT,
    CHECK (
        (result = 'PASS' AND reason IS NULL)
        OR (result = 'RETURNED' AND length(reason) BETWEEN 1 AND 4000)
    ),
    UNIQUE(inspection_id, criterion_id)
);

CREATE TABLE case_timeline_events (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    event_code TEXT NOT NULL CHECK (event_code IN (
        'DELIVERY_SUBMITTED', 'REVIEW_DEADLINE_SET', 'INSPECTION_STARTED',
        'INSPECTION_RETURNED', 'DELIVERY_ACCEPTED', 'REMEDIATION_SUBMITTED',
        'DISPUTE_OPENED', 'DISPUTE_POSITION_ADDED', 'EVIDENCE_EXPORTED'
    )),
    entity_type TEXT NOT NULL CHECK (entity_type IN ('DELIVERY', 'INSPECTION', 'DISPUTE', 'EXPORT')),
    entity_id TEXT NOT NULL,
    actor_id TEXT REFERENCES users(id),
    actor_role TEXT CHECK (actor_role IS NULL OR actor_role IN ('client', 'contractor')),
    occurred_at TEXT NOT NULL,
    effective_at TEXT,
    UNIQUE(case_id, event_code, entity_id, effective_at)
);

CREATE TABLE acceptance_events (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL UNIQUE REFERENCES cases(id),
    formal_candidate_id TEXT NOT NULL REFERENCES candidate_versions(id),
    delivery_id TEXT NOT NULL REFERENCES delivery_versions(id),
    inspection_id TEXT NOT NULL UNIQUE REFERENCES inspection_rounds(id),
    accepted_by_id TEXT NOT NULL REFERENCES users(id),
    evidence_sha256 TEXT NOT NULL CHECK (length(evidence_sha256) = 64 AND evidence_sha256 NOT GLOB '*[^0-9a-f]*'),
    accepted_at TEXT NOT NULL
);

CREATE TABLE disputes (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL UNIQUE REFERENCES cases(id),
    formal_candidate_id TEXT NOT NULL REFERENCES candidate_versions(id),
    delivery_id TEXT NOT NULL REFERENCES delivery_versions(id),
    inspection_id TEXT REFERENCES inspection_rounds(id),
    prior_state TEXT NOT NULL CHECK (prior_state IN ('DELIVERED', 'UNDER_ACCEPTANCE', 'REMEDIATION', 'REINSPECTION')),
    opened_by_id TEXT NOT NULL REFERENCES users(id),
    opened_by_role TEXT NOT NULL CHECK (opened_by_role IN ('client', 'contractor')),
    reason TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 4000),
    opened_at TEXT NOT NULL
);

CREATE TABLE dispute_positions (
    id TEXT PRIMARY KEY,
    dispute_id TEXT NOT NULL REFERENCES disputes(id),
    actor_id TEXT NOT NULL REFERENCES users(id),
    actor_role TEXT NOT NULL CHECK (actor_role IN ('client', 'contractor')),
    position_text TEXT NOT NULL CHECK (length(position_text) BETWEEN 1 AND 4000),
    submitted_at TEXT NOT NULL,
    UNIQUE(dispute_id, actor_role)
);

CREATE TABLE evidence_exports (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    version_no INTEGER NOT NULL CHECK (version_no >= 1),
    prepared_by_id TEXT NOT NULL REFERENCES users(id),
    source_fingerprint TEXT NOT NULL CHECK (length(source_fingerprint) = 64 AND source_fingerprint NOT GLOB '*[^0-9a-f]*'),
    relative_path TEXT NOT NULL UNIQUE CHECK (length(relative_path) BETWEEN 1 AND 1000),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
    byte_length INTEGER NOT NULL CHECK (byte_length > 0 AND byte_length <= 67108864),
    schema_version TEXT NOT NULL CHECK (schema_version = 'evidence-export/1.0'),
    prepared_at TEXT NOT NULL,
    UNIQUE(case_id, version_no),
    UNIQUE(case_id, source_fingerprint)
);

CREATE INDEX idx_delivery_versions_case ON delivery_versions(case_id, version_no);
CREATE INDEX idx_inspection_rounds_case ON inspection_rounds(case_id, round_no);
CREATE INDEX idx_case_timeline_events_case ON case_timeline_events(case_id, occurred_at, id);
CREATE INDEX idx_evidence_exports_case ON evidence_exports(case_id, version_no);

CREATE TRIGGER delivery_workflows_insert_guard
BEFORE INSERT ON delivery_workflows
BEGIN
    SELECT CASE WHEN NEW.state <> 'DELIVERED' OR NEW.current_delivery_id IS NULL
        OR NEW.current_inspection_id IS NOT NULL OR NEW.active_dispute_id IS NOT NULL
        THEN RAISE(ABORT, 'delivery_workflow_initial_state_invalid') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM cases AS c JOIN candidate_versions AS v ON v.id = c.formal_candidate_id
        WHERE c.id = NEW.case_id AND c.state = 'FORMAL' AND c.active_candidate_id IS NULL
          AND c.formal_candidate_id = NEW.formal_candidate_id AND v.case_id = c.id AND v.state = 'FORMAL'
    ) THEN RAISE(ABORT, 'delivery_formal_binding_invalid') END;
END;

CREATE TRIGGER delivery_workflows_update_guard
BEFORE UPDATE ON delivery_workflows
BEGIN
    SELECT CASE WHEN NEW.case_id <> OLD.case_id OR NEW.formal_candidate_id <> OLD.formal_candidate_id
        OR NEW.created_at <> OLD.created_at OR NEW.revision <> OLD.revision + 1
        THEN RAISE(ABORT, 'delivery_workflow_identity_invalid') END;
    SELECT CASE WHEN NOT (
        (OLD.state = 'FORMAL' AND NEW.state = 'DELIVERED')
        OR (OLD.state = 'DELIVERED' AND NEW.state IN ('UNDER_ACCEPTANCE', 'DISPUTED'))
        OR (OLD.state = 'UNDER_ACCEPTANCE' AND NEW.state IN ('ACCEPTED', 'REMEDIATION', 'DISPUTED'))
        OR (OLD.state = 'REMEDIATION' AND NEW.state IN ('REINSPECTION', 'DISPUTED'))
        OR (OLD.state = 'REINSPECTION' AND NEW.state IN ('ACCEPTED', 'REMEDIATION', 'DISPUTED'))
    ) THEN RAISE(ABORT, 'delivery_transition_invalid') END;
    SELECT CASE WHEN NEW.state = 'ACCEPTED' AND NOT EXISTS (
        SELECT 1 FROM acceptance_events AS a
        JOIN inspection_rounds AS i ON i.id = a.inspection_id
        WHERE a.case_id = NEW.case_id AND a.formal_candidate_id = NEW.formal_candidate_id
          AND a.delivery_id = NEW.current_delivery_id AND a.inspection_id = NEW.current_inspection_id
          AND i.case_id = NEW.case_id AND i.delivery_id = NEW.current_delivery_id
          AND i.decision = 'ACCEPTED' AND i.explicit_acceptance = 1
    ) THEN RAISE(ABORT, 'explicit_acceptance_missing') END;
END;

CREATE TRIGGER delivery_workflows_no_delete BEFORE DELETE ON delivery_workflows
BEGIN SELECT RAISE(ABORT, 'delivery_workflow_no_delete'); END;

CREATE TRIGGER delivery_workflows_pointer_guard
AFTER INSERT ON delivery_workflows WHEN NEW.current_delivery_id IS NOT NULL OR NEW.current_inspection_id IS NOT NULL OR NEW.active_dispute_id IS NOT NULL
BEGIN
    SELECT CASE WHEN NEW.current_delivery_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM delivery_versions WHERE id = NEW.current_delivery_id AND case_id = NEW.case_id
    ) THEN RAISE(ABORT, 'delivery_pointer_invalid') END;
    SELECT CASE WHEN NEW.current_inspection_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM inspection_rounds WHERE id = NEW.current_inspection_id AND case_id = NEW.case_id
    ) THEN RAISE(ABORT, 'inspection_pointer_invalid') END;
    SELECT CASE WHEN NEW.active_dispute_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM disputes WHERE id = NEW.active_dispute_id AND case_id = NEW.case_id
    ) THEN RAISE(ABORT, 'dispute_pointer_invalid') END;
END;

CREATE TRIGGER delivery_workflows_pointer_update_guard
AFTER UPDATE ON delivery_workflows
BEGIN
    SELECT CASE WHEN NEW.current_delivery_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM delivery_versions WHERE id = NEW.current_delivery_id AND case_id = NEW.case_id
    ) THEN RAISE(ABORT, 'delivery_pointer_invalid') END;
    SELECT CASE WHEN NEW.current_inspection_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM inspection_rounds WHERE id = NEW.current_inspection_id AND case_id = NEW.case_id
    ) THEN RAISE(ABORT, 'inspection_pointer_invalid') END;
    SELECT CASE WHEN NEW.active_dispute_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM disputes WHERE id = NEW.active_dispute_id AND case_id = NEW.case_id
    ) THEN RAISE(ABORT, 'dispute_pointer_invalid') END;
END;

CREATE TRIGGER delivery_versions_binding_guard
BEFORE INSERT ON delivery_versions
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM cases WHERE id = NEW.case_id AND state = 'FORMAL'
          AND formal_candidate_id = NEW.formal_candidate_id AND contractor_id = NEW.submitted_by_id
    ) THEN RAISE(ABORT, 'delivery_binding_invalid') END;
    SELECT CASE WHEN NEW.relative_path <> 'cases/' || NEW.case_id || '/deliveries/' || NEW.id || '/delivery.json'
        THEN RAISE(ABORT, 'delivery_path_invalid') END;
    SELECT CASE WHEN NEW.kind = 'REMEDIATION' AND NOT EXISTS (
        SELECT 1 FROM delivery_workflows AS w
        JOIN inspection_rounds AS i ON i.id = NEW.remediated_inspection_id
        WHERE w.case_id = NEW.case_id AND w.state = 'REMEDIATION'
          AND w.current_delivery_id = NEW.predecessor_delivery_id
          AND w.current_inspection_id = NEW.remediated_inspection_id
          AND i.case_id = NEW.case_id AND i.delivery_id = NEW.predecessor_delivery_id
          AND i.decision = 'RETURNED'
    ) THEN RAISE(ABORT, 'remediation_binding_invalid') END;
END;

CREATE TRIGGER delivery_items_insert_guard
BEFORE INSERT ON delivery_items
BEGIN
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM case_timeline_events AS e JOIN delivery_versions AS d ON d.case_id = e.case_id
        WHERE d.id = NEW.delivery_id AND e.entity_type = 'DELIVERY' AND e.entity_id = NEW.delivery_id
    ) THEN RAISE(ABORT, 'delivery_children_closed') END;
    SELECT CASE WHEN (SELECT COUNT(*) FROM delivery_items WHERE delivery_id = NEW.delivery_id) >=
        (SELECT item_count FROM delivery_versions WHERE id = NEW.delivery_id)
        THEN RAISE(ABORT, 'delivery_item_count_exceeded') END;
END;

CREATE TRIGGER delivery_tests_insert_guard
BEFORE INSERT ON delivery_test_results
BEGIN
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM case_timeline_events AS e JOIN delivery_versions AS d ON d.case_id = e.case_id
        WHERE d.id = NEW.delivery_id AND e.entity_type = 'DELIVERY' AND e.entity_id = NEW.delivery_id
    ) THEN RAISE(ABORT, 'delivery_children_closed') END;
    SELECT CASE WHEN (SELECT COUNT(*) FROM delivery_test_results WHERE delivery_id = NEW.delivery_id) >=
        (SELECT item_count FROM delivery_versions WHERE id = NEW.delivery_id)
        THEN RAISE(ABORT, 'delivery_test_count_exceeded') END;
END;

CREATE TRIGGER inspection_rounds_binding_guard
BEFORE INSERT ON inspection_rounds
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM cases AS c JOIN delivery_versions AS d ON d.id = NEW.delivery_id
        WHERE c.id = NEW.case_id AND c.owner_id = NEW.inspected_by_id AND d.case_id = c.id
    ) THEN RAISE(ABORT, 'inspection_binding_invalid') END;
END;

CREATE TRIGGER inspection_results_insert_guard
BEFORE INSERT ON inspection_item_results
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM inspection_rounds AS i
        JOIN delivery_items AS item ON item.id = NEW.delivery_item_id
        WHERE i.id = NEW.inspection_id AND i.delivery_id = item.delivery_id
          AND item.criterion_id = NEW.criterion_id
    ) THEN RAISE(ABORT, 'inspection_item_binding_invalid') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM inspection_rounds
        WHERE id = NEW.inspection_id AND decision = 'ACCEPTED' AND NEW.result <> 'PASS'
    ) THEN RAISE(ABORT, 'accepted_inspection_requires_pass') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM case_timeline_events AS e JOIN inspection_rounds AS i ON i.case_id = e.case_id
        WHERE i.id = NEW.inspection_id AND e.entity_type = 'INSPECTION' AND e.entity_id = NEW.inspection_id
    ) THEN RAISE(ABORT, 'inspection_children_closed') END;
    SELECT CASE WHEN (SELECT COUNT(*) FROM inspection_item_results WHERE inspection_id = NEW.inspection_id) >=
        (SELECT result_count FROM inspection_rounds WHERE id = NEW.inspection_id)
        THEN RAISE(ABORT, 'inspection_result_count_exceeded') END;
END;

CREATE TRIGGER acceptance_events_binding_guard
BEFORE INSERT ON acceptance_events
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM cases AS c
        JOIN inspection_rounds AS i ON i.id = NEW.inspection_id
        JOIN delivery_versions AS d ON d.id = NEW.delivery_id
        JOIN delivery_workflows AS w ON w.case_id = c.id
        WHERE c.id = NEW.case_id AND c.owner_id = NEW.accepted_by_id
          AND c.formal_candidate_id = NEW.formal_candidate_id
          AND i.case_id = c.id AND i.delivery_id = d.id AND d.case_id = c.id
          AND i.decision = 'ACCEPTED' AND i.explicit_acceptance = 1
          AND i.criteria_sha256 = d.criteria_sha256
          AND i.result_count = d.item_count
          AND i.result_count = (SELECT COUNT(*) FROM inspection_item_results WHERE inspection_id = i.id)
          AND NOT EXISTS (
              SELECT 1 FROM inspection_item_results AS result
              WHERE result.inspection_id = i.id AND result.result <> 'PASS'
          )
          AND w.state IN ('UNDER_ACCEPTANCE', 'REINSPECTION')
          AND w.current_delivery_id = NEW.delivery_id
          AND NEW.accepted_at = i.inspected_at
    ) THEN RAISE(ABORT, 'acceptance_binding_invalid') END;
END;

CREATE TRIGGER dispute_positions_binding_guard
BEFORE INSERT ON dispute_positions
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM disputes AS d JOIN cases AS c ON c.id = d.case_id
        WHERE d.id = NEW.dispute_id AND (
            (NEW.actor_role = 'client' AND c.owner_id = NEW.actor_id)
            OR (NEW.actor_role = 'contractor' AND c.contractor_id = NEW.actor_id)
        )
    ) THEN RAISE(ABORT, 'dispute_position_binding_invalid') END;
END;

CREATE TRIGGER delivery_versions_no_update BEFORE UPDATE ON delivery_versions
BEGIN SELECT RAISE(ABORT, 'delivery_versions_append_only'); END;
CREATE TRIGGER delivery_versions_no_delete BEFORE DELETE ON delivery_versions
BEGIN SELECT RAISE(ABORT, 'delivery_versions_append_only'); END;
CREATE TRIGGER delivery_items_no_update BEFORE UPDATE ON delivery_items
BEGIN SELECT RAISE(ABORT, 'delivery_items_append_only'); END;
CREATE TRIGGER delivery_items_no_delete BEFORE DELETE ON delivery_items
BEGIN SELECT RAISE(ABORT, 'delivery_items_append_only'); END;
CREATE TRIGGER delivery_tests_no_update BEFORE UPDATE ON delivery_test_results
BEGIN SELECT RAISE(ABORT, 'delivery_tests_append_only'); END;
CREATE TRIGGER delivery_tests_no_delete BEFORE DELETE ON delivery_test_results
BEGIN SELECT RAISE(ABORT, 'delivery_tests_append_only'); END;
CREATE TRIGGER inspections_no_update BEFORE UPDATE ON inspection_rounds
BEGIN SELECT RAISE(ABORT, 'inspections_append_only'); END;
CREATE TRIGGER inspections_no_delete BEFORE DELETE ON inspection_rounds
BEGIN SELECT RAISE(ABORT, 'inspections_append_only'); END;
CREATE TRIGGER inspection_results_no_update BEFORE UPDATE ON inspection_item_results
BEGIN SELECT RAISE(ABORT, 'inspection_results_append_only'); END;
CREATE TRIGGER inspection_results_no_delete BEFORE DELETE ON inspection_item_results
BEGIN SELECT RAISE(ABORT, 'inspection_results_append_only'); END;
CREATE TRIGGER timeline_no_update BEFORE UPDATE ON case_timeline_events
BEGIN SELECT RAISE(ABORT, 'timeline_append_only'); END;
CREATE TRIGGER timeline_no_delete BEFORE DELETE ON case_timeline_events
BEGIN SELECT RAISE(ABORT, 'timeline_append_only'); END;
CREATE TRIGGER disputes_no_update BEFORE UPDATE ON disputes
BEGIN SELECT RAISE(ABORT, 'disputes_append_only'); END;
CREATE TRIGGER disputes_no_delete BEFORE DELETE ON disputes
BEGIN SELECT RAISE(ABORT, 'disputes_append_only'); END;
CREATE TRIGGER dispute_positions_no_update BEFORE UPDATE ON dispute_positions
BEGIN SELECT RAISE(ABORT, 'dispute_positions_append_only'); END;
CREATE TRIGGER dispute_positions_no_delete BEFORE DELETE ON dispute_positions
BEGIN SELECT RAISE(ABORT, 'dispute_positions_append_only'); END;
CREATE TRIGGER evidence_exports_no_update BEFORE UPDATE ON evidence_exports
BEGIN SELECT RAISE(ABORT, 'evidence_exports_append_only'); END;
CREATE TRIGGER evidence_exports_no_delete BEFORE DELETE ON evidence_exports
BEGIN SELECT RAISE(ABORT, 'evidence_exports_append_only'); END;
CREATE TRIGGER acceptance_events_no_update BEFORE UPDATE ON acceptance_events
BEGIN SELECT RAISE(ABORT, 'acceptance_events_append_only'); END;
CREATE TRIGGER acceptance_events_no_delete BEFORE DELETE ON acceptance_events
BEGIN SELECT RAISE(ABORT, 'acceptance_events_append_only'); END;

CREATE TRIGGER evidence_exports_path_guard
BEFORE INSERT ON evidence_exports
WHEN NEW.relative_path <> 'cases/' || NEW.case_id || '/exports/' || NEW.id || '/evidence.zip'
BEGIN SELECT RAISE(ABORT, 'evidence_export_path_invalid'); END;
