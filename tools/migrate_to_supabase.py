"""
Copy everything from a local SQLite leads.db into the Postgres database in DATABASE_URL (Supabase).

    python tools/migrate_to_supabase.py              # uses ./leads.db and DATABASE_URL from .env
    python tools/migrate_to_supabase.py old.db       # another SQLite file
    python tools/migrate_to_supabase.py --force      # overwrite even if Postgres already has customers

Keeps every id (so topics, saved replies and chains stay linked) and resets the id counters.
"""
import asyncio
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("RUN_BOT", "0")

import bot as B  # noqa: E402  (loads .env and DATABASE_URL)

TABLES = ["users", "messages", "media_vault", "bot_messages", "media", "bot_settings", "auto_log", "scheduled_sends"]


async def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    force = "--force" in sys.argv
    src = args[0] if args else os.path.join(ROOT, "leads.db")
    if not B.USE_PG:
        sys.exit("DATABASE_URL is not set (add it to .env). Nothing to migrate to.")
    if not os.path.exists(src):
        sys.exit(f"SQLite file not found: {src}")

    await B.init_db()  # creates the tables in Postgres
    pool = await B._pg()
    existing = await pool.fetchval("SELECT COUNT(*) FROM users")
    if existing and not force:
        sys.exit(f"Postgres already has {existing} customers. Re-run with --force to overwrite them.")

    lite = sqlite3.connect(src)
    lite.row_factory = sqlite3.Row
    lite_tables = {r[0] for r in lite.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    async with pool.acquire() as conn:
        async with conn.transaction():
            for table in TABLES:
                if table not in lite_tables:
                    print(f"  {table:16} skipped (not in {os.path.basename(src)})")
                    continue
                pg_cols = {r["column_name"]: r["data_type"] for r in await conn.fetch(
                    "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = $1", table)}
                rows = [dict(r) for r in lite.execute(f"SELECT * FROM {table}")]
                cols = [c for c in (rows[0].keys() if rows else []) if c in pg_cols]
                await conn.execute(f"DELETE FROM {table}")
                for r in rows:
                    values = []
                    for c in cols:
                        v = r[c]
                        if v is not None and pg_cols[c] == "bigint":
                            v = int(v)
                        elif v is not None and pg_cols[c] == "text" and not isinstance(v, str):
                            v = str(v)
                        values.append(v)
                    placeholders = ", ".join(f"${i + 1}" for i in range(len(cols)))
                    await conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})", *values)
                if "id" in pg_cols:
                    await conn.execute(
                        f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                        f"COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)")
                print(f"  {table:16} {len(rows)} rows copied")
    await pool.close()
    print("Done. Your CRM now shows this data.")


if __name__ == "__main__":
    asyncio.run(main())
