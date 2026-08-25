#!/usr/bin/env python3
"""rpm's version comparison, for picking the newer of two candidates.

Needed because merging `updates/` over `releases/` means deciding which of
two builds of the same package is newer, and that decision is not string
comparison. Lexicographically `1.10` < `1.9` and `1.0` < `1.0~rc1`, and
both are backwards. Getting it wrong does not fail loudly: it pins an older
build than intended, which looks like a normal lockfile and quietly means
the security update everyone thinks is applied is not.

This is a transcription of rpmvercmp() from rpm's lib/rpmvercmp.c rather
than an interpretation of it, because the rules are genuinely odd and the
odd parts are the ones that matter:

  * Non-alphanumerics are separators and carry no weight of their own, so
    `1.2` and `1_2` and `1..2` all compare equal.
  * A digit run beats an alpha run: `1.2` > `1.a`.
  * Digit runs compare with leading zeros stripped and then by length
    before content, so `1.010` == `1.10` and `1.100` > `1.99`.
  * `~` sorts *before* anything, including the end of the string. That is
    what makes `1.0~rc1` < `1.0` and is how pre-releases are expressed.
  * `^` sorts *after* the end of the string but before a new segment, so
    `1.0^git1` > `1.0` and `1.0^git1` < `1.0.1`. Used for post-release
    snapshots.

Comparing whole EVRs is compare_evr(); rpmvercmp() alone is the segment
engine and is exported mostly so it can be tested against rpm's own cases.
"""


def _is_digit(char):
    return "0" <= char <= "9"


def _is_alpha(char):
    return ("a" <= char <= "z") or ("A" <= char <= "Z")


def _is_alnum(char):
    return _is_digit(char) or _is_alpha(char)


def rpmvercmp(one, two):
    """Compare two version strings. Returns -1, 0 or 1.

    A faithful port, including the early exact-match exit: that shortcut is
    not just an optimisation, it is what makes two equal strings compare
    equal even when they consist entirely of separators.
    """
    if one == two:
        return 0

    i, j = 0, 0
    len_one, len_two = len(one), len(two)

    while i < len_one or j < len_two:
        # Separators have no value of their own; skip to the next thing
        # that does. `~` and `^` are not separators -- they are operators.
        while i < len_one and not _is_alnum(one[i]) and one[i] not in "~^":
            i += 1
        while j < len_two and not _is_alnum(two[j]) and two[j] not in "~^":
            j += 1

        # Tilde sorts before everything, the end of the string included,
        # so a side that has one here is older regardless of what follows.
        one_tilde = i < len_one and one[i] == "~"
        two_tilde = j < len_two and two[j] == "~"
        if one_tilde or two_tilde:
            if not one_tilde:
                return 1
            if not two_tilde:
                return -1
            i += 1
            j += 1
            continue

        # Caret is the mirror image: after the end of the string, but
        # before any real segment. Hence the exhaustion checks come first
        # here and did not for `~`.
        one_caret = i < len_one and one[i] == "^"
        two_caret = j < len_two and two[j] == "^"
        if one_caret or two_caret:
            if i >= len_one:
                return -1
            if j >= len_two:
                return 1
            if not one_caret:
                return 1
            if not two_caret:
                return -1
            i += 1
            j += 1
            continue

        # One side ran out of segments entirely; settled after the loop.
        if i >= len_one or j >= len_two:
            break

        # Take a maximal run of one kind. Which kind is decided by the
        # left side alone -- that asymmetry is deliberate, and is how a
        # numeric run comes to outrank an alpha one below.
        start_one, start_two = i, j
        if _is_digit(one[i]):
            while i < len_one and _is_digit(one[i]):
                i += 1
            while j < len_two and _is_digit(two[j]):
                j += 1
            numeric = True
        else:
            while i < len_one and _is_alpha(one[i]):
                i += 1
            while j < len_two and _is_alpha(two[j]):
                j += 1
            numeric = False

        seg_one = one[start_one:i]
        seg_two = two[start_two:j]

        # The right side had nothing of the kind the left side led with.
        # If the left side was numeric it wins; if it was alpha then the
        # right side is numeric here, and numeric wins.
        if not seg_two:
            return 1 if numeric else -1

        if numeric:
            # Leading zeros are noise, so `010` and `10` are the same
            # number. After stripping, a longer digit run is a bigger
            # number -- which is the whole reason 1.10 > 1.9.
            seg_one = seg_one.lstrip("0")
            seg_two = seg_two.lstrip("0")
            if len(seg_one) != len(seg_two):
                return 1 if len(seg_one) > len(seg_two) else -1

        if seg_one != seg_two:
            return 1 if seg_one > seg_two else -1

    # Both exhausted together means equal; otherwise whoever still has
    # segments left is the newer one.
    if i >= len_one and j >= len_two:
        return 0
    return -1 if i >= len_one else 1


def _epoch_int(epoch):
    """An absent, empty or non-numeric epoch is 0, the way rpm treats it."""
    if epoch in (None, "", "(none)"):
        return 0
    try:
        return int(epoch)
    except (TypeError, ValueError):
        return 0


def compare_evr(one, two):
    """Compare (epoch, version, release) triples. Returns -1, 0 or 1.

    Epoch first and numerically, because it exists precisely to overrule a
    version comparison that would otherwise go the wrong way.
    """
    epoch_one, epoch_two = _epoch_int(one[0]), _epoch_int(two[0])
    if epoch_one != epoch_two:
        return 1 if epoch_one > epoch_two else -1

    result = rpmvercmp(one[1] or "", two[1] or "")
    if result:
        return result

    # Release is compared the same way, not skipped: `-2.fc43` over
    # `-1.fc43` is the single most common shape an update takes.
    return rpmvercmp(one[2] or "", two[2] or "")


def package_is_newer(candidate, incumbent):
    """Whether `candidate` should replace `incumbent` in a merged index.

    Takes the repodata dicts the solver already has, so callers do not
    each rebuild the triple and risk disagreeing about the epoch default.
    """
    return compare_evr(
        (candidate.get("epoch"), candidate.get("version"),
         candidate.get("release")),
        (incumbent.get("epoch"), incumbent.get("version"),
         incumbent.get("release")),
    ) > 0
