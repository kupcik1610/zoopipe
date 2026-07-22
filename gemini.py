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
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3-pro-image")
GEMINI_ENDPOINT = os.environ.get(
    "GEMINI_ENDPOINT", "https://generativelanguage.googleapis.com/v1beta")
# Output resolution. The model has no exact-pixel option -- it only emits fixed
# aspect-ratio / size tiers, then imaging._fit_on_white downscales to the 600x470
# CANVAS. 5:4 (1.25) is the closest ratio to 600:470 (~1.28), so padding is
# minimal. 1K and 2K bill identically (1120 output tokens); 4K costs ~2x. So we
# take the free upgrade to 2K and downscale ourselves. Override via env.
GEMINI_ASPECT_RATIO = os.environ.get("GEMINI_ASPECT_RATIO", "5:4")
GEMINI_IMAGE_SIZE = os.environ.get("GEMINI_IMAGE_SIZE", "2K")  # 1K|2K same price; 4K ~2x
# Let the model check what a real <latin_name> looks like via Google Search
# before drawing, so colours/markings match the actual species. Set 0 to disable.
GEMINI_GROUNDING = os.environ.get("GEMINI_GROUNDING", "1").strip().lower() \
    not in ("0", "false", "no", "off", "")
# Push near-white pixels (all channels >= 255 - this) to pure #FFFFFF so the
# generated background pads seamlessly onto the white frame. Kept tight so it
# never bleaches genuinely-light animals. 0 disables.
WHITE_CLAMP = int(os.environ.get("GEMINI_WHITE_CLAMP", "10"))
TIMEOUT = int(os.environ.get("GEMINI_TIMEOUT", "120"))

# ---- Vertex AI (Google Cloud credits) ---------------------------------------
GEMINI_BACKEND = os.environ.get("GEMINI_BACKEND", "").strip().lower()  # ""|vertex|aistudio
VERTEX_PROJECT = (os.environ.get("VERTEX_PROJECT")
                  or os.environ.get("GOOGLE_CLOUD_PROJECT") or "")
VERTEX_LOCATION = os.environ.get("VERTEX_LOCATION") or "global"  # "" -> global

_MIME = {"png": "image/png", "webp": "image/webp", "jpg": "image/jpeg"}


def _use_vertex():
    if GEMINI_BACKEND == "vertex":
        return True
    if GEMINI_BACKEND == "aistudio":
        return False
    return bool(VERTEX_PROJECT)  # auto: Vertex as soon as a Cloud project is set


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
        _token.update(value=d["access_token"], exp=now + int(d.get("expires_in", 3600)))
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
            raise RuntimeError("VERTEX_PROJECT (or GOOGLE_CLOUD_PROJECT) not set")
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
           f"the {n_refs} attached reference photos (all the same species)")
    return (
        f"Look up the animal whose scientific (Latin) name is '{latin_name}' and "
        f"recall exactly what a real one looks like -- its true colouration, "
        f"markings, pattern, texture and body proportions. Then generate a "
        f"realistic product-catalogue photograph of a single, live {latin_name}.\n"
        f"Use {ref} as a loose visual guide, NOT a template to copy exactly. They "
        f"are a rough outline of what to look for: lean on them for the lighting, "
        f"the specific colour and markings of this animal, and its overall look "
        f"and feel. Where a reference is unclear, cropped or atypical for the "
        f"species, defer to how a real {latin_name} actually looks. Keep the "
        f"species identity faithful and accurate; do not restyle or 'beautify' "
        f"the colours or pattern.\n"
        f"Requirements:\n"
        f"- One whole animal: complete full body, centred and fully in frame -- "
        f"whole tail, all limbs, both eyes. Extend it naturally if the references "
        f"are cropped or the animal is curled, folded up or turned away.\n"
        f"- Correct anatomy: the right number of natural, well-formed "
        f"fingers / toes / claws and limbs -- none missing, extra, fused, "
        f"bent-back or hidden.\n"
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
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
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
        raise RuntimeError(f"Gemini returned no candidates{f' (blocked: {fb})' if fb else ''}")

    parts = candidates[0].get("content", {}).get("parts") or []
    text_bits = []
    for part in parts:
        inline = part.get("inline_data") or part.get("inlineData")
        if inline and inline.get("data"):
            return base64.b64decode(inline["data"])
        if part.get("text"):
            text_bits.append(part["text"].strip())
    note = " ".join(text_bits)[:200]
    raise RuntimeError(f"Gemini returned no image{f': {note}' if note else ''}")


def _flatten_white(img):
    """Snap near-white pixels to pure white so the bg pads seamlessly. Only
    touches pixels whose darkest channel is already near 255, leaving genuinely
    coloured (even bright) pixels alone."""
    if WHITE_CLAMP <= 0:
        return img
    r, g, b = img.split()
    darkest = ImageChops.darker(ImageChops.darker(r, g), b)
    mask = darkest.point(lambda p: 255 if p >= 255 - WHITE_CLAMP else 0)
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
