# 9x KLUG Autopost

Postet automatisch Karussells und Reels auf @9xklug_raetsel.

## Neues Material nachlegen
- Karussell-Ordner (`9x_KLUG_<Art>_<Datum>`) in den Ordner **karussells** kopieren
- Reels in den passenden Unterordner von **reels** kopieren (Ordnername = Reel-Typ, MP4 + gleichnamiges PNG als Titelbild)
- In GitHub Desktop: **Commit** und **Push**

## Einstellungen ändern
Datei **einstellungen.yaml** öffnen (Editor reicht), ändern, speichern, Commit und Push.

## Nachsehen, was als Nächstes kommt
GitHub → Reiter **Actions** → „9x KLUG Autopost“ → **Run workflow** → `vorschau` → Ergebnis im Lauf unter „Summary“.

## Pause
In einstellungen.yaml `pausiert: true` setzen.

## Was schon gepostet wurde
Steht in `status/gepostet.json`. Ein Post wird nie zweimal veröffentlicht.
