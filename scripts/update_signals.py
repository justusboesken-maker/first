#!/usr/bin/env python3
"""Berechnet Kauf-/Verkaufssignale auf Basis des 50-Wochen-Durchschnitts.

Holt Tageskurse (USD) von Yahoo Finance, bildet daraus Wochenkerzen,
wendet die Regeln aus ASSETS an und schreibt das Ergebnis nach
docs/data/signals.json. Bei einem neuen Signal wird per ntfy (Push aufs
Handy) und/oder GitHub-Issue (E-Mail) benachrichtigt.

Regeln (jeweils Wochenschlusskurs gegen 50-Wochen-SMA):
  FTSE All-World TR  2 Wochenschlüsse in Folge über/unter dem MA
  Bitcoin            1 Wochenschluss mehr als 3 % über/unter dem MA
  Gold               4 Wochenschlüsse in Folge über/unter dem MA
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "docs" / "data" / "signals.json"

MA_WEEKS = 50
CHART_WEEKS = 156
NOTIFY_MAX_AGE_DAYS = 14

# calendar "exchange": Woche endet mit dem Freitagsschluss,
# calendar "crypto":   Woche endet Sonntag 24:00 UTC.
ASSETS = [
    {
        "id": "ftse",
        "name": "FTSE All-World TR",
        "symbol": "VWRA.L",
        "instrument": "Vanguard FTSE All-World UCITS ETF (USD, thesaurierend) als Total-Return-Abbild",
        "calendar": "exchange",
        "confirm_weeks": 2,
        "band_pct": 0.0,
    },
    {
        "id": "btc",
        "name": "Bitcoin",
        "symbol": "BTC-USD",
        "instrument": "Bitcoin in US-Dollar",
        "calendar": "crypto",
        "confirm_weeks": 1,
        "band_pct": 3.0,
    },
    {
        "id": "gold",
        "name": "Gold",
        "symbol": "GC=F",
        "instrument": "COMEX Gold-Future in US-Dollar je Feinunze",
        "calendar": "exchange",
        "confirm_weeks": 4,
        "band_pct": 0.0,
    },
]

YAHOO_HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


# --------------------------------------------------------------------------
# Kursdaten
# --------------------------------------------------------------------------

def http_get_json(url: str) -> dict:
    # curl_cffi imitiert einen Browser und kommt zuverlässiger an Yahoos
    # Bot-Schutz vorbei; ohne das Paket geht es mit urllib weiter.
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        cffi_requests = None
    if cffi_requests is not None:
        response = cffi_requests.get(url, impersonate="chrome", timeout=30)
        response.raise_for_status()
        return response.json()
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def exchange_tz(name: str | None, gmtoffset: int | None):
    if name:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return timezone(timedelta(seconds=gmtoffset or 0))


def parse_yahoo_chart(payload: dict, symbol: str) -> list[tuple[date, float]]:
    chart = payload.get("chart") or {}
    if chart.get("error"):
        raise ValueError(f"Yahoo-Fehler für {symbol}: {chart['error']}")
    result = (chart.get("result") or [None])[0]
    if not result:
        raise ValueError(f"Keine Kursdaten für {symbol}")
    meta = result.get("meta") or {}
    currency = meta.get("currency")
    if currency and currency.upper() != "USD":
        raise ValueError(f"{symbol} notiert in {currency}, erwartet wird USD")
    tz = exchange_tz(meta.get("exchangeTimezoneName"), meta.get("gmtoffset"))
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    by_day: dict[date, float] = {}
    for ts, close in zip(timestamps, closes):
        if close is None:
            continue
        by_day[datetime.fromtimestamp(ts, tz).date()] = float(close)
    if not by_day:
        raise ValueError(f"Keine Schlusskurse für {symbol}")
    return sorted(by_day.items())


def fetch_daily_closes(symbol: str) -> list[tuple[date, float]]:
    params = urllib.parse.urlencode({
        "period1": 946684800,  # 01.01.2000
        "period2": int(time.time()) + 86400,
        "interval": "1d",
        "includePrePost": "false",
    })
    last_error: Exception | None = None
    for attempt in range(3):
        for host in YAHOO_HOSTS:
            url = f"https://{host}/v8/finance/chart/{urllib.parse.quote(symbol)}?{params}"
            try:
                return parse_yahoo_chart(http_get_json(url), symbol)
            except Exception as exc:  # noqa: BLE001 - nächster Versuch
                last_error = exc
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Kursdaten für {symbol} nicht abrufbar: {last_error}")


# --------------------------------------------------------------------------
# Wochenkerzen & Signale
# --------------------------------------------------------------------------

def week_close_time(monday: date, calendar: str) -> datetime:
    """Zeitpunkt (UTC), ab dem die Wochenkerze als abgeschlossen gilt."""
    days = 7 if calendar == "crypto" else 5
    return datetime.combine(monday + timedelta(days=days), datetime.min.time(), tzinfo=timezone.utc)


def to_weekly(daily: list[tuple[date, float]], calendar: str, now: datetime):
    """Fasst Tageskurse zu Wochen (Mo–So) zusammen.

    Liefert (abgeschlossene Wochen, laufende Woche oder None). Eine Woche
    wird durch ihren letzten Handelstag und dessen Schlusskurs beschrieben.
    """
    weeks: dict[date, tuple[date, float]] = {}
    for day, close in daily:
        if calendar != "crypto" and day.weekday() >= 5:
            continue  # Wochenend-Ticks gehören nicht zum Freitagsschluss
        monday = day - timedelta(days=day.weekday())
        if monday not in weeks or day >= weeks[monday][0]:
            weeks[monday] = (day, close)
    complete, live = [], None
    for monday in sorted(weeks):
        day, close = weeks[monday]
        entry = {"date": day.isoformat(), "close": close}
        if now >= week_close_time(monday, calendar):
            complete.append(entry)
        else:
            live = entry
    return complete, live


def sma(values: list[float], length: int) -> list[float | None]:
    return [
        sum(values[i - length + 1:i + 1]) / length if i >= length - 1 else None
        for i in range(len(values))
    ]


def evaluate(weeks: list[dict], confirm_weeks: int, band_pct: float) -> dict:
    """Spielt die Regel über die gesamte Historie durch.

    Start ist "nicht investiert". Kauf, sobald `confirm_weeks` Schlusskurse in
    Folge mehr als `band_pct` % über dem MA liegen; Verkauf spiegelbildlich.
    """
    closes = [w["close"] for w in weeks]
    ma = sma(closes, MA_WEEKS)
    band = band_pct / 100
    invested = False
    above = below = 0
    rows, signals = [], []
    for week, close, avg in zip(weeks, closes, ma):
        if avg is None:
            rows.append({"date": week["date"], "close": close, "ma": None, "invested": False})
            continue
        above = above + 1 if close > avg * (1 + band) else 0
        below = below + 1 if close < avg * (1 - band) else 0
        kind = None
        if not invested and above >= confirm_weeks:
            invested, kind = True, "buy"
        elif invested and below >= confirm_weeks:
            invested, kind = False, "sell"
        if kind:
            signals.append({
                "date": week["date"],
                "type": kind,
                "close": close,
                "ma": avg,
                "distance_pct": (close / avg - 1) * 100,
            })
        rows.append({"date": week["date"], "close": close, "ma": avg, "invested": invested})
    return {"rows": rows, "signals": signals, "invested": invested, "above": above, "below": below}


def rule_texts(confirm_weeks: int, band_pct: float) -> dict:
    count = f"{confirm_weeks} Wochenschlüsse in Folge" if confirm_weeks > 1 else "Wochenschluss"
    margin = f"mehr als {band_pct:g} % " if band_pct else ""
    return {
        "buy": f"{count} {margin}über dem 50-Wochen-MA",
        "sell": f"{count} {margin}unter dem 50-Wochen-MA",
    }


def build_asset(config: dict, daily: list[tuple[date, float]], now: datetime) -> dict:
    weeks, live_week = to_weekly(daily, config["calendar"], now)
    if len(weeks) < MA_WEEKS:
        raise ValueError(f"Zu wenig Historie für {config['symbol']}: {len(weeks)} Wochen")
    confirm, band_pct = config["confirm_weeks"], config["band_pct"]
    band = band_pct / 100
    result = evaluate(weeks, confirm, band_pct)
    last = result["rows"][-1]
    invested = result["invested"]
    signals = [
        {"id": f"{config['id']}-{s['date']}-{s['type']}", **_rounded(s)}
        for s in result["signals"]
    ]
    last_signal = signals[-1] if signals else None

    if invested:
        trigger = {"type": "sell", "level": last["ma"] * (1 - band), "streak": result["below"]}
    else:
        trigger = {"type": "buy", "level": last["ma"] * (1 + band), "streak": result["above"]}
    trigger["needed"] = confirm

    live = None
    if live_week:
        closes = [w["close"] for w in weeks]
        live_ma = (sum(closes[-(MA_WEEKS - 1):]) + live_week["close"]) / MA_WEEKS
        if invested:
            hit = live_week["close"] < live_ma * (1 - band)
            streak = result["below"] + 1 if hit else 0
        else:
            hit = live_week["close"] > live_ma * (1 + band)
            streak = result["above"] + 1 if hit else 0
        live = _rounded({
            "date": live_week["date"],
            "close": live_week["close"],
            "ma": live_ma,
            "distance_pct": (live_week["close"] / live_ma - 1) * 100,
            "streak": streak,
            "would_signal": trigger["type"] if streak >= confirm else None,
        })

    return {
        "id": config["id"],
        "name": config["name"],
        "symbol": config["symbol"],
        "instrument": config["instrument"],
        "rule": {"confirm_weeks": confirm, "band_pct": band_pct, **rule_texts(confirm, band_pct)},
        "status": "invested" if invested else "out",
        "last_week": _rounded({
            "date": last["date"],
            "close": last["close"],
            "ma": last["ma"],
            "distance_pct": (last["close"] / last["ma"] - 1) * 100,
        }),
        "last_signal": last_signal,
        "change_since_signal_pct": (
            round((last["close"] / last_signal["close"] - 1) * 100, 2) if last_signal else None
        ),
        "trigger": _rounded(trigger),
        "live": live,
        "signals": signals,
        "chart": [
            _rounded(row) for row in result["rows"][-CHART_WEEKS:]
        ],
        "error": None,
    }


def _rounded(values: dict) -> dict:
    out = {}
    for key, value in values.items():
        if isinstance(value, float):
            value = round(value, 2) if key == "distance_pct" else round(value, 4)
        out[key] = value
    return out


# --------------------------------------------------------------------------
# Benachrichtigungen
# --------------------------------------------------------------------------

def fmt_usd(value: float) -> str:
    text = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{text} $"


def fmt_pct(value: float) -> str:
    return f"{value:+.1f} %".replace(".", ",")


def fmt_date(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%d.%m.%Y")


def pending_notifications(assets: list[dict], notified: set[str], today: date):
    """Neueste Signale je Anlage, über die noch nicht benachrichtigt wurde."""
    cutoff = today - timedelta(days=NOTIFY_MAX_AGE_DAYS)
    for asset in assets:
        signal = asset.get("last_signal")
        if not signal or asset.get("error") or signal["id"] in notified:
            continue
        if date.fromisoformat(signal["date"]) < cutoff:
            continue
        yield asset, signal


def compose_message(asset: dict, signal: dict) -> tuple[str, str]:
    buy = signal["type"] == "buy"
    title = f"{'Kaufsignal' if buy else 'Verkaufssignal'}: {asset['name']}"
    rule = asset["rule"]["buy" if buy else "sell"]
    body = (
        f"Wochenschluss {fmt_date(signal['date'])}: {fmt_usd(signal['close'])} "
        f"({fmt_pct(signal['distance_pct'])} zum 50-Wochen-MA von {fmt_usd(signal['ma'])}).\n"
        f"Regel: {rule}.\n"
        f"Neuer Status: {'investiert' if buy else 'nicht investiert'}."
    )
    return title, body


def post_json(url: str, payload: dict, headers: dict) -> None:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "signal-bot", **headers},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        response.read()


def send_ntfy(title: str, body: str, buy: bool) -> None:
    server = os.environ.get("NTFY_SERVER") or "https://ntfy.sh"
    headers = {}
    if os.environ.get("NTFY_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['NTFY_TOKEN']}"
    payload = {
        "topic": os.environ["NTFY_TOPIC"],
        "title": title,
        "message": body,
        "priority": 4,
        "tags": ["chart_with_upwards_trend" if buy else "chart_with_downwards_trend"],
    }
    if os.environ.get("SITE_URL"):
        payload["click"] = os.environ["SITE_URL"]
    post_json(server.rstrip("/") + "/", payload, headers)


def send_github_issue(title: str, body: str, buy: bool) -> None:
    repo = os.environ["GITHUB_REPOSITORY"]
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER")
    text = body.replace("\n", "\n\n")
    if os.environ.get("SITE_URL"):
        text += f"\n\n[Zur Übersicht]({os.environ['SITE_URL']})"
    if owner:
        text += f"\n\n@{owner} – schließe dieses Issue, sobald die Order ausgeführt ist."
    post_json(
        f"https://api.github.com/repos/{repo}/issues",
        {"title": f"{'🟢' if buy else '🔴'} {title}", "body": text},
        {
            "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
            "Accept": "application/vnd.github+json",
        },
    )


def notification_channels() -> list:
    channels = []
    if os.environ.get("NTFY_TOPIC"):
        channels.append(("ntfy", send_ntfy))
    issues_enabled = os.environ.get("NOTIFY_GITHUB_ISSUES", "true").lower() not in ("false", "0", "no")
    if issues_enabled and os.environ.get("GITHUB_TOKEN") and os.environ.get("GITHUB_REPOSITORY"):
        channels.append(("GitHub-Issue", send_github_issue))
    return channels


def notify(assets: list[dict], notified: set[str], today: date) -> set[str]:
    """Verschickt ausstehende Signale; liefert die IDs, die als erledigt gelten."""
    done = set()
    channels = notification_channels()
    for asset, signal in pending_notifications(assets, notified, today):
        title, body = compose_message(asset, signal)
        print(f"Neues Signal – {title}\n{body}")
        delivered = not channels  # ohne Kanal nur protokollieren
        for name, send in channels:
            try:
                send(title, body, signal["type"] == "buy")
                delivered = True
                print(f"  → per {name} verschickt")
            except Exception as exc:  # noqa: BLE001 - andere Kanäle trotzdem versuchen
                print(f"  → {name} fehlgeschlagen: {exc}", file=sys.stderr)
        if delivered:
            done.add(signal["id"])
    return done


# --------------------------------------------------------------------------
# Ablauf
# --------------------------------------------------------------------------

def load_previous(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--no-notify", action="store_true", help="keine Benachrichtigungen verschicken")
    args = parser.parse_args(argv)

    now = datetime.now(timezone.utc)
    previous = load_previous(args.output)
    previous_assets = {a["id"]: a for a in previous.get("assets", [])}

    assets, failures = [], []
    for config in ASSETS:
        try:
            asset = build_asset(config, fetch_daily_closes(config["symbol"]), now)
            print(f"{config['name']}: {asset['status']}, Wochenschluss {asset['last_week']['date']}")
        except Exception as exc:  # noqa: BLE001 - übrige Anlagen trotzdem aktualisieren
            failures.append(config["name"])
            print(f"{config['name']}: {exc}", file=sys.stderr)
            asset = previous_assets.get(config["id"])
            if asset is None:
                continue
            asset = {**asset, "error": str(exc)}
        assets.append(asset)

    if not assets:
        print("Keine Daten verfügbar – nichts geschrieben.", file=sys.stderr)
        return 1

    notified = set(previous.get("notified", []))
    if not args.no_notify:
        notified |= notify(assets, notified, now.date())

    output = {
        "generated_at": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "ma_weeks": MA_WEEKS,
        "assets": assets,
        # IDs haben die Form "<anlage>-<datum>-<typ>": nach Datum sortieren
        "notified": sorted(notified, key=lambda i: i.split("-", 1)[1])[-100:],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"Geschrieben: {args.output}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
