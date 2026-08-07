#!/usr/bin/env bash
#
# Build and verify a Relay release bundle (sdist + wheel) and stage it in
# release/relay-<version>/. Stops before tagging/publishing.
#
# Usage:
#   ./release.sh [branch]        # default branch: master
#
# Env overrides:
#   RELAY_SKIP_SMOKE=1           # skip the fresh-install + /health smoke
#
# The script NEVER tags, pushes, or publishes. Those are manual steps in
# RELEASE.md.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRANCH="${1:-master}"

step()  { printf "\n==== %s ====\n" "$*"; }
die()   { printf "ERROR: %s\n" "$*" >&2; exit 1; }

cd "$ROOT"

# ---------------------------------------------------------------------------
# 1. Preflight
# ---------------------------------------------------------------------------
step "Preflight"

[ -f "app/__version__.py" ] || die "app/__version__.py not found; run from the repository root."

if [ -n "$(git status --porcelain)" ]; then
    git status --porcelain
    die "Working tree is not clean. Commit or stash changes before releasing."
fi

CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
[ "$CURRENT_BRANCH" != "HEAD" ] || die "Detached HEAD; check out the release branch first."
[ "$CURRENT_BRANCH" = "$BRANCH" ] || die "On branch '$CURRENT_BRANCH'; expected '$BRANCH' (pass branch as argument to override)."

COMMIT_FULL="$(git rev-parse HEAD)"
COMMIT_SHORT="$(git rev-parse --short HEAD)"
echo "branch : $CURRENT_BRANCH"
echo "commit : $COMMIT_FULL"

PYTHON="python"
if [ -x ".venv/bin/python" ]; then PYTHON=".venv/bin/python"; fi
echo "python : $("$PYTHON" --version 2>&1)"

# ---------------------------------------------------------------------------
# 2. Version
# ---------------------------------------------------------------------------
step "Version"

VERSION="$(sed -n 's/__version__ *= *"\([^"]*\)"/\1/p' app/__version__.py)"
[ -n "$VERSION" ] || die "Could not parse version from app/__version__.py"
case "$VERSION" in
    [0-9]*[0-9A-Za-z.\-]*) ;;
    *) die "Version '$VERSION' is not a valid PEP 440 string." ;;
esac
echo "version : $VERSION"

# ---------------------------------------------------------------------------
# 3. Clean build
# ---------------------------------------------------------------------------
step "Clean build"

rm -rf build dist
rm -rf ./*.egg-info

"$PYTHON" -m build --sdist --wheel --outdir dist

WHL="$(ls dist/relay-$VERSION-*.whl)"
SDIST="$(ls dist/relay-$VERSION.tar.gz)"
echo "wheel : $(basename "$WHL") ($(wc -c <"$WHL" | tr -d ' ') bytes)"
echo "sdist : $(basename "$SDIST") ($(wc -c <"$SDIST" | tr -d ' ') bytes)"

# ---------------------------------------------------------------------------
# 4. Checksums
# ---------------------------------------------------------------------------
step "Checksums"

WHEEL_HASH="$(sha256sum "$WHL" | awk '{print $1}')"
SDIST_HASH="$(sha256sum "$SDIST" | awk '{print $1}')"
echo "$WHEEL_HASH  $(basename "$WHL")"
echo "$SDIST_HASH  $(basename "$SDIST")"
echo "SHA256SUMS written to the release bundle in the Bundle step."

# ---------------------------------------------------------------------------
# 5. Packaging verification
# ---------------------------------------------------------------------------
step "Packaging verification (tests/test_packaging.py)"

"$PYTHON" -m pytest tests/test_packaging.py -q

# ---------------------------------------------------------------------------
# 6. Fresh-install smoke (installed wheel, not source)
# ---------------------------------------------------------------------------
if [ "${RELAY_SKIP_SMOKE:-0}" = "1" ]; then
    echo "Skipping fresh-install smoke (RELAY_SKIP_SMOKE=1)."
else
    step "Fresh-install smoke"

    TMP="$(mktemp -d -t relay-release-smoke-XXXXXX)"
    trap 'rm -rf "$TMP"' EXIT

    "$PYTHON" -m venv "$TMP/venv"
    "$TMP/venv/bin/python" -m pip install --quiet --upgrade pip
    "$TMP/venv/bin/python" -m pip install --quiet --no-input "$WHL"

    INSTALLED_VERSION="$("$TMP/venv/bin/relay" --version)"
    echo "relay --version : $INSTALLED_VERSION"
    case "$INSTALLED_VERSION" in
        *"$VERSION"*) ;;
        *) die "Installed CLI version '$INSTALLED_VERSION' does not match source version '$VERSION'." ;;
    esac

    "$TMP/venv/bin/relay" --help >/dev/null
    [ "$?" -eq 0 ] || die "Installed 'relay --help' failed."

    PORT=$(( (RANDOM % 10000) + 18000 ))
    export RELAY_PORT="$PORT"
    export RELAY_STATE_DIR="$TMP/state"
    export PERSISTENCE_ENABLED=false

    "$TMP/venv/bin/relay" serve >"$TMP/serve.out.log" 2>"$TMP/serve.err.log" &
    PROC=$!

    HEALTHY=0
    for i in $(seq 1 60); do
        sleep 1
        if ! kill -0 "$PROC" 2>/dev/null; then
            cat "$TMP/serve.err.log"
            die "Installed 'relay serve' exited early."
        fi
        if BODY="$(curl -sS -m 2 "http://127.0.0.1:$PORT/health" 2>/dev/null)" &&
           curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/health" 2>/dev/null | grep -q '^200'; then
            HEALTHY=1
            echo "GET /health -> 200 $BODY"
            break
        fi
    done

    kill "$PROC" 2>/dev/null || true
    wait "$PROC" 2>/dev/null || true
    unset RELAY_PORT RELAY_STATE_DIR PERSISTENCE_ENABLED

    [ "$HEALTHY" = "1" ] || {
        cat "$TMP/serve.err.log"
        die "Installed 'relay serve' /health never returned 200 within 60s."
    }
fi

# ---------------------------------------------------------------------------
# 7. Bundle
# ---------------------------------------------------------------------------
step "Bundle"

BUNDLE="$ROOT/release/relay-$VERSION"
rm -rf "$BUNDLE"
mkdir -p "$BUNDLE"

cp "$WHL" "$SDIST" "$BUNDLE/"

{
    printf '%s  %s\n' "$WHEEL_HASH" "$(basename "$WHL")"
    printf '%s  %s\n' "$SDIST_HASH" "$(basename "$SDIST")"
} > "$BUNDLE/SHA256SUMS"

for doc in \
    README.md RELEASE.md CHANGELOG.md KNOWN-ISSUES.md \
    TEST_REPORT_TEMPLATE.md BUG_REPORT_TEMPLATE.md \
    docs/pre-release-checklist.md docs/post-install-verification.md \
    docs/known-limitations.md docs/release-decisions.md \
    docs/rollback-procedure.md; do
    if [ -f "$doc" ]; then cp "$doc" "$BUNDLE/"; fi
done

{
    printf '# Relay %s release bundle\n\n' "$VERSION"
    printf 'Generated:  %s\n' "$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
    printf 'Branch:     %s\n' "$CURRENT_BRANCH"
    printf 'Commit:     %s\n' "$COMMIT_FULL"
    printf 'Wheel:      %s (SHA256 %s)\n' "$(basename "$WHL")" "$WHEEL_HASH"
    printf 'Sdist:      %s (SHA256 %s)\n' "$(basename "$SDIST")" "$SDIST_HASH"
    printf '\nThis bundle is READY FOR REVIEW. Tagging, pushing, and publishing\n'
    printf 'are manual steps described in RELEASE.md.\n'
} > "$BUNDLE/MANIFEST.txt"

# ---------------------------------------------------------------------------
# 8. Summary
# ---------------------------------------------------------------------------
step "Done"
echo "Release bundle : $BUNDLE"
echo "Version        : $VERSION"
echo "Branch         : $CURRENT_BRANCH"
echo "Commit         : $COMMIT_FULL"
echo ""
echo "NEXT (manual, not performed): tag 'v$VERSION', push the tag, publish to PyPI."
