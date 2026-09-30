# tabularmapper

## Stack

- **Language / Runtime**: Python 3.9+
- **Framework**: FastAPI (optional, install with `[api]` extra); no required web framework
- **Key dependencies**: openpyxl ≥3.1 (Excel .xlsx), rapidfuzz ≥3.0 (fuzzy matching), python-dateutil ≥2.8, xlrd ≥2.0 (legacy .xls)
- **Package manager**: pip / uv (uv.lock present for dev)

## Build approach

<TBD, set by /scope>

## Commands

```bash
# Install (dev, all extras)
uv sync
# or: pip install -e ".[api,redis,postgres,dotenv]"

# Test
pytest -q

# Build wheel + sdist
uv build
# or: python -m build

# Publish
twine upload dist/*
```

## Specs

Stored in `docs/specs/`. Format: `docs/specs/NNNN-title.md`.

## Rules

- Every module uses `from __future__ import annotations`; all public and internal functions are type-annotated
- One module per responsibility: engine.py owns the pipeline, schema.py owns config loading, cli.py and api.py are thin wrappers with no business logic
- `configure()` swaps module-level globals atomically; it is not thread-safe at reconfiguration time — the FastAPI lifespan calls it once at startup
- The LLM matcher must see only the header row and column names, never row data; this is a hard privacy invariant
- Invalid config URLs fall back silently to the built-in bank preset rather than raising
- Bump the version in both `pyproject.toml` and `src/tabularmapper/__init__.py` on every release; they have diverged before

## Agent skills

- [fastapi-python](.agents/skills/fastapi-python/): `mindrally/skills`, FastAPI route and dependency injection conventions
- [python-testing](.agents/skills/python-testing/): `mindrally/skills`, pytest fixture patterns and test organisation
- [python-packaging](.agents/skills/python-packaging/): `wshobson/agents`, PyPI packaging, versioning, and distribution workflow

Declined: anthropics/skills@xlsx

## Context files

- [src/tabularmapper/AGENTS.md](src/tabularmapper/AGENTS.md): source package conventions and non-obvious pipeline invariants

_Drafted by /audit from the repo, worth a quick human pass. Edit freely: once a line stops matching this draft, later runs treat it as curated and will flag rather than overwrite it._
