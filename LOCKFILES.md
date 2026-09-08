# Lockfile workflow

The checked-in lockfiles are the reviewable boundary between upstream package
metadata and the Buck graph. Use the repository's `lockfiles` command to work with them;
it accepts plain `.lock.json` and compressed `.lock.json.gz` files without
making callers care which representation is present.

Lockfiles are selected by `FLAVOR[:RELEASE[:ARCH]]`. A selector can name one
lock (`fedora:45:x86_64`), both architectures of a release (`fedora:45`), or
an entire flavor (`fedora`). Commands that must operate on exactly one lock
reject broader selectors.

## Inspect

List every lock with its storage format, size, schema, and source count:

```sh
./lockfiles list
./lockfiles list fedora:45
```

Resolve a selector to its current filename. This is useful in scripts because
the result remains correct if the repository changes compression formats:

```sh
./lockfiles path fedora:45:x86_64
```

Print decoded JSON, or read one value using an
[RFC 6901](https://www.rfc-editor.org/rfc/rfc6901) JSON pointer:

```sh
./lockfiles show fedora:45:x86_64 | jq '.solve.problems'
./lockfiles get fedora:45:x86_64 /solve/build
./lockfiles get ubuntu:26.04:aarch64 /source_policy/summary/live
```

`show` and `get` write only JSON to stdout. Buck diagnostics go to stderr, so
their output can be safely piped into another program.

## Validate

Run the repository-wide check before sending a lockfile change for review:

```sh
./lockfiles check
```

The check verifies that:

- every filename agrees with the flavor, release, and architecture in JSON;
- no identity exists in both plain and compressed form;
- JSON formatting and gzip headers are deterministic;
- every stored file remains below the 5,000,000-byte import limit; and
- the corresponding generated Starlark and flavor index are byte-for-byte
  current.

Pass selectors to check a smaller set while iterating. `--no-generated` skips
only the Starlark freshness check:

```sh
./lockfiles check centos:10
./lockfiles check fedora:45:x86_64 --no-generated
```

## Update and regenerate

Prefer the distro solver or relock command when changing package selections.
Those commands preserve the existing lockfile encoding. Fedora has a dedicated
refresh loop:

```sh
buck2 run //tools:relock -- --release 45 --arch x86_64
```

The other RPM-family and Debian-family commands are documented in each
flavor's README. After a lock-only change, regenerate one release, one flavor,
or everything explicitly:

```sh
./lockfiles generate fedora:45:x86_64
./lockfiles generate centos:10
./lockfiles generate --all
```

For a deliberate hand edit, `edit` presents a temporary plain JSON file to
`$VISUAL` or `$EDITOR`, validates the result, rewrites the original encoding
atomically, and regenerates the matching Starlark:

```sh
EDITOR=vim ./lockfiles edit fedora:45:x86_64
```

Automation can make the same guarded update without handling gzip itself.
`replace` reads decoded JSON from a file or stdin, checks its identity, schema,
source policy, and final stored size, then atomically replaces the selected
lock and regenerates its Starlark:

```sh
./lockfiles show fedora:45:x86_64 \
  | jq '.solve.overrides["example"] = "provider"' \
  | ./lockfiles replace fedora:45:x86_64
```

Use `--no-generate` only when a later command in the same workflow will
regenerate the data.

## Compression

The default storage format comes from Buck configuration:

```ini
[buckos.lockfiles]
  compression = gzip
```

Set `compression = none` in `.buckconfig.local` for a local plain-JSON
workflow. Existing locks retain their current format until converted. Convert
selected files to the configured format, or override it for one invocation:

```sh
./lockfiles convert fedora:45
./lockfiles convert fedora:45 --compression none
./lockfiles convert --all --compression gzip
```

Conversion is atomic and regenerates Starlark because its generated header
records the lockfile path. Mutating commands require a selector or an explicit
`--all`; an omitted selector can never rewrite the whole matrix accidentally.
Plain files may exceed the downstream import limit, so convert them back to
gzip and run `./lockfiles check` before committing.

## Automation contract

Humans and automated agents should preserve these invariants:

1. Address locks through selectors or `path`; do not assume a compression
   suffix.
2. Treat lockfiles as generated package pins. Change solver inputs or recorded
   policy intentionally and keep unrelated pins untouched.
3. Never keep `.lock.json` and `.lock.json.gz` for the same identity.
4. Use `replace`, `edit`, or `convert` instead of invoking `gzip` directly.
5. Regenerate checked-in Starlark and finish with `lockfiles check`.
6. Review both the lock diff and generated-data diff before committing.

If a lock changes package or source pins, audit the Manifold mirror afterward:

```sh
python3 local_scripts/hydrate_manifold.py --check
python3 local_scripts/hydrate_manifold.py --only source --check
```

Run the same commands without `--check` to upload missing content. Existing
objects are detected and skipped; cleanup is handled separately.
