#!/usr/bin/env python3
"""Test-only implementation of the public RPM signer contract."""

import argparse


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--key-id", required=True)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    sign = subparsers.add_parser("rpm-sign")
    sign.add_argument("--in", dest="source", required=True)
    sign.add_argument("--out", required=True)
    sign.add_argument("--package-name", required=True)
    args = parser.parse_args()

    with open(args.source, "rb") as source:
        payload = source.read()
    marker = "\nSIGNED {} {}\n".format(args.key_id, args.package_name).encode()
    with open(args.out, "wb") as output:
        output.write(payload + marker)


if __name__ == "__main__":
    main()
