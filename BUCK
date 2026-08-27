load("//defs:flavor.bzl", "current_flavor")

_FLAVOR = current_flavor()

_BUILDROOT = read_config(
    "buckos." + _FLAVOR,
    "buildroot",
    "host",
)

# Config values are scoped to the cell that reads them, so the buildroot
# selector lives here with the repository configuration that controls it.
toolchain_alias(
    name = "buildroot",
    actual = "//flavors/{}:buildroot-{}".format(_FLAVOR, _BUILDROOT),
    visibility = ["PUBLIC"],
)

# `buck2 build //:flavor` prints which flavor the current configuration
# resolves to.  Cheap, but it is the fastest way to confirm .buckconfig and
# any -c override agree with what you think you are building.
export_file(
    name = "spec",
    src = "SPEC.md",
    visibility = ["PUBLIC"],
)

genrule(
    name = "flavor",
    out = "flavor.txt",
    cmd = "echo {} > $OUT".format(_FLAVOR),
    visibility = ["PUBLIC"],
)
