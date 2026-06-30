#!/usr/bin/env python3
"""Image processing shared by the web app (app.py) and the batch worker
(worker.py): background removal + normalize onto a white catalogue plate.

Background removal uses rembg. The model + options below are the quality recipe
validated in bg_lab.py -- birefnet-general gives the cleanest fins/edges, with
alpha-matting + mask post-processing to tidy soft edges and speckles. Flip these
constants if you want to trade quality for speed (e.g. isnet-general-use).
"""
import io, os

# ---- the recipe (tweak here) ------------------------------------------------
REMBG_MODEL = os.environ.get("REMBG_MODEL", "birefnet-general")
USE_ALPHA_MATTING = os.environ.get("REMBG_ALPHA", "1") != "0"
POST_PROCESS_MASK = os.environ.get("REMBG_POSTPROCESS", "1") != "0"
_AM = dict(
    alpha_matting_foreground_threshold=270,
    alpha_matting_background_threshold=20,
    alpha_matting_erode_size=11,
)
CANVAS = (600, 470)            # final plate size; subject fitted, rest padded white

# Pillow + rembg are OPTIONAL: without them callers fall back to saving raw.
try:
    from PIL import Image
    HAVE_PIL = True
except Exception:
    HAVE_PIL = False

try:
    from rembg import remove as _rembg_remove, new_session
    HAVE_REMBG = True
except Exception:
    HAVE_REMBG = False

_session = None


def session():
    """The rembg model session, created once and reused (model loads on first
    use; birefnet-general also downloads ~1GB the very first time)."""
    global _session
    if _session is None:
        _session = new_session(REMBG_MODEL)
    return _session


def _cut_out(image_bytes):
    """Run the bg-removal recipe -> transparent RGBA cut-out bytes (PNG)."""
    kw = {"session": session()}
    if USE_ALPHA_MATTING:
        kw["alpha_matting"] = True
        kw.update(_AM)
    if POST_PROCESS_MASK:
        kw["post_process_mask"] = True
    return _rembg_remove(image_bytes, **kw)


# ---- image processing: cut out -> fit on white canvas -> flip ---------------
def normalize(image_bytes, flip=False, rotate=0, trim=False):
    """Return (out_bytes, ext, notes[]).

    Pipeline: remove background -> rotate (degrees, clockwise) -> trim to the
    subject's bounding box -> fit onto a fixed CANVAS-sized white background ->
    flip so the head points LEFT. Rotating/trimming happen on the transparent
    cut-out so corners stay clean and the crop hugs the animal. The subject is
    scaled proportionally and centered, leftover space padded white -- it never
    distorts, an aspect-ratio mismatch just adds white.
    """
    notes = []
    if not HAVE_PIL:
        return image_bytes, _ext(image_bytes), ["pillow-missing: saved raw"]

    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    except Exception as e:
        # bytes Pillow can't decode (e.g. AVIF/SVG/corrupt) -> save raw, don't crash
        return image_bytes, _ext(image_bytes), [f"normalize-skipped:{type(e).__name__}"]

    # 1) background removal -> transparent RGBA cut-out
    has_alpha = False
    if HAVE_REMBG:
        try:
            cut = _cut_out(image_bytes)
            img = Image.open(io.BytesIO(cut)).convert("RGBA")
            has_alpha = True
            notes.append(f"bg-removed:{REMBG_MODEL}")
        except Exception as e:
            notes.append(f"rembg-failed:{type(e).__name__}")
    else:
        notes.append("rembg-missing: bg not whitened")

    # 2) rotate on the transparent canvas (CSS-style: positive = clockwise)
    if rotate:
        img = img.rotate(-rotate, resample=Image.BICUBIC, expand=True)
        notes.append(f"rotated:{rotate}")

    # 3) trim to the subject's bounding box (+ small proportional margin)
    if trim:
        bbox = img.getchannel("A").getbbox() if has_alpha else _nonwhite_bbox(img)
        if bbox:
            m = round(0.03 * max(img.width, img.height))
            l, t, r, b = bbox
            img = img.crop((max(0, l - m), max(0, t - m),
                            min(img.width, r + m), min(img.height, b + m)))
            notes.append("trimmed")

    # 4) fit the cut-out onto a fixed white canvas, scaled + centered.
    #    Compositing over OPAQUE white blends edges against white (no dark halo),
    #    so the later convert("RGB") is a no-op on an already-opaque image.
    w, h = CANVAS
    scale = min(w / img.width, h / img.height)
    nw, nh = max(1, round(img.width * scale)), max(1, round(img.height * scale))
    img = img.resize((nw, nh), Image.LANCZOS)
    bg = Image.new("RGBA", (w, h), (255, 255, 255, 255))
    bg.alpha_composite(img, ((w - nw) // 2, (h - nh) // 2))
    img = bg

    # 5) flip so the head points left
    if flip:
        img = img.transpose(Image.FLIP_LEFT_RIGHT)
        notes.append("flipped-to-left")

    out = io.BytesIO()
    img.convert("RGB").save(out, format="JPEG", quality=90)
    return out.getvalue(), "jpg", notes


def retouch(image_bytes, flip=False, rotate=0, trim=False):
    """Re-frame an ALREADY background-removed plate: rotate / trim / flip back
    onto the white canvas. Pure geometry -- no rembg, so it's instant and never
    competes with the worker for CPU. The plate is already on white, so rotation
    just fills new corners white and trim finds the subject by its non-white box.
    """
    notes = []
    if not HAVE_PIL:
        return image_bytes, _ext(image_bytes), ["pillow-missing: saved raw"]
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception as e:
        return image_bytes, _ext(image_bytes), [f"retouch-skipped:{type(e).__name__}"]

    if rotate:
        img = img.rotate(-rotate, resample=Image.BICUBIC, expand=True,
                         fillcolor=(255, 255, 255))
        notes.append(f"rotated:{rotate}")

    if trim:
        bbox = _nonwhite_bbox(img)
        if bbox:
            m = round(0.03 * max(img.width, img.height))
            l, t, r, b = bbox
            img = img.crop((max(0, l - m), max(0, t - m),
                            min(img.width, r + m), min(img.height, b + m)))
            notes.append("trimmed")

    w, h = CANVAS
    scale = min(w / img.width, h / img.height)
    nw, nh = max(1, round(img.width * scale)), max(1, round(img.height * scale))
    img = img.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("RGB", (w, h), (255, 255, 255))
    canvas.paste(img, ((w - nw) // 2, (h - nh) // 2))
    img = canvas

    if flip:
        img = img.transpose(Image.FLIP_LEFT_RIGHT)
        notes.append("flipped-to-left")

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=90)
    return out.getvalue(), "jpg", notes


def _nonwhite_bbox(img, thresh=12):
    """Bounding box of the non-white content (fallback when there's no alpha)."""
    from PIL import ImageChops
    rgb = img.convert("RGB")
    bg = Image.new("RGB", rgb.size, (255, 255, 255))
    diff = ImageChops.difference(rgb, bg).convert("L").point(lambda p: 255 if p > thresh else 0)
    return diff.getbbox()


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


def warm():
    """Load the model once so the first real job doesn't pay init cost mid-batch."""
    if not (HAVE_PIL and HAVE_REMBG):
        return
    try:
        buf = io.BytesIO()
        Image.new("RGB", (16, 16), (128, 128, 128)).save(buf, format="PNG")
        _rembg_remove(buf.getvalue(), session=session())
    except Exception:
        pass
