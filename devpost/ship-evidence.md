# Factual Ship Evidence

## Built locally

- English-only Flask 3.1 application on Python 3.12 with SQLite, Jinja, native JavaScript, ReportLab, fixed fonts, and strict JSON Schema validation.
- Private client-contractor case binding, immutable JSON A and JSON B review sources, same-byte candidate confirmation, and formal verification.
- Append-only delivery, test, inspection, explicit-acceptance, remediation, timeline, dispute-position, and evidence-export records, with closed-parent child guards and a 20-version delivery cap.
- Explicit client acceptance only; deadlines and silence never create consent.
- Party-only evidence ZIP with exact formal artifacts, an allowlisted delivery history, a strict manifest schema, bounded inner entries, and per-entry byte-length and SHA-256 verification.
- Administrator, third-member, disabled-account, anonymous, CSRF, stale-version, tamper, restart, and offline boundaries covered by automated tests.

## Local verification

Use the current repository commit and run:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m pip check
.venv/bin/python -m compileall -q app tests
```

Commit `c619738` passed independent exact staged-index review. Responsive follow-up commit `e7b7306` passed all 28 Slice 7 delivery/migration tests and the full suite at 203 passed with one sandbox-only loopback skip. The skipped loopback test passed in a permitted local run and released its port. A 789 CSS-pixel browser check confirmed that Delivery History scrolls within its own accessible region without causing whole-page horizontal overflow.

The test count increased after the offline App Map documentation test was added. On 2026-10-07, the sanitized 6-ship candidate passed 204 tests with the same one sandbox-only loopback skip, and `pip check` reported no broken requirements.

## Product boundary

The PoC preserves evidence and workflow history. It does not provide payment, escrow, refunds, external email, OAuth, third-party electronic signatures, legal advice, dispute adjudication, cloud storage, deployment, or public case discovery. Local application-layer administrator isolation is not end-to-end encryption and does not prevent the computer owner from reading local files.

## Learner-owned publication fields

The learner selected the MIT License on 2026-10-07. The bundled Noto Sans TC files retain their separate SIL Open Font License and provenance record.

These remain intentionally blank during the current `6-ship` preparation:

- Public GitHub repository URL
- Public short video URL
- Final project name and one-line description
- Devpost submission answers
- Exit survey and peer-feedback evidence

No remote, public push, deployment, video upload, or Devpost submission was created during `5-build`.
