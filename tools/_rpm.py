"""Shared rpm helpers for buckos-distro action scripts.

Everything here shells out to the host/buildroot rpm toolchain.  We never
reimplement rpm semantics -- see SPEC.md section 1.
"""

import hashlib
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


def scratch_dir(prefix, key=None, remove=None):
    """A private scratch directory for a tree Buck must not walk.

    `key` makes the name a function of the action instead of random, and
    that is a reproducibility fix, not a tidiness one.  The work area is
    bound into the sandbox at its own absolute path (see _isolation.py),
    so it *is* %_topdir, so it lands in every DW_AT_comp_dir the compiler
    emits.  ld then hashes the linked output -- debug sections included --
    into the GNU build-id.  find-debuginfo runs debugedit afterwards,
    which rewrites those paths to /usr/src/debug and leaves the note
    alone, so the path vanishes from the output while its fingerprint
    stays behind in 20 bytes that differ on every build.

    That is invisible in the obvious places: nothing greps out of the
    rpm, the DWARF is byte-identical, and the diff is a build-id plus the
    two things derived from it (.gnu_debuglink's CRC and the
    xz-compressed .gnu_debugdata, which carries its own copy of the
    note).  mkdtemp's suffix is even fixed-length, so the binaries do not
    change size.

    So: pass something that identifies the action and is stable across
    runs of it -- an output path is both.  Callers that only need a
    private directory can leave it None and keep mkdtemp semantics.

    `remove` overrides how a leftover from a previous run is deleted, and
    a caller that ran an rpm transaction in here has to supply one.  The
    transaction chowns files to the ids their packages declare -- bind's
    /var/named/slaves comes back owned by a subordinate uid, mode 0770 --
    and we own neither the directory nor the right to chmod it, so
    make_dirs_writable below cannot reach it and rmtree stops on
    EPERM.  _isolation.remove_tree deletes it from inside a namespace with
    the same maps, which is the only thing that can.  Not the default
    because this module is the layer _isolation is built on, and the
    dependency only runs one way.
    """
    base = os.environ.get(SCRATCH_ROOT_ENV) or _DEFAULT_SCRATCH_ROOT
    os.makedirs(base, exist_ok=True)
    if key is None:
        return tempfile.mkdtemp(prefix=prefix, dir=base)

    digest = hashlib.sha256(key.encode("utf-8", "surrogateescape")).hexdigest()
    path = os.path.join(base, prefix + digest[:16])
    # Reused across runs by construction, so a previous run's leftovers --
    # or its debris after a kill -- would otherwise be inherited as build
    # inputs.  mkdtemp got this for free by never repeating a name.
    if os.path.exists(path):
        if remove is None or not remove(path):
            make_dirs_writable(path)
            shutil.rmtree(path)
    os.makedirs(path, mode=0o700)
    return path


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


def overlay_tree(src, dest):
    """Layer one unpacked tree over another; the later layer wins.

    Not `shutil.copytree(..., dirs_exist_ok=True)`, which tolerates a
    pre-existing *directory* and nothing else.  Overlaying a locally
    built package onto a tree that already carries the pinned upstream
    copy of it means every path that package owns is already there, and
    `os.symlink` onto an existing path raises EEXIST -- so the first
    dependency shipping a soname link (liblzma.so.5) kills the copy.

    Replacement is the semantics this needs, not a merge: the reason to
    apply a layer at all is that its version of a path supersedes the one
    below.  Type changes are handled in both directions, because a
    rebuild is free to turn a directory into a symlink or back
    (/usr/lib -> usr/lib64 style moves do exactly that).

    Directories are left owner-writable for the reason make_dirs_writable
    spells out: rpm ships dr-xr-xr-x directories, and the next layer up
    has to be able to write into them.
    """
    for dirpath, dirnames, filenames in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        out_dir = dest if rel == os.curdir else os.path.join(dest, rel)
        _overlay_dir(dirpath, out_dir)

        # os.walk reports a symlink-to-directory in dirnames but does not
        # descend into it.  Recreate it as a link and drop it from the
        # walk, or the tree grows a real directory where rpm declared a
        # symlink -- and on a merged-usr layout that silently forks /lib
        # away from /usr/lib.
        real = []
        for name in dirnames:
            src_entry = os.path.join(dirpath, name)
            if os.path.islink(src_entry):
                _overlay_entry(src_entry, os.path.join(out_dir, name))
            else:
                real.append(name)
        dirnames[:] = real

        for name in filenames:
            _overlay_entry(
                os.path.join(dirpath, name),
                os.path.join(out_dir, name),
            )
    return dest


def _overlay_dir(src, dest):
    """Make dest a directory mirroring src, keeping anything already in it.

    A symlink to a directory is followed rather than replaced, and that is
    the merged-usr rule rather than a convenience.  Fedora's `filesystem`
    ships /lib64 as a symlink to usr/lib64, and a handful of packages --
    libtirpc is the one that found this -- still declare their files under
    /lib64.  On a real system that path *resolves through* the symlink and
    the file lands in /usr/lib64; that is what the merge means.

    Replacing the symlink with a real directory instead forks the two
    apart, and the way it fails is nasty.  The buildroot ends up with
    /lib64 holding one library and /usr/lib64 holding everything else,
    including ld-linux-x86-64.so.2 -- which is every binary's ELF
    interpreter, named absolutely as /lib64/ld-linux-x86-64.so.2.  Exec
    then fails on the interpreter rather than on the binary, so the kernel
    returns ENOENT for a file that plainly exists, the shell reports it as
    127, and nothing is written to stderr at all:

        Stdout: <empty>
        Stderr:

    26 of 126 probes died that way, each one reporting only that `rpm`
    could not be found in a tree containing /usr/bin/rpm.
    """
    if os.path.islink(dest) and os.path.isdir(dest):
        # Nothing to create: writes below here resolve through the link on
        # their own.  Mode is forced on the target, which is what needs it.
        _force_mode(dest, 0o700)
        return
    if os.path.lexists(dest) and not _is_real_dir(dest):
        _remove_any(dest)
    if not os.path.lexists(dest):
        os.makedirs(dest)
        shutil.copystat(src, dest)
    _force_mode(dest, 0o700)


def _overlay_entry(src, dest):
    """Put src at dest, replacing whatever is there."""
    _remove_any(dest)
    if os.path.islink(src):
        os.symlink(os.readlink(src), dest)
    else:
        shutil.copy2(src, dest, follow_symlinks=False)


def _remove_any(path):
    if not os.path.lexists(path):
        return
    if _is_real_dir(path):
        shutil.rmtree(path)
    else:
        os.unlink(path)


def _is_real_dir(path):
    """A directory and not a symlink to one -- lexists/isdir disagree there."""
    return os.path.isdir(path) and not os.path.islink(path)


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
                # Reshape the backslash names *as tar writes them*, not
                # afterwards -- see _UNREPRESENTABLE below.  A rename pass
                # after extraction is too late: the file exists under
                # buck-out in the window between tar creating it and the
                # pass renaming it, buck2 notices it there, and the build
                # that observed it succeeds while the *next* one fails on
                # a path that is no longer on disk.  That is how this was
                # first mistaken for stale daemon state.
                #
                # sed syntax, so a literal backslash is written \\, and
                # the default scope also rewrites symlink and hard-link
                # targets -- which is wanted, or a link to one of these
                # files would dangle at the un-nested name.
                "--transform", "s|\\\\|/|g",
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

    # Belt and braces.  tar's --transform above is what actually keeps the
    # backslash off disk; this catches anything that reached the tree by
    # some other route, and it is the half that reports -- tar reshapes
    # silently, and a file that has turned into a directory is exactly the
    # kind of thing that should show up in a log when someone later
    # wonders why an exact-name lookup missed.  In the normal case it
    # finds nothing and prints nothing.
    for before, after in nest_unrepresentable(dest):
        print(
            "buckos-distro: {}: split {} into {} -- buck2 reads a backslash "
            "as a path separator".format(
                os.path.basename(rpm_path), before, after
            ),
            file=sys.stderr,
        )


# Buck2 addresses every file in a directory output by a project-relative
# path, and a backslash cannot appear in one -- materialising such a path
# fails the whole build with "Error relativizing <path> is not relative to
# project root", naming a file nobody asked for.  systemd escapes a dash in
# a unit name as \x2d, so systemd's own payload carries
# system-systemd\x2dcryptsetup.slice and system-systemd\x2dveritysetup.slice,
# and any tree holding them is unbuildable as a directory output.
#
# The failure is worse than it looks: it survives deleting the file, because
# the tree is rebuilt from the rpm on the next run, and it aborts the build
# before any of our own targets are analysed.  It also outlives `buck2 kill`
# -- the path is recorded in buck-out/v2/cache/materializer_state, so the
# next invocation fails instantly on a file that is no longer on disk, and
# clearing that state is what actually breaks the loop.  See
# tools/scratch_contract_test.py for the same class of problem elsewhere.
_UNREPRESENTABLE = "\\"


def nest_unrepresentable(dest):
    """Split payload paths buck2 cannot address at the backslash.

    Buck2 reads the backslash as a path separator; this makes that true on
    disk rather than fighting it, so

        .../system/system-systemd\\x2dcryptsetup.slice     one file
        .../system/system-systemd/x2dcryptsetup.slice      a dir and a file

    Deleting the byte, or the file, also builds -- both were tried.
    Nesting is better for one reason: it round-trips.  Joining the
    components back with a backslash reconstructs the original name
    exactly, so anything that later wants a faithful tree can rebuild one
    from what is on disk.  A dropped file cannot be recovered at all, and
    a deleted byte cannot be put back because nothing records where it was.

    Safe *because of where this is called from*, which is worth stating
    plainly.  Every extract_rpm caller unpacks into a buck2 directory
    output that is used as a tool sysroot or a build installroot -- trees
    that run mksquashfs or rpmbuild, and never boot.  A systemd unit file
    in one of them is inert, and a systemd unit file that has become a
    directory is equally inert.

    A shipped image does not come through here at all: rootfs_install.py
    runs a real rpm transaction inside the sandbox and hands back a
    tarball, and a tar member has no such restriction.  So the image keeps
    every file rpm puts in it, backslashes included, and this reshaping
    can never change what a user actually boots.

    Returns [(before, after)] for the caller to report.
    """
    moved = []
    # topdown=False so a directory is visited after its contents, which is
    # what keeps a move safe mid-walk: nothing still queued sits under a
    # path this loop has already renamed.
    for root, dirs, files in os.walk(dest, topdown=False):
        for name in dirs + files:
            if _UNREPRESENTABLE not in name:
                continue

            parts = name.split(_UNREPRESENTABLE)
            if not all(parts):
                # A leading, trailing or doubled backslash would mean an
                # empty path component.  Nothing ships one, and inventing
                # a name for it would be worse than saying so.
                sys.exit(
                    "{}: cannot split a name with an empty "
                    "component".format(os.path.join(root, name))
                )

            src = os.path.join(root, name)
            dst = os.path.join(root, *parts)
            if os.path.lexists(dst):
                # The nested path is already taken by something rpm
                # unpacked on its own.  Merging would be a payload
                # silently going missing.
                sys.exit(
                    "{}: splitting it collides with the existing {}".format(
                        src, dst
                    )
                )

            parent = os.path.dirname(dst)
            os.makedirs(parent, exist_ok=True)
            # The intermediate directory is ours, not rpm's, so it gets
            # the owner bits make_dirs_writable would force rather than
            # whatever umask happens to be in effect.
            _force_mode(parent, 0o700)
            os.rename(src, dst)
            moved.append(
                (os.path.relpath(src, dest), os.path.relpath(dst, dest))
            )
    return moved


def stage_rpms(rpms, staging):
    """Gather rpms into one directory the sandbox already exposes.

    The alternative was bind-mounting each artifact's directory into the
    chroot, and it does not scale: Buck gives every http_file its own
    output directory, so even the modest seed set is 292 separate mounts
    per build, and a real image is thousands.

    Hardlinked where the filesystem allows it, which is the normal case --
    the scratch root defaults to /var/tmp precisely so it shares a device
    with buck-out, see scratch_dir above -- so this is a directory entry
    per package rather than a copy of the payload.  Buck inputs are
    read-only and rpm only reads them, so sharing the inode is safe.

    Names collide in principle (a locally built package can have the same
    filename as the pinned one it replaces), so a collision gets a numeric
    suffix rather than silently dropping one of the two.

    Every staged name is given a .rpm suffix if it lacks one.  That is not
    cosmetic: the transaction is handed to rpm as the glob `*.rpm` rather
    than as 292 explicit paths, because the argument list would otherwise
    approach the kernel's 128 KB limit on a single argument string, and a
    pinned package arrives from http_file named after its Buck target with
    no extension.  Unsuffixed, it would simply not be in the transaction,
    and rpm would report the resulting hole as a missing dependency
    somewhere else entirely.
    """
    os.makedirs(staging, exist_ok=True)
    staged = []
    used = set()
    for source in rpms:
        name = os.path.basename(source)
        if not name.endswith(".rpm"):
            name += ".rpm"
        if name in used:
            stem, dot, ext = name.rpartition(".")
            for n in range(1, 1000):
                candidate = "{}-{}{}{}".format(stem or name, n, dot, ext)
                if candidate not in used:
                    name = candidate
                    break
        used.add(name)
        dest = os.path.join(staging, name)
        try:
            os.link(source, dest)
        except OSError:
            # Cross-device, or a filesystem without hardlinks.
            shutil.copy2(source, dest)
        staged.append(dest)
    return staged


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
