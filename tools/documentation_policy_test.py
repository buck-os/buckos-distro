#!/usr/bin/env python3

import hashlib
import os
import unittest


PRIVATE_TEXT_HASHES = {
    6: frozenset({
        "5563332005bfc552d1a5e0e13c8409061802a2b41a03b47c55b5c25165890210",
        "dbbeb599bcdaed976aa7022e94e82754f639dc9b2e2f85f039137605b2009d8c",
    }),
    8: frozenset({
        "1f517a23d11ba7cff0b65d842a9ecaf180406b3ff7fd71acfddde00edf88e361",
        "39997082fea9d2dc08a88a15cfab9d0dbb0a5b82b3fd687897bed7eb1803e4b9",
        "4bc577a8881410d8f3146842eab7b9ef881ba8a80e0e82f79f61e7252ed218ad",
        "d0a515f98c35d835394b0c6ed87fccd4ce0c12f4a45c4ee56fce7a9e1934b392",
    }),
    11: frozenset({
        "943d8ab138420eb02a6af9bb0edb7ccea0a70e6959133fbdc057a3c8ec20b23d",
    }),
    12: frozenset({
        "57e7bf91b7aea4bc85d47afe276744cd623d957e7ce1a41e4cc914653a9e8c07",
    }),
    14: frozenset({
        "8314ab1146a536d8eb30b2fabf4d715f441e0c96144e03059354001bba36aac6",
    }),
    15: frozenset({
        "5c69398ee3cd63d9a61f29702043e793edf11e76cd0a26c051078b71b4dd6910",
    }),
    16: frozenset({
        "0fe3371f7095b667f0f7d9d3577a32e88030291c803b35759bf8820311ba9f55",
        "b0da8b4bf824f0c2042b03f7622295a09dc0c25faa38c0b604fb40e43c2d8108",
    }),
    17: frozenset({
        "2fd101e9abe1629803f24ce104b8457e99745fd508b8c28acb15b64338575920",
        "667e5993a2d4ab30560d6667aaccc1ade0237a517464a7db5e9944d39419b729",
        "8d7783225a59cbfcb45ffec4a92114ca8e3210a8e263fad7e73e32bc111866b0",
        "c0e61d81e6bc544f4085791bb946d5da657f09311ff7a0ef82ebc69b2b42ac65",
    }),
    18: frozenset({
        "b913443bbcdd9ab6c81a9f2da351f2b5f55d0a4179df61a1e5f1f8ebd4d5dfb8",
    }),
    21: frozenset({
        "db2732ee99b417998bacf86eea2ea696c8ccab45e9e765e72c68051e08feef9c",
    }),
}


def repo_root():
    for start in (os.getcwd(), os.path.dirname(os.path.abspath(__file__))):
        path = start
        while True:
            if os.path.isfile(os.path.join(path, ".buckroot")):
                return path
            parent = os.path.dirname(path)
            if parent == path:
                break
            path = parent
    raise AssertionError("cannot locate repository root")


def documentation_paths(root):
    excluded = {".git", "buck-out", "dev", "prelude"}
    for directory, names, files in os.walk(root):
        names[:] = sorted(
            name
            for name in names
            if name not in excluded and not name.startswith(".")
        )
        for name in sorted(files):
            if name.endswith((".md", ".rst", ".txt")):
                yield os.path.join(directory, name)


def private_text_at(line):
    lowered = line.lower()
    for length, expected in PRIVATE_TEXT_HASHES.items():
        for offset in range(len(lowered) - length + 1):
            candidate = lowered[offset:offset + length]
            digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
            if digest in expected:
                return candidate
    return None


class TestDocumentationPolicy(unittest.TestCase):
    def test_documentation_has_no_em_dash(self):
        violations = []
        root = repo_root()
        for path in documentation_paths(root):
            with open(path, encoding="utf-8") as stream:
                for number, line in enumerate(stream, 1):
                    if "\N{EM DASH}" in line:
                        violations.append(
                            "{}:{}: {}".format(
                                os.path.relpath(path, root), number, line.rstrip()
                            )
                        )
        self.assertEqual([], violations, "\n".join(violations))

    def test_documentation_has_no_private_team_terms(self):
        violations = []
        root = repo_root()
        for path in documentation_paths(root):
            with open(path, encoding="utf-8") as stream:
                for number, line in enumerate(stream, 1):
                    match = private_text_at(line)
                    if match is not None:
                        violations.append(
                            "{}:{}: private term {!r}".format(
                                os.path.relpath(path, root), number, match
                            )
                        )
        self.assertEqual([], violations, "\n".join(violations))


if __name__ == "__main__":
    unittest.main()
