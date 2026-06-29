"""Shared google-genai client.

Supports two backends, chosen by config:
  - Vertex AI  (spends your $300 GCP credits)  -> USE_VERTEX=true + project/location + ADC
  - Gemini Developer API (AI Studio key)       -> GEMINI_API_KEY
Returns None if neither is configured (pipeline then runs ungraded / no gap-fill).
"""
import warnings
warnings.filterwarnings("ignore")  # silence py3.9-EOL / LibreSSL FutureWarnings

from . import config

_client = None
_tried = False

def get_client():
    global _client, _tried
    if _tried:
        return _client
    _tried = True
    try:
        from google import genai
        from google.genai import types
        # hard timeout so one slow/hung request can't freeze the whole batch
        http_opts = types.HttpOptions(timeout=45000)  # milliseconds
        if config.USE_VERTEX and config.GCP_PROJECT:
            _client = genai.Client(vertexai=True, project=config.GCP_PROJECT,
                                   location=config.GCP_LOCATION, http_options=http_opts)
        elif config.GEMINI_API_KEY:
            _client = genai.Client(api_key=config.GEMINI_API_KEY, http_options=http_opts)
        else:
            _client = None
    except Exception as e:
        print(f"[client] could not init google-genai: {type(e).__name__}: {e}")
        _client = None
    return _client

def backend_name():
    if config.USE_VERTEX and config.GCP_PROJECT:
        return f"vertex:{config.GCP_PROJECT}/{config.GCP_LOCATION}"
    if config.GEMINI_API_KEY:
        return "gemini-dev-api"
    return "none"
