"""Shared rpm helpers for buckos-distro action scripts.

Everything here shells out to the host/buildroot rpm toolchain.  We never
reimplement rpm semantics -- see SPEC.md section 1.
"""

import os
import shutil
import subprocess
import sys
import tempfile

# Where trees that rpm unpacks are allowed to live.  Deliberately outside
# the project root, and this is not a preference.
#
# Buck2 points TMPDIR at a scratch path under buck-out, which is right for
# almost everything and wrong for these trees, because they contain a
# filename buck2 cannot represent.  systemd-udev ships
#
#   usr/lib/systemd/system/system-systemd\x2dcryptsetup.slice
#
# with a literal backslash -- legal in a filename, legal in rpm, and not
# expressible as a buck2 project-relative path.  When buck2 walks buck-out
# and meets it the build dies with
#
#   Error relativizing: `...system-systemd\x2dcryptsetup.slice;6553f100`
#     is not relative to project root
#
# and it dies in a way that does not stay fixed.  The unrepresentable path
# is recorded in daemon state, so every later invocation fails instantly on
# a file that no longer exists; the only escape is `buck2 kill`, which
# discards the action cache, which re-runs the install, which recreates the
# file.  Deleting the tree does not help and neither does waiting.
#
# Keeping these trees out of the project root breaks that loop at the only
# point where it can be broken: buck2 never sees the path at all.
#
# /var/tmp rather than /tmp because an unpacked image is gigabytes and /tmp
# is a small tmpfs on many systems.  The one property that matters for
# speed is that it share a device with buck-out, which is what lets rpm
# staging hardlink instead of copying half a gigabyte; override
# BUCKOS_SCRATCH_ROOT if that is not true locally.
#
# The tradeoff, stated plainly: a hard kill now litters /var/tmp instead of
# buck-out, and `buck2 clean` will not sweep it.  That is not a regression
# -- buck2 could not delete a path it cannot name either -- but it does
# mean the litter is the user's to remove.
SCRATCH_ROOT_ENV = "BUCKOS_SCRATCH_ROOT"
_DEFAULT_SCRATCH_ROOT = "/var/tmp"


def scratch_dir(prefix):
    """A private scratch directory for a tree Buck must not walk."""
    base = os.environ.get(SCRATCH_ROOT_ENV) or _DEFAULT_SCRATCH_ROOT
    os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(prefix=prefix, dir=base)


def run(cmd, **kwargs):
    """Run a command, echoing it, and fail loudly with its output."""
    printable = " ".join(str(c) for c in cmd)
    print("+ {}".format(printable), file=sys.stderr, flush=True)
    kwargs.setdefault("check", True)
    try:
        return subprocess.run([str(c) for c in cmd], **kwargs)
    except subprocess.CalledProcessError as exc:
        print(
            "command failed (exit {}): {}".format(exc.returncode, printable),
            file=sys.stderr,
        )
        for stream_name in ("stdout", "stderr"):
            stream = getattr(exc, stream_name, None)
            if stream:
                text = stream.decode(errors="replace") if isinstance(stream, bytes) else stream
                print("--- {} ---\n{}".format(stream_name, text), file=sys.stderr)
        raise


def require_tool(name):
    """Resolve a tool on PATH or exit with a clear message."""
    path = shutil.which(name)
    if not path:
        sys.exit(
            "buckos-distro: required tool '{}' not found on PATH.\n"
            "  PATH={}".format(name, os.environ.get("PATH", ""))
        )
    return path


def rpm_eval(expr, rpm="rpm"):
    """Expand an rpm macro expression using the ambient rpm configuration."""
    out = subprocess.run(
        [rpm, "--eval", expr],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def make_dirs_writable(dest):
    """Force owner access on an unpacked rpm tree: u+rwx dirs, u+r files.

    Two distinct problems, both caused by unpacking root-owned payloads as
    an unprivileged user:

      * rpm ships read-only directories -- the `filesystem` package owns
        /usr/lib and /usr/lib64 as dr-xr-xr-x.  Once tar has created
        /usr/lib at 0555, the *next* rpm cannot write into it and the
        assembly dies partway through with a wall of "Permission denied".
        Buck2 then cannot delete the tree on the following build either,
        which surfaces as an opaque cleanup error pointing nowhere near
        this code.

      * rpm ships unreadable files -- /etc/gshadow and /etc/shadow are
        mode 0000, which is correct on a real system because only root
        reads them.  Buck2 hashes every byte of an action's output to key
        its cache, so a file it cannot open fails the build after the
        action has already succeeded.

    So modes in an assembled tree are deliberately not byte-faithful to
    what rpm declares: the owner bits are forced on.  That is the right
    trade here -- this is a build environment to be entered and a Buck
    artifact to be hashed, not a filesystem image to be shipped.  Group
    and other bits, ownership semantics, symlinks and content are all
    left alone, so nothing that a build can observe changes.
    """
    # The tree root included: the `filesystem` package owns "/" itself and
    # ships it dr-xr-xr-x, so the very top of an assembled buildroot ends
    # up read-only and even mkdir of a skeleton directory fails.
    _force_mode(dest, 0o700)
    for root, dirnames, filenames in os.walk(dest):
        for name in dirnames:
            _force_mode(os.path.join(root, name), 0o700)
        for name in filenames:
            _force_mode(os.path.join(root, name), 0o400)


def _force_mode(path, bits):
    if os.path.islink(path):
        # Modes on a symlink belong to its target, which is either already
        # handled by this walk or lives outside the tree.
        return
    try:
        mode = os.stat(path).st_mode & 0o7777
        if mode & bits != bits:
            os.chmod(path, mode | bits)
    except OSError:
        # A dangling symlink or a race with a concurrent action; the
        # extraction itself reports anything that actually matters.
        pass


def extract_rpm(rpm_path, dest, rpm2archive="rpm2archive", tar="tar"):
    """Unpack a binary or source rpm into dest.

    Uses a pipe rather than `rpm -i` so no rpmdb is touched and no
    scriptlets run -- we only want the payload.

    rpm2archive | tar rather than rpm2cpio | cpio, for one specific
    reason: cpio applies a directory's final mode as soon as it creates
    it, so an archive that ships /usr/lib as dr-xr-xr-x *and* files
    beneath it fails on its own payload.  GNU tar's
    --delay-directory-restore defers those modes to the end of the
    archive, which is exactly the ordering rpm's payloads assume.
    """
    os.makedirs(dest, exist_ok=True)
    # Directories left read-only by an earlier rpm would block this one.
    make_dirs_writable(dest)

    with open(rpm_path, "rb") as fh:
        p1 = subprocess.Popen(
            [rpm2archive, "-"], stdin=fh, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        p2 = subprocess.Popen(
            [
                tar,
                "-xz",
                "--delay-directory-restore",
                # We are not root, and rpm payloads name root:root.
                "--no-same-owner",
                "-C", dest,
            ],
            stdin=p1.stdout,
            stderr=subprocess.PIPE,
        )
        p1.stdout.close()
        _, err2 = p2.communicate()
        _, err1 = p1.communicate()
        rc2, rc1 = p2.returncode, p1.returncode

    if rc1 != 0 or rc2 != 0:
        for label, err in (("rpm2archive", err1), ("tar", err2)):
            if err:
                print(
                    "--- {} ---\n{}".format(label, err.decode(errors="replace")),
                    file=sys.stderr,
                )
        sys.exit(
            "failed to unpack {} (rpm2archive={}, tar={})".format(
                rpm_path, rc1, rc2
            )
        )


def reproducible_env(env=None, source_date_epoch="1700000000"):
    """Return an env dict with the usual reproducibility knobs pinned.

    SOURCE_DATE_EPOCH is honoured by rpm >= 4.14 for file mtimes and by
    most build systems.  Without it, replayed builds are not cacheable
    across machines in any meaningful way.
    """
    out = dict(env or os.environ)
    out.setdefault("SOURCE_DATE_EPOCH", source_date_epoch)
    out.setdefault("LC_ALL", "C")
    out.setdefault("LANG", "C")
    out.setdefault("TZ", "UTC")
    # rpm bakes the build host into package metadata; pin it.
    out.setdefault("RPM_BUILD_HOST", "buckos-distro")
    return out
