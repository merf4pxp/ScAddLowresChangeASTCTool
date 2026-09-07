#!/usr/bin/env python3
"""
add_lowres.py — Add lowres texture to an SC file.

Usage:
    python add_lowres.py input.sc highres.png output.sc [PVRTexToolCLI.exe]

Dependencies:
    pip install zstandard flatbuffers Pillow
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

try:
    from PIL import Image
except ImportError:
    print("ERROR: pip install Pillow"); sys.exit(1)


# ── SC file parsing ──────────────────────────────────────────────────────

def read_sc(filepath):
    with open(filepath, 'rb') as f:
        data = bytearray(f.read())

    if data[:2] == b'SC':
        prefix_len = 2
        version = struct.unpack_from('<I', data, 2)[0]
        header_start = 6
    else:
        prefix_len = 4
        version = struct.unpack_from('<I', data, 4)[0]
        header_start = 8

    header_fb_size = struct.unpack_from('<I', data, header_start)[0]
    header_fb = data[header_start + 4 : header_start + 4 + header_fb_size]

    header_root = struct.unpack_from('<I', header_fb, 0)[0]
    header = Table(bytearray(header_fb), header_root)

    def get_uint(tbl, buf, vslot):
        off = tbl.Offset(vslot)
        if off:
            return struct.unpack_from('<I', buf, tbl.Pos + off)[0]
        return 0

    texture_count    = get_uint(header, header_fb, 12)
    textures_length  = get_uint(header, header_fb, 22)
    compressed_size  = get_uint(header, header_fb, 26)

    body_start = header_start + 4 + header_fb_size
    body = data[body_start:]

    # ВСЕГДА пытаемся zstd
    dctx = zstandard.ZstdDecompressor()
    try:
        body = bytearray(dctx.decompress(bytes(body)))
        print(f"  Body decompressed: {len(data) - body_start} -> {len(body)} bytes")
    except zstandard.ZstdError:
        try:
            body = bytearray(dctx.decompress(bytes(body), max_output_size=len(body)*20))
            print(f"  Body decompressed (forced): {len(data) - body_start} -> {len(body)} bytes")
        except zstandard.ZstdError:
            body = bytearray(body)
            print(f"  Body raw: {len(body)} bytes")

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
        'prefix_len': prefix_len,
        'version': version,
        'header_start': header_start,
        'header_fb_size': header_fb_size,
        'header_fb': bytearray(header_fb),
        'header_root': header_root,
        'texture_count': texture_count,
        'textures_length': textures_length,
        'compressed_size': compressed_size,
        'body_start': body_start,
        'body': body,
        'chunks': chunks,
        'compressed': True,  # всегда сжимаем при сборке
    }

# ── Textures chunk parsing ────────────────────────────────────────────────

def parse_texture_data(buf, pos):
    td = Table(buf, pos)

    def ubyte(slot):
        off = td.Offset(slot)
        return struct.unpack_from('B', buf, td.Pos + off)[0] if off else 0

    def ushort(slot):
        off = td.Offset(slot)
        return struct.unpack_from('<H', buf, td.Pos + off)[0] if off else 0

    data_off = td.Offset(12)  # field 4 → vtable slot 12
    data = b''
    if data_off:
        du = struct.unpack_from('<I', buf, td.Pos + data_off)[0]
        dpos = td.Pos + data_off + du
        dlen = struct.unpack_from('<I', buf, dpos)[0]
        data = bytes(buf[dpos+4 : dpos+4+dlen])

    return {
        'texture_format': ubyte(4),   # field 0
        'pixel_type':     ubyte(6),   # field 1
        'width':          ushort(8),  # field 2
        'height':         ushort(10), # field 3
        'data':           data,
    }


def parse_textures_chunk(chunk_data):
    buf = bytearray(chunk_data)
    root = struct.unpack_from('<I', buf, 0)[0]
    textures = Table(buf, root)

    off = textures.Offset(4)  # field 0 → vtable slot 4
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
        lr_off = ts.Offset(4)  # field 0 → vtable slot 4
        if lr_off:
            lru = struct.unpack_from('<I', buf, tsp + lr_off)[0]
            lowres = parse_texture_data(buf, tsp + lr_off + lru)

        highres = None
        hr_off = ts.Offset(6)  # field 1 → vtable slot 6
        if hr_off:
            hru = struct.unpack_from('<I', buf, tsp + hr_off)[0]
            highres = parse_texture_data(buf, tsp + hr_off + hru)

        sets.append({'lowres': lowres, 'highres': highres})

    return sets


# ── Textures chunk building ───────────────────────────────────────────────

def build_textures_chunk(texture_sets):
    builder = flatbuffers.Builder(0)

    ts_offsets = []
    for ts in texture_sets:
        # Build highres
        hr = ts['highres']
        hr_data = builder.CreateByteVector(hr['data'])
        builder.StartObject(6)
        builder.PrependUint8Slot(0, hr['texture_format'], 0)
        builder.PrependUint8Slot(1, hr['pixel_type'], 0)
        builder.PrependUint16Slot(2, hr['width'], 0)
        builder.PrependUint16Slot(3, hr['height'], 0)
        builder.PrependUOffsetTRelativeSlot(4, hr_data, 0)
        hr_off = builder.EndObject()

        # Build lowres (if present)
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

        # Build TextureSet
        builder.StartObject(2)
        if lr_off:
            builder.PrependUOffsetTRelativeSlot(0, lr_off, 0)  # lowres
        builder.PrependUOffsetTRelativeSlot(1, hr_off, 0)      # highres
        ts_offsets.append(builder.EndObject())

    # Build textures vector
    builder.StartVector(4, len(ts_offsets), 4)
    for off in reversed(ts_offsets):
        builder.PrependUOffsetTRelative(off)
    tv = builder.EndVector()

    # Build Textures table
    builder.StartObject(1)
    builder.PrependUOffsetTRelativeSlot(0, tv, 0)
    tt = builder.EndObject()

    builder.Finish(tt)  # NOT size-prefixed (chunk already has size prefix)

    return bytes(builder.Output())


# ── Lowres KTX creation ───────────────────────────────────────────────────

def create_lowres_ktx(highres_png, pvrtex_path):
    """Downscale highres PNG to 50% and convert to KTX."""
    img = Image.open(highres_png)
    lw = img.width // 2
    lh = img.height // 2
    lowres_img = img.resize((lw, lh), Image.LANCZOS)

    lowres_png = "temp_lowres.png"
    lowres_ktx = "temp_lowres.ktx"

    lowres_img.save(lowres_png)
    print(f"  Lowres PNG: {lw}x{lh} → {lowres_png}")

    cmd = [pvrtex_path, '-i', lowres_png, '-o', lowres_ktx, '-f', 'ASTC_6x6,UBN,sRGB']
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        # Try without sRGB
        cmd2 = [pvrtex_path, '-i', lowres_png, '-o', lowres_ktx, '-f', 'ASTC_6x6,UBN']
        result2 = subprocess.run(cmd2, capture_output=True, text=True)
        if result2.returncode != 0:
            print(f"  PVRTexToolCLI error:\n{result.stderr}\n{result2.stderr}")
            return None, lw, lh

    with open(lowres_ktx, 'rb') as f:
        data = f.read()

    # Cleanup
    for p in [lowres_png, lowres_ktx]:
        if os.path.exists(p):
            os.remove(p)

    return data, lw, lh


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 4:
        print("Usage: python add_lowres.py input.sc highres.png output.sc [PVRTexToolCLI.exe]")
        sys.exit(1)

    input_sc   = sys.argv[1]
    highres_png = sys.argv[2]
    output_sc  = sys.argv[3]
    pvrtex     = sys.argv[4] if len(sys.argv) > 4 else 'PVRTexToolCLI.exe'

    # ── 1. Read SC
    sc = read_sc(input_sc)
    print(f"SC version: 0x{sc['version']:08x}")
    print(f"Texture count: {sc['texture_count']}")
    print(f"Textures length: {sc['textures_length']}")
    print(f"Compressed: {sc['compressed']} (size={sc['compressed_size']})")
    print(f"Chunks: {len(sc['chunks'])}")

    # ── 2. Find Textures chunk
    tex_idx = None
    for i, c in enumerate(sc['chunks']):
        if c['size'] == sc['textures_length']:
            tex_idx = i
            break
    if tex_idx is None:
        tex_idx = len(sc['chunks']) - 1  # fallback: last chunk
    print(f"Textures chunk: #{tex_idx} (size={sc['chunks'][tex_idx]['size']} B)")

    # ── 3. Parse existing textures
    sets = parse_textures_chunk(sc['chunks'][tex_idx]['data'])
    for i, ts in enumerate(sets):
        hr = ts['highres']
        lr = ts['lowres']
        hr_info = f"{hr['width']}x{hr['height']} fmt={hr['texture_format']} {len(hr['data'])}B" if hr else "none"
        lr_info = f"{lr['width']}x{lr['height']} fmt={lr['texture_format']} {len(lr['data'])}B" if lr else "(none)"
        print(f"  [{i}] highres: {hr_info}  lowres: {lr_info}")

    if not sets:
        print("ERROR: No texture sets found!")
        sys.exit(1)

    if sets[0]['lowres']:
        print("WARNING: Lowres already exists, will be replaced.")

    # ── 4. Create lowres KTX
    print("\nCreating lowres KTX...")
    ktx_data, lw, lh = create_lowres_ktx(highres_png, pvrtex)
    if ktx_data is None:
        print("ERROR: Failed to create lowres KTX!")
        sys.exit(1)
    print(f"  Lowres KTX: {lw}x{lh}, {len(ktx_data)} B")

    # ── 5. Add lowres to texture set
    hr = sets[0]['highres']
    sets[0]['lowres'] = {
        'texture_format': hr['texture_format'],
        'pixel_type':     hr['pixel_type'],
        'width':          lw,
        'height':         lh,
        'data':           ktx_data,
    }

    # ── 6. Build new Textures chunk
    new_tex_data = build_textures_chunk(sets)
    new_tex_size = len(new_tex_data)
    print(f"\nNew Textures chunk: {new_tex_size} B (was {sc['textures_length']})")

    # ── 7. Rebuild body
    new_body = bytearray()
    for i, c in enumerate(sc['chunks']):
        if i == tex_idx:
            new_body += struct.pack('<I', new_tex_size)
            new_body += new_tex_data
        else:
            new_body += struct.pack('<I', c['size'])
            new_body += c['data']

    # ── 8. Update Header (textures_length + compressed_size)
    header_fb = sc['header_fb']
    header = Table(header_fb, sc['header_root'])

    tl_off = header.Offset(22)  # field 9 → vtable slot 22
    if tl_off:
        struct.pack_into('<I', header_fb, header.Pos + tl_off, new_tex_size)
    else:
        print("WARNING: textures_length field not found in header!")

    cctx = zstandard.ZstdCompressor()
    final_body = cctx.compress(bytes(new_body))

    cs_off = header.Offset(26)  # field 11 → vtable slot 26
    if cs_off:
        struct.pack_into('<I', header_fb, header.Pos + cs_off, len(final_body))
    else:
        print("WARNING: compressed_size field not found in header!")


    # ── 9. Write output
    with open(output_sc, 'wb') as f:
        # SC prefix + version
        f.write(bytes(sc['raw'][:sc['header_start']]))
        # Header (size-prefixed)
        f.write(struct.pack('<I', len(header_fb)))
        f.write(bytes(header_fb))
        # Body
        f.write(final_body)

    print(f"\nWritten: {output_sc} ({os.path.getsize(output_sc)} B)")

    # ── 10. Verify
    print("\nVerifying...")
    sc2 = read_sc(output_sc)
    sets2 = parse_textures_chunk(sc2['chunks'][tex_idx]['data'])
    for i, ts in enumerate(sets2):
        hr = ts['highres']
        lr = ts['lowres']
        hr_info = f"{hr['width']}x{hr['height']} fmt={hr['texture_format']} {len(hr['data'])}B" if hr else "none"
        lr_info = f"{lr['width']}x{lr['height']} fmt={lr['texture_format']} {len(lr['data'])}B" if lr else "(none)"
        print(f"  [{i}] highres: {hr_info}  lowres: {lr_info}")

    if sets2[0]['lowres']:
        print("\nSUCCESS: lowres is present!")
    else:
        print("\nFAILED: lowres not found in output!")


if __name__ == '__main__':
    main()
