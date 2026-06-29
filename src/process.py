"""Image post-processing: flip to face-left + whiten background.

Pillow and rembg are OPTIONAL. If they are not installed the pipeline still
runs and saves the original image; it just records that normalization was
skipped. Install them later with:  pip install pillow rembg
"""
import io

try:
    from PIL import Image
    HAVE_PIL = True
except Exception:
    HAVE_PIL = False

try:
    from rembg import remove as _rembg_remove
    HAVE_REMBG = True
except Exception:
    HAVE_REMBG = False


def normalize(image_bytes, facing="other", want_white_bg=True):
    """Return (out_bytes, ext, notes[]). Flips so head points LEFT; whitens bg."""
    notes = []
    if not HAVE_PIL:
        return image_bytes, _ext(image_bytes), ["pillow-missing: saved raw"]

    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    except Exception as e:
        # bytes Pillow can't decode (e.g. AVIF/SVG/corrupt) -> save raw, don't crash
        return image_bytes, _ext(image_bytes), [f"normalize-skipped:{type(e).__name__}"]

    # 1) background removal -> composite onto white
    if want_white_bg:
        if HAVE_REMBG:
            try:
                cut = _rembg_remove(image_bytes)
                img = Image.open(io.BytesIO(cut)).convert("RGBA")
                notes.append("bg-removed")
            except Exception as e:
                notes.append(f"rembg-failed:{type(e).__name__}")
        else:
            notes.append("rembg-missing: bg not whitened")
        white = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(white, img)

    # 2) flip so the head points left
    if facing == "right":
        img = img.transpose(Image.FLIP_LEFT_RIGHT)
        notes.append("flipped-to-left")

    out = io.BytesIO()
    img.convert("RGB").save(out, format="JPEG", quality=90)
    return out.getvalue(), "jpg", notes


def _ext(b):
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "webp"
    return "jpg"


def is_image(b):
    """True only for real raster image bytes (rejects HTML pages, SVG, etc.)."""
    if not b or len(b) < 64:
        return False
    return (b[:3] == b"\xff\xd8\xff"                      # JPEG
            or b[:8] == b"\x89PNG\r\n\x1a\n"              # PNG
            or (b[:4] == b"RIFF" and b[8:12] == b"WEBP")  # WEBP
            or b[:6] in (b"GIF87a", b"GIF89a"))           # GIF


def prewarm():
    """Create the rembg model session once (single-threaded) so concurrent
    workers don't race to initialise it on first use."""
    if not (HAVE_PIL and HAVE_REMBG):
        return
    try:
        import io as _io
        from PIL import Image as _Image
        buf = _io.BytesIO()
        _Image.new("RGB", (16, 16), (128, 128, 128)).save(buf, format="PNG")
        _rembg_remove(buf.getvalue())
    except Exception:
        pass
