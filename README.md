# SC Texture Tools

Two Python scripts for manipulating textures in `.sc` (SoulCraft?) container files:

- **`add_lowres.py`** – adds a low-resolution texture to an existing SC file, downscaling a given high‑resolution PNG and encoding it with ASTC (via PVRTexTool).
- **`astc_tool.py`** – converts the ASTC block size (e.g., 6×6 → 4×4) of the high‑resolution and/or low‑resolution texture inside an SC file.

Both scripts handle the internal FlatBuffers structure, Zstandard compression, and KTX texture packaging.

---

## Dependencies

- Python 3.6+
- `zstandard` – for decompressing/compressing the SC body.
- `flatbuffers` – for parsing and rebuilding the SC header and texture chunks.
- `Pillow` – **only required for `add_lowres.py`** (image resizing).
- **PVRTexToolCLI** – from Imagination Technologies’ [PVRTexTool](https://developer.imaginationtech.com/pvrtextool/).  
  The executable (`PVRTexToolCLI.exe` on Windows, or `PVRTexToolCLI` on Linux/macOS) must be installed and available in your `PATH` or passed explicitly.

Install Python dependencies:

```bash
pip install zstandard flatbuffers Pillow
```

# add_lowres.py
Adds a low‑resolution texture to the first texture set in an SC file.

## Usage
```bash
python add_lowres.py input.sc highres.png output.sc [PVRTexToolCLI.exe]
```
```input.sc``` – source SC file.

```highres.png``` – high‑resolution PNG image (will be downscaled to 50%).

```output.sc``` – output SC file with the new low‑res texture.

```PVRTexToolCLI.exe``` – (optional) path to the PVRTexTool CLI executable; defaults to PVRTexToolCLI.exe.

## What it does
**Parses the ```**SC**``` file (header, Zstandard‑compressed body, chunks).**

**Locates the textures chunk and parses the existing texture set.**

**Downscales the given PNG to half width/height using Image.LANCZOS.**

**Converts the downscaled PNG to a KTX file with ASTC 6×6 (sRGB) via PVRTexTool.**

**Inserts the new KTX data into the texture set as the low‑resolution entry.**

**Rebuilds the textures chunk, updates header fields (textures_length, compressed_size), re‑compresses the body, and writes the output.**

## Example
```bash
python add_lowres.py my_model.sc diffuse.png my_model_low.sc /opt/pvr/PVRTexToolCLI
```

# astc_tool.py
Converts the **```ASTC```** format (block size) of textures inside an SC file.

## Usage
```bash
python astc_tool.py input.sc output.sc PVRTexToolCLI.exe [highres_fmt] [lowres_fmt]
```
```input.sc``` – source SC file.

```output.sc``` – output SC file.

```PVRTexToolCLI.exe``` – path to the PVRTexTool CLI executable.

```highres_fmt``` – (optional) target ASTC format for the high‑resolution texture, e.g. ASTC_4x4, ASTC_6x6, ASTC_8x8; default: ASTC_4x4.

```lowres_fmt``` – (optional) target ASTC format for the low‑resolution texture; if omitted, uses highres_fmt.

Note: The script adds ,UBN (unorm) to the format string automatically. For sRGB use ,UBN,sRGB but the internal GL format is detected from the KTX header; the script preserves the colour space of the original texture.

## What it does
### Reads and decompresses the SC file.

**Finds the textures chunk and parses all texture sets (highres and lowres).**

**Extracts the KTX data from each texture, parses the KTX header to determine current width/height and GL internal format.**

**For each texture that needs conversion (highres always, lowres if present), calls PVRTexTool to re‑encode to the target ASTC block size.**

**Replaces the texture data and updates width/height fields.**

**Rebuilds the textures chunk, updates the SC header, re‑compresses, and writes the output.**

## Example
Convert highres from 6×6 to 4×4, leave lowres unchanged (if present):

```bash
python astc_tool.py game.sc game_converted.sc PVRTexToolCLI.exe ASTC_4x4
```

Convert both highres and lowres to 8×8:

```bash
python astc_tool.py game.sc game_converted.sc PVRTexToolCLI.exe ASTC_8x8 ASTC_8x8
```
## Notes
Both scripts assume the SC file contains exactly one texture set (the first one). They only modify the first texture set.

The internal FlatBuffers schema is not publicly documented; the scripts are based on reverse‑engineered offsets and may not work for all SC variants.

The scripts always re‑compress the body with Zstandard, even if the original was uncompressed.

Temporary files (e.g., temp_lowres.png, temp_*.ktx) are automatically cleaned up.

If PVRTexTool fails (e.g., missing executable), the script will exit with an error.