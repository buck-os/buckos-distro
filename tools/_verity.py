#!/usr/bin/env python3
"""Deterministic dm-verity format helpers shared by image tools.

The emitted tree is the kernel's version-1, no-superblock format.  Hash
levels are stored from root to leaves, while each level is calculated from
the leaves upward.  Keeping this implementation independent of veritysetup
makes the image transform host-independent and remote-execution safe.
"""

import hashlib
import json
import os


SCHEMA = "buckos.dm-verity.v1"
ALGORITHMS = ("sha256", "sha512")
CORRUPTION_POLICIES = ("eio", "panic", "restart")


def _digest(algorithm, salt, block):
    hasher = hashlib.new(algorithm)
    # dm-verity format version 1 prepends the salt.
    hasher.update(salt)
    hasher.update(block)
    return hasher.digest()


def _validate_block_size(value, name):
    if value < 512 or value > 65536 or value & (value - 1):
        raise ValueError("{} must be a power of two from 512 through 65536".format(name))


def validate_parameters(algorithm, data_block_size, hash_block_size, salt):
    if algorithm not in ALGORITHMS:
        raise ValueError("unsupported dm-verity hash algorithm {!r}".format(algorithm))
    _validate_block_size(data_block_size, "data block size")
    _validate_block_size(hash_block_size, "hash block size")
    digest_size = hashlib.new(algorithm).digest_size
    hashes_per_block = hash_block_size // digest_size
    if not hashes_per_block or hashes_per_block & (hashes_per_block - 1):
        raise ValueError(
            "hash block size divided by digest size must be a power of two"
        )
    if not salt:
        raise ValueError("dm-verity salt must not be empty")
    if len(salt) > 256:
        raise ValueError("dm-verity salt exceeds the kernel's 256-byte limit")
    return hashes_per_block


def parse_salt(value):
    if value == "auto":
        return None
    try:
        salt = bytes.fromhex(value)
    except ValueError as error:
        raise ValueError("salt must be 'auto' or an even-length hexadecimal string") from error
    if not salt:
        raise ValueError("explicit dm-verity salt must not be empty")
    return salt


def _copy_and_measure(source, destination, data_block_size):
    content = hashlib.sha256()
    size = 0
    with open(source, "rb") as input_stream, open(destination, "wb") as output_stream:
        for chunk in iter(lambda: input_stream.read(1 << 20), b""):
            content.update(chunk)
            output_stream.write(chunk)
            size += len(chunk)
        if not size:
            raise ValueError("dm-verity data image is empty")
        padding = (-size) % data_block_size
        if padding:
            output_stream.write(b"\0" * padding)
    return size, content.digest()


def _hash_data(path, algorithm, salt, block_size):
    digests = []
    with open(path, "rb") as stream:
        while True:
            block = stream.read(block_size)
            if not block:
                break
            if len(block) != block_size:
                raise ValueError("padded dm-verity image ends in a partial block")
            digests.append(_digest(algorithm, salt, block))
    return digests


def _level(digests, algorithm, salt, hash_block_size, hashes_per_block):
    blocks = []
    for offset in range(0, len(digests), hashes_per_block):
        block = b"".join(digests[offset:offset + hashes_per_block])
        blocks.append(block.ljust(hash_block_size, b"\0"))
    return blocks


def build_hash_tree(data_path, hash_path, algorithm, salt,
                    data_block_size, hash_block_size):
    """Write a version-1 tree and return (root hash, block count, tree size)."""
    hashes_per_block = validate_parameters(
        algorithm, data_block_size, hash_block_size, salt
    )
    digests = _hash_data(data_path, algorithm, salt, data_block_size)
    data_blocks = len(digests)
    if not data_blocks:
        raise ValueError("dm-verity data image is empty")

    # A one-block device has no on-disk tree: its data digest is the root.
    # This is observable in veritysetup as "Hash blocks: 0" and is required
    # for interoperability with the kernel's level calculation.
    if data_blocks == 1:
        with open(hash_path, "wb"):
            pass
        return digests[0], data_blocks, 0

    # Kernel offsets put the highest level first.  Calculation naturally
    # proceeds in the other direction, so retain levels and reverse on write.
    levels = []
    while True:
        blocks = _level(
            digests, algorithm, salt, hash_block_size, hashes_per_block
        )
        levels.append(blocks)
        if len(blocks) == 1:
            root_hash = _digest(algorithm, salt, blocks[0])
            break
        digests = [_digest(algorithm, salt, block) for block in blocks]

    with open(hash_path, "wb") as stream:
        for blocks in reversed(levels):
            for block in blocks:
                stream.write(block)
    return root_hash, data_blocks, sum(len(level) for level in levels) * hash_block_size


def format_image(source, data_path, hash_path, metadata_path, root_hash_path,
                 algorithm="sha256", salt_value="auto", data_block_size=4096,
                 hash_block_size=4096, corruption_policy="eio"):
    explicit_salt = parse_salt(salt_value)
    original_size, content_digest = _copy_and_measure(
        source, data_path, data_block_size
    )
    # A salt is public, not a key.  Deriving it from the complete input gives
    # each image its own deterministic domain without making builds random.
    salt = explicit_salt
    if salt is None:
        salt = hashlib.sha256(b"buckos.dm-verity.v1\0" + content_digest).digest()
    if corruption_policy not in CORRUPTION_POLICIES:
        raise ValueError("unsupported corruption policy {!r}".format(corruption_policy))

    root_hash, data_blocks, tree_size = build_hash_tree(
        data_path,
        hash_path,
        algorithm,
        salt,
        data_block_size,
        hash_block_size,
    )
    metadata = {
        "algorithm": algorithm,
        "corruption_policy": corruption_policy,
        "data_block_size": data_block_size,
        "data_blocks": data_blocks,
        "data_size": original_size,
        "hash_block_size": hash_block_size,
        "hash_start_block": 0,
        "hash_tree_size": tree_size,
        "padded_data_size": os.path.getsize(data_path),
        "root_hash": root_hash.hex(),
        "salt": salt.hex(),
        "schema": SCHEMA,
        "version": 1,
    }
    with open(metadata_path, "w", encoding="utf-8") as stream:
        json.dump(metadata, stream, indent=2, sort_keys=True)
        stream.write("\n")
    with open(root_hash_path, "w", encoding="ascii") as stream:
        stream.write(root_hash.hex())
    return metadata


def load_metadata(path):
    with open(path, encoding="utf-8") as stream:
        metadata = json.load(stream)
    if metadata.get("schema") != SCHEMA:
        raise ValueError("{} is not {} metadata".format(path, SCHEMA))
    required = {
        "algorithm", "corruption_policy", "data_block_size", "data_blocks",
        "hash_block_size", "hash_start_block", "root_hash", "salt", "version",
    }
    missing = sorted(required - set(metadata))
    if missing:
        raise ValueError("dm-verity metadata is missing {}".format(", ".join(missing)))
    if metadata["version"] != 1 or metadata["hash_start_block"] != 0:
        raise ValueError("only version-1, no-superblock dm-verity metadata is supported")
    try:
        salt = bytes.fromhex(metadata["salt"])
        root_hash = bytes.fromhex(metadata["root_hash"])
    except (TypeError, ValueError) as error:
        raise ValueError("dm-verity metadata contains non-hexadecimal hash data") from error
    validate_parameters(
        metadata["algorithm"],
        metadata["data_block_size"],
        metadata["hash_block_size"],
        salt,
    )
    if len(root_hash) != hashlib.new(metadata["algorithm"]).digest_size:
        raise ValueError("dm-verity root hash has the wrong length")
    if metadata["data_blocks"] <= 0:
        raise ValueError("dm-verity data block count must be positive")
    if metadata["corruption_policy"] not in CORRUPTION_POLICIES:
        raise ValueError("unsupported dm-verity corruption policy")
    return metadata


def kernel_arguments(metadata, media, data_path, hash_path, mapper="buckos-root"):
    """Return arguments consumed by the BuckOS early-boot verity hook."""
    policy = metadata["corruption_policy"]
    return [
        "rd.buckos.verity=1",
        "rd.buckos.verity.media={}".format(media),
        "rd.buckos.verity.data={}".format(data_path.lstrip("/")),
        "rd.buckos.verity.hash={}".format(hash_path.lstrip("/")),
        "rd.buckos.verity.root_hash={}".format(metadata["root_hash"]),
        "rd.buckos.verity.salt={}".format(metadata["salt"]),
        "rd.buckos.verity.algorithm={}".format(metadata["algorithm"]),
        "rd.buckos.verity.data_blocks={}".format(metadata["data_blocks"]),
        "rd.buckos.verity.data_block_size={}".format(metadata["data_block_size"]),
        "rd.buckos.verity.hash_block_size={}".format(metadata["hash_block_size"]),
        "rd.buckos.verity.hash_start_block={}".format(metadata["hash_start_block"]),
        "rd.buckos.verity.corruption={}".format(policy),
        "rd.buckos.verity.mapper={}".format(mapper),
    ]
