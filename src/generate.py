"""Imagen gap-fill generation via google-genai (Vertex AI or Gemini Dev API).

Used ONLY for species where no acceptable real photo was found. Output is a
representative image (NOT guaranteed to be the exact species) on white bg,
facing left -- flagged 'ai_generated' in the manifest, never passed off as real.
"""
from . import config
from .client import get_client

def generate(latin, sk):
    client = get_client()
    if client is None:
        return None
    from google.genai import types
    try:
        resp = client.models.generate_images(
            model=config.IMAGEN_MODEL,
            prompt=config.imagen_prompt(latin, sk),
            config=types.GenerateImagesConfig(number_of_images=1, aspect_ratio="4:3"),
        )
        imgs = getattr(resp, "generated_images", None) or []
        if not imgs:
            return None
        return imgs[0].image.image_bytes
    except Exception as e:
        print(f"[imagen] {latin}: {type(e).__name__}: {str(e)[:120]}")
        return None
