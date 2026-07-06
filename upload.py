#!/usr/bin/env python3
"""Nahrá jednu upravenú fotku na produkt (idpr) na minizoo.let.is.

Formulár sa ukladá celý, takže načítame edit formulár, verne zreprodukujeme
všetky jeho polia a len pridáme fotku do poľa 'podklad'. Volá sa z app.py;
prihlásenie cez cookie.txt. DRY=1 -> nič sa nepošle.
"""
import os, html as htmllib, mimetypes, urllib.request
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = "https://minizoo.let.is"
ENDPOINT = BASE + "/admin_sklad_sortiment"
COOKIE_FILE = os.path.join(HERE, "cookie.txt")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")

FORM_NAME = "upravnar_form"
FILE_FIELD = "podklad"
SAVE_P3 = "sortiment_uprav_uloz"
DRY = os.environ.get("DRY") == "1"


# ---------- prihlásenie / HTTP ----------
def _cookie():
    if not os.path.exists(COOKIE_FILE):
        raise RuntimeError(f"Chýba {COOKIE_FILE}. Vlož doň Cookie reťazec z prehliadača.")
    return open(COOKIE_FILE).read().strip()


def _headers():
    return {"User-Agent": UA, "Referer": ENDPOINT, "Cookie": _cookie()}


def http_get(url):
    req = urllib.request.Request(url, headers=_headers())
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


# ---------- verné čítanie formulára ----------
class FormSerializer(HTMLParser):
    """Vyzbiera 'úspešné controls' presne ako by ich poslal prehliadač."""
    def __init__(self):
        super().__init__()
        self.in_form = False
        self.fields = []
        self.sel_name = None; self.sel_selected = False
        self.sel_first = None; self.sel_multiple = False
        self.opt_val = None; self.opt_buf = None
        self.ta_name = None; self.ta_buf = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            if a.get("name") == FORM_NAME:
                self.in_form = True
            return
        if not self.in_form:
            return
        if tag == "input":
            n = a.get("name"); t = (a.get("type") or "text").lower()
            if not n:
                return
            if t in ("checkbox", "radio"):
                if "checked" in a:
                    self.fields.append((n, htmllib.unescape(a.get("value", "on"))))
            elif t == "file":
                pass                                    # fotku pridáme sami
            elif t == "image":
                self.fields.append((n + ".x", "1"))
                self.fields.append((n + ".y", "1"))
            elif t in ("submit", "button", "reset"):
                pass
            else:
                self.fields.append((n, htmllib.unescape(a.get("value", ""))))
        elif tag == "select":
            self.sel_name = a.get("name"); self.sel_selected = False
            self.sel_first = None; self.sel_multiple = "multiple" in a
        elif tag == "option" and self.sel_name is not None:
            self.opt_val = a.get("value")               # None => hodnota z textu
            self.opt_buf = []
            self._opt_selected = "selected" in a
        elif tag == "textarea":
            self.ta_name = a.get("name"); self.ta_buf = []

    def handle_data(self, data):
        if self.opt_buf is not None:
            self.opt_buf.append(data)
        if self.ta_buf is not None:
            self.ta_buf.append(data)

    def handle_endtag(self, tag):
        if tag == "form" and self.in_form:
            self.in_form = False
        elif tag == "option" and self.sel_name is not None and self.opt_buf is not None:
            val = self.opt_val if self.opt_val is not None else "".join(self.opt_buf).strip()
            if self.sel_first is None:
                self.sel_first = val
            if getattr(self, "_opt_selected", False):
                self.fields.append((self.sel_name, htmllib.unescape(val)))
                self.sel_selected = True
            self.opt_val = None; self.opt_buf = None
        elif tag == "select" and self.sel_name is not None:
            if not self.sel_selected and not self.sel_multiple and self.sel_first is not None:
                self.fields.append((self.sel_name, htmllib.unescape(self.sel_first)))
            self.sel_name = None
        elif tag == "textarea" and self.ta_name is not None:
            self.fields.append((self.ta_name, htmllib.unescape("".join(self.ta_buf))))
            self.ta_name = None; self.ta_buf = None


def read_form(idpr):
    html = http_get(ENDPOINT + f"?p3=sortiment_uprav&idpr={idpr}")
    p = FormSerializer(); p.feed(html)
    if not p.fields:
        raise RuntimeError(f"formulár {FORM_NAME} nenájdený pre idpr={idpr}")
    return p.fields


# ---------- multipart POST ----------
def post_multipart(url, fields, file_field, filename, filedata):
    boundary = "----minizooUpload7MA4YWxkTrZu0gW029"
    crlf = b"\r\n"
    buf = []
    for name, value in fields:
        buf.append(b"--" + boundary.encode())
        buf.append(f'Content-Disposition: form-data; name="{name}"'.encode())
        buf.append(b"")
        buf.append(value.encode("utf-8"))
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    buf.append(b"--" + boundary.encode())
    buf.append(f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"'.encode())
    buf.append(f"Content-Type: {ctype}".encode())
    buf.append(b"")
    body = crlf.join(buf) + crlf + filedata + crlf + b"--" + boundary.encode() + b"--" + crlf
    headers = dict(_headers())
    headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status, r.read()


# ---------- upload jednej fotky na jeden produkt ----------
def upload_one(idpr, imgpath, filename=None):
    """Pridá fotku k produktu. Vráti 'ok', 'dry' (DRY=1) alebo 'HTTP <kód>';
    chyby (login/formulár/sieť) vyhodia výnimku."""
    fields = read_form(idpr)
    with open(imgpath, "rb") as f:
        data = f.read()
    fname = filename or os.path.basename(imgpath)
    url = ENDPOINT + f"?p3=sortiment_uprav&idpr={idpr}"

    # delfile (zmazať fotku) sa nesmie poslať; p3 musí byť uloženie
    fields = [(n, v) for n, v in fields if n not in ("delfile", "p3")] + [("p3", SAVE_P3)]

    if DRY:
        print(f"    DRY: neposielam ({len(fields)} polí, fotka {fname}, {len(data)//1024} kB)")
        return "dry"

    status, _ = post_multipart(url, fields, FILE_FIELD, fname, data)
    if status != 200:
        return f"HTTP {status}"
    return "ok"
