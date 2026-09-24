# Marktsignale

Eine kleine Website, die Kauf- und Verkaufssignale für drei Anlagen anzeigt. Du siehst dort auch den aktuellen Stand (**investiert** / **nicht investiert**). Bei jedem neuen Signal bekommst du eine Benachrichtigung.

| Anlage | Kaufsignal | Verkaufssignal |
|---|---|---|
| FTSE All-World (Total Return) | 2 Wochenschlüsse in Folge **über** dem 50-Wochen-MA | 2 Wochenschlüsse in Folge **unter** dem 50-Wochen-MA |
| Bitcoin | Wochenschluss mehr als **3 % über** dem 50-Wochen-MA | Wochenschluss mehr als **3 % unter** dem 50-Wochen-MA |
| Gold | 4 Wochenschlüsse in Folge **über** dem 50-Wochen-MA | 4 Wochenschlüsse in Folge **unter** dem 50-Wochen-MA |

Alle Kurse sind in US-Dollar.

## So funktioniert es

```
GitHub Actions (täglich 06:17 UTC)
  └─ scripts/update_signals.py
       ├─ holt Tageskurse von Yahoo Finance und bildet Wochenkerzen
       ├─ berechnet den 50-Wochen-MA (einfacher gleitender Durchschnitt) und wendet die Regeln an
       ├─ schreibt docs/data/signals.json  ──►  Website (docs/)
       └─ bei neuem Signal: ntfy-Push und/oder GitHub-Issue (E-Mail)
```

- **Wochenkerzen:** Bei FTSE und Gold zählt der Freitagsschluss. Bei Bitcoin zählt der Schluss am Sonntag um 24:00 UTC. Nur abgeschlossene Wochen erzeugen Signale.
- **Status:** Das Skript spielt die Regeln über die gesamte Kurshistorie durch. Zu Beginn der Historie gilt der Status „nicht investiert“. Ein Kauf erfolgt nur aus „nicht investiert“ heraus, ein Verkauf nur aus „investiert“.
- **Laufende Woche:** Die Website zeigt eine vorläufige Einschätzung. Sie warnt, wenn die Woche auf dem aktuellen Niveau ein Signal auslösen würde.
- **Datenquellen (Yahoo Finance):**
  - `VWRA.L`: Vanguard FTSE All-World UCITS ETF, thesaurierend und in USD notiert. Er dient als Abbild des FTSE All-World Total Return, weil der Index selbst nicht frei abrufbar ist.
  - `BTC-USD`: Bitcoin in US-Dollar.
  - `GC=F`: COMEX-Gold-Future in US-Dollar je Feinunze.

  Die Symbole und Regeln stehen oben in `scripts/update_signals.py` (`ASSETS`) und lassen sich dort anpassen.

## Einrichtung

### 1. Daten erzeugen

Nach dem Merge in `main` startet der Workflow **„Signale aktualisieren“** automatisch. Du kannst ihn auch unter **Actions → Signale aktualisieren → Run workflow** von Hand starten. Danach läuft er täglich. Das Ergebnis landet in `docs/data/signals.json`.

### 2. Website veröffentlichen

**GitHub Pages:** Gehe zu **Settings → Pages → Build and deployment**, wähle *Deploy from a branch*, dann Branch `main` und Ordner `/docs`. Die Seite ist danach unter `https://<benutzername>.github.io/<repo>/` erreichbar.

> GitHub Pages ist für **private** Repositories nur mit einem kostenpflichtigen GitHub-Plan verfügbar. Du hast zwei Alternativen:
> - Du stellst das Repository auf öffentlich. Es enthält keine Geheimnisse, denn das ntfy-Topic liegt als Secret gespeichert.
> - Du verbindest das Repository mit Cloudflare Pages, Netlify oder Vercel. Als Publish-Ordner gibst du `docs` an, ein Build-Befehl ist nicht nötig.

### 3. Benachrichtigungen

| Kanal | Einrichtung |
|---|---|
| **GitHub-Issue / E-Mail** | Aktiv ohne weitere Einrichtung. Jedes neue Signal eröffnet ein Issue und erwähnt dich darin. GitHub schickt dir dann eine E-Mail und, falls du die GitHub-App nutzt, einen Push. Schließe das Issue, sobald die Order ausgeführt ist. Abschalten kannst du den Kanal mit der Repository-Variable `NOTIFY_GITHUB_ISSUES` = `false`. |
| **Push aufs Handy (ntfy)** | Installiere die App [ntfy](https://ntfy.sh) (iOS und Android, kostenlos, kein Konto nötig). Abonniere darin ein schwer zu erratendes Topic, z. B. `marktsignale-7f3k9q`. Lege es unter **Settings → Secrets and variables → Actions** als Secret `NTFY_TOPIC` an. |
| **Browser** | Klicke auf der Website auf „Benachrichtigungen aktivieren“. Das funktioniert, solange die Seite in einem Tab geöffnet ist. Die Seite prüft alle 15 Minuten auf neue Daten. |

Optionale Repository-Variablen:
- `SITE_URL`: Adresse der Website. Ein Tipp auf die Benachrichtigung öffnet dann die Übersicht.
- `NTFY_SERVER`: eigener ntfy-Server.

Optionales Secret:
- `NTFY_TOKEN`: Zugangstoken für geschützte Topics.

Benachrichtigt wird nur über Signale der letzten 14 Tage und je Signal nur einmal.

## Lokal ausführen

```bash
pip install -r requirements.txt
python scripts/update_signals.py --no-notify   # Daten holen, ohne zu benachrichtigen
python -m unittest discover -s tests            # Tests
python -m http.server -d docs 8000              # Website unter http://localhost:8000
```

---

Keine Anlageberatung. Die Signale folgen ausschließlich den oben beschriebenen Regeln.
