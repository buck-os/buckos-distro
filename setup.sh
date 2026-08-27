#!/usr/bin/env bash
# Install the open-source Buck2 binary and write machine-local configuration.
#
# The script checks host tools but does not install system packages.

set -euo pipefail

BINDIR="${BINDIR:-$HOME/.local/bin}"
BUCK2_VERSION="${BUCK2_VERSION:-latest}"
BUCK2_SOURCE="${BUCK2_SOURCE:-}"

log() { printf '==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }

BUCK2_BIN=""
DOWNLOAD_DIR=""

cleanup() {
    if [[ -n "$DOWNLOAD_DIR" ]]; then
        rm -rf "$DOWNLOAD_DIR"
    fi
}

trap cleanup EXIT

case "$(uname -m)" in
    x86_64)  arch=x86_64 ;;
    aarch64|arm64) arch=aarch64 ;;
    *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

# Buck2

require_command() {
    local name="$1"
    if ! command -v "$name" >/dev/null 2>&1; then
        echo "missing required command: $name" >&2
        exit 1
    fi
}

install_buck2() {
    if command -v buck2 >/dev/null 2>&1 && buck2 --version >/dev/null 2>&1; then
        BUCK2_BIN="$(command -v buck2)"
        log "buck2 already installed: $($BUCK2_BIN --version)"
        return
    fi

    if [[ -x "$BINDIR/buck2" ]] && "$BINDIR/buck2" --version >/dev/null 2>&1; then
        BUCK2_BIN="$BINDIR/buck2"
        log "buck2 already installed: $($BUCK2_BIN --version)"
        return
    fi

    mkdir -p "$BINDIR"

    if [[ -n "$BUCK2_SOURCE" ]]; then
        if [[ ! -f "$BUCK2_SOURCE" || ! -x "$BUCK2_SOURCE" ]]; then
            echo "BUCK2_SOURCE is not an executable file: $BUCK2_SOURCE" >&2
            exit 1
        fi
        log "installing buck2 from ${BUCK2_SOURCE}"
        install -m 0755 "$BUCK2_SOURCE" "$BINDIR/buck2"
    else
        require_command curl
        require_command zstd

        local url
        if [[ "$BUCK2_VERSION" == "latest" ]]; then
            url="https://github.com/facebook/buck2/releases/download/latest/buck2-${arch}-unknown-linux-gnu.zst"
        else
            url="https://github.com/facebook/buck2/releases/download/${BUCK2_VERSION}/buck2-${arch}-unknown-linux-gnu.zst"
        fi
        log "installing buck2 from ${url}"

        DOWNLOAD_DIR="$(mktemp -d)"
        curl --fail --location --retry 5 --retry-all-errors --retry-delay 1 --remove-on-error --silent --show-error --output "$DOWNLOAD_DIR/buck2.zst" "$url"
        zstd -d -q -o "$DOWNLOAD_DIR/buck2" "$DOWNLOAD_DIR/buck2.zst"
        install -m 0755 "$DOWNLOAD_DIR/buck2" "$BINDIR/buck2"
        rm -rf "$DOWNLOAD_DIR"
        DOWNLOAD_DIR=""
    fi

    BUCK2_BIN="$BINDIR/buck2"
    "$BUCK2_BIN" --version >/dev/null

    case ":$PATH:" in
        *":$BINDIR:"*) ;;
        *) warn "$BINDIR is not on PATH; add it before running buck2 directly" ;;
    esac
}

# Flavor prerequisites
#
# Package names differ across host distributions, so these are checked by
# executable name and never installed automatically.

check_flavor_tools() {
    local flavor="$1" missing=()

    case "$flavor" in
        fedora)
            for tool in python3 tar rpm2archive; do
                command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
            done
            ;;
        ubuntu|buckos)
            warn "flavor '$flavor' is declared but not implemented"
            ;;
        *)
            warn "unknown flavor '$flavor'"
            ;;
    esac

    # Either isolation backend is enough. tools/_isolation.py picks bwrap
    # when present and otherwise drives an unprivileged user namespace with
    # util-linux `unshare`, at equivalent hermeticity. Warning about a
    # missing bwrap alone would tell most hosts the hermetic path is
    # unavailable when it is not.
    if ! command -v bwrap >/dev/null 2>&1 && ! command -v unshare >/dev/null 2>&1; then
        warn "neither bwrap nor unshare found; only buildroot = host will work (non-hermetic, local-only)"
    fi

    # unshare needs helpers and subordinate id ranges to preserve non-root
    # ownership from RPM payloads.
    if ! command -v bwrap >/dev/null 2>&1; then
        if ! command -v newuidmap >/dev/null 2>&1 || ! command -v newgidmap >/dev/null 2>&1; then
            warn "newuidmap and newgidmap are required for full unshare mappings"
        elif ! grep -q "^$(id -un):" /etc/subuid 2>/dev/null || ! grep -q "^$(id -un):" /etc/subgid 2>/dev/null; then
            warn "no subordinate uid/gid ranges for $(id -un); RPM ownership mappings will fail"
        fi
    fi

    if [[ ${#missing[@]} -gt 0 ]]; then
        warn "flavor '$flavor' needs: ${missing[*]}"
    else
        log "flavor '$flavor' prerequisites present"
    fi
}

# Machine-local configuration

write_local_config() {
    if [[ -f .buckconfig.local ]]; then
        log ".buckconfig.local exists, leaving it alone"
        return
    fi

    log "writing .buckconfig.local"
    {
        echo "# Machine-local overrides.  Gitignored -- nothing here may be"
        echo "# required for a fresh clone to build."
        echo
        echo "[cell_aliases]"
        echo "  ovr_config = prelude"
        echo
        echo "[buckos]"
        echo "  remote_cache = false"
        echo "  remote_execution = false"

        # Something earlier in PATH may shadow rpmbuild with a wrapper, so
        # pin the real one if it is somewhere other than first.
        local real_rpmbuild
        real_rpmbuild="$(command -v rpmbuild 2>/dev/null || true)"
        if [[ -n "$real_rpmbuild" && "$real_rpmbuild" != "/usr/bin/rpmbuild" && -x /usr/bin/rpmbuild ]]; then
            echo
            echo "# $real_rpmbuild came first on PATH; pinning the real binary."
            echo "[buckos.fedora]"
            echo "  rpmbuild = /usr/bin/rpmbuild"
        fi
    } > .buckconfig.local
}

# Main

install_buck2
mkdir -p prelude
write_local_config

flavor="$(sed -n 's/^ *flavor *= *//p' .buckconfig | head -1)"
check_flavor_tools "${flavor:-fedora}"

log "done. Try: $BUCK2_BIN build //tests:hello"
