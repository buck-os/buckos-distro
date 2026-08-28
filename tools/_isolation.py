"""Entering a buildroot as / from an unprivileged Buck action.

Extracted from rpmbuild_replay.py because two actions now need it: the
spec replay, and rootfs installation (tools/rootfs_install.py).  A second
hand-rolled copy of a chroot incantation is how the two quietly stop
agreeing about what the sandbox guarantees.

Genuine hermeticity needs the buildroot mounted as /.  Without that, the
compiler, linker and rpm macros come from the machine rather than from the
distro being built -- a Fedora 45 spec compiled by CentOS 9's gcc against
CentOS 9's glibc is not a Fedora 45 package, even though it will be
labelled .fc45 and will usually succeed.

Modes:

    none     run against the ambient filesystem.  Non-hermetic.  Used with
             buildroot provenance "host".
    bwrap    bubblewrap with the buildroot bind-mounted at /, the work
             area bound read-write, and no network.  Unprivileged.
    unshare  util-linux `unshare`: an unprivileged user namespace, then
             chroot into the buildroot.  Equivalent hermeticity to bwrap,
             using tools present on any modern kernel.
    auto     bwrap if installed, else unshare.  Never falls back to
             "none": silently degrading to the host toolchain is exactly
             the failure this layer exists to prevent, and it would be
             invisible in the output.

Why this module runs the command rather than returning one
----------------------------------------------------------
Mapping more than a single uid into the namespace -- see
_map_subordinate_ids() -- is a handshake with the child process, not a
command-line flag.  A caller that received a command list would have to
perform that handshake itself, which is precisely the duplication this
module exists to prevent.  So the entry point is run_isolated().
"""

import os
import platform
import pwd
import shlex
import shutil
import subprocess
import sys

from _rpm import require_tool, run

ISOLATION_MODES = ("auto", "bwrap", "unshare", "none")

# Where the kernel records a namespace's id translations.
_UID_MAP = "/etc/subuid"
_GID_MAP = "/etc/subgid"

_CPU_ALIASES = {
    "amd64": "x86_64",
    "arm64": "aarch64",
    "x86_64": "x86_64",
    "aarch64": "aarch64",
}


def require_target_execution(target_cpu, provenance="binary-seed"):
    """Fail before chroot when this host cannot execute target binaries."""
    if not target_cpu:
        return
    target = _CPU_ALIASES.get(target_cpu, target_cpu)
    native = _CPU_ALIASES.get(platform.machine(), platform.machine())
    if provenance == "host" and target != native:
        sys.exit(
            "host provenance cannot build {} on {}; use the binary-seed "
            "buildroot with a native worker or a registered binfmt handler".format(
                target,
                native,
            )
        )
    if target == native:
        return

    handler = "/proc/sys/fs/binfmt_misc/qemu-{}".format(target)
    try:
        with open(handler, encoding="utf-8") as stream:
            status = stream.read()
    except OSError:
        status = ""
    if not status.startswith("enabled\n"):
        sys.exit(
            "this {} worker cannot execute {} target binaries: {} is not "
            "enabled. Use a native worker or register QEMU binfmt support".format(
                native,
                target,
                handler,
            )
        )


def resolve_isolation(mode):
    """Turn "auto" into a concrete mechanism.

    Deliberately never resolves to "none".  A missing sandbox must be a
    hard error: falling back to the host would produce a build that looks
    identical, is labelled identically, and is quietly made of the wrong
    distro's toolchain.
    """
    if mode != "auto":
        return mode
    if shutil.which("bwrap"):
        return "bwrap"
    if shutil.which("unshare"):
        return "unshare"
    sys.exit(
        "isolation=auto found neither bwrap nor unshare. A hermetic "
        "buildroot cannot be entered without one of them; install "
        "bubblewrap or util-linux, or set `buildroot = host` for the "
        "selected flavor in .buckconfig.local and accept that the result "
        "is not built with the target distro's toolchain."
    )


# ── Subordinate id ranges ────────────────────────────────────────────


def _subid_range(path):
    """This user's subordinate id range from /etc/sub[ug]id, or None.

    The file is keyed by either login name or numeric id, and both forms
    appear in the wild, so both are accepted.
    """
    try:
        user = pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        user = None
    keys = {str(os.getuid())}
    if user:
        keys.add(user)

    try:
        with open(path) as fh:
            for line in fh:
                fields = line.strip().split(":")
                if len(fields) != 3 or fields[0] not in keys:
                    continue
                try:
                    return int(fields[1]), int(fields[2])
                except ValueError:
                    continue
    except OSError:
        return None
    return None


def subid_mapping_available():
    """Can we map a whole range of ids, rather than just our own?

    This is the difference between a namespace that can install an rpm and
    one that cannot.  With only `unshare --map-root-user` exactly one id is
    mapped, so a payload owned by anyone but root fails to unpack:
    Fedora's `filesystem` package ships /var/spool/mail as root:mail, and
    chown to the unmapped gid 12 returns EINVAL a few files into the very
    first package.

    Needs both a configured range and the setuid helpers that install it --
    writing /proc/<pid>/gid_map directly is refused for any map wider than
    one entry.
    """
    return (
        _subid_range(_UID_MAP) is not None
        and _subid_range(_GID_MAP) is not None
        and shutil.which("newuidmap") is not None
        and shutil.which("newgidmap") is not None
    )


def _map_subordinate_ids(pid):
    """Give a namespaced process the full range of ids rpm needs.

    Two entries each way:

        0 <our id> 1        the namespace's root is us, so files we already
                            own stay ours and the chroot is enterable
        1 <base> <count>    everything else lands in the subordinate range,
                            so uid 8 (mail) inside is a real, distinct,
                            unprivileged id outside

    Installed with newuidmap/newgidmap rather than by writing the proc
    files: the kernel only lets an unprivileged process write a
    single-entry map, and the setuid helpers exist precisely to validate a
    wider one against /etc/sub[ug]id.
    """
    for helper, path, own in (
        ("newuidmap", _UID_MAP, os.getuid()),
        ("newgidmap", _GID_MAP, os.getgid()),
    ):
        base, count = _subid_range(path)
        run([
            require_tool(helper), str(pid),
            "0", str(own), "1",
            "1", str(base), str(count),
        ])


# ── The sandbox ──────────────────────────────────────────────────────


def _chroot_script(work, chdir, sysroot, sync_fds=None):
    """The shell that turns a fresh namespace into the buildroot as /.

    The work area is bound at its own absolute path inside the chroot so
    every path the caller already computed -- _topdir, _tmppath, and the
    --buildroot installroot, which are siblings under it -- resolves to
    the same string in as out.

    It is also the *only* writable path exposed, and deliberately so: a
    caller with inputs elsewhere in buck-out stages them under the work
    area (tools/rootfs_install.py hardlinks them) rather than growing this
    function a list of extra mounts.  Buck gives every artifact its own
    output directory, so "just bind the inputs" means hundreds of mount
    calls per action, and two sandboxes that disagree about which of them
    are writable.
    """
    root = os.path.abspath(sysroot)
    work = os.path.abspath(work)
    chdir = os.path.abspath(chdir)

    # Interpolated as shell assignments rather than passed through the
    # environment: `run()` replaces the child's env wholesale, so anything
    # this script needs has to travel inside the script itself.
    lines = ["set -e"]

    if sync_fds:
        ready, go = sync_fds
        # Nothing above this point may need privilege: until the parent has
        # written our id maps we are the overflow uid, owning nothing.
        lines += [
            "echo r >&{}".format(ready),
            "read _ <&{}".format(go),
        ]

    lines += [
        "ROOT={}".format(shlex.quote(root)),
        "WORK={}".format(shlex.quote(work)),
        "CHDIR={}".format(shlex.quote(chdir)),
        # Make the sysroot a mount point in its own right so chroot has a
        # real root to pivot onto.
        'mount --bind "$ROOT" "$ROOT"',
        'mkdir -p "$ROOT/proc" "$ROOT/dev" "$ROOT/sys" "$ROOT/tmp"',
        # Reuse the caller's procfs rather than mounting a new one. Linux
        # rejects a nested procfs mount when the caller's existing procfs
        # hides entries, as container managers commonly do. The recursive
        # bind preserves those restrictions and still avoids host paths in
        # the buildroot itself.
        'mount --rbind /proc "$ROOT/proc"',
        # rbind rather than a fresh devtmpfs: an unprivileged user
        # namespace cannot create device nodes, but it can carry the
        # host's existing ones across.
        'mount --rbind /dev "$ROOT/dev"',
        'mount --rbind /sys "$ROOT/sys"',
        'mount -t tmpfs tmpfs "$ROOT/tmp"',
        # After the tmpfs, not before: with no --work the work area is
        # itself under /tmp, and a mount point created first would be
        # hidden by the tmpfs that lands on top of it.
        'mkdir -p "$ROOT$WORK"',
        'mount --bind "$WORK" "$ROOT$WORK"',
        'cd "$ROOT$CHDIR"',
        'exec chroot "$ROOT" sh -c \'cd "$1"; shift; exec "$@"\' sh "$CHDIR" "$@"',
    ]
    return "\n".join(lines)


# The namespaces, minus --user, which is handled separately because how it
# is mapped is the whole question.
#
#   --mount       so the mounts are private and vanish with the build
#   --pid --fork  required before /proc can be mounted, and reaps stray
#                 build daemons on exit
#   --net         no network, so a spec that downloads mid-build fails here
#                 instead of producing an unreproducible artifact
_NAMESPACES = ("--mount", "--pid", "--fork", "--net", "--ipc")


def _run_unshare(cmd, work, chdir, sysroot, env):
    """Enter the buildroot as / using an unprivileged user namespace."""
    unshare = require_tool("unshare")

    if not subid_mapping_available():
        # Degraded, and said out loud.  Payloads owned by root still unpack,
        # so a buildroot assembles fine and a small rootfs may too -- but
        # anything shipping a non-root file dies partway through a
        # transaction, which reads as a corrupt package rather than as a
        # missing host configuration.
        print(
            "buckos-distro: WARNING: no subordinate id range for this user "
            "(see {} and {}) or no newuidmap/newgidmap. Falling back to a "
            "single mapped id; rpm cannot chown files to uids other than "
            "root, so any package shipping one will fail to unpack.".format(
                _UID_MAP, _GID_MAP
            ),
            file=sys.stderr,
        )
        script = _chroot_script(work, chdir, sysroot)
        return run(
            [unshare, "--user", "--map-root-user"] + list(_NAMESPACES) +
            ["--", "/bin/sh", "-c", script, "sh"] + list(cmd),
            env=env,
        )

    # The handshake.  `unshare` creates the user namespace and execs the
    # shell, which announces itself and blocks; we install the id maps from
    # out here, where we still have our own identity; then it proceeds.
    # The order is forced: a namespace's maps can only be written before it
    # has done anything requiring them, and only from outside.
    ready_r, ready_w = os.pipe()
    go_r, go_w = os.pipe()
    script = _chroot_script(work, chdir, sysroot, sync_fds=(ready_w, go_r))

    # No --map-root-user: it would write a single-entry map immediately, and
    # a namespace's uid_map can only be written once.
    argv = [unshare, "--user"] + list(_NAMESPACES) + [
        "--", "/bin/sh", "-c", script, "sh",
    ] + [str(c) for c in cmd]

    print("+ {}".format(" ".join(argv)), file=sys.stderr, flush=True)
    child = subprocess.Popen(argv, env=env, pass_fds=(ready_w, go_r))
    try:
        os.close(ready_w)
        os.close(go_r)
        # Blocks until the shell is running, which is after unshare(2) has
        # returned -- so the namespace we are about to map really exists.
        # An empty read means the child died first; let waiting report why
        # rather than failing here on a confusing newuidmap error.
        if os.read(ready_r, 2):
            _map_subordinate_ids(child.pid)
        os.write(go_w, b"go\n")
    finally:
        os.close(ready_r)
        os.close(go_w)

    status = child.wait()
    if status != 0:
        raise subprocess.CalledProcessError(status, argv)
    return status


def remove_tree(path):
    """Delete a tree an rpm transaction chowned out of our reach.

    A real transaction inside the sandbox chowns files to the ids their
    packages declare, and those land in the subordinate range on the way
    out: bind's /var/named/slaves comes back as uid 1879048216, mode 0770.
    We do not own it, so chmod is EPERM, and 0770 grants nothing to
    "other" -- so we cannot list it either, and an ordinary rmtree stops
    with

        PermissionError: [Errno 13] Permission denied:
            .../sysroot/var/named/slaves

    _rpm.make_dirs_writable cannot help: it forces owner bits, and we are
    not the owner.  The asymmetry is the whole point -- the tree was
    *created* by a process that could write those ids, so it has to be
    destroyed by one too.  Same namespace, same maps, where uid 0 is us
    and the range below maps back to what rpm asked for.

    No chroot: this deletes a host path, and the namespace is here for the
    id maps alone.

    Returns True if it removed the tree, False if it could not even try --
    the caller decides whether that is fatal, because it is not always:
    a leftover only matters when something reuses the path.
    """
    if not os.path.exists(path):
        return True
    if not subid_mapping_available():
        return False

    unshare = require_tool("unshare")
    ready_r, ready_w = os.pipe()
    go_r, go_w = os.pipe()
    # Same handshake as _run_unshare, and for the same reason: the maps go
    # in from outside, after the namespace exists and before anything in it
    # needs an identity.  Until then we are the overflow uid and own
    # nothing, so the rm must wait.
    script = "\n".join([
        "set -e",
        "echo r >&{}".format(ready_w),
        "read _ <&{}".format(go_r),
        'exec rm -rf -- "$1"',
    ])
    argv = [unshare, "--user", "--mount", "--",
            "/bin/sh", "-c", script, "sh", str(path)]

    print("+ {}".format(" ".join(argv)), file=sys.stderr, flush=True)
    child = subprocess.Popen(argv, pass_fds=(ready_w, go_r))
    try:
        os.close(ready_w)
        os.close(go_r)
        if os.read(ready_r, 2):
            _map_subordinate_ids(child.pid)
        os.write(go_w, b"go\n")
    finally:
        os.close(ready_r)
        os.close(go_w)

    return child.wait() == 0


def run_isolated(cmd, isolation, work, chdir, sysroot, env=None):
    """Run a command inside the sandbox the resolved mode implies.

    `sysroot` is the tree to become /, already composed if the caller
    layers anything onto the seed -- never the shared seed input itself,
    which is a Buck artifact other actions are reading concurrently.
    `work` is the scratch area to make visible inside; `chdir` is where to
    start.
    """
    if isolation == "none":
        # `chdir` is the sandbox's starting directory in the other modes,
        # where the chroot script cds to it.  With no sandbox it is just
        # the cwd, and it still has to be honoured: rpmbuild resolves
        # relative paths in a spec against wherever it was started.
        return run(cmd, env=env, cwd=chdir)

    if not sysroot:
        sys.exit(
            "isolation={} requires --buildroot-tree: there is no root to "
            "enter".format(isolation)
        )

    if isolation == "bwrap":
        bwrap = require_tool("bwrap")
        wrapper = [
            bwrap,
            "--unshare-net",
            "--unshare-pid",
            "--unshare-ipc",
            "--die-with-parent",
            # The flavor buildroot becomes /.
            "--bind", os.path.abspath(sysroot), "/",
            # The work area stays writable at its real path so paths the
            # caller computed outside resolve identically inside.
            "--bind", os.path.abspath(work), os.path.abspath(work),
            # Preserve any restrictions imposed on the caller's procfs.
            # A fresh procfs mount can be rejected inside a container when
            # its existing procfs hides sensitive entries.
            "--ro-bind", "/proc", "/proc",
            "--dev", "/dev",
            "--tmpfs", "/tmp",
            "--setenv", "HOME", "/builddir",
            "--chdir", os.path.abspath(chdir),
        ]
        return run(wrapper + list(cmd), env=env)

    if isolation == "unshare":
        return _run_unshare(cmd, work, chdir, sysroot, env)

    sys.exit("unknown isolation mode: {}".format(isolation))
