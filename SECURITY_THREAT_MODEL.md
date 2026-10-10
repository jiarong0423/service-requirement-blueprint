# Security Threat Model

Review date: 2026-10-11  
System boundary: local Flask PoC bound to loopback, with SQLite and controlled local artifact storage

## Assets

- Account credentials and active sessions.
- Private case membership and invitation state.
- JSON A, JSON B, candidate, formal, delivery, inspection, dispute, and evidence-export records.
- Exact stored bytes, SHA-256 digests, version links, confirmations, and server timestamps.
- Local signing key, SQLite database, and generated artifact directory.

## Trust Boundary

- The browser is untrusted for identity, role, time, case state, totals, confirmation state, and uploaded authority fields.
- Flask routes and services enforce authentication, CSRF, active-account status, case-party binding, version state, and artifact integrity.
- SQLite transactions are the authority for roles, states, version relationships, and bilateral confirmation.
- Controlled storage is trusted only after regular-file, path containment, size, and digest verification.
- The local operating-system owner is outside the PoC protection boundary and can read local files directly.
- Administrators are isolated at the application layer and gain no case access from the administrator role.

## Data Flow

1. A client signs in, creates a private case, completes the requirement guide, and fixes JSON A bytes.
2. A single-use invitation binds one contractor, who verifies the registered JSON A and submits JSON B for review.
3. The server fixes one candidate evidence set. Both parties acknowledge and confirm the same stored bytes.
4. Formalization changes database state without regenerating the candidate files.
5. Delivery, inspection, remediation, reinspection, acceptance, dispute, and evidence export append immutable records.
6. Every protected read rechecks current account, case membership, version state, path containment, size, and digest.

## Threats and Controls

| Threat | Control |
| --- | --- |
| Session fixation or stale authorization | Session cleared at login and logout; session version checked against SQLite; account disable revokes access. |
| Cross-site request forgery | Every state-changing route uses POST and a secure random CSRF nonce with constant-time comparison. |
| IDOR or administrator bypass | Every case page, API, download, and export rechecks explicit client or contractor binding. |
| Uploaded identity or workflow-state forgery | Server ignores authority fields and owns roles, status, confirmation, and timestamps. |
| JSON ambiguity | Strict UTF-8 parsing rejects duplicate keys, unknown fields, invalid schema versions, and non-integer money. |
| Candidate or formal-file substitution | Immutable UUID paths, no-overwrite creation, stored length and SHA-256, and revalidation on every read. |
| Confirmation race | Immediate SQLite transactions bind both role confirmations to one current candidate and invalidate stale rounds. |
| Silent or deadline-based consent | Acceptance requires an explicit current client action; deadlines only append events. |
| Evidence-export traversal or private-draft leakage | Fixed allowlist, safe archive names, source fingerprint, inner length and digest checks, and private-draft exclusion tests. |
| Sensitive error or log disclosure | Generic external errors and sanitized structured logging; authorization denials do not enumerate cases. |
| Network or cloud leakage | Core runtime and tests operate offline; the application binds to loopback and has no provider integration. |

## Residual Risks

- A person with direct access to the local operating system, SQLite file, signing key, or artifact directory can bypass application-layer controls.
- The PoC does not provide end-to-end encryption, third-party identity proof, trusted timestamping, legal advice, payment custody, or hosted-service hardening.
- Package installation requires a network connection and currently relies on exact versions rather than a hash-locked dependency file.
- Public deployment is outside the approved scope and requires a separate deployment threat model and gate.
