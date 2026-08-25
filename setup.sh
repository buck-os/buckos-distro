#!/usr/bin/env bash
# Install the open-source buck2 and write a machine-local config.
#
# Deliberately minimal: this repo builds distro packages with each
# flavor's own tooling (rpmbuild, dpkg-buildpackage), so the only thing
# that has to be installed is buck2 itself plus whatever the flavor's
# native driver needs.

set -euo pipefail

BINDIR="${BINDIR:-$HOME/.local/bin}"
BUCK2_VERSION="${BUCK2_VERSION:-latest}"

log() { printf '==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }

case "$(uname -m)" in
    x86_64)  arch=x86_64 ;;
    aarch64|arm64) arch=aarch64 ;;
    *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

# ── buck2 ────────────────────────────────────────────────────────────

install_buck2() {
    if command -v buck2 >/dev/null 2>&1 && buck2 --version >/dev/null 2>&1; then
        log "buck2 already installed: $(buck2 --version)"
        return
    fi

    local url="https://github.com/facebook/buck2/releases/download/${BUCK2_VERSION}/buck2-${arch}-unknown-linux-gnu.zst"
    log "installing buck2 from ${url}"
    mkdir -p "$BINDIR"
    curl -fsSL "$url" | zstd -d > "$BINDIR/buck2"
    chmod +x "$BINDIR/buck2"

    case ":$PATH:" in
        *":$BINDIR:"*) ;;
        *) warn "$BINDIR is not on PATH" ;;
    esac
}

# ── flavor prerequisites ─────────────────────────────────────────────
#
# Only checked, never installed: which packages provide these differs per
# host distro, and silently installing system packages is not this
# script's business.

check_flavor_tools() {
    local flavor="$1" missing=()

    case "$flavor" in
        fedora)
            for tool in rpm rpm2cpio cpio rpmbuild; do
                command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
            done
            ;;
        ubuntu)
            for tool in dpkg-source dpkg-buildpackage dpkg-deb; do
                command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
            done
            ;;
    esac

    # Either isolation backend is enough -- tools/_isolation.py picks bwrap
    # when present and otherwise drives an unprivileged user namespace with
    # util-linux `unshare`, at equivalent hermeticity.  Warning about a
    # missing bwrap alone would tell most hosts the hermetic path is
    # unavailable when it is not.
    if ! command -v bwrap >/dev/null 2>&1 && ! command -v unshare >/dev/null 2>&1; then
        warn "neither bwrap nor unshare found; only buildroot = host will work (non-hermetic, local-only)"
    fi

    # unshare needs a subordinate id range to map more than the one uid
    # --map-root-user gives it, and rpm needs several to install packages
    # owned by non-root users.  Missing entries fail inside newuidmap, well
    # away from anything that mentions them.
    if ! command -v bwrap >/dev/null 2>&1; then
        if ! grep -q "^$(id -un):" /etc/subuid 2>/dev/null; then
            warn "no /etc/subuid range for $(id -un); the unshare backend will fail to map ids"
        fi
    fi

    if [[ ${#missing[@]} -gt 0 ]]; then
        warn "flavor '$flavor' needs: ${missing[*]}"
    else
        log "flavor '$flavor' prerequisites present"
    fi
}

# ── machine-local config ─────────────────────────────────────────────

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

# ── main ─────────────────────────────────────────────────────────────

install_buck2
write_local_config

flavor="$(sed -n 's/^ *flavor *= *//p' .buckconfig | head -1)"
check_flavor_tools "${flavor:-fedora}"

log "done.  Try:  buck2 build //tests:hello"
