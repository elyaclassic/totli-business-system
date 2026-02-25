# AGENTS.md

## Cursor Cloud specific instructions

### Overview

TOTLI HOLVA is a monolithic Python/FastAPI business management system (ERP) for a confectionery manufacturer. It uses SQLite by default (file: `totli_holva.db`), server-side rendered HTML (Jinja2 + Bootstrap 5), and runs as a single process with no external service dependencies.

### Running the application

- **Dev server with hot-reload:** `PYTHONPATH=/workspace uvicorn main:app --host 0.0.0.0 --port 8080 --reload`
- **Without reload:** `PYTHONPATH=/workspace python3 main.py` (runs on port 8080)
- **Seed database:** `PYTHONPATH=/workspace python3 init_data.py` (creates admin user `admin`/`admin123` and sample data)
- The SQLite database file (`totli_holva.db`) is auto-created on first run; no migrations are required for a fresh start.

### Testing

- **Run smoke tests:** `PYTHONPATH=/workspace pytest tests/test_smoke.py -v`
- `PYTHONPATH=/workspace` is required because there is no `pyproject.toml`, `setup.cfg`, or `conftest.py` at the project root to configure the Python path.

### Linting

No dedicated linter is configured in this project. There is no `pyproject.toml`, `setup.cfg`, or `Makefile` with lint targets.

### Gotchas

- `python` is not available on this VM; use `python3` instead.
- Installed packages go to `~/.local/lib/python3.12/site-packages` and scripts to `~/.local/bin`. Ensure `~/.local/bin` is on `PATH` for tools like `uvicorn`, `pytest`, and `alembic`.
- The `main.py` file is very large (~10,900 lines). Most route logic lives inline in this file, with some routes split into `app/routes/`.
- The `pyodbc` dependency is Windows-only (marker: `platform_system=="Windows"`) and is skipped on Linux.
