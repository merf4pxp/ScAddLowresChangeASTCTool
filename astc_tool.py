#!/usr/bin/env python3
"""
convert_astc.py — Convert highres texture in SC file from ASTC 6x6 to ASTC 4x4.

Usage:
    python convert_astc.py input.sc output.sc PVRTexToolCLI.exe

Dependencies:
    pip install zstandard flatbuffers
"""

import struct
import sys
import os
import subprocess

try:
    import zstandard
except ImportError:
    print("ERROR: pip install zstandard"); sys.exit(1)

try:
    import flatbuffers
    from flatbuffers.table import Table
except ImportError:
    print("ERROR: pip install flatbuffers"); sys.exit(1)


# ── SC parsing ───────────────────────────────────────────────────────────

def read_sc(filepath):
    with open(filepath, 'rb') as f:
        data = bytearray(f.read())

    if data[:2] == b'SC':
        header_start = 6
    else:
        header_start = 8

    header_fb_size = struct.unpack_from('<I', data, header_start)[0]
    header_fb = data[header_start + 4 : header_start + 4 + header_fb_size]

    header_root = struct.unpack_from('<I', header_fb, 0)[0]
    header = Table(bytearray(header_fb), header_root)

    def get_uint(tbl, buf, vslot):
        off = tbl.Offset(vslot)
        return struct.unpack_from('<I', buf, tbl.Pos + off)[0] if off else 0

    texture_count   = get_uint(header, header_fb, 12)
    textures_length = get_uint(header, header_fb, 22)
    compressed_size = get_uint(header, header_fb, 26)

    body_start = header_start + 4 + header_fb_size
    body = data[body_start:]

    dctx = zstandard.ZstdDecompressor()
    try:
        body = bytearray(dctx.decompress(bytes(body)))
    except zstandard.ZstdError:
        body = bytearray(body)

    chunks = []
    pos = 0
    while pos + 4 <= len(body):
        csz = struct.unpack_from('<I', body, pos)[0]
        if csz == 0 or pos + 4 + csz > len(body):
            break
        chunks.append({'offset': pos, 'size': csz, 'data': body[pos+4:pos+4+csz]})
        pos += 4 + csz

    return {
        'raw': data,
        'header_start': header_start,
        'header_fb': bytearray(header_fb),
        'header_root': header_root,
        'texture_count': texture_count,
        'textures_length': textures_length,
        'compressed_size': compressed_size,
        'body_start': body_start,
        'chunks': chunks,
    }


# ── Texture parsing ──────────────────────────────────────────────────────

def parse_texture_data(buf, pos):
    td = Table(buf, pos)

    def ubyte(slot):
        off = td.Offset(slot)
        return struct.unpack_from('B', buf, td.Pos + off)[0] if off else 0

    def ushort(slot):
        off = td.Offset(slot)
        return struct.unpack_from('<H', buf, td.Pos + off)[0] if off else 0

    data_off = td.Offset(12)
    data = b''
    if data_off:
        du = struct.unpack_from('<I', buf, td.Pos + data_off)[0]
        dpos = td.Pos + data_off + du
        dlen = struct.unpack_from('<I', buf, dpos)[0]
        data = bytes(buf[dpos+4 : dpos+4+dlen])

    return {
        'texture_format': ubyte(4),
        'pixel_type':     ubyte(6),
        'width':          ushort(8),
        'height':         ushort(10),
        'data':           data,
    }


def parse_textures_chunk(chunk_data):
    buf = bytearray(chunk_data)
    root = struct.unpack_from('<I', buf, 0)[0]
    textures = Table(buf, root)

    off = textures.Offset(4)
    if not off:
        return []

    vu = struct.unpack_from('<I', buf, textures.Pos + off)[0]
    vpos = textures.Pos + off + vu
    vlen = struct.unpack_from('<I', buf, vpos)[0]

    sets = []
    for i in range(vlen):
        tsu = struct.unpack_from('<I', buf, vpos + 4 + i*4)[0]
        tsp = vpos + 4 + i*4 + tsu
        ts = Table(buf, tsp)

        lowres = None
        lr_off = ts.Offset(4)
        if lr_off:
            lru = struct.unpack_from('<I', buf, tsp + lr_off)[0]
            lowres = parse_texture_data(buf, tsp + lr_off + lru)

        highres = None
        hr_off = ts.Offset(6)
        if hr_off:
            hru = struct.unpack_from('<I', buf, tsp + hr_off)[0]
            highres = parse_texture_data(buf, tsp + hr_off + hru)

        sets.append({'lowres': lowres, 'highres': highres})

    return sets


# ── KTX helpers ──────────────────────────────────────────────────────────

KTX_MAGIC = b'\xabKTX 11\xbb\r\n\x1a\n'

def parse_ktx(data):
    """Parse KTX 1.0 header and return glInternalFormat + pixel data."""
    if data[:12] != KTX_MAGIC:
        print("  WARNING: KTX magic not found, trying anyway...")

    # KTX header: magic(12) + endianness(4) + glType(4) + glTypeSize(4) +
    #             glFormat(4) + glInternalFormat(4) + glBaseInternalFormat(4) +
    #             pixelWidth(4) + pixelHeight(4) + pixelDepth(4) +
    #             numberOfArrayElements(4) + numberOfFaces(4) +
    #             numberOfMipmapLevels(4) + bytesOfKeyValueData(4)
    gl_internal = struct.unpack_from('<I', data, 28)[0]
    width  = struct.unpack_from('<I', data, 36)[0]
    height = struct.unpack_from('<I', data, 40)[0]
    mip_levels = struct.unpack_from('<I', data, 52)[0] or 1
    kv_size = struct.unpack_from('<I', data, 56)[0]

    pos = 60 + kv_size
    mip_datas = []
    for i in range(mip_levels):
        if pos + 4 > len(data):
            break
        img_size = struct.unpack_from('<I', data, pos)[0]
        pos += 4
        mip_datas.append(data[pos:pos+img_size])
        pos += img_size
        # 4-byte alignment
        pad = (4 - (img_size % 4)) % 4
        pos += pad

    return {
        'gl_internal': gl_internal,
        'width': width,
        'height': height,
        'mip_levels': mip_levels,
        'mip_datas': mip_datas,
        'kv_size': kv_size,
        'raw': data,
    }


def build_ktx(gl_internal, width, height, mip_datas, kv_data=b''):
    """Build a KTX 1.0 file from components."""
    header = bytearray()
    header += KTX_MAGIC
    header += struct.pack('<I', 0x04030201)  # endianness
    header += struct.pack('<I', 0)  # glType
    header += struct.pack('<I', 1)  # glTypeSize
    header += struct.pack('<I', 0)  # glFormat
    header += struct.pack('<I', gl_internal)  # glInternalFormat
    header += struct.pack('<I', 0x1908)  # glBaseInternalFormat (RGBA8)
    header += struct.pack('<I', width)
    header += struct.pack('<I', height)
    header += struct.pack('<I', 0)  # pixelDepth
    header += struct.pack('<I', 0)  # numberOfArrayElements
    header += struct.pack('<I', 1)  # numberOfFaces (was 0, should be 1)
    header += struct.pack('<I', len(mip_datas))  # numberOfMipmapLevels
    header += struct.pack('<I', len(kv_data))  # bytesOfKeyValueData
    header += kv_data

    body = bytearray()
    for mip in mip_datas:
        body += struct.pack('<I', len(mip))
        body += mip
        pad = (4 - (len(mip) % 4)) % 4
        body += b'\x00' * pad

    return bytes(header) + bytes(body)


# ── ASTC conversion ──────────────────────────────────────────────────────

def convert_astc(ktx_data, target_format, pvrtex_path):
    """Convert KTX texture to target ASTC format via PVRTexToolCLI."""
    temp_in  = "temp_convert_in.ktx"
    temp_out = "temp_convert_out.ktx"

    with open(temp_in, 'wb') as f:
        f.write(ktx_data)

    cmd = [pvrtex_path, '-i', temp_in, '-o', temp_out, '-f', target_format]
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0 or not os.path.exists(temp_out):
        print(f"  PVRTexToolCLI error:\n{result.stderr}")
        for p in [temp_in, temp_out]:
            if os.path.exists(p):
                os.remove(p)
        return None

    with open(temp_out, 'rb') as f:
        new_ktx = f.read()

    for p in [temp_in, temp_out]:
        if os.path.exists(p):
            os.remove(p)

    return new_ktx


# ── Textures chunk building ──────────────────────────────────────────────

def build_textures_chunk(texture_sets):
    builder = flatbuffers.Builder(0)

    ts_offsets = []
    for ts in texture_sets:
        # Highres
        hr = ts['highres']
        hr_data = builder.CreateByteVector(hr['data'])
        builder.StartObject(6)
        builder.PrependUint8Slot(0, hr['texture_format'], 0)
        builder.PrependUint8Slot(1, hr['pixel_type'], 0)
        builder.PrependUint16Slot(2, hr['width'], 0)
        builder.PrependUint16Slot(3, hr['height'], 0)
        builder.PrependUOffsetTRelativeSlot(4, hr_data, 0)
        hr_off = builder.EndObject()

        # Lowres
        lr_off = 0
        if ts.get('lowres'):
            lr = ts['lowres']
            lr_data = builder.CreateByteVector(lr['data'])
            builder.StartObject(6)
            builder.PrependUint8Slot(0, lr['texture_format'], 0)
            builder.PrependUint8Slot(1, lr['pixel_type'], 0)
            builder.PrependUint16Slot(2, lr['width'], 0)
            builder.PrependUint16Slot(3, lr['height'], 0)
            builder.PrependUOffsetTRelativeSlot(4, lr_data, 0)
            lr_off = builder.EndObject()

        builder.StartObject(2)
        if lr_off:
            builder.PrependUOffsetTRelativeSlot(0, lr_off, 0)
        builder.PrependUOffsetTRelativeSlot(1, hr_off, 0)
        ts_offsets.append(builder.EndObject())

    builder.StartVector(4, len(ts_offsets), 4)
    for off in reversed(ts_offsets):
        builder.PrependUOffsetTRelative(off)
    tv = builder.EndVector()

    builder.StartObject(1)
    builder.PrependUOffsetTRelativeSlot(0, tv, 0)
    tt = builder.EndObject()
    builder.Finish(tt)

    return bytes(builder.Output())


# ── Header rebuilding ────────────────────────────────────────────────────

def rebuild_header(sc, new_tex_size, compressed_body_size):
    header_fb = sc['header_fb']
    header = Table(header_fb, sc['header_root'])

    def get_uint(slot):
        off = header.Offset(slot)
        return struct.unpack_from('<I', header_fb, header.Pos + off)[0] if off else 0

    translation_precision = get_uint(4)
    scale_precision       = get_uint(6)
    shape_count           = get_uint(8)
    movie_clips_count    = get_uint(10)
    texture_count        = get_uint(12)
    text_fields_count    = get_uint(14)
    unk3                 = get_uint(16)
    unk4                 = get_uint(18)
    resources_offset     = get_uint(20)
    external_matrix_bank_size = get_uint(28)

    # Parse metadata
    metadata_entries = []
    meta_off = header.Offset(24)
    if meta_off:
        mu = struct.unpack_from('<I', header_fb, header.Pos + meta_off)[0]
        mpos = header.Pos + meta_off + mu
        mlen = struct.unpack_from('<I', header_fb, mpos)[0]
        for i in range(mlen):
            eu = struct.unpack_from('<I', header_fb, mpos + 4 + i*4)[0]
            epos = mpos + 4 + i*4 + eu
            entry = Table(header_fb, epos)

            name = ""
            name_off = entry.Offset(4)
            if name_off:
                nu = struct.unpack_from('<I', header_fb, epos + name_off)[0]
                npos = epos + name_off + nu
                nlen = struct.unpack_from('<I', header_fb, npos)[0]
                name = header_fb[npos+4:npos+4+nlen].decode('utf-8', errors='replace')

            hash_bytes = b''
            hash_off = entry.Offset(6)
            if hash_off:
                hu = struct.unpack_from('<I', header_fb, epos + hash_off)[0]
                hpos = epos + hash_off + hu
                hlen = struct.unpack_from('<I', header_fb, hpos)[0]
                hash_bytes = bytes(header_fb[hpos+4:hpos+4+hlen])

            metadata_entries.append({'name': name, 'hash': hash_bytes})

    builder = flatbuffers.Builder(256)

    meta_offsets = []
    for entry in metadata_entries:
        name_off = builder.CreateString(entry['name'])
        hash_off = builder.CreateByteVector(entry['hash'])
        builder.StartObject(2)
        builder.PrependUOffsetTRelativeSlot(0, name_off, 0)
        builder.PrependUOffsetTRelativeSlot(1, hash_off, 0)
        meta_offsets.append(builder.EndObject())

    if meta_offsets:
        builder.StartVector(4, len(meta_offsets), 4)
        for off in reversed(meta_offsets):
            builder.PrependUOffsetTRelative(off)
        meta_vec = builder.EndVector()
    else:
        meta_vec = 0

    builder.StartObject(13)
    builder.PrependUint32Slot(0, translation_precision, 0)
    builder.PrependUint32Slot(1, scale_precision, 0)
    builder.PrependUint32Slot(2, shape_count, 0)
    builder.PrependUint32Slot(3, movie_clips_count, 0)
    builder.PrependUint32Slot(4, texture_count, 0)
    builder.PrependUint32Slot(5, text_fields_count, 0)
    builder.PrependUint32Slot(6, unk3, 0)
    builder.PrependUint32Slot(7, unk4, 0)
    builder.PrependUint32Slot(8, resources_offset, 0)
    builder.PrependUint32Slot(9, new_tex_size, 0)
    if meta_vec:
        builder.PrependUOffsetTRelativeSlot(10, meta_vec, 0)
    builder.PrependUint32Slot(11, compressed_body_size, 0)
    builder.PrependUint32Slot(12, external_matrix_bank_size, 0)
    header_off = builder.EndObject()
    builder.Finish(header_off)

    return bytes(builder.Output())


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    print("Usage: python convert_astc.py input.sc output.sc PVRTexToolCLI.exe [highres_fmt] [lowres_fmt]")
    print("       highres_fmt: ASTC_4x4 (default), ASTC_6x6, ASTC_8x8, etc.")
    print("       lowres_fmt:  same as highres by default")
    print()

    if len(sys.argv) < 4:
        print("ERROR: Not enough arguments.")
        sys.exit(1)

    input_sc      = sys.argv[1]
    output_sc     = sys.argv[2]
    pvrtex        = sys.argv[3]
    highres_fmt   = sys.argv[4] if len(sys.argv) > 4 else "ASTC_4x4"
    lowres_fmt    = sys.argv[5] if len(sys.argv) > 5 else highres_fmt

    # ── 1. Read SC
    print("Reading SC...")
    sc = read_sc(input_sc)
    print(f"  Textures length: {sc['textures_length']}")
    print(f"  Compressed size: {sc['compressed_size']}")
    print(f"  Chunks: {len(sc['chunks'])}")

    # ── 2. Find textures chunk
    tex_idx = None
    for i, c in enumerate(sc['chunks']):
        if c['size'] == sc['textures_length']:
            tex_idx = i
            break
    if tex_idx is None:
        tex_idx = len(sc['chunks']) - 1
    print(f"  Textures chunk: #{tex_idx}")

    # ── 3. Parse textures
    sets = parse_textures_chunk(sc['chunks'][tex_idx]['data'])
    for i, ts in enumerate(sets):
        hr = ts['highres']
        lr = ts['lowres']
        hr_info = f"{hr['width']}x{hr['height']} {len(hr['data'])}B" if hr else "none"
        lr_info = f"{lr['width']}x{lr['height']} {len(lr['data'])}B" if lr else "(none)"
        print(f"  [{i}] highres: {hr_info}  lowres: {lr_info}")

    if not sets or not sets[0]['highres']:
        print("ERROR: No highres texture found!")
        sys.exit(1)

    fmt_names = {
        0x93b0: "ASTC_4x4",
        0x93b1: "ASTC_5x4",
        0x93b2: "ASTC_5x5",
        0x93b3: "ASTC_6x5",
        0x93b4: "ASTC_6x6",
        0x93b5: "ASTC_8x5",
        0x93b6: "ASTC_8x6",
        0x93b7: "ASTC_8x8",
        0x93b8: "ASTC_10x5",
        0x93b9: "ASTC_10x6",
        0x93ba: "ASTC_10x8",
        0x93bb: "ASTC_10x10",
        0x93bc: "ASTC_12x10",
        0x93bd: "ASTC_12x12",
        0x93d0: "ASTC_4x4 sRGB",
        0x93d4: "ASTC_6x6 sRGB",
    }

    # ── 4. Convert highres
    hr = sets[0]['highres']
    ktx = parse_ktx(hr['data'])
    current_fmt = ktx['gl_internal']
    print(f"\n  Highres current: {ktx['width']}x{ktx['height']} glInternal=0x{current_fmt:04x} ({fmt_names.get(current_fmt, 'unknown')})")
    print(f"  Converting highres to {highres_fmt}...")

    new_ktx_data = convert_astc(hr['data'], highres_fmt + ",UBN", pvrtex)
    if new_ktx_data is None:
        print("ERROR: Highres conversion failed!")
        sys.exit(1)

    new_ktx = parse_ktx(new_ktx_data)
    print(f"  New highres KTX: {new_ktx['width']}x{new_ktx['height']} glInternal=0x{new_ktx['gl_internal']:04x} ({fmt_names.get(new_ktx['gl_internal'], 'unknown')}) {len(new_ktx_data)}B")

    sets[0]['highres']['data']   = new_ktx_data
    sets[0]['highres']['width']   = new_ktx['width']
    sets[0]['highres']['height']  = new_ktx['height']

    # ── 5. Convert lowres (if present)
    if sets[0].get('lowres'):
        lr = sets[0]['lowres']
        lr_ktx = parse_ktx(lr['data'])
        lr_current = lr_ktx['gl_internal']
        print(f"\n  Lowres current: {lr_ktx['width']}x{lr_ktx['height']} glInternal=0x{lr_current:04x} ({fmt_names.get(lr_current, 'unknown')})")
        print(f"  Converting lowres to {lowres_fmt}...")

        new_lr_ktx_data = convert_astc(lr['data'], lowres_fmt + ",UBN", pvrtex)
        if new_lr_ktx_data:
            new_lr_ktx = parse_ktx(new_lr_ktx_data)
            print(f"  New lowres KTX: {new_lr_ktx['width']}x{new_lr_ktx['height']} glInternal=0x{new_lr_ktx['gl_internal']:04x} ({fmt_names.get(new_lr_ktx['gl_internal'], 'unknown')}) {len(new_lr_ktx_data)}B")
            sets[0]['lowres']['data']   = new_lr_ktx_data
            sets[0]['lowres']['width']   = new_lr_ktx['width']
            sets[0]['lowres']['height']  = new_lr_ktx['height']
        else:
            print("  WARNING: Lowres conversion failed, keeping original.")
    else:
        print("\n  Lowres: (none, skipping)")

    # ── 6. Build new textures chunk
    new_tex_data = build_textures_chunk(sets)
    new_tex_size = len(new_tex_data)
    print(f"\n  New textures chunk: {new_tex_size} B (was {sc['textures_length']})")

    # ── 7. Rebuild body
    new_body = bytearray()
    for i, c in enumerate(sc['chunks']):
        if i == tex_idx:
            new_body += struct.pack('<I', new_tex_size)
            new_body += new_tex_data
        else:
            new_body += struct.pack('<I', c['size'])
            new_body += c['data']

    # ── 8. Compress
    print("\nCompressing body with zstd...")
    cctx = zstandard.ZstdCompressor()
    final_body = cctx.compress(bytes(new_body))
    print(f"  Compressed: {len(new_body)} -> {len(final_body)} bytes")

    # ── 9. Rebuild header
    print("\nRebuilding header...")
    new_header_fb = rebuild_header(sc, new_tex_size, len(final_body))

    # ── 10. Write output
    with open(output_sc, 'wb') as f:
        f.write(bytes(sc['raw'][:sc['header_start']]))
        f.write(struct.pack('<I', len(new_header_fb)))
        f.write(new_header_fb)
        f.write(final_body)

    print(f"\nWritten: {output_sc} ({os.path.getsize(output_sc)} B)")

    # ── 11. Verify
    print("\nVerifying...")
    sc2 = read_sc(output_sc)
    sets2 = parse_textures_chunk(sc2['chunks'][tex_idx]['data'])
    for i, ts in enumerate(sets2):
        hr = ts['highres']
        lr = ts['lowres']
        hr_ktx = parse_ktx(hr['data'])
        hr_info = f"{hr_ktx['width']}x{hr_ktx['height']} glInternal=0x{hr_ktx['gl_internal']:04x} {len(hr['data'])}B" if hr else "none"
        lr_ktx = parse_ktx(lr['data']) if lr else None
        lr_info = f"{lr_ktx['width']}x{lr_ktx['height']} glInternal=0x{lr_ktx['gl_internal']:04x} {len(lr['data'])}B" if lr else "(none)"
        print(f"  [{i}] highres: {hr_info}  lowres: {lr_info}")

    print(f"\n  compressed_size: {sc2['compressed_size']}")
    print("\nDone!")


if __name__ == '__main__':
    main()
