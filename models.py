import sqlite3
import os
import uuid
from datetime import datetime, timedelta

DB_PATH = os.path.join(os.path.dirname(__file__), 'bidcredit.db')

HOLD_RELEASE_MINUTES = 90
CONFIRM_WINDOW_MINUTES = 10
PENDING_BID_EXPIRY_MINUTES = 60


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


MIGRATIONS_DIR = os.path.join(os.path.dirname(__file__), 'migrations')


def init_db():
    """Apply any pending migrations from migrations/. Safe to call on every
    startup — only files whose number > PRAGMA user_version are run, in order,
    each inside its own transaction. See migrations/README.md for the full
    contract; the short version is: never edit existing migration files,
    always add a new numbered .sql file for any schema change."""
    conn = get_db()
    current = conn.execute('PRAGMA user_version').fetchone()[0]

    pending = []
    if os.path.isdir(MIGRATIONS_DIR):
        for filename in sorted(os.listdir(MIGRATIONS_DIR)):
            if not filename.endswith('.sql'):
                continue
            try:
                version = int(filename.split('_', 1)[0])
            except ValueError:
                continue
            if version > current:
                pending.append((version, filename))

    for version, filename in pending:
        path = os.path.join(MIGRATIONS_DIR, filename)
        with open(path, 'r') as f:
            sql = f.read()
        try:
            conn.execute('BEGIN')
            conn.executescript(sql)
            conn.execute(f'PRAGMA user_version = {version}')
            conn.commit()
        except Exception:
            conn.rollback()
            conn.close()
            raise

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


def update_venue(venue_id, name, description, location):
    conn = get_db()
    conn.execute(
        'UPDATE venues SET name = ?, description = ?, location = ? WHERE id = ?',
        (name, description, location, venue_id)
    )
    conn.commit()
    conn.close()


def delete_venue(venue_id):
    """Delete a venue along with all its sessions and bids. Returns False if an
    open session exists — admin must close it first to avoid stranding Stripe
    holds. POC convenience — primarily used to clean up demo data."""
    conn = get_db()
    open_count = conn.execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE venue_id = ? AND status = 'open'",
        (venue_id,)
    ).fetchone()['n']
    if open_count > 0:
        conn.close()
        return False
    session_ids = [r['id'] for r in conn.execute(
        'SELECT id FROM sessions WHERE venue_id = ?', (venue_id,)
    ).fetchall()]
    for sid in session_ids:
        conn.execute('DELETE FROM bids WHERE session_id = ?', (sid,))
    conn.execute('DELETE FROM sessions WHERE venue_id = ?', (venue_id,))
    conn.execute('DELETE FROM venues WHERE id = ?', (venue_id,))
    conn.commit()
    conn.close()
    return True


def delete_session(session_id):
    """Delete a session along with all its bids. Returns False if the session
    is still open — admin must close it first."""
    conn = get_db()
    session = conn.execute(
        'SELECT status FROM sessions WHERE id = ?', (session_id,)
    ).fetchone()
    if session and session['status'] == 'open':
        conn.close()
        return False
    conn.execute('DELETE FROM bids WHERE session_id = ?', (session_id,))
    conn.execute('DELETE FROM sessions WHERE id = ?', (session_id,))
    conn.commit()
    conn.close()
    return True


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
    """Atomic session close. Returns True if this call won the close-race
    (open → closed), False if the session was already closed or doesn't exist."""
    conn = get_db()
    cursor = conn.execute(
        "UPDATE sessions SET status = 'closed', closed_at = datetime('now') WHERE id = ? AND status = 'open'",
        (session_id,)
    )
    conn.commit()
    affected = cursor.rowcount
    conn.close()
    return affected > 0


def get_pending_bids_in_rank_order(session_id):
    """Pending bids only, sorted by amount DESC then created_at ASC.
    Used at session close to pick winners (top N) and outbid the rest."""
    conn = get_db()
    bids = conn.execute(
        """SELECT * FROM bids
           WHERE session_id = ? AND status = 'pending'
           ORDER BY amount DESC, created_at ASC""",
        (session_id,)
    ).fetchall()
    conn.close()
    return list(bids)


def find_next_promotable_runner_up(session_id):
    """Highest-ranked outbid bidder for this session who opted in to auto-promote
    AND whose Stripe hold hasn't been released yet. None if no one qualifies."""
    conn = get_db()
    bid = conn.execute(
        """SELECT * FROM bids
           WHERE session_id = ?
           AND status = 'outbid'
           AND auto_promote = 1
           AND released_at IS NULL
           ORDER BY amount DESC, created_at ASC
           LIMIT 1""",
        (session_id,)
    ).fetchone()
    conn.close()
    return bid


def promote_outbid_to_pending_confirm(bid_id):
    """Atomic: outbid → won_pending_confirm with a fresh 10-min confirm window.
    Used when a winner forfeits and the next opted-in runner-up gets the spot.
    Returns True if the transition happened — False means the bid moved on
    (e.g. their hold was released first, or another concurrent promotion ran)."""
    conn = get_db()
    cursor = conn.execute(
        """UPDATE bids
           SET status = 'won_pending_confirm',
               confirm_window_started_at = strftime('%Y-%m-%d %H:%M:%f', 'now')
           WHERE id = ? AND status = 'outbid' AND released_at IS NULL""",
        (bid_id,)
    )
    conn.commit()
    affected = cursor.rowcount
    conn.close()
    return affected > 0


def transition_to_pending_confirm(bid_id):
    """Atomic: pending → won_pending_confirm AND start the confirm-window clock.
    Returns True if the transition happened, False if the bid wasn't pending
    (e.g. it was cancelled in between, or another close already promoted it)."""
    conn = get_db()
    cursor = conn.execute(
        """UPDATE bids
           SET status = 'won_pending_confirm',
               confirm_window_started_at = strftime('%Y-%m-%d %H:%M:%f', 'now')
           WHERE id = ? AND status = 'pending'""",
        (bid_id,)
    )
    conn.commit()
    affected = cursor.rowcount
    conn.close()
    return affected > 0


# ─── Bids ──────────────────────────────────────────────────────

def place_bid(session_id, bidder_name, bidder_email, bidder_phone, amount,
              stripe_payment_intent='', auto_promote=True):
    conn = get_db()
    bid_id = uuid.uuid4().hex[:8]
    conn.execute(
        '''INSERT INTO bids
           (id, session_id, bidder_name, bidder_email, bidder_phone, amount,
            stripe_payment_intent, auto_promote)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
        (bid_id, session_id, bidder_name, bidder_email, bidder_phone, amount,
         stripe_payment_intent, 1 if auto_promote else 0)
    )
    conn.commit()
    conn.close()
    return bid_id


def get_bids_for_session(session_id, include_inactive=True):
    """Return bids for a session, ordered by amount DESC then created_at ASC.

    include_inactive=False excludes cancelled, expired, and forfeited bids — used
    by public bidder pages so dead bids don't pollute the visible leaderboard.
    """
    conn = get_db()
    if include_inactive:
        bids = conn.execute(
            'SELECT * FROM bids WHERE session_id = ? ORDER BY amount DESC, created_at ASC',
            (session_id,)
        ).fetchall()
    else:
        bids = conn.execute(
            """SELECT * FROM bids WHERE session_id = ?
               AND status NOT IN ('cancelled', 'expired', 'forfeited')
               ORDER BY amount DESC, created_at ASC""",
            (session_id,)
        ).fetchall()
    conn.close()
    return bids


def get_bid(bid_id):
    conn = get_db()
    bid = conn.execute('SELECT * FROM bids WHERE id = ?', (bid_id,)).fetchone()
    conn.close()
    return bid


def _format_threshold_minutes_ago(minutes):
    """Compose a timestamp string in the same format the bids table stores
    (YYYY-MM-DD HH:MM:SS.fff with millisecond precision) representing 'now
    minus N minutes'. Used to lex-compare against stored timestamps."""
    return (datetime.utcnow() - timedelta(minutes=minutes)).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]


def expire_stale_pending_bids():
    """Auto-expire pending bids placed more than PENDING_BID_EXPIRY_MINUTES ago.
    Backstop for sessions that never close — protects bidders from indefinite
    card holds when an admin forgets to close. Returns expired bid records so
    callers can release Stripe holds."""
    threshold = _format_threshold_minutes_ago(PENDING_BID_EXPIRY_MINUTES)
    conn = get_db()
    candidates = conn.execute(
        "SELECT * FROM bids WHERE status = 'pending' AND created_at < ?",
        (threshold,)
    ).fetchall()
    expired = []
    for bid in candidates:
        cursor = conn.execute(
            "UPDATE bids SET status = 'expired' WHERE id = ? AND status = 'pending'",
            (bid['id'],)
        )
        if cursor.rowcount > 0:
            expired.append(bid)
    conn.commit()
    conn.close()
    return expired


def expire_stale_confirm_windows():
    """Forfeit any won_pending_confirm bids whose 10-min window has elapsed.
    Returns the forfeited bid records so callers can release Stripe holds and
    trigger runner-up promotion. Caller must use atomic transitions in case
    a bidder's Y/N reply lands in the same instant."""
    threshold = _format_threshold_minutes_ago(CONFIRM_WINDOW_MINUTES)
    conn = get_db()
    candidates = conn.execute(
        """SELECT * FROM bids
           WHERE status = 'won_pending_confirm'
           AND confirm_window_started_at IS NOT NULL
           AND confirm_window_started_at < ?""",
        (threshold,)
    ).fetchall()
    forfeited = []
    for bid in candidates:
        cursor = conn.execute(
            """UPDATE bids SET status = 'forfeited'
               WHERE id = ? AND status = 'won_pending_confirm'""",
            (bid['id'],)
        )
        if cursor.rowcount > 0:
            forfeited.append(bid)
    conn.commit()
    conn.close()
    return forfeited


def find_pending_confirm_bid_by_phone(phone):
    """Find the most recent bid in won_pending_confirm state for this phone.
    Used to route inbound Y/N SMS replies to the correct bid. Phone is matched
    in normalized E.164 form, the same form Twilio sends in the From field."""
    if not phone:
        return None
    conn = get_db()
    bid = conn.execute(
        """SELECT * FROM bids
           WHERE bidder_phone = ? AND status = 'won_pending_confirm'
           ORDER BY confirm_window_started_at DESC LIMIT 1""",
        (phone,)
    ).fetchone()
    conn.close()
    return bid


def get_active_winners(session_id):
    """Bids currently holding a winning slot — either awaiting SMS confirm
    or already confirmed. Forfeited bids are explicitly excluded; the slot
    they vacated is filled by a promoted runner-up that ends up in this list."""
    conn = get_db()
    bids = conn.execute(
        """SELECT * FROM bids
           WHERE session_id = ?
           AND status IN ('won_pending_confirm', 'won_confirmed')
           ORDER BY amount DESC, created_at ASC""",
        (session_id,)
    ).fetchall()
    conn.close()
    return list(bids)


def update_bid_status(bid_id, status):
    conn = get_db()
    conn.execute('UPDATE bids SET status = ? WHERE id = ?', (status, bid_id))
    conn.commit()
    conn.close()


def transition_bid_status(bid_id, from_status, to_status):
    """Atomic conditional update: only flip from `from_status` to `to_status`.
    Returns True if the transition happened, False if the bid was no longer in
    `from_status` (lost the race). Use this for any state change that could
    race with another request — bidder cancel vs. session close, winner confirm
    vs. timeout, runner-up promotion, etc."""
    conn = get_db()
    cursor = conn.execute(
        'UPDATE bids SET status = ? WHERE id = ? AND status = ?',
        (to_status, bid_id, from_status)
    )
    conn.commit()
    affected = cursor.rowcount
    conn.close()
    return affected > 0


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
