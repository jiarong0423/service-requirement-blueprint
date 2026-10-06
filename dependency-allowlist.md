# Dependency Command Allowlist

Review date: 2026-10-07

## Allowed Command

After creating a project-local Python 3.12 virtual environment and reviewing the exact pins in `requirements.txt`, the only approved package installation command is:

```bash
.venv/bin/python -m pip install -r requirements.txt
```

## Boundary

- Run the command only from the repository root and only with the project-local virtual environment interpreter.
- Do not add, upgrade, or substitute packages during the competition release process.
- Do not run `npx`, `bunx`, `pnpm dlx`, `yarn dlx`, `uv pip`, `poetry add`, or an unpinned direct package install for this project.
- The public repository does not include a virtual environment, downloaded wheel, package cache, or credential.
- `pip check`, the full regression suite, and the fixed-document preflight must pass after installation.
