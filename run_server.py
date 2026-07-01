#!/usr/bin/env python3
"""Friendly launcher (this is what START.bat runs, or double-click it directly).

Differences from `python app.py`:
  * debug reloader is OFF -- one clean process, no double-spawned workers
  * opens your browser automatically
  * on the very FIRST launch it kicks off a background worker so the ~1 GB
    background-removal model downloads right away, instead of stalling the
    first time you press Process.
"""
import os, sys, threading, webbrowser, subprocess

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE_DIR, "out")

# local default; on the server systemd sets HOST/PORT and OPEN_BROWSER=0
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "5001"))
OPEN_BROWSER = os.environ.get("OPEN_BROWSER", "1") != "0"
URL = f"http://{HOST}:{PORT}"

# keep the downloaded model inside this project folder (START.bat sets this too;
# setdefault means running this file on its own still stays self-contained).
os.environ.setdefault("U2NET_HOME", os.path.join(BASE_DIR, "models"))


def _model_file():
    import imaging
    home = os.environ.get("U2NET_HOME") or os.path.join(os.path.expanduser("~"), ".u2net")
    return os.path.join(home, f"{imaging.REMBG_MODEL}.onnx")


def _prefetch_model():
    """A worker with an empty queue still 'warms' (= downloads) the model and
    then exits -- so the big one-time download happens now, in the background,
    right after first launch rather than during the first Process click."""
    if os.path.isfile(_model_file()):
        return
    os.makedirs(OUT_DIR, exist_ok=True)
    logf = open(os.path.join(OUT_DIR, "worker.log"), "a")
    subprocess.Popen([sys.executable, os.path.join(BASE_DIR, "worker.py")],
                     cwd=BASE_DIR, stdout=logf, stderr=logf)


def main():
    from app import app          # importing sets up the db + routes
    if OPEN_BROWSER:
        threading.Timer(1.5, lambda: webbrowser.open(URL)).start()
    threading.Timer(2.0, _prefetch_model).start()
    print(f"\n  zoopipe is running  ->  {URL}")
    print("  (the ~1 GB image model downloads by itself on the first run)")
    print("  keep this window open; close it to stop the server.\n")
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
