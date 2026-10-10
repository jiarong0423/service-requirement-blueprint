# Current Public Export Manifest

Review date: 2026-10-11  
Source snapshot: `9c146334fb384b40b16d837f5f7fd5f3e98d8aa8`
Public repository: <https://github.com/jiarong0423/service-requirement-blueprint>.

## Exact source identity

`public-export-manifest.json` contains the source commit, tree, and byte length/SHA-256 of all **141** source files. It was produced from a clean local Git commit by `tools/build_public_candidate.py`. Private local commit history is not published. The normal public commit is a descendant of the existing public main, and its manifest binds the separate local source identity.

## Allowed source and overlays

Source paths are the exact builder inventory: stable root controls, `app/`, `assets/fonts/`, `migrations/`, `tests/`, four named `tools/` helpers, and explicitly named public `devpost/` planning/demo documents and media diagrams. `demo-capture-guide.md`, the 161.8-second narration/SRT, architecture and six-step PNG/SVG, and Mermaid in README are included. No wildcard permits the internal capture master.

Seven preserved/reviewed public overlays are `SECURITY.md`, `SECURITY_THREAT_MODEL.md`, `dependency-allowlist.md`, `CODE_SECURITY_EVIDENCE.md`, `PACKAGE_REPUTATION_EVIDENCE.md`, `SECRET_SCAN_EVIDENCE.md`, and this document. Two additional public evidence files are `security-evidence/semgrep-rules.json` and `security-evidence/verification.json`. The source manifest itself is generated. These overlays are reviewed separately and are not described as exact canonical source blobs.

## Exclusion and rights boundary

Excluded: learner profile; local secrets and environment files; `instance/`; databases and sidecars; private case JSON/PDF/ZIP; recordings; raw private conversations; internal logs and output; browser profiles; private canonical Git history; environments, caches, and private absolute paths. No broad deletion or private-history rewrite occurs.

Original source/documentation uses the learner-selected MIT License. The bundled Noto Sans TC font files keep their SIL Open Font License and `assets/fonts/SOURCE.json` provenance. No video or third-party Python packages are redistributed here. README PNG diagrams are original project documentation.

## Checks before synchronization

Exact source hashes, current documentation, full extracted-package regression plus focused final-delta tests, isolated loopback start/stop, independent static review, bounded SAST match disposition, redacted Gitleaks, release-boundary scan, and AI security export/rules gates are reviewed. After push, remote commit/tree and every public file must be read back and compared; zero delta closes synchronization. Source release does not certify five new browser journeys, human audiovisual review, cloud deployment, personal eligibility, or a submitted Devpost form.
