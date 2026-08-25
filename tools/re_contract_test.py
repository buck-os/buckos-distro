#!/usr/bin/env python3
"""Enforce the remote-execution contract over the rule sources.

Remote execution is a property of every action, and it fails *silently*
when it is wrong.  An action that reads the host but is allowed onto the
shared cache does not error -- it uploads a machine-specific artifact that
every other machine then downloads instead of building.  Nothing goes red;
the build just becomes subtly wrong for everyone.

Convention cannot catch that, because the failure is an omission: a rule
author adds `ctx.actions.run(...)` and simply does not think about RE.  So
the contract is checked here instead of documented and hoped about.

Two rules, from defs/buildroot_helpers.bzl:

  1. An action that consumes the buildroot must derive BOTH its
     `local_only` and its `allow_cache_upload` from that buildroot's
     provenance, via buildroot_local_only()/buildroot_cache_upload().
     Hardcoding either one is the cache-poisoning bug above.

  2. An action that does not consume the buildroot may hardcode them, but
     must say why -- a marker comment naming it buildroot-independent.
     unpacking a pinned artifact really is host-independent; the claim
     just has to be made explicitly rather than by omission.

Run as `buck2 test //tools:re_contract_test`.
"""

import os
import re
import unittest

def _repo_root():
    """Locate the source tree, whether run directly or under buck2.

    A source lint has to read the checked-in .bzl files, and under buck2
    __file__ points into buck-out rather than the source tree.  buck2 runs
    tests with the project root as cwd, so search upward from both for the
    .buckroot marker and take whichever finds it.
    """
    for start in (os.getcwd(), os.path.dirname(os.path.abspath(__file__))):
        path = start
        while True:
            if os.path.exists(os.path.join(path, ".buckroot")):
                return path
            parent = os.path.dirname(path)
            if parent == path:
                break
            path = parent
    raise AssertionError(
        "cannot find .buckroot from cwd {} or {}".format(
            os.getcwd(), os.path.abspath(__file__)
        )
    )


REPO_ROOT = _repo_root()
RULES_DIR = os.path.join(REPO_ROOT, "defs")

# Written inside or just above a ctx.actions.run() to assert the action's
# execution does not depend on the buildroot's provenance.
INDEPENDENCE_MARKER = "re-contract: buildroot-independent"

# Calling any of these means the action reads the buildroot, so rule 1
# applies to it.
BUILDROOT_CONSUMERS = (
    "buildroot_args(",
    "buildroot_sysroot_args(",
    "buildroot_env(",
    "dep_installroot_args(",
    "buildroot_info(",
)

LOCAL_ONLY_GOVERNED = "local_only = buildroot_local_only(ctx)"
CACHE_UPLOAD_GOVERNED = "allow_cache_upload = buildroot_cache_upload(ctx)"


def strip_docstrings(text):
    """Blank out triple-quoted blocks, preserving offsets.

    These files document the RE traps by showing the offending code, so a
    scanner that reads prose as source flags the documentation itself.
    Newlines are kept so reported line numbers stay correct.
    """
    out = list(text)
    for match in re.finditer(r'"""(?:.|\n)*?"""', text):
        for i in range(match.start(), match.end()):
            if out[i] != "\n":
                out[i] = " "
    return "".join(out)


def bzl_files():
    for dirpath, _dirnames, filenames in os.walk(RULES_DIR):
        for filename in filenames:
            if filename.endswith(".bzl"):
                yield os.path.join(dirpath, filename)


def _balanced_call(text, start):
    """Return the source of the call whose opening paren follows `start`.

    A regex cannot do this: the argument lists contain nested calls and
    parenthesised strings.  Counting depth while skipping string literals
    is enough for Starlark, which has no f-string interpolation.

    Comments have to be skipped too, and not as an afterthought: these
    argument lists are heavily commented, and an ordinary possessive like
    "the buildroot's provenance" would otherwise open a string literal
    that swallows the rest of the file.
    """
    depth = 0
    i = text.index("(", start)
    begin = i
    quote = None
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch == "#":
            newline = text.find("\n", i)
            i = len(text) if newline == -1 else newline
            continue
        elif ch in "\"'":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[begin : i + 1]
        i += 1
    raise AssertionError("unbalanced parentheses starting at offset {}".format(begin))


def _enclosing_def(text, offset):
    """Name of the `def` a given offset sits inside, for error messages."""
    best = "<module level>"
    for match in re.finditer(r"^def (\w+)", text[:offset], re.MULTILINE):
        best = match.group(1)
    return best


def _preceding_comment(text, offset, lines_back=6):
    """The comment block immediately above a call, where markers may live."""
    head = text[:offset].rstrip()
    lines = head.split("\n")
    collected = []
    for line in reversed(lines[-lines_back:]):
        stripped = line.strip()
        if stripped.startswith("#"):
            collected.append(stripped)
        elif collected:
            break
    return "\n".join(collected)


def find_actions():
    """Every ctx.actions.run() in the rule sources, with its context."""
    actions = []
    for path in sorted(bzl_files()):
        with open(path) as fh:
            text = fh.read()
        for match in re.finditer(r"ctx\.actions\.run\b", text):
            call = _balanced_call(text, match.start())
            func = _enclosing_def(text, match.start())
            # The whole impl function, so we can tell whether this rule
            # touches the buildroot at all.
            func_start = text.rfind("\ndef ", 0, match.start())
            func_end = text.find("\ndef ", match.start())
            body = text[func_start : func_end if func_end != -1 else len(text)]
            actions.append({
                "path": os.path.relpath(path, REPO_ROOT),
                "line": text[: match.start()].count("\n") + 1,
                "func": func,
                "call": call,
                "body": body,
                "comment": _preceding_comment(text, match.start()),
            })
    return actions


def find_executor_configs():
    """Every CommandExecutorConfig() in the rule sources."""
    configs = []
    for path in sorted(bzl_files()):
        with open(path) as fh:
            text = fh.read()
        for match in re.finditer(r"CommandExecutorConfig\b", text):
            configs.append({
                "path": os.path.relpath(path, REPO_ROOT),
                "line": text[: match.start()].count("\n") + 1,
                "call": _balanced_call(text, match.start()),
            })
    return configs


class TestExecutorConfig(unittest.TestCase):
    """The execution platform must be able to *do* what its flags claim.

    Distinct from the contract tests below, which govern individual
    actions.  This one exists because the RE switch was dead: buck2
    rejects `remote_enabled = True` unless remote_execution_properties is
    also set, so `-c buckos.remote_execution=true` failed in analysis
    rather than dispatching anything.  Every action was correctly
    annotated for an RE that could not be turned on.

    The failure mode is what makes it worth a test: with RE off by
    default, nothing exercises the enabled path, so the switch can rot
    indefinitely while every other test stays green.
    """

    @classmethod
    def setUpClass(cls):
        cls.configs = find_executor_configs()

    def test_finds_the_configs(self):
        """A scanner that matches nothing would pass the next test."""
        self.assertGreaterEqual(
            len(self.configs),
            1,
            "expected an execution platform in defs/; the scanner probably "
            "stopped matching",
        )

    def test_remote_enabled_platforms_are_dispatchable(self):
        """Anything that can enable RE must say what to match a worker on."""
        for config in self.configs:
            if "remote_enabled" not in config["call"]:
                continue
            where = "{}:{}".format(config["path"], config["line"])
            self.assertIn(
                "remote_execution_properties",
                config["call"],
                "{}: sets remote_enabled without "
                "remote_execution_properties. buck2 rejects that "
                "combination during analysis, so enabling RE would fail "
                "the build rather than run anything remotely.".format(where),
            )
            self.assertIn(
                "remote_execution_use_case",
                config["call"],
                "{}: sets remote_enabled without "
                "remote_execution_use_case, which the backend needs to "
                "attribute and schedule the work.".format(where),
            )


class TestRemoteExecutionContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.actions = find_actions()

    def test_finds_the_actions(self):
        """Guard against the scanner silently matching nothing.

        Without this, deleting defs/ or breaking the regex would make every
        other test in this file pass vacuously.
        """
        self.assertGreaterEqual(
            len(self.actions),
            4,
            "expected to find the replay pipeline's actions; the scanner "
            "probably stopped matching",
        )

    def test_buildroot_actions_derive_their_execution(self):
        """Rule 1: consume the buildroot, derive both RE flags from it."""
        for action in self.actions:
            consumes = any(c in action["body"] for c in BUILDROOT_CONSUMERS)
            if not consumes:
                continue
            where = "{}:{} in {}".format(
                action["path"], action["line"], action["func"]
            )
            self.assertIn(
                LOCAL_ONLY_GOVERNED,
                action["call"],
                "{}: this action reads the buildroot, so it must set "
                "{} -- otherwise a host-provenance build is dispatched to "
                "an RE worker that has no rpm macros".format(
                    where, LOCAL_ONLY_GOVERNED
                ),
            )
            self.assertIn(
                CACHE_UPLOAD_GOVERNED,
                action["call"],
                "{}: this action reads the buildroot, so it must set "
                "{} -- otherwise a non-hermetic artifact is uploaded to the "
                "shared cache and served to other machines".format(
                    where, CACHE_UPLOAD_GOVERNED
                ),
            )

    def test_independent_actions_say_so(self):
        """Rule 2: hardcoding the RE flags requires an explicit claim."""
        for action in self.actions:
            consumes = any(c in action["body"] for c in BUILDROOT_CONSUMERS)
            if consumes:
                continue
            where = "{}:{} in {}".format(
                action["path"], action["line"], action["func"]
            )
            declared = (
                INDEPENDENCE_MARKER in action["call"]
                or INDEPENDENCE_MARKER in action["comment"]
            )
            self.assertTrue(
                declared,
                "{}: this action hardcodes its execution flags without "
                "consuming a buildroot. If that is correct, say so with a "
                "'{}' comment naming why it is host-independent.".format(
                    where, INDEPENDENCE_MARKER
                ),
            )

    def test_no_action_forces_local_unconditionally(self):
        """A literal local_only = True would quietly opt out of RE forever.

        The legitimate way to be local-only is to run against a
        non-hermetic buildroot, which buildroot_local_only() reports.
        """
        for action in self.actions:
            where = "{}:{} in {}".format(
                action["path"], action["line"], action["func"]
            )
            self.assertNotRegex(
                action["call"],
                r"local_only\s*=\s*True",
                "{}: hardcodes local_only = True, which opts this action "
                "out of remote execution permanently. Derive it from the "
                "buildroot instead.".format(where),
            )

    def test_projections_carry_the_whole_tree(self):
        """A subpath of a tree artifact must also pass the tree as hidden.

        This is the materialization trap, and it is invisible locally: on
        a developer's machine the rest of the tree is already on disk, so
        an under-declared action works fine.  On an RE worker only the
        declared inputs exist, so projecting `buildroot.project("usr/bin")`
        materializes that one directory and leaves usr/lib64/libc.so.6
        absent -- the tools then fail to load their own libraries, far from
        the rule that under-declared them.
        """
        for path in sorted(bzl_files()):
            with open(path) as fh:
                text = strip_docstrings(fh.read())
            for match in re.finditer(r"\.project\(", text):
                line_start = text.rfind("\n", 0, match.start()) + 1
                # A projection is only safe inside a cmd_args() that also
                # passes the underlying tree as a hidden input.
                call_start = text.rfind("cmd_args(", line_start, match.start())
                enclosing = (
                    _balanced_call(text, call_start)
                    if call_start != -1
                    else text[line_start : text.find("\n", match.start())]
                )
                if "hidden" in enclosing:
                    continue
                # Prose in a docstring or comment describing the trap is
                # not itself a violation.
                line_text = text[line_start : text.find("\n", match.start())]
                if line_text.lstrip().startswith(("#", "*", '"')):
                    continue
                self.fail(
                    "{}:{}: projects a subpath of a tree artifact without "
                    "passing the whole tree as a hidden input. On RE only "
                    "the projected subdirectory materializes.".format(
                        os.path.relpath(path, REPO_ROOT),
                        text[: match.start()].count("\n") + 1,
                    )
                )

    def test_dependencies_reach_actions_through_the_helper(self):
        """Dependency installroots must go through dep_installroot_args().

        That helper is what attaches `hidden = prefix` to every dependency,
        which is what makes the dependency materialize on a remote worker.
        A rule that hand-rolls the flag gets the path onto the command line
        without declaring the input, which again works locally and fails
        remotely.
        """
        for path in sorted(bzl_files()):
            if path.endswith("buildroot_helpers.bzl"):
                continue
            with open(path) as fh:
                text = fh.read()
            for match in re.finditer(r'"--dep-installroot"', text):
                self.fail(
                    "{}:{}: builds the --dep-installroot flag by hand. Use "
                    "dep_installroot_args(), which attaches the hidden "
                    "input that makes the dependency materialize on "
                    "RE.".format(
                        os.path.relpath(path, REPO_ROOT),
                        text[: match.start()].count("\n") + 1,
                    )
                )

    def test_helpers_are_not_bypassed(self):
        """`hermetic` must only be read through the two helpers.

        Re-deriving the RE disposition inline is how the two call sites
        drift apart -- one gets updated, the other does not.
        """
        for path in sorted(bzl_files()):
            if path.endswith("buildroot_helpers.bzl"):
                continue
            with open(path) as fh:
                text = fh.read()
            for match in re.finditer(r"\.hermetic\b", text):
                line = text[: match.start()].count("\n") + 1
                self.fail(
                    "{}:{}: reads BuildrootInfo.hermetic directly. Use "
                    "buildroot_local_only()/buildroot_cache_upload() so the "
                    "policy lives in one place.".format(
                        os.path.relpath(path, REPO_ROOT), line
                    )
                )


if __name__ == "__main__":
    unittest.main()
