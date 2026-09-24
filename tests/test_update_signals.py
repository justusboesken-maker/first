import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import update_signals as us  # noqa: E402


def weeks_from(closes):
    start = date(2020, 1, 3)  # Freitag
    return [{"date": (start + timedelta(weeks=i)).isoformat(), "close": c} for i, c in enumerate(closes)]


def signal_types(result):
    return [(s["date"], s["type"]) for s in result["signals"]]


class MovingAverageTest(unittest.TestCase):
    def test_sma(self):
        self.assertEqual(us.sma([1, 2, 3, 4], 2), [None, 1.5, 2.5, 3.5])


class YahooParseTest(unittest.TestCase):
    def payload(self, currency="USD"):
        return {"chart": {"error": None, "result": [{
            "meta": {"currency": currency, "exchangeTimezoneName": "America/New_York", "gmtoffset": -14400},
            # Mitternacht New York im Winter (EST) und im Sommer (EDT)
            "timestamp": [1767934800, 1783656000, 1783699200],
            "indicators": {"quote": [{"close": [4000.5, 3900.0, None]}]},
        }]}}

    def test_dates_use_exchange_timezone(self):
        self.assertEqual(us.parse_yahoo_chart(self.payload(), "GC=F"), [
            (date(2026, 1, 9), 4000.5),
            (date(2026, 7, 10), 3900.0),
        ])

    def test_rejects_other_currencies(self):
        with self.assertRaises(ValueError):
            us.parse_yahoo_chart(self.payload("GBp"), "VWRL.L")


class FakeSocket:
    """Spielt die Antworten des TradingView-Websockets ab."""

    def __init__(self, packets):
        self.packets = list(packets)
        self.sent = []

    def send(self, text):
        self.sent.append(text)

    def recv(self):
        return self.packets.pop(0)

    def close(self):
        pass


def tv(message):
    return us._tv_message(message["m"], message["p"])


class TradingViewTest(unittest.TestCase):
    # Gold-Sitzungen beginnen am Vorabend um 22:00 UTC, Indizes um 00:00 UTC
    day1 = int(datetime(2026, 9, 13, 22, tzinfo=timezone.utc).timestamp())
    day2 = int(datetime(2026, 9, 14, 22, tzinfo=timezone.utc).timestamp())

    def test_split_frames(self):
        raw = us._tv_frame('{"a":1}') + "~m~4~m~~h~7"
        self.assertEqual(us._tv_split(raw), ['{"a":1}', "~h~7"])

    def test_days_dated_on_trading_day(self):
        midnight = int(datetime(2026, 9, 16, tzinfo=timezone.utc).timestamp())
        closes = us.tv_days_to_closes({self.day2: 3650.5, self.day1: 3700.0, midnight: 3600.0})
        self.assertEqual(closes, [
            (date(2026, 9, 14), 3700.0), (date(2026, 9, 15), 3650.5), (date(2026, 9, 16), 3600.0),
        ])

    def test_fetch_series(self):
        socket = FakeSocket([
            us._tv_frame('{"session_id":"x"}'),
            tv({"m": "symbol_resolved", "p": ["cs", "sym", {"currency_code": "USD"}]}),
            "~m~4~m~~h~1",
            tv({"m": "timescale_update", "p": ["cs", {"s1": {"s": [
                {"i": 0, "v": [self.day1, 1, 2, 0.5, 3700.0, 0]},
                {"i": 1, "v": [self.day2, 1, 2, 0.5, 3650.5, 0]},
            ]}}]}) + tv({"m": "series_completed", "p": ["cs", "s1", "ok"]}),
        ])
        fake_module = mock.Mock(create_connection=mock.Mock(return_value=socket))
        with mock.patch.dict(sys.modules, {"websocket": fake_module}):
            closes = us.fetch_tradingview_daily("OANDA:XAUUSD")
        self.assertEqual([c for _, c in closes], [3700.0, 3650.5])
        self.assertIn("~m~4~m~~h~1", socket.sent)  # Heartbeat beantwortet
        self.assertIn('"1D"', socket.sent[3])

    def test_rejects_other_currency(self):
        socket = FakeSocket([tv({"m": "symbol_resolved", "p": ["cs", "sym", {"currency_code": "EUR"}]})])
        fake_module = mock.Mock(create_connection=mock.Mock(return_value=socket))
        with mock.patch.dict(sys.modules, {"websocket": fake_module}), self.assertRaises(ValueError):
            us.fetch_tradingview_daily("OANDA:XAUUSD")


class RuleTest(unittest.TestCase):
    base = [100.0] * us.MA_WEEKS  # MA genau 100, kein Signal

    def test_two_weeks_confirmation(self):
        closes = self.base + [110, 90, 110, 110, 90, 110, 90, 90]
        weeks = weeks_from(closes)
        result = us.evaluate(weeks, confirm_weeks=2, band_pct=0)
        n = us.MA_WEEKS
        self.assertEqual(signal_types(result), [
            (weeks[n + 3]["date"], "buy"),   # erst der zweite Schluss in Folge
            (weeks[n + 7]["date"], "sell"),
        ])
        self.assertFalse(result["invested"])

    def test_no_signal_on_flat_market(self):
        result = us.evaluate(weeks_from(self.base + [100.0] * 5), confirm_weeks=2, band_pct=0)
        self.assertEqual(result["signals"], [])
        self.assertFalse(result["invested"])

    def test_bitcoin_band(self):
        # 102 liegt unter der 3-%-Schwelle, 104 darüber; 98 und 97,5 bleiben
        # innerhalb der Spanne, erst 96 schließt mehr als 3 % unter dem MA.
        closes = self.base + [102, 104, 98, 97.5, 96]
        weeks = weeks_from(closes)
        result = us.evaluate(weeks, confirm_weeks=1, band_pct=3)
        n = us.MA_WEEKS
        self.assertEqual(signal_types(result), [
            (weeks[n + 1]["date"], "buy"),
            (weeks[n + 4]["date"], "sell"),
        ])

    def test_gold_four_weeks(self):
        closes = self.base + [101, 101, 101, 99, 101, 101, 101, 101]
        weeks = weeks_from(closes)
        result = us.evaluate(weeks, confirm_weeks=4, band_pct=0)
        self.assertEqual(signal_types(result), [(weeks[us.MA_WEEKS + 7]["date"], "buy")])
        self.assertTrue(result["invested"])
        self.assertEqual(result["above"], 4)


class WeeklyTest(unittest.TestCase):
    def daily(self, first, days, price=1.0):
        return [(first + timedelta(days=i), price + i) for i in range(days)]

    def test_exchange_week_closes_on_friday(self):
        daily = self.daily(date(2026, 9, 7), 12)  # Mo 07.09. bis Fr 18.09.
        now = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)
        complete, live = us.to_weekly(daily, "exchange", now)
        self.assertEqual(complete, [{"date": "2026-09-11", "close": 5.0}])
        self.assertEqual(live["date"], "2026-09-18")
        complete, live = us.to_weekly(daily, "exchange", datetime(2026, 9, 19, 6, tzinfo=timezone.utc))
        self.assertEqual([w["date"] for w in complete], ["2026-09-11", "2026-09-18"])
        self.assertIsNone(live)

    def test_crypto_week_includes_sunday(self):
        daily = self.daily(date(2026, 9, 7), 14)  # Mo 07.09. bis So 20.09.
        now = datetime(2026, 9, 20, 22, tzinfo=timezone.utc)
        complete, live = us.to_weekly(daily, "crypto", now)
        self.assertEqual(complete, [{"date": "2026-09-13", "close": 7.0}])
        self.assertEqual(live, {"date": "2026-09-20", "close": 14.0})
        complete, _ = us.to_weekly(daily, "crypto", datetime(2026, 9, 21, 0, 5, tzinfo=timezone.utc))
        self.assertEqual(len(complete), 2)


class BuildAssetTest(unittest.TestCase):
    config = {
        "id": "btc", "name": "Bitcoin", "symbol": "BTC-USD", "instrument": "",
        "calendar": "crypto", "confirm_weeks": 1, "band_pct": 3.0,
    }

    def test_live_week_preview(self):
        first = date(2025, 1, 6)  # Montag
        daily = [(first + timedelta(days=i), 100.0) for i in range(7 * us.MA_WEEKS)]
        daily += [(first + timedelta(days=7 * us.MA_WEEKS + i), 110.0) for i in range(3)]
        now = datetime.combine(daily[-1][0], datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=12)
        asset = us.build_asset(self.config, daily, now)
        self.assertEqual(asset["status"], "out")
        self.assertEqual(asset["trigger"], {"type": "buy", "level": 103.0, "streak": 0, "needed": 1})
        self.assertEqual(asset["live"]["would_signal"], "buy")
        self.assertEqual(asset["live"]["ma"], 100.2)
        # Monatsansicht: Tageskurse mit laufendem MA, letzter Tag = live-Woche
        self.assertEqual(asset["daily"][-1], {"date": daily[-1][0].isoformat(), "close": 110.0,
                                              "ma": 100.2, "invested": False})
        self.assertTrue(all(d["ma"] == 100.0 for d in asset["daily"][:-3]))


class NotificationTest(unittest.TestCase):
    def asset(self, signal_date, error=None):
        signal = {"id": f"btc-{signal_date}-buy", "date": signal_date, "type": "buy",
                  "close": 104000.0, "ma": 100000.0, "distance_pct": 4.0}
        return {"name": "Bitcoin", "last_signal": signal, "error": error,
                "rule": {"buy": "Wochenschluss mehr als 3 % über dem 50-Wochen-MA", "sell": ""}}

    def test_only_recent_unsent_signals(self):
        today = date(2026, 9, 24)
        assets = [self.asset("2026-09-20"), self.asset("2026-08-02"), self.asset("2026-09-13", error="x")]
        pending = list(us.pending_notifications(assets, set(), today))
        self.assertEqual([s["id"] for _, s in pending], ["btc-2026-09-20-buy"])
        self.assertEqual(list(us.pending_notifications(assets, {"btc-2026-09-20-buy"}, today)), [])

    def test_message_is_german(self):
        asset = self.asset("2026-09-20")
        title, body = us.compose_message(asset, asset["last_signal"])
        self.assertEqual(title, "Kaufsignal: Bitcoin")
        self.assertIn("104.000,00 $", body)
        self.assertIn("+4,0 %", body)
        self.assertIn("20.09.2026", body)


class MainTest(unittest.TestCase):
    """Gesamtablauf mit gemockten Kursdaten und Benachrichtigungen."""

    @staticmethod
    def fake_prices(config):
        today = datetime.now(timezone.utc).date()
        days = [today - timedelta(days=i) for i in range(500, -1, -1)]
        # lange flach, dann ein Sprung in den letzten zehn Tagen: Bitcoin
        # (1 Woche Bestätigung) liefert so immer ein frisches Kaufsignal.
        return [(d, 110.0 if (today - d).days < 10 else 100.0) for d in days]

    def run_main(self, output, fetch):
        env = {"NTFY_TOPIC": "test", "NOTIFY_GITHUB_ISSUES": "false"}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(us, "fetch_closes", side_effect=fetch), \
                mock.patch.object(us, "post_json") as post:
            code = us.main(["--output", str(output)])
        return code, post, json.loads(output.read_text(encoding="utf-8"))

    def test_notifies_once_and_keeps_old_data_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "signals.json"
            code, post, data = self.run_main(output, self.fake_prices)
            self.assertEqual(code, 0)
            self.assertEqual([a["id"] for a in data["assets"]], ["ftse", "btc", "gold"])
            btc = data["assets"][1]
            self.assertEqual(btc["status"], "invested")
            self.assertIn(btc["last_signal"]["id"], data["notified"])
            sent = [call.args[1]["title"] for call in post.call_args_list]
            self.assertIn("Kaufsignal: Bitcoin", sent)

            def failing(config):
                if config["id"] == "gold":
                    raise RuntimeError("nicht erreichbar")
                return self.fake_prices(config)

            code, post, again = self.run_main(output, failing)
            self.assertEqual(code, 1)
            post.assert_not_called()  # bereits gemeldet
            gold = again["assets"][2]
            self.assertEqual(gold["error"], "nicht erreichbar")
            self.assertEqual(gold["last_week"], data["assets"][2]["last_week"])


if __name__ == "__main__":
    unittest.main()
