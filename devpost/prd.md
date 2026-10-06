---
doc: prd
status: approved
language: en
---

# Service Requirement Blueprint — Product Requirements

Help small-business clients and independent website contractors turn vague private messages into one version that both parties can discuss, confirm, deliver against, and inspect.

## Core Journey

1. The client signs in and opens the requirement guide.
2. The client completes eight focused sections while viewing progress and a live blueprint.
3. The review screen identifies decided items, unresolved items, conflicts, omissions, and risks. When valid, the client generates and downloads JSON A.
4. The client creates an invitation-only private case and invites one approved contractor.
5. The contractor accepts the invitation and imports the exact JSON A registered for the case.
6. Both parties use JSON A as the read-only requirement baseline and resolve open issues in the case room.
7. The contractor creates a private JSON B draft, downloads and reimports it for validation, then submits a fixed version for the client's review. The client may object to fields but cannot edit the contractor's quote.
8. The contractor creates one candidate PDF and one candidate SVG from fixed JSON A and JSON B snapshots. Both parties review the same saved bytes.
9. Separate valid confirmations from the client and contractor formalize that candidate without rerendering it.
10. The contractor submits a fixed delivery and test record. The client accepts or returns specific items for remediation and reinspection.
11. Silence or expiry never becomes consent. Unresolved disputes preserve evidence for party-controlled export.
12. An out-of-scope request opens a separate proposal and cannot modify the original case.

## Screens and Behavior

### Start and Sign-In

- `/` redirects to `/en/`.
- The start page explains the guided requirement process and offers client and contractor entry points.
- The competition PoC exposes demonstration sign-in only. Membership applications and production account recovery remain Later.
- Invalid credentials use one generic message.

### Client Requirement Guide

The client completes:

1. Requirement Purpose
2. Users and Usage Context
3. Pages and Content
4. Functional Requirements
5. Data and Permissions
6. External Services and Existing Assets
7. Visual and Experience Preferences
8. Delivery, Maintenance, Budget, and Constraints

Each section supports structured choices, text, and an explicit undecided or contractor-proposal state. A feature that is not needed is marked excluded. Saving a section creates a new server revision; stale revisions are rejected.

### Requirement Review and JSON A

The review screen shows the project purpose, users, pages, primary flow, included and excluded features, data roles, third-party services, asset responsibilities, maintenance expectations, acceptance expectations, and unresolved items.

JSON A is generated only when all required sections are complete and there is no blocking contradiction. It is a discussion baseline, not a quote, contract, or bilateral confirmation. Every generated version is stored once, hashed with SHA-256, and never overwritten. The client downloads the stored bytes; the server does not reserialize the document for each request.

Budget data uses:

- `budget_currency`: a three-letter uppercase ISO 4217 code or null when no amount is supplied;
- `budget_min_amount`: a nonnegative integer or null; and
- `budget_max_amount`: a nonnegative integer or null.

The maximum cannot be less than the minimum. The platform records requirements only and does not process money.

### Invitation and Private Case

The client reimports JSON A, previews the case summary, and invites one approved contractor. The invitation is single-use, expires, and binds the designated account. Before acceptance the contractor sees only the invitation summary. After acceptance, the case has exactly one client and one contractor.

Only bound parties may read case data. Administrators manage accounts and service health but receive no case-content bypass. Guessing an identifier must not reveal whether a private case or artifact exists.

### Discussion and JSON B

The case room contains the immutable JSON A baseline, append-only discussion entries, unresolved questions, and the contractor's JSON B workspace. Original entries cannot be overwritten; a correction is a new record.

JSON B includes scope, exclusions, pages, functions, data, permissions, third-party services, milestones, effort, difficulty, one-time and recurring costs, party responsibilities, acceptance criteria, remediation, warranty or maintenance boundaries, expiry, and source identity.

The contractor's private draft is invisible to the client until the contractor downloads and reimports it successfully. Submission creates a new fixed review version. Client objections are attached to fields; only the contractor can create a revised JSON B version. Every source change invalidates earlier candidate confirmations.

### Candidate Document and Confirmation

One candidate build freezes JSON A, JSON B, the diagram, label-map version, font identity, and template version in a manifest. The builder saves one English-labeled PDF and one English-labeled SVG. The PDF and SVG render the same complete three-node, two-edge blueprint model, including full node and edge labels.

The fixed PDF uses lifecycle-neutral wording. Candidate, invalidated, and formal status appear only in the application verification page, so the exact reviewed bytes can become formal without contradicting text embedded in the file. Candidates produced with the superseded lifecycle-specific notice remain preserved but are invalidated and must be rebuilt before acknowledgement.

Before JSON A or a JSON B review version is fixed, the server checks every document character against the pinned embedded font set. Supported user text is preserved exactly and is never translated. Unsupported characters are rejected with a clear validation error, so a case cannot reach candidate review with text that the fixed PDF cannot render.

Each party must review and acknowledge the same candidate manifest, PDF digest, SVG digest, JSON A digest, and JSON B digest. One confirmation leaves the case pending. A party may withdraw before formalization. A requested revision invalidates the round and returns work to JSON B.

Two current confirmations from different bound roles formalize the existing candidate inside one database transaction. Formalization changes authoritative state only; it does not regenerate, copy, move, or replace the reviewed files.

### Delivery, Acceptance, and Dispute Evidence

Delivery records are append-only and bind the formal version. The client may accept or return specific items with reasons. Remediation creates a new delivery version and a new inspection result. Deadlines create events but never automatic consent.

During a dispute, the system freezes relevant versions and preserves the original agreement, delivery, tests, inspection results, remediation, notices, and both parties' positions. Either party may prepare an evidence export. The platform does not decide breach, legal defect, liability, damages, or dispute outcome.

## English Interface and Dormant Localization Seam

- English is the only enabled system language for the competition build.
- `/en/<path>` is the only registered product route family.
- `app/i18n/catalog.en.json` is the only active catalog and the canonical display-key registry.
- Templates use translation keys rather than scattered system prose.
- Missing, duplicate, invalid, empty, or raw-key catalog entries fail closed.
- `/zh-TW`, other locale prefixes, and unprefixed product paths return 404.
- No language switch appears in the UI.
- User-authored text is stored as Unicode without automatic translation. Text accepted by the fixed-document font check is preserved exactly in JSON, SVG, HTML, and PDF.
- Adding a future display language may add a catalog and route allowlist entry, but must not change user, case, permission, state, version, or artifact identities.

## Roles and Permissions

### Client

- Creates and revises JSON A drafts.
- Invites one contractor.
- Reviews JSON B and raises field-specific objections.
- Confirms a candidate as the client.
- Inspects delivery and records acceptance or remediation requests.

### Contractor

- Accepts or rejects an invitation.
- Imports and reads the registered JSON A.
- Creates and revises JSON B.
- Builds the candidate document.
- Confirms as the contractor.
- Submits delivery and remediation versions.

### Administrator

- Enables or disables demonstration accounts.
- Views a safe service-health projection.
- Cannot search, list, open, download, export, or infer case content by administrator role.
- Gains case access only through an explicit party binding, not through administrator status.

## States and Boundaries

Machine states use fixed English codes. Agreement states include `REQUIREMENT_DRAFT`, `INVITATION_PENDING`, `CASE_ACTIVE`, `JSON_B_PRIVATE_DRAFT`, `JSON_B_REVIEW`, `CANDIDATE_REVIEW`, and `FORMAL`. The formal agreement remains `FORMAL`; its separate delivery workflow uses `FORMAL`, `DELIVERED`, `UNDER_ACCEPTANCE`, `REMEDIATION`, `REINSPECTION`, `ACCEPTED`, and `DISPUTED`. This separation keeps the formal candidate pointer and bytes locked while delivery evidence advances.

Abnormal and terminal states include rejected, revoked, expired, superseded, invalidated, failed, and quarantined records. A record is never silently overwritten to simulate a transition.

## Security and Privacy Requirements

- Server-side SQLite data determines account status, roles, case bindings, revisions, and confirmation validity.
- Passwords use scrypt hashes; no default or plaintext password is stored.
- Signed Flask session cookies contain only user ID, session version, and CSRF nonce.
- Login and logout clear the previous session and rotate the nonce.
- Every state-changing request is POST with CSRF validation.
- Every protected read rechecks account and case authority.
- Strict JSON parsing rejects invalid UTF-8, a BOM, duplicate keys, unknown fields, invalid types, and oversize files.
- Artifact downloads reauthorize the user and verify SHA-256 before returning bytes.
- Logs, errors, and administrator pages exclude case text, JSON, document bytes, tokens, cookies, absolute paths, and private digests.
- Local PoC data is not end-to-end encrypted and remains readable to a person with direct operating-system access.

## Non-Goals for the Competition Build

- Country-specific legal, tax, identity, address, payment, or hosting behavior.
- Legal advice, guaranteed enforceability, digital-certificate signatures, or trusted third-party timestamps.
- Quote fairness, contractor qualification, work-quality guarantees, or dispute adjudication.
- Payment collection, escrow, refunds, forced deductions, checkout, or merchant onboarding.
- Public bidding, recommendations, rankings, reviews, or a large marketplace.
- Security certification of delivered websites.
- Automatic translation of party content.
- A promise of permanent storage or email delivery.

## Implemented Slice Acceptance

### Slice 2

The current learner checkpoint is ready when:

- only the English route family is registered;
- only the English catalog ships;
- unsupported locale routes return 404;
- account initialization, password hashing, session rotation, CSRF, account disablement, administrator isolation, and owner authorization pass;
- the eight-section client flow creates a private draft and one immutable JSON A;
- JSON A uses schema `json-a/1.1`, currency-neutral budget fields, exact saved bytes, and SHA-256 verification;
- supported Unicode user text survives database, HTML, SVG, JSON, and PDF round trips unchanged, while unsupported document glyphs fail before JSON A is fixed; and
- JSON B, candidate confirmation, delivery, payments, remote push, and deployment remain outside the Slice 2 boundary.

### Slice 3

The invitation and private-case increment is ready when:

- the client reimports the exact current JSON A before creating an invitation for one approved contractor;
- the raw single-use invitation code is displayed once, never stored, and its response is not cacheable;
- invitation expiry, rejection, revocation, replay, wrong-account use, and a case/invitation URL mismatch fail closed without changing another case;
- acceptance binds only the designated contractor and still withholds case content until that contractor imports the exact registered JSON A;
- the verified import activates the private case and both parties can navigate to the same room, download the same rehashed JSON A bytes, and view the same blueprint;
- administrators, third members, unauthenticated users, guessed identifiers, and uploads that assert identity or authority cannot access or alter the case;
- migration from a populated Slice 2 database uses a locked, validated backup snapshot and rolls back the canonical database on failed post-migration checks; and
- discussions, JSON B, candidate confirmation, delivery, payments, remote push, and deployment remain absent.

### Slice 4

The discussion and proposal increment is ready when:

- bound parties can append messages, add same-author corrections, and resolve or exclude issues only through proposal plus acceptance by the other role;
- original messages and issue events cannot be edited or deleted, and stale revisions create no partial transition;
- only the contractor can read or change the private JSON B draft and its private export;
- the client sees neither private content nor a private-draft status signal before submission;
- JSON B binds the current JSON A and records explicit scope, exclusions, milestones, effort, responsibilities, acceptance, remediation, maintenance, expiry, and globally neutral fee details;
- fees keep billing cycle separate from cost source, accept integer amounts only, and show server-computed totals without mixing currencies or recurring periods;
- the contractor downloads and reimports the exact private-export bytes before a party-visible review exists;
- later review versions preserve prior bytes and objections bind existing business fields without editing the contractor's document; and
- candidate documents, confirmations, delivery, payments, remote push, and deployment remain absent.

### Slice 5

The immutable candidate increment is ready when:

- only the bound contractor can create a candidate from the current fixed JSON A and JSON B, with no blocking issue and an unexpired quote;
- the manifest schema and verifier bind the complete JSON A and JSON B source identities, saved PDF and SVG bytes, template, English catalog, label map, and pinned fonts;
- both bound parties preview and download the same saved bytes, while administrators, third members, and unauthenticated users receive no candidate-content access;
- the active case pointer, current candidate row, artifact kinds, visibility, lifecycle, digests, lengths, and controlled-directory files agree;
- publication uses an atomic no-replace directory operation plus file and parent-directory fsync, and partial, orphaned, or failed candidates move to quarantine;
- PDF and SVG render the same complete bounded blueprint without truncating labels, long valid body text wraps across pages, and no preview or download rerenders a document; and
- confirmations, formalization, delivery, payments, remote push, and deployment remain absent.

### Slice 6

The bilateral same-byte formalization increment is ready when:

- each bound role explicitly acknowledges the same manifest, PDF, SVG, JSON A, and JSON B artifact identities, lengths, and SHA-256 values before confirming;
- one current role confirmation leaves the case pending, may be withdrawn before formalization, and never becomes bilateral consent through silence, delay, or quote expiry;
- two current confirmations from the distinct bound client and contractor roles formalize the same current candidate inside one `BEGIN IMMEDIATE` transaction;
- formalization changes only SQLite authority and artifact lifecycle metadata; it does not render, copy, move, replace, or rewrite the manifest, PDF, SVG, JSON A, or JSON B bytes;
- a required revision invalidates the round, current confirmations, candidate, and candidate files, preserves their history, and returns the case to JSON B review;
- stale revisions, expired quotes, source drift, file tampering, inactive accounts, administrators, third members, and same-user dual-role attempts fail closed with no partial transition;
- the party-only formal verification page reauthorizes and revalidates exact bytes on every read; formal pointers, candidate rows, artifacts, acknowledgements, and terminal confirmations cannot be overwritten or deleted; and
- a client can start a separate empty parent-linked proposal without copying scope, contractor binding, confirmation, or formal state, while delivery, acceptance, evidence export, payments, remote push, and deployment remain absent.

### Slice 7

The delivery and evidence increment is ready when:

- the contractor submits one fixed delivery item and test result for every acceptance criterion in the unchanged formal JSON B;
- each immutable delivery record identifies its author, exact formal version, predecessor, remediation source, server time, review deadline, byte length, and SHA-256, with at most 20 delivery versions per PoC case;
- the client explicitly starts inspection and either accepts every item or returns identified items with reasons; silence, deadlines, GET requests, restart, and notices never create consent;
- a returned inspection creates remediation work and a new delivery version, while all earlier deliveries, tests, inspection results, reasons, and timeline events remain readable and append-only;
- explicit all-pass acceptance creates a separate immutable acceptance record before the workflow can enter `ACCEPTED`;
- either party may open a dispute before acceptance and append its own final position, while the platform records no winner, breach, liability, damages, refund, payment, or forced outcome;
- only the bound parties can view delivery evidence, prepare an export, or download the same fixed export bytes; inactive accounts, administrators, third members, anonymous users, guessed identifiers, and stale state fail closed;
- the evidence ZIP uses a schema-validated allowlist for the formal five-file set, delivery records, inspections, acceptance, timeline, and dispute positions, and excludes private drafts, credentials, sessions, invitation secrets, logs, administrator data, unrelated cases, and absolute paths;
- startup and every protected download verify controlled paths, file type, byte length, SHA-256, ZIP entry set, inner entry lengths and digests, manifest identity, current formal binding, and fixed delivery-record content; orphan delivery and export directories move to quarantine; and
- the English README, local demo guide, and factual ship evidence describe only verified local behavior while payments, adjudication, remote push, deployment, publication, and additional locales remain outside the build.

## Revision

On 2026-10-06, the learner superseded the earlier multi-language interface and regional-money contract. The current competition target is English-only and globally neutral, with a dormant catalog seam and an explicit pinned-font acceptance boundary for fixed documents.
