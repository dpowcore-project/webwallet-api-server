#!/usr/bin/env python3
"""
Unit tests for server/address.py.
Run with:  python3 test_address.py
No external dependencies beyond requirements.txt.
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from server.address import (
    address_to_scriptpubkey,
    address_to_scripthash,
)
from server.segwit_addr import (
    CHARSET          as _B32_CHARSET,
    bech32_polymod   as _b32_polymod,
    bech32_hrp_expand as _b32_hrp_expand,
    convertbits      as _convertbits,
)
import base58
import hashlib


def _b58check_decode(addr: str) -> bytes:
    """Decode a Base58Check address -> version_byte + payload bytes."""
    return base58.b58decode_check(addr)


def fail(msg):
    print(f"  FAIL: {msg}")
    sys.exit(1)


def ok(label):
    print(f"  ok   {label}")


# ---------------------------------------------------------------------------
# base58check
# ---------------------------------------------------------------------------
print("=== base58check ===")

# Bitcoin genesis P2PKH address (version 0x00)
d = _b58check_decode("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa")
assert d[0] == 0x00, f"expected 0x00 got 0x{d[0]:02X}"
assert d[1:].hex() == "62e907b15cbf27d5425399ebf6f0fb50ebb88f18"
ok("Bitcoin mainnet P2PKH decode")

# Known P2SH (version 0x05)
d2 = _b58check_decode("3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy")
assert d2[0] == 0x05
ok("Bitcoin mainnet P2SH decode")

# Bad checksum must raise
try:
    _b58check_decode("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNb")   # last char changed
    fail("should have raised on bad checksum")
except ValueError:
    ok("bad checksum raises ValueError")

# ---------------------------------------------------------------------------
# Build a custom address with version 0x37 (Dpowcoin mainnet P2PKH) and round-trip
# ---------------------------------------------------------------------------
print()
print("=== custom version bytes ===")

import hashlib as _hl


def _make_b58_address(version_byte: int, hash160: bytes) -> str:
    B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    payload = bytes([version_byte]) + hash160
    chk = _hl.sha256(_hl.sha256(payload).digest()).digest()[:4]
    full = payload + chk
    n = int.from_bytes(full, "big")
    result = []
    while n:
        n, r = divmod(n, 58)
        result.append(B58[r])
    pad = len(full) - len(full.lstrip(b"\x00"))
    return "1" * pad + "".join(reversed(result))


for version, expected_script_prefix in [
    (0x37, "76a914"),   # Dpowcoin mainnet P2PKH
    (0x1C, "a914"),     # Dpowcoin mainnet P2SH
    (0x42, "76a914"),   # Dpowcoin testnet P2PKH
    (0x80, "a914"),     # Dpowcoin testnet P2SH
]:
    h = bytes(range(20))
    addr = _make_b58_address(version, h)
    script = address_to_scriptpubkey(addr)
    if not script.hex().startswith(expected_script_prefix):
        fail(f"version 0x{version:02X}: script prefix wrong: {script.hex()!r}")
    ok(f"version 0x{version:02X} -> {script.hex()[:14]}...")

# ---------------------------------------------------------------------------
# bech32 encode -> decode round-trip
# ---------------------------------------------------------------------------
print()
print("=== bech32 ===")


def _bech32_encode(hrp: str, data: list) -> str:
    """Minimal bech32 encoder for testing."""
    def checksum(hrp, data):
        values = _b32_hrp_expand(hrp) + data
        pm = _b32_polymod(values + [0] * 6) ^ 1
        return [(pm >> 5 * (5 - i)) & 31 for i in range(6)]
    combined = data + checksum(hrp, data)
    return hrp + "1" + "".join(_B32_CHARSET[d] for d in combined)


hash20 = bytes(range(20))
hash32 = bytes(range(32))

# P2WPKH with 'web' HRP
data_p2wpkh = [0] + _convertbits(list(hash20), 8, 5)
addr_p2wpkh = _bech32_encode("web", data_p2wpkh)
script = address_to_scriptpubkey(addr_p2wpkh)
expected = b"\x00\x14" + hash20
if script != expected:
    fail(f"P2WPKH script mismatch: {script.hex()} vs {expected.hex()}")
ok(f"web bech32 P2WPKH: {addr_p2wpkh}")

# P2WSH with 'web' HRP
data_p2wsh = [0] + _convertbits(list(hash32), 8, 5)
addr_p2wsh = _bech32_encode("web", data_p2wsh)
script2 = address_to_scriptpubkey(addr_p2wsh)
expected2 = b"\x00\x20" + hash32
if script2 != expected2:
    fail(f"P2WSH script mismatch: {script2.hex()} vs {expected2.hex()}")
ok(f"web bech32 P2WSH:  {addr_p2wsh}")

# Bad checksum
try:
    bad = addr_p2wpkh[:-1] + ("q" if addr_p2wpkh[-1] != "q" else "p")
    address_to_scriptpubkey(bad)
    fail("bad checksum should raise")
except ValueError:
    ok("bad bech32 checksum raises ValueError")

# ---------------------------------------------------------------------------
# scripthash
# ---------------------------------------------------------------------------
print()
print("=== scripthash ===")

hash20_b = bytes(20)   # all zeros
addr_zero = _bech32_encode("web", [0] + _convertbits(list(hash20_b), 8, 5))
script_zero = b"\x00\x14" + hash20_b
expected_sh = hashlib.sha256(script_zero).digest()[::-1].hex()
got_sh = address_to_scripthash(addr_zero)
if got_sh != expected_sh:
    fail(f"scripthash mismatch: {got_sh} vs {expected_sh}")
ok(f"scripthash: {got_sh}")

# ---------------------------------------------------------------------------
print()
print("ALL TESTS PASSED")
