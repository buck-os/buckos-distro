"""Read and write plain or deterministically gzip-compressed lockfiles."""

import configparser
import gzip
import io
import json
import os


PLAIN_SUFFIX = ".lock.json"
GZIP_SUFFIX = PLAIN_SUFFIX + ".gz"
LOCKFILE_SUFFIXES = (GZIP_SUFFIX, PLAIN_SUFFIX)
_CONFIG_SECTION = "buckos.lockfiles"
_CONFIG_KEY = "compression"
_COMPRESSIONS = ("none", "gzip")


def _repo_root():
    for start in (os.getcwd(), os.path.dirname(os.path.abspath(__file__))):
        directory = start
        while True:
            if os.path.isfile(os.path.join(directory, ".buckroot")):
                return directory
            parent = os.path.dirname(directory)
            if parent == directory:
                break
            directory = parent
    return None


def configured_compression(root=None):
    """Read the preferred format from .buckconfig and .buckconfig.local."""
    root = root or _repo_root()
    if root is None:
        return "none"
    parser = configparser.ConfigParser(interpolation=None)
    parser.read([
        os.path.join(root, ".buckconfig"),
        os.path.join(root, ".buckconfig.local"),
    ])
    value = parser.get(_CONFIG_SECTION, _CONFIG_KEY, fallback="none").strip()
    if value not in _COMPRESSIONS:
        raise ValueError(
            "[{}] {} must be one of {}, got {!r}".format(
                _CONFIG_SECTION,
                _CONFIG_KEY,
                ", ".join(_COMPRESSIONS),
                value,
            )
        )
    return value


def strip_lockfile_suffix(path):
    """Remove a supported lockfile suffix, preferring the longest match."""
    for suffix in LOCKFILE_SUFFIXES:
        if path.endswith(suffix):
            return path[:-len(suffix)]
    raise ValueError(
        "lockfile must end in {} or {}: {}".format(
            PLAIN_SUFFIX, GZIP_SUFFIX, path,
        )
    )


def lockfile_name(
        flavor,
        release,
        architecture,
        compression=None,
        root=None):
    """Canonical name, using Buck configuration unless explicitly selected."""
    compression = compression or configured_compression(root)
    if compression not in _COMPRESSIONS:
        raise ValueError("unsupported lockfile compression {!r}".format(compression))
    suffix = GZIP_SUFFIX if compression == "gzip" else PLAIN_SUFFIX
    return "{}-{}-{}{}".format(flavor, release, architecture, suffix)


def find_lockfile(directory, flavor, release, architecture):
    """Return the one existing plain/compressed path, or None.

    Keeping both encodings for one identity would let two callers refresh
    different files. Refuse that state instead of choosing by preference.
    """
    candidates = [
        os.path.join(
            directory,
            lockfile_name(
                flavor,
                release,
                architecture,
                compression=compression,
            ),
        )
        for compression in _COMPRESSIONS
    ]
    found = [path for path in candidates if os.path.isfile(path)]
    if len(found) > 1:
        raise ValueError(
            "lockfile exists in both plain and compressed form: {}".format(
                ", ".join(found)
            )
        )
    return found[0] if found else None


def lockfile_releases(directory, flavor, architecture):
    """Return releases with either encoding and reject duplicate identities."""
    prefix = flavor + "-"
    by_release = {}
    for name in os.listdir(directory):
        if not name.startswith(prefix):
            continue
        for suffix in LOCKFILE_SUFFIXES:
            ending = "-{}{}".format(architecture, suffix)
            if not name.endswith(ending):
                continue
            release = name[len(prefix):-len(ending)]
            if not release:
                continue
            previous = by_release.get(release)
            if previous is not None:
                raise ValueError(
                    "release {} has both {} and {}".format(
                        release, previous, name,
                    )
                )
            by_release[release] = name
            break
    return sorted(by_release)


def load_lockfile(path):
    opener = gzip.open if path.endswith(GZIP_SUFFIX) else open
    with opener(path, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def dump_lockfile(value, path):
    """Write stable pretty JSON, gzip-compressing when the name ends in .gz."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if not path.endswith(GZIP_SUFFIX):
        with open(path, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
        return

    # Gzip normally records the current time and source filename in its
    # header. Neither is a lockfile input, so pin both before checking the
    # bytes into source control.
    with open(path, "wb") as raw:
        with gzip.GzipFile(
            filename="",
            mode="wb",
            compresslevel=9,
            fileobj=raw,
            mtime=0,
        ) as compressed:
            with io.TextIOWrapper(
                compressed, encoding="utf-8", newline="\n"
            ) as stream:
                json.dump(value, stream, indent=2, sort_keys=True)
                stream.write("\n")
