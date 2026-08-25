#!/usr/bin/env python3
"""Tests for the dependency-graph algorithms.

Runs offline with synthetic data -- no rpm, no network, no repodata.
That is the point: the graph logic is the part most likely to be subtly
wrong, so it is kept free of I/O and tested directly.

    python3 tools/depgraph_test.py
"""

import sys
import unittest

from depgraph import (
    AmbiguousProvider,
    UnresolvedCapability,
    bootstrap_depth,
    find_cycles,
    is_rich_dep,
    parse_conditional_dep,
    parse_or_dep,
    parse_boolean,
    parse_range_dep,
    unparse_boolean,
    plan_build_order,
    project_to_source_graph,
    resolve_capability,
    runtime_closure,
    stage_cycle,
    strip_capability_version,
    strongly_connected_components,
    topological_order,
    unwrap_group,
    validate_overrides,
)


class TestCapabilityParsing(unittest.TestCase):
    def test_strips_version_constraints(self):
        self.assertEqual(strip_capability_version("gcc-c++ >= 4.8"), "gcc-c++")
        self.assertEqual(strip_capability_version("zlib = 1.3.1"), "zlib")
        self.assertEqual(strip_capability_version("foo < 2"), "foo")

    def test_preserves_isa_and_pkgconfig_forms(self):
        # The parenthesised suffix is part of the capability name, not a
        # version constraint -- stripping it would break the provides map.
        self.assertEqual(
            strip_capability_version("libfoo(x86-64)"), "libfoo(x86-64)"
        )
        self.assertEqual(
            strip_capability_version("pkgconfig(zlib) >= 1.2"), "pkgconfig(zlib)"
        )

    def test_detects_rich_deps(self):
        self.assertTrue(is_rich_dep("(python3-devel if python3)"))
        self.assertTrue(is_rich_dep("  (a or b)"))
        self.assertFalse(is_rich_dep("pkgconfig(zlib)"))

    def test_leaves_rich_deps_intact(self):
        # A version constraint *inside* a rich dep is not a trailing
        # constraint on the expression.  Splitting at " = " here would
        # produce "(cmake-rpm-macros" and throw the condition away.
        cap = "(cmake-rpm-macros = 3.31.6-4.fc43 if rpm-build)"
        self.assertEqual(strip_capability_version(cap), cap)


class TestConditionalDeps(unittest.TestCase):
    def test_parses_simple_conditional(self):
        self.assertEqual(
            parse_conditional_dep("(annobin-plugin-gcc if gcc)"),
            ("annobin-plugin-gcc", "gcc"),
        )

    def test_strips_versions_from_both_halves(self):
        self.assertEqual(
            parse_conditional_dep("(cmake-rpm-macros = 3.31.6-4.fc43 if rpm-build)"),
            ("cmake-rpm-macros", "rpm-build"),
        )

    def test_keeps_isa_suffix_in_the_then_half(self):
        self.assertEqual(
            parse_conditional_dep("(glibc-gconv-extra(x86-64) = 2.42 if glibc)"),
            ("glibc-gconv-extra(x86-64)", "glibc"),
        )

    def test_refuses_compound_expressions(self):
        # A partial reading of a boolean expression is worse than none.
        for cap in (
            "(a or b)",
            "(a if b else c)",
            "(python3dist(x) < 5 with python3dist(x) >= 4)",
            "(a if b and c)",
            "(a unless b)",
        ):
            self.assertIsNone(parse_conditional_dep(cap), cap)

    def test_ignores_plain_capabilities(self):
        self.assertIsNone(parse_conditional_dep("pkgconfig(zlib)"))


class TestRangeDeps(unittest.TestCase):
    def test_collapses_a_version_range_to_its_capability(self):
        # The exact dep `rpm --install` rejected the F43 seed over.
        self.assertEqual(
            parse_range_dep(
                "(python3.14dist(gitdb) < 5~~ with "
                "python3.14dist(gitdb) >= 4.0.1)"
            ),
            "python3.14dist(gitdb)",
        )

    def test_order_of_the_bounds_does_not_matter(self):
        self.assertEqual(
            parse_range_dep("(foo >= 1 with foo < 2)"),
            "foo",
        )

    def test_refuses_a_conjunction_of_different_capabilities(self):
        # "one package providing both" -- picking either name is a guess.
        self.assertIsNone(parse_range_dep("(foo with bar)"))

    def test_refuses_chained_with(self):
        self.assertIsNone(parse_range_dep("(foo >= 1 with foo < 2 with bar)"))

    def test_refuses_other_operators(self):
        for cap in ("(a or b)", "(a and b)", "(a if b)", "(a without b)"):
            self.assertIsNone(parse_range_dep(cap), cap)

    def test_ignores_plain_capabilities(self):
        self.assertIsNone(parse_range_dep("pkgconfig(zlib)"))


class TestGroupUnwrapping(unittest.TestCase):
    def test_drops_parens_that_only_group(self):
        self.assertEqual(unwrap_group("(foo >= 1)"), "foo >= 1")

    def test_keeps_parens_belonging_to_the_capability(self):
        # The inner parens are part of the name; the outer ones are not.
        self.assertEqual(unwrap_group("(pkgconfig(zlib) >= 1)"),
                         "pkgconfig(zlib) >= 1")
        # No outer group at all.
        self.assertEqual(unwrap_group("pkgconfig(zlib)"), "pkgconfig(zlib)")

    def test_leaves_real_expressions_alone(self):
        for cap in ("(a or b)", "(a if b)", "(a with b)", "(a and b)"):
            self.assertEqual(unwrap_group(cap), cap)

    def test_refuses_a_sequence_of_groups(self):
        # The first paren closes early, so these are not one group and
        # unwrapping would drop everything after it.
        self.assertEqual(unwrap_group("(a) (b)"), "(a) (b)")

    def test_makes_a_parenthesised_conditional_readable(self):
        # kernel-core's actual Requires.  Before unwrapping, the `then`
        # half looked rich and the whole dep was refused.
        self.assertEqual(
            parse_conditional_dep(
                "((linux-firmware >= 20150904-56.git6ebf5d57) if linux-firmware)"
            ),
            ("linux-firmware", "linux-firmware"),
        )


class TestOrDeps(unittest.TestCase):
    def test_splits_alternatives(self):
        # systemd's actual Requires.
        self.assertEqual(
            parse_or_dep("(util-linux-core or util-linux)"),
            ["util-linux-core", "util-linux"],
        )

    def test_strips_versions_from_each_branch(self):
        self.assertEqual(parse_or_dep("(foo >= 1 or bar = 2)"), ["foo", "bar"])

    def test_handles_more_than_two_branches(self):
        self.assertEqual(parse_or_dep("(a or b or c)"), ["a", "b", "c"])

    def test_refuses_mixed_operators(self):
        # A precedence question this two-way split does not answer.
        for cap in ("(a or b if c)", "(a or b with c)", "(a and b or c)"):
            self.assertIsNone(parse_or_dep(cap), cap)

    def test_ignores_other_shapes(self):
        self.assertIsNone(parse_or_dep("(a if b)"))
        self.assertIsNone(parse_or_dep("plain-capability"))

    def test_satisfied_branch_pulls_nothing_extra(self):
        # util-linux is already a root, so the choice is met and the
        # smaller alternative must not also be dragged in.
        requires = {"systemd": ["(util-linux-core or util-linux)"]}
        provides = {
            "systemd": ["systemd"],
            "util-linux": ["util-linux"],
            "util-linux-core": ["util-linux-core"],
        }
        closure, problems = runtime_closure(
            ["systemd", "util-linux"], requires, provides
        )
        self.assertEqual(closure, {"systemd", "util-linux"})
        self.assertEqual(problems, [])

    def test_branch_satisfied_later_still_counts(self):
        # The choice is read before anything provides a branch; the
        # fixed-point recheck is what keeps it from being a problem.
        requires = {
            "systemd": ["(util-linux-core or util-linux)", "dbus"],
            "dbus": ["util-linux"],
        }
        provides = {
            "systemd": ["systemd"], "dbus": ["dbus"],
            "util-linux": ["util-linux"], "util-linux-core": ["util-linux-core"],
        }
        closure, problems = runtime_closure(["systemd"], requires, provides)
        self.assertEqual(closure, {"systemd", "dbus", "util-linux"})
        self.assertEqual(problems, [])

    def test_no_branch_present_is_reported_not_guessed(self):
        requires = {"systemd": ["(util-linux-core or util-linux)"]}
        provides = {
            "systemd": ["systemd"],
            "util-linux": ["util-linux"],
            "util-linux-core": ["util-linux-core"],
        }
        closure, problems = runtime_closure(["systemd"], requires, provides)
        self.assertEqual(closure, {"systemd"})
        self.assertEqual([p[0] for p in problems], ["choice"])
        self.assertIn("--override", problems[0][1])

    def test_override_settles_the_choice(self):
        requires = {"systemd": ["(util-linux-core or util-linux)"]}
        provides = {
            "systemd": ["systemd"],
            "util-linux": ["util-linux"],
            "util-linux-core": ["util-linux-core"],
        }
        closure, problems = runtime_closure(
            ["systemd"], requires, provides,
            {"(util-linux-core or util-linux)": "util-linux-core"},
        )
        self.assertEqual(closure, {"systemd", "util-linux-core"})
        self.assertEqual(problems, [])

    def test_branch_may_be_a_capability_rather_than_a_package(self):
        # `alt in seen` would never match here: seen holds package names.
        requires = {"app": ["(libfoo.so.1 or libbar.so.1)"]}
        provides = {
            "app": ["app"],
            "libfoo.so.1": ["foo"],
            "libbar.so.1": ["bar"],
            "foo": ["foo"],
        }
        closure, problems = runtime_closure(["app", "foo"], requires, provides)
        self.assertEqual(closure, {"app", "foo"})
        self.assertEqual(problems, [])


class TestCapabilityResolution(unittest.TestCase):
    def test_unique_provider(self):
        provides = {"pkgconfig(zlib)": ["zlib-devel"]}
        self.assertEqual(
            resolve_capability("pkgconfig(zlib)", provides, "curl"), "zlib-devel"
        )

    def test_exact_name_match_wins_over_other_providers(self):
        # Both provide it, but zlib-devel *is* the capability name.
        provides = {"zlib-devel": ["zlib-devel", "zlib-ng-compat-devel"]}
        self.assertEqual(
            resolve_capability("zlib-devel", provides, "curl"), "zlib-devel"
        )

    def test_ambiguity_raises_rather_than_guessing(self):
        provides = {"java-devel": ["openjdk-17-devel", "openjdk-21-devel"]}
        with self.assertRaises(AmbiguousProvider):
            resolve_capability("java-devel", provides, "maven")

    def test_override_resolves_ambiguity(self):
        provides = {"java-devel": ["openjdk-17-devel", "openjdk-21-devel"]}
        self.assertEqual(
            resolve_capability(
                "java-devel", provides, "maven",
                overrides={"java-devel": "openjdk-21-devel"},
            ),
            "openjdk-21-devel",
        )

    def test_missing_provider_raises(self):
        with self.assertRaises(UnresolvedCapability):
            resolve_capability("nonexistent(thing)", {}, "somepkg")


class TestRuntimeClosure(unittest.TestCase):
    def test_closes_over_transitive_requires(self):
        # zlib-devel needs zlib needs glibc
        requires = {
            "zlib-devel": ["zlib"],
            "zlib": ["glibc"],
            "glibc": [],
        }
        provides = {"zlib-devel": ["zlib-devel"], "zlib": ["zlib"], "glibc": ["glibc"]}
        closure, problems = runtime_closure(["zlib-devel"], requires, provides)
        self.assertEqual(closure, {"zlib-devel", "zlib", "glibc"})
        self.assertEqual(problems, [])

    def test_handles_cyclic_requires_without_hanging(self):
        # Runtime Requires cycles are legal and common in rpm.
        requires = {"a": ["b"], "b": ["a"]}
        provides = {"a": ["a"], "b": ["b"]}
        closure, problems = runtime_closure(["a"], requires, provides)
        self.assertEqual(closure, {"a", "b"})
        self.assertEqual(problems, [])

    def test_collects_problems_instead_of_raising(self):
        # Three different failures, each reported as what it actually is.
        #
        # "(x or y)" is a `choice`: the expression was understood and what
        # is missing is a branch to satisfy it.  "(x and y)" is understood
        # too, and reports each term it cannot resolve rather than one
        # opaque complaint about the expression.  Only "(p or q and r)" is
        # `rich` -- mixing operators without parentheses is something rpm's
        # own grammar forbids, so there is no reading to guess at.
        requires = {
            "a": ["missing-thing", "(x or y)", "(x and y)", "(p or q and r)"],
        }
        provides = {"a": ["a"]}
        closure, problems = runtime_closure(["a"], requires, provides)
        kinds = sorted(p[0] for p in problems)
        self.assertEqual(
            kinds, ["choice", "rich", "unresolved", "unresolved", "unresolved"]
        )

    def test_conditional_fires_when_its_condition_is_in_the_closure(self):
        # The shape that matters: cmake pulls in its rpm macros because
        # rpm-build is in the buildroot.
        requires = {
            "cmake": ["(cmake-rpm-macros = 3.31 if rpm-build)"],
            "cmake-rpm-macros": [],
            "rpm-build": [],
        }
        provides = {n: [n] for n in requires}
        closure, problems = runtime_closure(
            ["cmake", "rpm-build"], requires, provides
        )
        self.assertIn("cmake-rpm-macros", closure)
        self.assertEqual(problems, [])

    def test_conditional_is_skipped_when_its_condition_is_absent(self):
        # (gpgverify if gnupg2) without gnupg2 is a dependency that
        # correctly does not apply -- not an unsolved one.
        requires = {"a": ["(gpgverify if gnupg2)"], "gpgverify": []}
        provides = {"a": ["a"], "gpgverify": ["gpgverify"], "gnupg2": ["gnupg2"]}
        closure, problems = runtime_closure(["a"], requires, provides)
        self.assertEqual(closure, {"a"})
        self.assertEqual(problems, [])

    def test_conditional_fires_on_a_condition_pulled_in_later(self):
        # "a" is read first and its condition is not yet satisfied; "b"
        # then drags in the conditioning package.  Evaluating once would
        # miss this, so the closure iterates to a fixed point.
        requires = {
            "a": ["(macros if tool)"],
            "b": ["tool"],
            "tool": [],
            "macros": [],
        }
        provides = {n: [n] for n in ("a", "b", "tool", "macros")}
        closure, problems = runtime_closure(["a", "b"], requires, provides)
        self.assertIn("macros", closure)
        self.assertEqual(problems, [])

    def test_version_range_dep_is_followed(self):
        # Unconditional, unlike "(A if B)": nothing to wait for, so it
        # pulls its provider in on the spot.
        requires = {"gitpython": ["(dist(gitdb) < 5 with dist(gitdb) >= 4)"]}
        provides = {"gitpython": ["gitpython"], "dist(gitdb)": ["gitdb"]}
        closure, problems = runtime_closure(["gitpython"], requires, provides)
        self.assertIn("gitdb", closure)
        self.assertEqual(problems, [])

    def test_unresolvable_condition_is_not_reported_repeatedly(self):
        # Re-asked every round, so a condition that never resolves must
        # not accumulate one problem entry per iteration.
        requires = {"a": ["(x if nothing-provides-this)"], "b": ["a"]}
        provides = {"a": ["a"], "b": ["b"], "x": ["x"]}
        _, problems = runtime_closure(["b"], requires, provides)
        self.assertEqual(problems, [])


class TestSourceGraphProjection(unittest.TestCase):
    def test_seed_deps_are_not_edges(self):
        # curl build-depends on zlib-devel; zlib is NOT in the build set,
        # so it comes from the seed and must not create an edge.
        build_deps = {"curl": {"zlib-devel", "glibc-devel"}}
        source_of = {"zlib-devel": "zlib", "glibc-devel": "glibc"}
        graph = project_to_source_graph(build_deps, source_of, {"curl"})
        self.assertEqual(graph, {"curl": set()})

    def test_built_deps_become_edges(self):
        build_deps = {"curl": {"zlib-devel"}, "zlib": set()}
        source_of = {"zlib-devel": "zlib"}
        graph = project_to_source_graph(build_deps, source_of, {"curl", "zlib"})
        self.assertEqual(graph, {"curl": {"zlib"}, "zlib": set()})

    def test_self_dependency_is_not_an_edge(self):
        # glibc build-requires glibc-devel, which glibc itself produces.
        build_deps = {"glibc": {"glibc-devel"}}
        source_of = {"glibc-devel": "glibc"}
        graph = project_to_source_graph(build_deps, source_of, {"glibc"})
        self.assertEqual(graph, {"glibc": set()})


class TestSCC(unittest.TestCase):
    def test_acyclic_graph_has_no_cycles(self):
        graph = {"a": {"b"}, "b": {"c"}, "c": set()}
        self.assertEqual(find_cycles(graph), [])

    def test_reverse_topological_order(self):
        # c must come before b before a: dependencies first.
        graph = {"a": {"b"}, "b": {"c"}, "c": set()}
        self.assertEqual(topological_order(graph), ["c", "b", "a"])

    def test_detects_two_node_cycle(self):
        graph = {"gcc": {"glibc"}, "glibc": {"gcc"}}
        cycles = find_cycles(graph)
        self.assertEqual(cycles, [["gcc", "glibc"]])

    def test_detects_larger_cycle(self):
        graph = {"a": {"b"}, "b": {"c"}, "c": {"a"}, "d": set()}
        cycles = find_cycles(graph)
        self.assertEqual(cycles, [["a", "b", "c"]])

    def test_deep_chain_does_not_overflow_stack(self):
        # Tarjan must be iterative: Fedora's graph is deep and Python's
        # recursion limit is 1000 by default.
        n = 5000
        graph = {str(i): {str(i + 1)} for i in range(n)}
        graph[str(n)] = set()
        comps = strongly_connected_components(graph)
        self.assertEqual(len(comps), n + 1)

    def test_deterministic_across_runs(self):
        graph = {"a": {"b", "c"}, "b": {"d"}, "c": {"d"}, "d": set()}
        first = strongly_connected_components(graph)
        for _ in range(20):
            self.assertEqual(strongly_connected_components(graph), first)


class TestStaging(unittest.TestCase):
    def test_stages_a_two_node_cycle(self):
        graph = {"gcc": {"glibc"}, "glibc": {"gcc"}}
        plan = stage_cycle(["gcc", "glibc"], graph, stages=3)
        self.assertEqual(len(plan), 6)  # 2 packages x 3 stages

        stage1 = [p for p in plan if p["stage"] == 1]
        self.assertTrue(all(p["cycle_deps_from"] == "seed" for p in stage1))

        stage2 = [p for p in plan if p["stage"] == 2]
        self.assertTrue(all(p["cycle_deps_from"] == "stage1" for p in stage2))

        # Exactly one stage per package ships.
        shipping = [p for p in plan if p["ships"]]
        self.assertEqual(sorted(p["source"] for p in shipping), ["gcc", "glibc"])
        self.assertTrue(all(p["stage"] == 3 for p in shipping))

    def test_stage_targets_are_distinct(self):
        graph = {"gcc": {"glibc"}, "glibc": {"gcc"}}
        plan = stage_cycle(["gcc", "glibc"], graph, stages=3)
        targets = [p["target"] for p in plan]
        self.assertEqual(len(targets), len(set(targets)))
        self.assertIn("gcc-stage1", targets)
        self.assertIn("glibc-stage3", targets)

    def test_rejects_zero_stages(self):
        with self.assertRaises(ValueError):
            stage_cycle(["a"], {"a": {"a"}}, stages=0)


class TestPlanBuildOrder(unittest.TestCase):
    def test_acyclic_plan_needs_no_staging(self):
        build_deps = {"curl": {"zlib-devel"}, "zlib": set()}
        source_of = {"zlib-devel": "zlib"}
        plan = plan_build_order(build_deps, source_of, {"curl", "zlib"})
        self.assertTrue(plan["acyclic"])
        self.assertEqual(plan["staged"], [])
        self.assertEqual(plan["order"], ["zlib", "curl"])

    def test_cyclic_plan_emits_stages_and_excludes_from_order(self):
        build_deps = {"gcc": {"glibc-devel"}, "glibc": {"gcc"}}
        source_of = {"glibc-devel": "glibc", "gcc": "gcc"}
        plan = plan_build_order(build_deps, source_of, {"gcc", "glibc"})
        self.assertFalse(plan["acyclic"])
        self.assertEqual(plan["cycles"], [["gcc", "glibc"]])
        # Cyclic packages are built via their stage targets, so they must
        # not also appear in the plain order.
        self.assertEqual(plan["order"], [])
        self.assertTrue(plan["staged"])


class TestBootstrapDepth(unittest.TestCase):
    def test_reports_built_vs_seeded_split(self):
        build_deps = {"curl": {"zlib-devel", "openssl-devel", "glibc-devel"}}
        source_of = {
            "zlib-devel": "zlib",
            "openssl-devel": "openssl",
            "glibc-devel": "glibc",
        }
        depth = bootstrap_depth(build_deps, source_of, {"curl", "zlib"})
        self.assertEqual(depth["built_from_source"], 1)   # zlib-devel
        self.assertEqual(depth["from_seed"], 2)           # openssl, glibc
        self.assertEqual(depth["fraction_built"], 0.3333)

    def test_empty_is_not_a_division_error(self):
        depth = bootstrap_depth({}, {}, set())
        self.assertEqual(depth["fraction_built"], 0.0)


class TestValidateOverrides(unittest.TestCase):
    """An override is an assertion about the repodata, so check it.

    Both bugs this catches were real.  `fedora-release-variant` was
    pointed at `fedora-release-common` for a long time and never failed,
    because `fedora-release` -- the actual provider -- was in the tree
    anyway; the override simply did nothing.  And
    `/usr/bin/systemd-sysusers=systemd` is plausible and wrong: systemd
    *requires* that path rather than shipping it, which is the whole
    reason the split package exists.  That one produced a clean solve and
    failed much later inside rpm, with a message about a file rather than
    about a flag.
    """

    PROVIDES = {
        "fedora-release-variant": {"fedora-release"},
        "/usr/bin/systemd-sysusers": {
            "systemd-standalone-sysusers",
            "systemd-sysusers",
        },
    }

    def test_correct_override_is_silent(self):
        self.assertEqual(
            validate_overrides(
                {"/usr/bin/systemd-sysusers": "systemd-sysusers"},
                self.PROVIDES,
            ),
            [],
        )

    def test_non_provider_is_reported_with_the_real_providers(self):
        problems = validate_overrides(
            {"/usr/bin/systemd-sysusers": "systemd"}, self.PROVIDES
        )
        self.assertEqual(len(problems), 1)
        kind, message, _scope = problems[0]
        self.assertEqual(kind, "bad-override")
        # The useful part of the message is the list of things that would
        # have worked, so a wrong flag tells you what to write instead.
        self.assertIn("systemd-standalone-sysusers", message)
        self.assertIn("systemd-sysusers", message)

    def test_unprovided_capability_is_reported(self):
        problems = validate_overrides(
            {"nothing-provides-this": "some-package"}, self.PROVIDES
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("nothing provides", problems[0][1])

    def test_the_historical_mistake(self):
        """fedora-release-common does not provide fedora-release-variant."""
        problems = validate_overrides(
            {"fedora-release-variant": "fedora-release-common"}, self.PROVIDES
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("fedora-release", problems[0][1])

    def test_scope_is_carried_through(self):
        """So a per-image override is not blamed on the global one."""
        problems = validate_overrides(
            {"/usr/bin/systemd-sysusers": "systemd"},
            self.PROVIDES,
            scope="image:live",
        )
        self.assertEqual(problems[0][2], "image:live")

    def test_rich_expressions_are_skipped(self):
        """Their value settles an (A or B); it is not a provider of the text.

        Checking them against `provides` would reject every correct one,
        since no package provides the literal string "(a or b)".
        """
        self.assertEqual(
            validate_overrides({"(a or b)": "a"}, self.PROVIDES), []
        )

    def test_no_overrides_is_not_a_problem(self):
        self.assertEqual(validate_overrides({}, self.PROVIDES), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestBooleanGrammar(unittest.TestCase):
    """The parser, over the shapes real Fedora repodata actually contains.

    Every expression here was taken from an F43 solve of the live image's
    126 source packages, which is what exposed that three hand-written
    shape matchers were not a grammar.
    """

    def test_capability_names_containing_parentheses(self):
        # The reason splitting has to be depth-aware: the *capability* has
        # parens of its own, so a naive scan mis-locates the group.
        node = parse_boolean(
            "(crate(anyhow/default) >= 1.0.0 with crate(anyhow/default) < 2.0.0~)"
        )
        self.assertEqual(node[0], "with")
        self.assertEqual(
            parse_range_dep(
                "(crate(anyhow/default) >= 1.0.0 with "
                "crate(anyhow/default) < 2.0.0~)"
            ),
            "crate(anyhow/default)",
        )

    def test_nested_with_inside_a_conditional(self):
        # redhat-rpm-config's, and so attached to a large fraction of
        # Fedora: 27 of the 44 expressions a full solve could not read.
        node = parse_boolean(
            "((rpm-build >= 4.14.90 with (rpm-build < 4.19.90 or "
            "rpm-build >= 4.19.91-8)) if rpm-build)"
        )
        self.assertEqual(node[0], "if")
        self.assertEqual(node[2], ("cap", "rpm-build"))
        self.assertEqual(node[1][0], "with")

    def test_a_condition_that_is_itself_a_choice(self):
        node = parse_boolean(
            "(appstream-data if (PackageKit or libdnf5-plugin-appstream))"
        )
        self.assertEqual(node[1], ("cap", "appstream-data"))
        self.assertEqual(node[2][0], "or")

    def test_chained_with_is_still_one_version_range(self):
        # python3-ldap carves three bad versions out of one range with four
        # `with` clauses. Reading only the first two is a different dep.
        expression = (
            "(python3.14dist(ldap3) < 2.5 with python3.14dist(ldap3) > 2.4 "
            "with python3.14dist(ldap3) >= 2.5)"
        )
        self.assertEqual(parse_range_dep(expression), "python3.14dist(ldap3)")

    def test_else_and_the_negative_operators_parse(self):
        self.assertEqual(
            parse_boolean("(a if b else c)"),
            ("if", ("cap", "a"), ("cap", "b"), ("cap", "c")),
        )
        self.assertEqual(
            parse_boolean("(a unless b)"),
            ("unless", ("cap", "a"), ("cap", "b"), None),
        )
        self.assertEqual(
            parse_boolean("(a without b)"),
            ("without", ("cap", "a"), ("cap", "b")),
        )

    def test_mixing_operators_without_parentheses_is_refused(self):
        # rpm's grammar forbids it, so there is no precedence to apply and
        # guessing one would silently mean a different dependency.
        self.assertIsNone(parse_boolean("(p or q and r)"))

    def test_unparse_round_trips(self):
        for expression in (
            "(a or b)",
            "(a if b else c)",
            "(a without b)",
            "((a with b) if c)",
        ):
            self.assertEqual(
                unparse_boolean(parse_boolean(expression)), expression
            )


class TestBooleanEvaluation(unittest.TestCase):
    def test_nested_conditional_fires_through_a_with(self):
        # The whole expression collapses to rpm-build: every term names
        # it, so the `with` is a version range and the `if` is satisfied
        # by the very thing it would pull in.
        requires = {
            "redhat-rpm-config": [
                "((rpm-build >= 4.14.90 with (rpm-build < 4.19.90 or "
                "rpm-build >= 4.19.91-8)) if rpm-build)"
            ],
            "rpm-build": [],
        }
        provides = {
            "redhat-rpm-config": ["redhat-rpm-config"],
            "rpm-build": ["rpm-build"],
        }
        closure, problems = runtime_closure(
            ["redhat-rpm-config", "rpm-build"], requires, provides
        )
        self.assertEqual(problems, [])
        self.assertIn("rpm-build", closure)

    def test_condition_that_is_a_choice_is_evaluated(self):
        requires = {
            "gnome-software": ["(appstream-data if (PackageKit or libdnf5))"],
            "libdnf5": [],
            "appstream-data": [],
        }
        provides = {
            "gnome-software": ["gnome-software"],
            "libdnf5": ["libdnf5"],
            "appstream-data": ["appstream-data"],
        }
        closure, problems = runtime_closure(
            ["gnome-software", "libdnf5"], requires, provides
        )
        self.assertEqual(problems, [])
        self.assertIn("appstream-data", closure)

    def test_condition_absent_means_not_required(self):
        requires = {"gnome-software": ["(appstream-data if (PackageKit or libdnf5))"]}
        provides = {
            "gnome-software": ["gnome-software"],
            "appstream-data": ["appstream-data"],
        }
        closure, problems = runtime_closure(["gnome-software"], requires, provides)
        self.assertEqual(problems, [])
        self.assertNotIn("appstream-data", closure)

    def test_else_branch_is_taken_when_the_condition_is_false(self):
        requires = {"a": ["(x if cond else y)"], "y": []}
        provides = {"a": ["a"], "x": ["x"], "y": ["y"]}
        closure, problems = runtime_closure(["a"], requires, provides)
        self.assertEqual(problems, [])
        self.assertIn("y", closure)
        self.assertNotIn("x", closure)

    def test_unless_is_settled_only_at_the_fixed_point(self):
        # "required unless B appears" cannot be answered while B might
        # still appear, so it fires only once nothing else can grow.
        requires = {"a": ["(fallback unless preferred)"], "fallback": []}
        provides = {"a": ["a"], "fallback": ["fallback"], "preferred": ["preferred"]}
        closure, problems = runtime_closure(["a"], requires, provides)
        self.assertEqual(problems, [])
        self.assertIn("fallback", closure)

        # With the escape present from the start, it never fires.
        closure, problems = runtime_closure(
            ["a", "preferred"], requires, provides
        )
        self.assertEqual(problems, [])
        self.assertNotIn("fallback", closure)

    def test_with_over_different_capabilities_intersects_providers(self):
        # `with` means one package providing both, so the answer is the
        # intersection -- not one package per term, which is `and`.
        requires = {"a": ["(featureA with featureB)"], "both": []}
        provides = {
            "a": ["a"],
            "featureA": ["both", "onlyA"],
            "featureB": ["both", "onlyB"],
        }
        closure, problems = runtime_closure(["a"], requires, provides)
        self.assertEqual(problems, [])
        self.assertIn("both", closure)
        self.assertNotIn("onlyA", closure)


if __name__ == "__main__":
    unittest.main()
