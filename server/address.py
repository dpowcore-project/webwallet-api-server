import hashlib

from .segwit_addr import bech32_decode, convertbits, Encoding
import base58 as _b58

__all__ = ["address_to_scriptpubkey", "address_to_scripthash"]

_BECH32_HRPS    = frozenset(["dpc"])
_P2PKH_VERSIONS = frozenset([0x37, 0x42])
_P2SH_VERSIONS  = frozenset([0x1C, 0x80])


def is_bech32_address(addr: str) -> bool:
    lower = addr.lower()
    return any(lower.startswith(hrp + "1") for hrp in _BECH32_HRPS)


def bech32_to_scriptpubkey(addr: str) -> bytes:
    encoding, hrp, data = bech32_decode(addr.lower())

    if encoding is None:
        raise ValueError(f"Invalid bech32/bech32m address: {addr!r}")
    if hrp not in _BECH32_HRPS:
        raise ValueError(f"Unknown HRP {hrp!r}: {addr!r}")
    if not data:
        raise ValueError(f"Empty bech32 data: {addr!r}")

    witness_version = data[0]

    if witness_version == 0:
        if encoding != Encoding.BECH32:
            raise ValueError(f"Witness v0 must use bech32 encoding: {addr!r}")
        program = convertbits(data[1:], 5, 8, False)
        if program is None:
            raise ValueError(f"Cannot decode witness program: {addr!r}")
        prog = bytes(program)
        if len(prog) == 20:
            return b"\x00\x14" + prog  # P2WPKH
        if len(prog) == 32:
            return b"\x00\x20" + prog  # P2WSH
        raise ValueError(f"Witness v0 program must be 20 or 32 bytes, got {len(prog)}: {addr!r}")

    if witness_version == 1:
        if encoding != Encoding.BECH32M:
            raise ValueError(f"Witness v1 must use bech32m encoding: {addr!r}")
        program = convertbits(data[1:], 5, 8, False)
        if program is None:
            raise ValueError(f"Cannot decode witness program: {addr!r}")
        prog = bytes(program)
        if len(prog) != 32:
            raise ValueError(f"Witness v1 (Taproot) program must be 32 bytes, got {len(prog)}: {addr!r}")
        return b"\x51\x20" + prog  # P2TR

    raise ValueError(f"Unsupported witness version {witness_version}: {addr!r}")


def base58_to_scriptpubkey(addr: str) -> bytes:
    try:
        raw = _b58.b58decode_check(addr)
    except Exception as exc:
        raise ValueError(f"Invalid base58check address {addr!r}: {exc}") from exc

    if len(raw) != 21:
        raise ValueError(f"Expected 21 decoded bytes, got {len(raw)}: {addr!r}")

    version, hash160 = raw[0], raw[1:]

    if version in _P2PKH_VERSIONS:
        return b"\x76\xa9\x14" + hash160 + b"\x88\xac"  # P2PKH
    if version in _P2SH_VERSIONS:
        return b"\xa9\x14" + hash160 + b"\x87"           # P2SH
    raise ValueError(f"Unknown version byte 0x{version:02X}: {addr!r}")


def address_to_scriptpubkey(addr: str) -> bytes:
    if is_bech32_address(addr):
        return bech32_to_scriptpubkey(addr)
    return base58_to_scriptpubkey(addr)


def address_to_scripthash(addr: str) -> str:
    script = address_to_scriptpubkey(addr)
    return hashlib.sha256(script).digest()[::-1].hex()
