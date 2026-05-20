-- Sessions become independent auctions per table size. Each session has 1+
-- session_tables rows (one per table size offered tonight) with its own count
-- and minimum bid. Bidders pick party size; the mapping party_size -> table_size
-- (2 or 4 or 6) is enforced in application code.
--
-- Backfill: every existing session gets a single 2-top bucket with the legacy
-- spots_available and min_bid. Every existing bid gets party_size = 2. This
-- keeps live data intact and lets old sessions/bids continue to function under
-- the new model without any data loss.

CREATE TABLE IF NOT EXISTS session_tables (
    session_id TEXT NOT NULL REFERENCES sessions(id),
    table_size INTEGER NOT NULL CHECK(table_size IN (2, 4, 6)),
    count INTEGER NOT NULL CHECK(count >= 0),
    min_bid INTEGER NOT NULL CHECK(min_bid >= 1),
    PRIMARY KEY (session_id, table_size)
);

ALTER TABLE bids ADD COLUMN party_size INTEGER NOT NULL DEFAULT 2;

INSERT INTO session_tables (session_id, table_size, count, min_bid)
SELECT id, 2, spots_available, min_bid FROM sessions;
