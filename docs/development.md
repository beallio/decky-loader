# Development

This guide covers the source layout, local checks, complete runtime builds, and isolated validation. For installation and everyday use, see the [README](../README.md). Plugin authors can start with the [plugin template](https://github.com/SteamDeckHomebrew/decky-plugin-template) and the [development wiki](https://wiki.deckbrew.xyz/en/loader-dev/development).

## Repository layout

| Path | Purpose |
| --- | --- |
| `backend-rust/src/` | Rust backend and plugin supervisor. |
| `backend-rust/native-json/` | Native JSON encoder crate. |
| `backend-rust/decky-plugin/` | SDK for native (Rust) plugin backends, licensed 0BSD. |
| `backend-rust/install.py`, `process_observer.py` | Production installer and process-ownership checks shipped in runtime bundles. |
| `backend-rust/build-config.json`, Cargo files, and `deny.toml` | Pinned build inputs and dependency policy. |
| `backend/` | Python backend, plugin support code, and frozen-worker packaging. |
| `frontend/` | TypeScript interface sources and frontend build configuration. |
| `tests/runtime/` | Installer, process observer, release-integrity, and Python-worker regression tests. |
| `tests/native-json/` | Differential encoder tests that require the compiled extension. |
| `tests/fixtures/runtime/` | Reusable Python and JavaScript plugin fixtures. These are test inputs, not installed plugins. |
| `scripts/runtime/` | Runtime build, bundle assembly, release validation, and dependency-audit tools. |
| `scripts/runtime/validation/` | Maintained compatibility, privileged lifecycle, and VM benchmark tools. |
| `scripts/` | End-user installation and diagnostic scripts. |
| `docs/` | Contributor documentation and README images. |

Keep runtime code, regression tests, and validation drivers in these separate locations. Do not add one-off experiment controllers or captured benchmark output to a source directory. Preserve useful behavior checks as maintained tests; keep disposable campaigns and their evidence outside the checkout.

## Temporary files and generated output

Use `TMPDIR` for disposable builds, downloads, compiler output, profiles, and validation results. If it is unset, use `/var/tmp/omp-1000`; create that directory with mode `0700` if needed. Choose a new output directory for each complete build.

Keep the temporary base path short for worker and socket tests. Linux Unix-domain socket paths include the generated directory names and have a small length limit; a deeply nested `TMPDIR` can exceed it.

The frontend build writes application assets to `backend/decky_loader/static/` and a bundle-size report to `frontend/stats.html`. These files are generated and ignored by Git. Python bytecode caches, Cargo targets, virtual environments, and `node_modules/` are also ignored. Do not commit them. Keep an existing dependency installation or generated application assets when they are needed for a local development launch.

## Tools and editor support

Use the exact Rust, Node.js, pnpm, uv, CPython, and PyInstaller versions in [build-config.json](../backend-rust/build-config.json) for a complete runtime build. The builder rejects a different Rust, Node.js, pnpm, or uv version. It uses the committed Cargo, uv (`backend/uv.lock`), and pnpm lockfiles.

The Python project files `backend/pyproject.toml` and `backend/uv.lock` belong to the fork; upstream uses Poetry. The runtime packages must still have the same versions as upstream's `backend/poetry.lock` at the pinned SDK tag, so plugins see the same libraries as on official Decky. `scripts/runtime/python_parity.py` checks this before every build for the shipped target (the pinned CPython on Linux x86-64): it evaluates the environment markers in both locks and fails on marker syntax it does not know. List a deliberate difference with its reason under `python_divergence` in `build-config.json`.

For Rust work in OMP, install `rust-analyzer` and the standard-library source for the pinned Rust toolchain, then configure the local editor to use `backend-rust`. Agent configuration and state, including `.omp/`, `.codex/`, and `.agents/`, are local files rather than project source. Add these directories to `.git/info/exclude` in each checkout; Git exclude rules are not shared between clones. Run Cargo checks explicitly before submitting changes.

## Local checks

Run the runtime Python regressions from the repository root:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests/runtime -p 'test_*.py'
```

Check the SDK backend types with [ty](https://docs.astral.sh/ty/), pinned in `backend/uv.lock`:

```sh
cd backend && UV_NO_CONFIG=1 uv sync --locked --group typecheck && uv run --no-sync ty check --error-on-warning
```

Check the Python packages against upstream with `python3 scripts/runtime/python_parity.py`.

Lint the fork's own Python with [ruff](https://docs.astral.sh/ruff/), pinned in `backend/uv.lock`, from the repository root. `ruff.toml` lists the files; unchanged and overlaid upstream files are not linted:

```sh
UV_NO_CONFIG=1 uv sync --project backend --locked --only-group lint && uv run --project backend --no-sync ruff check .
```

Run every `uv sync` and `uv lock` for this project with `UV_NO_CONFIG=1`. A personal or system `uv.toml` (for example `exclude-newer`) otherwise changes the lock options, and `--locked` fails in CI. The build and CI set it themselves.

The native JSON tests are separate because they require a compiled extension for the selected Python ABI. The complete runtime builder compiles that extension and runs `tests/native-json/` with the pinned CPython before it packages the worker.

Rust workspace tests also need a Python installation with its shared library and development files. If automatic detection selects an interpreter without `libpython`, set `PYO3_PYTHON` to a suitable installation before running Cargo.

Keep Cargo output outside the checkout, then run the Rust checks with the pinned toolchain selected:

```sh
export CARGO_TARGET_DIR="${TMPDIR:-/var/tmp/omp-1000}/decky-cargo-target"
cargo fmt --manifest-path backend-rust/Cargo.toml --all --check
cargo clippy --manifest-path backend-rust/Cargo.toml --locked --workspace --all-targets -- -D warnings
cargo test --manifest-path backend-rust/Cargo.toml --locked --workspace
```

The dependency audit also requires the pinned `cargo-audit`, `cargo-deny`, and `cargo-machete` versions listed in [the audit workflow](../.github/workflows/runtime-dependencies.yml):

```sh
scripts/runtime/audit.sh
```

Do not weaken the source, dependency-use, security, or license checks to make a build pass.

## Frontend development

Install the locked frontend dependencies and build the interface before testing UI changes:

```sh
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend run build
```

The output is consumed by both Python packaging and the complete Rust bundle build. The complete runtime builder performs this work in its temporary source snapshot; it does not replace the checkout's generated assets.

## Build a complete Rust runtime

Install the GitHub CLI and the pinned tools first. The builder downloads a checksum-verified CPython archive and installs Python dependencies in temporary environments. It does not activate a service or change an installed copy of Decky Loader.

```sh
python3 scripts/runtime/build.py \
  --release-version dev \
  --output "${TMPDIR:-/var/tmp/omp-1000}/decky-runtime"
```

For repeated `dev` builds, add `--cargo-target-dir DIR` to reuse Rust compiler output between runs; CI does this with a cache. The builder refuses this option for a release version, so release builds always compile from a clean directory.

The output directory must not already exist. A successful build produces:

- `bundle/`, containing the complete runtime;
- `decky-loader-runtime-linux-x86_64.zip`, the installable inner ZIP;
- `SHA256SUMS`;
- `build-result.json`.

The manifest records source hashes, tool and package versions, and the Rust release identity. These records support repeatable builds; they do not promise byte-for-byte identical binaries on different hosts. Developer tools and tests are build inputs, not installed runtime payloads.

The Python plugin support code and frontend are based on upstream `v3.3.0` (see `sdk` in [build-config.json](../backend-rust/build-config.json)). Bundle assembly verifies the pinned upstream baseline, records the worker startup changes, and checks packaged helper code against its source. The Rust release identity is separate; `dev` builds do not offer updates. CI fetches the upstream SDK tag and verifies its pinned commit and source hashes, so a fork does not need its own copy of that tag. The installer accepts a bundle on any upstream base when the bundle's recorded base and its worker's reported version agree, so installed loaders can update in-app across a base change.

### Worker compatibility boundaries

Python plugins still run in separate processes. A shared Python parent loads common support code and forks a child for each active plugin. This shares memory without putting every plugin in one interpreter. The compiled JSON encoder handles loader-managed replies and events; plugins keep their normal Python JSON library.

Plugin startup binds selected plugin information and loader-managed imports to the approved directory. That binding stays available until process exit, including shutdown callbacks and failed startup. It does not cover every file or helper program that a plugin opens.

### User service

The installed backend is a `systemd --user` unit for the selected account, `deck` by default. It has no `User=` line and is wanted by `default.target`. `KillMode=mixed` stops the main process first, then any remaining helpers in the unit. The installer keeps its state in `homebrew/services/.decky-user-installer/` and its offline recovery kit in `homebrew/services/.decky-user-deploy/`. It records the user unit directory that the user manager reports and reuses it for later status, update, and rollback commands.

Plugins with the `root` flag run only after root plugin support is enabled; see [Root plugin host](root-plugin-host.md). Normal install, update, status, and rollback never use sudo. Only the explicit `enable-root-plugins`, `disable-root-plugins`, `migrate-legacy-system` and `restore-legacy-system` commands run as root. The legacy commands keep a protected copy of the old system unit, drop-ins, binary, and restore helper in `/var/lib/decky-loader-runtime/legacy-migrations/<uid>/<unit>/`.

### Native JSON for plugins

`decky.native.dumps(value, *, ensure_ascii=True)` calls the compiled `_decky_native_json` encoder. The module loads the extension only when the function is called, so the SDK still imports in a development environment without it. A missing or incompatible extension raises `ImportError`; there is no Python fallback. This is a fork extension to the pinned upstream SDK.

### Native plugin backends

A plugin can ship its own executable instead of `main.py`. Name it in `plugin.json`:

```json
{ "name": "Example", "author": "Me", "flags": [], "backend": "bin/backend" }
```

The path must be relative and stay inside the plugin. The loader opens every part of it without following symbolic links and starts the file only if it is an ELF executable. Build for `x86_64-unknown-linux-musl` (or another fully static target) so that SteamOS library updates cannot break the plugin. The installer gives plugin files mode 0755, so the file stays executable.

The loader does for the executable what the Python worker does before it imports `main.py`: it creates the `settings`, `data` and `logs` folders, sets the same `DECKY_*`, `HOME` and `USER` variables, runs it as the host user in its own process group, and stops it with SIGTERM and then SIGKILL after five seconds. The program listens on the Unix socket named by `DECKY_PLUGIN_SOCKET` and speaks the worker protocol: newline-delimited JSON calls (`type` 0), replies (`type` 1) and events (`type` 2), plus `{"uninstall":true}` before an uninstall stop.

The `decky-plugin` crate implements all of this, including a log file in `DECKY_PLUGIN_LOG_DIR` that is named and pruned like the Python `decky.logger` file. It is licensed 0BSD, so a plugin that links it can use any license. `backend-rust/decky-plugin/examples/native_example.rs` is a complete backend. Frontend code does not change: `callable(name)` and `addEventListener(event, …)` work as they do with Python.

Limits:

- A plugin cannot have both `main.py` and `backend`, and native backends cannot use the `root` flag yet. The loader refuses such plugins.
- Only `loader/call_plugin_method` calls reach a native backend; the legacy `loader/call_legacy_plugin_method` route is refused.
- This is a fork feature. The upstream loader treats a plugin without `main.py` as frontend-only, so its backend calls fail there.
- The loader still needs the packaged Python worker at startup, even when every plugin is native, but it does not start the shared Python launcher for native plugins.

## Validate an owned VM copy

The maintained entry points are:

```sh
python3 scripts/runtime/validation/verify.py --help
python3 scripts/runtime/validation/validate_user.py --help
python3 scripts/runtime/validation/benchmark_vm.py --help
```

Run these tools in a Python 3.11-or-newer environment with the backend's locked dependencies installed. The HTTP/WebSocket verifier imports `aiohttp`; a bare system Python installation may not provide it.

Stage the validation scripts with their documented support inputs and fixtures. Keep captured evidence and disposable controllers in `TMPDIR`, not in the repository. The tools have different isolation and service effects:

- `benchmark_vm.py` creates uniquely owned transient services in private network namespaces. It checks that the normal installation is unchanged and does not stop or reconfigure `plugin_loader.service`.
- `verify.py` is an HTTP/WebSocket client for the supplied `--base` URL. It creates no service or network isolation. It makes plugin calls and reloads a fixture plugin, so point it at an owned test instance rather than treating it as a read-only health check.
- `validate_user.py --verify-only` checks the staged bundle and packaged worker without starting a service.
- `validate_user.py` without `--verify-only` starts a uniquely named temporary user service on a separate port. It does not stop or change `plugin_loader.service`. Run it as the plugin account, not as root.

The benchmark's Python baseline must be the approved Python executable; never pass an installed Rust binary as the Python variant. Its standard workflow expects an installed Python baseline. If the normal installation has already changed to Rust, use an independently verified historical Python copy and a separate reviewed campaign adapter. Do not replace the normal service merely to fit the benchmark's assumptions.

VM results do not establish compatibility with every third-party plugin or replace physical Steam Deck and Steam interface checks. Describe the exact candidate, workload, plugin counts, and measurement limits with each result. Do not infer reduced memory use from process count or from the implementation language.

## Release validation and publication

The `Runtime Builder` workflow runs checks and builds a complete Linux x86-64 artifact. It does not publish a release.

The `Runtime Release` workflow validates the selected source, version, complete file lists, and every payload checksum. Local validation uses `scripts/runtime/release.py`; run its `--help` for the required inputs. Build with the release tag as `--release-version` before validating it. A `dev` bundle is not a release.

The tag selects the channel, and the selected channel must agree with it:

| Tag | Channel | GitHub release |
| --- | --- | --- |
| `vX.Y.Z-rust.N` | stable | latest release |
| `vX.Y.Z-rust.N-rc.N` | preview | prerelease, not latest |

Publication is off by default and requires an explicit `--publish` selection from `refs/heads/main`. An existing tag must resolve to the artifact's exact source commit. Never publish to the official Python release feed.

The [Deck installer](../scripts/install-to-deck.sh) downloads the latest stable fork release by default, the newest preview with `--channel preview`, or one exact tag with `--version`. It checks the ZIP against the release's `.sha256` file. Offline installs use `--bundle ZIP --sha256 HASH --version TAG`.

## Upstream sync

The `Upstream Sync` workflow runs every week and can be started by hand. It runs `scripts/runtime/upstream_check.py`, which compares the pinned SDK tag in [build-config.json](../backend-rust/build-config.json) with the newest upstream tag. Stable tags `vX.Y.Z` always count, and a stable release ranks above its prereleases. Prerelease tags `vX.Y.Z-preN` count only while the pin is itself a prerelease. When a newer tag exists, the workflow opens one issue, `Upstream vX.Y.Z is available`, with the `upstream-sync` label. It updates that issue on later runs. When the fork is up to date, it does nothing.

The report has four parts:

- Each fork overlay file gets a three-way merge: base is the pinned tag, ours is the fork `HEAD`, theirs is the new tag. The result is `clean`, `conflict`, `unchanged-upstream`, or `deleted-upstream`.
- Each removed file shows whether upstream changed it.
- Other changes in `backend/decky_loader` and `frontend` come into the fork as they are.
- Each runtime Python package whose version in the new tag's `poetry.lock` differs from `backend/uv.lock`.

To run the check yourself, use `python3 scripts/runtime/upstream_check.py`. It prints JSON. Give a Git remote or path as an argument to use another source.

To move to a new upstream tag:

1. Fetch the tag: `git fetch --no-tags https://github.com/SteamDeckHomebrew/decky-loader.git refs/tags/vX.Y.Z:refs/tags/vX.Y.Z`.
2. Merge the upstream changes into `backend/decky_loader` and `frontend`. For each overlay file, merge by hand, or run `git merge-file` with the old tag as base. Keep the fork changes. For each removed file, check that the change does not matter, then keep it removed. Keep the fork's `backend/pyproject.toml` and `backend/uv.lock`, and do not bring back `backend/poetry.lock`.
3. In `build-config.json`, set `sdk.tag`, `sdk.commit` (`git rev-parse vX.Y.Z^{commit}`), `sdk.backend_tree` (`git rev-parse vX.Y.Z:backend/decky_loader`), and `sdk.frontend_tree` (`git rev-parse vX.Y.Z:frontend`). Update the base version named in this guide. Remove overlay entries that now equal upstream.
4. Set `version` in `backend/pyproject.toml` to the new tag without the `v`. For each package that the report lists, match upstream: change the dependency range in `pyproject.toml` if upstream changed it, then `UV_NO_CONFIG=1 uv lock --upgrade-package NAME==VERSION` in `backend`. `python3 scripts/runtime/python_parity.py` must pass.
5. Run the local checks and a complete build.
6. Release as `vX.Y.Z-rust.N` (use `-rc.N` for a preview). See [Release validation and publication](#release-validation-and-publication).
