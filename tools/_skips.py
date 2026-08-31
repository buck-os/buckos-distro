#!/usr/bin/env python3
"""Turn an environment-driven skip into a failure when coverage is required.

A skipped test reports as a pass.  `unittest` prints `OK (skipped=4)`, and
the layer above it -- buck2 -- counts targets rather than cases, so the
summary says `Skip 0` and a reviewer records the run as green.  The
information is there, four levels down in one target's stderr, and nobody
reads 599 cases of stderr.

That is not hypothetical.  The only mTLS handshake coverage in this tree
sits behind two independent environment gates, one for the `openssl` binary
and one for the `grpcio` module.  On a machine missing both, all four cases
skipped and the target printed a tick.  Installing `openssl` alone moved the
skip reason from one gate to the other and the target still printed a tick.
The gap was invisible from every summary anyone actually looks at.

The fix is not to make every environment gate a hard failure.  A gate means
"this machine is not provisioned", which is a true and useful thing to say
to someone running the suite on a laptop, and failing there would make the
tree hostile for no safety gain.  What matters is that a run being recorded
as an acceptance reference, or gating a review, must not be able to conceal
one.

So the escalation is opt-in and belongs to the runner rather than to the
test: set BUCKOS_REQUIRE_FULL_COVERAGE=1 and every gate that uses this
module fails instead of skipping.  Unset, behaviour is unchanged.

Two other kinds of skip deliberately do not route through here, because
neither can hide a provisioning gap:

  - a deliberate opt-in, whose prerequisite is an artifact nobody has and
    which is meant to stay skipped;
  - a platform guard for a CPU architecture this project does not target,
    which cannot fire on any machine it runs on.

`tools/skip_contract_test.py` holds the inventory of all three and fails if
a site appears, disappears, or is classified as environmental without using
this module.

## What a failure looks like, which depends on the gate's shape

A gate expressed as a decorator names every case it cost, because the
escalation replaces the test methods individually.  A gate that can only be
evaluated once the class is being set up -- an import that may raise, say --
has to raise from `setUpClass`, and unittest reports that as one class-level
error rather than as a failure per case, because a `setUpClass` error
preempts the methods entirely.

Both fail loudly and both name the missing prerequisite, so neither can be
mistaken for a pass.  The distinction is reporting granularity, not safety,
and it is a property of where the gate can be evaluated rather than
something a caller chooses.  Restructuring a dynamic gate into a decorator
to gain the granularity means moving its side effect to module import time,
which is a worse trade than the coarser report.

One consequence is easy to misread.  When more than one gate on the same
class is unmet, the decorator escalates first and neutralizes the class
setup, so the later gate never runs and is never mentioned.  The report then
names only the first missing prerequisite; the absence of the others from
that output says nothing about whether they are satisfied.  Fix what is
named, run again, and the next one appears.
"""

import functools
import os
import unittest


REQUIRE_FULL_COVERAGE_ENV = "BUCKOS_REQUIRE_FULL_COVERAGE"

# Only the test-method prefix unittest itself defaults to.  A suite that
# renamed it would fall through to the setUpClass fallback below rather
# than silently pass, which is the safe direction.
_TEST_METHOD_PREFIX = "test"


def full_coverage_required():
    """Is this run being recorded as an acceptance reference or a gate?"""
    return os.environ.get(REQUIRE_FULL_COVERAGE_ENV) == "1"


def _message(reason):
    return (
        "{} is set, so an environment-driven skip is a failure: {}. "
        "Provision the missing prerequisite on this machine, or unset {} "
        "to let the case skip again.".format(
            REQUIRE_FULL_COVERAGE_ENV, reason, REQUIRE_FULL_COVERAGE_ENV
        )
    )


def environmental_skip(reason):
    """Skip, or fail under full coverage.  Never returns.

    The dynamic form, for a gate that can only be evaluated while the test
    is running -- an import that may raise, say.  Call it in place of
    `raise unittest.SkipTest(reason)`.
    """
    if full_coverage_required():
        raise AssertionError(_message(reason))
    raise unittest.SkipTest(reason)


def _failing_function(function, message):
    @functools.wraps(function)
    def replacement(*_args, **_kwargs):
        raise AssertionError(message)

    return replacement


def _fail_instead_of_skipping(reason):
    message = _message(reason)

    def decorate(target):
        if not isinstance(target, type):
            return _failing_function(target, message)

        # Replace each test method rather than the class setup, so the
        # report names every case the missing prerequisite cost rather
        # than collapsing them into one setUpClass error.  dir() rather
        # than vars() so an inherited case is covered too; assigning on
        # the subclass shadows the base without touching it.
        replaced = 0
        for name in dir(target):
            if not name.startswith(_TEST_METHOD_PREFIX):
                continue
            attribute = getattr(target, name, None)
            if not callable(attribute):
                continue
            setattr(target, name, _failing_function(attribute, message))
            replaced += 1

        if replaced:
            # Stop the class setup from running.  This gate already said
            # the machine cannot support the class, so its setUpClass will
            # usually fail too -- and a setUpClass error preempts the test
            # methods, collapsing every case into one error that names
            # whichever prerequisite happened to be checked second.  The
            # point of replacing the methods was to name all of them.
            target.setUpClass = classmethod(lambda _cls: None)
        else:
            # No recognisable case to fail.  Silence here would be the
            # exact failure this module exists to prevent, so fail the
            # class setup instead of returning it untouched.
            def setUpClass(_cls):
                raise AssertionError(message)

            target.setUpClass = classmethod(setUpClass)
        return target

    return decorate


def environmental_skip_unless(condition, reason):
    """Decorate a case or a class: skip when false, fail under full coverage.

    Drop-in for `unittest.skipUnless` at a gate that describes the machine
    rather than a deliberate choice.
    """
    if condition:
        return lambda target: target
    if not full_coverage_required():
        return unittest.skip(reason)
    return _fail_instead_of_skipping(reason)
