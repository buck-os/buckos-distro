#!/usr/bin/env python3
"""What this build host can do, and which packages need it.

    buck2 run //tools:hostcheck

A source build reaches outside the sandbox in ways that are easy to miss.
The sandbox controls the *filesystem* a package builds in, and pins every
byte of it -- but a spec can still call a tool that talks to the running
kernel, and no amount of pinning changes which kernel that is.  When the
answer is "this one cannot", the build finds out at the point of use,
which for a kernel package is an hour of compiling later, in %install,
with a message about HMAC files.

So the checks live here instead, and each one names the packages it
decides.  Two properties matter:

  * A capability is probed by *doing the thing*, not by reading a version
    or a config file.  /proc/config.gz is frequently absent, and a kernel
    that claims a feature and refuses it is exactly the case worth
    catching.

  * A missing capability produces configuration, not a failure.  The
    packages that need it fall back to the pinned upstream binary, the
    build completes, and what was lost is stated rather than discovered.
    That is the honest trade: this repo exists to build from source, and a
    host that cannot build one package should still produce an image.

Nothing here runs in the build graph.  It writes advice for
.buckconfig.local, which is a human's file, and the committed
configuration assumes a capable host -- so a clone on a normal machine
builds everything from source without consulting this at all.
"""

import argparse
import os
import shutil
import socket
import subprocess
import sys

# Linux's netlink protocol number for the crypto user API, from
# include/uapi/linux/netlink.h.  Hardcoded because it is ABI: the value
# cannot change without breaking every binary that already uses it.
NETLINK_CRYPTO = 21


def _probe_netlink_crypto():
    """Can a process open the kernel's crypto configuration API?

    libkcapi's hashers -- sha512hmac, fipshmac, kcapi-hasher -- open this
    to ask the kernel about an algorithm before hashing with it.  A kernel
    built without CONFIG_CRYPTO_USER answers EPROTONOSUPPORT, and the tool
    reports it as

        Allocation of hmac(sha256) cipher failed (ret=-93)

    which names neither netlink nor the missing option.  93 is the errno.
    """
    try:
        socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, NETLINK_CRYPTO).close()
    except OSError as exc:
        return False, "errno {} ({})".format(exc.errno, exc.strerror)
    return True, "available"


def _probe_af_alg():
    """Can a process hash through the kernel's crypto socket?

    Distinct from the check above and worth keeping separate: AF_ALG is
    what actually computes the digest, NETLINK_CRYPTO only describes the
    algorithm.  A host can have one without the other, and this repo has
    seen exactly that -- which is why the first diagnosis of the hmac
    failures blamed AF_ALG and was wrong.
    """
    try:
        sock = socket.socket(socket.AF_ALG, socket.SOCK_SEQPACKET, 0)
        sock.bind(("hash", "hmac(sha256)"))
        sock.close()
    except OSError as exc:
        return False, "errno {} ({})".format(exc.errno, exc.strerror)
    return True, "available"


def _probe_user_namespaces():
    """Can this user create a namespace and map more than one id into it?

    Without a subordinate range rpm cannot chown a payload file to any id
    but root, so a package shipping one -- `filesystem` ships
    /var/spool/mail as root:mail -- fails partway through unpacking.
    """
    if not shutil.which("unshare"):
        return False, "unshare(1) is not installed"
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        from _isolation import subid_mapping_available
    except ImportError:
        return False, "cannot import _isolation"
    if not subid_mapping_available():
        return False, "no subordinate id range, or no newuidmap/newgidmap"
    return True, "available"


def _probe_tool(name):
    def probe():
        path = shutil.which(name)
        return (True, path) if path else (False, "not on PATH")
    return probe


# What the build needs from the host, and who needs it.
#
# `prebuilt` names source packages that cannot be built at all without the
# capability -- their specs call the tool unconditionally, so there is no
# switch to turn off and the only option is the pinned upstream binary.
#
# `bcond` names packages that *can* be built without it, because their
# spec guards the tool behind a %bcond.  Turning the bcond off is strictly
# better than falling back to a prebuilt binary: the package is still
# built from source, and only the guarded feature is missing.
CAPABILITIES = [
    {
        "name": "netlink-crypto",
        "summary": "kernel crypto user API (CONFIG_CRYPTO_USER)",
        "probe": _probe_netlink_crypto,
        "prebuilt": ["kernel", "libxcrypt"],
        "bcond": {"gmp": "fips", "nettle": "fipshmac"},
        "why": (
            "libkcapi's sha512hmac and fipshmac open a NETLINK_CRYPTO "
            "socket to look up an algorithm.  kernel.spec calls "
            "sha512hmac in %install to sign vmlinuz for FIPS, and "
            "libxcrypt calls fipshmac from %__spec_install_post; neither "
            "is guarded by a bcond.  gmp and nettle guard theirs."
        ),
    },
    {
        "name": "af-alg",
        "summary": "kernel crypto sockets (CONFIG_CRYPTO_USER_API_HASH)",
        "probe": _probe_af_alg,
        "prebuilt": [],
        "bcond": {},
        "why": "The digest itself is computed through an AF_ALG socket.",
    },
    {
        "name": "user-namespaces",
        "summary": "unprivileged user namespaces with a subordinate id range",
        "probe": _probe_user_namespaces,
        "prebuilt": [],
        "bcond": {},
        "why": (
            "Every build stage runs in one, and rpm needs more than a "
            "single mapped id to chown payload files.  Without this "
            "nothing builds; there is no fallback to offer."
        ),
    },
]

CAPABILITIES += [
    {
        "name": "tool:" + tool,
        "summary": tool,
        "probe": _probe_tool(tool),
        "prebuilt": [],
        "bcond": {},
        "why": why,
    }
    for tool, why in (
        ("rpm2archive", "unpacks every rpm payload"),
        ("rpmbuild", "runs the spec replay"),
        ("rpmspec", "answers the static BuildRequires query"),
        ("tar", "extracts payloads, and nests names buck2 cannot address"),
    )
]


def run_checks():
    """Probe everything.  Returns a list of (capability, ok, detail)."""
    results = []
    for cap in CAPABILITIES:
        ok, detail = cap["probe"]()
        results.append((cap, ok, detail))
    return results


def advice(results, flavor="fedora"):
    """The .buckconfig.local lines a host with gaps should carry."""
    prebuilt, bconds = [], []
    for cap, ok, _ in results:
        if ok:
            continue
        prebuilt.extend(cap["prebuilt"])
        bconds.extend(
            "{}:{}".format(pkg, bcond)
            for pkg, bcond in sorted(cap["bcond"].items())
        )
    lines = []
    if prebuilt or bconds:
        lines.append("[buckos.{}]".format(flavor))
    if bconds:
        lines.append("  without = " + ", ".join(sorted(bconds)))
    if prebuilt:
        lines.append("  prebuilt = " + ", ".join(sorted(set(prebuilt))))
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--flavor", default="fedora")
    ap.add_argument("--quiet", action="store_true",
                    help="print only the configuration, for appending to "
                         ".buckconfig.local")
    args = ap.parse_args(argv)

    results = run_checks()
    lines = advice(results, args.flavor)

    if not args.quiet:
        for cap, ok, detail in results:
            print("{:4} {:22} {:52} {}".format(
                "ok" if ok else "MISS", cap["name"], cap["summary"], detail))
        gaps = [(c, d) for c, ok, d in results if not ok]
        print()
        if not gaps:
            print("This host can build every package from source.")
            return 0
        for cap, _ in gaps:
            print("{}: {}".format(cap["name"], cap["why"]))
            if cap["bcond"]:
                print("  buildable with a feature disabled: {}".format(
                    ", ".join(sorted(cap["bcond"]))))
            if cap["prebuilt"]:
                print("  not buildable here, use the pinned binary: {}".format(
                    ", ".join(sorted(cap["prebuilt"]))))
            if not cap["bcond"] and not cap["prebuilt"]:
                print("  no fallback: this one is required.")
            print()

    for line in lines:
        print(line)
    # Non-zero only when something has no fallback, so this is usable as a
    # gate in CI without failing every host that merely needs a prebuilt.
    fatal = [c for c, ok, _ in results
             if not ok and not c["prebuilt"] and not c["bcond"]]
    return 1 if fatal else 0


if __name__ == "__main__":
    sys.exit(main())
