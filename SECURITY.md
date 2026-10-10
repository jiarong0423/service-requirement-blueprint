# Security Policy

## Supported Scope

This repository is an offline, loopback-only proof of concept. It is not a hosted service and has no production security certification. Security reports should identify the affected route, workflow state, and reproducible local steps without including real credentials or private case data.

## Secret Ownership and Storage

The local operator owns all demonstration credentials and the Flask signing key.

- Demonstration passwords enter the process through the three documented environment-variable names. They have no default values and are stored only as password hashes in the ignored local SQLite database.
- The Flask signing key is generated in the ignored `instance/secret.key` file with owner-only permissions.
- Signed-in sessions contain a user ID, session version, and CSRF nonce. A signed-out invitation handoff may briefly store an invitation ID until sign-in. Roles, case IDs, email addresses, and case content are loaded from SQLite for each authorized operation.
- No provider keys, OAuth credentials, payment credentials, or cloud service accounts are required.

## Rotation and Revocation

- Replace a demonstration password by recreating the local demonstration instance with new environment-provided values.
- Replace `instance/secret.key` to invalidate every existing signed session.
- Disable a demonstration account through the administrator interface to revoke that account's sessions by session version.
- Delete the ignored local `instance/` directory only when the operator intentionally wants to discard all local demonstration state; this is not an application action and is outside the public demo flow.

## Reporting Boundary

Do not attach `.env` files, databases, generated case artifacts, invitation links, cookies, CSRF values, evidence ZIPs, or local logs to a public report. The public repository contains synthetic example identities only.
