#!/usr/bin/env python3
"""A source lint: every skipped test case is declared, with a reason.

A skip reports as a pass.  `unittest` prints `OK (skipped=4)` and buck2,
which counts targets rather than cases, prints `Skip 0` above it.  So a
suite can lose coverage entirely and every summary a reviewer reads stays
green.  This tree did exactly that: the only mTLS handshake cases were
gated on both the `openssl` binary and the `grpcio` module, both were
missing on the machine about to gate an mTLS review, and the target
reported a tick.  Provisioning `openssl` alone moved the skip from one gate
to the other, and it still reported a tick.

A per-site fix does not hold.  Making each `skipUnless` a hard failure
repairs the sites someone thought about today and does nothing about the
one added next month, which is the same omission in a new place.  So the
inventory is checked here instead: every skip in the tree is enumerated
from the sources and must appear in REGISTERED below, with a
classification.  A new skip fails this test until someone writes down what
it is.  A registered skip that disappears fails too, because a lint whose
coverage silently shrinks is worse than no lint.

Three classifications, and the distinction is the point:

  environmental  The machine is missing a prerequisite.  This is the kind
                 that hides a real gap, so it must route through
                 tools/_skips.py, which fails instead of skipping when
                 BUCKOS_REQUIRE_FULL_COVERAGE=1.  Registering a site as
                 environmental while it uses a raw unittest skip is itself
                 a failure -- otherwise the classification is a comment.
  opt-in         Deliberately off by default, prerequisite is an artifact
                 nobody has.  Meant to stay skipped; escalating it would
                 make the suite unrunnable for everyone.
  platform       A CPU architecture this project does not target.  Cannot
                 fire on x86_64 or AArch64, so it hides nothing.

Read as text rather than imported, for the same reason re_contract_test.py
and scratch_contract_test.py are: the property is about what the source
says.  Importing a module proves nothing about a decorator that was
evaluated on some other machine.

Run as `buck2 test //tools:skip_contract_test`.
"""

import ast
import os
import unittest


def _repo_root():
    """Find the checked-in tree, which is not where this file runs from.

    buck2 runs a python_test out of a link-tree holding only its own srcs,
    so a sibling lookup relative to __file__ finds nothing -- and finding
    nothing is the dangerous case for a lint, because "no files to check"
    reads exactly like "no violations".  Same upward search for .buckroot
    as re_contract_test.py, and for the same reason.
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

# Every directory that can hold a test.  Listed rather than globbed from
# the repository root so that a new top-level tree is a deliberate
# decision, and so this lint never walks buck-out.
SCANNED = ("defs", "infra", "tests", "tools")

# Two files necessarily contain skip calls that are not skip sites: the
# implementation of the escalation, which holds the raw
# `raise unittest.SkipTest` everything else is forbidden, and this module,
# which has to invoke the helper to test that it behaves.  Exempted by
# exact name rather than by pattern, and asserted to exist below, so that
# renaming either one fails here instead of quietly widening the hole.
#
# The cost is real and worth stating: a genuine skip added to either file
# escapes this lint.  Both are small and exist only to serve it.
MECHANISM = os.path.join("tools", "_skips.py")
SELF = os.path.join("tools", "skip_contract_test.py")
EXEMPT = (MECHANISM, SELF)

# Raw unittest skips.  A site using one of these can only be opt-in or
# platform; see the module docstring.
RAW_DECORATORS = frozenset(("skip", "skipIf", "skipUnless"))
RAW_CALLS = frozenset(("skipTest",))
RAW_RAISES = frozenset(("SkipTest",))

# The tools/_skips.py forms.  A site using one of these must be
# environmental, and vice versa.
HELPER_DECORATORS = frozenset(("environmental_skip_unless",))
HELPER_CALLS = frozenset(("environmental_skip",))

CLASSIFICATIONS = frozenset(("environmental", "opt-in", "platform"))

# path, qualified name, kind, classification, reason.
#
# The qualified name rather than a line number: a line number churns on
# every edit above it and would make this lint a nuisance rather than a
# gate.  The reason is recorded too, so rewording a skip message is a
# deliberate update here rather than a silent drift.
REGISTERED = (
    (
        "infra/remote-execution/scripts/sdme_tls_test.py",
        "SdmeTlsTest.setUp",
        "call:environmental_skip",
        "environmental",
        "openssl is unavailable",
    ),
    (
        "infra/remote-execution/scripts/sdme_provision_test.py",
        "ProvisionPlanTest.pki_material",
        "call:environmental_skip",
        "environmental",
        "openssl is unavailable",
    ),
    (
        "infra/remote-execution/scripts/reapi_readiness_test.py",
        "TlsHandshakeTest",
        "decorator:environmental_skip_unless",
        "environmental",
        "openssl is unavailable",
    ),
    (
        "infra/remote-execution/scripts/reapi_readiness_test.py",
        "TlsHandshakeTest.setUpClass",
        "call:environmental_skip",
        "environmental",
        "<dynamic>",
    ),
    (
        "infra/remote-execution/scripts/sdme_provision_test.py",
        "ProvisionPlanTest.native_architecture",
        "call:skipTest",
        "platform",
        "unsupported test architecture",
    ),
    (
        "infra/remote-execution/scripts/sdme_provision_test.py",
        "ProvisionPlanTest.test_existing_filesystem_does_not_relax_incomplete_cache",
        "call:skipTest",
        "platform",
        "unsupported test architecture",
    ),
    (
        "infra/remote-execution/scripts/sdme_provision_test.py",
        "ProvisionPlanTest.test_prepare_runtime_accepts_offline_archives_without_podman",
        "call:skipTest",
        "platform",
        "unsupported test architecture",
    ),
    (
        "infra/remote-execution/scripts/sdme_provision_test.py",
        "ProvisionPlanTest.test_prepare_runtime_has_no_container_or_service_operations",
        "call:skipTest",
        "platform",
        "unsupported test architecture",
    ),
    (
        "infra/remote-execution/scripts/sdme_provision_test.py",
        "ProvisionPlanTest.test_prepare_runtime_recovers_archive_publication_with_transaction",
        "call:skipTest",
        "platform",
        "unsupported test architecture",
    ),
    (
        "infra/remote-execution/scripts/sdme_provision_test.py",
        "ProvisionPlanTest.test_prepare_runtime_rejects_archive_without_provenance_or_transaction",
        "call:skipTest",
        "platform",
        "unsupported test architecture",
    ),
    (
        "infra/remote-execution/scripts/sdme_provision_test.py",
        "ProvisionPlanTest.test_worker_apply_still_requires_probe_contract",
        "call:skipTest",
        "platform",
        "unsupported test architecture",
    ),
    (
        "infra/remote-execution/tests/pinned_mtls_regression_test.py",
        "PinnedMtlsRegressionTest",
        "decorator:skipUnless",
        "opt-in",
        "set BUCKOS_RUN_PINNED_MTLS_REGRESSION=1 for the exact-binary test",
    ),
)


def _called_name(node):
    """The bare attribute or identifier a call or reference resolves to."""
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Attribute):
        return target.attr
    return getattr(target, "id", None)


def _string_argument(node):
    """The last literal string among a call's arguments, or a placeholder.

    Last rather than first because the reason follows the condition in
    both `skipUnless(condition, reason)` and its helper equivalent.  A
    reason computed at runtime records as `<dynamic>`, which is honest:
    there is no fixed string to pin.
    """
    if not isinstance(node, ast.Call):
        return "<dynamic>"
    found = "<dynamic>"
    for argument in list(node.args) + [keyword.value for keyword in node.keywords]:
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            found = argument.value
    return found


class _SkipVisitor(ast.NodeVisitor):
    def __init__(self, relative_path):
        self.relative_path = relative_path
        self.scope = []
        self.found = []

    def _qualified(self, name=None):
        parts = self.scope + ([name] if name else [])
        return ".".join(parts) if parts else "<module>"

    def _record(self, qualified, kind, reason):
        self.found.append((self.relative_path, qualified, kind, reason))

    def _decorators(self, node):
        for decorator in node.decorator_list:
            name = _called_name(decorator)
            if name in RAW_DECORATORS or name in HELPER_DECORATORS:
                self._record(
                    self._qualified(node.name),
                    "decorator:" + name,
                    _string_argument(decorator),
                )

    def visit_ClassDef(self, node):
        self._decorators(node)
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def _visit_function(self, node):
        self._decorators(node)
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_Call(self, node):
        name = _called_name(node)
        if name in RAW_CALLS or name in HELPER_CALLS:
            self._record(self._qualified(), "call:" + name, _string_argument(node))
        self.generic_visit(node)

    def visit_Raise(self, node):
        if node.exc is not None:
            name = _called_name(node.exc)
            if name in RAW_RAISES:
                self._record(
                    self._qualified(), "raise:" + name, _string_argument(node.exc)
                )
        self.generic_visit(node)


def discover(repo_root=REPO_ROOT):
    """Every skip site in the scanned trees, as (path, qualname, kind, reason)."""
    found = []
    for directory in SCANNED:
        base = os.path.join(repo_root, directory)
        if not os.path.isdir(base):
            raise AssertionError(
                "{} is in SCANNED but is not a directory; update the list "
                "deliberately".format(directory)
            )
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [name for name in dirnames if name != "__pycache__"]
            for filename in sorted(filenames):
                if not filename.endswith(".py"):
                    continue
                path = os.path.join(dirpath, filename)
                relative = os.path.relpath(path, repo_root)
                if relative in EXEMPT:
                    continue
                with open(path, encoding="utf-8") as handle:
                    tree = ast.parse(handle.read(), filename=relative)
                visitor = _SkipVisitor(relative)
                visitor.visit(tree)
                found.extend(visitor.found)
    return sorted(found)


class TestSkipInventory(unittest.TestCase):
    def setUp(self):
        self.discovered = {site[:3]: site[3] for site in discover()}
        self.registered = {site[:3]: site[4] for site in REGISTERED}

    def test_no_unregistered_skip(self):
        extra = sorted(set(self.discovered) - set(self.registered))
        self.assertEqual(
            extra,
            [],
            "these skips are not registered in skip_contract_test.REGISTERED. "
            "Add each one with a classification of environmental, opt-in, or "
            "platform, and use tools/_skips.py if it is environmental. See "
            "this module's docstring for what the classifications mean:\n"
            + "\n".join("  {} {} {}".format(*site) for site in extra),
        )

    def test_no_registered_skip_has_vanished(self):
        """A lint whose coverage shrinks silently is worse than no lint."""
        missing = sorted(set(self.registered) - set(self.discovered))
        self.assertEqual(
            missing,
            [],
            "these skips are registered but no longer exist in the sources. "
            "If the skip was removed, remove its entry; if it moved or was "
            "renamed, update the entry deliberately:\n"
            + "\n".join("  {} {} {}".format(*site) for site in missing),
        )

    def test_reasons_match(self):
        for key in sorted(set(self.discovered) & set(self.registered)):
            with self.subTest(site=key):
                self.assertEqual(
                    self.registered[key],
                    self.discovered[key],
                    "the skip reason at {} {} changed; update REGISTERED "
                    "deliberately".format(key[0], key[1]),
                )


class TestSkipClassification(unittest.TestCase):
    def test_every_classification_is_known(self):
        for site in REGISTERED:
            with self.subTest(site=site[:3]):
                self.assertIn(site[3], CLASSIFICATIONS)

    def test_environmental_skips_use_the_helper(self):
        """The positive half: a classification that changes nothing is a comment.

        Without this, registering a raw `skipUnless` as environmental would
        pass the inventory check while BUCKOS_REQUIRE_FULL_COVERAGE quietly
        did nothing to it -- which is the original bug wearing a label.
        """
        for path, qualified, kind, classification, _reason in REGISTERED:
            if classification != "environmental":
                continue
            with self.subTest(site=(path, qualified)):
                name = kind.split(":", 1)[1]
                self.assertTrue(
                    name in HELPER_DECORATORS or name in HELPER_CALLS,
                    "{} {} is registered environmental but uses the raw "
                    "unittest skip `{}`, so BUCKOS_REQUIRE_FULL_COVERAGE "
                    "cannot escalate it. Use tools/_skips.py.".format(
                        path, qualified, name
                    ),
                )

    def test_helper_use_is_classified_environmental(self):
        """And the converse, so the helper cannot be used to hide an opt-in."""
        for path, qualified, kind, classification, _reason in REGISTERED:
            name = kind.split(":", 1)[1]
            if name not in HELPER_DECORATORS and name not in HELPER_CALLS:
                continue
            with self.subTest(site=(path, qualified)):
                self.assertEqual(
                    "environmental",
                    classification,
                    "{} {} uses tools/_skips.py but is registered {}; the "
                    "helper exists for environmental gates only".format(
                        path, qualified, classification
                    ),
                )

    def test_exempt_files_exist(self):
        """EXEMPT is skipped during the walk, so a rename must fail here."""
        for relative in EXEMPT:
            with self.subTest(path=relative):
                self.assertTrue(
                    os.path.isfile(os.path.join(REPO_ROOT, relative)),
                    "{} does not exist; it is exempted from the scan, so a "
                    "rename would silently widen the exemption".format(relative),
                )


class TestEscalation(unittest.TestCase):
    """The helper itself, since the classification above delegates to it."""

    def setUp(self):
        import _skips

        self.skips = _skips
        self.previous = os.environ.get(_skips.REQUIRE_FULL_COVERAGE_ENV)
        self.addCleanup(self.restore)

    def restore(self):
        variable = self.skips.REQUIRE_FULL_COVERAGE_ENV
        if self.previous is None:
            os.environ.pop(variable, None)
        else:
            os.environ[variable] = self.previous

    def require(self, value):
        variable = self.skips.REQUIRE_FULL_COVERAGE_ENV
        if value is None:
            os.environ.pop(variable, None)
        else:
            os.environ[variable] = value

    def test_satisfied_gate_is_untouched_either_way(self):
        for value in (None, "1"):
            with self.subTest(require=value):
                self.require(value)

                class Case(unittest.TestCase):
                    def test_ok(self):
                        pass

                decorated = self.skips.environmental_skip_unless(True, "reason")(Case)
                self.assertIs(decorated, Case)
                self.assertFalse(getattr(decorated, "__unittest_skip__", False))

    def test_unsatisfied_gate_skips_by_default(self):
        self.require(None)

        class Case(unittest.TestCase):
            def test_ok(self):
                pass

        decorated = self.skips.environmental_skip_unless(False, "no openssl")(Case)
        self.assertTrue(decorated.__unittest_skip__)
        self.assertEqual("no openssl", decorated.__unittest_skip_why__)

    def test_unsatisfied_gate_fails_under_full_coverage(self):
        self.require("1")

        class Case(unittest.TestCase):
            def test_one(self):
                pass

            def test_two(self):
                pass

        decorated = self.skips.environmental_skip_unless(False, "no openssl")(Case)
        self.assertFalse(getattr(decorated, "__unittest_skip__", False))
        with open(os.devnull, "w", encoding="utf-8") as sink:
            result = unittest.TextTestRunner(stream=sink, verbosity=0).run(
                unittest.TestLoader().loadTestsFromTestCase(decorated)
            )
        self.assertEqual(0, len(result.skipped))
        self.assertEqual(2, len(result.failures) + len(result.errors))
        self.assertEqual(2, result.testsRun)

    def test_failing_setupclass_does_not_mask_the_named_cases(self):
        """The real shape: two gates on one class, the second inside setUpClass.

        Left alone, setUpClass raises before any test method runs and
        unittest reports one error naming the second prerequisite, hiding
        both the first gate and the number of cases it cost.
        """
        self.require("1")

        class Case(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                raise AssertionError("second gate, checked later")

            def test_one(self):
                pass

            def test_two(self):
                pass

        decorated = self.skips.environmental_skip_unless(False, "no openssl")(Case)
        with open(os.devnull, "w", encoding="utf-8") as sink:
            result = unittest.TextTestRunner(stream=sink, verbosity=0).run(
                unittest.TestLoader().loadTestsFromTestCase(decorated)
            )
        self.assertEqual(2, result.testsRun)
        self.assertEqual(2, len(result.failures) + len(result.errors))
        reported = "".join(text for _case, text in result.failures + result.errors)
        self.assertIn("no openssl", reported)
        self.assertNotIn("second gate", reported)

    def test_a_class_with_no_cases_still_fails_rather_than_passing(self):
        self.require("1")

        class Case(unittest.TestCase):
            pass

        decorated = self.skips.environmental_skip_unless(False, "no openssl")(Case)
        with self.assertRaises(AssertionError):
            decorated.setUpClass()

    def test_dynamic_skip_raises_skiptest_by_default(self):
        self.require(None)
        with self.assertRaises(unittest.SkipTest):
            self.skips.environmental_skip("grpcio is unavailable")

    def test_dynamic_skip_fails_under_full_coverage(self):
        self.require("1")
        with self.assertRaises(AssertionError) as caught:
            self.skips.environmental_skip("grpcio is unavailable")
        self.assertNotIsInstance(caught.exception, unittest.SkipTest)
        self.assertIn("grpcio is unavailable", str(caught.exception))

    def test_only_an_exact_one_enables_the_escalation(self):
        """So a stray `true` or `0` cannot silently arm or disarm the gate."""
        for value in ("0", "", "true", "yes", "2"):
            with self.subTest(value=value):
                self.require(value)
                self.assertFalse(self.skips.full_coverage_required())
                with self.assertRaises(unittest.SkipTest):
                    self.skips.environmental_skip("reason")


if __name__ == "__main__":
    unittest.main(verbosity=2)
