"""
Datenbankschema für den Böck Einkaufsagenten.
Initialisiert alle Tabellen und stellt Verbindungen bereit.
"""

import sqlite3
import os
from datetime import date


def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str) -> None:
    conn = get_connection(db_path)
    try:
        with conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS inventory (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    name         TEXT    NOT NULL,
                    category     TEXT    NOT NULL,
                    quantity     REAL    NOT NULL,
                    unit         TEXT    NOT NULL,
                    shelf_life   TEXT    NOT NULL CHECK (shelf_life IN ('lang', 'mittel', 'kurz')),
                    added_date   DATE    NOT NULL,
                    expires_at   DATE,
                    notes        TEXT
                );

                CREATE TABLE IF NOT EXISTS orders (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_date   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    recipes      TEXT     NOT NULL,
                    cart_total   REAL,
                    cart_url     TEXT,
                    status       TEXT     NOT NULL CHECK (status IN ('completed', 'partial', 'failed'))
                );

                CREATE TABLE IF NOT EXISTS order_items (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id        INTEGER NOT NULL REFERENCES orders(id),
                    ingredient_name TEXT    NOT NULL,
                    product_name    TEXT,
                    product_url     TEXT,
                    price           REAL,
                    price_per_kg    REAL,
                    quantity        REAL,
                    unit            TEXT,
                    status          TEXT    NOT NULL CHECK (status IN (
                                        'ordered', 'not_found', 'too_expensive',
                                        'from_inventory', 'substituted'
                                    )),
                    notes           TEXT
                );

                CREATE TABLE IF NOT EXISTS price_threshold (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    category      TEXT     NOT NULL UNIQUE,
                    max_price_kg  REAL     NOT NULL,
                    updated_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS known_products (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    search_term   TEXT    NOT NULL,
                    product_name  TEXT    NOT NULL,
                    product_url   TEXT    NOT NULL,
                    price         REAL    NOT NULL,
                    price_per_kg  REAL,
                    unit          TEXT,
                    available     BOOLEAN NOT NULL DEFAULT 1,
                    last_checked  DATE    NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cart_state (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_name    TEXT    NOT NULL,
                    product_url     TEXT,
                    ingredient_name TEXT,
                    quantity        REAL    NOT NULL DEFAULT 1,
                    unit            TEXT    NOT NULL DEFAULT 'Stk',
                    added_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    cleared         INTEGER  NOT NULL DEFAULT 0
                );
            """)

            # Migration: Neue Spalten für cart_state (für ältere DB-Versionen)
            for col, definition in [
                ("quantity", "REAL NOT NULL DEFAULT 1"),
                ("unit", "TEXT NOT NULL DEFAULT 'Stk'"),
                ("order_id", "INTEGER"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE cart_state ADD COLUMN {col} {definition}")
                except Exception:
                    pass  # Spalte existiert bereits

            # Standard-Preisschwellen eintragen (nur wenn noch nicht vorhanden)
            defaults = [
                ("default",    30.0),
                ("Kräuter",    80.0),
                ("Pilze",      50.0),
                ("Gewürze",   300.0),
                ("Getrocknet", 120.0),
            ]
            for category, max_price_kg in defaults:
                conn.execute(
                    "INSERT OR IGNORE INTO price_threshold (category, max_price_kg) VALUES (?, ?)",
                    (category, max_price_kg),
                )
    finally:
        conn.close()


def get_price_threshold(db_path: str, category: str) -> float:
    """Gibt den max. Preis/kg für eine Kategorie zurück (Fallback: 'default')."""
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT max_price_kg FROM price_threshold WHERE category = ?", (category,)
        ).fetchone()
        if row:
            return row["max_price_kg"]
        row = conn.execute(
            "SELECT max_price_kg FROM price_threshold WHERE category = 'default'"
        ).fetchone()
        return row["max_price_kg"] if row else 30.0
    finally:
        conn.close()


if __name__ == "__main__":
    db_path = os.getenv("DB_PATH", "boeck_agent.db")
    init_db(db_path)

    conn = get_connection(db_path)
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    conn.close()

    table_names = ", ".join(t["name"] for t in tables)
    print(f"DB initialisiert: {db_path}")
    print(f"Tabellen: {table_names}")

    # Preisschwellen anzeigen
    conn = get_connection(db_path)
    thresholds = conn.execute("SELECT category, max_price_kg FROM price_threshold").fetchall()
    conn.close()
    print("\nPreisschwellen:")
    for t in thresholds:
        print(f"  {t['category']}: {t['max_price_kg']} €/kg")
