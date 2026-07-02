#!/usr/bin/env python3
"""Cut the background out with rembg (birefnet-general) and fit the subject onto
a white catalogue frame. Shared by app.py and the worker.

alpha-matting + mask post-processing were tried (see bg_lab.py) and dropped --
no visible gain over a plain birefnet mask plus the light edge feather below.
"""
from rembg import remove as _rembg_remove, new_session
from PIL import Image, ImageFilter, ImageChops
import io
import os

# ---- recipe knobs -----------------------------------------------------------
REMBG_MODEL = os.environ.get("REMBG_MODEL", "birefnet-general")
# Erode the alpha a couple px to eat the leftover bg fringe, then blur it so the
# edge anti-aliases onto white instead of stair-stepping.
EDGE_ERODE = 2        # px pulled inward off the subject edge
EDGE_FEATHER = 1.5    # gaussian blur radius applied to the alpha mask
CANVAS = (600, 470)   # final frame size; subject fitted, rest padded white


# ---- raw-bytes helpers ------------------------------------------------------
def is_image(b):
    """True only for real raster image bytes (rejects HTML pages, SVG, etc.)."""
    if not b or len(b) < 64:
        return False
    return (b[:3] == b"\xff\xd8\xff"                      # JPEG
            or b[:8] == b"\x89PNG\r\n\x1a\n"              # PNG
            or (b[:4] == b"RIFF" and b[8:12] == b"WEBP")  # WEBP
            or b[:6] in (b"GIF87a", b"GIF89a"))           # GIF


def _ext(b):
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "webp"
    return "jpg"


# ---- background removal (rembg) ---------------------------------------------
_session = None


def session():
    """The rembg session, made once and reused (first call loads the ~1GB model)."""
    global _session
    if _session is None:
        _session = new_session(REMBG_MODEL)
    return _session


def _smooth_alpha(img):
    """Erode + feather the alpha so the cut-out edge sits cleanly on white.
    Runs at full resolution, before any down-scale, for the softest edge."""
    a = img.getchannel("A")
    if EDGE_ERODE > 0:
        a = a.filter(ImageFilter.MinFilter(EDGE_ERODE * 2 + 1))
    if EDGE_FEATHER > 0:
        a = a.filter(ImageFilter.GaussianBlur(EDGE_FEATHER))
    img.putalpha(a)
    return img


def _cut_out(img):  # PIL in/out -> smoothed transparent RGBA cut-out (bg removed)
    cut = _rembg_remove(img, session=session()).convert("RGBA")
    return _smooth_alpha(cut)


# ---- framing geometry (Pillow) ----------------------------------------------
def _trim_to_subject(img, bbox):
    """Crop img to bbox plus a small proportional margin. bbox must be truthy."""
    m = round(0.03 * max(img.width, img.height))
    l, t, r, b = bbox
    return img.crop((max(0, l - m), max(0, t - m),
                     min(img.width, r + m), min(img.height, b + m)))


def _fit_on_white(img):
    """Scale an RGB image to fit CANVAS and center it on white, padding the rest
    (never distorts). Callers flatten any alpha onto white first."""
    w, h = CANVAS
    scale = min(w / img.width, h / img.height)
    nw = max(1, round(img.width * scale))
    nh = max(1, round(img.height * scale))
    img = img.resize((nw, nh), Image.LANCZOS)
    bg = Image.new("RGB", (w, h), (255, 255, 255))
    bg.paste(img, ((w - nw) // 2, (h - nh) // 2))
    return bg


def _nonwhite_bbox(img, thresh=12):
    """Bounding box of the non-white content (how adjust_frame finds the subject)."""
    rgb = img.convert("RGB")
    bg = Image.new("RGB", rgb.size, (255, 255, 255))
    diff = ImageChops.difference(rgb, bg).convert("L")
    diff = diff.point(lambda p: 255 if p > thresh else 0)
    return diff.getbbox()


# ---- build / re-frame a catalogue frame -------------------------------------
def make_frame(image_bytes):
    """Raw photo -> finished frame: (out_bytes, ext, notes). Cut out the
    background, trim to the subject, fit centered on white. Anything that can't
    produce a real frame (bad bytes, rembg failure, empty cut-out) raises, so
    process_one errors the job instead of saving a half-made or blank one."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    img = _cut_out(img)

    bbox = img.getchannel("A").getbbox()
    if not bbox:
        raise ValueError("empty cut-out: rembg found no subject")
    img = _trim_to_subject(img, bbox)

    # flatten onto white using the cut-out's own alpha (blends the feathered
    # edges against white, no dark halo), then fit centered on the frame.
    white = Image.new("RGB", img.size, (255, 255, 255))
    white.paste(img, (0, 0), img)
    img = _fit_on_white(white)

    out = io.BytesIO()
    # q100, 4:4:4: don't re-soften the edges we just feathered.
    img.save(out, format="JPEG", quality=100, subsampling=0)
    return out.getvalue(), "jpg", [f"bg-removed:{REMBG_MODEL}", "trimmed"]


def adjust_frame(image_bytes, flip=False, rotate=0, trim=False):
    """Re-frame an already-finished (white-bg) frame: rotate / trim / flip, no
    rembg. Finds the subject by its non-white box since there's no alpha left."""
    notes = []
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception as e:
        return image_bytes, _ext(image_bytes), [f"adjust-skipped:{type(e).__name__}"]

    if rotate:
        img = img.rotate(-rotate, resample=Image.BICUBIC, expand=True,
                         fillcolor=(255, 255, 255))
        notes.append(f"rotated:{rotate}")

    if trim:
        bbox = _nonwhite_bbox(img)
        if bbox:
            img = _trim_to_subject(img, bbox)
            notes.append("trimmed")

    img = _fit_on_white(img)

    if flip:
        img = img.transpose(Image.FLIP_LEFT_RIGHT)
        notes.append("flipped-to-left")

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=100, subsampling=0)
    return out.getvalue(), "jpg", notes
