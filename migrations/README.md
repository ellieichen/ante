# Migrations

Schema changes go here. **Never edit `models.py`'s `CREATE TABLE` statements directly** — `CREATE TABLE IF NOT EXISTS` silently does nothing on tables that already exist, so changes there are invisible to existing databases (this is what bit us on 2026-05-07).

## How it works

- Each migration is a `.sql` file numbered sequentially: `0001_initial_schema.sql`, `0002_add_foo.sql`, etc.
- `models.init_db()` runs all migrations whose number is greater than the DB's current `PRAGMA user_version`, in order, in transactions.
- After each successful migration, `user_version` is bumped so it's not re-run.
- This works on both fresh databases and existing ones — no special "first deploy" handling needed.

## Adding a new migration

1. Pick the next number (look at the highest existing file).
2. Create `NNNN_short_description.sql` describing the change.
3. **Test locally first** by running `python -c "import models; models.init_db()"` against a copy of prod data, not just an empty DB.
4. Commit, push, deploy.

### SQLite caveats

- `ALTER TABLE ADD COLUMN` works; **`ALTER TABLE ALTER COLUMN` does not exist** in SQLite.
- To change a column's type, default, or `CHECK` constraint, you have to rebuild the table: create a new table with the desired schema, `INSERT ... SELECT` the data over, drop the old, rename. See [SQLite docs](https://www.sqlite.org/lang_altertable.html).
- Wrap any rebuild in `BEGIN ... COMMIT` so a failure rolls back cleanly. Set `PRAGMA foreign_keys=OFF` *before* `BEGIN` and `ON` after `COMMIT`.

## Why this instead of alembic / yoyo / Flask-Migrate

Per research on 2026-05-07 (see `feedback_research_before_implementing.md` in Claude memory): for a single-developer POC with one production environment, ~30 lines of DIY code is simpler than adding a tool. Migrate to `yoyo-migrations` if/when a second developer joins or a staging environment is added.
