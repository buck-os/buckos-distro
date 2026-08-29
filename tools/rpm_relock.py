#!/usr/bin/env python3
"""Re-solve an RPM-family lockfile for another architecture."""

import argparse
import copy
import json
import os
import sys

import generate
import relock
import solve


def architecture_base(base, source_cpu, target_cpu, kind):
    if kind == "source":
        return base
    marker = "/{}/".format(source_cpu)
    if marker in base:
        return base.replace(marker, "/{}/".format(target_cpu))
    if base.endswith("/" + source_cpu):
        return base[:-len(source_cpu)] + target_cpu
    raise ValueError("cannot replace architecture in repository base {}".format(base))


def architecture_solve(recorded, source_cpu, target_cpu):
    result = copy.deepcopy(recorded)
    if target_cpu == "aarch64":
        replacements = {
            "grub2-efi-x64-modules": "grub2-efi-aa64-modules",
            "grub2-efi-x64": "grub2-efi-aa64",
            "shim-x64": "shim-aa64",
        }
        images = []
        for image in result.get("images", []):
            name, separator, roots = image.partition("=")
            packages = []
            for package in roots.split(","):
                if package in ("syslinux", "syslinux-nonlinux"):
                    continue
                packages.append(replacements.get(package, package))
            images.append("{}{}{}".format(name, separator, ",".join(packages)))
        result["images"] = images
        result["overrides"] = [
            value.replace("x86-64", "aarch-64")
            for value in result.get("overrides", [])
        ]
    if target_cpu != source_cpu:
        result["probe"] = None
    return result


def recorded_probe_path(template_path, recorded):
    name = recorded.get("probe")
    if not name:
        return None
    path = os.path.join(os.path.dirname(os.path.abspath(template_path)),
                        os.path.basename(name))
    if not os.path.exists(path):
        sys.exit("recorded probe is missing: {}".format(path))
    return path


def solve_argv(template, recorded, repos, target_cpu, output, probe=None,
               strict=True):
    argv = [
        "--flavor", template["flavor"],
        "--release", str(template["release"]),
        "--dist-tag", template["dist_tag"],
        "--target-cpu", target_cpu,
        "--stages", str(recorded["stages"]),
        "--out", output,
    ]
    if probe:
        argv += ["--probe", probe]
    if recorded.get("seed_only"):
        argv.append("--seed-only")
    for repo in repos:
        argv += [
            "--{}-repo".format(repo["kind"]), repo["name"],
            "--{}-base".format(repo["kind"]), repo["base"],
            "--{}-primary".format(repo["kind"]), repo["path"],
        ]
    builds = recorded.get("explicit_build")
    if builds is None:
        builds = [] if recorded.get("source_image_sets") else recorded.get("build", [])
    for value in builds:
        argv += ["--build", value]
    for exception in recorded.get("source_exceptions", []):
        argv += ["--source-exception", json.dumps(
            exception, sort_keys=True, separators=(",", ":"))]
    for flag, key in (
        ("--seed-package", "seed_packages"),
        ("--override", "overrides"),
        ("--image", "images"),
        ("--image-override", "image_overrides"),
        ("--source-variant", "source_variants"),
        ("--source-image", "source_image_sets"),
        ("--prebuilt-source", "prebuilt_sources"),
    ):
        for value in recorded.get(key, []):
            argv += [flag, value]
    if strict:
        argv.append("--strict")
    return argv


def convergence_state(lock):
    """The incomplete solve states that require another probe pass."""
    packages = lock.get("packages", {})
    return {
        "problems": lock.get("problems", []),
        "unprobed": {
            name: package["dynamic_buildrequires"]
            for name, package in sorted(packages.items())
            if package["dynamic_buildrequires"].get("suspected")
        },
        "unmet": {
            name: package["dynamic_buildrequires"]
            for name, package in sorted(packages.items())
            if package["dynamic_buildrequires"].get("unmet")
        },
    }


def converged(state):
    return not any(state.values())


def probe_required(state, probe_path):
    """Whether a probe pass is still required for this solve state."""
    return probe_path is None or not converged(state)


def describe_state(state):
    return (
        "{} unresolved problem(s), {} unprobed package(s), {} unmet probe(s)"
    ).format(
        len(state["problems"]),
        len(state["unprobed"]),
        len(state["unmet"]),
    )


def read_lock(path):
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def solve_and_generate(template, recorded, repos, target_cpu, output,
                       probe_path=None, strict=True):
    solve.main(solve_argv(
        template,
        recorded,
        repos,
        target_cpu,
        output,
        probe=probe_path,
        strict=strict,
    ))
    generate.main([output])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", required=True)
    parser.add_argument("--target-cpu", required=True, choices=("x86_64", "aarch64"))
    parser.add_argument("--output", required=True)
    parser.add_argument("--repo-cache", default=None)
    parser.add_argument("--no-generate", action="store_true")
    parser.add_argument("--probe", action="store_true",
                        help="probe dynamic BuildRequires and repeat the "
                             "solve until it converges")
    parser.add_argument("--buck2", default=None,
                        help="buck2 used for probes (default: ./buck2 or PATH)")
    parser.add_argument("-c", "--config", action="append", default=[],
                        metavar="SECTION.KEY=VALUE",
                        help="extra buck2 -c flag for probes (repeatable)")
    parser.add_argument("--source-image", action="append", default=[],
                        metavar="NAME",
                        help="replace source derivation with this image set "
                             "(repeatable)")
    parser.add_argument("--prebuilt-source", action="append", default=[],
                        metavar="SOURCE",
                        help="replace the explicit prebuilt source list "
                             "(repeatable)")
    parser.add_argument("--source-exception", action="append", default=[],
                        metavar="JSON",
                        help="replace source exceptions with JSON objects "
                             "(repeatable)")
    args = parser.parse_args(argv)
    if args.probe and args.no_generate:
        parser.error("--probe requires generated Buck targets")

    with open(args.template, encoding="utf-8") as stream:
        template = json.load(stream)
    source_cpu = template["target_cpu"]
    recorded = architecture_solve(
        template["solve"], source_cpu, args.target_cpu)
    if args.source_image:
        recorded["source_image_sets"] = sorted(set(args.source_image))
        recorded["explicit_build"] = []
    if args.prebuilt_source:
        recorded["prebuilt_sources"] = sorted(set(args.prebuilt_source))
    if args.source_exception:
        try:
            recorded["source_exceptions"] = [
                solve.parse_source_exception(value)
                for value in args.source_exception
            ]
        except ValueError as exc:
            parser.error(str(exc))
    cache = args.repo_cache or os.path.join(
        os.path.dirname(os.path.abspath(args.output)),
        "repodata",
        str(template["release"]),
        args.target_cpu,
    )
    repos = []
    for repo in template["repos"]:
        base = architecture_base(
            repo["base"], source_cpu, args.target_cpu, repo["kind"])
        path = relock.sync_repo(base, os.path.join(cache, repo["name"]), repo["name"])
        if path:
            repos.append({
                "base": base,
                "kind": repo["kind"],
                "name": repo["name"],
                "path": path,
            })
    initial_probe = (
        recorded_probe_path(args.template, recorded)
        if args.target_cpu == source_cpu
        else None
    )
    if args.no_generate:
        solve.main(solve_argv(
            template,
            recorded,
            repos,
            args.target_cpu,
            args.output,
            probe=initial_probe,
        ))
        return

    solve_and_generate(
        template,
        recorded,
        repos,
        args.target_cpu,
        args.output,
        probe_path=initial_probe,
        strict=not args.probe,
    )
    if not args.probe:
        return

    # Imported here because probe imports relock, which this module also
    # uses. Keeping the dependency out of module initialization makes the
    # direction explicit and avoids a partially initialized module when
    # these helpers are imported by tests.
    import probe as probe_mod

    root = relock.repo_root()
    config = list(probe_mod.DEFAULT_CONFIG)
    for flag in args.config:
        config += ["-c", flag]

    current_probe = initial_probe
    seen = set()
    while True:
        state = convergence_state(read_lock(args.output))
        if not probe_required(state, current_probe):
            # Re-run the completed solve with strict diagnostics enabled.
            # Intermediate probe solves must persist their problem state so
            # Buck can generate the targets needed to discover the missing
            # requirements; only the final solve is allowed to be strict.
            solve_and_generate(
                template,
                recorded,
                repos,
                args.target_cpu,
                args.output,
                probe_path=current_probe,
                strict=True,
            )
            final_state = convergence_state(read_lock(args.output))
            if not converged(final_state):
                sys.exit("{}: final strict solve is incomplete: {}".format(
                    args.output, describe_state(final_state)))
            print("{}: solve/probe converged".format(args.output),
                  file=sys.stderr)
            return
        fingerprint = json.dumps(state, sort_keys=True)
        if fingerprint in seen:
            sys.exit("{}: solve/probe did not converge: {}".format(
                args.output, describe_state(state)))
        seen.add(fingerprint)

        probe_mod.write_probe_file(
            args.output,
            root,
            config=config,
            buck2=probe_mod.resolve_buck2(root, args.buck2),
        )
        current_probe = probe_mod.probe_path(args.output)
        solve_and_generate(
            template,
            recorded,
            repos,
            args.target_cpu,
            args.output,
            probe_path=current_probe,
            strict=False,
        )


if __name__ == "__main__":
    main()
