"""Locate the BuckOS source tree for tools that inspect checked-in files."""

import os


REPO_ROOT_ENV = "BUCKOS_REPO_ROOT"


def _candidate(start):
    path = os.path.realpath(os.fspath(start))
    if os.path.isfile(path):
        path = os.path.dirname(path)
    while True:
        if os.path.isfile(os.path.join(path, ".buckroot")):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent


def find_repo_root(*starts):
    configured = os.environ.get(REPO_ROOT_ENV)
    if configured:
        root = os.path.abspath(configured)
        if not os.path.isfile(os.path.join(root, ".buckroot")):
            raise ValueError(
                "{} does not name a BuckOS repository: {}".format(
                    REPO_ROOT_ENV,
                    configured,
                )
            )
        return root

    if not starts:
        starts = (os.getcwd(), __file__)
    for start in starts:
        root = _candidate(start)
        if root is not None:
            return root
    return None
