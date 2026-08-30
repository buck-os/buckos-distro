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
    bwrap    bubblewrap in a full subordinate-id user namespace, with the
             buildroot at /, the work area bound read-write, and no network.
    unshare  util-linux `unshare`: an unprivileged user namespace, then
             chroot into the buildroot.  Not equivalent to bwrap: it
             rebinds the host's /dev and a writable /proc where bwrap
             gives a minimal /dev and a read-only one, and it degrades to
             a single mapped id where bwrap refuses to start.  The
             fallback, not the production path.
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

import contextlib
import os
import platform
import pwd
import shlex
import shutil
import subprocess
import sys

from _rpm import require_tool, run

ISOLATION_MODES = ("auto", "bwrap", "unshare", "none")

# Where the work area appears inside a sandbox, regardless of where it
# lives outside.
#
# The scratch directory has a random name, so binding it at its own
# absolute path made that name an *input* to every compiler that ran
# inside: it is %_topdir, so it is the working directory, so it lands in
# DW_AT_comp_dir, and from there in the GNU build-id.  Two builds of the
# same source in different scratch directories produced different
# binaries.  A constant takes the randomness out of the compiler's view.
#
# /build rather than something under /tmp because /tmp is a tmpfs in both
# modes, and rather than /builddir because that is $HOME and rpm already
# uses it.  It has to be a path no buildroot ships, which is asserted
# before the mount rather than assumed -- see _assert_no_real_build_dir.
SANDBOX_WORK = "/build"

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


def _close_fd(fd):
    """Close an optional file descriptor during multi-resource cleanup."""
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass


def _reap_namespace_holder(child):
    """Terminate and reap a namespace holder without leaving a child behind."""
    if child is None:
        return
    if child.poll() is None:
        try:
            child.terminate()
        except ProcessLookupError:
            pass
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            child.kill()
        except ProcessLookupError:
            pass
        child.wait()


@contextlib.contextmanager
def _mapped_user_namespace():
    """Yield an fd for a live user namespace with the full subordinate map.

    Bubblewrap's own --unshare-user form maps one identity. Create the
    namespace separately so the parent can install both map entries, then
    keep its holder alive while Bubblewrap enters it through --userns.
    """
    if not subid_mapping_available():
        sys.exit(
            "isolation=bwrap requires subordinate UID and GID ranges plus "
            "newuidmap and newgidmap; a single-ID user namespace cannot "
            "preserve package payload ownership"
        )

    unshare = require_tool("unshare")
    ready_r = ready_w = hold_r = hold_w = namespace_fd = None
    child = None
    try:
        ready_r, ready_w = os.pipe()
        hold_r, hold_w = os.pipe()
        script = "\n".join([
            "set -e",
            "echo r >&{}".format(ready_w),
            "read _ <&{}".format(hold_r),
        ])
        argv = [
            unshare, "--user", "--",
            "/bin/sh", "-c", script,
        ]

        print("+ {}".format(" ".join(argv)), file=sys.stderr, flush=True)
        child = subprocess.Popen(argv, pass_fds=(ready_w, hold_r))
        _close_fd(ready_w)
        ready_w = None
        _close_fd(hold_r)
        hold_r = None

        # The byte is written after unshare(2), so the namespace exists but
        # the holder has not done anything that requires a mapped identity.
        if not os.read(ready_r, 2):
            status = child.wait()
            raise subprocess.CalledProcessError(status or 1, argv)
        _close_fd(ready_r)
        ready_r = None

        _map_subordinate_ids(child.pid)
        namespace_fd = os.open(
            "/proc/{}/ns/user".format(child.pid),
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
        )
        yield namespace_fd
    finally:
        _close_fd(namespace_fd)
        _close_fd(ready_r)
        _close_fd(ready_w)
        _close_fd(hold_r)
        _close_fd(hold_w)
        _reap_namespace_holder(child)


# ── The sandbox ──────────────────────────────────────────────────────


def sandbox_path(path, work, isolation):
    """Where a path under the work area is visible inside the sandbox.

    Call this on every path that crosses into the sandbox -- anything
    interpolated into a script, or passed as an argument to the command
    run_isolated() executes.  Paths the driver only uses for its own file
    I/O stay as they are: the work area is bound, not moved, so both names
    refer to the same directory and only one of them is meaningful inside.

    Raises rather than passing an unrecognised path through, because the
    work area is the *only* writable path the sandbox exposes (see
    _chroot_script).  A path from anywhere else is either a bug or a
    literal that belongs in the buildroot's own namespace, and returning
    it unchanged would produce exactly the failure this translation exists
    to prevent: a name that resolves outside and not inside, discovered
    part-way through an expensive action rather than here.

    isolation="none" has no mount namespace and therefore no bind, so the
    work area is at its host path and the translation is the identity.
    That mode is a property of the caller, not of the path, which is why
    it is a parameter rather than something this function detects.
    """
    if isolation == "none":
        return path

    absolute = os.path.abspath(path)
    root = os.path.abspath(work)
    if absolute == root:
        return SANDBOX_WORK
    if absolute.startswith(root + os.sep):
        return os.path.join(SANDBOX_WORK, os.path.relpath(absolute, root))

    raise ValueError(
        "{} is not under the work area {}, so it has no address inside "
        "the sandbox".format(absolute, root)
    )


def fabricated_mount_components(sandbox_root):
    """The mount point the sandbox invents inside its root, if it invents one.

    Bubblewrap has to create the work area's mount point inside the tree
    it mounts at `/`, and that directory survives the sandbox.  When the
    tree is the image being built, rather than a buildroot, it is shipped.

    Two steps do that.  The Debian rootfs transaction runs against the
    target, and the SELinux relabel runs against the unpacked image
    because the policy deciding the labels has to be the image's own.
    Everything else passes a buildroot and is unaffected.

    Call it before the sandbox runs, while the distinction is still
    observable, and prune what it returns afterwards.  A tree that already
    owns the directory gets an empty list, so pruning cannot delete a
    shipped one.

    It used to walk the host path component by component, because the bind
    was at a path with as many components as the machine's scratch
    directory happened to have and any suffix of them could be invented.
    A fixed bind makes it one known name; the list survives only because
    callers iterate it, and because a second fabricated path would belong
    here rather than in a new function.
    """
    candidate = os.path.join(sandbox_root, SANDBOX_WORK.lstrip("/"))
    return [] if os.path.isdir(candidate) else [candidate]


def _excerpt(text, needle, span=40):
    """The neighbourhood of `needle`, so a 4KB script names its own bug."""
    at = text.find(needle)
    start, end = max(0, at - span), min(len(text), at + len(needle) + span)
    return "{}{}{}".format(
        "..." if start else "", text[start:end], "..." if end < len(text) else ""
    )


def _assert_no_host_work_path(cmd, env, work):
    """Refuse to enter the sandbox carrying a path that does not exist in it.

    A mis-translated path has two directions and they are not symmetric.
    A *sandbox* path used host-side fails immediately with ENOENT, because
    SANDBOX_WORK does not exist out here -- cheap and loud.  A *host* path
    used inside fails deep in an action that may already have run for an
    hour, with a message about a missing file that plainly exists.

    The dangerous direction is the detectable one.  With a fixed bind,
    nothing entering a sandbox has any business naming the host work area:
    it is reachable in there only as SANDBOX_WORK.  So this is not a
    heuristic and it has no false positives to weigh -- an occurrence *is*
    the defect, by construction.

    Checking here rather than at each caller is what makes it a property
    of the boundary rather than a rule drivers have to remember.  It turns
    the whole class from a runtime failure into a build-time one without
    triaging anything by hand.
    """
    host = os.path.abspath(work)
    offenders = []
    for index, argument in enumerate(cmd):
        if isinstance(argument, str) and host in argument:
            offenders.append(("argv[{}]".format(index), _excerpt(argument, host)))
    for name, value in sorted((env or {}).items()):
        if isinstance(value, str) and host in value:
            offenders.append(("${}".format(name), _excerpt(value, host)))

    if offenders:
        sys.exit(
            "the host work area {} is named in {} thing(s) entering the "
            "sandbox, where that path does not exist. It is visible in "
            "there as {} -- translate with sandbox_path() at the point it "
            "crosses:\n{}".format(
                host,
                len(offenders),
                SANDBOX_WORK,
                "\n".join("  {}: {}".format(where, what) for where, what in offenders),
            )
        )


def _assert_no_real_build_dir(sysroot):
    """Refuse to shadow a buildroot that ships SANDBOX_WORK itself.

    Mounting the work area over a directory the tree genuinely owns would
    hide its contents for the length of the build, and the symptom would
    be missing files rather than a mount error.  No buildroot in the fleet
    ships one today -- checked across ten, on both flavors and both
    architectures -- but nothing stops a future package from adding one,
    and this is a cheap stat against a claim that would otherwise decay
    into a comment.
    """
    if not sysroot:
        return
    candidate = os.path.join(os.path.abspath(sysroot), SANDBOX_WORK.lstrip("/"))
    if os.path.isdir(candidate) and os.listdir(candidate):
        sys.exit(
            "buildroot {} ships a non-empty {}, which the work area mount "
            "would hide".format(sysroot, SANDBOX_WORK)
        )


def _chroot_script(work, chdir, sysroot, sync_fds=None):
    """The shell that turns a fresh namespace into the buildroot as /.

    The work area is bound at SANDBOX_WORK, a constant, rather than at its
    own absolute path: the paths a caller computes outside are translated
    with sandbox_path() before they cross, so the scratch directory's
    random name never reaches a tool running inside.

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
    chdir = sandbox_path(chdir, work, "unshare")

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
        # Where the work area lands inside, not where it lives outside.
        "MOUNT={}".format(shlex.quote(SANDBOX_WORK)),
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
        # One known component rather than the host path's whole chain of
        # directories, which is also what retires a hazard this ordering
        # used to exist for: with no --work the work area is itself under
        # /tmp, so the mount point landed inside the tmpfs mounted just
        # above and was hidden by it.  SANDBOX_WORK is never under /tmp,
        # so the order no longer decides anything -- kept as it is because
        # a mount point created after its parent filesystem is the
        # sequence that reads correctly either way.
        'mkdir -p "$ROOT$MOUNT"',
        'mount --bind "$WORK" "$ROOT$MOUNT"',
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


def _with_action_tmpdir(env, work, isolation):
    """Point temporary files at the work area, which is on real disk.

    `/tmp` inside the sandbox is a tmpfs in both modes -- `--tmpfs /tmp`
    under Bubblewrap, `mount -t tmpfs` under unshare -- so a tool that
    falls back to it charges its intermediates to memory.  On a machine
    with a terabyte of RAM that tmpfs is half of it, and a build with
    hundreds of concurrent compilers writing there is memory pressure
    whose failure mode looks like anything except a configuration choice.

    The work area is the only writable disk the sandbox has: it lives
    under the scratch root, which everything already assumes is a real
    filesystem.  The directory is created at its host name and named to
    the child by its sandbox one, which under a fixed bind is the constant
    SANDBOX_WORK + "/tmp" -- so this variable, like the rest of the pinned
    environment, no longer varies between two runs of the same build.

    Set here rather than in reproducible_env because this is the only
    layer that knows the work area.  The pinned variables there are
    constants; this one is a function of the action.

    setdefault rather than assignment, which is the opposite of what
    reproducible_env does, and the difference is exactly why it is safe.
    That function was refusing *inherited* values, which no caller chose.
    Here every value in the dict was put there deliberately by a driver:
    rpmbuild_replay points at its own topdir tmp so it agrees with rpm's
    %_tmppath define, and dpkgbuild_replay chooses /tmp on purpose.  A
    caller that has said something more specific should keep it.
    """
    if env is None:
        return None
    out = dict(env)
    action_tmp = os.path.join(os.path.abspath(work), "tmp")
    os.makedirs(action_tmp, exist_ok=True)
    # TMP and TEMP alongside TMPDIR because rpmbuild_replay already found
    # it needed all three; a tool honouring only one of the others would
    # otherwise still reach the tmpfs.
    for name in ("TMPDIR", "TMP", "TEMP"):
        out.setdefault(name, sandbox_path(action_tmp, work, isolation))
    return out


def run_isolated(cmd, isolation, work, chdir, sysroot, env=None):
    """Run a command inside the sandbox the resolved mode implies.

    `sysroot` is the tree to become /, already composed if the caller
    layers anything onto the seed -- never the shared seed input itself,
    which is a Buck artifact other actions are reading concurrently.
    `work` is the scratch area to make visible inside, bound at
    SANDBOX_WORK in the sandboxed modes; `chdir` is where to start, given
    as a host path under `work` and translated here so no caller has to
    remember to do it.  Anything else the command names must be translated
    by the caller with sandbox_path().
    """
    env = _with_action_tmpdir(env, work, isolation)

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

    _assert_no_real_build_dir(sysroot)
    _assert_no_host_work_path(cmd, env, work)

    if isolation == "bwrap":
        bwrap = require_tool("bwrap")
        with _mapped_user_namespace() as namespace_fd:
            wrapper = [
                bwrap,
                "--userns", str(namespace_fd),
                "--uid", "0",
                "--gid", "0",
                "--unshare-net",
                "--unshare-pid",
                "--unshare-ipc",
                "--die-with-parent",
                # The flavor buildroot becomes /.
                "--bind", os.path.abspath(sysroot), "/",
                # The work area, writable, at a constant address.  Callers
                # translate with sandbox_path() rather than reusing the
                # host name.
                "--bind", os.path.abspath(work), SANDBOX_WORK,
                # Preserve any restrictions imposed on the caller's procfs.
                # A fresh procfs mount can be rejected inside a container when
                # its existing procfs hides sensitive entries.
                "--ro-bind", "/proc", "/proc",
                # A spec can ask the running kernel a question only the
                # running kernel can answer: libcap-ng's %build reads BTF
                # from /sys/kernel/btf/vmlinux, and the buildroot's own /sys
                # is a fabricated empty directory.  Read-only because reading
                # is all any of them do, and because the unshare path already
                # rebinds it -- a mount one mode provides and the other hides
                # makes the two stop agreeing about what a build can see.
                "--ro-bind", "/sys", "/sys",
                "--dev", "/dev",
                "--tmpfs", "/tmp",
                "--setenv", "HOME", "/builddir",
                "--chdir", sandbox_path(chdir, work, isolation),
            ]
            return run(
                wrapper + list(cmd),
                env=env,
                pass_fds=(namespace_fd,),
            )

    if isolation == "unshare":
        return _run_unshare(cmd, work, chdir, sysroot, env)

    sys.exit("unknown isolation mode: {}".format(isolation))
