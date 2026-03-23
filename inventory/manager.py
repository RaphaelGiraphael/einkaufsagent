"""
Vorratsmanager: Lesen, Abziehen und Aktualisieren des Inventars (SQLite).

Haltbarkeits-Logik (nach jeder Bestellung):
  kurz  (≤3 Tage)  → 0%   verbleibend
  mittel (≤7 Tage) → 30%  verbleibend
  lang  (≤60 Tage) → 80%  verbleibend
"""

import logging
import os
import time
from datetime import date, timedelta

from db.schema import get_connection

logger = logging.getLogger(__name__)

# Haltbarkeits-Schwellen in Tagen
_SHELF_LIFE_DAYS = {
    "kurz":   3,
    "mittel": 7,
    "lang":   60,
}

# Verbleibender Anteil nach Bestellung
_SHELF_LIFE_REMAINING = {
    "kurz":   0.0,
    "mittel": 0.3,
    "lang":   0.8,
}

_UNIT_CONVERSIONS: dict[tuple[str, str], float] = {
    # (von, nach): Faktor
    ("kg", "g"):   1000.0,
    ("g",  "kg"):  0.001,
    ("l",  "ml"):  1000.0,
    ("ml", "l"):   0.001,
}


def _db_path() -> str:
    return os.getenv("DB_PATH", "boeck_agent.db")


# ---------------------------------------------------------------------------
# Vorrat prüfen
# ---------------------------------------------------------------------------

async def check_inventory(
    ingredients: list[dict],
) -> tuple[list[dict], list[dict]]:
    """
    Prüft welche Zutaten aus dem Vorrat genommen werden können.

    Gibt zurück: (zu_bestellen, aus_vorrat)
    - zu_bestellen: Zutaten die fehlen oder nicht ausreichen
    - aus_vorrat: Zutaten die vollständig aus dem Vorrat kommen
    """
    apply_shelf_life_decay()

    conn = get_connection(_db_path())
    to_order: list[dict] = []
    from_inventory: list[dict] = []

    try:
        for ingredient in ingredients:
            name = ingredient["name"]
            needed_qty = float(ingredient.get("quantity", 1))
            needed_unit = ingredient.get("unit", "Stück")

            # Vorrat nach Name suchen (case-insensitive)
            rows = conn.execute(
                "SELECT * FROM inventory WHERE lower(name) = lower(?) AND quantity > 0",
                (name,),
            ).fetchall()

            available_qty = _sum_available(rows, needed_unit)

            if available_qty >= needed_qty:
                from_inventory.append({**ingredient, "source": "inventory"})
            elif available_qty > 0:
                # Teilweise aus Vorrat
                remaining = needed_qty - available_qty
                from_inventory.append({
                    **ingredient,
                    "quantity": available_qty,
                    "source": "inventory_partial",
                })
                to_order.append({**ingredient, "quantity": remaining})
            else:
                to_order.append(ingredient)
    finally:
        conn.close()

    return to_order, from_inventory


def _sum_available(rows, target_unit: str) -> float:
    """Summiert verfügbare Mengen, konvertiert bei Bedarf."""
    total = 0.0
    for row in rows:
        qty = row["quantity"]
        unit = row["unit"]
        if unit == target_unit:
            total += qty
        else:
            factor = _UNIT_CONVERSIONS.get((unit, target_unit))
            if factor:
                total += qty * factor
            # Unbekannte Einheit: ignorieren (kein sinnvoller Vergleich)
    return total


# ---------------------------------------------------------------------------
# Vorrat abziehen
# ---------------------------------------------------------------------------

def deduct_inventory(ingredients: list[dict]) -> None:
    """
    Zieht verbrauchte Mengen vom Vorrat ab.
    Einträge die auf 0 oder darunter fallen werden auf 0 gesetzt.
    """
    conn = get_connection(_db_path())
    try:
        with conn:
            for ingredient in ingredients:
                name = ingredient["name"]
                needed_qty = float(ingredient.get("quantity", 1))
                needed_unit = ingredient.get("unit", "Stück")

                rows = conn.execute(
                    "SELECT * FROM inventory WHERE lower(name) = lower(?) AND quantity > 0 "
                    "ORDER BY expires_at ASC NULLS LAST",
                    (name,),
                ).fetchall()

                for row in rows:
                    if needed_qty <= 0:
                        break
                    avail = row["quantity"]
                    unit = row["unit"]

                    # In gleiche Einheit konvertieren
                    if unit != needed_unit:
                        factor = _UNIT_CONVERSIONS.get((needed_unit, unit))
                        if factor:
                            needed_in_row_unit = needed_qty * factor
                        else:
                            continue
                    else:
                        needed_in_row_unit = needed_qty

                    if avail >= needed_in_row_unit:
                        new_qty = avail - needed_in_row_unit
                        needed_qty = 0
                    else:
                        new_qty = 0
                        consumed_in_orig = avail / (_UNIT_CONVERSIONS.get((unit, needed_unit)) or 1.0)
                        needed_qty -= consumed_in_orig if unit != needed_unit else avail

                    conn.execute(
                        "UPDATE inventory SET quantity = ? WHERE id = ?",
                        (max(0.0, new_qty), row["id"]),
                    )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Haltbarkeits-Decay
# ---------------------------------------------------------------------------

_DECAY_LAST_RUN: float = 0.0
_DECAY_INTERVAL: float = 60.0  # Maximal einmal pro Minute ausführen


def apply_shelf_life_decay() -> None:
    """
    Schreibt alte Vorräte ab basierend auf ihrer Haltbarkeitskategorie.
    Wird automatisch vor jeder Vorratsprüfung aufgerufen (max. 1x/Minute).
    """
    global _DECAY_LAST_RUN
    now = time.monotonic()
    if now - _DECAY_LAST_RUN < _DECAY_INTERVAL:
        return
    _DECAY_LAST_RUN = now

    today = date.today()
    conn = get_connection(_db_path())
    updated = 0
    try:
        with conn:
            rows = conn.execute(
                "SELECT id, shelf_life, added_date, quantity FROM inventory WHERE quantity > 0"
            ).fetchall()

            for row in rows:
                shelf_life = row["shelf_life"]
                max_days = _SHELF_LIFE_DAYS.get(shelf_life)
                if max_days is None:
                    continue

                added = date.fromisoformat(row["added_date"])
                age_days = (today - added).days

                if age_days >= max_days:
                    remaining_ratio = _SHELF_LIFE_REMAINING[shelf_life]
                    new_qty = row["quantity"] * remaining_ratio
                    conn.execute(
                        "UPDATE inventory SET quantity = ? WHERE id = ?",
                        (new_qty, row["id"]),
                    )
                    updated += 1

    finally:
        conn.close()

    if updated:
        logger.info("Haltbarkeits-Decay: %d Einträge aktualisiert", updated)


# ---------------------------------------------------------------------------
# Vorrat hinzufügen
# ---------------------------------------------------------------------------

def add_to_inventory(items: list[dict], exact: bool = False) -> None:
    """
    Fügt neue Produkte zum Vorrat hinzu.
    exact=True: Menge unverändert speichern (manuelle Eingabe via /vorrat).
    exact=False (Standard): Anfangs-Haltbarkeits-Ratio anwenden (nach Bestellung).
    """
    today = date.today()
    conn = get_connection(_db_path())
    try:
        with conn:
            for item in items:
                shelf_life = item.get("shelf_life", "mittel")
                initial_ratio = 1.0 if exact else _SHELF_LIFE_REMAINING.get(shelf_life, 0.3)
                quantity = float(item.get("quantity", 1)) * initial_ratio
                expires_offset = _SHELF_LIFE_DAYS.get(shelf_life)
                expires_at = (today + timedelta(days=expires_offset)).isoformat() if expires_offset else None

                unit = item.get("unit", "Stück")

                if exact:
                    # Manuelle Eingabe: alle vorhandenen Einträge für diesen Namen
                    # löschen und durch einen einzigen neuen ersetzen (aggregiert)
                    conn.execute(
                        "DELETE FROM inventory WHERE lower(name) = lower(?)", (item["name"],)
                    )
                    conn.execute(
                        "INSERT INTO inventory (name, category, quantity, unit, shelf_life, "
                        "added_date, expires_at, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            item["name"],
                            item.get("category", "Sonstiges"),
                            quantity,
                            unit,
                            shelf_life,
                            today.isoformat(),
                            expires_at,
                            item.get("notes"),
                        ),
                    )
                else:
                    # Nach Bestellung: zu vorhandenem Eintrag gleicher Einheit addieren
                    existing = conn.execute(
                        "SELECT id, quantity FROM inventory WHERE lower(name) = lower(?) AND unit = ?",
                        (item["name"], unit),
                    ).fetchone()
                    if existing:
                        conn.execute(
                            "UPDATE inventory SET quantity = ?, added_date = ?, expires_at = ?, notes = ? "
                            "WHERE id = ?",
                            (existing["quantity"] + quantity, today.isoformat(), expires_at,
                             item.get("notes"), existing["id"]),
                        )
                    else:
                        conn.execute(
                            "INSERT INTO inventory (name, category, quantity, unit, shelf_life, "
                            "added_date, expires_at, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                item["name"],
                                item.get("category", "Sonstiges"),
                                quantity,
                                unit,
                                shelf_life,
                                today.isoformat(),
                                expires_at,
                                item.get("notes"),
                            ),
                        )
    finally:
        conn.close()


def get_available_qtys(ingredients: list[dict]) -> dict[str, float]:
    """
    Gibt für jede Zutat die verfügbare Menge im Vorrat zurück.
    Schlüssel: ingredient["name"].lower().
    Menge: in der Einheit der jeweiligen Zutat (konvertiert falls nötig).
    Löst auch den Haltbarkeits-Decay aus.
    """
    apply_shelf_life_decay()
    result: dict[str, float] = {}
    conn = get_connection(_db_path())
    try:
        for ing in ingredients:
            name = ing["name"]
            unit = ing.get("unit", "Stück")
            rows = conn.execute(
                "SELECT quantity, unit FROM inventory "
                "WHERE lower(name) = lower(?) AND quantity > 0",
                (name,),
            ).fetchall()
            result[name.lower()] = _sum_available(rows, unit)
    finally:
        conn.close()
    return result


def list_inventory() -> list[dict]:
    """Gibt alle Vorrats-Einträge mit Menge > 0 zurück."""
    conn = get_connection(_db_path())
    try:
        rows = conn.execute(
            "SELECT * FROM inventory WHERE quantity > 0 ORDER BY category, name"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_inventory_qty(item_id: int, delta: float) -> tuple[str, float, str] | None:
    """
    Ändert die Menge eines Inventar-Eintrags um delta.
    Gibt (name, new_qty, unit) zurück, oder None wenn nicht gefunden.
    """
    conn = get_connection(_db_path())
    try:
        row = conn.execute("SELECT * FROM inventory WHERE id = ?", (item_id,)).fetchone()
        if not row:
            return None
        new_qty = max(0.0, row["quantity"] + delta)
        with conn:
            conn.execute("UPDATE inventory SET quantity = ? WHERE id = ?", (new_qty, item_id))
        return row["name"], new_qty, row["unit"]
    finally:
        conn.close()


def set_inventory_qty(item_id: int, new_qty: float) -> tuple[str, float, str] | None:
    """
    Setzt die Menge eines Inventar-Eintrags direkt.
    Gibt (name, new_qty, unit) zurück, oder None wenn nicht gefunden.
    """
    conn = get_connection(_db_path())
    try:
        row = conn.execute("SELECT * FROM inventory WHERE id = ?", (item_id,)).fetchone()
        if not row:
            return None
        new_qty = max(0.0, new_qty)
        with conn:
            conn.execute("UPDATE inventory SET quantity = ? WHERE id = ?", (new_qty, item_id))
        return row["name"], new_qty, row["unit"]
    finally:
        conn.close()


def delete_inventory_item(item_id: int) -> str | None:
    """Löscht einen Inventar-Eintrag nach ID. Gibt den Namen zurück."""
    conn = get_connection(_db_path())
    try:
        row = conn.execute("SELECT name FROM inventory WHERE id = ?", (item_id,)).fetchone()
        if not row:
            return None
        with conn:
            conn.execute("DELETE FROM inventory WHERE id = ?", (item_id,))
        return row["name"]
    finally:
        conn.close()


def delete_inventory_by_name(name: str) -> int:
    """Löscht alle Inventar-Einträge mit dem angegebenen Namen. Gibt Anzahl zurück."""
    conn = get_connection(_db_path())
    try:
        with conn:
            cur = conn.execute(
                "DELETE FROM inventory WHERE lower(name) = lower(?)", (name,)
            )
            return cur.rowcount
    finally:
        conn.close()


def get_missing_items() -> list[dict]:
    """
    Gibt Zutaten zurück, die in den Bestellungen des aktuellen Warenkorbs
    nicht gefunden oder als zu teuer markiert wurden.
    Verknüpfung über cart_state.order_id (nur nicht-geleerte Einträge).
    """
    conn = get_connection(_db_path())
    try:
        # order_ids aller Rezepte im aktuellen Warenkorb-Kontext
        order_id_rows = conn.execute(
            "SELECT DISTINCT order_id FROM cart_state WHERE cleared = 0 AND order_id IS NOT NULL"
        ).fetchall()
        order_ids = [r["order_id"] for r in order_id_rows]
        if not order_ids:
            return []

        placeholders = ",".join("?" * len(order_ids))
        rows = conn.execute(
            f"""
            SELECT oi.ingredient_name, oi.status, oi.notes, oi.price_per_kg
            FROM order_items oi
            WHERE oi.order_id IN ({placeholders})
              AND oi.status IN ('not_found', 'too_expensive')
            ORDER BY oi.status, oi.ingredient_name
            """,
            order_ids,
        ).fetchall()

        seen: set[str] = set()
        result = []
        for row in rows:
            key = row["ingredient_name"].lower()
            if key not in seen:
                seen.add(key)
                result.append(dict(row))
        return result
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Standalone-Test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import asyncio
    from dotenv import load_dotenv
    load_dotenv()

    from db.schema import init_db
    init_db(_db_path())

    print("=== Test 1: Vorrat anlegen ===")
    add_to_inventory([
        {"name": "Olivenöl",  "category": "Öle",    "quantity": 1000, "unit": "ml", "shelf_life": "lang"},
        {"name": "Tomaten",   "category": "Gemüse",  "quantity": 500,  "unit": "g",  "shelf_life": "kurz"},
        {"name": "Karotten",  "category": "Gemüse",  "quantity": 1000, "unit": "g",  "shelf_life": "mittel"},
    ])
    inv = list_inventory()
    print(f"Vorrat ({len(inv)} Einträge):")
    for i in inv:
        print(f"  {i['name']}: {i['quantity']:.0f} {i['unit']} ({i['shelf_life']})")

    print("\n=== Test 2: Vorrat prüfen ===")
    ingredients = [
        {"name": "Olivenöl", "quantity": 50,  "unit": "ml"},   # vorhanden
        {"name": "Tomaten",  "quantity": 800, "unit": "g"},     # nicht genug (kurz=0%)
        {"name": "Nudeln",   "quantity": 500, "unit": "g"},     # fehlt komplett
    ]
    to_order, from_inv = asyncio.run(check_inventory(ingredients))
    print(f"Aus Vorrat ({len(from_inv)}): {[i['name'] for i in from_inv]}")
    print(f"Zu bestellen ({len(to_order)}): {[i['name'] for i in to_order]}")

    print("\n=== Test 3: Abziehen ===")
    deduct_inventory([{"name": "Karotten", "quantity": 200, "unit": "g"}])
    inv_after = list_inventory()
    karotten = next((i for i in inv_after if i["name"] == "Karotten"), None)
    print(f"Karotten nach Abzug von 200g: {karotten['quantity']:.0f}g" if karotten else "nicht gefunden")
