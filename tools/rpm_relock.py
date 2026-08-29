#!/usr/bin/env python3
"""Re-solve an RPM-family lockfile for another architecture."""

import argparse
import copy
import json
import os

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


def architecture_solve(recorded, target_cpu):
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
    result["probe"] = None
    return result


def solve_argv(template, recorded, repos, target_cpu, output):
    argv = [
        "--flavor", template["flavor"],
        "--release", str(template["release"]),
        "--dist-tag", template["dist_tag"],
        "--target-cpu", target_cpu,
        "--stages", str(recorded["stages"]),
        "--out", output,
    ]
    if recorded.get("seed_only"):
        argv.append("--seed-only")
    for repo in repos:
        argv += [
            "--{}-repo".format(repo["kind"]), repo["name"],
            "--{}-base".format(repo["kind"]), repo["base"],
            "--{}-primary".format(repo["kind"]), repo["path"],
        ]
    for flag, key in (
        ("--build", "build"),
        ("--seed-package", "seed_packages"),
        ("--override", "overrides"),
        ("--image", "images"),
        ("--image-override", "image_overrides"),
    ):
        for value in recorded.get(key, []):
            argv += [flag, value]
    argv.append("--strict")
    return argv


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", required=True)
    parser.add_argument("--target-cpu", required=True, choices=("x86_64", "aarch64"))
    parser.add_argument("--output", required=True)
    parser.add_argument("--repo-cache", default=None)
    parser.add_argument("--no-generate", action="store_true")
    args = parser.parse_args(argv)

    with open(args.template, encoding="utf-8") as stream:
        template = json.load(stream)
    source_cpu = template["target_cpu"]
    recorded = architecture_solve(template["solve"], args.target_cpu)
    cache = args.repo_cache or os.path.join(
        os.path.dirname(os.path.abspath(args.output)),
        "repodata",
        str(template["release"]),
        args.target_cpu,
    )
    repos = []
    for repo in template["repos"]:
        base = architecture_base(repo["base"], source_cpu, args.target_cpu, repo["kind"])
        path = relock.sync_repo(base, os.path.join(cache, repo["name"]), repo["name"])
        if path:
            repos.append({
                "base": base,
                "kind": repo["kind"],
                "name": repo["name"],
                "path": path,
            })
    solve.main(solve_argv(template, recorded, repos, args.target_cpu, args.output))
    if not args.no_generate:
        generate.main([args.output])


if __name__ == "__main__":
    main()
