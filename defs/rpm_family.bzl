"""RPM-family buildroot and package definitions, one set per release.

Lives in a .bzl rather than inline in BUCK because the BUCK dialect allows
neither `def` nor top-level `if`, and defining a release's targets is a
loop over a config-driven list that needs both.

This file is the handwritten half of the generate step: `generated/*.bzl`
holds pure data produced by tools/generate.py, and the macros here turn it
into targets.  Keeping the logic out of the generated file is what makes
the generated diff reviewable.
"""

load("//defs:flavor.bzl", "package", "subpackage_rpm_target", "subpackage_target")
load(
    "//defs:architectures.bzl",
    "ARCHITECTURES",
    "DEFAULT_ARCHITECTURE",
    "execution_compatible_with",
    "release_arch_suffix",
    "target_platform",
)
load("//defs:releases.bzl", "iso_volume_label", "release_suffix")
load("//defs/rules:boot.bzl", "initramfs", "kernel_image")
load("//defs/rules:buildroot.bzl", "host_buildroot", "seeded_buildroot")
load("//defs/rules:srpm.bzl", "prebuilt_rpm")
load("//defs/rules:image.bzl", "iso_image", "squashfs")
load(
    "//defs/rules:kernel.bzl",
    "configured_kernel_set",
    "kernel_rootfs",
)
load("//defs/rules:rootfs.bzl", "rootfs")
load("//defs/rules:signing.bzl", "ima_manifest")

_RPM_FLAVORS = {
    "centos": {
        # Keep the release component in the rewritten suffix so one
        # mirror root can serve every configured Stream release.
        "mirror_from": "https://mirror.stream.centos.org",
        "supplier": "Organization: CentOS",
    },
    "centos-hyperscale": {
        # The Stream and SIG repositories share this root. EPEL URLs are
        # deliberately unaffected by a CentOS mirror rewrite.
        "mirror_from": "https://mirror.stream.centos.org",
        "supplier": "Organization: CentOS",
    },
    "fedora": {
        "mirror_from": "https://dl.fedoraproject.org/pub/fedora/linux",
        "supplier": "Organization: Fedora Project",
    },
}

def _flavor_config(flavor):
    config = _RPM_FLAVORS.get(flavor)
    if config == None:
        fail("unsupported RPM-family flavor: {}".format(flavor))
    return config

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

# What mksquashfs needs to compile that @buildsys-build does not already
# provide.  One entry, and it is the header zstd_wrapper.c includes; see
# the buildroot-squashfs-tools comment in rpm_buildroots_for.
#
# These names come out of the pin table, not out of thin air: the pin
# exists because something in the build set already build-requires it. If
# that consumer ever leaves the set the pin goes with it and the buildroot
# fails at analysis on a missing target, which is the right failure but
# not an obvious one.
_SQUASHFS_COMPILE_SEED = {"libzstd-devel": True}

# Every bootable image set is built twice, from the same package list.
#
# "" takes each package from the source build that produces it wherever a
# recipe exists, which is what this repo is for and what an unsuffixed
# target means.  "-prebuilt" takes the whole set from the pinned upstream
# binaries instead.
#
# The second is worth a target rather than a config switch because the two
# are most useful side by side: the same image built both ways is the only
# direct evidence that replaying a distro's sources reproduces the distro,
# and the pinned image is the thing to boot when a source build breaks
# somewhere unrelated to what is being tested.  A switch would make them
# alternatives; targets make them comparable in one graph.
#
# Order matters only in that "" is first, so the source-built image is
# what a reader meets before the fallback.
_IMAGE_VARIANTS = ["", "-prebuilt"]

# Kernel command line for a live ISO.
#
# root=live:CDLABEL=<label> is what sends dracut's dmsquash-live module
# looking for the squashfs, and the label has to match the ISO's volume id
# exactly or the initramfs waits for a device that never appears.  The rule
# derives it from volume_label for that reason rather than taking both.
#
# SELinux is enforcing, and getting there took building the labels
# ourselves.  The image carries selinux-policy-targeted, so a kernel with
# SELinux on loads that policy and enforces it -- and for a long time it
# enforced against a filesystem with no labels at all, because
# setxattr("security.selinux") is EPERM inside a nested user namespace and
# every stage of this build runs in one.  systemd then failed every
# labelling call and gave up before starting a unit:
#
#   systemd[1]: Failed to set SELinux security context
#               system_u:object_r:systemd_unit_file_t:s0 for /run/systemd/units:
#               Permission denied
#   systemd[1]: Failed to allocate manager object: Permission denied
#   systemd[1]: Freezing execution.
#
# so the cmdline carried selinux=0.  It no longer does.  The labels are
# now written directly into the squashfs by mksquashfs, from contexts the
# image computes with its own policy -- no setxattr(2) anywhere, so the
# namespace never comes into it.  tools/squashfs_build.py has the
# mechanism; squashfs(selinux_relabel = True) below turns it on.
#
# Measured on the built image rather than assumed: enforcing=1, policy
# loaded in 78ms, PID 1 running as system_u:system_r:init_t, zero AVC
# denials, login prompt reached.
#
_LIVE_KERNEL_ARGS = "rd.live.image console=tty0"

def _ima_signing_key():
    """Configured signing identity, or empty when IMA images are disabled."""
    return read_config("buckos.security", "ima_signing_key", "")

def _ima_signing_mode():
    return read_config("buckos.security", "ima_signing_mode", "all")

def _ima_kernel_args():
    return read_config(
        "buckos.security",
        "ima_kernel_args",
        "ima_appraise=enforce ima_policy=appraise_tcb",
    )

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
# Redirection therefore belongs in configuration.

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
# A static content-addressed HTTP store. The full digest is required in the
# template so different pins cannot resolve to the same URL. Optional release
# and filename components are escaped for use in URL paths.

# Rewrites the prefix of the lockfile's recorded base, for a plain mirror
# of upstream's directory layout rather than a digest endpoint.  Lets a
# clone point at a nearer or an archived copy without regenerating -- which
# matters because a URL pin rots on upstream's schedule, not on this repo's:
# a release moves to archives.fedoraproject.org at EOL and the recorded
# base stops resolving even though the pins are still perfectly good.
# Percent-encodings for the characters an rpm filename can carry that a URL
# cannot take literally.  Two tables because the rules differ by position,
# and the difference is exactly where this went wrong:
#
#   `+` is a legal, literal character in a URL *path*, so Fedora serves
#   gcc-c++-15.3.1-1.fc45.x86_64.rpm at that name and escaping it there
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

def _filename_parts(filename):
    if filename.endswith(".src.rpm"):
        return filename[:-len(".src.rpm")], ".src.rpm"
    if "." in filename:
        parts = filename.rsplit(".", 1)
        return parts[0], "." + parts[1]
    return filename, ""

def _render_package_url(flavor, template, data, entry):
    if "{sha256}" not in template:
        fail("[buckos.{}] package_url_template must contain {{sha256}}".format(flavor))

    filename = entry["location"].split("/")[-1]
    stem, extension = _filename_parts(filename)
    replacements = {
        "{ext}": _escape(extension, _PATH_ESCAPES),
        "{filename}": _escape(filename, _PATH_ESCAPES),
        "{release}": _escape(data.RELEASE, _PATH_ESCAPES),
        "{sha256}": entry["sha256"],
        "{sha256_12}": entry["sha256"][:12],
        "{stem}": _escape(stem, _PATH_ESCAPES),
    }

    remaining = template
    for placeholder in replacements:
        remaining = remaining.replace(placeholder, "")
    if "{" in remaining or "}" in remaining:
        fail("[buckos.{}] package_url_template contains an unsupported placeholder: {}".format(flavor, template))

    url = template
    for placeholder, value in replacements.items():
        url = url.replace(placeholder, value)
    return url

def _download_url(flavor, data, entry):
    """The one URL this pinned rpm is fetched from."""
    config = _flavor_config(flavor)
    section = "buckos." + flavor
    blob_base = read_config(section, "blob_base", "")
    mirror_base = read_config(section, "mirror_base", "")
    package_url_template = read_config(section, "package_url_template", "")

    if package_url_template:
        if blob_base or mirror_base:
            fail("[{}] package_url_template cannot be combined with blob_base or mirror_base".format(section))
        return _render_package_url(flavor, package_url_template, data, entry)

    if blob_base:
        return "{}/{}/{}?release={}&location={}".format(
            blob_base,
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
        fail("{} {}: lockfile records no base URL for repo {}, so there is nowhere to fetch {} from. Re-solve with --binary-base/--source-base, or set [{}] package_url_template or blob_base.".format(
            flavor,
            data.RELEASE,
            repo if repo else "(unattributed)",
            entry["location"],
            section,
        ))
    mirror_from = config["mirror_from"]
    if mirror_base and base.startswith(mirror_from):
        base = mirror_base + base[len(mirror_from):]
    return "{}/{}".format(base, _escape(entry["location"], _PATH_ESCAPES))

def rpm_downloads(flavor, data, suffix, platform):
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
        _rpm_download(flavor, data, entry, suffix, defined, platform)

    for entry in getattr(data, "VARIANT_SEED_RPMS", []):
        _rpm_download(flavor, data, entry, suffix, defined, platform)

    for name in sorted(data.IMAGE_SETS):
        for entry in data.IMAGE_SETS[name]:
            _rpm_download(flavor, data, entry, suffix, defined, platform)

    for entry in data.SOURCE_RPMS:
        _rpm_download(flavor, data, entry, suffix, defined, platform)

def _rpm_download(flavor, data, entry, suffix, defined, platform):
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
        urls = [_download_url(flavor, data, entry)],
        sha256 = entry["sha256"],
        default_target_platform = platform,
        visibility = ["PUBLIC"],
    )

def rpm_buildroots(flavor, release, suffix, platform, exec_constraints, data = None):
    """Define the host and binary-seed buildroots for one RPM release.

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
    dist_tag = data.DIST_TAG if data != None else ""
    target_cpu = data.TARGET_CPU if data != None else "x86_64"

    host_buildroot(
        name = "buildroot-host" + suffix,
        dist_tag = dist_tag,
        target_cpu = target_cpu,
        default_target_platform = platform,
        visibility = ["PUBLIC"],
    )

    # ── Production: a pinned binary seed ─────────────────────────────
    #
    # This is where the dependency graph is cut (SPEC.md section 3a).  Every
    # rpm in seed_rpms is fetched by sha256 and is NOT built from source; the
    # size of this list is the repo's bootstrap debt, so it stays as close to
    # the upstream build-system group as possible and grows only deliberately.
    #
    # This is also the answer to "does the build use the target toolchain":
    # gcc, glibc, rpm, redhat-rpm-config and the rest all come out of this
    # list, at the exact versions that release shipped.  The host
    # buildroot above uses whatever the machine happens to have, which is a
    # different distro's toolchain wearing the target release's dist tag.
    #
    # BASE_SEED, not every rpm in the pin table.  The table is the union of
    # what every package needs; the buildroot is @buildsys-build closed over
    # its Requires, and each package overlays its own extras on top (see
    # _rpm_one_package).  Handing every package the union is how libmnl got
    # a doxygen its spec never asked for and emitted files its %files does
    # not list -- tools/solve.py's base_seed comment has the whole argument.
    seed_rpms = []
    if data != None:
        base = {name: True for name in data.BASE_SEED}
        seed_rpms = [
            ":" + entry["target"] + suffix
            for entry in data.SEED_RPMS
            if entry["name"] in base
        ]

    seeded_buildroot(
        name = "buildroot-binary-seed" + suffix,
        dist_tag = dist_tag,
        macros = "//defs:macros.buckos-distro",
        seed_rpms = seed_rpms,
        target_cpu = target_cpu,
        default_target_platform = platform,
        exec_compatible_with = exec_constraints,
        visibility = ["PUBLIC"],
    )

    # ── The one buildroot that has to host a compile ─────────────────
    #
    # rpm_images routes Enterprise Linux 9's squashfs into a buildroot and
    # builds mksquashfs from source there, because the packaged tool cannot
    # write per-file xattrs.  That makes this the only image action whose
    # buildroot must satisfy a *compile* rather than run a packaged tool,
    # and nothing in the seed was ever asked to.
    #
    # The base carries gcc, make and glibc's headers, so XATTR_SUPPORT is
    # already satisfied -- squashfs-tools' xattr.c reaches for sys/xattr.h
    # and nothing outside glibc.  zstd_wrapper.c includes <zstd.h>, and
    # ZSTD_SUPPORT cannot be turned off: tools/squashfs_build.py compiles
    # with COMP_DEFAULT=zstd and the images are zstd-compressed, so a tool
    # without it cannot write them.
    #
    # A separate buildroot rather than a wider base.  Putting libzstd-devel
    # in BASE_SEED would hand a header to every package in the flavor to
    # serve one compile, which is the union the comment above exists to
    # prevent.
    #
    # Defined for every release, not only the ones that route to it: the
    # pin is present in all twelve RPM-family locks, and a definition that
    # appears and disappears with a condition is harder to find than an
    # unused target is to ignore.
    compile_seed = seed_rpms
    if data != None:
        compile_seed = seed_rpms + [
            ":" + entry["target"] + suffix
            for entry in data.SEED_RPMS
            if entry["name"] in _SQUASHFS_COMPILE_SEED
        ]

    seeded_buildroot(
        name = "buildroot-squashfs-tools" + suffix,
        dist_tag = dist_tag,
        macros = "//defs:macros.buckos-distro",
        seed_rpms = compile_seed,
        target_cpu = target_cpu,
        default_target_platform = platform,
        exec_compatible_with = exec_constraints,
        visibility = ["PUBLIC"],
    )

def rpm_buildroot_target(flavor, suffix):
    """The buildroot a release's packages build against.

    Pinned per release rather than left to `//:buildroot`. The toolchain
    alias is a single global target, so
    every package in the graph would build against one release's
    buildroot no matter which release it belongs to. That is exactly the "release
    as a global mode" failure defs/releases.bzl exists to prevent, and it
    is silent: the build succeeds and the artifact is mislabelled.
    """
    provenance = read_config("buckos." + flavor, "buildroot", "host")
    return ":buildroot-{}{}".format(provenance, suffix)

def _binary_providers(recipes, skip):
    """Map binary names to their normal source recipes.

    Version variants are build-dependency-only alternatives. They remain
    addressable through dep_variants, but must never replace the normal
    producer selected for an ordinary build dependency or live image.
    """
    provider = {}
    for recipe in recipes:
        if recipe.get("variant_of") or recipe["name"] in skip:
            continue
        for subpackage in recipe["subpackages"]:
            provider[subpackage] = recipe
    return provider

def rpm_packages(flavor, data, suffix, platform, exec_constraints):
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
    buildroot = rpm_buildroot_target(flavor, suffix)

    # The prebuilt trees recipes overlay.  Defined here rather than from
    # the fan-out so it cannot be called for one release and forgotten for
    # another -- every path that defines packages needs them.
    rpm_seed_installroots(data, suffix)

    # binary package name -> its pinned download, for the overlay half.
    base_names = {name: True for name in data.BASE_SEED}
    seed_rpm = {
        entry["name"]: entry
        for entry in data.SEED_RPMS
        if entry["name"] not in base_names
    }

    # Which recipe produces a given binary package.  Built from the
    # subpackage lists, which are the binary names rpm will emit.
    # Version variants are excluded: a variant builds the same binary names
    # as the package it varies, so letting it into this map would reroute
    # every consumer instead of the ones that asked.  Consumers that want it
    # name it in dep_variants, which _rpm_one_package consults first.
    # Packages this host cannot build.  Left out of the map that answers
    # "who builds this binary", which is all it takes: every consumer's
    # lookup then misses and falls through to the pinned rpm already in the
    # seed, and rpm_image_rootfs does the same for the image.  The targets
    # are still defined -- an unreferenced target costs nothing, and
    # deleting them would dangle anything that names one directly.
    prebuilt = prebuilt_sources(flavor)
    skip = {name: True for name in prebuilt}
    if prebuilt:
        print(("buckos-distro: WARNING: {} come from upstream binaries, not " +
               "from source -- [buckos.{}] prebuilt selected their pinned " +
               "providers. The " +
               "image is still complete; its provenance is not.").format(
            ", ".join(prebuilt), flavor))

    provider = _binary_providers(data.RECIPES, skip)
    variant_recipe = {}
    for recipe in data.RECIPES:
        if recipe.get("variant_of"):
            variant_recipe[recipe["name"]] = recipe

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
    # Variants included here even though they are kept out of `provider`:
    # this map answers "which build of <recipe> do I depend on", and a
    # variant that lands in a cycle is staged like anything else.  Only the
    # binary-to-recipe lookup has to exclude them.
    default_variant = {}
    for recipe in data.RECIPES:
        default_variant[recipe["name"]] = ships.get(recipe["name"], recipe["name"])

    staged_sources = {entry["source"]: True for entry in data.STAGED}

    for recipe in data.RECIPES:
        if recipe["name"] in staged_sources:
            continue
        _rpm_one_package(
            flavor, data, suffix, buildroot, by_source, recipe,
            recipe["name"],
            _variant_map(default_variant, recipe["name"], {}, ""),
            provider,
            seed_rpm,
            variant_recipe,
            platform,
            exec_constraints,
        )

    for entry in data.STAGED:
        recipe = _recipe_named(data, entry["source"])
        cycle_deps = {name: True for name in entry["cycle_deps"]}
        _rpm_one_package(
            flavor, data, suffix, buildroot, by_source, recipe,
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
            seed_rpm,
            variant_recipe,
            platform,
            exec_constraints,
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
            default_target_platform = platform,
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
                default_target_platform = platform,
                visibility = ["PUBLIC"],
            )
            # The rpm file gets the same treatment, so an image set asking
            # for a built package by name reaches the stage that ships
            # rather than whichever one happens to be named first.
            native.alias(
                name = subpackage_rpm_target(
                    recipe["name"] + suffix, recipe["source_name"], sub,
                ),
                actual = ":" + subpackage_rpm_target(
                    shipping + suffix, recipe["source_name"], sub,
                ),
                default_target_platform = platform,
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
            default_target_platform = platform,
            visibility = ["PUBLIC"],
        )

def prebuilt_sources(flavor):
    """Source packages to take from upstream instead of building.

        [buckos.fedora]
          prebuilt = bash

    A local diagnostic or recovery override for a source recipe already
    present in the generated graph.  It removes that source from the
    provider map so consumers use the pinned RPM instead.  It cannot add a
    recipe that the source policy excluded before generation.

    Configuration rather than a committed default, and local rather than
    solved.  The lockfile has to stay host-independent -- it is reviewed as
    a diff and must describe the same distro everywhere -- so this belongs
    at the build layer, where it changes which target satisfies a
    dependency and nothing about what was pinned.

    Not silent.  Every selected package comes from upstream rather than the
    available recipe, so rpm_packages says so on every evaluation.
    """
    raw = read_config("buckos." + flavor, "prebuilt", "")
    return sorted([name.strip() for name in raw.split(",") if name.strip()])

def _disabled_bconds(flavor):
    """Per-package `--without` from local config, as {source: [bcond]}.

        [buckos.fedora]
          without = gmp:fips, nettle:fipshmac

    Configuration rather than a table in this file, because what it is for
    is a property of the *build host* rather than of the distro.  The case
    it exists for: libkcapi's fipshmac opens a NETLINK_CRYPTO socket to ask
    the kernel about an algorithm, and a kernel built without
    CONFIG_CRYPTO_USER answers EPROTONOSUPPORT -- which surfaces as

        Allocation of hmac(sha256) cipher failed (ret=-93)

    in %install, for gmp, nettle and libxcrypt.  Nothing to do with the
    sandbox: the same binary fails the same way run directly on the host,
    and the AF_ALG socket it actually hashes with binds fine.  A stock
    Fedora kernel enables CONFIG_CRYPTO_USER and needs none of this, so
    defaulting to it here would ship a distro without FIPS integrity
    hashes to work around one machine.

    libxcrypt cannot be helped this way and is not worth pretending
    otherwise: it calls fipshmac from %__spec_install_post with no bcond
    guarding it, and the spec even says why a %global will not work.  On a
    host without CONFIG_CRYPTO_USER that package does not build.
    """
    raw = read_config("buckos." + flavor, "without", "")
    out = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" not in item:
            fail("[buckos.{}] without expects source:bcond entries, got {}".format(flavor, item))
        source, bcond = item.split(":", 1)
        out.setdefault(source.strip(), []).append(bcond.strip())
    return out

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

def seed_installroot_target(entry, suffix):
    """Target name for a prebuilt binary package unpacked as an installroot.

    Distinct from the `rpm-*` http_file that downloads it: that is a file,
    and what a build overlays is a tree.

    Keyed on that download's target rather than on the binary package name,
    which it used to be.  The name is not unique once a version variant is
    in the graph: acl builds twice in Fedora 43, so the pin table carries
    libacl-devel at both 2.4.0 and 2.3.2, and one installroot per name gave
    whichever the loop saw last to everybody.
    """
    return "seedroot-" + entry["target"] + suffix

def rpm_seed_installroots(data, suffix):
    """One installroot per prebuilt package some recipe overlays.

    Defined once per release and shared by every recipe that names it --
    two packages needing the same prebuilt dependency unpack it once, which
    is what keeps base-plus-overlay cheaper than a buildroot per package.

    Only the ones actually overlaid.  The base is already in the shared
    tree, and an installroot for it would be a second copy of a package the
    buildroot has.
    """
    if data == None:
        return

    base = {name: True for name in data.BASE_SEED}
    # Keyed by download target, not by name: two builds of one package can
    # both be pinned, and each needs its own tree.
    by_target = {}
    for entry in data.SEED_RPMS:
        if entry["name"] not in base:
            by_target[entry["target"]] = entry

    # One for every pinned package outside the base, not only the ones a
    # recipe's seed_deps names.  A cycle stage also reaches for prebuilts
    # that the solver classified as built -- zlib-ng-compat-devel for
    # lzo-stage1 -- and those appear in no seed_deps list, so filtering by
    # that would leave the fallback in _rpm_one_package pointing at a
    # target nobody defined.  An unreferenced target costs nothing; a
    # missing one is an analysis error a long way from its cause.
    # Variant pins get an installroot unconditionally: they exist only
    # because something routed to them, and the base filter above is by
    # name, which is exactly the test they would fail.
    for entry in getattr(data, "VARIANT_SEED_RPMS", []):
        by_target[entry["target"]] = entry

    for target in sorted(by_target):
        entry = by_target[target]
        prebuilt_rpm(
            name = seed_installroot_target(entry, suffix),
            rpm = ":" + entry["target"] + suffix,
            package_name = entry["name"],
            visibility = ["PUBLIC"],
        )

def _rpm_one_package(
        flavor,
        data,
        suffix,
        buildroot,
        by_source,
        recipe,
        target_name,
        variant,
        provider,
        seed_rpm,
        variant_recipe,
        platform,
        exec_constraints):
    """One package() call, with build_deps resolved to subpackage labels."""
    # Keyed on the recipe, not the spec: a version variant shares its
    # spec name with the package it varies and must not share its srpm.
    source = by_source[recipe["name"]]
    version, _, release = recipe["evr"].rpartition("-")

    # Drop any epoch: rpm writes "1:5.8.1" in metadata but %{version} is
    # just the version.
    _, _, version = version.rpartition(":")

    build_deps = []
    # The .rpm behind each installroot, so the sysroot's database can be
    # told what the overlay put on disk.  Assembled here rather than
    # derived later: only this loop knows which target produced which
    # dependency, and an installroot does not carry the file it came from.
    dep_rpms = []
    dep_variants = recipe.get("dep_variants", {})
    seed_dep_names = {name: True for name in recipe.get("seed_deps", [])}
    # Pin-table entries for the routed binaries, by download target, so a
    # cycle stage that falls back to a prebuilt gets the variant's build.
    seed_by_target = {}
    for entry in data.SEED_RPMS:
        seed_by_target[entry["target"]] = entry
    for entry in getattr(data, "VARIANT_SEED_RPMS", []):
        seed_by_target[entry["target"]] = entry
    variant_seed = {}
    for binary, target in recipe.get("variant_seed", {}).items():
        found = seed_by_target.get(target)
        if found != None:
            variant_seed[binary] = found
    for binary in recipe["build_deps"]:
        # A version variant wins over the default producer, and only for
        # the packages that named it.  acl builds twice in Fedora 43: 2.4.0
        # for everyone, because rsync needs a symbol it added, and 2.3.2 for
        # tar, whose 1.35 source cannot compile against 2.4.0's header.
        # Routed by the solver rather than guessed here.
        routed = dep_variants.get(binary)
        if routed != None:
            spec = variant_recipe.get(routed)
            if spec == None:
                fail("{} routes {} to variant {}, which no recipe defines".format(
                    recipe["name"], binary, routed))
            # Through the stage map like any other built dep.  A variant can
            # land in a cycle -- acl-compat does, via zstd and zlib-ng back
            # to tar -- and a stage that must take this from the seed gets
            # None here, exactly as it would for a non-variant.
            chosen = variant.get(routed)
            if chosen == None:
                # The stage takes it prebuilt -- and it must be the
                # variant's build, not the newest.  tar-stage1 is exactly
                # this: it is in a cycle, so it gets acl from the pin table,
                # and the pin table's newest libacl-devel is the 2.4.0 whose
                # header tar cannot compile against.
                fallback = variant_seed.get(binary) or seed_rpm.get(binary)
                if fallback != None:
                    build_deps.append(
                        ":" + seed_installroot_target(fallback, suffix),
                    )
                    dep_rpms.append(":" + fallback["target"] + suffix)
                continue
            build_deps.append(":" + subpackage_target(
                chosen + suffix, spec["source_name"], binary,
            ))
            dep_rpms.append(":" + subpackage_rpm_target(
                chosen + suffix, spec["source_name"], binary,
            ))
            continue
        producer = provider.get(binary)
        if producer == None:
            # Not built by this repo -- either never was, or its source
            # package is on the prebuilt list for this host.
            #
            # The two need different handling and used to get the same.  A
            # genuine seed dep is either in the shared base or in the
            # recipe's own seed_deps, and the loop below overlays it.  A
            # *declassified* one is in neither: the solver put it in
            # deps_built because it is a package this repo builds, so no
            # seed_deps entry was ever emitted for it.  Falling through
            # left systemd without the kernel-devel it BuildRequires:
            #
            #   error: Failed build dependencies:
            #       kernel-devel is needed by systemd-258.10-1.fc43.x86_64
            #
            # So overlay it from the pin table here, skipping anything the
            # seed_deps loop will handle to avoid naming it twice.
            fallback = seed_rpm.get(binary)
            if fallback != None and binary not in seed_dep_names:
                build_deps.append(
                    ":" + seed_installroot_target(fallback, suffix),
                )
                dep_rpms.append(":" + fallback["target"] + suffix)
            continue
        chosen = variant.get(producer["name"])
        if chosen == None:
            # Excluded by _variant_map: a cycle dependency this stage takes
            # from the seed rather than from a sibling stage, or the
            # package's own earlier self.
            #
            # "From the seed" used to need no wiring, because the shared
            # buildroot was the union of every package's closure and so
            # already contained the prebuilt copy.  With the buildroot
            # narrowed to @buildsys-build it does not, and the stage fails
            # on a dependency the solver correctly classified as built:
            #
            #   error: Failed build dependencies:
            #       zlib-devel is needed by lzo-2.10-15.fc43.x86_64
            #
            # -- zlib-devel being provided by zlib-ng-compat-devel, which
            # this repo builds, which is exactly why stage 1 may not use it.
            # So the prebuilt is overlaid explicitly here.
            fallback = variant_seed.get(binary) or seed_rpm.get(binary)
            if fallback != None:
                build_deps.append(
                    ":" + seed_installroot_target(fallback, suffix),
                )
                dep_rpms.append(":" + fallback["target"] + suffix)
            continue
        build_deps.append(":" + subpackage_target(
            chosen + suffix, producer["source_name"], binary,
        ))
        dep_rpms.append(":" + subpackage_rpm_target(
            chosen + suffix, producer["source_name"], binary,
        ))

    # And the prebuilt half.  The shared buildroot carries @buildsys-build
    # and nothing else, so whatever else this package's BuildRequires
    # closed over is overlaid here, as an installroot per binary package.
    #
    # Same mechanism the source-built deps above use -- srpm_build takes
    # them all through dep_installroot_args -- so a dependency being
    # compiled here or fetched is invisible to the replay, which is the
    # property that lets the build set grow one package at a time.
    for binary in recipe.get("seed_deps", []):
        entry = seed_rpm.get(binary)
        if entry == None:
            # In the base, or absent from the pin table -- the first needs
            # no overlay and the second the solver already reported.
            continue
        build_deps.append(":" + seed_installroot_target(entry, suffix))
        dep_rpms.append(":" + entry["target"] + suffix)

    package(
        name = target_name + suffix,
        flavor = flavor,
        buildroot = buildroot,
        srpm = ":" + source["target"] + suffix,
        source_name = recipe["source_name"],
        version = version,
        release = release,
        build_deps = sorted(build_deps),
        dep_rpms = sorted(dep_rpms),
        # Keyed on the source package, not the target: gmp-stage1 and
        # gmp-stage2 replay one spec and must be told the same thing.
        #
        # Mapped flag-to-itself with `use` left empty, which is how
        # _resolve_bconds spells "pass --without": every bcond in the map
        # gets an explicit --with or --without, so the spec never silently
        # keeps its own default.
        use = [],
        use_bcond = {
            b: b
            for b in _disabled_bconds(flavor).get(recipe["source_name"], [])
        },
        subpackages = recipe["subpackages"],
        supplier = _flavor_config(flavor)["supplier"],
        # The release this recipe's data came from, which is the one whose
        # %if branches the spec should take.  Same source as DIST_TAG, so
        # `dist` and `fedora` can no longer disagree about which release is
        # being built.
        distro_release = data.RELEASE,
        default_target_platform = platform,
        exec_compatible_with = exec_constraints,
        visibility = ["PUBLIC"],
    )

def rpm_rootfs(flavor, data, suffix, platform, exec_constraints):
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
        buildroot = rpm_buildroot_target(flavor, suffix),
        rpms = [":" + entry["target"] + suffix for entry in data.SEED_RPMS],
        default_target_platform = platform,
        exec_compatible_with = exec_constraints,
        visibility = ["PUBLIC"],
    )

def rpm_image_rootfs(flavor, data, suffix, platform, exec_constraints):
    """A rootfs per named image set: the thing an ISO is actually made of.

    Same rule as rootfs-seed, different closure, and that is the whole
    point of having two.  rootfs-seed proves the mechanism against a list
    the solver derived for another purpose; these are the lists someone
    chose, closed over their runtime Requires, and they contain a kernel.
    """
    buildroot = rpm_buildroot_target(flavor, suffix)

    # Which recipe, if any, produces each binary package.  This is what
    # decides whether a package in an image comes out of this repo's
    # compiler or off Fedora's mirror, and it is the join the two halves
    # of the build were missing: the replay pipeline produced rpms nothing
    # could install, and the image pipeline installed rpms nothing here
    # had built.
    #
    # Keyed on the *binary* name because that is what an image set names.
    # A source package contributes every subpackage it emits, so building
    # zlib-ng means the image gets this repo's zlib-ng-compat too, not a
    # locally built zlib-ng beside a downloaded compat library from a
    # different compile.
    skip = {name: True for name in prebuilt_sources(flavor)}
    built = {}
    for subpackage, recipe in _binary_providers(data.RECIPES, skip).items():
        built[subpackage] = subpackage_rpm_target(
            recipe["name"] + suffix, recipe["source_name"], subpackage,
        )

    for name in sorted(data.IMAGE_SETS):
        if name in _TOOL_SETS:
            continue
        for variant in _IMAGE_VARIANTS:
            rpms = []
            from_source = 0
            for entry in data.IMAGE_SETS[name]:
                # The prebuilt variant consults no recipe at all.  Not even
                # for packages this host could build: the point of it is a
                # set with one provenance, so a half-source image cannot be
                # mistaken for the pinned one it is being compared against.
                local = built.get(entry["name"]) if variant == "" else None
                if local:
                    rpms.append(":" + local)
                    from_source += 1
                else:
                    rpms.append(":" + entry["target"] + suffix)
            rootfs(
                name = "rootfs-" + name + variant + suffix,
                buildroot = buildroot,
                rpms = rpms,
                selinux_modules = (
                    ["//flavors/centos-hyperscale:systemd-260-compat.cil"]
                    if flavor == "centos-hyperscale"
                    else []
                ),
                default_target_platform = platform,
                exec_compatible_with = exec_constraints,
                visibility = ["PUBLIC"],
            )

def rpm_image_tools(data, suffix, platform, exec_constraints):
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
            target_cpu = data.TARGET_CPU,
            default_target_platform = platform,
            exec_compatible_with = exec_constraints,
            visibility = ["PUBLIC"],
        )

def rpm_boot(flavor, data, suffix, platform, exec_constraints):
    """Kernel and initramfs per image set: what a bootloader loads.

    Only for sets that have a kernel, which in practice means the ones
    meant to boot.  A set without one still gets its targets defined --
    they fail when built, naming the set, rather than being silently
    absent, which is the difference between "this image does not boot" and
    "I cannot find the target that would tell me".
    """
    buildroot = rpm_buildroot_target(flavor, suffix)
    signing_key = _ima_signing_key()
    kernel_set = configured_kernel_set()

    for name in sorted(data.IMAGE_SETS):
        if name in _TOOL_SETS:
            continue
        for variant in _IMAGE_VARIANTS:
            base_rootfs_target = ":rootfs-" + name + variant + suffix
            rootfs_target = base_rootfs_target
            if kernel_set.targets:
                kernel_rootfs_name = "rootfs-kernel-" + name + variant + suffix
                kernel_rootfs(
                    name = kernel_rootfs_name,
                    architecture = data.TARGET_CPU,
                    ima_signing_key = signing_key if signing_key else None,
                    kernels = kernel_set.targets,
                    rootfs = base_rootfs_target,
                    default_target_platform = platform,
                    visibility = ["PUBLIC"],
                )
                rootfs_target = ":" + kernel_rootfs_name

            kernel_name = "kernel-" + name + variant + suffix
            initramfs_name = "initramfs-" + name + variant + suffix
            if kernel_set.targets:
                for index, kernel_target in enumerate(kernel_set.targets):
                    custom_suffix = "-custom-{}".format(index)
                    kernel_image(
                        name = kernel_name + custom_suffix,
                        architecture = data.TARGET_CPU,
                        kernel = kernel_target,
                        rootfs = rootfs_target,
                        default_target_platform = platform,
                        visibility = ["PUBLIC"],
                    )
                    initramfs(
                        name = initramfs_name + custom_suffix,
                        buildroot = buildroot,
                        kernel = kernel_target,
                        rootfs = rootfs_target,
                        add_modules = _INITRAMFS_MODULES.get(name, []),
                        ima_signing_key = signing_key if signing_key else None,
                        default_target_platform = platform,
                        exec_compatible_with = exec_constraints,
                        visibility = ["PUBLIC"],
                    )
                native.alias(
                    name = kernel_name,
                    actual = ":{}-custom-{}".format(kernel_name, kernel_set.default_index),
                    default_target_platform = platform,
                    visibility = ["PUBLIC"],
                )
                native.alias(
                    name = initramfs_name,
                    actual = ":{}-custom-{}".format(initramfs_name, kernel_set.default_index),
                    default_target_platform = platform,
                    visibility = ["PUBLIC"],
                )
            else:
                kernel_image(
                    name = kernel_name,
                    architecture = data.TARGET_CPU,
                    kernel = None,
                    rootfs = rootfs_target,
                    default_target_platform = platform,
                    visibility = ["PUBLIC"],
                )
                initramfs(
                    name = initramfs_name,
                    buildroot = buildroot,
                    kernel = None,
                    rootfs = rootfs_target,
                    add_modules = _INITRAMFS_MODULES.get(name, []),
                    ima_signing_key = signing_key if signing_key else None,
                    default_target_platform = platform,
                    exec_compatible_with = exec_constraints,
                    visibility = ["PUBLIC"],
                )

def rpm_images(flavor, data, release, suffix, platform, exec_constraints):
    """squashfs and ISO per bootable image set.

    The squashfs is separate from the ISO rather than folded into it
    because it is the expensive half -- compressing a whole root
    filesystem -- and it does not change when the kernel command line or
    the volume label does.  Fusing them would recompress the rootfs on
    every ISO tweak, which is the same "split on what this actually costs"
    argument defs/rules/boot.bzl makes for kernel_image and initramfs.

    The ISO runs in the image-tools buildroot.  Squashfs normally does too;
    Enterprise Linux 9 instead compiles the pinned 4.6.1 source because its
    packaged tool cannot add per-file xattrs, then uses that
    target-architecture binary for the image.  That compile gets its own
    buildroot -- the binary seed plus the headers it needs, which the seed
    alone does not carry.
    """
    tools = ":buildroot-image-tools" + suffix
    old_squashfs = release == "9" and flavor in ("centos", "centos-hyperscale")
    squashfs_tools = ":buildroot-squashfs-tools" + suffix if old_squashfs else tools
    squashfs_source = "//tools:squashfs-tools-4.6.1-source" if old_squashfs else None
    signing_key = _ima_signing_key()
    kernel_set = configured_kernel_set()

    for name in sorted(data.IMAGE_SETS):
        if name in _TOOL_SETS:
            continue
        for variant in _IMAGE_VARIANTS:
            rootfs_target = (
                ":rootfs-kernel-" + name + variant + suffix
                if kernel_set.targets
                else ":rootfs-" + name + variant + suffix
            )
            manifest_target = None
            if signing_key:
                manifest_name = "ima-manifest-" + name + variant + suffix
                ima_manifest(
                    name = manifest_name,
                    rootfs = rootfs_target,
                    signing_key = signing_key,
                    mode = _ima_signing_mode(),
                    default_target_platform = platform,
                    visibility = ["PUBLIC"],
                )
                manifest_target = ":" + manifest_name

            squashfs(
                name = "squashfs-" + name + variant + suffix,
                buildroot = squashfs_tools,
                mksquashfs_source = squashfs_source,
                ima_manifest = manifest_target,
                rootfs = rootfs_target,
                # Fedora ships selinux-policy-targeted in every bootable
                # set and boots enforcing, so an unlabelled image does not
                # boot at all -- systemd freezes as PID 1 before starting a
                # unit.  Labelling here is what lets the kernel command
                # line stop saying selinux=0.
                selinux_relabel = True,
                default_target_platform = platform,
                exec_compatible_with = exec_constraints,
                visibility = ["PUBLIC"],
            )

            # Uppercase because the volume id is what ends up in the kernel
            # command line as CDLABEL=, and genisoimage-style volume ids
            # are upper-cased by the filesystem -- a lowercase label here
            # would be written uppercase and then not match at boot.
            #
            # The variant is part of the label, not decoration: booting one
            # of these is how you find out which one you burned, and two
            # ISOs claiming the same CDLABEL would have the live root of
            # whichever disc was found first.
            label = iso_volume_label("{}-{}-{}{}".format(
                flavor.upper(),
                release,
                name.upper(),
                variant.upper(),
            ))

            iso_image(
                name = "iso-" + name + variant + suffix,
                buildroot = tools,
                kernel = ":kernel-" + name + variant + suffix,
                initramfs = ":initramfs-" + name + variant + suffix,
                additional_initramfs = [
                    ":initramfs-{}{}{}-custom-{}".format(name, variant, suffix, index)
                    for index in kernel_set.additional_indices
                ],
                additional_kernels = [
                    ":kernel-{}{}{}-custom-{}".format(name, variant, suffix, index)
                    for index in kernel_set.additional_indices
                ],
                squashfs = ":squashfs-" + name + variant + suffix,
                volume_label = label,
                kernel_args = "{} {}".format(
                    "{} {}".format(
                        _LIVE_KERNEL_ARGS,
                        _ima_kernel_args() if signing_key else "",
                    ).strip(),
                    "console=ttyAMA0,115200" if data.TARGET_CPU == "aarch64" else "console=ttyS0,115200",
                ),
                boot_mode = "hybrid" if data.TARGET_CPU == "x86_64" else "uefi",
                target_cpu = data.TARGET_CPU,
                default_target_platform = platform,
                exec_compatible_with = exec_constraints,
                visibility = ["PUBLIC"],
            )

# ── Per-release fan-out ──────────────────────────────────────────────
#
# Each release gets a `-<release>` suffix, and the default release gets a
# second, unsuffixed copy so callers that do not care which release they
# get keep working.  The three passes are separate because Buck needs a
# target defined before it is referenced: downloads, then the buildroot
# that consumes them, then the packages that build against it.

def _data_for(flavor, data_by_release_arch, release, architecture):
    by_arch = data_by_release_arch.get(release)
    data = by_arch.get(architecture) if by_arch != None else None
    if data == None:
        fail(
            "{} release {} has no generated {} data; solve and generate flavors/{}/lock/{}-{}-{}.lock.json".format(
                flavor,
                release,
                architecture,
                flavor,
                flavor,
                release,
                architecture,
            ),
        )
    if data.FLAVOR != flavor:
        fail("{} release {} loaded {} data".format(flavor, release, data.FLAVOR))
    if data.TARGET_CPU != architecture:
        fail("{} release {} {} loaded {} data".format(
            flavor,
            release,
            architecture,
            data.TARGET_CPU,
        ))
    return data

def rpm_downloads_for(flavor, releases, default, data_by_release_arch):
    """http_file targets for every pinned rpm, per release."""
    for release in releases:
        for architecture in ARCHITECTURES:
            rpm_downloads(
                flavor,
                _data_for(flavor, data_by_release_arch, release, architecture),
                release_arch_suffix(release, architecture),
                target_platform(flavor, release, architecture),
            )

    # Compatibility targets remain x86_64. They are separate definitions
    # because http_file has no cheap alias form that preserves every output
    # name consumed by generated package labels.
    for release in releases:
        rpm_downloads(
            flavor,
            _data_for(flavor, data_by_release_arch, release, DEFAULT_ARCHITECTURE),
            release_suffix(release),
            target_platform(flavor, release, DEFAULT_ARCHITECTURE),
        )
    rpm_downloads(
        flavor,
        _data_for(flavor, data_by_release_arch, default, DEFAULT_ARCHITECTURE),
        "",
        target_platform(flavor, default, DEFAULT_ARCHITECTURE),
    )

def rpm_buildroots_for(flavor, releases, default, data_by_release_arch = None):
    """Define buildroots for every release, plus the unsuffixed default."""
    data_by_release_arch = data_by_release_arch or {}

    for release in releases:
        for architecture in ARCHITECTURES:
            rpm_buildroots(
                flavor,
                release,
                release_arch_suffix(release, architecture),
                target_platform(flavor, release, architecture),
                execution_compatible_with(architecture),
                _data_for(flavor, data_by_release_arch, release, architecture),
            )

    for release in releases:
        rpm_buildroots(
            flavor,
            release,
            release_suffix(release),
            target_platform(flavor, release, DEFAULT_ARCHITECTURE),
            execution_compatible_with(DEFAULT_ARCHITECTURE),
            _data_for(flavor, data_by_release_arch, release, DEFAULT_ARCHITECTURE),
        )
    rpm_buildroots(
        flavor,
        default,
        "",
        target_platform(flavor, default, DEFAULT_ARCHITECTURE),
        execution_compatible_with(DEFAULT_ARCHITECTURE),
        _data_for(flavor, data_by_release_arch, default, DEFAULT_ARCHITECTURE),
    )

def rpm_packages_for(flavor, releases, default, data_by_release_arch):
    """package() calls for every recipe, per release."""
    for release in releases:
        for architecture in ARCHITECTURES:
            rpm_packages(
                flavor,
                _data_for(flavor, data_by_release_arch, release, architecture),
                release_arch_suffix(release, architecture),
                target_platform(flavor, release, architecture),
                execution_compatible_with(architecture),
            )

    for release in releases:
        rpm_packages(
            flavor,
            _data_for(flavor, data_by_release_arch, release, DEFAULT_ARCHITECTURE),
            release_suffix(release),
            target_platform(flavor, release, DEFAULT_ARCHITECTURE),
            execution_compatible_with(DEFAULT_ARCHITECTURE),
        )
    rpm_packages(
        flavor,
        _data_for(flavor, data_by_release_arch, default, DEFAULT_ARCHITECTURE),
        "",
        target_platform(flavor, default, DEFAULT_ARCHITECTURE),
        execution_compatible_with(DEFAULT_ARCHITECTURE),
    )

def _rpm_images_for_one(flavor, data, release, suffix, architecture):
    platform = target_platform(flavor, release, architecture)
    exec_constraints = execution_compatible_with(architecture)
    rpm_rootfs(flavor, data, suffix, platform, exec_constraints)
    rpm_image_rootfs(flavor, data, suffix, platform, exec_constraints)
    rpm_image_tools(data, suffix, platform, exec_constraints)
    rpm_boot(flavor, data, suffix, platform, exec_constraints)
    rpm_images(flavor, data, release, suffix, platform, exec_constraints)

def rpm_rootfs_for(flavor, releases, default, data_by_release_arch):
    """rootfs and boot targets for every release, plus the default.

    Boot targets are defined in the same pass, after the rootfs they read:
    Buck needs a target to exist before it is referenced, and
    kernel-<set> / initramfs-<set> both take :rootfs-<set>.
    """
    for release in releases:
        for architecture in ARCHITECTURES:
            _rpm_images_for_one(
                flavor,
                _data_for(flavor, data_by_release_arch, release, architecture),
                release,
                release_arch_suffix(release, architecture),
                architecture,
            )

    for release in releases:
        _rpm_images_for_one(
            flavor,
            _data_for(flavor, data_by_release_arch, release, DEFAULT_ARCHITECTURE),
            release,
            release_suffix(release),
            DEFAULT_ARCHITECTURE,
        )
    _rpm_images_for_one(
        flavor,
        _data_for(flavor, data_by_release_arch, default, DEFAULT_ARCHITECTURE),
        default,
        "",
        DEFAULT_ARCHITECTURE,
    )
