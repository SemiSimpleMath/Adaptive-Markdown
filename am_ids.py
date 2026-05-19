"""ULID-style sticky identifiers.

Short, time-sortable, prefixed. Format: {prefix}-{10ts}{6rand} = 18 chars.
Timestamp (Crockford base32, ms precision) lexicographically sorts by
mint order. 6 random chars (30 bits) prevent collisions inside a single
millisecond — safe for hundreds of mints per ms.
"""
from __future__ import annotations

import os
import time

_CROCKFORD_BASE32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _b32_encode(n: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(_CROCKFORD_BASE32[n & 0x1F])
        n >>= 5
    return "".join(reversed(out))


def gen_id(prefix: str) -> str:
    """Mint a sortable, prefixed sticky ID. e.g. gen_id('d') -> 'd-01HNVQ7E9KMX2BNF'."""
    ts_ms = int(time.time() * 1000)
    ts_part = _b32_encode(ts_ms, 10)
    rand_bits = int.from_bytes(os.urandom(4), "big") & ((1 << 30) - 1)
    rand_part = _b32_encode(rand_bits, 6)
    return f"{prefix}-{ts_part}{rand_part}"
