load("//defs:flavor.bzl", "current_flavor")

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
    cmd = "echo {} > $OUT".format(current_flavor()),
    visibility = ["PUBLIC"],
)
