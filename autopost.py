#!/usr/bin/env python3
"""9x KLUG Autopost: postet Karussells und Reels automatisch auf Instagram.

Befehle:
  python autopost.py prepare [--test reel|karussell]  -> legt fällige Medien in _site/ ab
  python autopost.py publish                          -> veröffentlicht, was prepare vorbereitet hat
  python autopost.py vorschau [tage]                  -> zeigt, was in den nächsten Tagen gepostet wird
"""
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent
KARUSSELL_DIR = ROOT / "karussells"
REEL_DIR = ROOT / "reels"
STATUS_FILE = ROOT / "status" / "gepostet.json"
SITE = ROOT / "_site"
PLAN_FILE = ROOT / "_plan.json"
TZ = ZoneInfo("Europe/Berlin")
API = "https://graph.instagram.com/v25.0"
IMG_EXT = {".png", ".jpg", ".jpeg"}
FOLDER_RE = re.compile(r"^9x_KLUG_(.+?)_\d{4}-\d{2}-\d{2}")
HASHTAG_RE = re.compile(r"(?<!\w)#[^\s#]+")


# ---------------------------------------------------------------- Hilfen
def norm(s):
    return unicodedata.normalize("NFC", str(s)).strip().lower()


def natkey(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", str(s))]


def load_cfg():
    with open(ROOT / "einstellungen.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_status():
    if STATUS_FILE.exists():
        return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    return {"gepostet": [], "termine_erledigt": [], "letzter_reel_typ": None,
            "token_erneuert": None, "verlauf": []}


def save_status(st):
    STATUS_FILE.parent.mkdir(exist_ok=True)
    STATUS_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


GEPOSTET_CSV = ROOT / "status" / "gepostet.csv"
GEPLANT_CSV = ROOT / "status" / "geplant.csv"
CSV_KOPF = ["Datum", "Uhrzeit", "Typ", "Art", "Datei/Ordner", "Caption-Anfang", "Instagram-Link", "Test"]


def write_csv(path, header, rows, append=False):
    """Excel-taugliche CSV (Semikolon, UTF-8 mit BOM, damit Umlaute stimmen)."""
    import csv
    path.parent.mkdir(exist_ok=True)
    neu = not path.exists() or not append
    with open(path, "a" if append and path.exists() else "w", encoding="utf-8-sig" if neu else "utf-8",
              newline="") as f:
        w = csv.writer(f, delimiter=";")
        if neu:
            w.writerow(header)
        w.writerows(rows)


def kurzname(quelle):
    return quelle.split("/", 1)[1] if "/" in quelle else quelle


def rel(p):
    return unicodedata.normalize("NFC", p.relative_to(ROOT).as_posix())


def as_date(v):
    return v if isinstance(v, dt.date) else dt.date.fromisoformat(str(v))


def warnung(text):
    """Legt ein GitHub-Issue an -> du bekommst eine E-Mail."""
    print("WARNUNG:", text)
    if os.environ.get("GITHUB_ACTIONS") and os.environ.get("GH_TOKEN"):
        subprocess.run(["gh", "issue", "create", "--title", "9x KLUG Autopost: " + text[:80],
                        "--body", text], check=False)


# ---------------------------------------------------------------- Vorrat
def karussell_posts(cfg, st):
    """dict ordner-key -> Liste noch nicht geposteter Post-Ordner (sortiert)."""
    done = set(st["gepostet"])
    pool = {norm(e["ordner"]): [] for e in cfg["karussell_rhythmus"]}
    if not KARUSSELL_DIR.exists():
        return pool
    for batch in sorted(KARUSSELL_DIR.iterdir(), key=lambda p: natkey(p.name)):
        m = FOLDER_RE.match(batch.name) if batch.is_dir() else None
        if not m or norm(m.group(1)) not in pool:
            continue
        for post in sorted([p for p in batch.iterdir() if p.is_dir()], key=lambda p: natkey(p.name)):
            if rel(post) not in done and any(f.suffix.lower() in IMG_EXT for f in post.iterdir()):
                pool[norm(m.group(1))].append(post)
    return pool


def reel_files(cfg, st):
    done = set(st["gepostet"])
    pool = {norm(e["ordner"]): [] for e in cfg["reel_rhythmus"]}
    if not REEL_DIR.exists():
        return pool
    for d in REEL_DIR.iterdir():
        if d.is_dir() and norm(d.name) in pool:
            vids = sorted([f for f in d.iterdir() if f.suffix.lower() in {".mp4", ".mov"}],
                          key=lambda p: natkey(p.name))
            pool[norm(d.name)] = [v for v in vids if rel(v) not in done]
    return pool


def pick_karussell(cfg, st, day, pool):
    rhythm = cfg["karussell_rhythmus"]
    idx = (day - as_date(cfg["rhythmus_start"])).days % len(rhythm)
    heute = rhythm[idx]
    if pool.get(norm(heute["ordner"])):
        return heute, pool[norm(heute["ordner"])][0], None
    # Ersatz: Art mit dem meisten Vorrat, aber nicht die von gestern oder morgen
    nachbarn = {norm(rhythm[(idx - 1) % len(rhythm)]["ordner"]), norm(rhythm[(idx + 1) % len(rhythm)]["ordner"])}
    mit_vorrat = [e for e in rhythm if pool.get(norm(e["ordner"]))]
    kandidaten = [e for e in mit_vorrat if norm(e["ordner"]) not in nachbarn] or mit_vorrat
    if not kandidaten:
        return None, None, "Keine Karussells mehr im Vorrat. Bitte nachlegen."
    e = max(kandidaten, key=lambda x: len(pool[norm(x["ordner"])]))
    return e, pool[norm(e["ordner"])][0], (
        f"Kein Vorrat mehr für '{heute['name']}'. Stattdessen gepostet: '{e['name']}'. Bitte nachlegen.")


def pick_reel(cfg, last_type, pool):
    rhythm = cfg["reel_rhythmus"]
    keys = [norm(e["ordner"]) for e in rhythm]
    start = (keys.index(norm(last_type)) + 1) if last_type and norm(last_type) in keys else 0
    for step in range(len(rhythm)):
        e = rhythm[(start + step) % len(rhythm)]
        if norm(e["ordner"]) == norm(last_type or ""):
            continue  # nie zweimal hintereinander derselbe Typ
        if pool.get(norm(e["ordner"])):
            return e, pool[norm(e["ordner"])][0], None
    return None, None, "Keine Reels mehr im Vorrat (oder nur noch ein einziger Typ). Bitte nachlegen."


# ---------------------------------------------------------------- Medien
def to_jpeg(src, dst, fit_feed=True):
    from PIL import Image
    img = Image.open(src)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (246, 241, 231))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    img = img.convert("RGB")
    if fit_feed:
        w, h = img.size
        if h / w > 1.25:  # höher als 4:5 -> seitlich auffüllen (Instagram-Vorgabe)
            nw = int(round(h / 1.25))
            canvas = Image.new("RGB", (nw, h), img.getpixel((0, 0)))
            canvas.paste(img, ((nw - w) // 2, 0))
            img = canvas
        if img.size[0] > 1440:
            img = img.resize((1440, int(img.size[1] * 1440 / img.size[0])), Image.LANCZOS)
    img.save(dst, "JPEG", quality=95)


def caption_text(post_dir):
    for f in sorted(post_dir.iterdir()):
        if f.suffix.lower() == ".txt" and "caption" in f.name.lower():
            raw = f.read_bytes()
            for enc in ("utf-8-sig", "cp1252"):
                try:
                    txt = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            txt = HASHTAG_RE.sub("", txt)
            lines = [l.rstrip() for l in txt.splitlines()]
            return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return ""


def find_cover(video):
    pngs = [p for p in video.parent.iterdir() if p.suffix.lower() in IMG_EXT]
    for p in pngs:
        if norm(p.stem) == norm(video.stem):
            return p
    cands = [p for p in pngs if norm(p.stem).startswith(norm(video.stem))]
    return sorted(cands, key=lambda p: len(p.stem))[0] if cands else None


def build(item_type, cfg, st, day, test=False):
    """Bereitet einen Post vor und legt Dateien in _site/ ab. Gibt den Plan zurück."""
    if SITE.exists():
        shutil.rmtree(SITE)
    SITE.mkdir()
    stamp = dt.datetime.now(TZ).strftime("%Y%m%d%H%M%S")
    if item_type == "karussell":
        entry, post, hinweis = pick_karussell(cfg, st, day, karussell_posts(cfg, st))
        if not post:
            return None, hinweis
        slides = sorted([f for f in post.iterdir() if f.suffix.lower() in IMG_EXT], key=lambda p: natkey(p.name))
        if len(slides) > 10:
            hinweis = (hinweis or "") + f" {rel(post)} hat {len(slides)} Slides, Instagram erlaubt 10. Nur die ersten 10 gepostet."
            slides = slides[:10]
        files = []
        for i, s in enumerate(slides, 1):
            name = f"{stamp}_slide{i:02d}.jpg"
            to_jpeg(s, SITE / name)
            files.append(name)
        text = caption_text(post)
        caption = (text + "\n\n" if text else "") + cfg["karussell_hashtags"]
        plan = {"typ": "karussell", "art": entry["name"], "quelle": rel(post), "bilder": files, "caption": caption}
    else:
        entry, video, hinweis = pick_reel(cfg, st.get("letzter_reel_typ"), reel_files(cfg, st))
        if not video:
            return None, hinweis
        vname = f"{stamp}_reel{video.suffix.lower()}"
        shutil.copy(video, SITE / vname)
        cover = find_cover(video)
        cname = None
        if cover:
            cname = f"{stamp}_cover.jpg"
            to_jpeg(cover, SITE / cname, fit_feed=False)
        plan = {"typ": "reel", "art": entry["ordner"], "quelle": rel(video), "video": vname,
                "cover": cname, "caption": entry["hashtags"]}
    plan["test"] = test
    return plan, hinweis


# ---------------------------------------------------------------- Zeitplan
def due_slot(cfg, st, now):
    if cfg.get("pausiert"):
        print("Pausiert.")
        return None
    if now.date() < as_date(cfg["aktiv_ab"]):
        print("Noch nicht aktiv.")
        return None
    erledigt = set(st["termine_erledigt"])
    for slot in sorted(cfg["zeitplan"], key=lambda s: s["zeit"]):
        h, m = map(int, str(slot["zeit"]).split(":"))
        t = now.replace(hour=h, minute=m, second=0, microsecond=0)
        key = f"{now.date()}_{slot['zeit']}_{slot['typ']}"
        late = (now - t).total_seconds() / 3600
        if st.get("fehlversuche", {}).get(key, 0) >= 3:
            continue
        if key not in erledigt and 0 <= late <= cfg.get("max_verspaetung_stunden", 6):
            return {"key": key, "typ": slot["typ"]}
    return None


def cmd_prepare(test_type=None):
    cfg, st = load_cfg(), load_status()
    now = dt.datetime.now(TZ)
    slot = {"key": None, "typ": test_type} if test_type else due_slot(cfg, st, now)
    plan = None
    if slot:
        plan, hinweis = build(slot["typ"], cfg, st, now.date(), test=bool(test_type))
        if hinweis:
            warnung(hinweis)
        if plan:
            plan["termin"] = slot["key"]
            PLAN_FILE.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
            print("Vorbereitet:", plan["typ"], plan["art"], plan["quelle"])
        elif slot["key"]:
            st["termine_erledigt"].append(slot["key"])  # nichts zu posten -> Termin abhaken
            save_status(st)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"faellig={'true' if plan else 'false'}\n")
    if not plan:
        print("Nichts fällig.")


# ---------------------------------------------------------------- Instagram
def ig(method, path, **params):
    import requests
    params["access_token"] = os.environ["IG_TOKEN"]
    r = requests.request(method, f"{API}/{path}", data=params if method == "POST" else None,
                         params=None if method == "POST" else params, timeout=120)
    data = r.json()
    if r.status_code >= 400 or "error" in data:
        raise RuntimeError(f"Instagram-Fehler bei {path}: {data}")
    return data


def wait_ready(cid, minutes=8):
    for _ in range(minutes * 4):
        s = ig("GET", cid, fields="status_code")["status_code"]
        if s == "FINISHED":
            return
        if s in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram konnte das Medium nicht verarbeiten (Status {s}).")
        time.sleep(15)
    raise RuntimeError("Instagram braucht zu lange für die Verarbeitung.")


def wait_url(url):
    import requests
    for _ in range(30):
        try:
            if requests.head(url, timeout=20).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(10)
    raise RuntimeError(f"Datei ist nicht öffentlich erreichbar: {url}")


def refresh_token(st):
    last = st.get("token_erneuert")
    if last and (dt.date.today() - dt.date.fromisoformat(last)).days < 7:
        return
    import requests
    r = requests.get("https://graph.instagram.com/refresh_access_token",
                     params={"grant_type": "ig_refresh_token", "access_token": os.environ["IG_TOKEN"]}, timeout=60)
    data = r.json()
    if "access_token" not in data:
        warnung(f"Zugangsschlüssel konnte nicht erneuert werden: {data}")
        return
    if os.environ.get("GH_PAT"):
        env = dict(os.environ, GH_TOKEN=os.environ["GH_PAT"])
        res = subprocess.run(["gh", "secret", "set", "IG_TOKEN", "--body", data["access_token"]], env=env)
        if res.returncode == 0:
            st["token_erneuert"] = dt.date.today().isoformat()
            print("Zugangsschlüssel erneuert.")
            return
    warnung("Zugangsschlüssel erneuert, aber konnte nicht gespeichert werden (GH_PAT prüfen).")


def cmd_publish():
    plan = json.loads(PLAN_FILE.read_text(encoding="utf-8"))
    try:
        _publish(plan)
    except Exception:
        st = load_status()
        if plan.get("termin"):
            fv = st.setdefault("fehlversuche", {})
            fv[plan["termin"]] = fv.get(plan["termin"], 0) + 1
            save_status(st)
        raise


def _publish(plan):
    base = os.environ["PAGES_URL"].rstrip("/") + "/"
    st = load_status()
    user = ig("GET", "me", fields="user_id,username")
    uid = user["user_id"]
    print("Poste auf @" + user.get("username", "?"))
    if plan["typ"] == "karussell":
        for b in plan["bilder"]:
            wait_url(base + b)
        if len(plan["bilder"]) == 1:
            cid = ig("POST", f"{uid}/media", image_url=base + plan["bilder"][0], caption=plan["caption"])["id"]
        else:
            kids = [ig("POST", f"{uid}/media", image_url=base + b, is_carousel_item="true")["id"]
                    for b in plan["bilder"]]
            for k in kids:
                wait_ready(k)
            cid = ig("POST", f"{uid}/media", media_type="CAROUSEL", children=",".join(kids),
                     caption=plan["caption"])["id"]
    else:
        wait_url(base + plan["video"])
        params = dict(media_type="REELS", video_url=base + plan["video"], caption=plan["caption"],
                      share_to_feed="true")
        if plan.get("cover"):
            wait_url(base + plan["cover"])
            params["cover_url"] = base + plan["cover"]
        cid = ig("POST", f"{uid}/media", **params)["id"]
    wait_ready(cid)
    media_id = ig("POST", f"{uid}/media_publish", creation_id=cid)["id"]
    print("Veröffentlicht, Medien-ID", media_id)
    try:
        link = ig("GET", media_id, fields="permalink").get("permalink", "")
    except Exception:
        link = ""
    jetzt = dt.datetime.now(TZ)
    cap = " / ".join(l.strip() for l in plan["caption"].splitlines() if l.strip())
    write_csv(GEPOSTET_CSV, CSV_KOPF, [[jetzt.strftime("%d.%m.%Y"), jetzt.strftime("%H:%M"),
              "Reel" if plan["typ"] == "reel" else "Karussell", plan["art"], kurzname(plan["quelle"]),
              cap[:120], link, "ja" if plan.get("test") else ""]], append=True)

    st["gepostet"].append(plan["quelle"])
    if plan.get("termin"):
        st["termine_erledigt"].append(plan["termin"])
    if plan["typ"] == "reel":
        st["letzter_reel_typ"] = plan["art"]
    st["verlauf"].append({"zeit": dt.datetime.now(TZ).isoformat(timespec="minutes"), "typ": plan["typ"],
                          "art": plan["art"], "quelle": plan["quelle"], "test": plan.get("test", False)})
    st["termine_erledigt"] = st["termine_erledigt"][-200:]
    save_status(st)
    try:
        refresh_token(st)
    finally:
        save_status(st)
    cmd_vorschau(14, ausgabe=False)


# ---------------------------------------------------------------- Vorschau
def cmd_vorschau(days=14, ausgabe=True):
    cfg, st = load_cfg(), load_status()
    st = json.loads(json.dumps(st))
    jetzt = dt.datetime.now(TZ)
    start = max(jetzt.date(), as_date(cfg["aktiv_ab"]))
    kpool, rpool = karussell_posts(cfg, st), reel_files(cfg, st)
    last = st.get("letzter_reel_typ")
    wd = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
    lines = ["| Datum | Zeit | Typ | Art | Datei |", "|---|---|---|---|---|"]
    rows = []
    for d in range(days):
        day = start + dt.timedelta(days=d)
        for slot in sorted(cfg["zeitplan"], key=lambda s: s["zeit"]):
            if day == jetzt.date() and str(slot["zeit"]) <= jetzt.strftime("%H:%M"):
                continue  # heute schon vorbei
            if slot["typ"] == "karussell":
                e, item, _ = pick_karussell(cfg, st, day, kpool)
                art = e["name"] if e else "-"
                if item:
                    kpool[norm(e["ordner"])].remove(item)
            else:
                e, item, _ = pick_reel(cfg, last, rpool)
                art = e["ordner"] if e else "-"
                if item:
                    rpool[norm(e["ordner"])].remove(item)
                    last = e["ordner"]
            datei = kurzname(rel(item)) if item else "KEIN VORRAT"
            lines.append(f"| {wd[day.weekday()]} {day:%d.%m.} | {slot['zeit']} | {slot['typ']} | {art} | {datei} |")
            rows.append([day.strftime("%d.%m.%Y"), wd[day.weekday()], slot["zeit"],
                         "Reel" if slot["typ"] == "reel" else "Karussell", art, datei])
    vorrat = ["", "**Vorrat Karussells:** " + ", ".join(
        f"{e['name']}: {len(karussell_posts(cfg, st)[norm(e['ordner'])])}" for e in cfg["karussell_rhythmus"])]
    vorrat.append("**Vorrat Reels:** " + ", ".join(
        f"{e['ordner']}: {len(reel_files(cfg, st)[norm(e['ordner'])])}" for e in cfg["reel_rhythmus"]))
    write_csv(GEPLANT_CSV, ["Datum", "Tag", "Uhrzeit", "Typ", "Art", "Datei/Ordner"], rows)
    text = "\n".join(lines + vorrat)
    if not ausgabe:
        return
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("## Vorschau\n\n" + text + "\n")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "vorschau"
    if cmd == "prepare":
        t = sys.argv[3] if len(sys.argv) > 3 and sys.argv[2] == "--test" else None
        cmd_prepare(t if t in ("reel", "karussell") else None)
    elif cmd == "publish":
        cmd_publish()
    else:
        cmd_vorschau(int(sys.argv[2]) if len(sys.argv) > 2 else 14)
