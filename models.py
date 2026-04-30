import sqlite3
import os
import uuid

DB_PATH = os.path.join(os.path.dirname(__file__), 'bidcredit.db')

HOLD_RELEASE_MINUTES = 90


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS venues (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            location TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            venue_id TEXT NOT NULL REFERENCES venues(id),
            spots_available INTEGER NOT NULL DEFAULT 1,
            min_bid INTEGER NOT NULL DEFAULT 50,
            status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'closed')),
            closed_at TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS bids (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            bidder_name TEXT NOT NULL,
            bidder_email TEXT NOT NULL,
            bidder_phone TEXT,
            amount INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'won', 'outbid', 'cancelled')),
            stripe_payment_intent TEXT,
            released_at TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
    ''')
    conn.commit()
    conn.close()


# ─── Venues ────────────────────────────────────────────────────

def create_venue(name, description='', location=''):
    conn = get_db()
    venue_id = uuid.uuid4().hex[:8]
    conn.execute(
        'INSERT INTO venues (id, name, description, location) VALUES (?, ?, ?, ?)',
        (venue_id, name, description, location)
    )
    conn.commit()
    conn.close()
    return venue_id


def get_venue(venue_id):
    conn = get_db()
    venue = conn.execute('SELECT * FROM venues WHERE id = ?', (venue_id,)).fetchone()
    conn.close()
    return venue


def get_all_venues():
    conn = get_db()
    venues = conn.execute('SELECT * FROM venues ORDER BY created_at DESC').fetchall()
    conn.close()
    return venues


# ─── Sessions ──────────────────────────────────────────────────

def create_session(venue_id, spots_available=1, min_bid=50):
    conn = get_db()
    session_id = uuid.uuid4().hex[:8]
    conn.execute(
        'INSERT INTO sessions (id, venue_id, spots_available, min_bid) VALUES (?, ?, ?, ?)',
        (session_id, venue_id, spots_available, min_bid)
    )
    conn.commit()
    conn.close()
    return session_id


def get_session(session_id):
    conn = get_db()
    session = conn.execute('SELECT * FROM sessions WHERE id = ?', (session_id,)).fetchone()
    conn.close()
    return session


def get_open_session_for_venue(venue_id):
    conn = get_db()
    session = conn.execute(
        "SELECT * FROM sessions WHERE venue_id = ? AND status = 'open' ORDER BY created_at DESC LIMIT 1",
        (venue_id,)
    ).fetchone()
    conn.close()
    return session


def get_sessions_for_venue(venue_id):
    conn = get_db()
    sessions = conn.execute(
        'SELECT * FROM sessions WHERE venue_id = ? ORDER BY created_at DESC',
        (venue_id,)
    ).fetchall()
    conn.close()
    return sessions


def close_session(session_id):
    conn = get_db()
    conn.execute(
        "UPDATE sessions SET status = 'closed', closed_at = datetime('now') WHERE id = ?",
        (session_id,)
    )
    conn.commit()
    conn.close()


# ─── Bids ──────────────────────────────────────────────────────

def place_bid(session_id, bidder_name, bidder_email, bidder_phone, amount, stripe_payment_intent=''):
    conn = get_db()
    bid_id = uuid.uuid4().hex[:8]
    conn.execute(
        'INSERT INTO bids (id, session_id, bidder_name, bidder_email, bidder_phone, amount, stripe_payment_intent) VALUES (?, ?, ?, ?, ?, ?, ?)',
        (bid_id, session_id, bidder_name, bidder_email, bidder_phone, amount, stripe_payment_intent)
    )
    conn.commit()
    conn.close()
    return bid_id


def get_bids_for_session(session_id):
    conn = get_db()
    bids = conn.execute(
        'SELECT * FROM bids WHERE session_id = ? ORDER BY amount DESC, created_at ASC',
        (session_id,)
    ).fetchall()
    conn.close()
    return bids


def get_bid(bid_id):
    conn = get_db()
    bid = conn.execute('SELECT * FROM bids WHERE id = ?', (bid_id,)).fetchone()
    conn.close()
    return bid


def get_winners_and_losers(session_id):
    """Top N bids (where N = spots_available) win at their own bid amount (first-price)."""
    conn = get_db()
    session = conn.execute('SELECT spots_available FROM sessions WHERE id = ?', (session_id,)).fetchone()
    spots = session['spots_available'] if session else 0
    bids = conn.execute(
        "SELECT * FROM bids WHERE session_id = ? AND status != 'cancelled' ORDER BY amount DESC, created_at ASC",
        (session_id,)
    ).fetchall()
    conn.close()
    return list(bids[:spots]), list(bids[spots:])


def update_bid_status(bid_id, status):
    conn = get_db()
    conn.execute('UPDATE bids SET status = ? WHERE id = ?', (status, bid_id))
    conn.commit()
    conn.close()


def update_bid_amount(bid_id, amount):
    conn = get_db()
    conn.execute('UPDATE bids SET amount = ? WHERE id = ?', (amount, bid_id))
    conn.commit()
    conn.close()


def mark_bid_released(bid_id):
    conn = get_db()
    conn.execute("UPDATE bids SET released_at = datetime('now') WHERE id = ?", (bid_id,))
    conn.commit()
    conn.close()


def get_outbid_unreleased(session_id):
    """Outbid bids that still have a hold to release."""
    conn = get_db()
    bids = conn.execute(
        "SELECT * FROM bids WHERE session_id = ? AND status = 'outbid' AND released_at IS NULL AND stripe_payment_intent != ''",
        (session_id,)
    ).fetchall()
    conn.close()
    return list(bids)


def get_bid_stats(session_id):
    conn = get_db()
    stats = conn.execute('''
        SELECT
            COUNT(*) as total_bids,
            MAX(amount) as highest_bid,
            MIN(amount) as lowest_bid,
            AVG(amount) as avg_bid
        FROM bids WHERE session_id = ? AND status != 'cancelled'
    ''', (session_id,)).fetchone()
    conn.close()
    return stats
