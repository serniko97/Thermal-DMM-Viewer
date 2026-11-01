#!/usr/bin/env python3
"""
fedtool.py - analyse a FeelElec / ToolTop UPDATA.FED firmware update file.

The meter updates itself from a file named UPDATA.FED placed on its USB drive.
This tool inspects that file: size and alignment, byte statistics (entropy,
histogram, chi-square), a test for repeating-key XOR, a test for ECB-style
block repetition, and a scan for an appended CRC-32 or hash.

On the one file analysed so far (90x120 model, V117) every statistic says the
payload is encrypted or otherwise whitened: see ../README.md. This tool is
meant to be run on more files (other models, other versions) to look for
structure that a single file can't reveal, for example a shared header or a
constant region.

Pure Python 3 standard library. Does not modify the file.

Usage:
  python fedtool.py UPDATA.FED
  python fedtool.py a.FED b.FED        compare several files
  python fedtool.py --json UPDATA.FED  machine-readable output
"""

from __future__ import annotations

import collections
import hashlib
import json
import math
import struct
import sys
import zlib


def shannon_entropy(data: bytes) -> float:
    """Bits of entropy per byte (0..8). ~8.0 means indistinguishable from random."""
    if not data:
        return 0.0
    counts = collections.Counter(data)
    n = len(data)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


def chi_square_uniform(data: bytes) -> float:
    """Chi-square of the byte histogram against a uniform distribution (255 dof).
    For random data this is about 255 +/- 23; a compressed-but-not-encrypted
    payload usually scores far higher because some byte values dominate."""
    expected = len(data) / 256
    counts = collections.Counter(data)
    return sum((counts.get(b, 0) - expected) ** 2 / expected for b in range(256))


def bit_ratio(data: bytes) -> float:
    """Fraction of 1 bits. Encryption/compression gives ~0.5."""
    ones = sum(bin(b).count("1") for b in data)
    return ones / (len(data) * 8) if data else 0.0


def _uniform_topfreq_baseline(column_size: int) -> float:
    """Expected 'peakiness' (max bin count / size) for a uniform-random column of
    this size over 256 values. Even random data looks peaky when the column is
    short, so a repeating-XOR score is only meaningful compared against this.
    Derived from the expected maximum of 256 multinomial counts."""
    if column_size <= 0:
        return 1.0
    # Mean + ~2 sigma of the max of 256 bins, good enough to flag real structure.
    mean = column_size / 256
    import math as _m
    # expected maximum of k iid ~ Poisson(mean) bins, rough extreme-value estimate
    expected_max = mean + _m.sqrt(2 * max(mean, 1e-9) * _m.log(256)) + _m.log(256)
    return min(expected_max / column_size, 1.0)


def repeating_xor_score(data: bytes, period: int) -> float:
    """Average 'peakiness' of the byte histogram within each column when the data
    is folded at the given period. A repeating XOR key of length `period` leaves
    each column as plaintext XORed with one constant byte, so a real text/binary
    payload would make some columns clearly peaked. Compared against the uniform
    baseline (1/256 ~ 0.0039)."""
    if period <= 0 or period >= len(data):
        return 0.0
    total = 0.0
    for off in range(period):
        col = data[off::period]
        if col:
            total += max(collections.Counter(col).values()) / len(col)
    return total / period


def block_repetition(data: bytes, block: int) -> tuple[int, int]:
    """(number of blocks, number of duplicate blocks). Lots of duplicates is the
    classic sign of an ECB-mode cipher or an uncompressed payload with repeats."""
    blocks = [data[i:i + block] for i in range(0, len(data) - block + 1, block)]
    unique = len(set(blocks))
    return len(blocks), len(blocks) - unique


def trailer_checks(data: bytes) -> dict:
    """Look for a CRC-32 (last 4 bytes) or a hash (last 16/20/32 bytes) of the
    rest of the file - a common way to protect an update against corruption."""
    out = {}
    if len(data) > 4:
        body = data[:-4]
        out["crc32_body"] = f"{zlib.crc32(body) & 0xFFFFFFFF:08x}"
        out["trailer_le"] = f"{struct.unpack('<I', data[-4:])[0]:08x}"
        out["trailer_be"] = f"{struct.unpack('>I', data[-4:])[0]:08x}"
        out["crc32_match"] = out["crc32_body"] in (out["trailer_le"], out["trailer_be"])
    for n, algo in ((16, "md5"), (20, "sha1"), (32, "sha256")):
        if len(data) > n:
            out[f"{algo}_trailer_match"] = hashlib.new(algo, data[:-n]).digest() == data[-n:]
    return out


def analyse(path: str) -> dict:
    with open(path, "rb") as fh:
        data = fh.read()
    n = len(data)
    entropy = shannon_entropy(data)
    periods = (16, 24, 32, 64, 128, 256, 512, 1024)
    xor = {p: round(repeating_xor_score(data, p), 5) for p in periods}
    # A period shows a real repeating key only if it beats the random baseline
    # for its column length by a clear margin.
    xor_excess = max(xor[p] - _uniform_topfreq_baseline(n // p) for p in periods)
    dup16 = block_repetition(data, 16)[1]
    dup8 = block_repetition(data, 8)[1]
    looks_encrypted = (entropy > 7.9 and dup16 == 0
                       and xor_excess < 0.01 and len(set(data)) == 256)
    return {
        "file": path,
        "size": n,
        "size_hex": f"0x{n:x}",
        "sha256": hashlib.sha256(data).hexdigest(),
        "alignment": {str(b): n % b for b in (512, 1024, 2048, 4096)},
        "entropy_bits_per_byte": round(entropy, 4),
        "chi_square_vs_uniform": round(chi_square_uniform(data), 1),
        "bit_ratio": round(bit_ratio(data), 5),
        "distinct_byte_values": len(set(data)),
        "duplicate_8B_blocks": dup8,
        "duplicate_16B_blocks": dup16,
        "repeating_xor_scores": xor,
        "repeating_xor_excess_over_random": round(xor_excess, 5),
        "trailer": trailer_checks(data),
        "magic_first_4_bytes": data[:4].hex(),
        "verdict": "encrypted / whitened (no exploitable structure in this file)"
        if looks_encrypted else "structure present - investigate further",
    }


def print_report(r: dict) -> None:
    print(f"File:            {r['file']}")
    print(f"Size:            {r['size']:,} bytes ({r['size_hex']})")
    al = r["alignment"]
    print(f"Alignment:       mod 512 = {al['512']}, mod 1024 = {al['1024']}, "
          f"mod 4096 = {al['4096']}")
    print(f"SHA-256:         {r['sha256']}")
    print(f"First 4 bytes:   {r['magic_first_4_bytes']}  (no known container magic)")
    print(f"Entropy:         {r['entropy_bits_per_byte']} bits/byte  (8.0 = random)")
    print(f"Chi-square:      {r['chi_square_vs_uniform']}  (255 dof; random ≈ 255 ± 23)")
    print(f"Bit ratio:       {r['bit_ratio']}  (0.5 = balanced)")
    print(f"Distinct bytes:  {r['distinct_byte_values']} / 256")
    print(f"Duplicate blocks: {r['duplicate_16B_blocks']} of 16 B, "
          f"{r['duplicate_8B_blocks']} of 8 B  (0 = no ECB-style repetition)")
    excess = r["repeating_xor_excess_over_random"]
    print(f"Repeating-XOR:   max excess over random {excess} "
          f"({'no repeating key' if excess < 0.01 else 'possible period - investigate'})")
    t = r["trailer"]
    print(f"Trailer CRC-32:  body {t.get('crc32_body')} vs stored "
          f"{t.get('trailer_le')}/{t.get('trailer_be')} → "
          f"{'match' if t.get('crc32_match') else 'no match'}")
    print(f"Trailer hash:    md5 {t.get('md5_trailer_match')}, "
          f"sha1 {t.get('sha1_trailer_match')}, sha256 {t.get('sha256_trailer_match')}")
    print(f"Verdict:         {r['verdict']}")


def main(argv: list[str]) -> int:
    as_json = "--json" in argv
    files = [a for a in argv if not a.startswith("-")]
    if not files:
        print(__doc__)
        return 2
    results = [analyse(f) for f in files]
    if as_json:
        print(json.dumps(results if len(results) != 1 else results[0], indent=2))
        return 0
    for i, r in enumerate(results):
        if i:
            print()
        print_report(r)
    if len(results) > 1:
        print("\nShared-structure checks across files:")
        first = results[0]["size"]
        print(f"  same size: {all(r['size'] == first for r in results)}")
        # If two files differ only in a few byte positions, those are candidate
        # header/version fields. Only meaningful for equal-length files.
        if len({r["size"] for r in results}) == 1 and len(results) == 2:
            a = open(results[0]["file"], "rb").read()
            b = open(results[1]["file"], "rb").read()
            diffs = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
            print(f"  differing byte positions: {len(diffs)} "
                  f"({100 * len(diffs) / len(a):.1f}% of the file)")
            if 0 < len(diffs) <= 64:
                print(f"  positions: {diffs}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
