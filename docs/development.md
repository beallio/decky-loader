# Development

This guide covers the source layout, local checks, complete runtime builds, and isolated validation. For installation and everyday use, see the [README](../README.md). Plugin authors can start with the [plugin template](https://github.com/SteamDeckHomebrew/decky-plugin-template) and the [development wiki](https://wiki.deckbrew.xyz/en/loader-dev/development).

## Repository layout

| Path | Purpose |
| --- | --- |
| `backend-rust/src/` | Rust backend and plugin supervisor. |
| `backend-rust/native-json/` | Native JSON encoder crate. |
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

Use the exact Rust, Node.js, pnpm, CPython, Poetry, and PyInstaller versions in [build-config.json](../backend-rust/build-config.json) for a complete runtime build. The builder rejects a different Rust, Node.js, or pnpm version. It uses the committed Cargo, Poetry, and pnpm lockfiles.

For Rust work in OMP, install `rust-analyzer` and the standard-library source for the pinned Rust toolchain, then configure the local editor to use `backend-rust`. Agent configuration and state, including `.omp/`, `.codex/`, and `.agents/`, are local files rather than project source. Add these directories to `.git/info/exclude` in each checkout; Git exclude rules are not shared between clones. Run Cargo checks explicitly before submitting changes.

## Local checks

Run the runtime Python regressions from the repository root:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests/runtime -p 'test_*.py'
```

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

The output directory must not already exist. A successful build produces:

- `bundle/`, containing the complete runtime;
- `decky-loader-runtime-linux-x86_64.zip`, the installable inner ZIP;
- `SHA256SUMS`;
- `build-result.json`.

The manifest records source hashes, tool and package versions, and the Rust release identity. These records support repeatable builds; they do not promise byte-for-byte identical binaries on different hosts. Developer tools and tests are build inputs, not installed runtime payloads.

The Python plugin support code and frontend remain based on `v3.2.10`. Bundle assembly verifies the pinned upstream baseline, records the worker startup changes, and checks packaged helper code against its source. The Rust release identity is separate; `dev` builds do not offer updates. CI fetches the upstream SDK tag and verifies its pinned commit and source hashes, so a fork does not need its own copy of that tag.

### Worker compatibility boundaries

Python plugins still run in separate processes. A shared Python parent loads common support code and forks a child for each active plugin. This shares memory without putting every plugin in one interpreter. The compiled JSON encoder handles loader-managed replies and events; plugins keep their normal Python JSON library.

Plugin startup binds selected plugin information and loader-managed imports to the approved directory. That binding stays available until process exit, including shutdown callbacks and failed startup. It does not cover every file or helper program that a plugin opens.

### User service

The installed backend is a `systemd --user` unit for the selected account, `deck` by default. It has no `User=` line and is wanted by `default.target`. `KillMode=mixed` stops the main process first, then any remaining helpers in the unit. The installer keeps its state in `homebrew/services/.decky-user-installer/` and its offline recovery kit in `homebrew/services/.decky-user-deploy/`. It records the user unit directory that the user manager reports and reuses it for later status, update, and rollback commands.

Plugins with the `root` flag are refused with a clear error. Normal install, update, status, and rollback never use sudo. Only the explicit `migrate-legacy-system` and `restore-legacy-system` commands run as root. They keep a protected copy of the old system unit, drop-ins, binary, and restore helper in `/var/lib/decky-loader-runtime/legacy-migrations/<uid>/<unit>/`.

### Native JSON for plugins

`decky.native.dumps(value, *, ensure_ascii=True)` calls the compiled `_decky_native_json` encoder. The module loads the extension only when the function is called, so the SDK still imports in a development environment without it. A missing or incompatible extension raises `ImportError`; there is no Python fallback. This is a fork extension to the pinned `v3.2.10` SDK.

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
