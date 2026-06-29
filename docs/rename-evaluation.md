# Rename Evaluation

## Decision

Use **StatsTalk** as the user-facing product name and keep `snla` as the internal Python package name.

## Rationale

- SPSS is now optional, so a product name centered on SPSS is misleading.
- `StatsTalk` communicates both the domain and the natural-language interaction model.
- Keeping the `snla` package avoids a wide refactor across imports, tests, scripts, and MCP tool names.
- MCP tool names such as `snla_analyze` remain stable for existing clients.

## What Changed

- Desktop window title: StatsTalk.
- PyInstaller output: `StatsTalk.exe`.
- README and user-facing docs: StatsTalk.
- Internal package: unchanged (`snla`).

## What Should Not Change Without A Dedicated Migration

- `snla/` package directory.
- `from snla...` imports.
- MCP tool prefixes.
- Existing environment variable names such as `SPSS_PATH` and `SPSS_PYTHON_PATH`.

## Future Full Rename Cost

A full package rename would touch imports, tests, scripts, PyInstaller configuration, MCP contracts, and likely user documentation. Treat it as a separate migration with automated import rewriting and a full regression run.
