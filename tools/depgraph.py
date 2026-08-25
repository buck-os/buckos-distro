"""Dependency-graph algorithms for the buckos-distro solver.

Pure data-structure code, deliberately free of any rpm/dnf/network
dependency so it can be tested offline.  The rpm-specific parts (reading
repodata, resolving capabilities through libsolv) live in solve.py and
feed this module plain dicts.

Vocabulary
----------
capability   a string a package Provides and others Require, e.g.
             "zlib-devel", "pkgconfig(zlib)", "/usr/bin/sed"
binary pkg   a concrete binary package name ("glibc-devel")
source pkg   the source package that builds it ("glibc")

See SPEC.md section 3a.
"""

import re
from collections import defaultdict

# ── Capability resolution ────────────────────────────────────────────


class AmbiguousProvider(Exception):
    """Several packages provide a capability and no policy picked one."""

    def __init__(self, capability, candidates):
        self.capability = capability
        self.candidates = candidates
        super().__init__(
            "capability {!r} is provided by {} packages: {}".format(
                capability, len(candidates), ", ".join(sorted(candidates))
            )
        )


class UnresolvedCapability(Exception):
    """Nothing in the universe provides a required capability."""

    def __init__(self, capability, required_by):
        self.capability = capability
        self.required_by = required_by
        super().__init__(
            "capability {!r} (required by {}) has no provider".format(
                capability, required_by
            )
        )


def resolve_capability(capability, provides, required_by, overrides=None):
    """Map one capability to a single providing binary package.

    Policy, in order:
      1. an explicit override from the flavor's config
      2. the unique provider, when there is exactly one
      3. an exact name match -- "zlib-devel" provided by zlib-devel itself
         beats some other package that happens to Provide it
      4. otherwise raise, so the ambiguity is resolved by a human and
         recorded in the lockfile rather than guessed at build time
    """
    overrides = overrides or {}
    if capability in overrides:
        return overrides[capability]

    candidates = provides.get(capability)
    if not candidates:
        raise UnresolvedCapability(capability, required_by)

    candidates = set(candidates)
    if len(candidates) == 1:
        return next(iter(candidates))
    if capability in candidates:
        return capability
    raise AmbiguousProvider(capability, candidates)


def validate_overrides(overrides, provides, scope=""):
    """Check that each override names a package that really provides it.

    An override is a human answering an ambiguity the solver refused to
    guess at, and until now it was taken entirely on trust: whatever
    package was named went into the closure and the capability was marked
    satisfied.  A typo, or a package that used to provide a capability and
    no longer does, therefore produced a *clean* solve -- zero unresolved,
    a well-formed lockfile -- and surfaced only when rpm ran the
    transaction and said the capability was needed by something and
    provided by nothing.  That is the worst place to find out: a hundred
    packages downloaded, a namespace set up, and an error message that
    names the requiring package rather than the wrong flag.

    Both failure modes are real.  `/usr/bin/systemd-sysusers=systemd` is
    plausible and wrong -- systemd requires that path in F43 rather than
    shipping it, which is why the split package exists at all -- and
    `fedora-release-variant=fedora-release-common` was wrong for a year
    without anyone noticing, because fedora-release provides the
    capability and was in the tree anyway.

    Returns problems rather than raising, matching runtime_closure: a
    reviewer wants every bad flag in one pass, not the first one.

    Rich-expression keys are skipped.  Their value is a package to pull in
    to settle an `(A or B)`, not a provider of the expression text, so
    there is nothing to check it against.
    """
    problems = []
    for capability in sorted(overrides):
        if is_rich_dep(capability) or parse_or_dep(capability):
            continue
        package = overrides[capability]
        providers = provides.get(capability)
        if not providers:
            problems.append((
                "bad-override",
                "--override {}={} names a capability nothing provides".format(
                    capability, package
                ),
                scope or "override",
            ))
        elif package not in providers:
            problems.append((
                "bad-override",
                "--override {}={} but {} does not provide it; providers are "
                "{}".format(
                    capability, package, package, ", ".join(sorted(providers))
                ),
                scope or "override",
            ))
    return problems


def strip_capability_version(capability):
    """Drop version constraints and arch qualifiers from a capability.

    "gcc-c++ >= 4.8"   -> "gcc-c++"
    "libfoo(x86-64)"   -> "libfoo(x86-64)"   (isa suffix is part of the name)

    Version ranges are intentionally discarded: the lockfile pins an exact
    NEVRA per capability, so the range only matters at solve time, and at
    solve time libsolv enforces it.  Keeping the range here would make the
    provides-map key not match.

    Rich deps are returned untouched.  They have their own internal
    grammar, and a version constraint inside one is not a trailing
    constraint on the whole expression: splitting
    "(cmake-rpm-macros = 3.31.6-4.fc43 if rpm-build)" at " = " yields
    "(cmake-rpm-macros", which is not a capability, loses the condition
    that gives the dep its meaning, and gets reported that way in the
    lockfile's problem list.
    """
    if is_rich_dep(capability):
        return capability.strip()
    for op in (">=", "<=", "=", ">", "<"):
        idx = capability.find(" {} ".format(op))
        if idx != -1:
            return capability[:idx].strip()
    return capability.strip()


def is_rich_dep(capability):
    """True for rpm boolean/rich dependencies, e.g. "(a or b)".

    These need libsolv to evaluate; the solver routes them there rather
    than trying to parse them.
    """
    return capability.lstrip().startswith("(")


# "(THEN if COND)" -- non-greedy THEN so the split lands on the first
# " if ", and $-anchored so the closing paren is the expression's own.
_RICH_IF = re.compile(r"^\((?P<then>.+?) if (?P<cond>.+?)\)$")

# Any of these inside either half means the expression is compound, and
# this parser has no business claiming to understand it.
_RICH_COMPOUND = (" or ", " and ", " with ", " without ", " unless ", " else ")

# Every operator rpm's boolean grammar has, including the conditional.
_RICH_OPS = _RICH_COMPOUND + (" if ",)


def _is_single_group(text):
    """Are the outer parentheses of `text` its own, matched at the end?

    "(a or b)" yes.  "(a) or (b)" no -- there the first paren closes
    early, so the string is a sequence of groups rather than one, and
    treating it as one would silently drop everything after the first.
    """
    if not (text.startswith("(") and text.endswith(")")):
        return False
    depth = 0
    for index, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index == len(text) - 1
    return False


def unwrap_group(capability):
    """Drop parentheses that only group: "(foo >= 1)" -> "foo >= 1".

    rpm's own dependency generators emit these.  kernel-core carries

        ((linux-firmware >= 20150904-56.git6ebf5d57) if linux-firmware)

    and the inner parens wrap a plain versioned capability -- there is no
    boolean operator inside them at all.  Without this the outer
    "(A if B)" is refused, because A itself looks rich, and a conditional
    that is in fact perfectly simple gets reported as an expression
    nothing can read.

    Only a group whose parens are balanced end to end and which contains
    no operator is unwrapped.  Anything else is a real sub-expression and
    comes back untouched; `pkgconfig(zlib)` is not even a candidate, since
    it does not start with a paren.
    """
    text = capability.strip()
    if not _is_single_group(text):
        return text
    inner = text[1:-1].strip()
    if any(op in inner for op in _RICH_OPS):
        return text
    return inner


def parse_or_dep(capability):
    """Split "(A or B or ...)" into its alternatives, else None.

    Returned with version constraints stripped, in the order rpm wrote
    them.  Unlike every other shape here this one is deliberately *not*
    resolved on sight: `or` is satisfied by any one branch, so the honest
    question is whether the closure already contains one -- and that
    cannot be answered until the closure stops growing.  runtime_closure
    defers it alongside the conditionals for exactly that reason, and
    reports it only if the fixed point arrives with no branch present.

    systemd's `Requires: (util-linux-core or util-linux)` is the case that
    forced this. Both branches are real packages providing the same
    programs; resolving on sight would pick the first and put a second
    `mount` into an image that already had one, or -- worse, since
    util-linux-core is a subset -- pick the smaller one and leave the
    image without `mount` at all if the branch chosen were dropped later.
    """
    text = capability.strip()
    if not _is_single_group(text):
        return None
    inner = text[1:-1]
    if " or " not in inner:
        return None
    # A mixed expression -- "(a or b if c)" -- is a precedence question
    # this two-way split does not answer, so it is refused rather than
    # guessed at.
    for op in _RICH_OPS:
        if op != " or " and op in inner:
            return None
    parts = [unwrap_group(part) for part in inner.split(" or ")]
    if any(not part or is_rich_dep(part) for part in parts):
        return None
    return [strip_capability_version(part) for part in parts]


def parse_conditional_dep(capability):
    """Split a simple "(A if B)" rich dep into (A, B), else None.

    This one shape is worth evaluating rather than deferring, because it
    is how Fedora attaches build-time macros to a buildroot.  cmake
    carries `Requires: (cmake-rpm-macros = 3.31.6-4.fc43 if rpm-build)`,
    and every other `*-rpm-macros` package hangs off its tool the same
    way.  Treating that as an opaque rich dep does not lose a nicety: the
    macro file never lands, %cmake stays unexpanded, and the spec's
    %build section runs as literal shell text -- which fails as
    "line 58: fg: no job control", nowhere near the cause.

    Only the simple shape.  Anything compound is still handed back as
    unparsed, because a partial reading of a boolean expression is worse
    than an honest refusal to read it.
    """
    if not is_rich_dep(capability):
        return None
    match = _RICH_IF.match(capability.strip())
    if not match:
        return None
    then, cond = unwrap_group(match.group("then")), unwrap_group(match.group("cond"))
    for op in _RICH_COMPOUND:
        if op in then or op in cond:
            return None
    if is_rich_dep(then) or is_rich_dep(cond):
        return None
    return strip_capability_version(then), strip_capability_version(cond)


# "(A op V with A op V)" -- a version *range*, not a choice between two
# packages.  Non-greedy so the split lands on the first " with ".
_RICH_WITH = re.compile(r"^\((?P<left>.+?) with (?P<right>.+?)\)$")


def parse_range_dep(capability):
    """Collapse "(X >= a with X < b)" to X, else None.

    rpm's `with` means "one package satisfying both halves", so when both
    halves name the same capability the expression is a version range
    written the only way rpm's grammar allows -- not a boolean choice.
    Reducing it to the capability name loses only the range, which is
    exactly what strip_capability_version() already discards everywhere
    else: the lockfile pins one exact build, so a range never decides
    anything.

    This is the last shape standing between the solver and a closed set.
    `rpm --install` over the F43 seed rejected the transaction with

        (python3.14dist(gitdb) < 5~~ with python3.14dist(gitdb) >= 4.0.1)
        is needed by python3-GitPython

    -- one unresolved dep, and it is this.  Deferring it is not neutral:
    the package that would satisfy it never enters the closure, so the
    rootfs that gets built is one rpm refuses to install.

    Halves naming *different* capabilities are still refused.  Those are a
    genuine conjunction -- "one package providing both of these" -- and
    picking either name would be a guess about which provider wins.
    """
    if not is_rich_dep(capability):
        return None
    match = _RICH_WITH.match(capability.strip())
    if not match:
        return None
    left, right = unwrap_group(match.group("left")), unwrap_group(match.group("right"))
    for op in _RICH_COMPOUND:
        # " with " itself is excluded: a third half means a chain this
        # two-way split has already mis-read.
        if op in left or op in right:
            return None
    if is_rich_dep(left) or is_rich_dep(right):
        return None
    left, right = strip_capability_version(left), strip_capability_version(right)
    return left if left == right else None


# ── Transitive closure ───────────────────────────────────────────────


def runtime_closure(roots, requires, provides, overrides=None):
    """Transitively close a set of binary packages over their Requires.

    Installing zlib-devel means installing zlib and its deps too, or the
    buildroot is not actually usable.  Returns a set of binary package
    names.  Unresolvable capabilities are collected rather than raised so
    the caller can report them all at once.

    "(A if B)" deps are evaluated here rather than deferred, against the
    closure itself: B's condition is "B is in the buildroot", and the
    buildroot is exactly what this function computes.  That makes the
    evaluation order-dependent -- a conditional read early can have its
    condition satisfied by a package pulled in later -- so pending
    conditionals are re-checked to a fixed point instead of once.

    "(A or B)" is deferred the same way and for the same reason, but with
    the opposite ending: a conditional whose condition never holds is
    simply not required, whereas a choice with no branch satisfied is an
    unmet dependency and is reported.  An `--override` keyed on the whole
    expression text settles it, which is the same lever ambiguity gets
    everywhere else here.
    """
    overrides = overrides or {}
    seen = set()
    problems = []
    frontier = list(roots)
    # (then_capability, cond_capability, required_by) not yet fired.
    pending = []
    # (expression, [alternative, ...], required_by) with no branch present.
    choices = []

    def resolve(cap, who):
        try:
            return resolve_capability(cap, provides, who, overrides)
        except (AmbiguousProvider, UnresolvedCapability) as exc:
            problems.append(("unresolved", str(exc), who))
            return None

    def capability_present(cap, who):
        """Does the closure so far already contain a provider of `cap`?

        Asked of an `if` clause -- is the condition true -- and of each
        branch of an `or` -- is this alternative already installed.

        Quiet on purpose, and re-asked every round, so it must not record
        problems: a capability nothing in this universe provides is not an
        unsolved dependency here, it is a condition that is false or a
        branch that is absent.  Reporting it would both mislead and pile up
        one duplicate per fixed-point iteration.
        """
        try:
            return resolve_capability(cap, provides, who, overrides) in seen
        except (AmbiguousProvider, UnresolvedCapability):
            return False

    while True:
        while frontier:
            pkg = frontier.pop()
            if pkg in seen:
                continue
            seen.add(pkg)
            for cap in requires.get(pkg, ()):
                base = strip_capability_version(cap)
                if is_rich_dep(base):
                    resolved = _reduce_rich(base, pkg, pending, choices, problems)
                    if resolved is None:
                        continue
                    base = resolved
                provider = resolve(base, pkg)
                if provider is not None and provider not in seen:
                    frontier.append(provider)

        # The frontier is drained, so `seen` is stable enough to test
        # conditions against.  Anything that fires refills it and we go
        # round again; anything whose condition is still unmet stays
        # pending, and is simply not required -- (gpgverify if gnupg2)
        # in a buildroot without gnupg2 is a dependency that correctly
        # does not apply, not an unsolved one.
        still_pending = []
        for then_cap, cond_cap, who in pending:
            if not capability_present(cond_cap, who):
                still_pending.append((then_cap, cond_cap, who))
                continue
            provider = resolve(then_cap, who)
            if provider is not None and provider not in seen:
                frontier.append(provider)
        pending = still_pending

        # Same treatment for the choices, and the same reason -- but "is
        # any branch already here" is a question that can only be asked of
        # a drained frontier, and an override that names a branch counts as
        # a decision rather than a presence, so it fires immediately.
        still_choosing = []
        for expression, alternatives, who in choices:
            # Through capability_present, not `alt in seen`: a branch can
            # be a capability rather than a package name -- rpm writes
            # "(libfoo.so.1 or libbar.so.1)" as readily as it writes two
            # package names -- and comparing a capability against a set of
            # package names silently never matches.
            if any(capability_present(alt, who) for alt in alternatives):
                continue
            if expression in overrides:
                chosen = overrides[expression]
                if chosen not in seen:
                    frontier.append(chosen)
                continue
            still_choosing.append((expression, alternatives, who))
        choices = still_choosing

        if not frontier:
            break

    # Nothing is growing any more, so a choice still unsatisfied here will
    # never be.  Reported rather than resolved: rpm picks a branch by
    # policy, and inventing a policy is how a solver quietly installs a
    # different distro than the one anyone reviewed.
    for expression, alternatives, who in choices:
        problems.append((
            "choice",
            "{} -- no branch present; settle it with "
            "--override '{}=<package>'".format(expression, expression),
            who,
        ))
    return seen, problems


def _reduce_rich(base, pkg, pending, choices, problems):
    """Reduce one rich dep to a plain capability, or defer/refuse it.

    Returns the capability to resolve, or None when the expression has
    been taken care of another way -- deferred to the fixed point, or
    recorded as a problem.  Split out of runtime_closure because the
    shapes now outnumber the loop that consumes them.
    """
    # Parentheses with no operator inside are just parentheses.  Checked
    # first, because it is what makes "((A >= v) if B)" readable at all.
    ungrouped = unwrap_group(base)
    if not is_rich_dep(ungrouped):
        return strip_capability_version(ungrouped)

    conditional = parse_conditional_dep(base)
    if conditional is not None:
        pending.append(conditional + (pkg,))
        return None

    alternatives = parse_or_dep(base)
    if alternatives is not None:
        choices.append((base, alternatives, pkg))
        return None

    # A version range is unconditional: resolve it now rather than
    # deferring it, which is only for expressions with something to test.
    ranged = parse_range_dep(base)
    if ranged is not None:
        return ranged

    # Left for libsolv; recorded so it is never silently dropped.
    problems.append(("rich", base, pkg))
    return None


# ── Source-level graph and cycle handling ────────────────────────────


def project_to_source_graph(build_deps, source_of, build_set):
    """Collapse binary-level build deps onto a source-package graph.

    build_deps: {source_pkg: set(binary_pkg)}  -- resolved BuildRequires
                closure for each source package we intend to build
    source_of:  {binary_pkg: source_pkg}
    build_set:  the source packages being built from source

    Only edges *within* build_set become graph edges.  An edge to a
    package outside build_set is satisfied from the seed and therefore
    cannot participate in a cycle.
    """
    graph = {src: set() for src in build_set}
    for src, bins in build_deps.items():
        if src not in build_set:
            continue
        for binary in bins:
            dep_src = source_of.get(binary)
            if dep_src is None or dep_src not in build_set:
                continue  # from seed -- not an edge
            if dep_src != src:
                graph[src].add(dep_src)
    return graph


def strongly_connected_components(graph):
    """Tarjan's SCC, iterative so deep graphs do not blow the stack.

    Returns components in reverse topological order: a component appears
    only after everything it depends on.  Deterministic given a
    deterministic graph, because neighbours are visited sorted.
    """
    index_of = {}
    low = {}
    on_stack = {}
    stack = []
    result = []
    counter = [0]

    for root in sorted(graph):
        if root in index_of:
            continue
        # work items: (node, iterator over its sorted neighbours)
        work = [(root, iter(sorted(graph[root])))]
        index_of[root] = low[root] = counter[0]
        counter[0] += 1
        stack.append(root)
        on_stack[root] = True

        while work:
            node, it = work[-1]
            advanced = False
            for nxt in it:
                if nxt not in index_of:
                    index_of[nxt] = low[nxt] = counter[0]
                    counter[0] += 1
                    stack.append(nxt)
                    on_stack[nxt] = True
                    work.append((nxt, iter(sorted(graph.get(nxt, ())))))
                    advanced = True
                    break
                if on_stack.get(nxt):
                    low[node] = min(low[node], index_of[nxt])
            if advanced:
                continue

            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index_of[node]:
                component = []
                while True:
                    w = stack.pop()
                    on_stack[w] = False
                    component.append(w)
                    if w == node:
                        break
                result.append(sorted(component))

    return result


def has_self_loop(graph, node):
    return node in graph.get(node, ())


def find_cycles(graph):
    """Return the SCCs that represent genuine bootstrap cycles."""
    cycles = []
    for comp in strongly_connected_components(graph):
        if len(comp) > 1 or has_self_loop(graph, comp[0]):
            cycles.append(comp)
    return cycles


def topological_order(graph):
    """Deterministic topological order; SCCs collapse to a stable block.

    Built from the SCC order, so it is total even when cycles exist.
    """
    order = []
    for comp in strongly_connected_components(graph):
        order.extend(comp)
    return order


# ── Cycle breaking by staging ────────────────────────────────────────


def stage_cycle(component, graph, stages=3):
    """Turn a dependency cycle into an explicit staged bootstrap.

    A cycle like gcc <-> glibc cannot be a DAG, but the real-world
    bootstrap is a DAG once the stages are named:

        gcc-stage1    against the seed's glibc-devel
        glibc-stage2  against gcc-stage1
        gcc-stage3    against glibc-stage2     <- ships

    Returns a list of stage records in build order.  Stage 1 members take
    every in-cycle dep from the seed; later stages take in-cycle deps from
    the previous stage.  Deps outside the component are unaffected.

    `stages` is the number of passes.  Two is enough to make each package
    self-hosted; three is the conventional choice because it lets you
    verify stage2 and stage3 are identical, which is the standard
    bootstrap sanity check.
    """
    if stages < 1:
        raise ValueError("stages must be >= 1")

    members = sorted(component)
    plan = []
    for stage in range(1, stages + 1):
        for pkg in members:
            in_cycle_deps = sorted(
                d for d in graph.get(pkg, ()) if d in set(members)
            )
            plan.append({
                "source": pkg,
                "stage": stage,
                "target": "{}-stage{}".format(pkg, stage),
                # Stage 1 bootstraps off the binary seed; later stages
                # consume the previous stage's output.
                "cycle_deps_from": "seed" if stage == 1 else "stage{}".format(stage - 1),
                "cycle_deps": in_cycle_deps,
                "ships": stage == stages,
            })
    return plan


def plan_build_order(build_deps, source_of, build_set, stages=3):
    """Full graph plan: order, cycles, and the staging needed to break them.

    Returns a dict suitable for embedding in the lockfile.
    """
    graph = project_to_source_graph(build_deps, source_of, build_set)
    cycles = find_cycles(graph)
    cyclic = {pkg for comp in cycles for pkg in comp}

    staged = []
    for comp in cycles:
        staged.extend(stage_cycle(comp, graph, stages=stages))

    return {
        "order": [p for p in topological_order(graph) if p not in cyclic],
        "cycles": cycles,
        "staged": staged,
        "acyclic": not cycles,
    }


# ── Bootstrap depth reporting ────────────────────────────────────────


def bootstrap_depth(build_deps, source_of, build_set):
    """Summarize how much of the closure is built vs seeded.

    The headline number for "how much of this distro do we actually build
    from source" (SPEC.md section 3a).
    """
    built = set()
    seeded = set()
    for src, bins in build_deps.items():
        if src not in build_set:
            continue
        for binary in bins:
            dep_src = source_of.get(binary)
            if dep_src in build_set:
                built.add(binary)
            else:
                seeded.add(binary)

    total = len(built) + len(seeded)
    return {
        "built_from_source": len(built),
        "from_seed": len(seeded),
        "total_build_deps": total,
        "fraction_built": round(len(built) / total, 4) if total else 0.0,
        "source_packages_built": len(build_set),
    }
