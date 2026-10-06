# Public Export Manifest

Review date: 2026-10-07

Source snapshot: `fc4b1dd0a6123736f01eded908c2b11599a7bf05`

Public repository: <https://github.com/jiarong0423/service-requirement-blueprint>

Export decision: published after the learner's explicit authorization on 2026-10-07; later updates remain subject to the same allowlist and release gates

## Allowed Paths

| Path | Classification | Reason |
| --- | --- | --- |
| `.gitattributes` | public configuration | Normalizes tracked text files. |
| `.gitignore` | public configuration | Excludes local credentials, runtime data, generated case artifacts, caches, and learner profiles. |
| `README.md` | public documentation | Contains factual setup, operation, scope, and verification instructions. |
| `requirements.txt` | public dependency manifest | Pins the complete Python environment used by the PoC. |
| `app/**` | public source | Flask application, schema, templates, static assets, security helpers, and workflows. |
| `assets/fonts/**` | public third-party asset | Noto Sans TC font files plus their OFL license and provenance record. |
| `migrations/**` | public source | Ordered SQLite schema migrations required by the application. |
| `tests/**` | public verification | Deterministic functional, authorization, integrity, recovery, and evidence-export tests. |
| `tools/**` | public tooling | Offline font-preflight and planning-document rendering helpers. |
| `devpost/scope.md` | public planning evidence | Required approved Scope produced through the Devpost Learn workflow. |
| `devpost/prd.md` | public planning evidence | Required approved product requirements produced through the Devpost Learn workflow. |
| `devpost/spec.md` | public planning evidence | Required approved technical specification produced through the Devpost Learn workflow. |
| `devpost/checklist.md` | public build evidence | Approved build slices, verification results, and learner gates without private profile content. |
| `devpost/scope.html` | public review companion | Offline visual rendering of the approved Scope. |
| `devpost/prd.html` | public review companion | Offline visual rendering of the approved PRD. |
| `devpost/spec.html` | public review companion | Offline visual rendering of the approved Spec. |
| `devpost/app-map.html` | public learning artifact | Offline map of the finished code path and verification evidence. |
| `devpost/demo-guide.md` | public technical guide | Factual local demonstration steps; it is not submission narration. |
| `devpost/ship-evidence.md` | public factual evidence | Records verified build facts and unresolved publication actions. |
| `PUBLIC_EXPORT_MANIFEST.md` | public governance | Defines this allowlist and exclusion boundary. |
| `SECURITY.md` | public security boundary | Documents local secret ownership, rotation, and responsible disclosure. |
| `SECURITY_THREAT_MODEL.md` | public security design evidence | Describes assets, trust boundaries, threats, and controls. |
| `PACKAGE_REPUTATION_EVIDENCE.md` | public dependency evidence | Records local package review scope and remaining limitations. |
| `dependency-allowlist.md` | public dependency control | Restricts package-runner use to the documented pinned requirements install. |
| `CODE_SECURITY_EVIDENCE.md` | public code-review evidence | Records the security-focused verification performed for this snapshot. |
| `SECRET_SCAN_EVIDENCE.md` | public secret-scan evidence | Records candidate-only current-file and history-boundary checks. |
| `LICENSE` | public license | MIT License selected by the learner for original project source and documentation. |

## Explicitly Excluded

- The canonical repository's `.git/` directory and all prior commit history.
- `.agents/**`; these local copies of the curriculum are not needed to run or judge the submission and are excluded from the sanitized snapshot.
- `devpost/learner-profile.md` and `devpost/learner-profile.zh-TW.md`.
- `logs/**`, including internal decision and runtime records.
- `instance/**`, SQLite databases and sidecars, local secret keys, sessions, invitations, generated JSON, PDF, SVG, ZIP, delivery records, and demonstration case data.
- `.env`, `.env.*`, credential files, private keys, tokens, browser state, and local authentication material.
- `.venv/**`, Python bytecode, test caches, coverage reports, build output, and editor or operating-system metadata.
- `docs/ai-generated/**`, screenshots, temporary review exports, scanner reports, and isolation output.
- Local absolute paths, private workspace metadata, personal email addresses, and autosave author identities from the canonical Git history.

## Rights Classification

- Application source, tests, planning documents, diagrams, and factual technical guides were created for this project during the submission period.
- Python packages are referenced as dependencies and are not redistributed in the repository.
- The bundled Noto Sans TC font files are redistributed under the included SIL Open Font License in `assets/fonts/OFL.txt`; provenance is recorded in `assets/fonts/SOURCE.json`.
- Original project source and documentation use the root MIT License selected on 2026-10-07.

## Required Verification

- Confirm that the candidate contains only the paths listed above.
- Run the complete test suite from the candidate root using the pinned local environment.
- Confirm that only the command in `dependency-allowlist.md` is used to install dependencies.
- Run the local release-boundary scanner and AI security export gate against the candidate root.
- Review all scanner high findings by path and rule without exposing matched values.
- Confirm that `LICENSE` exists and is visible at repository root.
- Confirm that the learner's explicit authorization covers the repository name, public visibility, and exact sanitized candidate before each publication action.
