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


# Any of these inside either half means the expression is compound.
_RICH_COMPOUND = (" or ", " and ", " with ", " without ", " unless ", " else ")

# Every operator rpm's boolean grammar has, including the conditional.
_RICH_OPS = _RICH_COMPOUND + (" if ",)

# Longest first, so " without " is never mistaken for " with " followed by
# a stray "out".  (It could not be -- " with " demands a space where
# "without" has an "o" -- but relying on that is a trap for whoever adds
# the next operator.)
_BOOL_OPS = ("without", "unless", "with", "else", "and", "or", "if")


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


# ── rpm's boolean dependency grammar ─────────────────────────────────
#
# Three hand-written shape matchers used to live here, one per expression
# rpm had so far turned out to emit, each refusing anything compound.  That
# held until the build set grew past a handful of packages: solving the
# whole live image's 126 source packages surfaced 44 expressions none of
# them would read, and 27 of those were one shape --
#
#   ((rpm-build >= 4.14.90 with (rpm-build < 4.19.90 or rpm-build >= 4.19.91-8))
#    if rpm-build)
#
# which is redhat-rpm-config's, and therefore attached to a large fraction
# of Fedora.  A fourth matcher would have read that one and stopped at the
# fifth shape.  So this parses the grammar instead.
#
# The grammar, from rpm's rpmdsParse:
#
#   expr    := '(' body ')'
#   body    := operand (OP operand)*          -- one OP per level, chained
#            | operand 'if' operand ['else' operand]
#            | operand 'unless' operand ['else' operand]
#   operand := expr | capability
#   OP      := and | or | with | without
#
# rpm requires explicit parentheses to mix operators, which is what makes
# a flat per-level split correct rather than a precedence guess.
#
# Splitting is depth-aware, and that is not decoration: capability names
# contain parentheses of their own -- `crate(anyhow/default)`,
# `pkgconfig(zlib)`, `python3.14dist(ldap3)` -- so a naive search for
# " with " inside `(crate(a/b) >= 1 with crate(a/b) < 2)` is fine but a
# naive search for the *closing* paren is not.


def _top_level_ops(text):
    """Every operator token at paren depth zero, as (index, op)."""
    found = []
    depth = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0 and char == " ":
            for op in _BOOL_OPS:
                token = " {} ".format(op)
                if text.startswith(token, index):
                    found.append((index, op))
                    # Land on the token's trailing space so an operator
                    # never swallows the separator the next one needs.
                    index += len(token) - 1
                    break
        index += 1
    return found


def _split_top(text, op):
    """Split on one operator at depth zero, keeping every operand."""
    token = " {} ".format(op)
    parts = []
    depth = 0
    start = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0 and text.startswith(token, index):
            parts.append(text[start:index])
            index += len(token)
            start = index
            continue
        index += 1
    parts.append(text[start:])
    return [part.strip() for part in parts]


def parse_boolean(capability):
    """Parse an rpm boolean dependency into a tree, or None if unreadable.

    Nodes are plain tuples so they compare, print and test without a class:

        ("cap", "gcc >= 4.8")
        ("and" | "or" | "with", [child, ...])
        ("without", left, right)
        ("if" | "unless", then, cond, else_or_None)

    None means "this parser does not understand it", which the caller must
    treat as a refusal rather than as an absence -- the whole point of the
    shape matchers this replaces was that a partial reading of a boolean
    expression is worse than an honest one.
    """
    text = capability.strip()
    if not is_rich_dep(text) or not _is_single_group(text):
        return None
    return _parse_body(text[1:-1].strip())


def _parse_body(text):
    text = text.strip()
    if not text:
        return None

    if _is_single_group(text):
        inner = text[1:-1].strip()
        # Parens with no operator inside are just parens -- the case
        # unwrap_group() exists for, reached here structurally.
        return _parse_body(inner) if _top_level_ops(inner) else ("cap", inner)

    ops = _top_level_ops(text)
    if not ops:
        return ("cap", text)

    names = [op for _, op in ops]

    # `if` and `unless` are the only operators that take a third operand,
    # and the only ones that may appear alongside a different keyword.
    if names[0] in ("if", "unless"):
        head = names[0]
        if len(ops) == 1:
            index, _ = ops[0]
            then = _parse_body(text[:index])
            cond = _parse_body(text[index + len(head) + 2:])
            return None if then is None or cond is None else (head, then, cond, None)
        if len(ops) == 2 and names[1] == "else":
            first, _ = ops[0]
            second, _ = ops[1]
            then = _parse_body(text[:first])
            cond = _parse_body(text[first + len(head) + 2:second])
            other = _parse_body(text[second + len("else") + 2:])
            if then is None or cond is None or other is None:
                return None
            return (head, then, cond, other)
        return None

    # Everything else chains, and rpm forbids mixing without parentheses.
    if len(set(names)) != 1:
        return None
    op = names[0]
    if op == "else":
        return None

    children = [_parse_body(part) for part in _split_top(text, op)]
    if any(child is None for child in children):
        return None

    if op == "without":
        # Binary by definition: "provides A but not B".  A chain would be
        # a precedence question rpm does not let you ask.
        return None if len(children) != 2 else ("without", children[0], children[1])
    return (op, children)


def unparse_boolean(node):
    """Render a tree back to rpm's syntax, for messages and override keys.

    Round-trips through parse_boolean, which is what makes it usable as an
    override key: a human reading `--override '(a or b)=a'` off an error
    message needs the string they type to be the string the solver looks
    up, and the sub-expression that failed is often not the text any spec
    literally wrote.
    """
    kind = node[0]
    if kind == "cap":
        return node[1]
    if kind in ("and", "or", "with"):
        return "({})".format(
            " {} ".format(kind).join(unparse_boolean(c) for c in node[1])
        )
    if kind == "without":
        return "({} without {})".format(
            unparse_boolean(node[1]), unparse_boolean(node[2])
        )
    # if / unless, with the optional else tail.
    text = "({} {} {}".format(
        unparse_boolean(node[1]), kind, unparse_boolean(node[2])
    )
    if node[3] is not None:
        text += " else {}".format(unparse_boolean(node[3]))
    return text + ")"


def _leaf_names(node):
    """Every bare capability name a node mentions, or None if not all leaves."""
    kind = node[0]
    if kind == "cap":
        return [strip_capability_version(node[1])]
    if kind in ("and", "or", "with"):
        names = []
        for child in node[1]:
            child_names = _leaf_names(child)
            if child_names is None:
                return None
            names.extend(child_names)
        return names
    return None


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
    node = parse_boolean(capability)
    if node is None or node[0] != "or":
        return None
    # Only a flat choice between plain capabilities. A branch that is
    # itself an expression is a real sub-question, and the closure's
    # branch-presence test takes capability names.
    if any(child[0] != "cap" for child in node[1]):
        return None
    return [strip_capability_version(child[1]) for child in node[1]]


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
    node = parse_boolean(capability)
    if node is None or node[0] != "if" or node[3] is not None:
        return None
    then, cond = node[1], node[2]
    if then[0] != "cap" or cond[0] != "cap":
        return None
    return strip_capability_version(then[1]), strip_capability_version(cond[1])




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
    node = parse_boolean(capability)
    if node is None or node[0] != "with":
        return None
    # A chain of three or more is still a range if every term names the
    # same capability -- python3-ldap writes four `with` clauses to carve
    # three bad versions out of one range, and reading only the first two
    # would be a different dependency.
    names = _leaf_names(node)
    if not names or len(set(names)) != 1:
        return None
    return names[0]


# ── Transitive closure ───────────────────────────────────────────────


def runtime_closure(roots, requires, provides, overrides=None, extra=None):
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
    # (node, required_by) for expressions whose condition has not settled.
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

    def satisfied(node, who):
        """Is this expression already true of the closure so far?

        The question asked of a *condition*, which is a different question
        from what a requirement asks.  `(A if B)` needs to know whether B
        holds, and B may itself be `(C or D)` -- redhat-rpm-config and
        systemd both write conditions that way.
        """
        kind = node[0]
        if kind == "cap":
            return capability_present(strip_capability_version(node[1]), who)
        if kind == "or":
            return any(satisfied(child, who) for child in node[1])
        if kind in ("and", "with"):
            return all(satisfied(child, who) for child in node[1])
        if kind == "without":
            return satisfied(node[1], who) and not satisfied(node[2], who)
        if kind == "if":
            if satisfied(node[2], who):
                return satisfied(node[1], who)
            return satisfied(node[3], who) if node[3] else True
        if kind == "unless":
            if satisfied(node[2], who):
                return satisfied(node[3], who) if node[3] else True
            return satisfied(node[1], who)
        return False

    def intersect(node, who):
        """Packages satisfying every leaf of a `with`, or a `without`.

        `with` means one package providing all of it, so the answer is the
        intersection of the providers -- not, as an `and` would be, one
        package per term.  The common case never gets here: when every
        term names the same capability the expression is a version range
        and parse_range_dep collapses it.  What is left is a genuine
        conjunction like `(foo with bar)`, where the intersection is the
        only honest reading.
        """
        names = _leaf_names(node) if node[0] == "with" else None
        if names is None and node[0] == "without":
            left, right = _leaf_names(node[1]), _leaf_names(node[2])
            if not left or not right:
                return None
            candidates = set(provides.get(left[0], ()))
            return candidates - set(provides.get(right[0], ()))
        if not names:
            return None
        candidates = None
        for name in names:
            group = set(provides.get(name, ()))
            candidates = group if candidates is None else (candidates & group)
        return candidates

    def require(node, who, final=False):
        """Packages this expression demands now, or None to try again later.

        `final` is the fixed point telling `unless` that its escape hatch
        never arrived.  Everything else is order-independent; `unless` is
        not, because "required unless B shows up" cannot be answered while
        B might still show up.
        """
        kind = node[0]

        if kind == "cap":
            provider = resolve(strip_capability_version(node[1]), who)
            return [] if provider is None else [provider]

        if kind == "and":
            out = []
            for child in node[1]:
                got = require(child, who, final)
                if got is None:
                    pending.append((child, who))
                else:
                    out.extend(got)
            return out

        if kind == "or":
            if any(satisfied(child, who) for child in node[1]):
                return []
            alternatives = _leaf_names(node)
            text = unparse_boolean(node)
            if alternatives is None:
                # Branches that are themselves expressions cannot be named
                # in a --override, so there is no lever to offer.
                problems.append(("rich", text, who))
                return []
            choices.append((text, alternatives, who))
            return []

        if kind in ("with", "without"):
            if kind == "with":
                collapsed = _leaf_names(node)
                if collapsed and len(set(collapsed)) == 1:
                    provider = resolve(collapsed[0], who)
                    return [] if provider is None else [provider]
            candidates = intersect(node, who)
            text = unparse_boolean(node)
            if candidates is None:
                problems.append(("rich", text, who))
                return []
            if len(candidates) == 1:
                return [next(iter(candidates))]
            if text in overrides:
                return [overrides[text]]
            problems.append((
                "unresolved",
                "{} is satisfied by {} packages: {}".format(
                    text, len(candidates), ", ".join(sorted(candidates)) or "none"
                ),
                who,
            ))
            return []

        if kind == "if":
            if satisfied(node[2], who):
                return require(node[1], who, final)
            if node[3] is not None:
                return require(node[3], who, final)
            # Condition false so far.  Not required, and not a problem --
            # (gpgverify if gnupg2) in a buildroot without gnupg2 is a
            # dependency that correctly does not apply.
            return None

        if kind == "unless":
            if satisfied(node[2], who):
                return require(node[3], who, final) if node[3] is not None else []
            return require(node[1], who, final) if final else None

        problems.append(("rich", unparse_boolean(node), who))
        return []

    def reduce_rich(base, pkg):
        """Turn one rich requirement into packages, deferring what must be."""
        node = parse_boolean(base)
        if node is None:
            # Unreadable rather than unsatisfiable.  Recorded so it is
            # never silently dropped, which is the whole contract here.
            problems.append(("rich", base, pkg))
            return []
        got = require(node, pkg)
        if got is None:
            pending.append((node, pkg))
            return []
        return got

    # Root-level expressions -- a BuildRequires that is itself boolean.
    # `roots` cannot carry them because it holds package names, and the
    # caller cannot evaluate them itself because the condition asks about
    # the very closure this function is computing.
    for cap, who in (extra or ()):
        base = strip_capability_version(cap)
        if is_rich_dep(base):
            frontier.extend(reduce_rich(base, who))
            continue
        provider = resolve(base, who)
        if provider is not None:
            frontier.append(provider)

    while True:
        while frontier:
            pkg = frontier.pop()
            if pkg in seen:
                continue
            seen.add(pkg)
            for cap in requires.get(pkg, ()):
                base = strip_capability_version(cap)
                if is_rich_dep(base):
                    for provider in reduce_rich(base, pkg):
                        if provider not in seen:
                            frontier.append(provider)
                    continue
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
        for node, who in pending:
            got = require(node, who)
            if got is None:
                still_pending.append((node, who))
                continue
            for provider in got:
                if provider not in seen:
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
            # Nothing positive can fire any more, so `unless` has run out
            # of chances to be let off.  Settled here rather than in the
            # loop above because "required unless B appears" cannot be
            # answered while B might still appear -- firing it early would
            # pull in a package a later round would have excused.
            still_pending = []
            for node, who in pending:
                got = require(node, who, final=True)
                if got is None:
                    still_pending.append((node, who))
                    continue
                for provider in got:
                    if provider not in seen:
                        frontier.append(provider)
            pending = still_pending

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
