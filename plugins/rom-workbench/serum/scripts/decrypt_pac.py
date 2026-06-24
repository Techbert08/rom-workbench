#!/usr/bin/env python3
"""
Decrypt a PIN2DMD .pac file and extract PAL and VNI chunks.

PAC format:
  [0:4]   magic "PAC "
  [4]     version (uint8)
  [5..]   chunks:
            [+0:2]       type (int16 BE): 1=PAL, 2=VNI
            [+2:6]       len  (int32 BE): byte count of encrypted data
            [+6:+6+len]  AES-128-CBC(GZip(data)), key=IV=vni_key

Usage:
  python3 scripts/decrypt_pac.py --pac pin2dmd.pac --key <hex_key> --out output/
"""

import argparse, struct, zlib, os
from Crypto.Cipher import AES

CHUNK_NAMES = {1: "pal", 2: "vni"}


def decrypt_chunk(data: bytes, key: bytes) -> bytes:
    cipher = AES.new(key, AES.MODE_CBC, iv=key)
    decrypted = cipher.decrypt(data)
    # wbits=31 → gzip mode; stops at the natural end-of-stream so AES
    # zero-padding that follows the compressed data doesn't cause an error.
    d = zlib.decompressobj(wbits=31)
    return d.decompress(decrypted) + d.flush()


def main():
    parser = argparse.ArgumentParser(description="Decrypt PIN2DMD .pac file")
    parser.add_argument("--pac", required=True, help="Input .pac file")
    parser.add_argument("--key", required=True, help="AES key as 32 hex chars")
    parser.add_argument("--out", required=True,
                        help="Output directory (receives <stem>.pal and <stem>.vni)")
    args = parser.parse_args()

    key = bytes.fromhex(args.key)
    assert len(key) == 16, "Key must be 16 bytes (32 hex chars)"

    with open(args.pac, "rb") as f:
        raw = f.read()

    assert raw[0:4] == b"PAC ", f"Bad PAC magic: {raw[0:4]!r}"
    stem = os.path.splitext(os.path.basename(args.pac))[0]
    os.makedirs(args.out, exist_ok=True)

    print(f"PAC version {raw[4]}, {len(raw):,} bytes")

    offset = 5
    while offset + 6 <= len(raw):
        chunk_type = struct.unpack_from(">h", raw, offset)[0]
        chunk_len  = struct.unpack_from(">i", raw, offset + 2)[0]
        data_start = offset + 6
        data_end   = data_start + chunk_len

        ext  = CHUNK_NAMES.get(chunk_type, f"type{chunk_type}")
        name = f"{ext.upper()}"
        print(f"\n  Chunk {name}: {chunk_len:,} bytes encrypted")

        encrypted = raw[data_start:data_end]
        plain     = decrypt_chunk(encrypted, key)

        out_path = os.path.join(args.out, f"{stem}.{ext}")
        with open(out_path, "wb") as f:
            f.write(plain)
        print(f"  -> {out_path} ({len(plain):,} bytes)")

        offset = data_end

    print("\nDone.")


if __name__ == "__main__":
    main()
