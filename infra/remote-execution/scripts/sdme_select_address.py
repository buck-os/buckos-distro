#!/usr/bin/env python3
"""Select the safest usable worker-listener address from `sdme ps` JSON."""

import ipaddress
import json
import sys
from collections.abc import Sequence
from typing import Optional, Union


RFC1918 = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
ULA = ipaddress.ip_network("fc00::/7")


IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]


def _priority(address: IPAddress) -> Optional[int]:
    if address.is_loopback or address.is_multicast or address.is_unspecified:
        return None
    if isinstance(address, ipaddress.IPv4Address):
        if any(address in network for network in RFC1918):
            return 0
    elif address in ULA:
        return 0
    if address.is_link_local:
        return 1
    return None


def select_worker_bind_address(values: Sequence[object]) -> str:
    candidates: list[tuple[int, int, int, IPAddress]] = []
    for value in values:
        if not isinstance(value, str):
            continue
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            continue
        priority = _priority(address)
        if priority is not None:
            candidates.append((priority, address.version != 4, int(address), address))
    if not candidates:
        raise ValueError("no private or link-local non-wildcard SDME zone address is available")
    selected = min(candidates)[3]
    return "[{}]".format(selected) if selected.version == 6 else str(selected)


def main() -> int:
    try:
        record = json.load(sys.stdin)
        values = record["addresses"]
        if not isinstance(values, list):
            raise ValueError("invalid SDME address inventory shape")
        print(select_worker_bind_address(values))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        print("sdme-address: {}".format(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
