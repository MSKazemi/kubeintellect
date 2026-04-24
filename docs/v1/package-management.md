# Package Management with `uv`

This project uses [`uv`](https://docs.astral.sh/uv/) as its Python package manager.
It replaces `pip` + `requirements.txt` with `pyproject.toml` + `uv.lock` for reproducible, fast builds.

---

## Prerequisites

Install `uv` on your machine:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Verify:

```bash
uv --version
```

---

## Project Structure

```
KubeIntellect/
├── pyproject.toml      # dependency declarations (edit this, not requirements.txt)
├── uv.lock             # auto-generated lock file — commit this, never edit manually
├── .python-version     # pins Python version (3.12)
└── .venv/              # local virtual environment — gitignored
```

---

## First-Time Setup (new machine / fresh clone)

```bash
# Install all deps (prod + dev) and create .venv automatically
uv sync
```

That's it. `uv` reads `pyproject.toml` and `uv.lock`, creates `.venv/`, and installs everything.

---

## Daily Commands

### Activate the virtual environment (optional)

```bash
source .venv/bin/activate   # Linux/macOS
.venv\Scripts\activate      # Windows
```

Or skip activation entirely and prefix commands with `uv run`:

```bash
uv run pytest
uv run uvicorn app.main:app --reload
```

### Add a production dependency

```bash
uv add fastapi
uv add "langchain>=0.3"
```

### Add a development-only dependency

```bash
uv add --dev pytest
uv add --dev ruff
```

### Remove a dependency

```bash
uv remove requests
```

### Upgrade a specific package

```bash
uv lock --upgrade-package langchain
uv sync
```

### Upgrade all packages (within declared version bounds)

```bash
uv lock --upgrade
uv sync
```

---

## Dependency Groups

Dependencies are split into groups in `pyproject.toml`:

| Group | Purpose | Installed in Docker prod? |
|-------|---------|--------------------------|
| `dependencies` | Production runtime | Yes |
| `dev` | Testing, linting, local tools | No |

Install only production deps:

```bash
uv sync --no-dev
```

Install everything including dev:

```bash
uv sync
```

---

## Docker

The `Dockerfile` uses `uv` with these important flags:

```dockerfile
RUN uv sync --frozen --no-dev --no-install-project
```

| Flag | Meaning |
|------|---------|
| `--frozen` | Fail if `uv.lock` is out of sync with `pyproject.toml` — catches drift in CI |
| `--no-dev` | Skip dev dependencies (`pytest` etc.) — keeps the image lean |
| `--no-install-project` | Don't install the project itself, only its dependencies (faster layer cache) |

To rebuild after changing dependencies:

```bash
docker build -t kubeintellect .
```

---

## CI / Production Rules

- Always commit `uv.lock` to git. It is the single source of truth for exact resolved versions.
- Never edit `uv.lock` by hand. Let `uv lock` regenerate it.
- Never pin with `==` in `pyproject.toml` — use `>=` floors and let the lock file handle exact pinning.
- The Docker build uses `--frozen`, so if you change `pyproject.toml` without running `uv lock`, the build will fail intentionally.

---

## Troubleshooting

### Lock file is out of date

```bash
uv lock
uv sync
```

### Dependency conflict

```bash
uv lock --upgrade   # try re-resolving everything
```

### Clean reinstall

```bash
rm -rf .venv
uv sync
```

### Check what is installed

```bash
uv pip list
```

### Show dependency tree

```bash
uv tree
```
