"""The distro-neutral front door.

`package()` is the single macro every recipe in this repo calls, and the
only place that knows which flavors exist.  Its job is to take a
flavor-agnostic description and dispatch to that flavor's frontend, which
fills the four slots from SPEC.md section 2:

    flavor  source package        declared deps    build driver        output
    ------  -------------------   --------------   -----------------   ------
    fedora  .src.rpm             BuildRequires    rpmbuild -bb        .rpm
    ubuntu  .dsc + debian.tar    Build-Depends    dpkg-buildpackage   .deb
    buckos  upstream tarball     Buck labels      configure && make   prefix tree

Deliberately mirrors buckos-build's defs/package.bzl: one chokepoint macro
that creates a chain of intermediate targets and ends in a
`native.alias`.  Keeping the shape identical means buckos-build's
transform rules (strip, stamp, sign) can be dropped in later without
restructuring anything.
"""

load(
    "//defs/rules/srpm.bzl",
    "built_rpm",
    "prebuilt_rpm",
    "rpm_subpackage",
    "srpm_build",
    "srpm_buildrequires",
    "srpm_unpack",
)

FLAVORS = ("fedora", "ubuntu", "buckos")

def current_flavor():
    """The flavor selected by .buckconfig, overridable per invocation with
    `buck2 build ... -c buckos.flavor=ubuntu`."""
    return read_config("buckos", "flavor", "fedora")

# ── Fedora frontend ──────────────────────────────────────────────────

def _fedora_package(
        name,
        srpm,
        source_name = None,
        version = "",
        release = "",
        build_deps = None,
        subpackages = None,
        use = None,
        use_bcond = None,
        defines = None,
        nocheck = True,
        # Named rather than left in **kwargs because two rules need it and
        # only one would get it otherwise.  A probe that ran against the
        # flavor's default buildroot instead of the release's would answer
        # a question about the wrong Fedora -- silently, since it produces
        # a plausible dependency list either way.
        buildroot = None,
        visibility = None,
        **kwargs):
    """Replay one source rpm.

    Creates:
        :name-topdir          the unpacked srpm
        :name-buildrequires   what the spec really needs (lock-time only)
        :name-build           the rpmbuild replay (the expensive action)
        :name-<sub>           one cheap projection per subpackage
        :name                 alias to the binary package sharing the source name

    `source_name` decouples the target name from the source package's own
    name.  Two things need that: bootstrap stages, where gcc-stage1 and
    gcc-stage3 replay the same gcc srpm; and USE-flag variants built side
    by side.  It defaults to `name`, which is the common case.
    """
    source_name = source_name or name
    build_deps = build_deps or []
    subpackages = subpackages or [source_name]
    defines = defines or []

    srpm_unpack(
        name = name + "-topdir",
        srpm = srpm,
        expect_spec = source_name + ".spec",
    )

    with_bconds, without_bconds = _resolve_bconds(use, use_bcond)

    # Nothing depends on this, by construction -- it is how the lockfile
    # learns what to put in build_deps in the first place, so a build that
    # needed it would be a build asking a question it has already been
    # given the answer to.  `tools/relock.py --probe` builds it, reads the
    # JSON, and records the result; see srpm_buildrequires.
    #
    # Emitted for every package rather than only the ones with a
    # %generate_buildrequires block, because whether a spec has one is a
    # property of the spec, and the recipe generator only sees repodata.
    # A probe of a spec without a generator is cheap and returns an empty
    # dynamic set, which is a useful thing to have recorded.
    srpm_buildrequires(
        name = name + "-buildrequires",
        topdir = ":" + name + "-topdir",
        package_name = source_name,
        build_deps = build_deps,
        defines = defines,
        with_bconds = with_bconds,
        without_bconds = without_bconds,
        buildroot = buildroot,
        fedora_release = read_config("buckos.fedora", "release", None),
        visibility = visibility,
    )

    srpm_build(
        name = name + "-build",
        topdir = ":" + name + "-topdir",
        package_name = source_name,
        version = version,
        release = release,
        build_deps = build_deps,
        defines = defines,
        with_bconds = with_bconds,
        without_bconds = without_bconds,
        nocheck = nocheck,
        buildroot = buildroot,
        fedora_release = read_config("buckos.fedora", "release", None),
        # Only the frontend knows the ambient rpmbuild may be a wrapper;
        # host provenance needs the real binary.
        rpmbuild = read_config("buckos.fedora", "rpmbuild", None),
        visibility = visibility,
        **kwargs
    )

    # One projection per binary package.  These are cheap: the compile
    # lives in :name-build, which Buck memoizes across all of them.
    for sub in subpackages:
        rpm_subpackage(
            name = _subpackage_target(name, source_name, sub),
            srpm = ":" + name + "-build",
            rpm = _subpackage_rpm_name(source_name, sub),
            visibility = visibility,
        )
        # And the rpm file itself, for a rootfs rather than a buildroot.
        # Defined alongside unconditionally: which binary packages an
        # image happens to want is not knowable here, both projections
        # are one file operation over an already-built directory, and a
        # target nothing references costs nothing.
        built_rpm(
            name = subpackage_rpm_target(name, source_name, sub),
            srpm = ":" + name + "-build",
            rpm = _subpackage_rpm_name(source_name, sub),
            visibility = visibility,
        )

    # `:name` is the binary package sharing the source package's name --
    # what a BuildRequires on it means.  A source package that publishes no
    # such binary rpm (util-linux-core, and every other -core split) has to
    # be referenced by subpackage, so alias to the build and let the error
    # surface at the reference rather than here.
    if source_name in subpackages:
        actual = ":" + _subpackage_target(name, source_name, source_name)
    else:
        actual = ":" + name + "-build"

    native.alias(
        name = name,
        actual = actual,
        visibility = visibility,
    )

def subpackage_target(name, source_name, sub):
    """The target name package() gives a subpackage projection.

    Public because a build_deps entry in a lockfile is a *binary* package
    name -- "xz-libs" -- and turning it into something buck2 can depend on
    means knowing what package() called that projection.  A caller that
    reconstructed the rule would be a second copy of it, wrong the first
    time the naming changes and wrong silently, since a mistyped label
    fails as "no such target" rather than as a naming bug.
    """
    return _subpackage_target(name, source_name, sub)

def subpackage_rpm_target(name, source_name, sub):
    """The target name for a subpackage's .rpm file.

    Public for the same reason subpackage_target is: an image set names a
    *binary* package, and turning that into the label of the rpm this repo
    built for it means knowing what package() called the rule.
    """
    return _subpackage_target(name, source_name, sub) + "-rpm"

def _subpackage_target(name, source_name, sub):
    """Target name for a subpackage projection.

    Must not collide with `:name` itself, which is the alias, hence the
    "-main" suffix for the subpackage that shares the source name.
    """
    if sub == source_name:
        return name + "-main"
    return name + "-" + _strip_prefix(sub, source_name + "-")

def _strip_prefix(value, prefix):
    if value.startswith(prefix):
        return value[len(prefix):]
    return value

def _subpackage_rpm_name(source_name, sub):
    """Binary package name for a declared subpackage.

    Recipes name subpackages the short way (`devel`, `libs`) the same way
    a spec's `%package devel` does; rpm expands that to `<name>-devel`.  A
    subpackage declared with `-n` keeps its own full name, which the recipe
    signals by writing it out in full.
    """
    if sub == source_name:
        return source_name
    if sub.startswith(source_name + "-"):
        return sub
    return source_name + "-" + sub

def _resolve_bconds(use, use_bcond):
    """Turn USE flags into rpm --with/--without pairs (SPEC.md section 5).

    `use` is the set of flags this package should build with; `use_bcond`
    maps a flag name onto the spec's bcond name when they differ, e.g.
    {"ssl": "openssl"} for a spec whose conditional is %bcond_with
    openssl.  Every flag in the map gets an explicit --with or --without so
    the build never silently inherits the spec's default.
    """
    use = use or []
    use_bcond = use_bcond or {}

    enabled = []
    disabled = []
    for flag, bcond in sorted(use_bcond.items()):
        if flag in use:
            enabled.append(bcond)
        else:
            disabled.append(bcond)

    # Flags with no mapping are assumed to share their name with the bcond.
    for flag in use:
        if flag not in use_bcond:
            enabled.append(flag)

    return sorted(enabled), sorted(disabled)

# ── Dispatch ─────────────────────────────────────────────────────────

def package(name, flavor = None, **kwargs):
    """Build a package with its flavor's native tooling.

    flavor defaults to the one selected in .buckconfig, so a recipe
    directory can be flavor-agnostic and the same target graph switches
    wholesale with `-c buckos.flavor=...`.
    """
    flavor = flavor or current_flavor()

    if flavor == "fedora":
        _fedora_package(name = name, **kwargs)
    elif flavor in FLAVORS:
        fail(
            "flavor {} is declared but its frontend is not implemented yet; see flavors/{}/README.md".format(flavor, flavor),
        )
    else:
        fail(
            "unknown flavor {}; expected one of {}".format(
                flavor,
                ", ".join(FLAVORS),
            ),
        )

# Re-exported so recipe files need only load this module.
seed_package = prebuilt_rpm
