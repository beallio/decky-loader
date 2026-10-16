#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd -- "$DIR/../.." && pwd)"
RUST_DIR="$REPO/backend-rust"
: "${RUSTSEC_ADVISORY_DB:?Set RUSTSEC_ADVISORY_DB to a fresh clone of https://github.com/RustSec/advisory-db}"

for tool in cargo cargo-audit cargo-deny cargo-machete git; do
    command -v "$tool" >/dev/null || { printf 'Required tool is missing: %s\n' "$tool" >&2; exit 2; }
done

origin="$(git -C "$RUSTSEC_ADVISORY_DB" remote get-url origin)"
case "${origin,,}" in
    https://github.com/rustsec/advisory-db|https://github.com/rustsec/advisory-db.git) ;;
    *) printf 'The advisory database must come from the official RustSec repository.\n' >&2; exit 2 ;;
esac

printf 'RustSec database revision: '
git -C "$RUSTSEC_ADVISORY_DB" rev-parse HEAD
cargo audit --version
cargo deny --version
cargo machete --version

# Fetch locked sources for provenance and dependency inspection without building them.
cargo fetch --locked --manifest-path "$RUST_DIR/Cargo.toml"
failed=0
check() {
    printf '\nChecking: %s\n' "$1"
    shift
    if "$@"; then
        return 0
    else
        failed=1
    fi
}

check 'RustSec vulnerabilities, unmaintained crates, and warnings' \
    cargo audit --file "$RUST_DIR/Cargo.lock" --db "$RUSTSEC_ADVISORY_DB" --no-fetch --deny warnings
check 'Source provenance, wildcard versions, and duplicate versions' \
    cargo deny --locked --offline --manifest-path "$RUST_DIR/Cargo.toml" --config "$RUST_DIR/deny.toml" \
        check sources bans --show-stats
check 'Unused direct dependencies' cargo machete "$RUST_DIR"

# License findings remain visible, but are not a gate for this private experiment.
printf '\nLicense inventory (informational, not a validation gate)\n'
if ! cargo deny --locked --offline --manifest-path "$RUST_DIR/Cargo.toml" --config "$RUST_DIR/deny.toml" \
    check licenses --show-stats; then
    printf 'License findings recorded; continuing under the private-test policy.\n'
fi
exit "$failed"
