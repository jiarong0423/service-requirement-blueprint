# Local Demo Guide

This guide demonstrates one accepted case and one disputed case without network access. Use separate browser profiles or one normal and one private window so the client and contractor keep distinct signed sessions.

## Prepare

1. Initialize and start the app with the commands in `README.md`.
2. Open `http://127.0.0.1:5000/en/login` in two sessions.
3. Sign in as `owner@example.test` in the client session and `contractor@example.test` in the contractor session.

## Agreement path

1. As the client, create a requirement, complete all eight sections, review the blueprint, and download JSON A.
2. Create a private invitation for the contractor. Transfer the local link, one-time code, and unchanged JSON A through the demonstration channel.
3. As the contractor, accept the invitation and import the exact JSON A.
4. Add a case message and resolve any blocking issue. Create JSON B, download its private export, and reimport the exact bytes for party review.
5. Create the candidate. In each session, inspect the same PDF, SVG, manifest, JSON A, and JSON B digests; acknowledge and confirm that candidate.
6. Open the formal verification page and show that both sessions download the same formal PDF bytes.

## Return, remediation, and explicit acceptance

1. From formal verification, open **Delivery and Acceptance Review**.
2. As the contractor, submit one note, evidence reference, test outcome, and observation for every formal criterion.
3. As the client, explicitly begin inspection. Return at least one item and state a concrete reason.
4. As the contractor, submit a remediation delivery. Confirm that the original delivery and inspection remain visible.
5. As the client, mark every current item `PASS`, check the explicit acceptance statement, and submit.
6. Confirm that the acceptance panel identifies the fixed delivery, inspection, client, server time, and evidence digest. A deadline by itself must not create this record.
7. Prepare the evidence export. Download it in both sessions and inspect `export-manifest.json`, the formal five-file set, immutable delivery versions, both inspections, acceptance record, and timeline events. The same unchanged source state returns the same export bytes.

## Dispute evidence path

Use a separate case so acceptance remains terminal.

1. Formalize and submit an initial delivery.
2. Either party opens a dispute with a factual reason.
3. Each party records one final position. Leave the other position absent until that party submits it; the platform must not invent a response.
4. Prepare the evidence export. Show that it contains both submitted positions and no winner, liability, damages, refund, or payment decision.

## Negative checks

- Open `/zh-TW/` and confirm `404`.
- Sign in as the administrator and try a known delivery URL; confirm the same neutral denial used for other outsiders.
- Reload a delivered case and wait past no interval; confirm that no GET or silence changes it to accepted.
- Try to accept without checking the explicit acceptance statement; confirm rejection.
- Return an item without a reason; confirm rejection.
- Inspect the ZIP and confirm it contains no private JSON B draft, password hash, session, CSRF value, invitation secret, log, absolute path, or unrelated case.
- Confirm the page states that this PoC supports at most 20 immutable delivery versions per case.

The public Git repository, short video, submission text, deployment, and publishing steps belong to `6-ship` and are outside this local build guide.
