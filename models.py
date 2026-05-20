import sqlite3
import os
import uuid
from datetime import datetime, timedelta

DB_PATH = os.path.join(os.path.dirname(__file__), 'bidcredit.db')

HOLD_RELEASE_MINUTES = 90
CONFIRM_WINDOW_MINUTES = 10
PENDING_BID_EXPIRY_MINUTES = 60

TABLE_SIZES = (2, 4, 6)


def map_party_to_table(party_size):
    """Bidder declares their party size; the system silently maps to the smallest
    table_size that fits. Party of 1 or 2 -> 2-top; 3 or 4 -> 4-top; 5 or 6 -> 6-top.
    Returns None for parties of 7+ (handled as walk-in, no bid available)."""
    if party_size is None:
        return None
    try:
        n = int(party_size)
    except (TypeError, ValueError):
        return None
    if n < 1:
        return None
    for size in TABLE_SIZES:
        if n <= size:
            return size
    return None


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
        conn.execute('DELETE FROM session_tables WHERE session_id = ?', (sid,))
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
    conn.execute('DELETE FROM session_tables WHERE session_id = ?', (session_id,))
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

def create_session(venue_id, table_mix):
    """Open a session with a per-bucket table mix. table_mix is a list of dicts:
    [{'table_size': 2, 'count': 3, 'min_bid': 50}, {'table_size': 4, 'count': 2, 'min_bid': 100}, ...]
    Only buckets with count > 0 are inserted. The legacy sessions.spots_available
    and sessions.min_bid columns are set to the sum-of-counts and lowest min_bid
    for back-compat with code/views that still read them."""
    buckets = [b for b in table_mix if int(b.get('count', 0)) > 0]
    if not buckets:
        raise ValueError('At least one table size must have count > 0')

    legacy_spots = sum(int(b['count']) for b in buckets)
    legacy_min_bid = min(int(b['min_bid']) for b in buckets)

    conn = get_db()
    session_id = uuid.uuid4().hex[:8]
    conn.execute(
        'INSERT INTO sessions (id, venue_id, spots_available, min_bid) VALUES (?, ?, ?, ?)',
        (session_id, venue_id, legacy_spots, legacy_min_bid)
    )
    for b in buckets:
        conn.execute(
            'INSERT INTO session_tables (session_id, table_size, count, min_bid) VALUES (?, ?, ?, ?)',
            (session_id, int(b['table_size']), int(b['count']), int(b['min_bid']))
        )
    conn.commit()
    conn.close()
    return session_id


def get_session_tables(session_id):
    """Return all bucket rows for a session, ordered by table_size ASC."""
    conn = get_db()
    rows = conn.execute(
        'SELECT * FROM session_tables WHERE session_id = ? ORDER BY table_size ASC',
        (session_id,)
    ).fetchall()
    conn.close()
    return list(rows)


def get_session_table(session_id, table_size):
    """Return a single bucket row, or None if the session doesn't offer that size."""
    conn = get_db()
    row = conn.execute(
        'SELECT * FROM session_tables WHERE session_id = ? AND table_size = ?',
        (session_id, int(table_size))
    ).fetchone()
    conn.close()
    return row


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


def get_pending_bids_in_rank_order(session_id, table_size=None):
    """Pending bids only, sorted by amount DESC then created_at ASC.
    If table_size is provided, filter to bids whose mapped party-size bucket
    matches. Used at session close to pick top N winners per bucket."""
    conn = get_db()
    if table_size is None:
        bids = conn.execute(
            """SELECT * FROM bids
               WHERE session_id = ? AND status = 'pending'
               ORDER BY amount DESC, created_at ASC""",
            (session_id,)
        ).fetchall()
    else:
        ts = int(table_size)
        # Party-size -> table-size mapping must match map_party_to_table.
        # 2-top: parties 1,2 | 4-top: parties 3,4 | 6-top: parties 5,6.
        bounds = {2: (1, 2), 4: (3, 4), 6: (5, 6)}
        lo, hi = bounds[ts]
        bids = conn.execute(
            """SELECT * FROM bids
               WHERE session_id = ? AND status = 'pending'
               AND party_size BETWEEN ? AND ?
               ORDER BY amount DESC, created_at ASC""",
            (session_id, lo, hi)
        ).fetchall()
    conn.close()
    return list(bids)


def find_next_promotable_runner_up(session_id, table_size):
    """Highest-ranked outbid bidder for this session AND bucket who opted in to
    auto-promote AND whose Stripe hold hasn't been released yet. None if no one
    qualifies. Scoped per-bucket so a 4-top forfeit doesn't promote a 2-top bidder."""
    bounds = {2: (1, 2), 4: (3, 4), 6: (5, 6)}
    lo, hi = bounds[int(table_size)]
    conn = get_db()
    bid = conn.execute(
        """SELECT * FROM bids
           WHERE session_id = ?
           AND status = 'outbid'
           AND auto_promote = 1
           AND released_at IS NULL
           AND party_size BETWEEN ? AND ?
           ORDER BY amount DESC, created_at ASC
           LIMIT 1""",
        (session_id, lo, hi)
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
              party_size, stripe_payment_intent='', auto_promote=True):
    conn = get_db()
    bid_id = uuid.uuid4().hex[:8]
    conn.execute(
        '''INSERT INTO bids
           (id, session_id, bidder_name, bidder_email, bidder_phone, amount,
            party_size, stripe_payment_intent, auto_promote)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (bid_id, session_id, bidder_name, bidder_email, bidder_phone, amount,
         int(party_size), stripe_payment_intent, 1 if auto_promote else 0)
    )
    conn.commit()
    conn.close()
    return bid_id


def get_bids_for_session(session_id, include_inactive=True, table_size=None):
    """Return bids for a session, ordered by amount DESC then created_at ASC.

    include_inactive=False excludes cancelled, expired, and forfeited bids — used
    by public bidder pages so dead bids don't pollute the visible leaderboard.
    table_size, if provided, filters to bids whose party-size maps to that bucket
    — used by the bidder status page to silently scope the leaderboard to the
    viewer's bucket.
    """
    params = [session_id]
    where = ['session_id = ?']
    if not include_inactive:
        where.append("status NOT IN ('cancelled', 'expired', 'forfeited')")
    if table_size is not None:
        bounds = {2: (1, 2), 4: (3, 4), 6: (5, 6)}
        lo, hi = bounds[int(table_size)]
        where.append('party_size BETWEEN ? AND ?')
        params.extend([lo, hi])
    sql = ('SELECT * FROM bids WHERE ' + ' AND '.join(where) +
           ' ORDER BY amount DESC, created_at ASC')
    conn = get_db()
    bids = conn.execute(sql, tuple(params)).fetchall()
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


def get_active_winners(session_id, table_size=None):
    """Bids currently holding a winning slot — either awaiting SMS confirm
    or already confirmed. Forfeited bids are explicitly excluded; the slot
    they vacated is filled by a promoted runner-up that ends up in this list.
    If table_size is provided, scope to that bucket only."""
    params = [session_id]
    where = ["session_id = ?", "status IN ('won_pending_confirm', 'won_confirmed')"]
    if table_size is not None:
        bounds = {2: (1, 2), 4: (3, 4), 6: (5, 6)}
        lo, hi = bounds[int(table_size)]
        where.append('party_size BETWEEN ? AND ?')
        params.extend([lo, hi])
    sql = ('SELECT * FROM bids WHERE ' + ' AND '.join(where) +
           ' ORDER BY amount DESC, created_at ASC')
    conn = get_db()
    bids = conn.execute(sql, tuple(params)).fetchall()
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


def get_bid_stats(session_id, table_size=None):
    """Aggregate bid stats for the session, optionally scoped to a single bucket.
    Bidder-facing pages pass table_size so the 'top bid' a bidder sees reflects
    their competition, not the whole session."""
    params = [session_id]
    where = ["session_id = ?", "status != 'cancelled'"]
    if table_size is not None:
        bounds = {2: (1, 2), 4: (3, 4), 6: (5, 6)}
        lo, hi = bounds[int(table_size)]
        where.append('party_size BETWEEN ? AND ?')
        params.extend([lo, hi])
    sql = '''
        SELECT
            COUNT(*) as total_bids,
            MAX(amount) as highest_bid,
            MIN(amount) as lowest_bid,
            AVG(amount) as avg_bid
        FROM bids WHERE ''' + ' AND '.join(where)
    conn = get_db()
    stats = conn.execute(sql, tuple(params)).fetchone()
    conn.close()
    return stats
