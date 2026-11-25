# Implementation Plan: Strict Per-Track Upstream `@decky/ui` Version Check

## Problem Definition

Issue [#14](https://github.com/beallio/decky-loader/issues/14) (`Upstream decky-loader locks @decky/ui 5.0.0`) remains open even though PR #13 moved this fork to upstream `v4.0.0-pre1`. Two issues cause this:

1. `.github/workflows/upstream-ui.yml` parses `frontend/pnpm-lock.yaml` with `grep -oP "^  '@decky/ui@\K[^']+(?=':)" | sort -u`. Because the fork's dual-client setup intentionally locks `@decky/ui` (types), `@decky/ui-v5` (Steam Beta / ESM runtime), and `@decky/ui-legacy` (Steam Stable / Webpack runtime), `ours` becomes a multiline string (`4.16.4\n5.0.0\n5.0.1`), breaking both the Markdown table and the `[[ "$main" == "$ours" && "$released" == "$ours" ]]` comparison.
2. `@decky/ui-v5` in `frontend/package.json` is pinned to `5.0.1` while `@decky/ui` and upstream `main` (`v4.0.0-pre1`) lock `5.0.0`. Both `5.0.1` and `5.0.2` contain only TypeScript type fixes (zero runtime JavaScript changes), so aligning `@decky/ui-v5` to `5.0.0` gives identical plugin runtime behavior while restoring strict parity with upstream `main`.

## Architecture Overview

- **Checker script (`scripts/runtime/upstream_ui_check.py`):** Pure Python stdlib script (compatible with Python 3.10+ on `ubuntu-22.04` runners and pinned CPython 3.14) that parses `pnpm-lock.yaml` `importers['.'].dependencies`, performs strict exact-equality checks per track, prints a structured JSON report, and optionally writes Markdown for the tracking issue.
- **Workflow (`.github/workflows/upstream-ui.yml`):** Fetches upstream `main` and latest release `frontend/pnpm-lock.yaml`, latest `decky-frontend-lib` release tag, and recent upstream lockfile commits, then delegates parsing, strict comparison, and Markdown formatting to `scripts/runtime/upstream_ui_check.py`.
- **Frontend lockfile alignment (`frontend/package.json`, `frontend/pnpm-lock.yaml`):** Pin `@decky/ui-v5` to `npm:@decky/ui@5.0.0` so both `@decky/ui` and `@decky/ui-v5` strictly match upstream `main` (`5.0.0`) while `@decky/ui-legacy` strictly matches upstream `v3.3.1` (`4.16.4`), eliminating the duplicate `5.0.1` entry.
- **Documentation (`docs/development.md`):** Update the UI library table (`@decky/ui-v5` `5.0.0`) and document the strict per-track workflow behavior.

## Core Data Structures

- `LockedUiVersions`: parsed dependency mapping from `importers['.'].dependencies` for `@decky/ui` (required) and optional `@decky/ui-v5`, `@decky/ui-legacy` (with `npm:@decky/ui@` / `@decky/ui@` alias prefixes and peer suffixes stripped).
- Comparison report dict:
  - `mode`: `"dual-client"` or `"single-client"`
  - `ours`: `{ "@decky/ui": str, ... }`
  - `upstream_main`: `str`
  - `upstream_release_tag`: `str`
  - `upstream_release`: `str`
  - `library_release`: `str`
  - `in_sync`: `bool` (true only when every required track strictly equals its upstream counterpart)
  - `mismatches`: `list[str]`
  - `issue_title`: `str`
  - `close_comment`: `str`

## Public Interfaces

- CLI: `python3 scripts/runtime/upstream_ui_check.py --ours-lock PATH --upstream-main-lock PATH --upstream-release-lock PATH --upstream-release-tag TAG --library-release TAG [--repo OWNER/REPO] [--commits-json PATH] [--markdown PATH]`
- Library functions imported by unit tests:
  - `parse_locked_ui_versions(lockfile_text: str) -> dict[str, str]`
  - `compare_ui_versions(ours: dict[str, str], upstream_main: str, upstream_release_tag: str, upstream_release: str, library_release: str) -> dict`
  - `render_issue_markdown(report: dict, repo: str, recent_commits: list[dict] | None = None) -> str`

## Dependency Requirements

- Zero new Python, Node.js, or Rust dependencies.
- `frontend/package.json`: change `@decky/ui-v5` from `npm:@decky/ui@5.0.1` to `npm:@decky/ui@5.0.0` and regenerate `frontend/pnpm-lock.yaml` with pinned `pnpm` (`10.33.0`).

## Testing Strategy

1. Strict TDD (Red → Green → Refactor): add `tests/runtime/test_upstream_ui_check.py` first covering:
   - Parsing single-client and dual-client `pnpm-lock.yaml` (including aliased `npm:@decky/ui@...` versions, quoted/unquoted values, peer suffixes, and missing `@decky/ui` errors).
   - Strict dual-client comparison: exact match passes; any mismatch in `@decky/ui`, `@decky/ui-v5`, `@decky/ui-legacy`, upstream `main`, upstream stable release, or incomplete alias pair fails with descriptive mismatch messages.
   - Strict single-client comparison fallback once `@decky/ui-legacy` / `@decky/ui-v5` are removed.
   - Clean single-line Markdown table rendering (no embedded newlines in cells) and accurate issue titles.
2. Full local checks:
   - `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests/runtime -p 'test_*.py'`
   - `UV_NO_CONFIG=1 uv run --project backend --no-sync ruff check .`
   - `pnpm --dir frontend install --frozen-lockfile && pnpm --dir frontend run lint && pnpm --dir frontend run typecheck && pnpm --dir frontend run build`
   - `node --test tests/frontend/*.test.mts`
   - Live dry-run of `scripts/runtime/upstream_ui_check.py` against current `frontend/pnpm-lock.yaml` and upstream `main` / `v3.3.1` lockfiles.
