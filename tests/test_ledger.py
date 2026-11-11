import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ledger"))
import ledger as L  # noqa: E402

T0 = 1_800_000_000
ITEMS = [{"sku": "MUG", "title": "Mug", "qty": 1, "price_minor": 2800}, {"sku": "SCARF", "title": "Scarf", "qty": 1, "price_minor": 3500}]


class Base(unittest.TestCase):
    def setUp(self):
        self.conn = L.connect()
        self.sent = []
        for sku in ("MUG", "SCARF"):
            self.conn.execute("INSERT INTO stock VALUES (?,?)", (sku, 5))
        L.schedule(self.conn, "c1", "Buyer@Example.net", T0, ITEMS)

    def send(self, email, step, items):
        self.sent.append((email, step, [i["sku"] for i in items], [i["price_minor"] for i in items]))

    def run_at(self, hours):
        return L.run_due(self.conn, T0 + int(hours * 3600), self.send)

    def statuses(self):
        return {r["step"]: (r["status"], r["reason"]) for r in self.conn.execute("SELECT * FROM ledger ORDER BY step")}


class Schedule(Base):
    def test_three_rows_at_one_24_and_48_hours(self):
        due = {r["step"]: r["due_at"] - T0 for r in self.conn.execute("SELECT * FROM ledger")}
        self.assertEqual(due, {"step1": 3600, "step2": 86400, "step3": 172800})

    def test_scheduling_twice_creates_nothing_new(self):
        self.assertEqual(L.schedule(self.conn, "c1", "buyer@example.net", T0, ITEMS), 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0], 3)

    def test_nothing_is_sent_before_it_is_due(self):
        self.assertEqual(self.run_at(0.5), [])
        self.assertEqual(self.sent, [])

    def test_full_sequence_sends_three_emails_to_the_normalised_address(self):
        for h in (1, 24, 48):
            self.run_at(h)
        self.assertEqual([s[1] for s in self.sent], ["step1", "step2", "step3"])
        self.assertTrue(all(s[0] == "buyer@example.net" for s in self.sent))

    def test_changing_the_cart_before_the_first_email_restarts_the_clock(self):
        self.assertTrue(L.on_cart_change(self.conn, "c1", T0 + 1800, ITEMS))
        self.assertEqual(self.run_at(1.2), [])          # the old 1 hour mark passes: nothing goes out
        self.run_at(1.6)                                 # 1800 s + 1 h
        self.assertEqual(len(self.sent), 1)

    def test_changing_the_cart_after_the_first_email_does_not_restart_the_sequence(self):
        self.run_at(1)
        self.assertFalse(L.on_cart_change(self.conn, "c1", T0 + 7200, ITEMS))
        self.run_at(24)
        self.assertEqual([s[1] for s in self.sent], ["step1", "step2"])


class Cancel(Base):
    def test_order_before_first_email_cancels_everything(self):
        self.conn.execute("INSERT INTO orders VALUES ('o1','buyer@example.net',?)", (T0 + 600,))
        for h in (1, 24, 48):
            self.run_at(h)
        self.assertEqual(self.sent, [])
        self.assertEqual({v for v in self.statuses().values()}, {("skipped", "ordered")})

    def test_order_after_first_email_stops_the_rest(self):
        self.run_at(1)
        self.conn.execute("INSERT INTO orders VALUES ('o1','buyer@example.net',?)", (T0 + 5 * 3600,))
        self.run_at(24)
        self.run_at(48)
        self.assertEqual([s[1] for s in self.sent], ["step1"])
        self.assertEqual(self.statuses()["step2"], ("skipped", "ordered"))

    def test_an_order_placed_before_the_cart_was_last_changed_does_not_cancel(self):
        self.conn.execute("INSERT INTO orders VALUES ('old','buyer@example.net',?)", (T0 - 86400,))
        self.run_at(1)
        self.assertEqual(len(self.sent), 1)

    def test_unsubscribe_between_emails_stops_the_rest(self):
        self.run_at(1)
        L.suppress(self.conn, "BUYER@example.net", "unsubscribed", T0 + 7200)
        self.run_at(24)
        self.assertEqual(self.statuses()["step2"], ("skipped", "suppressed"))
        self.assertEqual(len(self.sent), 1)

    def test_all_items_out_of_stock_skips(self):
        self.conn.execute("UPDATE stock SET available=0")
        self.run_at(1)
        self.assertEqual(self.statuses()["step1"], ("skipped", "out_of_stock"))

    def test_some_items_out_of_stock_are_dropped_and_live_price_is_used(self):
        self.conn.execute("UPDATE stock SET available=0 WHERE sku='MUG'")
        self.conn.execute("INSERT INTO prices VALUES ('SCARF', 2999)")
        self.run_at(1)
        self.assertEqual(self.sent[0][2:], (["SCARF"], [2999]))

    def test_empty_cart_skips(self):
        self.conn.execute("UPDATE carts SET items='[]'")
        self.run_at(1)
        self.assertEqual(self.statuses()["step1"], ("skipped", "empty_cart"))


class Caps(Base):
    def test_frequency_cap_stops_a_fourth_email_in_the_window(self):
        for n in range(3):
            self.conn.execute("INSERT INTO sent_log VALUES ('buyer@example.net', ?, ?)", (L.FLOW, T0 - n * 3600))
        self.run_at(1)
        self.assertEqual(self.statuses()["step1"], ("skipped", "frequency_cap"))

    def test_emails_older_than_the_window_do_not_count(self):
        for n in range(3):
            self.conn.execute("INSERT INTO sent_log VALUES ('buyer@example.net', ?, ?)", (L.FLOW, T0 - 8 * 86400 - n))
        self.run_at(1)
        self.assertEqual(len(self.sent), 1)

    def test_recent_email_from_another_flow_skips(self):
        self.conn.execute("INSERT INTO sent_log VALUES ('buyer@example.net', 'welcome', ?)", (T0 + 3600 - 2 * 3600,))
        self.run_at(1)
        self.assertEqual(self.statuses()["step1"], ("skipped", "recent_contact"))

    def test_late_worker_does_not_send_a_backlog(self):
        self.run_at(30)  # the job was down; the 1 hour email is 29 hours late, the 24 hour one is 6 hours late
        self.assertEqual(self.statuses()["step1"], ("skipped", "too_late"))
        self.assertEqual(self.statuses()["step2"][0], "sent")


class SafeReruns(Base):
    def test_running_the_same_minute_twice_sends_once(self):
        self.run_at(1)
        self.run_at(1)
        self.assertEqual(len(self.sent), 1)

    def test_two_workers_cannot_claim_the_same_row(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "ledger.db")
            a, b = L.connect(path), L.connect(path)
            L.schedule(a, "c9", "x@example.net", T0, ITEMS)
            self.assertTrue(L.claim(a, "c9", "step1", T0 + 3600))
            self.assertFalse(L.claim(b, "c9", "step1", T0 + 3600))

    def test_a_crash_after_the_claim_never_sends_twice(self):
        L.claim(self.conn, "c1", "step1", T0 + 3600)   # a worker claimed the row and died before sending
        self.run_at(2)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.statuses()["step1"][0], "claimed")

    def test_a_send_error_is_recorded_and_not_retried(self):
        def boom(*args):
            raise ConnectionError("smtp down")
        L.run_due(self.conn, T0 + 3600, boom)
        self.assertEqual(self.statuses()["step1"], ("failed", "ConnectionError"))
        self.run_at(2)
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
