"""Fedora buildroot and package definitions, one set per release.

Lives in a .bzl rather than inline in BUCK because the BUCK dialect allows
neither `def` nor top-level `if`, and defining a release's targets is a
loop over a config-driven list that needs both.

This file is the handwritten half of the generate step: `generated/*.bzl`
holds pure data produced by tools/generate.py, and the macros here turn it
into targets.  Keeping the logic out of the generated file is what makes
the generated diff reviewable.
"""

load("//defs:flavor.bzl", "package", "subpackage_target")
load("//defs:releases.bzl", "release_suffix")
load("//defs/rules/boot.bzl", "initramfs", "kernel_image")
load("//defs/rules/buildroot.bzl", "host_buildroot", "seeded_buildroot")
load("//defs/rules/image.bzl", "iso_image", "squashfs")
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

# Image sets that describe a set of *tools* rather than something to boot.
#
# They go through the solver for the same reason the bootable sets do --
# it is the only thing that closes a package list over its Requires -- but
# what comes out the other end is a buildroot, not a rootfs.  An ISO built
# with the host's xorriso and the host's mksquashfs is not reproducible,
# and "which mksquashfs" is as much a build input as any rpm in the image.
#
# Listed rather than inferred: nothing about a package list says whether
# it is meant to boot.  A set left off this list silently gets kernel and
# initramfs targets it can never satisfy, so the failure would surface as
# a confusing dracut error rather than as "this set has no kernel".
_TOOL_SETS = ["image-tools"]

# Kernel command line for a live ISO.
#
# root=live:CDLABEL=<label> is what sends dracut's dmsquash-live module
# looking for the squashfs, and the label has to match the ISO's volume id
# exactly or the initramfs waits for a device that never appears.  The rule
# derives it from volume_label for that reason rather than taking both.
#
# selinux=0 is load-bearing, not a convenience.  The image carries
# selinux-policy-targeted, so a kernel with SELinux enabled loads that
# policy and enforces it -- against a filesystem with no labels at all.
#
# Nothing in this build can label it.  setxattr("security.selinux")
# returns EPERM inside a nested user namespace, which is where every stage
# here runs -- even under `unshare -Ur`, where a user.* xattr on the same
# file succeeds.  Not a blanket rule about security.*, and the exception
# is the interesting part: security.capability *is* settable from a user
# namespace through the kernel's v3 format, which is why these images
# carry real file capabilities on arping, clockdiff, newuidmap and
# newgidmap.  The kernel extends that to capabilities and withholds it
# from labels, so rpm-plugin-selinux sets no context, mksquashfs has none
# to carry, and a live rootfs comes out with zero labels tree-wide.
#
# What that costs is not subtle.  systemd fails every labelling call and
# gives up before it starts a single unit:
#
#   systemd[1]: Failed to set SELinux security context
#               system_u:object_r:systemd_unit_file_t:s0 for /run/systemd/units:
#               Permission denied
#   systemd[1]: Failed to allocate manager object: Permission denied
#   systemd[1]: Freezing execution.
#
# enforcing=0 would also boot, but it leaves the policy loaded and every
# file unlabeled_t, which trades a clean failure for a running system that
# lies about being confined.  Turning SELinux off says what is true.
# See the Limitations section of the README for what fixing it would take.
#
# The serial console is deliberate and stays in the shipped cmdline.  The
# only way this repo can answer "does the image boot" is a headless VM, so
# the console that carries the answer is part of the product, not part of
# a debugging session.  `quiet` is the opposite: it hid the systemd
# failure above behind a frozen splash, and the diagnosis started by
# taking it back out.
_LIVE_KERNEL_ARGS = "rd.live.image selinux=0 console=tty0 console=ttyS0,115200"

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
    """One package() per recipe, staged where the lockfile says to stage.

    A recipe's build_deps arrive as *binary* package names -- "xz-libs",
    not a label -- so the work here is turning each one into the target
    that produces it, and doing that without creating a cycle buck2 will
    reject at parse time.  gzip, xz and zlib-ng genuinely require each
    other to build, so there is no ordering of three plain targets that
    works.

    tools/solve.py already decided how to break it, and this honours that
    decision rather than re-deriving it: each cycle member is built once
    per stage, every stage against the previous one's output, with stage 1
    against the pinned upstream binaries in the seed.  Only the stage
    marked `ships` is what the plain target name means, so `:gzip-43` is
    an alias for `:gzip-stage3-43` and nothing outside this function needs
    to know the package was staged at all.

    A dep this repo does not build resolves to nothing on purpose: it is
    already in the buildroot tree rpmbuild runs inside, installed there
    from the same pinned rpm, and adding a label for it would be a second
    copy of a package that is already present.
    """
    by_source = {entry["name"]: entry for entry in data.SOURCE_RPMS}
    buildroot = fedora_buildroot_target(suffix)

    # Which recipe produces a given binary package.  Built from the
    # subpackage lists, which are the binary names rpm will emit.
    provider = {}
    for recipe in data.RECIPES:
        for sub in recipe["subpackages"]:
            provider[sub] = recipe

    # source -> the variant that ships; and (source, stage) -> its target.
    # Looked up rather than formatted, so the naming stays the solver's to
    # choose.
    ships = {}
    stage_target = {}
    for entry in data.STAGED:
        stage_target[_stage_key(entry["source"], entry["stage"])] = entry["target"]
        if entry["ships"]:
            ships[entry["source"]] = entry["target"]

    # The default answer to "which variant of this source do I depend on":
    # its shipping stage if it is staged, else the package itself.
    default_variant = {}
    for recipe in data.RECIPES:
        default_variant[recipe["name"]] = ships.get(recipe["name"], recipe["name"])

    staged_sources = {entry["source"]: True for entry in data.STAGED}

    for recipe in data.RECIPES:
        if recipe["name"] in staged_sources:
            continue
        _fedora_one_package(
            data, suffix, buildroot, by_source, recipe,
            recipe["name"],
            _variant_map(default_variant, recipe["name"], {}, ""),
            provider,
        )

    for entry in data.STAGED:
        recipe = _recipe_named(data, entry["source"])
        cycle_deps = {name: True for name in entry["cycle_deps"]}
        _fedora_one_package(
            data, suffix, buildroot, by_source, recipe,
            entry["target"],
            _variant_map(
                default_variant,
                # Excluded, not remapped: a package's own earlier stage is
                # reached through cycle_deps if the solver put it there,
                # and letting it fall through to the shipping variant
                # would make stage 1 depend on stage 3.
                entry["source"],
                cycle_deps,
                entry["cycle_deps_from"],
                stage_target,
            ),
            provider,
        )

    # The stable names.  Everything downstream -- an image set, another
    # package's build_deps, a person typing a target -- says `gzip-43` and
    # gets whichever stage the solver marked as shipping.
    for recipe in data.RECIPES:
        shipping = ships.get(recipe["name"])
        if shipping == None:
            continue
        native.alias(
            name = recipe["name"] + suffix,
            actual = ":" + shipping + suffix,
            visibility = ["PUBLIC"],
        )
        for sub in recipe["subpackages"]:
            native.alias(
                name = subpackage_target(
                    recipe["name"] + suffix, recipe["source_name"], sub,
                ),
                actual = ":" + subpackage_target(
                    shipping + suffix, recipe["source_name"], sub,
                ),
                visibility = ["PUBLIC"],
            )

    # One probe per source package, whatever staging did to it.  What a
    # spec BuildRequires is a property of the spec, so probing every stage
    # would be asking one question three times.
    #
    # Stage 1 is the one asked, not the shipping stage, because the answer
    # feeds the solver and the solver's world is the pinned seed -- stage 1
    # is the variant that builds against it.  Later stages build against
    # packages whose existence the answer is supposed to justify.
    for recipe in data.RECIPES:
        source = recipe["name"]
        variant = stage_target.get(_stage_key(source, 1), source)
        native.alias(
            name = "probe-" + source + suffix,
            actual = ":" + variant + suffix + "-buildrequires",
            visibility = ["PUBLIC"],
        )

def _stage_key(source, stage):
    return "{}/stage{}".format(source, stage)

def _recipe_named(data, name):
    for recipe in data.RECIPES:
        if recipe["name"] == name:
            return recipe
    fail("STAGED names source package {}, which has no recipe".format(name))

def _variant_map(default_variant, exclude, cycle_deps, from_stage, stage_target = {}):
    """Which variant of each built source package to depend on.

    `cycle_deps` are the ones the solver staged; they resolve to the stage
    named by `from_stage`, or -- when that is "seed" -- to nothing at all,
    which leaves the pinned upstream copy already in the buildroot to
    satisfy them.  That is what makes stage 1 buildable.
    """
    out = {}
    for source, variant in default_variant.items():
        if source == exclude:
            continue
        if source in cycle_deps:
            if from_stage == "seed":
                continue
            key = "{}/{}".format(source, from_stage)
            if key not in stage_target:
                fail("no staged target for {} at {}".format(source, from_stage))
            out[source] = stage_target[key]
        else:
            out[source] = variant
    return out

def _fedora_one_package(
        data,
        suffix,
        buildroot,
        by_source,
        recipe,
        target_name,
        variant,
        provider):
    """One package() call, with build_deps resolved to subpackage labels."""
    source = by_source[recipe["source_name"]]
    version, _, release = recipe["evr"].rpartition("-")

    # Drop any epoch: rpm writes "1:5.8.1" in metadata but %{version} is
    # just the version.
    _, _, version = version.rpartition(":")

    build_deps = []
    for binary in recipe["build_deps"]:
        producer = provider.get(binary)
        if producer == None:
            # Not built by this repo, so it is in the seed already.
            continue
        chosen = variant.get(producer["name"])
        if chosen == None:
            # Excluded by _variant_map: self-reference, or a cycle dep this
            # stage deliberately takes from the seed.
            continue
        build_deps.append(":" + subpackage_target(
            chosen + suffix, producer["source_name"], binary,
        ))

    package(
        name = target_name + suffix,
        flavor = "fedora",
        buildroot = buildroot,
        srpm = ":" + source["target"] + suffix,
        source_name = recipe["source_name"],
        version = version,
        release = release,
        build_deps = sorted(build_deps),
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
        if name in _TOOL_SETS:
            continue
        rootfs(
            name = "rootfs-" + name + suffix,
            buildroot = buildroot,
            rpms = [
                ":" + entry["target"] + suffix
                for entry in data.IMAGE_SETS[name]
            ],
            visibility = ["PUBLIC"],
        )

def fedora_image_tools(data, suffix):
    """A buildroot per tool set: what assembles an image, not what boots.

    seeded_buildroot rather than rootfs because the consumer is an action,
    not a bootloader.  It wants a tree it can chroot into and run
    mksquashfs and xorriso from, with no rpm database and no scriptlets --
    which is precisely the difference defs/rules/rootfs.bzl's docstring
    draws between the two, read from the other side.

    Being a seeded buildroot also makes it hermetic, so the image rules
    that consume it are RE-eligible and cacheable.  A rule reaching for the
    host's /usr/bin/xorriso would be neither.
    """
    for name in sorted(data.IMAGE_SETS):
        if name not in _TOOL_SETS:
            continue
        seeded_buildroot(
            name = "buildroot-" + name + suffix,
            seed_rpms = [
                ":" + entry["target"] + suffix
                for entry in data.IMAGE_SETS[name]
            ],
            target_cpu = "x86_64",
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
        if name in _TOOL_SETS:
            continue
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

def fedora_images(data, release, suffix):
    """squashfs and ISO per bootable image set.

    The squashfs is separate from the ISO rather than folded into it
    because it is the expensive half -- compressing a whole root
    filesystem -- and it does not change when the kernel command line or
    the volume label does.  Fusing them would recompress the rootfs on
    every ISO tweak, which is the same "split on what this actually costs"
    argument defs/rules/boot.bzl makes for kernel_image and initramfs.

    Both run in the image-tools buildroot, not the package buildroot: they
    need mksquashfs and xorriso, which are not in @buildsys-build and have
    no business being added to it.
    """
    tools = ":buildroot-image-tools" + suffix

    for name in sorted(data.IMAGE_SETS):
        if name in _TOOL_SETS:
            continue

        squashfs(
            name = "squashfs-" + name + suffix,
            buildroot = tools,
            rootfs = ":rootfs-" + name + suffix,
            visibility = ["PUBLIC"],
        )

        # Uppercase because the volume id is what ends up in the kernel
        # command line as CDLABEL=, and genisoimage-style volume ids are
        # upper-cased by the filesystem -- a lowercase label here would be
        # written uppercase and then not match at boot.
        label = "FEDORA-{}-{}".format(release, name.upper())

        iso_image(
            name = "iso-" + name + suffix,
            buildroot = tools,
            kernel = ":kernel-" + name + suffix,
            initramfs = ":initramfs-" + name + suffix,
            squashfs = ":squashfs-" + name + suffix,
            volume_label = label,
            kernel_args = _LIVE_KERNEL_ARGS,
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
        fedora_image_tools(data, suffix)
        fedora_boot(data, suffix)
        fedora_images(data, release, suffix)

    default_data = _data_for(data_by_release, default)
    fedora_rootfs(default_data, "")
    fedora_image_rootfs(default_data, "")
    fedora_image_tools(default_data, "")
    fedora_boot(default_data, "")
    fedora_images(default_data, default, "")
