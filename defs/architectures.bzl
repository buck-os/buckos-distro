"""Architecture names and target-platform helpers.

The package-manager spelling is deliberately kept out of this module.
The build graph uses the two kernel/Buck spellings everywhere and maps to
RPM or dpkg vocabulary only at their respective lockfile boundaries.
"""

ARCHITECTURES = ("x86_64", "aarch64")
DEFAULT_ARCHITECTURE = "x86_64"

def architecture_suffix(architecture):
    _validate_architecture(architecture)
    return "-" + architecture

def release_arch_suffix(release, architecture):
    return "-{}{}".format(release, architecture_suffix(architecture))

def target_platform(flavor, release, architecture):
    _validate_architecture(architecture)
    return "//platforms:{}-{}-{}".format(flavor, release, architecture)

def execution_compatible_with(architecture):
    _validate_architecture(architecture)
    return ["//platforms:can-execute-{}".format(architecture)]

def _validate_architecture(architecture):
    if architecture not in ARCHITECTURES:
        fail("unsupported architecture {!r}; expected one of {}".format(
            architecture,
            ", ".join(ARCHITECTURES),
        ))
