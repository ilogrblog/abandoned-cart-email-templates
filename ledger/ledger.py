"""A send ledger for abandoned cart emails: when to skip, cancel or never repeat a send.

A reference for the rules, not a production scheduler. It uses SQLite and the standard library only, so you
can read it, run the tests and port the rules to your own stack (a queue worker, a Shopify Flow, a WooCommerce
Action Scheduler job, a Magento cron). Times are Unix seconds in UTC.

The rules, in the order they are checked just before each send:

  1. The address is suppressed (unsubscribed, bounced, complained).        -> skip "suppressed"
  2. The customer ordered after the cart was last changed.                 -> skip "ordered"
  3. The cart is empty.                                                    -> skip "empty_cart"
  4. Every item is out of stock (otherwise out-of-stock lines are dropped and live prices are used).
                                                                           -> skip "out_of_stock"
  5. The address already received the maximum number of cart emails in the cap window.
                                                                           -> skip "frequency_cap"
  6. Another flow emailed the address recently.                            -> skip "recent_contact"
  7. The job is running too late (an outage): the 1 hour email is not sent at 30 hours.
                                                                           -> skip "too_late"

Re-runs are safe because a row is claimed with one conditional UPDATE before anything is sent, and the
(cart, step) pair is the primary key, so scheduling twice cannot create a second row. A crash after a claim
leaves the row claimed and it is never sent again: one missed reminder is cheaper than a duplicate.
"""
import json
import sqlite3
from dataclasses import dataclass

HOUR = 3600
DAY = 24 * HOUR
STEPS = (("step1", 1 * HOUR), ("step2", 24 * HOUR), ("step3", 48 * HOUR))
FLOW = "abandoned_cart"


@dataclass(frozen=True)
class Policy:
    cap_count: int = 3            # cart emails per address ...
    cap_window: int = 7 * DAY     # ... in this window
    other_flow_gap: int = 12 * HOUR
    grace: int = 6 * HOUR         # how late a due email may still go out


SCHEMA = """
CREATE TABLE IF NOT EXISTS carts (cart_id TEXT PRIMARY KEY, email TEXT NOT NULL, last_activity INTEGER NOT NULL, items TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ledger (
  cart_id TEXT NOT NULL, step TEXT NOT NULL, due_at INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'scheduled', reason TEXT, claimed_at INTEGER, sent_at INTEGER,
  PRIMARY KEY (cart_id, step));
CREATE TABLE IF NOT EXISTS orders (order_id TEXT PRIMARY KEY, email TEXT NOT NULL, placed_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS suppressions (email TEXT PRIMARY KEY, reason TEXT NOT NULL, at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS stock (sku TEXT PRIMARY KEY, available INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS prices (sku TEXT PRIMARY KEY, price_minor INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sent_log (email TEXT NOT NULL, flow TEXT NOT NULL, sent_at INTEGER NOT NULL);
"""


def connect(path=":memory:"):
    conn = sqlite3.connect(path, isolation_level=None)  # autocommit: every statement is atomic on its own
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def norm(email):
    return email.strip().lower()


def schedule(conn, cart_id, email, last_activity, items):
    """Record the cart and create its three rows. Calling it again for the same cart creates nothing new."""
    conn.execute(
        "INSERT INTO carts (cart_id, email, last_activity, items) VALUES (?,?,?,?) "
        "ON CONFLICT(cart_id) DO NOTHING",
        (cart_id, norm(email), last_activity, json.dumps(items)),
    )
    created = 0
    for step, delay in STEPS:
        cur = conn.execute(
            "INSERT OR IGNORE INTO ledger (cart_id, step, due_at) VALUES (?,?,?)",
            (cart_id, step, last_activity + delay),
        )
        created += cur.rowcount
    return created


def on_cart_change(conn, cart_id, last_activity, items):
    """The customer changed the cart. Before the first email goes out, restart the clock. Afterwards, leave it."""
    sent = conn.execute(
        "SELECT COUNT(*) FROM ledger WHERE cart_id=? AND status IN ('claimed','sent')", (cart_id,)
    ).fetchone()[0]
    conn.execute("UPDATE carts SET last_activity=?, items=? WHERE cart_id=?", (last_activity, json.dumps(items), cart_id))
    if sent:
        return False
    for step, delay in STEPS:
        conn.execute(
            "UPDATE ledger SET due_at=? WHERE cart_id=? AND step=? AND status='scheduled'",
            (last_activity + delay, cart_id, step),
        )
    return True


def suppress(conn, email, reason, at):
    conn.execute("INSERT OR REPLACE INTO suppressions (email, reason, at) VALUES (?,?,?)", (norm(email), reason, at))


def claim(conn, cart_id, step, now):
    """Take a row for sending. True for exactly one caller, however many workers run at once."""
    cur = conn.execute(
        "UPDATE ledger SET status='claimed', claimed_at=? WHERE cart_id=? AND step=? AND status='scheduled'",
        (now, cart_id, step),
    )
    return cur.rowcount == 1


def decide(conn, cart, row, now, policy=Policy()):
    """Return ("send", live_items) or ("skip", reason). Reads only; changes nothing."""
    email = cart["email"]
    if conn.execute("SELECT 1 FROM suppressions WHERE email=?", (email,)).fetchone():
        return "skip", "suppressed"
    if conn.execute("SELECT 1 FROM orders WHERE email=? AND placed_at>=?", (email, cart["last_activity"])).fetchone():
        return "skip", "ordered"
    items = json.loads(cart["items"])
    if not items:
        return "skip", "empty_cart"
    live = []
    for item in items:
        stock = conn.execute("SELECT available FROM stock WHERE sku=?", (item["sku"],)).fetchone()
        if stock is None or stock["available"] < 1:
            continue
        price = conn.execute("SELECT price_minor FROM prices WHERE sku=?", (item["sku"],)).fetchone()
        live.append({**item, "price_minor": price["price_minor"] if price else item["price_minor"]})
    if not live:
        return "skip", "out_of_stock"
    recent_own = conn.execute(
        "SELECT COUNT(*) FROM sent_log WHERE email=? AND flow=? AND sent_at>?", (email, FLOW, now - policy.cap_window)
    ).fetchone()[0]
    if recent_own >= policy.cap_count:
        return "skip", "frequency_cap"
    if conn.execute(
        "SELECT 1 FROM sent_log WHERE email=? AND flow<>? AND sent_at>?", (email, FLOW, now - policy.other_flow_gap)
    ).fetchone():
        return "skip", "recent_contact"
    if now - row["due_at"] > policy.grace:
        return "skip", "too_late"
    return "send", live


def run_due(conn, now, send, policy=Policy()):
    """Process every row that is due. `send(email, step, items)` does the actual sending and may raise."""
    results = []
    due = conn.execute(
        "SELECT * FROM ledger WHERE status='scheduled' AND due_at<=? ORDER BY due_at, cart_id, step", (now,)
    ).fetchall()
    for row in due:
        if not claim(conn, row["cart_id"], row["step"], now):
            continue  # another worker took it
        cart = conn.execute("SELECT * FROM carts WHERE cart_id=?", (row["cart_id"],)).fetchone()
        action, detail = decide(conn, cart, row, now, policy)
        if action == "skip":
            conn.execute("UPDATE ledger SET status='skipped', reason=? WHERE cart_id=? AND step=?", (detail, row["cart_id"], row["step"]))
            results.append((row["cart_id"], row["step"], "skipped", detail))
            continue
        try:
            send(cart["email"], row["step"], detail)
        except Exception as exc:  # noqa: BLE001 - any failure must end in a recorded state
            conn.execute("UPDATE ledger SET status='failed', reason=? WHERE cart_id=? AND step=?", (type(exc).__name__, row["cart_id"], row["step"]))
            results.append((row["cart_id"], row["step"], "failed", type(exc).__name__))
            continue
        conn.execute("UPDATE ledger SET status='sent', sent_at=? WHERE cart_id=? AND step=?", (now, row["cart_id"], row["step"]))
        conn.execute("INSERT INTO sent_log (email, flow, sent_at) VALUES (?,?,?)", (cart["email"], FLOW, now))
        results.append((row["cart_id"], row["step"], "sent", ""))
    return results
