"""Distro releases as a first-class axis.

A distro is not one thing: Fedora 44 and Fedora 45 are different build
universes with different compilers, different macros, and different
pinned dependency sets.  Treating "which release" as a global mode the
repo is switched into makes the common questions -- does this package
still build on the next release? what changed between them? -- require
two separate builds that cannot be compared in one graph.

So release is an axis, not a mode:

  * every release named in ``[buckos.<flavor>] releases`` gets its own
    buildroot targets, suffixed with the release, and they coexist;
  * ``[buckos.<flavor>] release`` picks which one the unsuffixed default
    targets alias, for callers that do not care;
  * ``//platforms:<flavor>-<release>`` is a real constraint value, so a
    target can be declared incompatible with a release rather than
    silently building against the wrong one.

This mirrors how ``flavor`` and ``provenance`` are already modelled in
platforms/BUCK: a property carried in the configuration, not a string
passed around in providers and hoped about.
"""

def flavor_releases(flavor, default = ""):
    """The releases configured for a flavor, in declaration order.

    Reads ``[buckos.<flavor>] releases`` as a comma-separated list.
    Returns a list of strings; blank entries are dropped so a trailing
    comma is not an error.
    """
    raw = read_config("buckos." + flavor, "releases", default)
    releases = [r.strip() for r in raw.split(",") if r.strip()]
    if not releases:
        fail(
            "[buckos.{}] releases is empty. ".format(flavor) +
            "List at least one release, e.g. releases = 44,45",
        )
    return releases

def default_release(flavor, releases):
    """Which release the unsuffixed targets alias.

    Defaults to the last entry in ``releases`` -- the newest, by the
    convention that the list is written oldest-first -- so adding a new
    release moves the default forward without a second config edit.
    """
    release = read_config("buckos." + flavor, "release", releases[-1])
    if release not in releases:
        fail(
            "[buckos.{}] release = {} is not in releases = {}. ".format(
                flavor,
                release,
                ",".join(releases),
            ) +
            "Add it to the releases list, or pick one already there.",
        )
    return release

def release_suffix(release):
    """Target-name suffix for a release: 44 -> '-44'."""
    return "-" + release

def release_constraint(flavor, release):
    """The constraint value naming a (flavor, release) pair."""
    return "//platforms:{}-{}".format(flavor, release)

# ISO9660 caps the volume identifier at 32 characters and xorriso refuses a
# longer one outright, so an over-long label is a build failure rather than a
# cosmetic problem.  Only CENTOS-HYPERSCALE reaches it, at 33 and 34 for its
# two prebuilt labels, and it took until the first Hyperscale image build for
# anyone to find out.
#
# Trimming the tail keeps every label that already fits untouched, which
# matters because the label is also the live-root kernel argument and the
# boot tests assert it.  It also keeps a trimmed prebuilt label distinct from
# its source sibling, since the source variant contributes no suffix at all.
# `tools/iso_label_test.py` enforces that across the whole configured matrix
# rather than leaving it to inspection.
_ISO_VOLUME_LABEL_MAX = 32

def iso_volume_label(text):
    """The ISO9660 volume identifier for a live image, within spec length."""
    if not text:
        fail("an ISO volume label cannot be empty")
    return text[:_ISO_VOLUME_LABEL_MAX]
