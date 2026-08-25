"""Fedora buildroot and package definitions, one set per release.

Lives in a .bzl rather than inline in BUCK because the BUCK dialect allows
neither `def` nor top-level `if`, and defining a release's targets is a
loop over a config-driven list that needs both.

This file is the handwritten half of the generate step: `generated/*.bzl`
holds pure data produced by tools/generate.py, and the macros here turn it
into targets.  Keeping the logic out of the generated file is what makes
the generated diff reviewable.
"""

load("//defs:flavor.bzl", "package")
load("//defs:releases.bzl", "release_suffix")
load("//defs/rules/boot.bzl", "initramfs", "kernel_image")
load("//defs/rules/buildroot.bzl", "host_buildroot", "seeded_buildroot")
load("//defs/rules/rootfs.bzl", "rootfs")

# Which dracut modules an image set's initramfs has to be told to include.
#
# Listed per set rather than inferred, because it cannot be inferred: the
# `live` set contains dracut-live, but a package being *installed* is not
# the same as its module being *enabled*, and dracut's own host-only
# autodetection is exactly what --no-hostonly turns off.  So the decision
# is written down here, next to the set it belongs to.
#
# dmsquash-live is what teaches the initramfs to find a squashfs on
# removable media and pivot into it.  Without it a live ISO boots as far as
# a dracut emergency shell and no further -- and the build stays green,
# because nothing between here and a boot attempt can tell.
_INITRAMFS_MODULES = {
    "live": ["dmsquash-live"],
}

# ── Where an rpm is fetched from ─────────────────────────────────────
#
# Exactly one URL per rpm.  Not a preference list: this prelude's
# http_file asserts `len(urls) == 1` (prelude/http_file.bzl), so a
# fallback chain is not available to fall back on.
#
# That makes the single URL have to be right rather than merely likely,
# which is why the base comes from the lockfile instead of from a table of
# upstream's layout here.  A repo-relative `location` does not record which
# repo produced it, and the repodata that would is gitignored, so the solve
# is the last point that knows -- see --binary-base in tools/solve.py.
#
# Which URL is used is a separate question from which bytes are correct.
# The sha256 is the identity and buck2 enforces it, so any mirror serving
# the same digest is interchangeable and none of them can corrupt a build.
# Redirection therefore belongs in configuration, and the two knobs below
# are the whole of it.

# A read-through, content-addressed cache, tried instead of upstream when
# set.  The sha256 leads, so re-pinning a package changes the URL and buck2
# cannot serve stale bytes from its download cache; the
# `?release=&location=` tail tells the cache where to look on a miss.
#
# No default, and that is the fix rather than an omission.  A default
# pointing at a localhost port meant a clone could not fetch anything until
# something was listening there, which made an unshipped local helper a
# silent build dependency -- the build did not fail saying so, it just
# could not download.  Set it in .buckconfig.local, which is gitignored.
_BLOB_BASE = read_config("buckos.fedora", "blob_base", "")

# Rewrites the prefix of the lockfile's recorded base, for a plain mirror
# of upstream's directory layout rather than a digest endpoint.  Lets a
# clone point at a nearer or an archived copy without regenerating -- which
# matters because a URL pin rots on upstream's schedule, not on this repo's:
# a release moves to archives.fedoraproject.org at EOL and the recorded
# base stops resolving even though the pins are still perfectly good.
_MIRROR_FROM = "https://dl.fedoraproject.org/pub/fedora/linux"
_MIRROR_BASE = read_config("buckos.fedora", "mirror_base", "")

# Percent-encodings for the characters an rpm filename can carry that a URL
# cannot take literally.  Two tables because the rules differ by position,
# and the difference is exactly where this went wrong:
#
#   `+` is a legal, literal character in a URL *path*, so Fedora serves
#   gcc-c++-15.3.1-1.fc43.x86_64.rpm at that name and escaping it there
#   would be noise.  In a *query string* it decodes to a space, so the same
#   filename passed as a parameter arrives as `gcc-c  -15.3.1...` -- a
#   request for a file that does not exist, from a name that looks right in
#   every log it appears in.  Fedora has a handful of these: gcc-c++,
#   libstdc++, libstdc++-devel, perl-Text-Tabs+Wrap.
#
#   `^` is not valid anywhere in a URL and has to be escaped in both.  rpm
#   uses it for post-release snapshots (1.0^git1), so it shows up in
#   versions rather than names and no pin carries one today -- but a
#   package picking up a snapshot build is an ordinary update, not an
#   exotic event, so it is handled rather than waited for.
_PATH_ESCAPES = {"^": "%5E", " ": "%20", "%": "%25", "#": "%23", "?": "%3F"}

_QUERY_ESCAPES = dict(_PATH_ESCAPES, **{"+": "%2B", "&": "%26", "=": "%3D"})

def _escape(text, table):
    out = ""
    for char in text.elems():
        out += table.get(char, char)
    return out

def _download_url(data, entry):
    """The one URL this pinned rpm is fetched from."""
    if _BLOB_BASE:
        return "{}/{}/{}?release={}&location={}".format(
            _BLOB_BASE,
            entry["sha256"],
            _escape(entry["location"].split("/")[-1], _PATH_ESCAPES),
            data.RELEASE,
            _escape(entry["location"], _QUERY_ESCAPES),
        )

    # `location` is relative to the repo the entry came from and says
    # nothing about which one that was, so the pin carries the repo name
    # and the lockfile carries that repo's base.  Not derivable from the
    # entry: an rpm fixed after GA has the same repo-relative path under
    # updates/ that its original has under releases/, so guessing from the
    # filename would fetch the GA tree and 404 on exactly the packages
    # that received a fix.
    repo = entry["repo"]
    base = data.REPO_BASE.get(repo, "")
    if not base:
        fail("fedora {}: lockfile records no base URL for repo {}, so there is nowhere to fetch {} from. Re-solve with --binary-base/--source-base, or set [buckos.fedora] blob_base.".format(
            data.RELEASE,
            repo if repo else "(unattributed)",
            entry["location"],
        ))
    if _MIRROR_BASE and base.startswith(_MIRROR_FROM):
        base = _MIRROR_BASE + base[len(_MIRROR_FROM):]
    return "{}/{}".format(base, _escape(entry["location"], _PATH_ESCAPES))

def fedora_rpm_downloads(data, suffix):
    """One http_file per pinned rpm: seed closure, image sets, source rpms.

    sha256 is passed through to http_file, so the pin in the lockfile is
    enforced by buck2 itself.  A mirror serving the wrong bytes fails the
    build here rather than silently producing a different distro.

    Deduplicated by target name, which is where the seed and the image
    closures meet.  They are separate answers to separate questions and
    each list stands alone, but a package in both -- glibc, bash, systemd
    -- is one rpm and must be one http_file, or buck2 rejects the package
    at parse time for defining the same target twice.
    """
    defined = {}
    for entry in data.SEED_RPMS:
        _rpm_download(data, entry, suffix, defined)

    for name in sorted(data.IMAGE_SETS):
        for entry in data.IMAGE_SETS[name]:
            _rpm_download(data, entry, suffix, defined)

    for entry in data.SOURCE_RPMS:
        _rpm_download(data, entry, suffix, defined)

def _rpm_download(data, entry, suffix, defined):
    name = entry["target"] + suffix
    if name in defined:
        # Same target name from a different pin would mean two rpms with
        # the same name-version-arch and different content, which is a
        # broken lockfile rather than something to quietly pick from.
        if defined[name] != entry["sha256"]:
            fail("{}: two different pins for the same rpm ({} and {})".format(
                name,
                defined[name],
                entry["sha256"],
            ))
        return
    defined[name] = entry["sha256"]
    native.http_file(
        name = name,
        urls = [_download_url(data, entry)],
        sha256 = entry["sha256"],
        visibility = ["PUBLIC"],
    )

def fedora_buildroots(release, suffix, data = None):
    """Define the host and binary-seed buildroots for one Fedora release.

    Called once per release with a `-<release>` suffix, and once more with
    an empty suffix for the default release's unsuffixed aliases.
    """

    # ── Development: the host's own rpm installation ─────────────────
    #
    # Not hermetic.  Consuming actions become local_only with cache upload
    # disabled (defs/buildroot_helpers.bzl), the same treatment buckos-build
    # gives host_tools_exec.  The dist tag deliberately reflects the *target*
    # release even though the host may be something else entirely -- so the
    # artifacts are labelled honestly and their mislabelling is visible
    # rather than silent.
    host_buildroot(
        name = "buildroot-host" + suffix,
        dist_tag = ".fc{}".format(release),
        target_cpu = "x86_64",
        visibility = ["PUBLIC"],
    )

    # ── Production: a pinned binary seed ─────────────────────────────
    #
    # This is where the dependency graph is cut (SPEC.md section 3a).  Every
    # rpm in seed_rpms is fetched by sha256 and is NOT built from source; the
    # size of this list is the repo's bootstrap debt, so it stays as close to
    # Fedora's @buildsys-build group as possible and grows only deliberately.
    #
    # This is also the answer to "does the build use the Fedora toolchain":
    # gcc, glibc, rpm, redhat-rpm-config and the rest all come out of this
    # list, at the exact versions that Fedora release shipped.  The host
    # buildroot above uses whatever the machine happens to have, which is a
    # different distro's toolchain wearing a .fc<release> dist tag.
    seed_rpms = []
    if data != None:
        seed_rpms = [
            ":" + entry["target"] + suffix
            for entry in data.SEED_RPMS
        ]

    seeded_buildroot(
        name = "buildroot-binary-seed" + suffix,
        dist_tag = ".fc{}".format(release),
        macros = "macros.buckos-distro",
        seed_rpms = seed_rpms,
        target_cpu = "x86_64",
        visibility = ["PUBLIC"],
    )

def fedora_buildroot_target(suffix):
    """The buildroot a release's packages build against.

    Pinned per release rather than left to `toolchains//:buildroot`, for
    two reasons.

    The release axis: the toolchain alias is a single global target, so
    every package in the graph would build against one release's
    buildroot no matter which release it belongs to -- gzip-43 compiled
    by Fedora 44's gcc and stamped .fc44.  That is exactly the "release
    as a global mode" failure defs/releases.bzl exists to prevent, and it
    is silent: the build succeeds and the artifact is mislabelled.

    And config visibility: buck2 resolves read_config per *cell*, and the
    toolchains cell has no .buckconfig of its own, so
    `[buckos.fedora] buildroot` set at the repo root is invisible there
    and the alias always fell back to "host".  Reading it here, in the
    cell that owns the setting, is what makes the setting mean anything.
    """
    provenance = read_config("buckos.fedora", "buildroot", "host")
    return ":buildroot-{}{}".format(provenance, suffix)

def fedora_packages(data, suffix):
    """One package() per recipe in the generated data.

    build_deps are left empty on purpose for now.  The lockfile records
    them as binary package names, and turning those into target labels
    needs the cycle staging in `staged` to be honoured -- xz and zlib-ng
    genuinely depend on each other, so a naive mapping produces a cycle
    buck2 rejects at parse time.  Until the staging is wired up, these
    packages build against the seed alone, which is what Fedora's own
    bootstrap does for the same set.
    """
    by_source = {entry["name"]: entry for entry in data.SOURCE_RPMS}
    buildroot = fedora_buildroot_target(suffix)

    for recipe in data.RECIPES:
        source = by_source[recipe["source_name"]]
        version, _, release = recipe["evr"].rpartition("-")
        # Drop any epoch: rpm writes "1:5.8.1" in metadata but %{version}
        # is just the version.
        _, _, version = version.rpartition(":")

        package(
            name = recipe["name"] + suffix,
            flavor = "fedora",
            buildroot = buildroot,
            srpm = ":" + source["target"] + suffix,
            source_name = recipe["source_name"],
            version = version,
            release = release,
            subpackages = recipe["subpackages"],
            visibility = ["PUBLIC"],
        )

def fedora_rootfs(data, suffix):
    """A rootfs installed from the release's pinned seed closure.

    The seed is a *build* closure, not the package set anyone would ship,
    so this is not a product image -- it is the mechanism under one, and
    the first thing that has ever asked rpm to run a real transaction over
    the solver's output.

    That makes it a test with teeth, in a way an image assembled from a
    hand-listed set would not be.  rpm's dependency check runs here (the
    rule's `nodeps` defaults off), so a seed the solver believes is closed
    but which is missing a Requires fails here with the capability named.
    Nothing else in the repo can catch that: the buildroot has no database
    to check against, so it accepts an incomplete seed and the gap surfaces
    much later as a compile error.
    """
    rootfs(
        name = "rootfs-seed" + suffix,
        buildroot = fedora_buildroot_target(suffix),
        rpms = [":" + entry["target"] + suffix for entry in data.SEED_RPMS],
        visibility = ["PUBLIC"],
    )

def fedora_image_rootfs(data, suffix):
    """A rootfs per named image set: the thing an ISO is actually made of.

    Same rule as rootfs-seed, different closure, and that is the whole
    point of having two.  rootfs-seed proves the mechanism against a list
    the solver derived for another purpose; these are the lists someone
    chose, closed over their runtime Requires, and they contain a kernel.
    """
    buildroot = fedora_buildroot_target(suffix)

    for name in sorted(data.IMAGE_SETS):
        rootfs(
            name = "rootfs-" + name + suffix,
            buildroot = buildroot,
            rpms = [
                ":" + entry["target"] + suffix
                for entry in data.IMAGE_SETS[name]
            ],
            visibility = ["PUBLIC"],
        )

def fedora_boot(data, suffix):
    """Kernel and initramfs per image set: what a bootloader loads.

    Only for sets that have a kernel, which in practice means the ones
    meant to boot.  A set without one still gets its targets defined --
    they fail when built, naming the set, rather than being silently
    absent, which is the difference between "this image does not boot" and
    "I cannot find the target that would tell me".
    """
    buildroot = fedora_buildroot_target(suffix)

    for name in sorted(data.IMAGE_SETS):
        rootfs_target = ":rootfs-" + name + suffix

        kernel_image(
            name = "kernel-" + name + suffix,
            rootfs = rootfs_target,
            visibility = ["PUBLIC"],
        )

        initramfs(
            name = "initramfs-" + name + suffix,
            buildroot = buildroot,
            rootfs = rootfs_target,
            add_modules = _INITRAMFS_MODULES.get(name, []),
            visibility = ["PUBLIC"],
        )

# ── Per-release fan-out ──────────────────────────────────────────────
#
# Each release gets a `-<release>` suffix, and the default release gets a
# second, unsuffixed copy so callers that do not care which Fedora they
# get keep working.  The three passes are separate because Buck needs a
# target defined before it is referenced: downloads, then the buildroot
# that consumes them, then the packages that build against it.

def _missing(release):
    fail(
        "fedora release {} is in `[buckos.fedora] releases` but has no ".format(release) +
        "generated data. Solve and generate it:\n" +
        "    tools/solve.py --release {}\n".format(release) +
        "    tools/generate.py flavors/fedora/lock/fedora-{}.lock.json".format(release),
    )

def _data_for(data_by_release, release):
    data = data_by_release.get(release)
    if data == None:
        _missing(release)
    return data

def fedora_downloads_for(releases, default, data_by_release):
    """http_file targets for every pinned rpm, per release."""
    for release in releases:
        fedora_rpm_downloads(
            _data_for(data_by_release, release),
            release_suffix(release),
        )

    fedora_rpm_downloads(_data_for(data_by_release, default), "")

def fedora_buildroots_for(releases, default, data_by_release = None):
    """Define buildroots for every release, plus the unsuffixed default."""
    data_by_release = data_by_release or {}

    for release in releases:
        fedora_buildroots(
            release,
            release_suffix(release),
            data_by_release.get(release),
        )

    fedora_buildroots(default, "", data_by_release.get(default))

def fedora_packages_for(releases, default, data_by_release):
    """package() calls for every recipe, per release."""
    for release in releases:
        fedora_packages(
            _data_for(data_by_release, release),
            release_suffix(release),
        )

    fedora_packages(_data_for(data_by_release, default), "")

def fedora_rootfs_for(releases, default, data_by_release):
    """rootfs and boot targets for every release, plus the default.

    Boot targets are defined in the same pass, after the rootfs they read:
    Buck needs a target to exist before it is referenced, and
    kernel-<set> / initramfs-<set> both take :rootfs-<set>.
    """
    for release in releases:
        data = _data_for(data_by_release, release)
        suffix = release_suffix(release)
        fedora_rootfs(data, suffix)
        fedora_image_rootfs(data, suffix)
        fedora_boot(data, suffix)

    default_data = _data_for(data_by_release, default)
    fedora_rootfs(default_data, "")
    fedora_image_rootfs(default_data, "")
    fedora_boot(default_data, "")
