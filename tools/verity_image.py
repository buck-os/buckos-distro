#!/usr/bin/env python3
"""Create deterministic version-1 dm-verity data and hash-tree artifacts."""

import argparse
import sys

from _verity import ALGORITHMS, CORRUPTION_POLICIES, format_image


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--out-data", required=True)
    parser.add_argument("--out-hash", required=True)
    parser.add_argument("--out-metadata", required=True)
    parser.add_argument("--out-root-hash", required=True)
    parser.add_argument("--algorithm", choices=ALGORITHMS, default="sha256")
    parser.add_argument("--salt", default="auto")
    parser.add_argument("--data-block-size", type=int, default=4096)
    parser.add_argument("--hash-block-size", type=int, default=4096)
    parser.add_argument(
        "--corruption-policy", choices=CORRUPTION_POLICIES, default="eio"
    )
    args = parser.parse_args(argv)
    try:
        format_image(
            args.image,
            args.out_data,
            args.out_hash,
            args.out_metadata,
            args.out_root_hash,
            algorithm=args.algorithm,
            salt_value=args.salt,
            data_block_size=args.data_block_size,
            hash_block_size=args.hash_block_size,
            corruption_policy=args.corruption_policy,
        )
    except (OSError, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
