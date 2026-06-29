import re, csv, html, os, glob

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# raw MiniZOO export pages live next to this script; output CSV goes to data/
FILES = sorted(glob.glob(os.path.join(HERE, "page*.html")))
OUT_CSV = os.path.join(ROOT, "data", "ryby.csv")

# availability column classes in header order
AVAIL = [
    ("hb", "hballzobrazuj"), ("pc", "pcallzobrazuj"), ("sz", "szallzobrazuj"),
    ("aq", "aqallzobrazuj"), ("vs", "vsallzobrazuj"), ("mo", "moallzobrazuj"),
    ("pccz", "pcczallzobrazuj"), ("szcz", "czaallzobrazuj"), ("cz1", "czballzobrazuj"),
    ("pl", "plallzobrazuj"), ("uk", "ukallzobrazuj"), ("hu", "huallzobrazuj"),
    ("ch", "challzobrazuj"),
]

def clean(s):
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()

rows = []
for fn in FILES:
    data = open(fn, encoding="utf-8", errors="replace").read()
    # split into rows
    parts = re.split(r"(<tr id='riadok\d+'>)", data)
    # parts: [pre, tag, body, tag, body, ...]
    for i in range(1, len(parts), 2):
        tag = parts[i]
        body = parts[i+1]
        # body runs until next <tr id=...> already split; but trim at </tr> if present? keep cells
        rid = re.search(r"riadok(\d+)", tag).group(1)
        cells = re.findall(r"<td.*?</td>", body, re.S)
        if len(cells) < 6:
            continue
        # cell0: dates
        dates = clean(cells[0])
        zarad, posl = "", ""
        m = re.match(r"(.*?)(\d{2}\.\d{2}\.\d{4}.*)", dates)
        # dates contain "15.08.2018 24.04.2026 o 23:09:01"
        dparts = re.findall(r"\d{2}\.\d{2}\.\d{4}(?: o [\d:]+)?", dates)
        if len(dparts) >= 1: zarad = dparts[0]
        if len(dparts) >= 2: posl = dparts[1]

        # cell1: name  [<b>RY667</b>] <b>Slovak name</b><br>Latin name
        c1 = cells[1]
        code_m = re.search(r"\[<b[^>]*>([^<]+)</b>\]", c1)
        code = code_m.group(1).strip() if code_m else ""
        bolds = re.findall(r"<b>(.*?)</b>", c1, re.S)
        sk_name = clean(bolds[0]) if bolds else ""
        # latin: text after the last </b> up to end / <br>
        after = re.split(r"</b>", c1)[-1]
        latin = clean(after)

        # cell2: skladom count "0 ks"
        skl = clean(cells[2])
        skl_m = re.search(r"(-?\d+)\s*ks", skl)
        skladom_ks = skl_m.group(1) if skl_m else ""

        # cell3: skupina  Ryby / Tetry
        c3 = clean(cells[3])

        # cell4: stav skladom input value
        stav_m = re.search(r"value='(-?\d+)'", cells[4])
        stav = stav_m.group(1) if stav_m else ""

        # availability flags from whole body: eye.png =1, eye-not.png=0
        avail_vals = {}
        for key, cls in AVAIL:
            am = re.search(r"class='" + cls + r"'[^>]*src='obr/(eye(?:-not)?)\.png'", body)
            if am:
                avail_vals[key] = "1" if am.group(1) == "eye" else "0"
            else:
                avail_vals[key] = ""

        row = {
            "id": rid, "kod": code, "nazov_sk": sk_name, "nazov_lat": latin,
            "skupina": c3, "skladom_ks": skladom_ks, "stav_skladom": stav,
            "zaradenie": zarad, "posledny_pohyb": posl,
        }
        row.update(avail_vals)
        rows.append(row)

fields = ["id","kod","nazov_sk","nazov_lat","skupina","skladom_ks","stav_skladom",
          "zaradenie","posledny_pohyb"] + [k for k,_ in AVAIL]

os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
with open(OUT_CSV,"w",newline="",encoding="utf-8-sig") as f:
    w = csv.DictWriter(f, fieldnames=fields)
    w.writeheader()
    w.writerows(rows)

print("rows:", len(rows))
# sanity sample
for r in rows[:3]:
    print(r)
print("...")
print(rows[-1])
# count missing names
print("missing nazov_sk:", sum(1 for r in rows if not r["nazov_sk"]))
print("missing kod:", sum(1 for r in rows if not r["kod"]))
