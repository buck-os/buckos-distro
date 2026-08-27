load(
    ":debian-13.bzl",
    _ARCHITECTURE_13 = "ARCHITECTURE",
    _CODENAME_13 = "CODENAME",
    _DISTRO_13 = "DISTRO",
    _RELEASE_13 = "RELEASE",
    _SEED_DEBS_13 = "SEED_DEBS",
    _SOURCES_13 = "SOURCES",
)

DATA_BY_RELEASE = {
    "13": struct(
        ARCHITECTURE = _ARCHITECTURE_13,
        CODENAME = _CODENAME_13,
        DISTRO = _DISTRO_13,
        RELEASE = _RELEASE_13,
        SEED_DEBS = _SEED_DEBS_13,
        SOURCES = _SOURCES_13,
    ),
}
