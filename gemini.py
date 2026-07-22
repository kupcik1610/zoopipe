#!/usr/bin/env python3
"""Build a catalogue frame with Gemini image generation.

We hand the source photo to Gemini as a *reference* and ask it to generate a
fresh, clean studio image of the same animal (by its Latin name) on a pure-white
background. The result is then normalised onto the white CANVAS the rest of the
pipeline uses, so finished frames edit / upload just like any other.

Two backends, same request/response shape (plain REST over urllib, no extra
dependency):

  * AI Studio (default) -- endpoint generativelanguage.googleapis.com, auth via
    an API key in GEMINI_API_KEY. Billed to the AI Studio project.
  * Vertex AI -- endpoint {loc}-aiplatform.googleapis.com, auth via a Google
    Cloud OAuth token. Billed to your Cloud project, so *Google Cloud free
    credits apply here*. Selected automatically when VERTEX_PROJECT is set (or
    force it with GEMINI_BACKEND=vertex).

Model defaults to gemini-3-pro-image ("Nano Banana Pro"); override with
GEMINI_MODEL (e.g. gemini-3.1-flash-image for a faster/cheaper run, or
gemini-2.5-flash-image). All of them share this generateContent API and accept
reference images, so switching is just the env var. (Imagen models are
deprecated / shutting down and are text-only -- don't use them here.)
"""
import base64
import io
import json
import os
import subprocess
import time
import urllib.error
import urllib.request

from PIL import Image, ImageChops

import imaging  # reuse CANVAS + _fit_on_white for a consistent white frame

# ---- config knobs -----------------------------------------------------------
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-image")
GEMINI_ENDPOINT = os.environ.get(
    "GEMINI_ENDPOINT", "https://generativelanguage.googleapis.com/v1beta")
# Output resolution. The model has no exact-pixel option -- it only emits fixed
# aspect-ratio / size tiers, then imaging._fit_on_white downscales to the 600x470
# CANVAS. 5:4 (1.25) is the closest ratio to 600:470 (~1.28), so padding is
# minimal. 1K and 2K bill identically (1120 output tokens; 4K costs ~2x), but 1K
# generates noticeably faster and is already far more detail than a 600x470 frame
# needs, so downscaling from 1K loses nothing. Override via env.
GEMINI_ASPECT_RATIO = os.environ.get("GEMINI_ASPECT_RATIO", "5:4")
# 1K|2K same price; 1K is faster
GEMINI_IMAGE_SIZE = os.environ.get("GEMINI_IMAGE_SIZE", "1K")
# Let the model check what a real <latin_name> looks like via Google Search
# before drawing, so colours/markings match the actual species. Set 0 to disable.
GEMINI_GROUNDING = os.environ.get("GEMINI_GROUNDING", "1").strip().lower() \
    not in ("0", "false", "no", "off", "")
# The generated background is measured from the image corners (which are always
# background) and any pixel within BG_TOL of that measured colour is snapped to
# pure #FFFFFF -- this kills the slight tint Gemini leaves without bleaching the
# subject, which sits far from the background colour. BG_CORNER is the fraction
# of each corner sampled to measure the background. GEMINI_BG_TOL=0 disables.
BG_TOL = int(os.environ.get("GEMINI_BG_TOL", "20"))
BG_CORNER = float(os.environ.get("GEMINI_BG_CORNER", "0.06"))
TIMEOUT = int(os.environ.get("GEMINI_TIMEOUT", "120"))

# ---- Vertex AI (Google Cloud credits) ---------------------------------------
GEMINI_BACKEND = os.environ.get(
    "GEMINI_BACKEND", "").strip().lower()  # ""|vertex|aistudio
VERTEX_PROJECT = (os.environ.get("VERTEX_PROJECT")
                  or os.environ.get("GOOGLE_CLOUD_PROJECT") or "")
VERTEX_LOCATION = os.environ.get("VERTEX_LOCATION") or "global"  # "" -> global

_MIME = {"png": "image/png", "webp": "image/webp", "jpg": "image/jpeg"}


def _use_vertex():
    if GEMINI_BACKEND == "vertex":
        return True
    if GEMINI_BACKEND == "aistudio":
        return False
    # auto: Vertex as soon as a Cloud project is set
    return bool(VERTEX_PROJECT)


def api_key():
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise RuntimeError(
            "GEMINI_API_KEY not set (get one at https://aistudio.google.com/apikey)")
    return key


_token = {"value": None, "exp": 0.0}


def _access_token():
    """A Google Cloud OAuth token for Vertex, cached until ~1 min before expiry.
    Tries, in order: GOOGLE_ACCESS_TOKEN, the GCE metadata server (free on the
    VM), then local `gcloud auth print-access-token`."""
    override = os.environ.get("GOOGLE_ACCESS_TOKEN")
    if override:
        return override
    now = time.time()
    if _token["value"] and now < _token["exp"] - 60:
        return _token["value"]

    # 1) GCE / Cloud Run metadata server -- the VM's service account, no setup.
    try:
        req = urllib.request.Request(
            "http://metadata.google.internal/computeMetadata/v1/"
            "instance/service-accounts/default/token",
            headers={"Metadata-Flavor": "Google"})
        with urllib.request.urlopen(req, timeout=5) as r:
            d = json.loads(r.read())
        _token.update(value=d["access_token"], exp=now +
                      int(d.get("expires_in", 3600)))
        return _token["value"]
    except Exception:
        pass

    # 2) Local dev: whatever `gcloud auth login` is signed in as.
    try:
        out = subprocess.run(["gcloud", "auth", "print-access-token"],
                             capture_output=True, text=True, timeout=30)
        if out.returncode == 0 and out.stdout.strip():
            _token.update(value=out.stdout.strip(), exp=now + 3300)
            return _token["value"]
    except Exception:
        pass

    raise RuntimeError(
        "no Vertex access token: set GOOGLE_ACCESS_TOKEN, run on a GCE VM, or "
        "sign in locally with `gcloud auth login`")


def _endpoint_and_headers():
    """(url, headers) for the active backend."""
    if _use_vertex():
        if not VERTEX_PROJECT:
            raise RuntimeError(
                "VERTEX_PROJECT (or GOOGLE_CLOUD_PROJECT) not set")
        host = ("aiplatform.googleapis.com" if VERTEX_LOCATION == "global"
                else f"{VERTEX_LOCATION}-aiplatform.googleapis.com")
        url = (f"https://{host}/v1/projects/{VERTEX_PROJECT}/locations/"
               f"{VERTEX_LOCATION}/publishers/google/models/"
               f"{GEMINI_MODEL}:generateContent")
        return url, {"Content-Type": "application/json",
                     "Authorization": f"Bearer {_access_token()}"}
    url = f"{GEMINI_ENDPOINT}/models/{GEMINI_MODEL}:generateContent"
    return url, {"Content-Type": "application/json", "x-goog-api-key": api_key()}


def _prompt(latin_name, n_refs):
    ref = ("the attached reference photo" if n_refs == 1 else
           f"the {n_refs} attached reference photos (all the same animal)")
    return (
        f"Generate a realistic product-catalogue photograph of a single, live "
        f"{latin_name}, using {ref} as the AUTHORITATIVE source for what this "
        f"specific animal looks like.\n"
        f"Colour is decided by the photo, not by the species in general. "
        f"Reproduce the exact colouration, markings, pattern, shading and texture "
        f"shown in the reference as faithfully as you can, and treat it as this "
        f"individual's true appearance even if it is unusual or atypical for a "
        f"'{latin_name}'. Do NOT substitute the generic or textbook colours of the "
        f"species, and do NOT restyle, recolour, brighten, saturate or 'beautify' "
        f"it. If the reference disagrees with how a typical {latin_name} looks, the "
        f"reference wins.\n"
        f"Use your own knowledge of {latin_name} ONLY to complete and correct the "
        f"animal where the photo does not show it: if the reference is cropped, "
        f"partial, folded, curled or turned away, extend THE SAME animal -- same "
        f"colours, same markings -- into a full, natural, anatomically-correct "
        f"body. Never invent a differently-coloured or differently-patterned "
        f"animal to fill in the missing parts.\n"
        f"Requirements:\n"
        f"- Exactly ONE individual animal, with exactly ONE head and ONE tail. "
        f"Never two heads, never a second head where the tail should be, never a "
        f"duplicated or split body. Complete full body, centred and fully in "
        f"frame -- whole tail, all limbs, both eyes. Extend it naturally if the "
        f"references are cropped or the animal is curled, folded up or turned "
        f"away.\n"
        f"- Correct anatomy: the right number of natural, well-formed "
        f"fingers / toes / claws and limbs -- none missing, extra, fused, "
        f"bent-back or hidden.\n"
        f"- A physically solid, continuous body that never intersects, clips "
        f"through or passes into itself or another limb. Where the body overlaps "
        f"itself, one part rests convincingly on top of the other, never merging "
        f"or tunnelling through it.\n"
        f"- If this is a snake, eel, worm or other long, legless animal: draw it "
        f"as a SINGLE continuous tube with one clear head at one end and a single "
        f"tapering tail at the other. Pose it simply and readably -- gently "
        f"stretched out or in ONE loose, open loop -- NOT a dense pile of "
        f"overlapping coils, so the entire length can be followed unbroken from "
        f"head to tail and the body never crosses through itself. When in doubt, "
        f"prefer a simpler, more stretched-out pose over a tightly coiled one.\n"
        f"- A relaxed, natural pose and even, flattering lighting.\n"
        f"- A genuine, sharp photograph -- NOT an illustration, painting or 3D "
        f"render.\n"
        f"Background: pure solid white (#FFFFFF), seamless -- no floor, no shadow, "
        f"no props, no border, no text or watermark."
    )


def _call_gemini(images, latin_name):
    """POST the reference photo(s) + prompt, return the first generated image's
    raw bytes. Raises on HTTP error, refusal, or an imageless response."""
    parts = [{"text": _prompt(latin_name, len(images))}]
    for image_bytes in images:
        parts.append({"inline_data": {
            "mime_type": _MIME.get(imaging._ext(image_bytes), "image/jpeg"),
            "data": base64.b64encode(image_bytes).decode("ascii"),
        }})
    # role is required by Vertex ("user"/"model"); AI Studio tolerates it too.
    request = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            # Grounding emits a short text reasoning step alongside the image, so
            # allow both modalities; the parse loop below returns the image part
            # and ignores any text.
            "responseModalities": ["TEXT", "IMAGE"],
            "imageConfig": {
                "aspectRatio": GEMINI_ASPECT_RATIO,
                "imageSize": GEMINI_IMAGE_SIZE,
            },
        },
    }
    if GEMINI_GROUNDING:
        request["tools"] = [{"google_search": {}}]  # camel/snake both accepted
    body = json.dumps(request).encode("utf-8")

    url, headers = _endpoint_and_headers()
    req = urllib.request.Request(
        url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            resp = json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read()).get("error", {}).get("message", "")
        except Exception:
            pass
        raise RuntimeError(f"Gemini HTTP {e.code}: {detail or e.reason}")

    candidates = resp.get("candidates") or []
    if not candidates:
        fb = resp.get("promptFeedback", {}).get("blockReason")
        raise RuntimeError(
            f"Gemini returned no candidates{f' (blocked: {fb})' if fb else ''}")

    parts = candidates[0].get("content", {}).get("parts") or []
    text_bits = []
    for part in parts:
        inline = part.get("inline_data") or part.get("inlineData")
        if inline and inline.get("data"):
            return base64.b64decode(inline["data"])
        if part.get("text"):
            text_bits.append(part["text"].strip())
    note = " ".join(text_bits)[:200]
    raise RuntimeError(
        f"Gemini returned no image{f': {note}' if note else ''}")


def _channel_median(hist):
    """Median value (0-255) of a single-channel histogram."""
    total = sum(hist)
    if not total:
        return 255
    half, acc = total / 2, 0
    for value, count in enumerate(hist):
        acc += count
        if acc >= half:
            return value
    return 255


def _bg_color(img):
    """Estimate the true background colour from the four corners. Uses the
    per-channel median across all four corner patches, so a corner that happens
    to clip the subject can't skew the result (needs <50% subject to stay
    robust, which centred animal frames comfortably satisfy)."""
    w, h = img.size
    cw = max(1, int(w * BG_CORNER))
    ch = max(1, int(h * BG_CORNER))
    corners = [(0, 0, cw, ch), (w - cw, 0, w, ch),
               (0, h - ch, cw, h), (w - cw, h - ch, w, h)]
    patch = Image.new("RGB", (cw * 2, ch * 2))
    for box, pos in zip(corners, [(0, 0), (cw, 0), (0, ch), (cw, ch)]):
        patch.paste(img.crop(box), pos)
    return tuple(_channel_median(band.histogram()) for band in patch.split())


def _flatten_white(img):
    """Measure the real background colour from the corners and snap every pixel
    within BG_TOL of it (per-channel) to pure #FFFFFF. This removes any tint --
    and its gradient/noise -- while leaving the subject untouched, since the
    subject sits far from the measured background colour."""
    if BG_TOL <= 0:
        return img
    bg = _bg_color(img)
    dist = None
    for band, level in zip(img.split(), bg):
        d = ImageChops.difference(band, Image.new("L", img.size, level))
        dist = d if dist is None else ImageChops.lighter(
            dist, d)  # max abs diff
    mask = dist.point(lambda p: 255 if p <= BG_TOL else 0)
    white = Image.new("RGB", img.size, (255, 255, 255))
    return Image.composite(white, img, mask)


def make_frame(images, latin_name):
    """Reference photo(s) + Latin name -> finished frame: (out_bytes, ext, notes).

    `images` is one image's bytes or a list of them (all references for the same
    animal, sent together in one prompt). Raises on any failure (bad key,
    refusal, no image) so worker.process_one errors the job instead of saving a
    blank frame."""
    if isinstance(images, (bytes, bytearray)):
        images = [images]
    images = [im for im in images if im]
    if not images:
        raise ValueError("no reference image to generate from")
    latin_name = (latin_name or "").strip()
    if not latin_name:
        raise ValueError("no Latin name to generate from")

    gen = _call_gemini(images, latin_name)
    if not imaging.is_image(gen):
        raise ValueError("Gemini response was not a valid image")

    img = Image.open(io.BytesIO(gen)).convert("RGB")
    img = _flatten_white(img)
    img = imaging._fit_on_white(img)  # normalise to the catalogue CANVAS

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=100, subsampling=0)
    notes = [f"gemini:{GEMINI_MODEL}", f"latin:{latin_name}",
             f"res:{GEMINI_IMAGE_SIZE}@{GEMINI_ASPECT_RATIO}"]
    if GEMINI_GROUNDING:
        notes.append("grounded")
    if len(images) > 1:
        notes.append(f"refs:{len(images)}")
    return out.getvalue(), "jpg", notes
