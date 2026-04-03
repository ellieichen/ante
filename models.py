import sqlite3
import os
import uuid
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), 'bidcredit.db')


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
            type TEXT NOT NULL CHECK(type IN ('restaurant', 'line')),
            description TEXT,
            location TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY,
            venue_id TEXT NOT NULL REFERENCES venues(id),
            title TEXT NOT NULL,
            date TEXT NOT NULL,
            time_slot TEXT,
            spots_available INTEGER NOT NULL DEFAULT 2,
            min_bid INTEGER NOT NULL DEFAULT 50,
            bid_deadline TEXT,
            status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'closed', 'completed')),
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS bids (
            id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL REFERENCES events(id),
            bidder_name TEXT NOT NULL,
            bidder_email TEXT NOT NULL,
            bidder_phone TEXT,
            amount INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'won', 'outbid', 'cancelled')),
            stripe_payment_intent TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
    ''')
    conn.commit()
    conn.close()


# --- Venue helpers ---

def create_venue(name, venue_type, description='', location=''):
    conn = get_db()
    venue_id = uuid.uuid4().hex[:8]
    conn.execute(
        'INSERT INTO venues (id, name, type, description, location) VALUES (?, ?, ?, ?, ?)',
        (venue_id, name, venue_type, description, location)
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


# --- Event helpers ---

def create_event(venue_id, title, date, time_slot='', spots_available=2, min_bid=50, bid_deadline=''):
    conn = get_db()
    event_id = uuid.uuid4().hex[:8]
    conn.execute(
        'INSERT INTO events (id, venue_id, title, date, time_slot, spots_available, min_bid, bid_deadline) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (event_id, venue_id, title, date, time_slot, spots_available, min_bid, bid_deadline)
    )
    conn.commit()
    conn.close()
    return event_id


def get_event(event_id):
    conn = get_db()
    event = conn.execute('SELECT * FROM events WHERE id = ?', (event_id,)).fetchone()
    conn.close()
    return event


def get_events_for_venue(venue_id):
    conn = get_db()
    events = conn.execute(
        'SELECT * FROM events WHERE venue_id = ? ORDER BY date DESC, created_at DESC',
        (venue_id,)
    ).fetchall()
    conn.close()
    return events


def close_event(event_id):
    conn = get_db()
    conn.execute("UPDATE events SET status = 'closed' WHERE id = ?", (event_id,))
    conn.commit()
    conn.close()


# --- Bid helpers ---

def place_bid(event_id, bidder_name, bidder_email, bidder_phone, amount, stripe_payment_intent=''):
    conn = get_db()
    bid_id = uuid.uuid4().hex[:8]
    conn.execute(
        'INSERT INTO bids (id, event_id, bidder_name, bidder_email, bidder_phone, amount, stripe_payment_intent) VALUES (?, ?, ?, ?, ?, ?, ?)',
        (bid_id, event_id, bidder_name, bidder_email, bidder_phone, amount, stripe_payment_intent)
    )
    conn.commit()
    conn.close()
    return bid_id


def get_bids_for_event(event_id):
    conn = get_db()
    bids = conn.execute(
        'SELECT * FROM bids WHERE event_id = ? ORDER BY amount DESC, created_at ASC',
        (event_id,)
    ).fetchall()
    conn.close()
    return bids


def get_bid(bid_id):
    conn = get_db()
    bid = conn.execute('SELECT * FROM bids WHERE id = ?', (bid_id,)).fetchone()
    conn.close()
    return bid


def get_winning_bids(event_id):
    """Get top N bids based on spots available (second-price auction)."""
    conn = get_db()
    event = conn.execute('SELECT spots_available FROM events WHERE id = ?', (event_id,)).fetchone()
    spots = event['spots_available'] if event else 0
    bids = conn.execute(
        'SELECT * FROM bids WHERE event_id = ? AND status != ? ORDER BY amount DESC, created_at ASC',
        (event_id, 'cancelled')
    ).fetchall()
    conn.close()
    return list(bids[:spots]), list(bids[spots:])


def update_bid_status(bid_id, status):
    conn = get_db()
    conn.execute('UPDATE bids SET status = ? WHERE id = ?', (status, bid_id))
    conn.commit()
    conn.close()


def get_bid_stats(event_id):
    """Get anonymous bid stats for display."""
    conn = get_db()
    stats = conn.execute('''
        SELECT
            COUNT(*) as total_bids,
            MAX(amount) as highest_bid,
            MIN(amount) as lowest_bid,
            AVG(amount) as avg_bid
        FROM bids WHERE event_id = ? AND status != 'cancelled'
    ''', (event_id,)).fetchone()
    conn.close()
    return stats
