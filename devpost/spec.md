---
doc: spec
status: approved
language: en
---

# Service Requirement Blueprint — Technical Specification

## Technical Direction

- Python 3.12
- Flask 3.1
- SQLite
- Jinja templates and native JavaScript
- ReportLab server-side PDF generation
- Local loopback runtime for the competition PoC

The application is one offline-capable monolith. SQLite is the authority for account state, case bindings, revisions, document relationships, and confirmations. JSON, SVG, PDF, and export artifacts live in a controlled local directory and are referenced by immutable database records.

## English-Only Interface Contract

`DEFAULT_LOCALE = "en"` and `SUPPORTED_LOCALES = ("en",)` are the current runtime contract. `/` redirects to `/en/`. The route registrar derives product routes from the locale registry, so the structure can support a future catalog without duplicating business logic. The current app registers no `/zh-TW` route and presents no language switch.

`app/i18n/catalog.en.json` is the only shipped catalog. Templates use stable translation keys. Catalog loading requires strict UTF-8, rejects a BOM and duplicate keys, and requires nonempty values that are not raw keys. Missing keys and formatting errors fail closed; there is no fallback catalog.

English-only constrains system-owned UI and documents. User-authored data remains Unicode and is never automatically translated or normalized into another language. JSON A generation and JSON B private export check all fixed-document text against the pinned embedded fonts; accepted text is preserved exactly, and unsupported glyphs fail before an immutable source version exists.

## Application Layers

### Routes

Routes parse requests, enforce HTTP and CSRF contracts, call services, and render catalog-backed templates. Route code does not decide identity from uploaded JSON or session-supplied roles.

Routes implemented through Slice 7:

| Method | Route | Authority |
|---|---|---|
| GET | `/` | public redirect to `/en/` |
| GET | `/en/` | public |
| GET | `/en/i18n/client.json` | public, allowlisted keys only |
| GET, POST | `/en/login` | signed out |
| POST | `/en/logout` | signed in, CSRF |
| GET | `/en/account` | active account |
| GET | `/en/owner/guide` | active member |
| POST | `/en/owner/requirements` | active non-admin member, CSRF |
| GET, POST | `/en/owner/requirements/{case_id}/sections/{section_no}` | bound client; POST requires CSRF and revision |
| GET | `/en/owner/requirements/{case_id}/review` | bound client |
| GET | `/en/owner/requirements/{case_id}/blueprint.svg` | bound client |
| POST | `/en/owner/requirements/{case_id}/json-a` | bound client, CSRF, current revision |
| GET | `/en/owner/requirements/{case_id}/json-a/{artifact_id}/download` | bound client, reauthorized and rehashed |
| GET | `/en/owner/requirements/{case_id}/publish` | bound client |
| POST | `/en/owner/requirements/{case_id}/publish/preview` | bound client, CSRF, current revision, exact JSON A bytes |
| POST | `/en/owner/requirements/{case_id}/invitations` | bound client, CSRF, designated approved member |
| GET | `/en/owner/requirements/{case_id}/invitation` | bound client |
| POST | `/en/owner/requirements/{case_id}/invitations/{invitation_id}/revoke` | bound client and same-case invitation, CSRF |
| POST | `/en/owner/requirements/{case_id}/invitations/{invitation_id}/cancel-before-import` | bound client, accepted invitation before exact JSON A import, CSRF and current revision |
| GET | `/en/invitations/{invitation_id}` | designated active member, summary only |
| POST | `/en/invitations/{invitation_id}/accept` | designated active member, CSRF, one-time code |
| POST | `/en/invitations/{invitation_id}/reject` | designated active member, CSRF, one-time code |
| GET, POST | `/en/cases/{case_id}/json-a/import` | bound contractor; POST requires CSRF and exact registered bytes |
| GET | `/en/cases/{case_id}` | bound client or contractor after verified import |
| GET | `/en/cases/{case_id}/json-a/download` | bound client or contractor, reauthorized and rehashed |
| GET | `/en/cases/{case_id}/blueprint.svg` | bound client or contractor after verified import |
| POST | `/en/cases/{case_id}/messages` | bound party, CSRF, append-only message or correction |
| POST | `/en/cases/{case_id}/issues` | bound party, CSRF, open clarification issue |
| POST | `/en/cases/{case_id}/issues/{issue_id}/events` | bound party, CSRF, current issue revision and allowed transition |
| GET, POST | `/en/cases/{case_id}/json-b/draft` | bound contractor only; POST requires CSRF and current draft revision |
| POST | `/en/cases/{case_id}/json-b/draft/export` | bound contractor only, CSRF, current draft revision |
| GET | `/en/cases/{case_id}/json-b/draft/exports/{artifact_id}/download` | bound contractor only, reauthorized and rehashed |
| GET, POST | `/en/cases/{case_id}/json-b/review/import` | bound contractor only; POST requires CSRF and exact private-export bytes |
| GET | `/en/cases/{case_id}/json-b/reviews/{version_no}` | bound party, immutable review |
| GET | `/en/cases/{case_id}/json-b/reviews/{version_no}/download` | bound party, reauthorized and rehashed |
| POST | `/en/cases/{case_id}/json-b/reviews/{version_no}/objections` | bound client only, CSRF, valid business-field pointer |
| POST | `/en/cases/{case_id}/json-b/reviews/{version_no}/revise` | bound contractor only, CSRF, create next private revision |
| POST | `/en/cases/{case_id}/candidates` | bound contractor only, CSRF, current fixed sources |
| GET | `/en/cases/{case_id}/candidates/{candidate_id}` | bound party in `JSON_B_REVIEW`, `CANDIDATE_REVIEW`, or `FORMAL`; current, invalidated, or formal historical candidate with saved-byte verification |
| GET | `/en/cases/{case_id}/candidates/{candidate_id}/manifest.json` | bound party, reauthorized and revalidated, attachment |
| GET | `/en/cases/{case_id}/candidates/{candidate_id}/document.pdf` | bound party, reauthorized and revalidated, inline or exact-byte attachment |
| GET | `/en/cases/{case_id}/candidates/{candidate_id}/diagram.svg` | bound party, reauthorized and revalidated, inline or exact-byte attachment |
| POST | `/en/cases/{case_id}/candidates/{candidate_id}/acknowledgements` | bound party, CSRF, current round revision and exact fixed tuple; creates the base acknowledgement or one renewal linked to one outstanding exact withdrawal |
| POST | `/en/cases/{case_id}/candidates/{candidate_id}/confirmations` | acknowledged bound party, CSRF, current round revision; second role may formalize |
| POST | `/en/cases/{case_id}/candidates/{candidate_id}/confirmations/withdraw` | confirming party, CSRF, only before formalization |
| POST | `/en/cases/{case_id}/candidates/{candidate_id}/revision-requests` | bound party, CSRF, reason required; invalidates the round |
| GET | `/en/cases/{case_id}/formal` | bound party, exact-byte formal verification |
| GET | `/en/cases/{case_id}/formal/manifest.json` | bound party, fresh authorization and digest validation |
| GET | `/en/cases/{case_id}/formal/document.pdf` | bound party, fresh authorization and digest validation |
| GET | `/en/cases/{case_id}/formal/diagram.svg` | bound party, fresh authorization and digest validation |
| GET | `/en/cases/{case_id}/formal/requirement.json` | bound party, exact saved source bytes and digest validation |
| GET | `/en/cases/{case_id}/formal/proposal.json` | bound party, exact saved source bytes and digest validation |
| POST | `/en/cases/{case_id}/separate-proposals` | formal case client, CSRF, idempotent empty child proposal |
| GET | `/en/cases/{case_id}/delivery` | bound party, current formal and delivery state revalidated |
| POST | `/en/cases/{case_id}/deliveries` | bound contractor, CSRF, `FORMAL` or `REMEDIATION`, idempotent complete criterion set |
| GET | `/en/cases/{case_id}/deliveries/{delivery_id}/download` | bound party, current authorization plus path, length, and digest validation |
| POST | `/en/cases/{case_id}/deliveries/{delivery_id}/begin-inspection` | bound client, CSRF, current `DELIVERED` revision |
| POST | `/en/cases/{case_id}/deliveries/{delivery_id}/inspections` | bound client, CSRF, current `UNDER_ACCEPTANCE` or `REINSPECTION` revision; complete criteria; all-pass requires explicit acceptance, while any returned item forbids it |
| POST | `/en/cases/{case_id}/disputes` | bound party, CSRF, current `DELIVERED`, `UNDER_ACCEPTANCE`, `REMEDIATION`, or `REINSPECTION` revision and reason |
| POST | `/en/cases/{case_id}/disputes/{dispute_id}/positions` | bound party, CSRF, dispute must match the workflow's active dispute, one append-only final position per role |
| POST | `/en/cases/{case_id}/evidence-exports` | bound party, CSRF, `ACCEPTED` or `DISPUTED`, idempotent fixed source fingerprint; `?download=1` returns the current ZIP attachment with private no-store headers |
| GET | `/en/cases/{case_id}/evidence-exports/{export_id}/download` | bound party, current authorization plus ZIP schema, path, length, digest, and inner-entry validation |
| GET | `/en/admin/accounts` | administrator safe projection |
| POST | `/en/admin/accounts/{user_id}/status` | administrator, CSRF |
| GET | `/en/admin/service-health` | administrator safe projection |

Unknown locale prefixes and unprefixed product paths return 404.

### Services

- `case_workflow`: creates cases, validates sections, controls revisions, builds JSON A, and commits current artifact pointers.
- `json_contracts`: strict bytes parsing, Schema validation, one-time serialization, and SHA-256.
- `artifact_store`: UUID-only paths, no-overwrite installation, fsync, read-only final files, and digest-on-read.
- `blueprint`: derives safe structured nodes and edges and escapes text before SVG output.
- `invitation_workflow`: verifies exact JSON A source bytes, issues single-use expiring invitations, binds the designated contractor, controls terminal invitation states, and activates the private case only after exact-byte reimport.
- `discussion_workflow`: authorizes current case parties, appends messages and corrections, opens issues, and applies revision-checked two-role issue transitions.
- `json_b_workflow`: keeps contractor drafts private, computes currency-aware totals, binds current JSON A, exports immutable private bytes, validates exact reimport, publishes review versions, and preserves superseded bytes.
- `candidate_documents`: validates one bounded diagram model and renders the same complete model into fixed PDF and SVG bytes using catalog-backed labels and pinned fonts. Server-controlled JSON A codes receive readable labels, while contractor-authored JSON B `role_code` and `service_code` values remain verbatim.
- `candidate_workflow`: checks candidate prerequisites and complete source identities, commits one active candidate transactionally, revalidates protected reads, and reconciles controlled storage at startup.
- `formalization_workflow`: derives live party roles, binds acknowledgements and confirmations to the complete fixed artifact tuple, serializes withdrawal/revision/formalization, verifies formal reads, and creates idempotent empty child proposals.
- `delivery_workflow`: binds post-formal delivery, inspection, remediation, reinspection, explicit acceptance, dispute, and party-position transitions to the unchanged formal candidate and preserves append-only evidence.
- `evidence_export`: creates and reads one immutable party-only ZIP for accepted or disputed cases, validates the schema-allowlisted entry set and source fingerprint, and reconciles controlled storage at startup.
- `document_integrity`: loads and hashes catalog, label-map, font, licence, and provenance assets; enforces the supported-glyph boundary; validates inactive PDF and safe SVG output.
- `security`: password hashing, bounded password verification, session loading, CSRF, and case-party authorization.

### Templates and Client JavaScript

Templates contain structure and translation-key lookups. Native JavaScript may progressively enhance the page but does not own authorization, workflow state, translation data, or validation authority. If JavaScript fails, server forms remain usable.

## SQLite Contract

Every connection enables and reads back:

- foreign keys;
- WAL journal mode;
- synchronous `FULL`; and
- a 5000 millisecond busy timeout.

Mutations use explicit `BEGIN IMMEDIATE`, `COMMIT`, and `ROLLBACK`. Multi-row security changes and version transitions are atomic.

Schema upgrades run only through the migration CLI. When an upgrade is needed, it acquires `BEGIN IMMEDIATE` before taking a SQLite backup snapshot, validates and fsyncs that private backup, applies all pending migrations, then runs integrity and foreign-key checks before commit. A failed check rolls back the canonical database; a concurrent writer cannot enter between the snapshot and migration commit.

Current tables:

### `users`

Stores normalized email, display name, scrypt password hash, system role, account status, session version, and server timestamps.

### `cases`

Stores the client and optional contractor bindings, title, agreement state, content revision, current JSON A and JSON B pointers, active and formal candidate pointers, and timestamps. The agreement states include `JSON_B_PRIVATE_DRAFT`, `JSON_B_REVIEW`, `CANDIDATE_REVIEW`, and terminal `FORMAL`. Slice 7 does not unlock or overload this formal state; delivery lifecycle is stored separately.

### `requirement_drafts`

Stores the private client draft as Unicode JSON plus the completed-section set. It is not an export, quote, contract, or formal record.

### `artifacts`

Stores immutable artifact identity, case, author, kind, version, visibility, lifecycle, relative path, SHA-256, byte length, schema version, and timestamp. A new version supersedes the prior pointer without overwriting prior bytes.

### `audit_events`

Stores identifiers, action/result codes, correlation ID, and server time. It must not store passwords, tokens, cookies, document bytes, user text, request bodies, or absolute paths.

### `invitations`

Stores the case, owner, designated contractor, fixed JSON A identity, one-way token hash, server-owned lifecycle timestamps, and terminal state. At most one pending invitation exists per case. Raw invitation codes are returned once and are never stored.

### `json_a_source_verifications`

Stores the party, invitation, artifact, supplied byte digest and length, document identity, and server verification time for the exact JSON A each party reimported. Uploaded identity, role, status, and time fields never grant authority.

### `case_messages`, `case_issues`, and `issue_events`

Store party-authored discussion, correction links, current issue state/revision, and immutable transition history. Database triggers reject message and event updates or deletes. Issue closure requires an acceptance event from the role that did not create the current proposal.

### `json_b_drafts`, `json_b_private_exports`, and `json_b_versions`

Store one current contractor-only draft, its immutable private exports, and party-visible submitted review versions. The draft binds the current JSON A identity and an optimistic revision. A submitted review reuses the exact saved private-export artifact; later versions supersede metadata without overwriting bytes.

### `candidate_versions`

Stores one current fixed candidate per case, its exact JSON A and JSON B source identities, manifest/PDF/SVG artifact pointers, template identity, English catalog digest, label-map digest, creation actor, review revision, lifecycle timestamps, invalidation reason, version, and state. A candidate is `CURRENT`, `INVALIDATED`, `SUPERSEDED`, or `FORMAL`. The case must point to the same current candidate in `CANDIDATE_REVIEW` and the same formal candidate in `FORMAL`; startup fails closed on their pointer, artifact-row, manifest, or file mismatch. Historical `SUPERSEDED` directories remain retained and are checked against their recorded three-file paths, lengths, and digests without reinterpreting an older manifest schema.

### `candidate_review_rounds`, `candidate_acknowledgements`, `candidate_acknowledgement_renewals`, and `candidate_confirmations`

Store the open, invalidated, or formalized review round and each role's explicit acknowledgement and confirmation history. Every base acknowledgement and confirmation duplicates the exact manifest, PDF, SVG, JSON A, and JSON B artifact IDs, byte lengths, and SHA-256 values it binds. A base acknowledgement is append-only. Each acknowledgement-renewal event is also append-only and links that same base acknowledgement to one exact withdrawn confirmation; database triggers reject mismatched roles, actors, rounds, cases, candidates, non-withdrawn confirmations, malformed audit timestamps, or application attempts to create a legacy marker. Migration `007` uses the append-only SQLite confirmation-row creation sequence only to reconstruct which pre-policy withdrawals were followed by another confirmation, and records those relationships as `LEGACY_ACKNOWLEDGEMENT_REUSE` rather than falsely labelling them as new acknowledgements. Runtime authorization never orders timestamps or UUIDs; more than one outstanding withdrawal fails closed as an acknowledgement conflict. All post-migration renewals are `EXPLICIT`. Confirmations may move only from `CURRENT` to `WITHDRAWN`, `INVALIDATED`, or `FORMALIZED`; terminal rows and formal records are immutable. Partial unique indexes allow at most one current confirmation per role and actor.

### `separate_proposal_requests`

Stores an append-only idempotency record linking one formal parent case to a fresh child requirement draft created by the original client. The child starts without copied scope, contractor, JSON, candidate, confirmation, or formal state.

### `delivery_workflows`, `delivery_versions`, `delivery_items`, and `delivery_test_results`

`delivery_workflows` is a one-to-one post-formal state machine bound to the unchanged formal candidate. It stores `FORMAL`, `DELIVERED`, `UNDER_ACCEPTANCE`, `REMEDIATION`, `REINSPECTION`, `ACCEPTED`, or `DISPUTED` plus current delivery, inspection, dispute, and revision pointers. Delivery versions contain an immutable server-generated JSON record, one item and one test result for every formal JSON B acceptance criterion, a predecessor and returned-inspection binding for remediation, server time, review deadline, byte length, and SHA-256. Submitted evidence rows reject update and delete.

### `inspection_rounds`, `inspection_item_results`, and `case_timeline_events`

Each client inspection covers the exact criterion set of the current delivery. Every returned item requires a reason; all-pass acceptance requires a separate explicit acceptance value in the same POST. Timeline rows preserve delivery, deadline, inspection, remediation, dispute, position, and export events. Deadline rows are observations and never transition consent state.

### `disputes`, `dispute_positions`, and `evidence_exports`

A dispute binds the formal candidate, current delivery, latest inspection, prior workflow state, opener role, reason, and server time. Each role may append one final position. No winner, breach, liability, damages, refund, payment, or forced-resolution field exists. Evidence exports are immutable party-only ZIP records with a source fingerprint, relative controlled path, exact byte length, SHA-256, version, and server time.

## Slice 4 Discussion and JSON B Contract

Slice 4 expands the case machine to `JSON_B_PRIVATE_DRAFT` and `JSON_B_REVIEW`. JSON A remains the fixed requirement baseline in both states. The case room keeps messages, issues, issue events, and submitted JSON B review versions party-visible; the contractor's draft and active private export remain contractor-only.

Messages are append-only. A correction inserts a new message that points to an earlier message from the same author and case. Messages and issue events have database triggers that reject update and delete operations. Issues use optimistic revisions and append-only events. Supported transitions are `OPEN -> PROPOSED -> RESOLVED`, `PROPOSED -> REVISION_REQUESTED -> PROPOSED`, and `OPEN|REVISION_REQUESTED -> OUT_OF_SCOPE_PROPOSED -> OUT_OF_SCOPE`. A proposal reaches `RESOLVED` or `OUT_OF_SCOPE` only when the other case role accepts that exact revision. Silence, expiry, upload, or a case-state change never closes an issue.

JSON B uses JSON Schema Draft 2020-12, strict UTF-8, a 1 MiB byte limit, and `additionalProperties: false` at every object level. Its server-owned identity fields are `schema_version = "json-b/1.0"`, `document_type = "contractor_proposal"`, UUID document and case IDs, positive version, generation time, and the exact current JSON A artifact/document/version/schema/digest/length binding. Business fields cover requirement understanding, included and excluded scope, pages, functions, data and permissions, third-party services, milestones, effort and difficulty, responsibilities, acceptance, remediation, maintenance, quote expiry, fee items, and computed totals.

The English-only globally neutral fee contract uses:

- `billing_kind`: `one_time` or `recurring`;
- `cost_source`: `contractor` or `third_party`;
- `currency_code`: three uppercase ASCII letters;
- `amount`: a nonnegative integer no greater than 9,999,999,999;
- `billing_period`: null for one-time fees and `monthly`, `quarterly`, or `yearly` for recurring fees; and
- `included_in_quote`: boolean.

The server groups totals by billing kind, source, currency, period, and inclusion flag. It never combines currencies or recurring periods. Uploaded totals must equal server recomputation. Floats, booleans, strings, negative values, conversions, tax, payment, escrow, and quote-fairness behavior are rejected or absent.

The contractor saves a revisioned private structured draft. Export adds server identity, JSON A binding, and computed totals, serializes once, and installs one private immutable artifact. Repeating export for the unchanged draft returns that same artifact. The contractor downloads and reimports the exact bytes. Submission rechecks bytes, digest, document identity, totals, quote expiry, current JSON A, current bindings, and revisions inside `BEGIN IMMEDIATE`, then changes that same saved artifact to party-only review visibility and updates the current JSON B pointer. It never stores the upload as another file. A changed draft invalidates its prior unsubmitted export; a later submitted version supersedes the prior review record without changing or deleting old bytes.

Only the client may add an objection to an allowlisted, resolving RFC 6901 business-field pointer in a submitted review. An objection appends a linked issue and never edits JSON B. Server identity, source identity, and computed-total fields cannot be objected to. Only the contractor creates the revised private draft and later review version.

## Account, Session, and CSRF Contract

- `init-demo` requires three nonempty password environment variables and has no defaults.
- Missing input creates no secret, database, account, or data directory.
- The secret is 32 random bytes, installed atomically with mode `0600`.
- All three demonstration accounts are inserted in one transaction with scrypt hashes.
- Initialization failure removes only files created by that attempt.
- Signed-in Flask session data contains `user_id`, `session_version`, and `csrf_nonce`. A signed-out invitation handoff may briefly contain `post_login_invitation_id`; sign-in clears the previous session before creating the authenticated one.
- Login and logout clear the old session and issue a new nonce.
- The nonce has at least 32 bytes of entropy and is compared in constant time.
- Every POST validates CSRF before entering the service layer.
- Each protected request reloads current account status and session version from SQLite.
- Disabling an account increments its session version in the same transaction.
- Invalid credentials return one message and still execute the password-verification path for an unknown account.
- Password verification reports `VALID`, `INVALID`, or `BUSY`. Exhausted KDF capacity returns the same `503 Service Unavailable`, `Retry-After: 1`, and generic message for existing and unknown accounts; it is never collapsed into an invalid-credential result.

## Administrator Isolation

Administrator routes select only the approved account projection and service-health values. They do not join or query cases, requirement drafts, artifacts, discussions, documents, or evidence. Administrator role is never a case-content bypass. If an administrator is also a party in a future case, access must come from that explicit party binding.

Error pages, logs, health pages, and account lists must not expose case IDs, titles, drafts, digests, filenames, paths, or document summaries.

## JSON A 1.1 Contract

The schema uses JSON Schema Draft 2020-12 and `additionalProperties: false` for every object. The top-level fields are:

- `schema_version = "json-a/1.1"`
- `document_type = "requirement_request"`
- server-owned `document_id`, `version`, and `generated_at`
- `project`
- `users_and_scenarios`
- `pages_and_content`
- `features`
- `data_and_permissions`
- `external_services_and_assets`
- `visual_preferences`
- `delivery_and_constraints`
- structured `blueprint`
- `open_items`

`delivery_and_constraints` includes:

- `budget_status`
- `budget_currency`: null or three uppercase ASCII letters
- `budget_min_amount`: null or a nonnegative integer
- `budget_max_amount`: null or a nonnegative integer

If either amount exists, currency is required. If both amounts exist, minimum must not exceed maximum. Floats and string amounts are rejected. The PoC records whole-unit requirement amounts and does not execute payments.

The parser accepts strict UTF-8 only, rejects a BOM, invalid bytes, replacement characters, duplicate keys at any depth, unknown fields, invalid formats, and files larger than 1 MiB. Multipart requests are limited to 2 MiB.

Identity, role, status, confirmation, and server time are never accepted as uploaded authority. User strings remain exactly as accepted by the application.

The server serializes JSON A once as UTF-8 without a BOM and with one final LF. It hashes and saves those exact bytes. Every download returns the saved file after current authorization and digest verification. Reformatting or reserialization creates different bytes and cannot impersonate the stored artifact.

## Controlled Artifact Storage

Artifact paths use server UUIDs only:

```text
instance/data/cases/<case_uuid>/artifacts/<artifact_uuid>/content.json
instance/data/cases/<case_uuid>/artifacts/<artifact_uuid>/diagram.svg
instance/data/cases/<case_uuid>/staging/<operation_uuid>.tmp
instance/data/cases/<case_uuid>/quarantine/<orphan_uuid>/
```

Resolved paths must remain inside the controlled data root. Directories use `0700`; temporary files use `0600`; final files use `0400`. Installation uses no-existing-destination semantics, file fsync, parent-directory fsync, and no `os.replace` over an existing artifact. A failed installation never becomes current or downloadable.

## Gate 0 Document Preflight

Gate 0 inputs are one English catalog, a fixed label-key map, a globally neutral sample, licensed Unicode-capable font assets, and pinned font provenance.

The sample uses:

- `currency_code`: a three-letter currency code;
- `amount`: a nonnegative integer;
- English project and system text; and
- Unicode-safe user-content handling.

The builder produces exactly one `document.pdf`, one `diagram.svg`, and one `manifest.json`. The PDF and SVG use English system labels. The manifest uses schema `gate0-preflight-manifest/1.1` and `label_mode = "english_only"`.

The first saved candidate bytes are the identity. The system does not require a future ReportLab run to reproduce identical bytes. All later preview, download, acknowledgement, confirmation, and formalization operations refer to the originally saved files.

The current Noto Sans TC files remain pinned, licensed implementation assets because they provide broad Unicode coverage for user-authored text. Their internal family name does not enable a second UI language or create a localization guarantee. Broad coverage is not universal coverage: the server derives the supported code-point set from the pinned fonts and rejects unsupported document characters before JSON A generation or JSON B private export.

## Candidate and Formal Version Contract

Slice 5 builds a candidate only after fixed JSON A and JSON B sources are valid, the JSON B complete source object matches JSON A, the quote is current, and no blocking issue remains. A candidate manifest binds complete source identities, template version, English catalog digest, label-map digest, font digests, one SVG digest, and one PDF digest. The manifest is schema-validated before publication and revalidated with its saved files on every protected read.

The JSON A blueprint contract contains exactly three nodes and two directed edges. Node and edge labels are each bounded to 240 characters. One validated semantic model feeds both the ReportLab drawing and the standalone safe SVG; both outputs retain every node, edge, direction, and complete label.

Candidate publication writes the three files to a private staging directory, fsyncs each file and the staging directory, then atomically renames the directory with no-replace semantics. Parent directories are fsynced. Any post-publish failure quarantines the published directory. Startup reconciliation rejects a mismatch between the case pointer, current candidate row, artifact contract, manifest, or saved files and quarantines unreferenced staging and candidate directories.

The fixed PDF uses lifecycle-neutral wording; candidate, invalidated, and formal status live on authenticated verification pages. Template `candidate-document/1.1` introduced this boundary. Template `candidate-document/1.2` keeps internal identifiers, digests, schema names, and machine fields in the manifest and authenticated verification disclosure while rendering plain-language agreement content in the PDF. Template `candidate-document/1.3` preserves contractor-authored JSON B `role_code` and `service_code` values exactly while retaining readable labels for server-controlled JSON A codes and declared enums. Template `candidate-document/1.4` names each open item from its bound JSON A field and rejects unresolved pointers before rendering. Earlier immutable candidates are never reinterpreted or rerendered.

Each party acknowledgement and confirmation binds the same manifest, PDF, SVG, JSON A, and JSON B artifact IDs, lengths, and digests. Mutations run under `BEGIN IMMEDIATE`, derive role and time from live SQLite data, recheck both active accounts, pointers, sources, quote expiry, blocking issues, manifest, and exact saved files, and reject stale input with zero writes. A party may withdraw only its own current confirmation before formalization. That withdrawn confirmation blocks reconfirmation until the same party creates an append-only acknowledgement-renewal event linked to that exact withdrawal; a second withdrawal requires another renewal. A required revision invalidates the round and current confirmations, clears the active pointer, marks the candidate files invalidated, and returns to `JSON_B_REVIEW` while preserving all rows and bytes.

Two current confirmations from the distinct bound roles formalize the existing candidate in one transaction. The transition clears the active pointer, sets the formal pointer, changes candidate and bound artifact lifecycle metadata to formal, and marks both confirmations formalized. It never invokes ReportLab, copies, moves, replaces, or writes a document file. Formal reads are party-only and revalidate the same saved bytes. Formal case identity, candidate rows, artifacts, acknowledgements, and terminal confirmations reject overwrite or deletion.

## Delivery, Acceptance, and Evidence Export

Delivery versions, items, test results, inspections, inspection results, acceptance events, timeline events, disputes, positions, and evidence exports are append-only. A delivery contains the exact criterion set from the unchanged formal JSON B and is saved once as a server-generated JSON file below `cases/<case_id>/deliveries/<delivery_id>/delivery.json`. The PoC permits at most 20 delivery versions per case so remediation history and export size stay bounded. The delivery request digest binds the actor, case, formal candidate, summary, items, and tests; replaying the same idempotency key with changed content fails.

A returned item requires a reason. Remediation creates a new delivery version bound to the returned inspection, followed by reinspection. Only an explicit all-pass client POST creates a separate acceptance event and permits the workflow to enter `ACCEPTED`. The database guard requires the current delivery, complete criterion set, all-pass child results, matching criterion digest, client identity, and inspection time. Every protected delivery view and startup reconciliation recomputes inspection consistency and the acceptance evidence digest. Deadline events, silence, GET requests, and restart never imply consent. A dispute preserves both parties' final positions without platform adjudication.

Evidence export is party-only and excludes private drafts. It includes exactly the formal manifest, PDF, SVG, JSON A and JSON B plus the five allowlisted delivery-record files for deliveries, inspections, explicit acceptance, timeline, and dispute positions. The ZIP manifest conforms to JSON Schema Draft 2020-12 with no unknown fields and lists each required path, length, and SHA-256 in canonical path order. Export verification rejects a missing, substituted, duplicate, or unsafe path; encrypted or non-regular entries; oversized manifests; more than 64 MiB of declared content; mismatched inner bytes; a source fingerprint that does not recompute from the canonical entry list; or a manifest that does not match the database export, case, formal candidate, version, schema, and source fingerprint. Preparing an export is POST with CSRF; `?download=1` prepares or reuses the package for the current evidence state and immediately returns it. Saved downloads use GET with fresh account, party authorization, controlled-path, regular-file, length, outer-digest, manifest, and inner-entry validation. The dashboard and every saved download recompute the current evidence fingerprint inside one deferred SQLite read snapshot. Only the package whose fingerprint matches the current case evidence is downloadable; earlier immutable packages remain listed as superseded history. A matching current fingerprint returns the same stored ZIP bytes instead of silently regenerating the export.

Startup reconciliation performs the same delivery and export checks before serving requests. It compares every fixed delivery JSON object with the append-only database rows, verifies exact controlled paths, and quarantines unreferenced delivery and export directories. Database transactions and unique idempotency constraints serialize concurrent preparation so identical export requests converge on one row and one fixed ZIP.

## Error Handling and Logs

User-facing errors use neutral catalog keys. Unexpected errors return sanitized output. Logs may include correlation IDs, machine actions, result codes, and server timestamps. They exclude secrets, session data, CSRF values, credentials, case content, user text, document bytes, and absolute paths.

## Build Verification Gates

### Gate 0

```bash
.venv/bin/python -m pytest -q tests/test_gate0.py tests/test_i18n_contract.py
```

Required evidence:

- exactly one enabled English catalog;
- catalog strictness and missing-key failure;
- no `/zh-TW` route registration;
- English PDF and SVG system labels;
- original sample Unicode user content unchanged within the pinned-font acceptance set;
- embedded pinned fonts and valid licensing;
- unique non-overwriting output directories;
- atomic publication, quarantine, digest verification, and sanitized CLI output.

### Slice 2

```bash
.venv/bin/python -m pytest -q \
  tests/test_auth_csrf.py \
  tests/test_permissions.py \
  tests/test_json_contracts.py \
  tests/test_i18n_contract.py
```

Required evidence:

- fail-closed initialization and cleanup;
- password, session, CSRF, account-disable, and stale-session behavior;
- owner-only case and artifact access;
- administrator SQL boundary and safe projection;
- English route coverage and unsupported-locale 404;
- strict JSON A 1.1 validation;
- currency-code and integer-amount rules;
- exact saved bytes across repeated downloads and restart;
- no overwrite, tamper fail-closed, and Unicode round trip;
- no JSON B, candidate confirmation, delivery, payment, or cloud behavior in the Slice 2 boundary.

### Slice 3

```bash
.venv/bin/python -m pytest -q \
  tests/test_invitations.py \
  tests/test_slice3_routes.py \
  tests/test_permissions.py \
  tests/test_json_contracts.py \
  tests/test_migration_cli.py \
  tests/test_i18n_contract.py
```

Required evidence:

- populated Slice 2 databases upgrade atomically after a locked, validated private backup snapshot;
- a single-use expiring invitation binds one approved contractor and never persists the raw code;
- preview is non-authoritative and exact registered JSON A bytes are required again at creation and contractor import;
- wrong account, administrator, third member, replay, wrong case, expired, revoked, rejected, and tampered-source paths fail closed;
- case activation occurs only after the designated contractor imports the registered JSON A;
- both parties can navigate to the same private case, download the same verified bytes, and view the same blueprint; and
- JSON B, discussion, candidate confirmation, delivery, payment, remote push, and deployment remain absent.

### Slice 4

```bash
.venv/bin/python -m pytest -q \
  tests/test_migration_cli.py \
  tests/test_json_b_schema.py \
  tests/test_discussion.py \
  tests/test_json_b.py \
  tests/test_slice4_routes.py \
  tests/test_permissions.py
```

Required evidence:

- populated Slice 3 databases migrate without losing party bindings, invitation history, JSON A verifications, artifact identity, or bytes;
- party messages and issue events are append-only and corrections preserve originals;
- issue resolution and out-of-scope decisions require distinct-role proposal and acceptance at the same issue revision;
- contractor draft content, private export identity, and draft-state detail remain hidden from clients, administrators, third members, and anonymous users;
- strict JSON B validation rejects unknown authority, malformed encoding, duplicate keys, invalid money, invalid periods, wrong totals, stale revisions, wrong source, and changed bytes;
- repeated export and exact submission are idempotent, while semantic-equivalent reserialization is rejected;
- submitted and superseded JSON B review versions remain party-readable with the original SHA-256 and bytes;
- client objections bind a resolving allowlisted business pointer and never edit review bytes; and
- candidate PDF/SVG, confirmation, delivery, payment, remote push, and deployment remain absent.

### Slice 5

```bash
.venv/bin/python -m pytest -q \
  tests/test_candidate_documents.py \
  tests/test_candidate_workflow.py \
  tests/test_slice5_routes.py \
  tests/test_permissions.py
```

Required evidence:

- a candidate is built only from the current fixed JSON A and JSON B with no blocking issue and an unexpired quote;
- one schema-validated manifest binds the complete source objects, fixed PDF and SVG bytes, template, English catalog, label map, and pinned fonts;
- both parties read the same protected files while administrators, third members, and anonymous users fail closed;
- no preview or download rerenders, replaces, or overwrites the saved candidate;
- long accepted text and every blueprint node and edge remain readable without unsafe SVG markup; and
- confirmation, formalization, delivery, payment, remote push, and deployment remain absent.

### Slice 6

```bash
.venv/bin/python -m pytest -q \
  tests/test_slice6_migration.py \
  tests/test_formalization_workflow.py \
  tests/test_slice6_routes.py \
  tests/test_candidate_workflow.py \
  tests/test_permissions.py
```

Required evidence:

- a populated Slice 5 database migrates atomically and invalidates lifecycle-specific legacy candidates without changing their rows or file bytes;
- each acknowledgement and confirmation binds the complete manifest, PDF, SVG, JSON A, and JSON B artifact IDs, byte lengths, and SHA-256 values;
- one confirmation remains pending and revocable, while silence, expiry, stale revisions, tampering, or inactive parties never become consent; reconfirmation after each withdrawal fails until a new acknowledgement-renewal event is recorded;
- simultaneous confirmation, confirmation-versus-revision, and withdrawal-versus-confirmation operations serialize to one coherent result;
- two distinct-role confirmations formalize the original candidate by database transition only, without rendering, copying, moving, or rewriting any file;
- formal identity, candidate, artifacts, acknowledgements, and terminal confirmations reject overwrite or deletion;
- only the bound parties can reopen, verify, and download the same formal bytes after restart; and
- a separate proposal starts empty and parent-linked, while Slice 7 delivery and acceptance behavior remains absent.

### Slice 7

```bash
.venv/bin/python -m pytest -q \
  tests/test_slice7_migration.py \
  tests/test_slice7_delivery.py \
  tests/test_offline_runtime.py \
  tests/test_ship_docs.py \
  tests/test_permissions.py \
  tests/test_i18n_contract.py
```

Required evidence:

- a populated Slice 6 database migrates atomically, preserves existing rows and files, and rolls back completely when the migration or post-migration checks fail;
- delivery, test, inspection, acceptance, timeline, dispute, position, and export evidence rejects update and delete, while closed parent records reject late child rows;
- delivery and inspection idempotency keys replay exact requests but reject changed content, and concurrent export requests converge on one fixed export;
- the complete return, remediation, reinspection, and explicit acceptance journey preserves every prior version and never treats silence, a deadline, GET, restart, or notice as consent;
- party authorization is rechecked for every page and download; inactive parties, administrators, third members, and anonymous clients receive no delivery or export bytes;
- the evidence export contains the formal five-file set and allowlisted delivery evidence with actor IDs and roles, while private-draft sentinel content and unrelated service data remain absent;
- outer-file and inner-ZIP tampering, controlled-path substitution, schema failure, delivery-record drift, missing files, and restart reconciliation fail closed; orphan fixed directories move to quarantine;
- unexpected application errors return a correlation ID and log only the method and endpoint, without private request content, credentials, document bytes, or absolute filesystem paths;
- the full journey succeeds with outbound network calls denied, and the loopback smoke process receives bounded TERM cleanup and releases its port; and
- README, demo instructions, and ship evidence stay English, factual, and local, while payment, adjudication, remote push, deployment, public publishing, and additional locales remain absent.

### Full Regression

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m pip check
.venv/bin/python -m compileall -q app tests
```

## Simplifications

- Local loopback HTTP is allowed only with `POC_LOCAL_HTTP=1`; production HTTPS and secure cookies remain Later.
- Demonstration accounts replace registration, email verification, password reset, and production recovery.
- SQLite replaces a cloud database.
- Local controlled storage replaces object storage.
- One English catalog replaces the previous multi-language delivery contract while retaining the localization seam.
- One globally neutral case replaces country-specific product behavior.
- No email, OAuth, payments, remote services, or third-party signature service is used.

## Revision

On 2026-10-06, the learner replaced the prior multi-language route, document-label, and regional-money contracts. Version 1.1 enables English only, preserves a dormant catalog seam, uses currency-neutral budget fields, and retains Unicode-safe user content.

On 2026-10-06, Slice 6 made fixed-document wording lifecycle-neutral so the exact reviewed bytes can later become formal. The migration preserves but invalidates candidates containing the superseded candidate-only notice; users must create a new candidate rather than reinterpret or rerender old bytes.
