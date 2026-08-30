#!/usr/bin/env python3
"""Locate tools/_skips.py from a test in this directory.

The three suites here need the skip helper, and finding it is a bootstrap
problem rather than an import: buck2 supplies it through the //tools:_skips
dep so the plain import works, while a direct `python3 -m unittest` from this
directory has no tools/ on sys.path. Falling back to a local no-op would
disarm BUCKOS_REQUIRE_FULL_COVERAGE exactly where someone is running by hand,
so this locates the real file and fails if it is absent.

A sibling module rather than a copy in each suite: the directory is on
sys.path in both cases, so importing this by name has no bootstrap problem of
its own.
"""

import importlib.util
import sys
from pathlib import Path


def load_skips():
    """tools/_skips.py, whether run under buck2 or straight from this directory.

    buck2 supplies it through the //tools:_skips dep, so the plain import
    works there.  A direct `python3 -m unittest` from this directory has no
    tools/ on sys.path, and falling back to a local no-op would disarm
    BUCKOS_REQUIRE_FULL_COVERAGE exactly where someone is running by hand,
    so locate the real file instead and fail if it is not there.
    """
    try:
        import _skips

        return _skips
    except ImportError:
        pass
    for start in (Path.cwd(), Path(__file__).resolve().parent):
        for candidate in [start, *start.parents]:
            path = candidate / "tools" / "_skips.py"
            if path.is_file():
                spec = importlib.util.spec_from_file_location("_skips", path)
                assert spec is not None and spec.loader is not None
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                spec.loader.exec_module(module)
                return module
    raise ImportError("cannot locate tools/_skips.py")
