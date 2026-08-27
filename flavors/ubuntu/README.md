# Ubuntu flavor

The Ubuntu flavor is declared but not implemented.

`defs/flavor.bzl` accepts `ubuntu` as a known flavor name and fails package loading with an implementation-status message. The checked-in `[buckos.ubuntu]` configuration is not backed by Ubuntu buildroot, package, solver, or image targets.

This directory contains no DPKG source-unpack rule, package replay rule, dependency resolver, generated package data, or package recipes.
