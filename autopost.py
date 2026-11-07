#!/usr/bin/env python3
"""9x KLUG Autopost: postet Karussells und Reels automatisch auf Instagram
und lädt die Reels zusätzlich als YouTube Shorts hoch (seit 03.11.2026, Schalter youtube -> aktiv)
und als Reels auf die Facebook-Seite 9x KLUG (seit 07.11.2026, Schalter facebook -> aktiv).

Befehle:
  python autopost.py prepare [--test reel|karussell]  -> legt fällige Medien in _site/ ab
  python autopost.py publish                          -> veröffentlicht, was prepare vorbereitet hat
  python autopost.py youtube                          -> lädt das eben auf Instagram gepostete Reel zu YouTube hoch
  python autopost.py youtube-test                     -> lädt das zuletzt gepostete Reel PRIVAT zu YouTube hoch (Test)
  python autopost.py youtube-nachholen <datei>        -> lädt ein Reel nach, dessen YouTube-Upload fehlte
  python autopost.py facebook                         -> postet das eben auf Instagram gepostete Reel auf Facebook
  python autopost.py facebook-test                    -> legt das zuletzt gepostete Reel als ENTWURF auf Facebook an
  python autopost.py facebook-nachholen <datei>       -> postet ein Reel nach, das auf Facebook fehlt
  python autopost.py vorschau [tage]                  -> zeigt, was in den nächsten Tagen gepostet wird
                                                         (inkl. Caption) und schreibt die Caption-Liste
                                                         aller Reels im Vorrat (status/captions_reels.csv)
"""
import datetime as dt
import hashlib
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


def media_root():
    """Wo die Reels/Karussells liegen.

    Auf dem eigenen PC: im Repo-Ordner. Bei GitHub (Spar-Modus) werden Videos und
    Bilder NICHT heruntergeladen. Stattdessen werden leere Platzhalter mit denselben
    Namen in einem Nebenordner angelegt. Nur die Dateien, die wirklich gepostet
    werden, werden danach einzeln geholt (siehe hole_dateien)."""
    if (ROOT / "reels").exists() or (ROOT / "karussells").exists():
        return ROOT
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--", "karussells", "reels"],
                             capture_output=True, check=True).stdout
    except Exception:
        return ROOT
    if not out:
        return ROOT
    schatten = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "9xklug_dateien"
    for raw in out.split(b"\0"):
        if raw:
            p = schatten / raw.decode("utf-8")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.touch()
    print("Spar-Modus: Medien werden nur bei Bedarf geladen.")
    return schatten


MEDIA_ROOT = media_root()
KARUSSELL_DIR = MEDIA_ROOT / "karussells"
REEL_DIR = MEDIA_ROOT / "reels"


def hole_dateien(paths):
    """Lädt im Spar-Modus genau diese Dateien von GitHub (statt alles)."""
    if MEDIA_ROOT == ROOT:
        return
    for p in paths:
        gitpfad = p.relative_to(MEDIA_ROOT).as_posix()
        data = subprocess.run(["git", "-C", str(ROOT), "cat-file", "blob", "HEAD:" + gitpfad],
                              capture_output=True, check=True).stdout
        p.write_bytes(data)
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
CSV_KOPF = ["Datum", "Uhrzeit", "Typ", "Art", "Datei/Ordner", "Caption-Anfang", "Instagram-Link", "Test", "YouTube",
            "Facebook"]


def _csv_lesen(path):
    import csv
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.reader(f, delimiter=";"))


def _csv_kopf_aktualisieren():
    """Ältere gepostet.csv (ohne Spalte YouTube bzw. Facebook) einmalig um die fehlenden Spalten ergänzen.
    Neue Spalten kommen immer hinten dran, bestehende Spalten und Zeilen bleiben unverändert."""
    if not GEPOSTET_CSV.exists():
        return
    zeilen = _csv_lesen(GEPOSTET_CSV)
    if not zeilen:
        return
    fehlt = [k for k in CSV_KOPF if k not in zeilen[0]]
    if not fehlt:
        return
    kopf = zeilen[0] + fehlt
    rows = [z + [""] * (len(kopf) - len(z)) for z in zeilen[1:]]
    write_csv(GEPOSTET_CSV, kopf, rows)


def csv_spalte_eintragen(quelle, spalte, wert):
    """Trägt in gepostet.csv beim (letzten) Instagram-Eintrag dieses Reels den Stand einer Plattform ein."""
    if not GEPOSTET_CSV.exists():
        return
    _csv_kopf_aktualisieren()
    zeilen = _csv_lesen(GEPOSTET_CSV)
    kopf = zeilen[0]
    i_typ, i_datei, i_test, i_sp = (kopf.index(k) for k in ("Typ", "Datei/Ordner", "Test", spalte))
    for z in reversed(zeilen[1:]):
        z += [""] * (len(kopf) - len(z))
        if z[i_typ] == "Reel" and z[i_datei] == kurzname(quelle) and not z[i_test].startswith("ja"):
            z[i_sp] = wert
            break
    else:
        return
    write_csv(GEPOSTET_CSV, kopf, zeilen[1:])


def csv_youtube_eintragen(quelle, wert):
    csv_spalte_eintragen(quelle, "YouTube", wert)


def csv_facebook_eintragen(quelle, wert):
    csv_spalte_eintragen(quelle, "Facebook", wert)


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
    return unicodedata.normalize("NFC", p.relative_to(MEDIA_ROOT).as_posix())


def as_date(v):
    return v if isinstance(v, dt.date) else dt.date.fromisoformat(str(v))


def warnung(text):
    """Legt ein GitHub-Issue an -> du bekommst eine E-Mail."""
    print("WARNUNG:", text)
    if os.environ.get("GITHUB_ACTIONS") and os.environ.get("GH_TOKEN"):
        subprocess.run(["gh", "issue", "create", "--title", "9x KLUG Autopost: " + text[:80],
                        "--body", text], check=False)


# ---------------------------------------------------------------- Vorrat
WOCHENTAGE = ["mo", "di", "mi", "do", "fr", "sa", "so"]


def extra_termine(cfg):
    """Zusätzliche Termine mit festen Tagen (Abschnitt extra_termine)."""
    return cfg.get("extra_termine") or []


def an_tag(extra, day):
    tage = [norm(t)[:2] for t in (extra.get("tage") or WOCHENTAGE)]
    return WOCHENTAGE[day.weekday()] in tage


def karussell_posts(cfg, st, entries=None):
    """dict ordner-key -> Liste noch nicht geposteter Post-Ordner (sortiert)."""
    done = set(st["gepostet"])
    pool = {norm(e["ordner"]): [] for e in (entries if entries is not None else cfg["karussell_rhythmus"])}
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


def reel_files(cfg, st, entries=None):
    done = set(st["gepostet"])
    pool = {norm(e["ordner"]): [] for e in (entries if entries is not None else cfg["reel_rhythmus"])}
    if not REEL_DIR.exists():
        return pool
    for d in REEL_DIR.iterdir():
        if d.is_dir() and norm(d.name) in pool:
            vids = sorted([f for f in d.iterdir() if f.suffix.lower() in {".mp4", ".mov"}],
                          key=lambda p: natkey(p.name))
            pool[norm(d.name)] = [v for v in vids if rel(v) not in done]
    return pool


def letzte_karussell_art(st):
    """Name der zuletzt (echt, nicht test) geposteten Karussell-Art."""
    if st.get("letzte_karussell_art"):
        return st["letzte_karussell_art"]
    for v in reversed(st.get("verlauf", [])):
        if v.get("typ") == "karussell" and not v.get("test"):
            return v.get("art")
    return None


def pick_karussell(cfg, last_art, pool):
    """Rätselarten kommen der Reihe nach dran (egal an welchem Tag).
    Hat eine Art keinen Vorrat mehr, kommt die nächste in der Liste dran."""
    rhythm = cfg["karussell_rhythmus"]
    namen = [norm(e["name"]) for e in rhythm]
    start = (namen.index(norm(last_art)) + 1) if last_art and norm(last_art) in namen else 0
    heute = rhythm[start % len(rhythm)]
    for step in range(len(rhythm)):
        e = rhythm[(start + step) % len(rhythm)]
        if pool.get(norm(e["ordner"])):
            hinweis = None if step == 0 else (
                f"Kein Vorrat mehr für '{heute['name']}'. Stattdessen gepostet: '{e['name']}'. Bitte nachlegen.")
            return e, pool[norm(e["ordner"])][0], hinweis
    return None, None, "Keine Karussells mehr im Vorrat. Bitte nachlegen."


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


# ---------------------------------------------------------------- Reel-Caption
# Aufbau (Texte stehen alle in einstellungen.yaml, nichts wird aus Dateinamen erraten):
#   Zeile 1: Hook. Liegt neben dem Video eine gleichnamige .txt, kommt deren Text.
#            Sonst der feste Text des Typs (hook).
#   Zeile 2: Aufforderung aus der Gruppe des Typs (aufforderung). Welcher Satz der Gruppe,
#            hängt fest am Dateinamen: gleiches Reel = immer gleicher Satz (Vorschau = echter Post).
#   Danach:  Schlusssatz (reel_caption -> schluss) und die Hashtags des Typs.
# Fehlt hook oder Aufforderung für einen Typ, wird wie früher nur mit Hashtags gepostet
# und es kommt eine Hinweis-Mail.
CAPTION_MAX = 2200  # Instagram-Grenze für Captions


def lies_text(p):
    """Liest eine kleine Textdatei (UTF-8 oder Windows-Umlaute). Im Spar-Modus wird sie vorher geholt,
    sonst wäre sie nur ein leerer Platzhalter."""
    hole_dateien([p])
    raw = p.read_bytes()
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return ""


def begleittext(video):
    """Text aus der .txt mit gleichem Namen wie das Video. Gibt (text, datei) zurück, sonst (None, None)."""
    for p in video.parent.iterdir():
        if p.suffix.lower() == ".txt" and norm(p.stem) == norm(video.stem):
            return " ".join(lies_text(p).split()) or None, p
    return None, None


def reel_caption(cfg, entry, video):
    """Baut die Caption für ein Reel. Gibt (caption, hinweis) zurück."""
    hashtags = str(entry.get("hashtags") or "").strip()
    typ = entry.get("name", entry["ordner"])
    rc = cfg.get("reel_caption") or {}
    gruppe = str(entry.get("aufforderung") or "").strip()
    saetze = [str(s).strip() for s in ((rc.get("aufforderungen") or {}).get(gruppe) or []) if str(s or "").strip()]
    hook = str(entry.get("hook") or "").strip()
    hinweise = []

    txt, datei = begleittext(video)
    if datei and not txt:
        hinweise.append(f"Textdatei {rel(datei)} ist leer, fester Text des Typs verwendet.")
    if txt:
        hook = txt

    fehlt = []
    if not hook:
        fehlt.append("hook")
    if not gruppe:
        fehlt.append("aufforderung")
    elif not saetze:
        fehlt.append(f"Aufforderungs-Gruppe '{gruppe}' unter reel_caption")
    if fehlt:
        hinweise.append(f"Reel-Typ '{typ}': {', '.join(fehlt)} fehlt in einstellungen.yaml. "
                        "Reel wird nur mit Hashtags gepostet.")
        return hashtags, " ".join(hinweise)

    nr = int(hashlib.md5(norm(video.name).encode("utf-8")).hexdigest(), 16)
    teile = [hook + "\n" + saetze[nr % len(saetze)]]
    if str(rc.get("schluss") or "").strip():
        teile.append(str(rc["schluss"]).strip())
    if hashtags:
        teile.append(hashtags)
    caption = "\n\n".join(teile)
    if len(caption) > CAPTION_MAX:
        hinweise.append(f"Caption für {rel(video)} ist länger als {CAPTION_MAX} Zeichen. "
                        "Reel wird nur mit Hashtags gepostet.")
        return hashtags, " ".join(hinweise)
    return caption, " ".join(hinweise) or None


# ---------------------------------------------------------------- Reel-Titelbild aus dem Video
# Steht beim Reel-Typ "titelbild_sekunde", wird das Bild zu diesem Zeitpunkt aus dem Video geschnitten.
# Das Profil-Raster zeigt nur den mittleren 3:4-Bereich (1080x1440) des Titelbilds. Damit Logo und
# Überschrift (oben im Reel) sichtbar sind, wird der OBERE 3:4-Teil des Bildes genau dorthin gesetzt.
# Klappt etwas nicht, gilt wie bisher die titelbild.png des Typs (+ Mail).
def _ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def titelbild_aus_video(video, sekunde, ziel):
    """Baut das Titelbild nach ziel (JPG). Gibt None zurück, wenn alles geklappt hat, sonst den Grund."""
    try:
        sekunde = float(sekunde)
    except (TypeError, ValueError):
        return f"titelbild_sekunde '{sekunde}' ist keine Zahl"
    if sekunde < 0:
        return "titelbild_sekunde darf nicht negativ sein"
    exe = _ffmpeg()
    if not exe:
        return "ffmpeg ist nicht verfügbar"
    roh = Path(str(ziel) + ".roh.png")
    r = subprocess.run([exe, "-loglevel", "error", "-y", "-ss", f"{sekunde:.2f}", "-i", str(video),
                        "-frames:v", "1", str(roh)], capture_output=True)
    if r.returncode != 0 or not roh.exists() or roh.stat().st_size == 0:
        return f"kein Bild bei {sekunde:g} s (Video kürzer?)"
    from PIL import Image
    with Image.open(roh) as im:
        img = im.convert("RGB")
    roh.unlink()
    w, h = img.size
    if abs(h / w - 16 / 9) > 0.02:
        return f"Video ist nicht im Format 9:16 ({w}x{h})"
    img = img.resize((1080, 1920), Image.LANCZOS)
    titel = Image.new("RGB", (1080, 1920), img.getpixel((8, 960)))  # Randfarbe des Reels
    titel.paste(img.crop((0, 0, 1080, 1440)), (0, 240))
    titel.save(ziel, "JPEG", quality=92)
    return None


def find_cover(video):
    pngs = [p for p in video.parent.iterdir() if p.suffix.lower() in IMG_EXT]
    for p in pngs:
        if norm(p.stem) == "titelbild":
            return p
    for p in pngs:
        if norm(p.stem) == norm(video.stem):
            return p
    cands = [p for p in pngs if norm(p.stem).startswith(norm(video.stem))]
    return sorted(cands, key=lambda p: len(p.stem))[0] if cands else None


def build(item_type, cfg, st, day, test=False, extra=None):
    """Bereitet einen Post vor und legt Dateien in _site/ ab. Gibt den Plan zurück."""
    if SITE.exists():
        shutil.rmtree(SITE)
    SITE.mkdir()
    stamp = dt.datetime.now(TZ).strftime("%Y%m%d%H%M%S")
    if item_type == "karussell":
        if extra:
            items = karussell_posts(cfg, st, [extra])[norm(extra["ordner"])]
            entry = dict(extra, name=extra.get("name", extra["ordner"]))
            post, hinweis = (items[0], None) if items else (None, f"Kein Vorrat mehr für '{entry['name']}' (zusätzlicher Termin fällt aus). Bitte nachlegen.")
        else:
            entry, post, hinweis = pick_karussell(cfg, letzte_karussell_art(st), karussell_posts(cfg, st))
        if not post:
            return None, hinweis
        hole_dateien([f for f in post.iterdir() if f.is_file()])
        slides = sorted([f for f in post.iterdir() if f.suffix.lower() in IMG_EXT], key=lambda p: natkey(p.name))
        if cfg.get("cover_weglassen") and len(slides) > 1:
            slides = slides[1:]  # Cover (erstes Slide) überspringen, das Rätsel ist dann das erste Bild
        if len(slides) > 10:
            hinweis = (hinweis or "") + f" {rel(post)} hat {len(slides)} Slides, Instagram erlaubt 10. Nur die ersten 10 gepostet."
            slides = slides[:10]
        files = []
        for i, s in enumerate(slides, 1):
            name = f"{stamp}_slide{i:02d}.jpg"
            to_jpeg(s, SITE / name)
            files.append(name)
        text = caption_text(post)
        caption = (text + "\n\n" if text else "") + (entry.get("hashtags") or cfg["karussell_hashtags"])
        plan = {"typ": "karussell", "art": entry["name"], "quelle": rel(post), "bilder": files, "caption": caption}
    else:
        if extra:
            items = reel_files(cfg, st, [extra])[norm(extra["ordner"])]
            entry = extra
            video, hinweis = (items[0], None) if items else (None, f"Kein Vorrat mehr für '{extra['ordner']}' (zusätzliches Reel fällt aus). Bitte nachlegen.")
        else:
            entry, video, hinweis = pick_reel(cfg, st.get("letzter_reel_typ"), reel_files(cfg, st))
        if not video:
            return None, hinweis
        vname = f"{stamp}_reel{video.suffix.lower()}"
        cover = find_cover(video)
        hole_dateien([video] + ([cover] if cover else []))
        shutil.copy(video, SITE / vname)
        cname, thinweis = None, None
        if entry.get("titelbild_sekunde") is not None:
            cname = f"{stamp}_cover.jpg"
            grund = titelbild_aus_video(SITE / vname, entry["titelbild_sekunde"], SITE / cname)
            if grund:
                cname = None
                thinweis = f"Titelbild aus dem Video für {rel(video)} nicht möglich ({grund}). titelbild.png verwendet."
        if not cname and cover:
            cname = f"{stamp}_cover.jpg"
            to_jpeg(cover, SITE / cname, fit_feed=False)
        caption, chinweis = reel_caption(cfg, entry, video)
        hinweis = " ".join(h for h in (hinweis, thinweis, chinweis) if h) or None
        plan = {"typ": "reel", "art": entry["ordner"], "quelle": rel(video), "video": vname,
                "cover": cname, "caption": caption}
    plan["test"] = test
    plan["extra"] = bool(extra)
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
    slots = [dict(s, extra=None) for s in cfg["zeitplan"] if an_tag(s, now.date())]
    slots += [{"zeit": e["zeit"], "typ": e["typ"], "extra": e} for e in extra_termine(cfg) if an_tag(e, now.date())]
    for slot in sorted(slots, key=lambda s: str(s["zeit"])):
        h, m = map(int, str(slot["zeit"]).split(":"))
        t = now.replace(hour=h, minute=m, second=0, microsecond=0)
        key = f"{now.date()}_{slot['zeit']}_{slot['typ']}"
        if slot["extra"]:
            key += "_extra_" + norm(slot["extra"]["ordner"])
        late = (now - t).total_seconds() / 3600
        if st.get("fehlversuche", {}).get(key, 0) >= 3:
            continue
        if key not in erledigt and 0 <= late <= cfg.get("max_verspaetung_stunden", 6):
            return {"key": key, "typ": slot["typ"], "extra": slot["extra"]}
    return None


def cmd_prepare(test_type=None):
    cfg, st = load_cfg(), load_status()
    now = dt.datetime.now(TZ)
    slot = {"key": None, "typ": test_type} if test_type else due_slot(cfg, st, now)
    plan = None
    if slot:
        plan, hinweis = build(slot["typ"], cfg, st, now.date(), test=bool(test_type), extra=slot.get("extra"))
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


def _caption_norm(t):
    t = unicodedata.normalize("NFC", t or "")
    return " ".join(t.split()).lower()


def schon_online(uid, plan, st, minuten=None):
    """Sucht auf dem Profil nach einem Post mit genau dieser Caption.
    Schutz gegen Doppelposts: Instagram meldet manchmal einen Fehler, obwohl der Post schon online ist.
    - Karussell mit eigenem Text: Caption ist eindeutig -> Suche über die letzten 14 Tage.
    - Reel / Caption nur aus Hashtags: nicht eindeutig -> nur kurz nach einem Fehlversuch desselben Termins.
    Gibt den Link (oder die Medien-ID) zurück, sonst None."""
    eigene = _caption_norm(HASHTAG_RE.sub("", plan["caption"]))
    if minuten is None:
        if plan["typ"] == "karussell" and eigene:
            minuten = 14 * 24 * 60
        elif plan.get("termin") and st.get("fehlversuche", {}).get(plan["termin"], 0) > 0:
            minuten = 180
        else:
            return None
    try:
        daten = ig("GET", f"{uid}/media", fields="id,caption,timestamp,permalink,media_type", limit=30).get("data", [])
    except Exception as e:
        print("Doppelpost-Prüfung nicht möglich:", e)
        return None
    ziel = _caption_norm(plan["caption"])
    grenze = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minuten)
    typ_ok = ("CAROUSEL_ALBUM", "IMAGE") if plan["typ"] == "karussell" else ("VIDEO", "REELS")
    for m in daten:
        try:
            zeit = dt.datetime.strptime(m["timestamp"], "%Y-%m-%dT%H:%M:%S%z")
        except Exception:
            continue
        if zeit < grenze:
            continue
        if m.get("media_type") and m["media_type"] not in typ_ok:
            continue
        if _caption_norm(m.get("caption")) == ziel:
            return m.get("permalink") or m["id"]
    return None


def _publish(plan):
    base = os.environ["PAGES_URL"].rstrip("/") + "/"
    st = load_status()
    user = ig("GET", "me", fields="user_id,username")
    uid = user["user_id"]
    print("Poste auf @" + user.get("username", "?"))
    if not plan.get("test"):
        link = schon_online(uid, plan, st)
        if link:
            print("Dieser Post ist schon online, wird NICHT nochmal gepostet:", link)
            _als_gepostet(plan, st, link, hinweis="schon online")
            return
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
    try:
        media_id = ig("POST", f"{uid}/media_publish", creation_id=cid)["id"]
    except Exception as fehler:
        # Instagram meldet hier manchmal einen Fehler, obwohl der Post online gegangen ist.
        print("Fehler beim Veröffentlichen, prüfe ob der Post trotzdem online ist:", fehler)
        for _ in range(4):
            time.sleep(20)
            link = schon_online(uid, plan, st, minuten=30)
            if link:
                print("Post ist trotz Fehlermeldung online:", link)
                _als_gepostet(plan, st, link)
                return
        raise
    print("Veröffentlicht, Medien-ID", media_id)
    try:
        link = ig("GET", media_id, fields="permalink").get("permalink", "")
    except Exception:
        link = ""
    _als_gepostet(plan, st, link)


def _als_gepostet(plan, st, link, hinweis=""):
    jetzt = dt.datetime.now(TZ)
    cap = " / ".join(l.strip() for l in plan["caption"].splitlines() if l.strip())
    test = "ja" if plan.get("test") else ""
    if hinweis:
        test = (test + " " + hinweis).strip()
    # YouTube- und Facebook-Spalte: Karussells und Testposts gehen nie dorthin ("–"),
    # bei Reels tragen der YouTube- und der Facebook-Schritt danach ihren Stand ein.
    yt = fb = "–" if plan["typ"] == "karussell" or plan.get("test") else ""
    _csv_kopf_aktualisieren()
    write_csv(GEPOSTET_CSV, CSV_KOPF, [[jetzt.strftime("%d.%m.%Y"), jetzt.strftime("%H:%M"),
              "Reel" if plan["typ"] == "reel" else "Karussell", plan["art"], kurzname(plan["quelle"]),
              cap[:120], link, test, yt, fb]], append=True)

    st["gepostet"].append(plan["quelle"])
    if plan.get("termin"):
        st["termine_erledigt"].append(plan["termin"])
    if plan["typ"] == "reel" and not plan.get("extra"):
        st["letzter_reel_typ"] = plan["art"]
    if plan["typ"] == "karussell" and not plan.get("extra") and not plan.get("test"):
        st["letzte_karussell_art"] = plan["art"]
    st["verlauf"].append({"zeit": dt.datetime.now(TZ).isoformat(timespec="minutes"), "typ": plan["typ"],
                          "art": plan["art"], "quelle": plan["quelle"], "test": plan.get("test", False)})
    st["termine_erledigt"] = st["termine_erledigt"][-200:]
    save_status(st)
    try:
        refresh_token(st)
    finally:
        save_status(st)
    cmd_vorschau(14, ausgabe=False)


# ---------------------------------------------------------------- YouTube Shorts
# Regeln (Konzept vom 03.11.2026, Details in einstellungen.yaml unter "youtube"):
# - YouTube hängt an Instagram: Jedes Reel, das auf Instagram gepostet wurde, wird danach zu YouTube
#   hochgeladen. Karussells und Testposts nie. Ein YouTube-Fehler stoppt nie Instagram.
# - Solange youtube -> aktiv: false, wird nichts automatisch hochgeladen (Uploads über ein ungeprüftes
#   Google-Projekt wären dauerhaft "gesperrt als privat").
# - Titel = Zeile 1 der Reel-Caption (Text der .txt, sonst fester hook). Über 100 Zeichen: fester hook.
#   Nie abschneiden. Beschreibung = youtube_text des Typs + Schlusssatz.
# - Doppel-Schutz: Jedes Reel wird höchstens einmal hochgeladen (status/gepostet.json -> youtube).
#   Vor dem Upload steht dort "unklar". Nur ein eindeutig abgelehnter Upload wird (im selben Lauf,
#   höchstens 3-mal) wiederholt. Ist unklar, ob das Video angekommen ist: kein neuer Versuch + Mail.
YT_TOKEN_URL = "https://oauth2.googleapis.com/token"
YT_UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
YT_TITEL_MAX = 100
YT_BESCHREIBUNG_MAX = 4900  # YouTube-Grenze 5000 Bytes, mit Puffer
YT_VERSUCHE = 3
YT_SCHLUESSEL_HILFE = (
    "So erneuerst du den YouTube-Schlüssel: developers.google.com/oauthplayground öffnen -> Zahnrad -> "
    "'Use your own OAuth credentials' mit Client-ID und Clientschlüssel aus deiner JSON-Datei -> Scope "
    "https://www.googleapis.com/auth/youtube.upload -> Authorize APIs (Konto kontakt.niaki, Kanal 9x KLUG) -> "
    "Exchange authorization code for tokens -> Refresh token kopieren -> bei GitHub das Secret "
    "YT_REFRESH_TOKEN ersetzen.")


class YtFehler(Exception):
    """klar=True: YouTube hat eindeutig abgelehnt, es gibt sicher kein Video (Wiederholen ist sicher).
    klar=False: unklar, ob das Video angekommen ist (nicht wiederholen). schluessel=True: Schlüssel ungültig."""

    def __init__(self, text, klar=True, schluessel=False):
        super().__init__(text)
        self.klar = klar
        self.schluessel = schluessel


def yt_cfg(cfg):
    return cfg.get("youtube") or {}


def yt_eintrag(cfg, art):
    """Der Reel-Typ (aus reel_rhythmus oder extra_termine) mit diesem Ordnernamen."""
    for e in list(cfg.get("reel_rhythmus") or []) + [x for x in extra_termine(cfg) if x.get("typ") == "reel"]:
        if norm(e["ordner"]) == norm(art):
            return e
    return None


def _yt_sauber(text):
    """YouTube verbietet < und > in Titel und Beschreibung."""
    return str(text or "").replace("<", "").replace(">", "").strip()


def yt_texte(cfg, entry, video):
    """Gibt (titel, beschreibung, hinweis) zurück. titel ist None, wenn kein gültiger Titel möglich ist."""
    yc = yt_cfg(cfg)
    hinweise = []
    fest = " ".join(_yt_sauber(entry.get("hook")).split())
    txt, _ = begleittext(video)
    titel = " ".join(_yt_sauber(txt).split()) if txt else fest
    if len(titel) > YT_TITEL_MAX:
        hinweise.append(f"Titel aus der Textdatei ist länger als {YT_TITEL_MAX} Zeichen, fester hook des Typs verwendet.")
        titel = fest
    if not titel or len(titel) > YT_TITEL_MAX:
        return None, "", f"Kein gültiger YouTube-Titel für {rel(video)} (hook fehlt oder ist länger als {YT_TITEL_MAX} Zeichen)."
    teile = []
    eigen = _yt_sauber(entry.get("youtube_text"))
    if eigen:
        teile.append(eigen)
    else:
        hinweise.append(f"Reel-Typ '{entry['ordner']}' hat keinen youtube_text in einstellungen.yaml, nur Schlusssatz verwendet.")
    if _yt_sauber(yc.get("schluss")):
        teile.append(_yt_sauber(yc["schluss"]))
    if yc.get("hashtags_anhaengen") and str(entry.get("hashtags") or "").strip():
        teile.append(_yt_sauber(entry["hashtags"]))
    if yc.get("impressum_zeile") and _yt_sauber(yc.get("impressum_url")):
        teile.append("Impressum: " + _yt_sauber(yc["impressum_url"]))
    beschreibung = "\n\n".join(teile)
    if len(beschreibung.encode("utf-8")) > YT_BESCHREIBUNG_MAX:
        hinweise.append("YouTube-Beschreibung zu lang, nur Schlusssatz verwendet.")
        beschreibung = _yt_sauber(yc.get("schluss"))
    return titel, beschreibung, " ".join(hinweise) or None


def _yt_texte_sicher(cfg, entry, video):
    try:
        return yt_texte(cfg, entry, video)
    except Exception as fehler:
        return None, "", f"Fehler: {fehler}"


def yt_token():
    import requests
    try:
        r = requests.post(YT_TOKEN_URL, data={"client_id": os.environ.get("YT_CLIENT_ID", ""),
                                              "client_secret": os.environ.get("YT_CLIENT_SECRET", ""),
                                              "refresh_token": os.environ.get("YT_REFRESH_TOKEN", ""),
                                              "grant_type": "refresh_token"}, timeout=60)
        data = r.json()
    except Exception as fehler:
        raise YtFehler(f"Anmeldung bei Google nicht möglich: {fehler}")
    if "access_token" in data:
        return data["access_token"]
    if data.get("error") in ("invalid_grant", "invalid_client", "unauthorized_client"):
        raise YtFehler(f"YouTube-Schlüssel ungültig oder abgelaufen ({data.get('error')}).", schluessel=True)
    raise YtFehler(f"Anmeldung bei Google abgelehnt: {data}")


def _yt_stand(session_url, groesse, token):
    """Fragt bei YouTube nach, wie weit der Upload ist. ('fertig', id) / ('weiter', ab_byte) / None (unklar)."""
    import requests
    try:
        r = requests.put(session_url, headers={"Authorization": "Bearer " + token, "Content-Length": "0",
                                               "Content-Range": f"bytes */{groesse}"}, timeout=60)
    except Exception:
        return None
    if r.status_code in (200, 201):
        try:
            return "fertig", r.json()["id"]
        except Exception:
            return None
    if r.status_code == 308:
        bereich = r.headers.get("Range", "")
        m = re.match(r"bytes=0-(\d+)", bereich)
        return "weiter", (int(m.group(1)) + 1) if m else 0
    if r.status_code in (404, 410):
        return "abgelaufen", None  # Sitzung verfallen, Upload nicht abgeschlossen -> kein Video
    return None


def yt_hochladen(video, titel, beschreibung, yc, privat=False):
    """Lädt das Video hoch (fortsetzbarer Upload) und gibt die Video-ID zurück. Wirft YtFehler."""
    import requests
    token = yt_token()
    meta = {
        "snippet": {"title": titel, "description": beschreibung,
                    "categoryId": str(yc.get("kategorie", 24)),
                    "defaultLanguage": str(yc.get("sprache", "de")),
                    "defaultAudioLanguage": str(yc.get("sprache", "de"))},
        "status": {"privacyStatus": "private" if privat else str(yc.get("sichtbarkeit", "public")),
                   "selfDeclaredMadeForKids": bool(yc.get("fuer_kinder", False)),
                   "containsSyntheticMedia": bool(yc.get("ki_inhalt", False))},
    }
    daten = video.read_bytes()
    groesse = len(daten)
    if not groesse:
        raise YtFehler(f"Video {rel(video)} ist leer.")
    try:
        r = requests.post(YT_UPLOAD_URL, params={"uploadType": "resumable", "part": "snippet,status",
                                                 "notifySubscribers": "true" if yc.get("abonnenten_benachrichtigen", True) else "false"},
                          headers={"Authorization": "Bearer " + token, "Content-Type": "application/json; charset=UTF-8",
                                   "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(groesse)},
                          data=json.dumps(meta).encode("utf-8"), timeout=60)
    except Exception as fehler:
        raise YtFehler(f"Upload-Start nicht möglich: {fehler}")  # ohne Sitzung gibt es kein Video
    if r.status_code != 200 or not r.headers.get("Location"):
        raise YtFehler(f"YouTube hat den Upload abgelehnt ({r.status_code}): {r.text[:400]}")
    session_url = r.headers["Location"]
    ab = 0
    for _ in range(5):
        try:
            kopf = {"Authorization": "Bearer " + token, "Content-Length": str(groesse - ab)}
            if ab:
                kopf["Content-Range"] = f"bytes {ab}-{groesse - 1}/{groesse}"
            else:
                kopf["Content-Type"] = "video/mp4"
            r = requests.put(session_url, headers=kopf, data=daten[ab:], timeout=900)
            if r.status_code in (200, 201):
                return r.json()["id"]
            if 400 <= r.status_code < 500 and r.status_code not in (408, 429):
                raise YtFehler(f"YouTube hat das Video abgelehnt ({r.status_code}): {r.text[:400]}")
        except YtFehler:
            raise
        except Exception as fehler:
            print("Verbindung beim Upload unterbrochen, frage Stand ab:", fehler)
        time.sleep(20)
        stand = _yt_stand(session_url, groesse, token)
        if stand and stand[0] == "fertig":
            return stand[1]
        if stand and stand[0] == "weiter":
            ab = stand[1]
            continue
        if stand and stand[0] == "abgelaufen":
            raise YtFehler("Upload-Sitzung abgelaufen, Video wurde nicht angelegt.")
    raise YtFehler("Nicht sicher, ob das Video bei YouTube angekommen ist.", klar=False)


def yt_link(video_id):
    return f"https://youtube.com/shorts/{video_id}"


def _yt_versuchen(cfg, st, quelle, art):
    """Lädt ein auf Instagram gepostetes Reel zu YouTube hoch, mit Doppel-Schutz. Speichert den Status."""
    yts = st.setdefault("youtube", {})
    if (yts.get(quelle) or {}).get("status") == "ok":
        print("Schon auf YouTube, wird nicht nochmal hochgeladen:", quelle)
        return True
    entry = yt_eintrag(cfg, art)
    video = MEDIA_ROOT / quelle
    name = kurzname(quelle)
    if not entry:
        grund = f"Reel-Typ '{art}' nicht in einstellungen.yaml gefunden"
        yts[quelle] = {"status": "fehlgeschlagen", "grund": grund, "zeit": dt.datetime.now(TZ).isoformat(timespec="minutes")}
        save_status(st)
        csv_youtube_eintragen(quelle, "fehlgeschlagen (nachholen möglich)")
        warnung(f"YouTube: Upload für {name} nicht möglich ({grund}).")
        return False
    titel, beschreibung, hinweis = yt_texte(cfg, entry, video)
    if hinweis:
        warnung("YouTube: " + hinweis)
    if not titel:
        yts[quelle] = {"status": "fehlgeschlagen", "grund": "kein gültiger Titel", "zeit": dt.datetime.now(TZ).isoformat(timespec="minutes")}
        save_status(st)
        csv_youtube_eintragen(quelle, "fehlgeschlagen (nachholen möglich)")
        return False
    hole_dateien([video])
    # Erst "unklar" speichern: bricht der Lauf mittendrin ab, wird nie automatisch nochmal hochgeladen.
    yts[quelle] = {"status": "unklar", "zeit": dt.datetime.now(TZ).isoformat(timespec="minutes")}
    save_status(st)
    csv_youtube_eintragen(quelle, "unklar: bitte im YouTube Studio prüfen")
    fehler = None
    for versuch in range(1, YT_VERSUCHE + 1):
        try:
            vid = yt_hochladen(video, titel, beschreibung, yt_cfg(cfg))
            yts[quelle] = {"status": "ok", "id": vid, "titel": titel,
                           "zeit": dt.datetime.now(TZ).isoformat(timespec="minutes")}
            save_status(st)
            csv_youtube_eintragen(quelle, yt_link(vid))
            print("Auf YouTube hochgeladen:", yt_link(vid))
            return True
        except YtFehler as f:
            fehler = f
            print(f"YouTube-Versuch {versuch} fehlgeschlagen:", f)
            if not f.klar or f.schluessel:
                break
            if versuch < YT_VERSUCHE:
                time.sleep(60)
    jetzt = dt.datetime.now(TZ).isoformat(timespec="minutes")
    if not fehler.klar:
        yts[quelle] = {"status": "unklar", "grund": str(fehler), "zeit": jetzt}
        save_status(st)
        warnung(f"YouTube: Upload von {name} unklar. Bitte im YouTube Studio nachschauen, ob das Short da ist. "
                f"Falls nicht: Actions -> Run workflow -> youtube-nachholen -> Datei {name}. Grund: {fehler}")
        return False
    yts[quelle] = {"status": "fehlgeschlagen", "grund": str(fehler), "zeit": jetzt}
    save_status(st)
    csv_youtube_eintragen(quelle, "fehlgeschlagen (nachholen möglich)")
    extra = (" " + YT_SCHLUESSEL_HILFE) if fehler.schluessel else ""
    warnung(f"YouTube: Upload von {name} fehlgeschlagen: {fehler}.{extra} Danach nachholen: Actions -> Run workflow -> "
            f"youtube-nachholen -> Datei {name}")
    return False


def cmd_youtube():
    """Läuft direkt nach dem Instagram-Post: lädt dasselbe Reel zu YouTube hoch."""
    if not PLAN_FILE.exists():
        return
    plan = json.loads(PLAN_FILE.read_text(encoding="utf-8"))
    if plan.get("typ") != "reel" or plan.get("test"):
        print("Kein Reel (oder Testpost), nichts für YouTube.")
        return
    cfg, st = load_cfg(), load_status()
    quelle = plan["quelle"]
    if quelle not in st["gepostet"]:
        print("Reel ist nicht auf Instagram gepostet, nichts für YouTube.")
        return
    if not yt_cfg(cfg).get("aktiv"):
        st.setdefault("youtube", {}).setdefault(quelle, {"status": "aus"})
        save_status(st)
        csv_youtube_eintragen(quelle, "aus")
        print("YouTube ist ausgeschaltet (youtube -> aktiv: false).")
        return
    _yt_versuchen(cfg, st, quelle, plan["art"])


def _art_aus_quelle(quelle):
    teile = quelle.split("/")
    return teile[1] if len(teile) >= 3 else ""


def cmd_youtube_nachholen(datei):
    """Lädt ein auf Instagram gepostetes Reel nach, dessen YouTube-Upload fehlt (nie eines, das schon oben ist)."""
    cfg, st = load_cfg(), load_status()
    if not yt_cfg(cfg).get("aktiv"):
        print("YouTube ist ausgeschaltet (youtube -> aktiv: false). Nachholen erst nach der Genehmigung.")
        sys.exit(1)
    datei = (datei or "").strip().replace("\\", "/")
    if not datei:
        print("Bitte bei 'datei' den Dateinamen eintragen (wie in gepostet.csv, Spalte Datei/Ordner).")
        sys.exit(1)
    reels = [q for q in dict.fromkeys(st["gepostet"]) if q.startswith("reels/")]
    treffer = [q for q in reels if norm(kurzname(q)) == norm(datei) or norm(q) == norm(datei)]
    if not treffer:
        treffer = [q for q in reels if norm(q.rsplit("/", 1)[-1]) == norm(datei.rsplit("/", 1)[-1])]
    if not treffer:
        print(f"'{datei}' ist kein auf Instagram gepostetes Reel. Schreibweise wie in gepostet.csv prüfen.")
        sys.exit(1)
    if len(treffer) > 1:
        print("Mehrere Reels passen, bitte mit Ordner angeben:", ", ".join(kurzname(q) for q in treffer))
        sys.exit(1)
    quelle = treffer[0]
    if (st.get("youtube", {}).get(quelle) or {}).get("status") == "ok":
        print("Dieses Reel ist schon auf YouTube:", yt_link(st["youtube"][quelle]["id"]))
        sys.exit(1)
    if not _yt_versuchen(cfg, st, quelle, _art_aus_quelle(quelle)):
        sys.exit(1)


def cmd_youtube_test():
    """Nachweis fürs Prüfungsformular: lädt das zuletzt auf Instagram gepostete Reel PRIVAT hoch.
    Zählt nicht als echter Upload (das Reel kann später ganz normal hochgeladen werden)."""
    cfg, st = load_cfg(), load_status()
    reels = [v for v in st.get("verlauf", []) if v.get("typ") == "reel" and not v.get("test")]
    if not reels:
        print("Noch kein Reel auf Instagram gepostet, nichts zum Testen.")
        sys.exit(1)
    quelle, art = reels[-1]["quelle"], reels[-1]["art"]
    entry = yt_eintrag(cfg, art)
    if not entry:
        print(f"Reel-Typ '{art}' nicht in einstellungen.yaml gefunden.")
        sys.exit(1)
    video = MEDIA_ROOT / quelle
    titel, beschreibung, hinweis = yt_texte(cfg, entry, video)
    if hinweis:
        print("Hinweis:", hinweis)
    if not titel:
        sys.exit(1)
    hole_dateien([video])
    print(f"Test-Upload (privat): {kurzname(quelle)}\nTitel: {titel}\nBeschreibung:\n{beschreibung}\n")
    try:
        vid = yt_hochladen(video, titel, beschreibung, yt_cfg(cfg), privat=True)
    except YtFehler as f:
        print("Test-Upload fehlgeschlagen:", f)
        if f.schluessel:
            print(YT_SCHLUESSEL_HILFE)
        sys.exit(1)
    st.setdefault("youtube_tests", []).append({"zeit": dt.datetime.now(TZ).isoformat(timespec="minutes"),
                                               "quelle": quelle, "id": vid})
    save_status(st)
    text = (f"Test-Upload erfolgreich: {yt_link(vid)} (privat). Titel: {titel}")
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("## YouTube-Test\n\n" + text + "\n")


# ---------------------------------------------------------------- Facebook-Seite (Reels)
# Regeln (Konzept vom 07.11.2026, Details in einstellungen.yaml unter "facebook"):
# - Facebook hängt an Instagram: Jedes Reel, das auf Instagram gepostet wurde, geht danach im selben
#   Lauf auch als Reel auf die Facebook-Seite 9x KLUG. Karussells und Testposts nie.
#   Ein Facebook-Fehler stoppt nie Instagram (und nie YouTube).
# - Text = Zeile 1 aus der gleichnamigen .txt des Reels (falls vorhanden), darunter facebook_text des Typs.
#   Reels ohne .txt bekommen nur facebook_text. Keine Hashtags.
# - Eigener Stand je Reel in status/gepostet.json -> facebook und Spalte "Facebook" in gepostet.csv.
#   Damit lässt sich später ein eigener Facebook-Zeitplan nachrüsten (Reels, die dort noch fehlen).
# - Doppel-Schutz: Jedes Reel wird höchstens einmal veröffentlicht. Vor dem Veröffentlichen steht
#   "unklar" im Status. Hochladen (noch nicht öffentlich) darf wiederholt werden, Veröffentlichen nicht.
#   Ist unklar, ob das Reel online ging, fragt das Programm bei Facebook nach. Bleibt es unklar:
#   kein neuer Versuch + Mail.
# - Das Video wird direkt hochgeladen (nicht über einen Link), braucht also keine GitHub-Pages-Seite.
FB_API = "https://graph.facebook.com"
FB_UPLOAD = "https://rupload.facebook.com/video-upload"
FB_VERSUCHE = 3
FB_TEXT_MAX = 5000  # Facebook erlaubt deutlich mehr, Puffer gegen Fehler in den Einstellungen
FB_SCHLUESSEL_HILFE = (
    "So erneuerst du den Facebook-Schlüssel: developers.facebook.com/tools/explorer -> App '9xklug Autopost' -> "
    "Nutzer-Token anfordern mit pages_show_list, pages_read_engagement, pages_manage_posts -> Generate Access Token "
    "(nur Seite 9x KLUG) -> 'i' -> Im Zugriffsschlüssel-Tool öffnen -> Zugriffsschlüssel verlängern -> den verlängerten "
    "Schlüssel im Explorer einsetzen -> me/accounts?fields=id,name,access_token senden -> access_token von 9x KLUG "
    "im Zugriffsschlüssel-Tool prüfen (Läuft ab: Nie) -> bei GitHub das Secret FB_PAGE_TOKEN ersetzen.")


class FbFehler(Exception):
    """klar=True: sicher nicht veröffentlicht (Wiederholen ist sicher). klar=False: unklar, ob online.
    schluessel=True: Schlüssel ungültig oder Rechte fehlen."""

    def __init__(self, text, klar=True, schluessel=False):
        super().__init__(text)
        self.klar = klar
        self.schluessel = schluessel


def fb_cfg(cfg):
    return cfg.get("facebook") or {}


def fb_text(cfg, entry, video):
    """Gibt (text, hinweis) zurück. text ist None, wenn kein Text möglich ist."""
    hinweise = []
    txt, datei = begleittext(video)
    if datei and not txt:
        hinweise.append(f"Textdatei {rel(datei)} ist leer.")
    eigen = str(entry.get("facebook_text") or "").strip()
    if eigen:
        eigen = "\n".join(l.rstrip() for l in eigen.splitlines()).strip()
    teile = [t for t in (txt, eigen) if t]
    if not eigen:
        hinweise.append(f"Reel-Typ '{entry['ordner']}' hat keinen facebook_text in einstellungen.yaml.")
        if not txt:
            fest = str(entry.get("hook") or "").strip()
            if fest:
                teile = [fest]
                hinweise.append("Fester hook des Typs als Text verwendet.")
    if not teile:
        return None, " ".join(hinweise + [f"Kein Facebook-Text für {rel(video)} möglich."])
    text = "\n\n".join(teile)
    if len(text) > FB_TEXT_MAX:
        return None, " ".join(hinweise + [f"Facebook-Text für {rel(video)} ist länger als {FB_TEXT_MAX} Zeichen."])
    return text, " ".join(hinweise) or None


def _fb_text_sicher(cfg, entry, video):
    try:
        return fb_text(cfg, entry, video)
    except Exception as fehler:
        return None, f"Fehler: {fehler}"


def _fb_version(cfg):
    return str(fb_cfg(cfg).get("api_version") or "v25.0")


def fb_api(cfg, method, path, **params):
    """Ruft die Facebook-Schnittstelle auf. Wirft FbFehler (klar=True), wenn Facebook ablehnt."""
    import requests
    params["access_token"] = os.environ.get("FB_PAGE_TOKEN", "")
    url = f"{FB_API}/{_fb_version(cfg)}/{path}"
    try:
        r = requests.request(method, url, data=params if method == "POST" else None,
                             params=None if method == "POST" else params, timeout=120)
        data = r.json()
    except Exception as fehler:
        raise FbFehler(f"Facebook nicht erreichbar ({path}): {fehler}", klar=False)
    if r.status_code >= 400 or "error" in data:
        err = data.get("error") or {}
        code = err.get("code")
        # 190/102 = Schlüssel ungültig oder abgelaufen, 10 und 200-299 = Berechtigung fehlt
        schluessel = isinstance(code, int) and (code in (190, 102, 10) or 200 <= code <= 299)
        raise FbFehler(f"Facebook-Fehler bei {path}: {err.get('message') or data}", schluessel=schluessel)
    return data


def fb_seite_pruefen(cfg):
    """Prüft Schlüssel und Seite. Gibt den Seitennamen zurück."""
    if not os.environ.get("FB_PAGE_TOKEN") or not os.environ.get("FB_PAGE_ID"):
        raise FbFehler("GitHub-Secret FB_PAGE_TOKEN oder FB_PAGE_ID fehlt.", schluessel=True)
    return fb_api(cfg, "GET", os.environ["FB_PAGE_ID"], fields="name").get("name", "?")


def fb_status(cfg, video_id):
    """Stand eines Facebook-Videos: ('online'|'verarbeitung'|'nicht_veroeffentlicht'|'fehler'|'unbekannt', permalink).
    'nicht_veroeffentlicht' = hochgeladen, aber das Veröffentlichen hat (noch) nicht begonnen."""
    try:
        d = fb_api(cfg, "GET", video_id, fields="status,permalink_url")
    except FbFehler:
        return "unbekannt", ""
    s = d.get("status") or {}
    link = d.get("permalink_url") or ""
    if link.startswith("/"):
        link = "https://www.facebook.com" + link
    pub = (s.get("publishing_phase") or {}).get("status")
    vs = s.get("video_status")
    if vs == "error" or pub == "error" or (s.get("processing_phase") or {}).get("status") == "error":
        return "fehler", link
    if pub == "complete":
        return "online", link
    if pub == "in_progress":
        return "verarbeitung", link
    if pub == "not_started":
        return "nicht_veroeffentlicht", link
    if vs in ("ready", "published"):
        return "online", link
    return "unbekannt", link


def fb_hochladen(cfg, video):
    """Legt das Reel bei Facebook an und lädt das Video hoch (noch NICHT veröffentlicht). Gibt die Video-ID zurück.
    Ein Fehler hier ist sicher (es ist nichts online), daher klar=True."""
    import requests
    page = os.environ["FB_PAGE_ID"]
    daten = video.read_bytes()
    if not daten:
        raise FbFehler(f"Video {rel(video)} ist leer.")
    start = fb_api(cfg, "POST", f"{page}/video_reels", upload_phase="start")
    vid = start.get("video_id")
    if not vid:
        raise FbFehler(f"Facebook hat keine Video-ID geliefert: {start}")
    try:
        r = requests.post(start.get("upload_url") or f"{FB_UPLOAD}/{_fb_version(cfg)}/{vid}",
                          headers={"Authorization": "OAuth " + os.environ["FB_PAGE_TOKEN"],
                                   "offset": "0", "file_size": str(len(daten))},
                          data=daten, timeout=900)
        antwort = r.json()
    except Exception as fehler:
        raise FbFehler(f"Video-Upload zu Facebook unterbrochen: {fehler}")
    if r.status_code >= 400 or not antwort.get("success"):
        raise FbFehler(f"Facebook hat das Video abgelehnt ({r.status_code}): {str(antwort)[:400]}")
    return vid


def fb_veroeffentlichen(cfg, vid, text, entwurf=False):
    """Veröffentlicht das hochgeladene Reel (oder legt es als Entwurf ab). Gibt den Link zurück (kann leer sein).
    Jeder unklare Fehler hier ist NICHT sicher wiederholbar."""
    page = os.environ["FB_PAGE_ID"]
    try:
        fb_api(cfg, "POST", f"{page}/video_reels", upload_phase="finish", video_id=vid,
               video_state="DRAFT" if entwurf else "PUBLISHED", description=text)
    except FbFehler as fehler:
        if fehler.schluessel:
            raise
        # Facebook meldet manchmal einen Fehler, obwohl das Reel angenommen wurde -> nachfragen.
        time.sleep(20)
        stand, link = fb_status(cfg, vid)
        if stand in ("online", "verarbeitung") and not entwurf:
            return link
        # Sicher nicht online: Facebook hat eindeutig abgelehnt, oder es meldet selbst "nicht veröffentlicht".
        # Verbindungsfehler ohne klare Antwort und ohne klaren Stand = unklar, nicht wiederholen.
        klar = stand == "nicht_veroeffentlicht" or (fehler.klar and stand in ("fehler", "unbekannt"))
        raise FbFehler(f"{fehler} (Stand bei Facebook: {stand})", klar=klar)
    if entwurf:
        return ""
    # Auf die Verarbeitung warten (höchstens ca. 5 Minuten). Bleibt sie länger, gilt das Reel als gepostet,
    # denn Facebook hat es angenommen und stellt es nach der Verarbeitung von selbst online.
    link = ""
    for _ in range(20):
        time.sleep(15)
        stand, link = fb_status(cfg, vid)
        if stand == "online":
            return link
        if stand == "fehler":
            # Verarbeitung fehlgeschlagen: das Reel ist nicht online, Nachholen ist sicher.
            raise FbFehler("Facebook konnte das Reel nicht verarbeiten (Status error)")
    print("Facebook verarbeitet das Reel noch, es geht von selbst online.")
    return link


def fb_link(vid, link=""):
    return link or f"https://www.facebook.com/reel/{vid}"


def _fb_versuchen(cfg, st, quelle, art):
    """Postet ein auf Instagram gepostetes Reel auf die Facebook-Seite, mit Doppel-Schutz. Speichert den Status."""
    fbs = st.setdefault("facebook", {})
    if (fbs.get(quelle) or {}).get("status") == "ok":
        print("Schon auf Facebook, wird nicht nochmal gepostet:", quelle)
        return True
    if (fbs.get(quelle) or {}).get("status") == "unklar":
        print("Stand auf Facebook unklar, wird zur Sicherheit nicht nochmal gepostet:", quelle)
        return False
    entry = yt_eintrag(cfg, art)
    video = MEDIA_ROOT / quelle
    name = kurzname(quelle)
    jetzt = lambda: dt.datetime.now(TZ).isoformat(timespec="minutes")

    def fehlgeschlagen(grund, mail):
        fbs[quelle] = {"status": "fehlgeschlagen", "grund": grund, "zeit": jetzt()}
        save_status(st)
        csv_facebook_eintragen(quelle, "fehlgeschlagen (nachholen möglich)")
        warnung(mail)
        return False

    if not entry:
        return fehlgeschlagen(f"Reel-Typ '{art}' nicht in einstellungen.yaml",
                              f"Facebook: Reel {name} nicht gepostet (Reel-Typ '{art}' nicht in einstellungen.yaml).")
    text, hinweis = fb_text(cfg, entry, video)
    if hinweis:
        warnung("Facebook: " + hinweis)
    if not text:
        fbs[quelle] = {"status": "fehlgeschlagen", "grund": "kein Text", "zeit": jetzt()}
        save_status(st)
        csv_facebook_eintragen(quelle, "fehlgeschlagen (nachholen möglich)")
        return False
    try:
        fb_seite_pruefen(cfg)
    except FbFehler as f:
        extra = (" " + FB_SCHLUESSEL_HILFE) if f.schluessel else ""
        return fehlgeschlagen(str(f), f"Facebook: Reel {name} nicht gepostet: {f}.{extra} Danach nachholen: "
                                      f"Actions -> Run workflow -> facebook-nachholen -> Datei {name}")
    hole_dateien([video])

    # 1) Hochladen (noch nicht öffentlich). Fehler hier sind sicher, bis zu 3 Versuche.
    vid, fehler = None, None
    for versuch in range(1, FB_VERSUCHE + 1):
        try:
            vid = fb_hochladen(cfg, video)
            break
        except FbFehler as f:
            fehler = f
            print(f"Facebook-Upload Versuch {versuch} fehlgeschlagen:", f)
            if f.schluessel:
                break
            if versuch < FB_VERSUCHE:
                time.sleep(60)
    if not vid:
        extra = (" " + FB_SCHLUESSEL_HILFE) if fehler.schluessel else ""
        return fehlgeschlagen(str(fehler), f"Facebook: Reel {name} nicht gepostet: {fehler}.{extra} Danach nachholen: "
                                           f"Actions -> Run workflow -> facebook-nachholen -> Datei {name}")

    # 2) Veröffentlichen. Erst "unklar" speichern: bricht der Lauf ab, wird nie automatisch nochmal gepostet.
    fbs[quelle] = {"status": "unklar", "id": vid, "zeit": jetzt()}
    save_status(st)
    csv_facebook_eintragen(quelle, "unklar: bitte auf der Facebook-Seite prüfen")
    try:
        link = fb_veroeffentlichen(cfg, vid, text)
    except FbFehler as f:
        if f.klar and not f.schluessel:
            return fehlgeschlagen(str(f), f"Facebook: Reel {name} nicht gepostet: {f}. Danach nachholen: "
                                          f"Actions -> Run workflow -> facebook-nachholen -> Datei {name}")
        fbs[quelle] = {"status": "unklar", "id": vid, "grund": str(f), "zeit": jetzt()}
        save_status(st)
        extra = (" " + FB_SCHLUESSEL_HILFE) if f.schluessel else ""
        warnung(f"Facebook: Ob {name} online ist, ist unklar. Bitte auf der Facebook-Seite (Reels) nachschauen. "
                f"Falls es fehlt: Actions -> Run workflow -> facebook-nachholen -> Datei {name}.{extra} Grund: {f}")
        return False
    fbs[quelle] = {"status": "ok", "id": vid, "link": fb_link(vid, link), "zeit": jetzt()}
    save_status(st)
    csv_facebook_eintragen(quelle, fb_link(vid, link))
    print("Auf Facebook gepostet:", fb_link(vid, link))
    return True


def cmd_facebook():
    """Läuft direkt nach dem Instagram-Post: postet dasselbe Reel auf die Facebook-Seite."""
    if not PLAN_FILE.exists():
        return
    plan = json.loads(PLAN_FILE.read_text(encoding="utf-8"))
    if plan.get("typ") != "reel" or plan.get("test"):
        print("Kein Reel (oder Testpost), nichts für Facebook.")
        return
    cfg, st = load_cfg(), load_status()
    quelle = plan["quelle"]
    if quelle not in st["gepostet"]:
        print("Reel ist nicht auf Instagram gepostet, nichts für Facebook.")
        return
    if not fb_cfg(cfg).get("aktiv"):
        st.setdefault("facebook", {}).setdefault(quelle, {"status": "aus"})
        save_status(st)
        csv_facebook_eintragen(quelle, "aus")
        print("Facebook ist ausgeschaltet (facebook -> aktiv: false).")
        return
    _fb_versuchen(cfg, st, quelle, plan["art"])


def cmd_facebook_nachholen(datei):
    """Postet ein auf Instagram gepostetes Reel nach, das auf Facebook fehlt (nie eines, das schon dort ist)."""
    cfg, st = load_cfg(), load_status()
    if not fb_cfg(cfg).get("aktiv"):
        print("Facebook ist ausgeschaltet (facebook -> aktiv: false). Erst einschalten, dann nachholen.")
        sys.exit(1)
    datei = (datei or "").strip().replace("\\", "/")
    if not datei:
        print("Bitte bei 'datei' den Dateinamen eintragen (wie in gepostet.csv, Spalte Datei/Ordner).")
        sys.exit(1)
    reels = [q for q in dict.fromkeys(st["gepostet"]) if q.startswith("reels/")]
    treffer = [q for q in reels if norm(kurzname(q)) == norm(datei) or norm(q) == norm(datei)]
    if not treffer:
        treffer = [q for q in reels if norm(q.rsplit("/", 1)[-1]) == norm(datei.rsplit("/", 1)[-1])]
    if not treffer:
        print(f"'{datei}' ist kein auf Instagram gepostetes Reel. Schreibweise wie in gepostet.csv prüfen.")
        sys.exit(1)
    if len(treffer) > 1:
        print("Mehrere Reels passen, bitte mit Ordner angeben:", ", ".join(kurzname(q) for q in treffer))
        sys.exit(1)
    quelle = treffer[0]
    eintrag = st.get("facebook", {}).get(quelle) or {}
    if eintrag.get("status") == "ok":
        print("Dieses Reel ist schon auf Facebook:", eintrag.get("link") or eintrag.get("id"))
        sys.exit(1)
    if eintrag.get("status") == "unklar":
        # Du hast auf der Seite nachgeschaut und es fehlt: Sperre aufheben.
        print("Stand war unklar, du hast nachgeschaut und das Reel fehlt: wird jetzt gepostet.")
        st["facebook"].pop(quelle, None)
    if not _fb_versuchen(cfg, st, quelle, _art_aus_quelle(quelle)):
        sys.exit(1)


def cmd_facebook_test():
    """Test: legt das zuletzt auf Instagram gepostete Reel als ENTWURF auf der Facebook-Seite an (nicht öffentlich).
    Zählt nicht als echter Post. Den Entwurf findest du in der Meta Business Suite unter Inhalte -> Entwürfe."""
    cfg, st = load_cfg(), load_status()
    try:
        seite = fb_seite_pruefen(cfg)
    except FbFehler as f:
        print("Test fehlgeschlagen:", f)
        if f.schluessel:
            print(FB_SCHLUESSEL_HILFE)
        sys.exit(1)
    print("Schlüssel funktioniert, Seite:", seite)
    reels = [v for v in st.get("verlauf", []) if v.get("typ") == "reel" and not v.get("test")]
    if not reels:
        print("Noch kein Reel auf Instagram gepostet, nichts zum Testen.")
        sys.exit(1)
    quelle, art = reels[-1]["quelle"], reels[-1]["art"]
    entry = yt_eintrag(cfg, art)
    if not entry:
        print(f"Reel-Typ '{art}' nicht in einstellungen.yaml gefunden.")
        sys.exit(1)
    video = MEDIA_ROOT / quelle
    text, hinweis = fb_text(cfg, entry, video)
    if hinweis:
        print("Hinweis:", hinweis)
    if not text:
        sys.exit(1)
    hole_dateien([video])
    print(f"Test (Entwurf, nicht öffentlich): {kurzname(quelle)}\nText:\n{text}\n")
    try:
        vid = fb_hochladen(cfg, video)
        fb_veroeffentlichen(cfg, vid, text, entwurf=True)
    except FbFehler as f:
        print("Test fehlgeschlagen:", f)
        if f.schluessel:
            print(FB_SCHLUESSEL_HILFE)
        sys.exit(1)
    st.setdefault("facebook_tests", []).append({"zeit": dt.datetime.now(TZ).isoformat(timespec="minutes"),
                                                "quelle": quelle, "id": vid})
    save_status(st)
    ergebnis = (f"Test erfolgreich: Seite '{seite}', Reel {kurzname(quelle)} als Entwurf angelegt (Video-ID {vid}). "
                "Zu finden in der Meta Business Suite unter Inhalte -> Entwürfe. Danach dort löschen.")
    print(ergebnis)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("## Facebook-Test\n\n" + ergebnis + "\n\nText:\n\n" + text + "\n")


# ---------------------------------------------------------------- Vorschau
def cmd_vorschau(days=14, ausgabe=True):
    cfg, st = load_cfg(), load_status()
    st = json.loads(json.dumps(st))
    jetzt = dt.datetime.now(TZ)
    start = max(jetzt.date(), as_date(cfg["aktiv_ab"]))
    kpool, rpool = karussell_posts(cfg, st), reel_files(cfg, st)
    extras = extra_termine(cfg)
    xpool = {}
    for e in extras:
        f = karussell_posts if e["typ"] == "karussell" else reel_files
        xpool[id(e)] = list(f(cfg, st, [e])[norm(e["ordner"])])
    last = st.get("letzter_reel_typ")
    last_k = letzte_karussell_art(st)
    wd = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
    lines = ["| Datum | Zeit | Typ | Art | Datei | Caption (Reels) |", "|---|---|---|---|---|---|"]
    rows = []
    titel = []  # (Beschriftung, Typ-Eintrag, Video) für die Titelbild-Vorschau
    for d in range(days):
        day = start + dt.timedelta(days=d)
        slots = [dict(s, extra=None) for s in cfg["zeitplan"] if an_tag(s, day)]
        slots += [{"zeit": e["zeit"], "typ": e["typ"], "extra": e} for e in extras if an_tag(e, day)]
        for slot in sorted(slots, key=lambda s: str(s["zeit"])):
            if day == jetzt.date() and str(slot["zeit"]) <= jetzt.strftime("%H:%M"):
                continue  # heute schon vorbei
            if slot["extra"]:
                e = slot["extra"]
                items = xpool[id(e)]
                item = items.pop(0) if items else None
                art = e.get("name", e["ordner"]) + " (extra)"
            elif slot["typ"] == "karussell":
                e, item, _ = pick_karussell(cfg, last_k, kpool)
                art = e["name"] if e else "-"
                if item:
                    kpool[norm(e["ordner"])].remove(item)
                    last_k = e["name"]
            else:
                e, item, _ = pick_reel(cfg, last, rpool)
                art = e["ordner"] if e else "-"
                if item:
                    rpool[norm(e["ordner"])].remove(item)
                    last = e["ordner"]
            datei = kurzname(rel(item)) if item else "KEIN VORRAT"
            cap = ""
            if item and slot["typ"] == "reel":
                cap, _ = _caption_sicher(cfg, e, item)
                titel.append((f"{wd[day.weekday()]} {day:%d.%m.} {slot['zeit']}", e, item))
            lines.append(f"| {wd[day.weekday()]} {day:%d.%m.} | {slot['zeit']} | {slot['typ']} | {art} | {datei} | "
                         f"{' / '.join(l for l in cap.splitlines() if l.strip())} |")
            rows.append([day.strftime("%d.%m.%Y"), wd[day.weekday()], slot["zeit"],
                         "Reel" if slot["typ"] == "reel" else "Karussell", art, datei, cap])
    vorrat = ["", "**Vorrat Karussells:** " + ", ".join(
        f"{e['name']}: {len(karussell_posts(cfg, st)[norm(e['ordner'])])}" for e in cfg["karussell_rhythmus"])]
    vorrat.append("**Vorrat Reels:** " + ", ".join(
        f"{e['ordner']}: {len(reel_files(cfg, st)[norm(e['ordner'])])}" for e in cfg["reel_rhythmus"]))
    if extras:
        vorrat.append("**Vorrat Zusatztermine:** " + ", ".join(
            f"{e.get('name', e['ordner'])}: {len((karussell_posts if e['typ'] == 'karussell' else reel_files)(cfg, st, [e])[norm(e['ordner'])])}"
            for e in extras))
    vorrat.append("**YouTube:** " + ("an (Reels werden nach dem Instagram-Post hochgeladen)"
                                     if yt_cfg(cfg).get("aktiv") else "aus (youtube -> aktiv: false)"))
    vorrat.append("**Facebook:** " + ("an (Reels werden nach dem Instagram-Post auf die Seite gepostet)"
                                      if fb_cfg(cfg).get("aktiv") else "aus (facebook -> aktiv: false)"))
    write_csv(GEPLANT_CSV, ["Datum", "Tag", "Uhrzeit", "Typ", "Art", "Datei/Ordner", "Caption (Reels)"], rows)
    text = "\n".join(lines + vorrat)
    if not ausgabe:
        return
    caption_liste(cfg, st)
    try:
        titelbild_vorschau(titel)
    except Exception as fehler:
        print("Titelbild-Vorschau nicht möglich:", fehler)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("## Vorschau\n\n" + text + "\n")


def _caption_sicher(cfg, entry, video):
    """Wie reel_caption, aber ein Lesefehler stoppt nie die Vorschau (nur Anzeige)."""
    try:
        return reel_caption(cfg, entry, video)
    except Exception as fehler:
        return "(Caption konnte nicht gelesen werden)", f"Fehler: {fehler}"


CAPTIONS_CSV = ROOT / "status" / "captions_reels.csv"
TITEL_DIR = ROOT / "_titelbilder"  # wird bei GitHub als Download "titelbilder" am Lauf angehängt


def titelbild_vorschau(titel):
    """Erzeugt die Titelbilder der geplanten Reels genau so, wie sie gepostet würden,
    plus eine Übersicht (so sieht es im Profil-Raster aus). Fehler hier stoppen nie die Vorschau."""
    from PIL import Image, ImageDraw
    if TITEL_DIR.exists():
        shutil.rmtree(TITEL_DIR)
    TITEL_DIR.mkdir()
    kacheln = []
    for nr, (wann, e, video) in enumerate(titel, 1):
        ziel = TITEL_DIR / f"{nr:02d}.jpg"
        quelle = "titelbild.png"
        try:
            grund = "keine titelbild_sekunde"
            if e.get("titelbild_sekunde") is not None:
                hole_dateien([video])
                grund = titelbild_aus_video(video, e["titelbild_sekunde"], ziel)
                if MEDIA_ROOT != ROOT:
                    video.write_bytes(b"")  # Platz sparen: Video wieder leeren
            if grund:
                cover = find_cover(video)
                if not cover:
                    kacheln.append((wann, e["ordner"], "kein Titelbild", None))
                    continue
                hole_dateien([cover])
                to_jpeg(cover, ziel, fit_feed=False)
            else:
                quelle = f"aus Video ({e['titelbild_sekunde']} s)"
            with Image.open(ziel) as im:
                kacheln.append((wann, e["ordner"], quelle, im.convert("RGB").crop((0, 240, 1080, 1680)).resize((270, 360))))
        except Exception as fehler:
            kacheln.append((wann, e["ordner"], f"Fehler: {fehler}"[:60], None))
    if not kacheln:
        return
    spalten = 3
    zeilen = (len(kacheln) + spalten - 1) // spalten
    blatt = Image.new("RGB", (spalten * 280, zeilen * 410), "white")
    d = ImageDraw.Draw(blatt)
    try:  # Schrift mit Umlauten (bei GitHub vorhanden), sonst Standardschrift
        from PIL import ImageFont
        schrift = ImageFont.truetype("DejaVuSans.ttf", 13)
    except Exception:
        schrift = None
    for i, (wann, typ, quelle, bild) in enumerate(kacheln):
        x, y = (i % spalten) * 280, (i // spalten) * 410
        if bild:
            blatt.paste(bild, (x + 5, y + 45))
        d.text((x + 5, y + 5), f"{wann}  {typ}", fill="black", font=schrift)
        d.text((x + 5, y + 22), quelle, fill="gray", font=schrift)
    blatt.save(TITEL_DIR / "00_Uebersicht_Raster.jpg", "JPEG", quality=88)
    print(f"Titelbild-Vorschau: {len(kacheln)} Bilder -> Download 'titelbilder' am Lauf")


def caption_liste(cfg, st):
    """Schreibt für jedes noch nicht gepostete Reel genau die Caption, mit der es rausgehen würde."""
    eintraege = list(cfg.get("reel_rhythmus") or []) + [e for e in extra_termine(cfg) if e.get("typ") == "reel"]
    rows = []
    for e in eintraege:
        for v in reel_files(cfg, st, [e])[norm(e["ordner"])]:
            cap, hinweis = _caption_sicher(cfg, e, v)
            txt, _ = begleittext(v)
            yt_titel, yt_beschr, yt_hinweis = _yt_texte_sicher(cfg, e, v)
            fb, fb_hinweis = _fb_text_sicher(cfg, e, v)
            rows.append([e["ordner"], kurzname(rel(v)), "Textdatei" if txt else "fester Text", cap, hinweis or "",
                         yt_titel or "(kein gültiger Titel)", yt_beschr, yt_hinweis or "",
                         fb or "(kein Text möglich)", fb_hinweis or ""])
    write_csv(CAPTIONS_CSV, ["Typ", "Datei", "Zeile 1 aus", "Caption", "Hinweis",
                             "YouTube-Titel", "YouTube-Beschreibung", "YouTube-Hinweis",
                             "Facebook-Text", "Facebook-Hinweis"], rows)
    print(f"Caption-Liste: {len(rows)} Reels -> status/captions_reels.csv")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "vorschau"
    if cmd == "prepare":
        t = sys.argv[3] if len(sys.argv) > 3 and sys.argv[2] == "--test" else None
        cmd_prepare(t if t in ("reel", "karussell") else None)
    elif cmd == "publish":
        cmd_publish()
    elif cmd == "youtube":
        cmd_youtube()
    elif cmd == "youtube-test":
        cmd_youtube_test()
    elif cmd == "youtube-nachholen":
        cmd_youtube_nachholen(sys.argv[2] if len(sys.argv) > 2 else os.environ.get("DATEI", ""))
    elif cmd == "facebook":
        cmd_facebook()
    elif cmd == "facebook-test":
        cmd_facebook_test()
    elif cmd == "facebook-nachholen":
        cmd_facebook_nachholen(sys.argv[2] if len(sys.argv) > 2 else os.environ.get("DATEI", ""))
    else:
        cmd_vorschau(int(sys.argv[2]) if len(sys.argv) > 2 else 14)
