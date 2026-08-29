"""The distro-neutral front door.

`package()` is the single macro every recipe in this repo calls, and the
only place that knows which flavors exist.  Its job is to take a
flavor-agnostic description and dispatch to that flavor's frontend, which
fills the four slots from SPEC.md section 2:

    flavor  source package        declared deps    build driver        output
    ------  -------------------   --------------   -----------------   ------
    fedora  .src.rpm             BuildRequires    rpmbuild -bb        .rpm
    centos  .src.rpm             BuildRequires    rpmbuild -bb        .rpm
    centos-hyperscale
            .src.rpm             BuildRequires    rpmbuild -bb        .rpm
    debian  .dsc + debian.tar    Build-Depends    dpkg-buildpackage   .deb
    ubuntu  .dsc + debian.tar    Build-Depends    dpkg-buildpackage   .deb
    buckos  upstream tarball     Buck labels      configure && make   prefix tree

Deliberately mirrors buckos-build's defs/package.bzl: one chokepoint macro
that creates a chain of intermediate targets and ends in a
`native.alias`.  Keeping the shape identical means buckos-build's
transform rules (strip, stamp, sign) can be dropped in later without
restructuring anything.
"""

load(
    "//defs/rules/dsc.bzl",
    "built_deb",
    "deb_build",
    "deb_subpackage",
    "dsc_unpack",
)
load(
    "//defs/rules/srpm.bzl",
    "built_rpm",
    "prebuilt_rpm",
    "rpm_subpackage",
    "srpm_build",
    "srpm_buildrequires",
    "srpm_unpack",
)

FLAVORS = (
    "fedora",
    "centos",
    "centos-hyperscale",
    "debian",
    "ubuntu",
    "buckos",
)

def current_flavor():
    """The flavor selected by .buckconfig, overridable per invocation with
    `buck2 build ... -c buckos.flavor=ubuntu`."""
    return read_config("buckos", "flavor", "fedora")

# ── RPM-family frontend ──────────────────────────────────────────────

def _rpm_package(
        name,
        flavor,
        srpm,
        source_name = None,
        version = "",
        release = "",
        build_deps = None,
        dep_rpms = None,
        subpackages = None,
        use = None,
        use_bcond = None,
        defines = None,
        nocheck = True,
        # Named rather than left in **kwargs because two rules need it and
        # only one would get it otherwise.  A probe that ran against the
        # flavor's default buildroot instead of the release's would answer
        # a question about the wrong release -- silently, since it produces
        # a plausible dependency list either way.
        buildroot = None,
        # Which release's %{fedora} the spec is evaluated against, named
        # here for exactly the reason `buildroot` is: the caller knows and
        # the ambient configuration does not.  See distro_release below.
        distro_release = None,
        default_target_platform = "//platforms:linux-x86_64",
        exec_compatible_with = ["//platforms:can-execute-x86_64"],
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
    dep_rpms = dep_rpms or []
    subpackages = subpackages or [source_name]
    defines = defines or []

    srpm_unpack(
        name = name + "-topdir",
        srpm = srpm,
        expect_spec = source_name + ".spec",
        default_target_platform = default_target_platform,
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
    # %{fedora} decides which branch of a spec's %if runs, so it has to be
    # the release this package belongs to and not the one the graph happens
    # to default to.  `[buckos.fedora] release` names the release the
    # *unsuffixed* targets alias; reading it here handed every release the
    # default's number, so in a graph holding 43 and 44 every F43 spec was
    # evaluated as though it were F44.
    #
    # libseccomp is the visible version: it guards its python bindings with
    #
    #     %if 0%{?fedora} >= 44
    #
    # and the F43 build demanded python3-devel, cython and setuptools that
    # F43 does not put in its buildroot, because it had been told it was 44.
    # Undefined is not a safe fallback either -- 0%{?fedora} is then 0 and
    # every Fedora-only branch silently takes the wrong path -- so the
    # caller passes the release and only a caller that has none omits it.
    if flavor != "fedora":
        distro_release = None

    srpm_buildrequires(
        name = name + "-buildrequires",
        topdir = ":" + name + "-topdir",
        package_name = source_name,
        build_deps = build_deps,
        dep_rpms = dep_rpms,
        defines = defines,
        with_bconds = with_bconds,
        without_bconds = without_bconds,
        buildroot = buildroot,
        fedora_release = distro_release,
        default_target_platform = default_target_platform,
        exec_compatible_with = exec_compatible_with,
        visibility = visibility,
    )

    srpm_build(
        name = name + "-build",
        topdir = ":" + name + "-topdir",
        package_name = source_name,
        version = version,
        release = release,
        build_deps = build_deps,
        dep_rpms = dep_rpms,
        defines = defines,
        with_bconds = with_bconds,
        without_bconds = without_bconds,
        nocheck = nocheck,
        buildroot = buildroot,
        fedora_release = distro_release,
        flavor = flavor,
        # Only the frontend knows the ambient rpmbuild may be a wrapper;
        # host provenance needs the real binary.
        rpmbuild = read_config("buckos." + flavor, "rpmbuild", None),
        default_target_platform = default_target_platform,
        exec_compatible_with = exec_compatible_with,
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
            default_target_platform = default_target_platform,
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
            default_target_platform = default_target_platform,
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
        default_target_platform = default_target_platform,
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

# The suffixes _rpm_package hangs off `name` for its own machinery.  A
# subpackage projection that landed on one of these would be the second
# target registered under that name, and buck2 reports the collision at the
# *second* registration -- so the error names rpm_subpackage and says
# nothing about the srpm_build it actually clashed with.
_RESERVED_SUFFIXES = ("topdir", "buildrequires", "build")

def _subpackage_target(name, source_name, sub):
    """Target name for a subpackage projection.

    Must not collide with `:name` itself, which is the alias, hence the
    "-main" suffix for the subpackage that shares the source name.

    Nor with the intermediates `:name-build` and friends, which is not
    hypothetical: the rpm source package builds a subpackage called
    rpm-build, whose tail after stripping the source name is exactly
    "build".  Those keep their prefix instead of losing it, so the target
    is `rpm-stage1-43-rpm-build` -- longer, and unambiguous.
    """
    if sub == source_name:
        return name + "-main"
    tail = _strip_prefix(sub, source_name + "-")
    if tail in _RESERVED_SUFFIXES:
        tail = sub
    return name + "-" + tail

def _strip_prefix(value, prefix):
    if value.startswith(prefix):
        return value[len(prefix):]
    return value

def _subpackage_rpm_name(source_name, sub):
    """Binary package name for a declared subpackage: the name, verbatim.

    This used to expand a short form the way a spec does -- `%package
    devel` under `Name: attr` becomes `attr-devel`, so a recipe saying
    "devel" meant "attr-devel".  That is wrong here, and wrong in a way
    that only a package with a `%package -n` shows.

    Subpackage lists come from the lockfile, and the lockfile records what
    rpm will actually emit: `attr` builds `attr`, `libattr` and
    `libattr-devel`, none of which is a short form.  Expanding turned the
    last two into `attr-libattr` and `attr-libattr-devel`, names no rpm in
    the build has, and the failure landed at selection time as "no rpm for
    binary package 'attr-libattr-devel'".

    No heuristic rescues that, which is worth saying because the obvious
    one nearly does: "expand only names without a hyphen" handles
    `libattr-devel` and still mangles `libattr`.  The distinction is not
    in the string.

    Nothing needs the short form either -- the only hand-written recipes
    name a subpackage that equals the source package, which was already
    the identity case.  So the expansion is gone rather than narrowed.
    """
    return sub

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

# ── Debian-family frontend ──────────────────────────────────────────

def _deb_package(
        name,
        flavor,
        dsc,
        source_files,
        source_name = None,
        version = "",
        version_full = "",
        release = "",
        build_deps = None,
        subpackages = None,
        binary_metadata = None,
        build_profiles = None,
        nocheck = True,
        buildroot = None,
        default_target_platform = "//platforms:linux-x86_64",
        exec_compatible_with = ["//platforms:can-execute-x86_64"],
        visibility = None,
        **kwargs):
    """Replay one Debian source package with dpkg-buildpackage."""
    source_name = source_name or name
    build_deps = build_deps or []
    subpackages = subpackages or [source_name]
    binary_metadata = binary_metadata or {}
    build_profiles = [profile for profile in (build_profiles or [])]
    if nocheck and "nocheck" not in build_profiles:
        build_profiles.append("nocheck")

    dsc_unpack(
        name = name + "-source",
        dsc = dsc,
        source_files = source_files,
        package_name = source_name,
        flavor = flavor,
        version = version,
        release = release,
        default_target_platform = default_target_platform,
        visibility = visibility,
    )

    deb_build(
        name = name + "-build",
        source = ":" + name + "-source",
        dsc = dsc,
        build_deps = build_deps,
        build_profiles = build_profiles,
        nocheck = nocheck,
        buildroot = buildroot,
        dpkg_buildpackage = read_config("buckos." + flavor, "dpkg_buildpackage", None),
        default_target_platform = default_target_platform,
        exec_compatible_with = exec_compatible_with,
        visibility = visibility,
        **kwargs
    )

    for subpackage in subpackages:
        metadata = binary_metadata.get(subpackage, {})
        architecture = metadata.get("architecture", "")
        source_version = metadata.get("source_version", version_full)
        deb_subpackage(
            name = _subpackage_target(name, source_name, subpackage),
            source = ":" + name + "-build",
            package_name = subpackage,
            architecture = architecture,
            source_name = source_name,
            source_version = source_version,
            default_target_platform = default_target_platform,
            visibility = visibility,
        )
        if architecture and source_version:
            built_deb(
                name = subpackage_deb_target(name, source_name, subpackage),
                source = ":" + name + "-build",
                package_name = subpackage,
                architecture = architecture,
                source_name = source_name,
                source_version = source_version,
                default_target_platform = default_target_platform,
                visibility = visibility,
            )

    if source_name in subpackages:
        actual = ":" + _subpackage_target(name, source_name, source_name)
    else:
        actual = ":" + name + "-build"

    native.alias(
        name = name,
        actual = actual,
        default_target_platform = default_target_platform,
        visibility = visibility,
    )

def subpackage_deb_target(name, source_name, subpackage):
    """Target name for one selected DEB emitted by a source build."""
    return _subpackage_target(name, source_name, subpackage) + "-deb"

# ── Dispatch ─────────────────────────────────────────────────────────

def package(name, flavor = None, **kwargs):
    """Build a package with its flavor's native tooling.

    flavor defaults to the one selected in .buckconfig, so a recipe
    directory can be flavor-agnostic and the same target graph switches
    wholesale with `-c buckos.flavor=...`.
    """
    flavor = flavor or current_flavor()

    if flavor in ("fedora", "centos", "centos-hyperscale"):
        _rpm_package(name = name, flavor = flavor, **kwargs)
    elif flavor in ("debian", "ubuntu"):
        _deb_package(name = name, flavor = flavor, **kwargs)
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
