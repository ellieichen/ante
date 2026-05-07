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
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN (
                'pending',
                'cancelled',
                'expired',
                'won_pending_confirm',
                'won_confirmed',
                'forfeited',
                'outbid'
            )),
            auto_promote INTEGER NOT NULL DEFAULT 1,
            stripe_payment_intent TEXT,
            released_at TEXT,
            confirm_window_started_at TEXT,
            created_at TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now'))
        );
