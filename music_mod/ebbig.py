"""Reader for Skate 3 "EB\\0\\3" BIG archives (records may be RefPack-compressed)."""
import struct
import sys


def refpack_decompress(src):
    flags = src[0]
    if src[1] != 0xFB:
        raise ValueError("not refpack")
    p = 2
    width = 4 if flags & 0x01 else 3
    if flags & 0x80:
        p += width
    size = int.from_bytes(src[p:p + width], "big")
    p += width
    out = bytearray()
    while p < len(src):
        b0 = src[p]
        if b0 < 0x80:
            b1 = src[p + 1]
            lit = b0 & 3
            n = ((b0 >> 2) & 7) + 3
            off = ((b0 & 0x60) << 3) + b1 + 1
            p += 2
        elif b0 < 0xC0:
            b1, b2 = src[p + 1], src[p + 2]
            lit = b1 >> 6
            n = (b0 & 0x3F) + 4
            off = ((b1 & 0x3F) << 8) + b2 + 1
            p += 3
        elif b0 < 0xE0:
            b1, b2, b3 = src[p + 1], src[p + 2], src[p + 3]
            lit = b0 & 3
            n = ((b0 & 0x0C) << 6) + b3 + 5
            off = ((b0 & 0x10) << 12) + (b1 << 8) + b2 + 1
            p += 4
        elif b0 < 0xFC:
            lit = ((b0 & 0x1F) << 2) + 4
            out += src[p + 1:p + 1 + lit]
            p += 1 + lit
            continue
        else:
            lit = b0 & 3
            out += src[p + 1:p + 1 + lit]
            break
        out += src[p:p + lit]
        p += lit
        start = len(out) - off
        for k in range(n):
            out.append(out[start + k])
    return bytes(out[:size])


def refpack_compress(data):
    """Compress data using RefPack container format."""
    out = bytearray()
    out.append(0x10)  # flags: 3-byte size header
    out.append(0xFB)
    out.extend(len(data).to_bytes(3, "big"))
    p = 0
    while p + 4 <= len(data):
        chunk = min(112, (len(data) - p) & ~3)
        b0 = 0xE0 | ((chunk - 4) >> 2)
        out.append(b0)
        out.extend(data[p:p + chunk])
        p += chunk
    rem = len(data) - p
    out.append(0xFC | rem)
    out.extend(data[p:])
    return bytes(out)


def chunkref_decompress(src):
    """'chunkref' container: a series of independently RefPack-compressed chunks."""
    u32 = lambda o: struct.unpack(">I", src[o:o + 4])[0]
    total, count, align = u32(0x0C), u32(0x14), u32(0x18) or 1
    out = bytearray()
    # each chunk: u32 csize, u32 kind, then data starting on an `align` boundary
    end = 0x20
    for _ in range(count):
        data_at = (end + 8 + align - 1) // align * align
        csize, kind = u32(data_at - 8), u32(data_at - 4)
        body = src[data_at:data_at + csize]
        out += refpack_decompress(body) if kind == 2 else body
        end = data_at + csize
    return bytes(out[:total])


def decompress(src, size):
    if src[:8] == b"chunkref":
        return chunkref_decompress(src)
    return refpack_decompress(src)


def read_records(path):
    d = open(path, "rb").read()
    if d[:4] != b"EB\x00\x03":
        raise ValueError("not EB v3")
    u32 = lambda o: struct.unpack(">I", d[o:o + 4])[0]
    count = u32(4)
    shift = (u32(8) >> 8) & 0xFF
    recs = []
    for i in range(count):
        off, csize, size, rid = (u32(0x30 + 16 * i + 4 * k) for k in range(4))
        off <<= shift
        if csize:
            data = decompress(d[off:off + csize], size)
        else:
            data = d[off:off + size]
        recs.append((rid, data))
    return recs


if __name__ == "__main__":
    for rid, data in read_records(sys.argv[1]):
        print(f"{rid:08x} {len(data)} {data[:16]!r}")
