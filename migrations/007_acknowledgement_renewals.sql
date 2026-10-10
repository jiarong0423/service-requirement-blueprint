CREATE TABLE candidate_acknowledgement_renewals (
    id TEXT PRIMARY KEY,
    acknowledgement_id TEXT NOT NULL REFERENCES candidate_acknowledgements(id),
    withdrawn_confirmation_id TEXT NOT NULL UNIQUE REFERENCES candidate_confirmations(id),
    renewal_kind TEXT NOT NULL CHECK (
        renewal_kind IN ('EXPLICIT', 'LEGACY_ACKNOWLEDGEMENT_REUSE')
    ),
    renewed_at TEXT NOT NULL CHECK (julianday(renewed_at) IS NOT NULL)
);

CREATE INDEX idx_candidate_acknowledgement_renewals_acknowledgement
ON candidate_acknowledgement_renewals(acknowledgement_id, renewed_at, id);

INSERT INTO candidate_acknowledgement_renewals (
    id, acknowledgement_id, withdrawn_confirmation_id, renewal_kind, renewed_at
)
SELECT
    lower(
        substr(hex(randomblob(16)), 1, 8) || '-' ||
        substr(hex(randomblob(16)), 9, 4) || '-' ||
        substr(hex(randomblob(16)), 13, 4) || '-' ||
        substr(hex(randomblob(16)), 17, 4) || '-' ||
        substr(hex(randomblob(16)), 21, 12)
    ),
    acknowledgement.id,
    withdrawn.id,
    'LEGACY_ACKNOWLEDGEMENT_REUSE',
    (
        SELECT later.confirmed_at
        FROM candidate_confirmations AS later
        WHERE later.round_id = withdrawn.round_id
          AND later.actor_id = withdrawn.actor_id
          AND later.actor_role = withdrawn.actor_role
          AND later.rowid > withdrawn.rowid
        ORDER BY later.rowid
        LIMIT 1
    )
FROM candidate_confirmations AS withdrawn
JOIN candidate_acknowledgements AS acknowledgement
  ON acknowledgement.round_id = withdrawn.round_id
 AND acknowledgement.actor_id = withdrawn.actor_id
 AND acknowledgement.actor_role = withdrawn.actor_role
WHERE withdrawn.state = 'WITHDRAWN'
  AND EXISTS (
      SELECT 1
      FROM candidate_confirmations AS later
      WHERE later.round_id = withdrawn.round_id
        AND later.actor_id = withdrawn.actor_id
        AND later.actor_role = withdrawn.actor_role
        AND later.rowid > withdrawn.rowid
  );

CREATE TRIGGER candidate_acknowledgement_renewals_insert_guard
BEFORE INSERT ON candidate_acknowledgement_renewals
WHEN NEW.renewal_kind <> 'EXPLICIT' OR NOT EXISTS (
    SELECT 1
    FROM candidate_acknowledgements AS acknowledgement
    JOIN candidate_confirmations AS confirmation
      ON confirmation.id = NEW.withdrawn_confirmation_id
    WHERE acknowledgement.id = NEW.acknowledgement_id
      AND confirmation.state = 'WITHDRAWN'
      AND confirmation.round_id = acknowledgement.round_id
      AND confirmation.case_id = acknowledgement.case_id
      AND confirmation.candidate_id = acknowledgement.candidate_id
      AND confirmation.actor_id = acknowledgement.actor_id
      AND confirmation.actor_role = acknowledgement.actor_role
)
BEGIN
    SELECT RAISE(ABORT, 'acknowledgement_renewal_binding_invalid');
END;

CREATE TRIGGER candidate_acknowledgement_renewals_no_update
BEFORE UPDATE ON candidate_acknowledgement_renewals
BEGIN
    SELECT RAISE(ABORT, 'acknowledgement_renewals_append_only');
END;

CREATE TRIGGER candidate_acknowledgement_renewals_no_delete
BEFORE DELETE ON candidate_acknowledgement_renewals
BEGIN
    SELECT RAISE(ABORT, 'acknowledgement_renewals_append_only');
END;
