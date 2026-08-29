"""Tests for the image-set closure.

depgraph_test covers what a `Requires` means.  This covers the part that
sits above it: which closure a given override applies to.  That distinction
has no signature in the output -- a lockfile built with the wrong override
is still a well-formed lockfile, listing packages that all exist, and the
mistake only surfaces much later as rpm refusing a transaction.  So it gets
tested here rather than trusted to a re-solve.
"""

import argparse
import json
import os
import tempfile
import unittest
from unittest import mock

from solve import (
    BUILDSYS_BUILD,
    CENTOS_BUILDSYS_BUILD,
    IMPLICIT_GROUPS,
    PROBE_SCHEMA,
    build_universe,
    check_public_base,
    collect_repos,
    derive_repo_name,
    load_probe,
    merge_fallback_packages,
    merge_packages,
    parse_override,
    parse_source_exception,
    probe_identity_errors,
    probed_buildrequires,
    rpm_source_policy_inputs,
    solve,
    solve_image_sets,
    solve_package_set,
    source_build_set,
)
from depgraph import runtime_closure


class TestOverrideParsing(unittest.TestCase):
    def test_rich_dependency_may_contain_an_equals_operator(self):
        expression = "(redhat-release with system-release(releasever) = 10)"
        self.assertEqual(
            parse_override(expression + "=centos-stream-release", "--override"),
            (expression, "centos-stream-release"),
        )

    def test_source_exception_is_one_json_object(self):
        self.assertEqual(
            parse_source_exception(
                '{"package":"kernel-core","kind":"host-kernel-capability",'
                '"reason":"Host capability is absent."}'
            )["package"],
            "kernel-core",
        )
        with self.assertRaisesRegex(ValueError, "must be a JSON object"):
            parse_source_exception('["kernel-core"]')


def binary(name, requires=(), provides=(), source="src-1.fc43.src.rpm",
           version="1", release="1.fc43", arch="x86_64", repo=None):
    return {
        "name": name,
        "requires": list(requires),
        "provides": list(provides),
        "sourcerpm": source,
        "version": version,
        "release": release,
        "epoch": "0",
        "arch": arch,
        "location": "{}/{}-{}.{}.rpm".format(name[0], name, release, arch),
        "checksum": "0" * 64,
        "checksum_type": "sha256",
        "repo": repo,
    }


def source(name, requires=(), version="1", release="1.fc43"):
    return {
        "name": name,
        "requires": list(requires),
        "provides": [],
        "sourcerpm": None,
        "version": version,
        "release": release,
        "epoch": "0",
        "arch": "src",
        "location": "{}/{}-{}-{}.src.rpm".format(name[0], name, version, release),
        "checksum": "0" * 64,
        "checksum_type": "sha256",
        "repo": "source-releases",
    }


def universe_of(*pkgs):
    return build_universe(list(pkgs), [])


class TestImageSets(unittest.TestCase):
    def test_closes_over_runtime_requires(self):
        universe = universe_of(
            binary("shell", requires=["libc"]),
            binary("glibc", provides=["libc"]),
        )
        sets, problems = solve_image_sets(universe, {"live": ["shell"]})
        self.assertEqual(problems, [])
        self.assertEqual(sets["live"], ["glibc", "shell"])

    def test_reports_a_root_that_does_not_exist(self):
        universe = universe_of(binary("shell"))
        sets, problems = solve_image_sets(universe, {"live": ["shell", "nope"]})
        kinds = [kind for kind, _, _ in problems]
        self.assertEqual(kinds, ["missing-binary"])
        # The rest of the set still solves: one retired package name should
        # not cost the report on the other thirty.
        self.assertEqual(sets["live"], ["shell"])

    def test_attributes_problems_to_the_image_not_the_package(self):
        universe = universe_of(binary("shell", requires=["missing-cap"]))
        _, problems = solve_image_sets(universe, {"live": ["shell"]})
        self.assertEqual(len(problems), 1)
        self.assertIn("image:live", problems[0][2])

    def test_each_set_is_closed_independently(self):
        universe = universe_of(
            binary("shell"),
            binary("kernel"),
        )
        sets, problems = solve_image_sets(
            universe, {"live": ["shell"], "netinst": ["kernel"]}
        )
        self.assertEqual(problems, [])
        self.assertEqual(sets["live"], ["shell"])
        self.assertEqual(sets["netinst"], ["kernel"])

    def test_generic_package_set_uses_the_requested_scope(self):
        universe = universe_of(binary("shell", requires=["missing-cap"]))
        closure, problems = solve_package_set(
            universe, ["shell"], scope="buildroot",
        )
        self.assertEqual(closure, ["shell"])
        self.assertEqual(problems[0][2], "buildroot (shell)")


class TestSourceBuildSet(unittest.TestCase):
    def test_derives_source_names_from_the_selected_image_closure(self):
        self.assertEqual(
            source_build_set(
                {
                    "image-tools": ["xorriso"],
                    "live": ["bash", "kernel-core", "kernel-modules"],
                },
                {
                    "bash": "bash",
                    "kernel-core": "kernel",
                    "kernel-modules": "kernel",
                    "xorriso": "libisoburn",
                },
                ["live"],
            ),
            {"bash", "kernel"},
        )

    def test_explicit_prebuilt_sources_are_removed(self):
        self.assertEqual(
            source_build_set(
                {"live": ["bash", "kernel-core"]},
                {"bash": "bash", "kernel-core": "kernel"},
                ["live"],
                ["kernel"],
            ),
            {"bash"},
        )

    def test_rejects_a_stale_prebuilt_source(self):
        with self.assertRaisesRegex(
            ValueError, "do not produce a selected image payload: kernel"
        ):
            source_build_set(
                {"live": ["bash"]},
                {"bash": "bash"},
                ["live"],
                ["kernel"],
            )

    def test_prebuilt_sources_require_an_image_derived_policy(self):
        with self.assertRaisesRegex(ValueError, "require --source-image"):
            source_build_set({}, {}, [], ["kernel"])

    def test_normalizes_policy_inputs_and_effective_producers(self):
        images, producers = rpm_source_policy_inputs({
            "solve": {
                "source_image_sets": ["live"],
                "prebuilt_sources": ["kernel"],
            },
            "image_sets": {
                "live": [
                    {"name": "kernel-core", "source": "kernel"},
                    {"name": "bash", "source": "bash"},
                ],
            },
            "packages": {
                "bash": {"source": {"name": "bash"}},
                "kernel": {"source": {"name": "kernel"}},
            },
        })
        self.assertEqual(images, {"live": [
            {"package": "bash", "source": "bash"},
            {"package": "kernel-core", "source": "kernel"},
        ]})
        self.assertEqual(producers, {"bash"})


class TestScopedOverrides(unittest.TestCase):
    """The systemd-sysusers case, reduced.

    Two packages provide one capability.  The right answer differs between
    a buildroot (the standalone binary, which is smaller) and an image (the
    full package, which is installed anyway and owns the same file), so the
    global override has to be overridable per set.
    """

    def universe(self):
        return universe_of(
            binary("systemd", provides=["/usr/bin/systemd-sysusers"]),
            binary("systemd-standalone-sysusers",
                   provides=["/usr/bin/systemd-sysusers"]),
            binary("setup", requires=["/usr/bin/systemd-sysusers"]),
        )

    def test_global_override_applies_by_default(self):
        sets, problems = solve_image_sets(
            self.universe(), {"live": ["setup"]},
            overrides={"/usr/bin/systemd-sysusers":
                       "systemd-standalone-sysusers"},
        )
        self.assertEqual(problems, [])
        self.assertIn("systemd-standalone-sysusers", sets["live"])

    def test_image_override_wins_over_the_global_one(self):
        sets, problems = solve_image_sets(
            self.universe(), {"live": ["setup"]},
            overrides={"/usr/bin/systemd-sysusers":
                       "systemd-standalone-sysusers"},
            image_overrides={"live": {"/usr/bin/systemd-sysusers": "systemd"}},
        )
        self.assertEqual(problems, [])
        self.assertIn("systemd", sets["live"])
        self.assertNotIn("systemd-standalone-sysusers", sets["live"])

    def test_an_override_scoped_to_one_set_leaves_the_others_alone(self):
        sets, _ = solve_image_sets(
            self.universe(),
            {"live": ["setup"], "minimal": ["setup"]},
            overrides={"/usr/bin/systemd-sysusers":
                       "systemd-standalone-sysusers"},
            image_overrides={"live": {"/usr/bin/systemd-sysusers": "systemd"}},
        )
        self.assertIn("systemd", sets["live"])
        self.assertIn("systemd-standalone-sysusers", sets["minimal"])

    def test_the_global_overrides_are_not_mutated(self):
        # Layering by dict(a, **b) rather than a.update(b): the caller's
        # dict is also the build closure's, and one leaked key there would
        # silently rebuild every buildroot.
        overrides = {"/usr/bin/systemd-sysusers":
                     "systemd-standalone-sysusers"}
        solve_image_sets(
            self.universe(), {"live": ["setup"]}, overrides,
            image_overrides={"live": {"other-cap": "whatever"}},
        )
        self.assertEqual(
            overrides,
            {"/usr/bin/systemd-sysusers": "systemd-standalone-sysusers"},
        )


class TestDynamicBuildRequires(unittest.TestCase):
    """Where a package's BuildRequires came from.

    The failure mode is silence.  A spec that computes its BuildRequires in
    %generate_buildrequires declares only the handful needed to run the
    computation, so a solve from repodata alone produces a lockfile that is
    complete-looking, well-formed, and missing most of the buildroot -- and
    nothing goes wrong until %build fails somewhere unrelated.  The probe
    results are what close that gap, so what is tested here is that they
    reach the solve, and that a lockfile can say which of the two answers
    it got.
    """

    def universe(self, *extra_requires):
        # The implicit group has to resolve or every capability in the
        # package under test is drowned in unrelated failures.
        seed = [binary(name) for name in BUILDSYS_BUILD]
        return build_universe(
            seed + [binary(name) for name in extra_requires],
            [source("widget", requires=["cargo", "rust-packaging"])],
        )

    def test_repodata_alone_records_the_heuristic_and_no_capabilities(self):
        _, _, _, dynamic, _base = solve(self.universe("cargo", "rust-packaging"),
                                 {"widget"})
        self.assertEqual(dynamic["widget"], {
            "source": "repodata",
            "capabilities": [],
            "suspected": ["rust-packaging"],
            "unmet": False,
        })

    def test_a_probe_replaces_repodatas_answer(self):
        universe = self.universe("cargo", "rust-packaging", "openssl-devel")
        report = {
            "buildrequires": ["cargo", "rust-packaging", "openssl-devel"],
            "static": ["cargo", "rust-packaging"],
            "dynamic": ["openssl-devel"],
            "generated": True,
            "unmet": True,
        }
        deps, _, problems, dynamic, _base = solve(
            universe, {"widget"}, probe={"widget": report})

        self.assertIn("openssl-devel", deps["widget"])
        self.assertEqual(dynamic["widget"]["source"], "probe")
        self.assertEqual(dynamic["widget"]["capabilities"], ["openssl-devel"])
        # No longer a guess, so the guess is not recorded alongside it.
        self.assertEqual(dynamic["widget"]["suspected"], [])
        self.assertTrue(dynamic["widget"]["unmet"])
        # An unmet probe is the expected result of a *first* probe -- the
        # generator asked for what this solve is about to add -- so it must
        # not be a problem, or --strict fails the run that fixes it.
        self.assertEqual([kind for kind, _, _ in problems], [])

    def test_the_implicit_group_survives_a_probe(self):
        # A generator never mentions @buildsys-build: it runs inside it.
        # Taking the probe's word literally would drop gcc and make from
        # every probed package's buildroot.
        got = probed_buildrequires({"buildrequires": ["openssl-devel"]})
        self.assertIn("gcc", got)
        self.assertIn("rpm-build", got)
        self.assertEqual(got[-1], "openssl-devel")

    def test_centos_can_supply_its_own_implicit_build_group(self):
        binaries = [binary(name) for name in CENTOS_BUILDSYS_BUILD]
        universe = build_universe(binaries, [source("widget")])
        deps, _, problems, _, base = solve(
            universe,
            {"widget"},
            implicit=CENTOS_BUILDSYS_BUILD,
        )
        self.assertEqual(problems, [])
        self.assertIn("centos-stream-release", deps["widget"])
        self.assertNotIn("fedora-release-common", deps["widget"])
        # And the shared base buildroot follows the flavor too.  It is
        # closed over the same implicit group, so hardcoding Fedora's would
        # build every CentOS package in a tree CentOS never promised -- and
        # would do it quietly, since the per-package overlay still supplies
        # what the spec asked for by name.
        self.assertIn("centos-stream-release", base)
        self.assertNotIn("fedora-release-common", base)

    def test_centos_hyperscale_inherits_the_centos_implicit_group(self):
        self.assertEqual(
            IMPLICIT_GROUPS["centos-hyperscale"],
            CENTOS_BUILDSYS_BUILD,
        )

    def test_rpmlib_entries_from_the_header_are_dropped(self):
        # `rpm -qp --requires` on a source header emits these unconditionally
        # and nothing provides them, so a solve handed one reports an
        # unresolvable dependency on a thing that does not exist.
        got = probed_buildrequires(
            {"buildrequires": ["rpmlib(FileDigests) <= 4.6.0-1", "cargo"]},
            implicit=(),
        )
        self.assertEqual(got, ["cargo"])


class TestProbeFile(unittest.TestCase):
    def write(self, payload):
        handle, path = tempfile.mkstemp(suffix=".probe.json")
        with os.fdopen(handle, "w") as fh:
            json.dump(payload, fh)
        self.addCleanup(os.unlink, path)
        return path

    def test_a_report_for_a_package_no_longer_built_is_ignored(self):
        # A probe file outlives the build list that produced it. Left in,
        # a stale report goes on contributing BuildRequires for something
        # that is not built any more.
        path = self.write({
            "schema": PROBE_SCHEMA,
            "packages": {"widget": {"dynamic": []}, "dropped": {"dynamic": []}},
        })
        self.assertEqual(sorted(load_probe(path, {"widget"})), ["widget"])

    def test_no_probe_file_is_not_an_error(self):
        # The first solve of a release cannot have one: the probe needs a
        # buildroot, and the buildroot comes from a solve.
        self.assertEqual(load_probe(None, {"widget"}), {})

    def test_an_unknown_schema_is_refused(self):
        path = self.write({"schema": PROBE_SCHEMA + 1, "packages": {}})
        with self.assertRaises(SystemExit):
            load_probe(path, {"widget"})

    def test_a_probe_for_another_release_is_refused(self):
        path = self.write({
            "schema": PROBE_SCHEMA,
            "flavor": "fedora",
            "release": "45",
            "target_cpu": "x86_64",
            "packages": {"widget": {"dynamic": []}},
        })
        with self.assertRaisesRegex(SystemExit, "release='45', expected '44'"):
            load_probe(
                path,
                {"widget"},
                flavor="fedora",
                release="44",
                target_cpu="x86_64",
            )

    def test_legacy_probe_without_target_cpu_keeps_working(self):
        self.assertEqual(
            [],
            probe_identity_errors(
                {"flavor": "fedora", "release": "44"},
                flavor="fedora",
                release="44",
                target_cpu="x86_64",
            ),
        )


class TestMergePackages(unittest.TestCase):
    """Layering updates/ over releases/.

    The failure this guards against is not a crash: picking the wrong build
    produces a perfectly well-formed lockfile that pins the unfixed rpm, and
    the only symptom is that a security update everyone believes is applied
    is not.
    """

    def merge(self, *groups):
        packages, replacements, _superseded = merge_packages(list(groups))
        return packages, replacements

    def test_a_newer_build_in_a_later_repo_wins(self):
        packages, replaced = self.merge(
            ("releases", [binary("glibc", release="1.fc43", repo="releases")]),
            ("updates", [binary("glibc", release="5.fc43", repo="updates")]),
        )
        self.assertEqual([p["release"] for p in packages], ["5.fc43"])
        self.assertEqual(packages[0]["repo"], "updates")
        self.assertEqual(
            replaced,
            [{"name": "glibc", "arch": "x86_64",
              "from": "1-1.fc43", "from_repo": "releases",
              "to": "1-5.fc43", "to_repo": "updates"}],
        )

    def test_an_epel_next_rebuild_supersedes_epel(self):
        packages, replaced = self.merge(
            ("epel", [binary("widget", release="1.el9", repo="epel")]),
            ("epel-next", [
                binary("widget", release="1.el9.next", repo="epel-next"),
            ]),
        )
        self.assertEqual(packages[0]["release"], "1.el9.next")
        self.assertEqual(packages[0]["repo"], "epel-next")
        self.assertEqual(replaced[0]["to_repo"], "epel-next")

    def test_repo_order_cannot_downgrade(self):
        """Version decides; order only settles exact ties.

        Passing the repos the wrong way round is a plausible mistake and
        would otherwise be an invisible one.
        """
        packages, replaced = self.merge(
            ("updates", [binary("glibc", release="5.fc43", repo="updates")]),
            ("releases", [binary("glibc", release="1.fc43", repo="releases")]),
        )
        self.assertEqual([p["release"] for p in packages], ["5.fc43"])
        self.assertEqual(replaced, [])

    def test_the_same_build_in_both_repos_is_not_a_replacement(self):
        packages, replaced = self.merge(
            ("releases", [binary("bash", repo="releases")]),
            ("updates", [binary("bash", repo="updates")]),
        )
        self.assertEqual(len(packages), 1)
        self.assertEqual(packages[0]["repo"], "releases")
        self.assertEqual(replaced, [])

    def test_versions_compare_by_rpm_rules_not_as_strings(self):
        # Lexicographically "1.9" > "1.10", which is the whole reason
        # rpmvercmp exists.
        packages, _ = self.merge(
            ("releases", [binary("tzdata", version="1.9", repo="releases")]),
            ("updates", [binary("tzdata", version="1.10", repo="updates")]),
        )
        self.assertEqual(packages[0]["version"], "1.10")

    def test_arches_are_separate_slots(self):
        """i686 and x86_64 builds coexist and do not replace each other.

        Fedora's x86_64 repo carries thousands of i686 multilib rpms under
        identical package names; keying the merge on name alone would make
        the winner a matter of document order.
        """
        packages, replaced = self.merge(
            ("releases", [
                binary("glibc", arch="x86_64", repo="releases"),
                binary("glibc", arch="i686", release="9.fc43",
                       repo="releases"),
            ]),
        )
        self.assertEqual(
            sorted((p["arch"], p["release"]) for p in packages),
            [("i686", "9.fc43"), ("x86_64", "1.fc43")],
        )
        self.assertEqual(replaced, [])

    def test_output_is_sorted_not_merely_deterministic(self):
        """A dict preserves insertion order, which is not the same thing.

        Insertion order here is the order packages appear in primary.xml,
        which is Fedora's to change whenever it regenerates repodata.  The
        result is committed, so it has to be sorted rather than reproducible
        only against one particular copy of the input.
        """
        packages, _ = self.merge(
            ("releases", [binary(n) for n in ("zlib", "attr", "make")]),
        )
        self.assertEqual([p["name"] for p in packages],
                         ["attr", "make", "zlib"])

    def test_the_replacement_count_does_not_depend_on_document_order(self):
        """Three builds of one package in one repo is one answer, not two.

        Folding each straight into the global index would count a
        replacement per step, so 1.0, 1.1, 1.2 would report two and the same
        three in the order 1.2, 1.0, 1.1 would report none -- a lockfile
        whose summary changes when upstream reshuffles its xml.
        """
        builds = [binary("kernel", version=v, repo="releases")
                  for v in ("1.0", "1.1", "1.2")]
        for order in ([0, 1, 2], [2, 0, 1], [1, 2, 0]):
            with self.subTest(order=order):
                packages, replaced = self.merge(
                    ("releases", [builds[i] for i in order]),
                )
                self.assertEqual([p["version"] for p in packages], ["1.2"])
                self.assertEqual(replaced, [])

    def test_a_replacement_is_reported_against_the_base_not_the_last_seen(self):
        packages, replaced = self.merge(
            ("releases", [binary("curl", release="1.fc43", repo="releases")]),
            ("updates", [binary("curl", release="2.fc43", repo="updates")]),
            ("testing", [binary("curl", release="3.fc43", repo="testing")]),
        )
        self.assertEqual(packages[0]["release"], "3.fc43")
        self.assertEqual(
            (replaced[0]["from"], replaced[0]["from_repo"]),
            ("1-1.fc43", "releases"),
        )


class TestFallbackPackages(unittest.TestCase):
    def test_fills_a_missing_name_arch_slot(self):
        compose = [binary("bash", repo="compose")]
        combined, added, shadowed, _superseded = merge_fallback_packages(
            compose,
            [("koji", [binary("ducktype", repo="koji")])],
        )
        self.assertEqual([pkg["name"] for pkg in combined],
                         ["bash", "ducktype"])
        self.assertEqual([pkg["repo"] for pkg in added], ["koji"])
        self.assertEqual(shadowed, [])

    def test_cannot_replace_a_compose_package_with_a_newer_build(self):
        compose = [binary("openssl", release="1.el10", repo="compose")]
        combined, added, shadowed, _superseded = merge_fallback_packages(
            compose,
            [("koji", [
                binary("openssl", release="9.el10", repo="koji"),
            ])],
        )
        self.assertEqual(len(combined), 1)
        self.assertEqual(combined[0]["release"], "1.el10")
        self.assertEqual(combined[0]["repo"], "compose")
        self.assertEqual(added, [])
        self.assertEqual([pkg["repo"] for pkg in shadowed], ["koji"])

    def test_fallback_capabilities_do_not_expand_an_image_closure(self):
        compose = [binary("app", requires=["fallback-cap"], repo="compose")]
        combined, _added, _shadowed, _superseded = merge_fallback_packages(
            compose,
            [("koji", [
                binary("fallback-devel", provides=["fallback-cap"],
                       repo="koji"),
            ])],
        )
        compose_universe = build_universe(compose, [])
        buildroot_universe = build_universe(combined, [])

        image_sets, problems = solve_image_sets(
            compose_universe, {"live": ["app"]}
        )
        buildroot_closure, buildroot_problems = solve_package_set(
            buildroot_universe, ["app"], scope="buildroot"
        )

        self.assertEqual(image_sets["live"], ["app"])
        self.assertEqual(len(problems), 1)
        self.assertEqual(buildroot_problems, [])
        self.assertEqual(buildroot_closure, ["app", "fallback-devel"])

    def test_compose_keeps_the_plain_name_across_architectures(self):
        compose = [binary("tool", arch="noarch", repo="compose")]
        combined, _added, _shadowed, _superseded = merge_fallback_packages(
            compose,
            [("koji", [
                binary("tool", arch="x86_64", provides=["build-only-cap"],
                       repo="koji"),
            ])],
        )
        universe = build_universe(
            combined, [], fallback_repos={"koji"}
        )
        self.assertEqual(universe["binary_index"]["tool"]["repo"], "compose")
        self.assertEqual(universe["provides"]["build-only-cap"],
                         ["tool.x86_64"])

    def test_images_use_compose_while_buildrequires_use_fallback(self):
        compose = [
            binary("app", source="app-1-1.fc43.src.rpm", arch="noarch",
                   repo="compose"),
        ]
        buildroot = [
            binary("app", source="app-9-9.fc43.src.rpm",
                   release="9.fc43", repo="koji"),
            binary("fallback-devel", requires=["fallback-runtime"],
                   provides=["fallback-cap"], repo="koji"),
            binary("fallback-runtime", repo="koji"),
        ]
        sources = [source("app", requires=["fallback-cap"])]

        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, "lock.json")
            by_path = {
                "compose.xml": compose,
                "buildroot.xml": buildroot,
                "source.xml": sources,
            }

            def parse(path, repo=None):
                return [dict(pkg, repo=repo) for pkg in by_path[path]]

            with (
                mock.patch("solve.parse_primary", side_effect=parse),
                mock.patch.dict(IMPLICIT_GROUPS, {"fedora": ()}),
            ):
                from solve import main
                main([
                    "--binary-primary", "compose.xml",
                    "--binary-base",
                    "https://dl.fedoraproject.org/pub/fedora/linux",
                    "--binary-repo", "compose",
                    "--buildroot-primary", "buildroot.xml",
                    "--buildroot-base",
                    "https://kojihub.stream.centos.org/kojifiles/repos/c10s-build/1/x86_64",
                    "--buildroot-repo", "koji",
                    "--source-primary", "source.xml",
                    "--source-base",
                    "https://dl.fedoraproject.org/pub/fedora/linux/source",
                    "--source-repo", "source",
                    "--image", "live=app",
                    "--source-image", "live",
                    "--release", "43",
                    "--dist-tag", ".fc43",
                    "--out", output,
                    "--strict",
                ])

            with open(output, encoding="utf-8") as stream:
                lock = json.load(stream)

        self.assertEqual(
            [(repo["name"], repo["kind"]) for repo in lock["repos"]],
            [("compose", "binary"), ("koji", "buildroot"),
             ("source", "source")],
        )
        self.assertEqual(
            [(pkg["name"], pkg["evr"], pkg["arch"], pkg["repo"])
             for pkg in lock["image_sets"]["live"]],
            [("app", "1-1.fc43", "noarch", "compose")],
        )
        self.assertEqual(lock["source_policy"]["summary"]["live"], {
            "pinned": 0,
            "source": 1,
            "total": 1,
        })
        self.assertEqual(lock["packages"]["app"]["source"]["repo"],
                         "source")
        self.assertEqual(lock["packages"]["app"]["subpackages"], ["app"])
        self.assertEqual(
            [(pkg["name"], pkg["repo"])
             for pkg in lock["packages"]["app"]["deps_seed"]],
            [("fallback-devel", "koji"),
             ("fallback-runtime", "koji")],
        )


class TestArchSelection(unittest.TestCase):
    """Collapsing several arches of one name down to the one that is wanted."""

    def test_the_target_arch_wins_over_a_newer_foreign_build(self):
        for order in (False, True):
            pkgs = [
                binary("glibc", arch="x86_64", release="1.fc43",
                       provides=["libc.so.6()(64bit)"]),
                binary("glibc", arch="i686", release="9.fc43",
                       provides=["libc.so.6"]),
            ]
            with self.subTest(reversed=order):
                universe = build_universe(
                    list(reversed(pkgs)) if order else pkgs, [],
                    target_cpu="x86_64",
                )
                self.assertEqual(
                    universe["binary_index"]["glibc"]["arch"], "x86_64"
                )

    def test_a_losing_builds_capabilities_are_not_attributed_to_the_winner(self):
        """The reason winners are picked before any Provides are read.

        The 32-bit build provides `libc.so.6` with no (64bit) marker.  If it
        contributed to the capability map on its way to losing, that
        capability would resolve to the *name* of the 64-bit package, which
        does not provide it -- and the buildroot would be quietly missing a
        library that the solve says is present.

        The capability itself is legitimate and now resolvable, to the build
        that actually has it; see TestForeignArch.  What must never happen
        is the attribution, which is what this checks.
        """
        universe = build_universe([
            binary("glibc", arch="i686", release="9.fc43",
                   provides=["libc.so.6"]),
            binary("glibc", arch="x86_64", release="1.fc43",
                   provides=["libc.so.6()(64bit)"]),
        ], [], target_cpu="x86_64")
        self.assertEqual(universe["provides"]["libc.so.6"], ["glibc.i686"])
        self.assertEqual(universe["provides"]["libc.so.6()(64bit)"], ["glibc"])

    def test_a_package_that_exists_only_as_a_foreign_arch_is_kept(self):
        # Ranked last, not dropped: it is still the only answer to a
        # Requires on it, and dropping it turns a resolvable capability
        # into a solve failure.
        universe = build_universe(
            [binary("wine-core", arch="i686")], [], target_cpu="x86_64"
        )
        self.assertIn("wine-core", universe["binary_index"])

    def test_noarch_is_preferred_over_a_foreign_arch(self):
        universe = build_universe([
            binary("fonts", arch="i686", release="9.fc43"),
            binary("fonts", arch="noarch", release="1.fc43"),
        ], [], target_cpu="x86_64")
        self.assertEqual(universe["binary_index"]["fonts"]["arch"], "noarch")


class TestForeignArch(unittest.TestCase):
    """Reaching a build the collapse discarded, by naming its arch.

    gcc is the only caller today: its spec asks for
    `(glibc32 or glibc-devel(x86-32))` on every 64-bit arch, and in a
    collapsed x86_64 universe neither branch has a provider.
    """

    def multilib(self):
        """Shaped after what Fedora 43 actually publishes.

        The mix matters and an invented fixture gets it wrong: glibc-devel
        for i686 requires plain `glibc` and plain `kernel-headers`, which
        are arch-neutral, *and* unmarked sonames like `libm.so.6`, which
        are 32-bit by virtue of carrying no `()(64bit)` marker.  The
        sonames are what actually reach the 32-bit libc; the plain names
        are deliberately answerable by whatever is already installed.
        """
        return [
            binary("glibc", arch="x86_64",
                   provides=["libc.so.6()(64bit)", "libm.so.6()(64bit)"]),
            binary("glibc", arch="i686",
                   provides=["libc.so.6", "libm.so.6"],
                   requires=["glibc-common"]),
            binary("glibc-devel", arch="x86_64",
                   requires=["glibc", "kernel-headers", "libm.so.6()(64bit)"],
                   provides=["glibc-devel(x86-64)"]),
            binary("glibc-devel", arch="i686",
                   requires=["glibc", "kernel-headers", "libm.so.6"],
                   provides=["glibc-devel(x86-32)"]),
            # Arch-neutral in name but shipped per arch, and skewed in
            # version -- which is the case that made rpm refuse the
            # transaction outright.
            binary("kernel-headers", arch="x86_64", version="7.1.3"),
            binary("kernel-headers", arch="i686", version="6.17.0"),
            binary("glibc-common", arch="x86_64"),
            binary("gcc", arch="x86_64"),
        ]

    def test_a_discarded_build_is_addressable_by_arch(self):
        universe = build_universe(self.multilib(), [], target_cpu="x86_64")
        self.assertEqual(
            universe["foreign_index"]["glibc-devel.i686"]["arch"], "i686")
        # And the collapsed view is untouched.
        self.assertEqual(
            universe["binary_index"]["glibc-devel"]["arch"], "x86_64")

    def test_a_foreign_capability_resolves_to_the_build_that_has_it(self):
        """Registered, and pointed at the arch-qualified name.

        The old rule kept these out of the shared map entirely, which was
        protecting the right thing for the wrong reason: the hazard is a
        32-bit capability attributed to the *64-bit package*, not the
        capability existing.  Naming the i686 build says something true.
        """
        universe = build_universe(self.multilib(), [], target_cpu="x86_64")
        self.assertEqual(
            universe["provides"]["glibc-devel(x86-32)"], ["glibc-devel.i686"])
        self.assertEqual(universe["provides"]["libc.so.6"], ["glibc.i686"])
        # And the 64-bit side is untouched.
        self.assertEqual(
            universe["provides"]["libc.so.6()(64bit)"], ["glibc"])
        self.assertEqual(universe["provides"]["glibc-devel"], ["glibc-devel"])

    def test_a_contested_capability_keeps_the_collapsed_answer(self):
        """Only empty slots get filled, which is the safety argument.

        Both arches ship /bin/awk, unmarked.  A 64-bit package requiring it
        means the 64-bit gawk, and registering the i686 build alongside
        would turn a settled lookup into an ambiguity -- 21,556 of them
        across Fedora 43, measured.
        """
        universe = build_universe([
            binary("gawk", arch="x86_64", provides=["/bin/awk"]),
            binary("gawk", arch="i686", provides=["/bin/awk"]),
        ], [], target_cpu="x86_64")
        self.assertEqual(universe["provides"]["/bin/awk"], ["gawk"])

    def test_a_package_only_this_arch_ships_is_not_duplicated(self):
        """lrmi exists as i686 only, so it already won the collapse.

        A second, qualified entry would make `lrmi(x86-32)` ambiguous
        between `lrmi` and `lrmi.i686` -- the same rpm, twice.  Six
        packages in Fedora 43 are shaped like this.
        """
        universe = build_universe(
            [binary("lrmi", arch="i686", provides=["lrmi(x86-32)"])],
            [], target_cpu="x86_64")
        self.assertEqual(universe["provides"]["lrmi(x86-32)"], ["lrmi"])
        self.assertNotIn("lrmi.i686", universe["foreign_index"])

    def test_the_closure_follows_the_capability_names(self):
        """Arch-specificity is in the capability, not in a layered map.

        glibc-devel.i686 requires the unmarked soname `libm.so.6`, which
        only the 32-bit build provides, so glibc.i686 comes in.  It also
        requires plain `kernel-headers`, which is deliberately arch-neutral
        -- and answering *that* with the i686 build is a real bug, not a
        nicety: rpm refuses to install an older kernel-headers.i686
        alongside the newer x86_64 one the base already has.
        """
        universe = build_universe(self.multilib(), [], target_cpu="x86_64")
        closure, problems = runtime_closure(
            ["glibc-devel.i686"], universe["requires"], universe["provides"])

        self.assertEqual(problems, [])
        self.assertIn("glibc.i686", closure)     # via libm.so.6
        self.assertIn("kernel-headers", closure)  # arch-neutral, base's copy
        self.assertNotIn("kernel-headers.i686", closure)

    def test_gcc_needs_no_override_at_all(self):
        """The whole point: a spec asks for 32-bit and gets it.

        `(glibc32 or glibc-devel(x86-32))` used to be reported as a choice
        with no candidates, because neither branch had a provider in a
        collapsed universe.  Now the second branch resolves, the first is
        still nothing -- Fedora 43 ships no glibc32 -- and an alternative
        nothing provides is not an alternative.
        """
        universe = build_universe(self.multilib(), [], target_cpu="x86_64")
        closure, problems = runtime_closure(
            ["gcc"], universe["requires"], universe["provides"],
            extra=[("(glibc32 or glibc-devel(x86-32))", "gcc")],
        )
        self.assertEqual(problems, [])
        self.assertIn("glibc-devel.i686", closure)

        self.assertIn("glibc.i686", closure)

    def test_a_genuine_choice_is_still_refused(self):
        """Two live branches is a real question, and stays one."""
        universe = build_universe(self.multilib() + [
            binary("alpha", provides=["thing"]),
            binary("beta", provides=["thing-too"]),
            binary("asks", requires=["(thing or thing-too)"]),
        ], [], target_cpu="x86_64")
        _, problems = runtime_closure(
            ["asks"], universe["requires"], universe["provides"])
        self.assertEqual([kind for kind, _, _ in problems], ["choice"])

    def test_a_package_that_asks_for_nothing_32_bit_gets_nothing(self):
        """The second arch appears only where a spec asked for one."""
        universe = build_universe(self.multilib(), [], target_cpu="x86_64")
        closure, problems = runtime_closure(
            ["gcc"], universe["requires"], universe["provides"])
        self.assertEqual(problems, [])
        self.assertEqual([c for c in closure if c.endswith(".i686")], [])


class TestRepoTable(unittest.TestCase):
    RELEASES = ("https://dl.fedoraproject.org/pub/fedora/linux/releases/43"
                "/Everything/x86_64/os")
    UPDATES = ("https://dl.fedoraproject.org/pub/fedora/linux/updates/43"
               "/Everything/x86_64")

    def test_names_come_from_the_layout_word_in_the_url(self):
        self.assertEqual(
            derive_repo_name("binary", self.RELEASES, set()), "binary-releases"
        )
        self.assertEqual(
            derive_repo_name("binary", self.UPDATES, set()), "binary-updates"
        )

    def test_a_url_with_no_layout_word_falls_back_to_the_kind(self):
        self.assertEqual(
            derive_repo_name("source", "https://dl.fedoraproject.org/pub",
                             set()),
            "source",
        )

    def test_a_collision_is_suffixed_rather_than_overwriting(self):
        # The name is a key -- two repos sharing one would make half the
        # pins resolve against the wrong base.
        self.assertEqual(
            derive_repo_name("binary", self.UPDATES, {"binary-updates"}),
            "binary-updates2",
        )

    def args(self, **kw):
        base = dict(binary_primary=[], binary_base=[], binary_repo=[],
                    buildroot_primary=[], buildroot_base=[],
                    buildroot_repo=[],
                    source_primary=[], source_base=[], source_repo=[])
        base.update(kw)
        return argparse.Namespace(**base)

    def test_the_nth_base_pairs_with_the_nth_primary(self):
        repos = collect_repos(self.args(
            binary_primary=["/tmp/a/primary.xml", "/tmp/b/primary.xml"],
            binary_base=[self.RELEASES, self.UPDATES],
        ))
        self.assertEqual(
            [(r["name"], r["base"]) for r in repos],
            [("binary-releases", self.RELEASES),
             ("binary-updates", self.UPDATES)],
        )

    def test_the_repodata_path_is_kept_for_the_solve_but_named_by_basename(self):
        repos = collect_repos(self.args(
            binary_primary=["/home/someone/scratch/primary.xml"],
            binary_base=[self.RELEASES],
        ))
        self.assertEqual(repos[0]["path"], "/home/someone/scratch/primary.xml")
        self.assertEqual(repos[0]["primary"], "primary.xml")

    def test_a_missed_base_is_refused_rather_than_zipped_away(self):
        """zip() would drop the unpaired primary and fail hours later.

        The symptom would be a package with no download URL, at build time,
        with nothing pointing back at the command line that caused it.
        """
        with self.assertRaises(SystemExit):
            collect_repos(self.args(
                binary_primary=["/tmp/a/primary.xml", "/tmp/b/primary.xml"],
                binary_base=[self.RELEASES],
            ))

    def test_a_mispaired_name_is_refused(self):
        with self.assertRaises(SystemExit):
            collect_repos(self.args(
                binary_primary=["/tmp/a/primary.xml", "/tmp/b/primary.xml"],
                binary_base=[self.RELEASES, self.UPDATES],
                binary_repo=["only-one"],
            ))

    def test_binary_and_source_repos_share_one_namespace(self):
        repos = collect_repos(self.args(
            binary_primary=["/tmp/a/primary.xml"],
            binary_base=[self.RELEASES],
            source_primary=["/tmp/c/primary.xml"],
            source_base=[self.RELEASES.replace("x86_64/os", "source/tree")],
        ))
        self.assertEqual([r["name"] for r in repos],
                         ["binary-releases", "source-releases"])
        self.assertEqual([r["kind"] for r in repos], ["binary", "source"])

    def test_buildroot_triplet_is_parsed_and_named(self):
        koji = ("https://kojihub.stream.centos.org/kojifiles/repos/"
                "c10s-build/824779/x86_64")
        repos = collect_repos(self.args(
            buildroot_primary=["/tmp/koji/primary.xml.gz"],
            buildroot_base=[koji],
            buildroot_repo=["buildroot-koji"],
        ))
        self.assertEqual(repos, [{
            "name": "buildroot-koji",
            "kind": "buildroot",
            "base": koji,
            "primary": "primary.xml.gz",
            "path": "/tmp/koji/primary.xml.gz",
        }])

    def test_explicit_repo_names_must_be_unique_across_kinds(self):
        with self.assertRaisesRegex(SystemExit, "used more than once"):
            collect_repos(self.args(
                binary_primary=["/tmp/compose.xml"],
                binary_base=[self.RELEASES],
                binary_repo=["packages"],
                buildroot_primary=["/tmp/buildroot.xml"],
                buildroot_base=[self.RELEASES],
                buildroot_repo=["packages"],
            ))


class TestPublicBaseURLs(unittest.TestCase):
    """The base URL recorded in a lockfile is published with it.

    A lockfile is committed and pushed, so a mirror address that reaches
    this field is a hostname disclosed in a public repo.  The reason it
    needs a test rather than care is that passing the mirror is exactly
    what makes the solve succeed on a host without egress -- the wrong
    value is the one that works, and the resulting lockfile is correct in
    every respect a reviewer would think to check.
    """

    def test_public_mirrors_are_accepted(self):
        for url in (
            "https://dl.fedoraproject.org/pub/fedora/linux/releases/43"
            "/Everything/x86_64/os",
            "https://archives.fedoraproject.org/pub/archive/fedora/linux"
            "/releases/41/Everything/source/tree",
            "https://dl.fedoraproject.org/pub/epel/next/9/Everything/x86_64",
            "https://mirror.stream.centos.org/9-stream/BaseOS/x86_64/os",
            "https://mirror.stream.centos.org/10-stream/BaseOS/x86_64/os",
            "https://kojihub.stream.centos.org/kojifiles/repos/"
            "c10s-build/824779/x86_64",
        ):
            with self.subTest(url=url):
                self.assertEqual(check_public_base("--binary-base", url), url)

    def test_a_trailing_slash_is_normalised(self):
        # Otherwise the join downstream produces a doubled slash, which
        # most servers tolerate and some proxies do not.
        self.assertEqual(
            check_public_base(
                "--binary-base",
                "https://dl.fedoraproject.org/pub/fedora/linux/",
            ),
            "https://dl.fedoraproject.org/pub/fedora/linux",
        )

    def test_empty_is_allowed(self):
        """Not every caller records a base; only a wrong one is a problem."""
        self.assertEqual(check_public_base("--binary-base", ""), "")

    def test_a_private_host_is_refused(self):
        for url in (
            "http://localhost:8080/fedora",
            "http://127.0.0.1/fedora",
            "https://mirror.internal.example/fedora/linux",
            # A public hostname on a private port is still a redirection,
            # and comparing the netloc rather than the host would let it by.
            "https://dl.fedoraproject.org:8080/pub/fedora/linux",
            # As would embedding the real host as userinfo.
            "https://dl.fedoraproject.org@internal.example/pub",
        ):
            with self.subTest(url=url):
                with self.assertRaises(SystemExit):
                    check_public_base("--binary-base", url)

    def test_a_non_url_is_refused(self):
        for url in ("/mnt/fedora", "dl.fedoraproject.org/pub", "file:///srv"):
            with self.subTest(url=url):
                with self.assertRaises(SystemExit):
                    check_public_base("--source-base", url)


if __name__ == "__main__":
    unittest.main()
