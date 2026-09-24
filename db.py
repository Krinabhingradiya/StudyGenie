import os
import sqlite3

def get_db_connection():
    db_url = os.environ.get("DATABASE_URL")

    # 1. Production (Render) with Neon / Supabase PostgreSQL
    if db_url:
        import psycopg2
        import psycopg2.extras

        # Render uri standard 'postgres://' deta hai jise psycopg2 'postgresql://' mangta hai
        if db_url.startswith("postgres://"):
            db_url = db_url.replace("postgres://", "postgresql://", 1)

        conn = psycopg2.connect(db_url)
        return conn

    # 2. Local Machine Testing (Fallback)
    else:
        conn = sqlite3.connect("user.db")
        conn.row_factory = sqlite3.Row
        return conn
