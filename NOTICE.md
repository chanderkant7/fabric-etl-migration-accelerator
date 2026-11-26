# Third-Party Open Source Software Notice

This Microsoft internal IP asset incorporates unmodified open-source libraries installed via standard package registries (`PyPI` and `npm`) at build/runtime. No open-source source code is vendored or modified in this repository.

## Runtime Dependencies

### Python (`pyproject.toml`)
- **defusedxml** — Python Software Foundation License 2.0
- **fastapi** — MIT License
- **jinja2** — BSD-3-Clause License
- **pydantic** — MIT License
- **pyjwt[crypto]** (`PyJWT`, `cryptography`) — MIT License / Apache-2.0 OR BSD-3-Clause
- **python-dotenv** — BSD-3-Clause License
- **python-multipart** — Apache License 2.0
- **typer** — MIT License
- **uvicorn** — BSD-3-Clause License

### Vendored Browser Library (`src/accelerator/ui/static/`)
- **htmx** (`htmx.min.js` v2.0.4) — BSD-2-Clause License

Refer to `pyproject.toml` (and `pip freeze` in the active environment) for transitive dependency versions and to `https://aka.ms/ospo` for Microsoft Open Source Programs Office guidance.
