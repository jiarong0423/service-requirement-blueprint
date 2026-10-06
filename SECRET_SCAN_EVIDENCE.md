# Secret Scan Evidence

Review date: 2026-10-07  
Target: sanitized public candidate only

## Boundary

- The candidate was created from an explicit allowlist and contains no canonical `.git` history.
- Learner profiles, internal decisions, runtime logs, local databases, generated artifacts, credentials, environments, and scanner output are excluded.
- Demonstration credentials are requested at runtime through environment-variable names; no password value is included.
- Synthetic accounts use the reserved `.test` domain.

## Results

- Release-boundary scanner: PASS with zero findings and zero blocking findings.
- AI security current-file scan: zero critical and zero high findings.
- AI security rules-check: PASS with zero blocking findings.
- AI security export-gate: PASS with zero blocking findings.
- Canonical Git history was not copied into this candidate, so the private decision log, personal author addresses, and autosave identity are absent.

## Medium-Finding Review

The final AI security scan reported 57 medium heuristic indicators. They were reviewed by category:

- 30 `credential_terms` indicators are password, session, CSRF, invitation, and integrity-control names in source, templates, documentation, and negative tests. They contain no credential values.
- 10 `agent_permission` indicators are ordinary invitation, confirmation, approval, or license-permission wording. One is the standard MIT License permission grant. They do not grant an agent or administrator additional runtime authority.
- 8 `install_hook` indicators are ordinary installation, delivery, or workflow wording. The candidate contains no package-manager lifecycle hook.
- 5 `shell_command` indicators are documented local setup or verification commands.
- 3 `process_spawn` indicators are bounded tests for offline subprocess behavior and renderer execution.
- 1 `package_runner` indicator is the pinned requirements installation command governed by `dependency-allowlist.md`.

No medium indicator expands the public allowlist, embeds a secret value, enables publication, or creates a deployment path.

The detailed machine reports and final candidate digest remain outside the public tree. Any later critical or high finding must block publication until repaired or explicitly classified with evidence.
