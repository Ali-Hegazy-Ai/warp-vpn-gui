"""Generate a simple WARP-style icon (green circle on light background)."""

import struct
import zlib
import sys


def chunk(ctype: bytes, data: bytes) -> bytes:
    c = ctype + data
    return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)


def create_png(filename: str, size: int = 48) -> None:
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
    raw = b""
    green = b"\x28\xa7\x45"
    bg = b"\xf0\xf0\xf0"
    radius = size // 2 - 2
    cx, cy = size // 2, size // 2
    for y in range(size):
        raw += b"\x00"
        for x in range(size):
            dx, dy = x - cx, y - cy
            raw += green if dx * dx + dy * dy < radius * radius else bg
    idat = chunk(b"IDAT", zlib.compress(raw))
    iend = chunk(b"IEND", b"")
    with open(filename, "wb") as f:
        f.write(sig + ihdr + idat + iend)


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "warp-vpn.png"
    create_png(out)
    print(f"Icon created: {out}")
