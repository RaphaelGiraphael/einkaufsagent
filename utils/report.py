"""
Telegram-Report-Formatter und Bestellhistorie.

Formatiert das Ergebnis der Pipeline als Telegram Markdown-Nachricht
und speichert die Bestellung in der Datenbank.
"""

import json
import logging
import os
from datetime import datetime

from db.schema import get_connection

logger = logging.getLogger(__name__)


def format_report(
    cart_items: list[dict],
    not_found: list[dict],
    too_expensive: list[dict],
    from_inventory: list[dict],
    cart_url: str,
    total: float,
    recipe_names: list[str] | None = None,
    already_in_cart: list[dict] | None = None,
    price_warnings: dict[str, dict] | None = None,
) -> str:
    """
    Erstellt den Telegram-Report im vorgegebenen Format.

    Returns: Formatierter Markdown-Text für Telegram.
    """
    lines = []

    # Warenkorb-Zusammenfassung
    if cart_items:
        total_str = f"~{total:.2f} €".replace(".", ",") if total else ""
        lines.append(f"✅ *{len(cart_items)} neue Artikel im Warenkorb*{' (' + total_str + ')' if total_str else ''}")
        for item in cart_items:
            name = item.get("name", item.get("ingredient_name", "?"))
            price = item.get("price")
            price_str = f" – {str(price).replace('.', ',')} €" if price else ""
            lines.append(f"  · {name}{price_str}")
    else:
        lines.append("⚠️ *Kein Artikel konnte zum Warenkorb hinzugefügt werden*")

    # Nicht bestellbar
    if not_found:
        lines.append("")
        lines.append(f"❌ *Nicht bestellbar ({len(not_found)}):*")
        for item in not_found:
            name = item.get("name", item.get("ingredient_name", "?"))
            lines.append(f"  · {name}")

    # Zu teuer
    if too_expensive:
        lines.append("")
        lines.append(f"💸 *Zu teuer übersprungen ({len(too_expensive)}):*")
        for item in too_expensive:
            name = item.get("name", item.get("ingredient_name", "?"))
            price_per_kg = item.get("price_per_kg")
            max_price_kg = item.get("max_price_kg")
            detail = ""
            if price_per_kg and max_price_kg:
                detail = f" – {str(price_per_kg).replace('.', ',')} €/kg > Limit {str(max_price_kg).replace('.', ',')} €/kg"
            lines.append(f"  · {name}{detail}")

    # Bereits im Warenkorb
    if already_in_cart:
        lines.append("")
        lines.append(f"🔁 *Bereits im Warenkorb ({len(already_in_cart)}):*")
        for item in already_in_cart:
            name = item.get("name", item.get("ingredient_name", "?"))
            lines.append(f"  · {name}")

    # Aus Vorrat
    if from_inventory:
        lines.append("")
        lines.append(f"🧺 *Aus Vorrat entnommen ({len(from_inventory)}):*")
        for item in from_inventory:
            name = item.get("name", item.get("ingredient_name", "?"))
            qty = item.get("quantity", "")
            unit = item.get("unit", "")
            qty_str = f" ({qty} {unit})" if qty else ""
            lines.append(f"  · {name}{qty_str}")

    # Preisvergleich-Warnungen
    if price_warnings:
        lines.append("")
        lines.append("📊 *Preisvergleich:*")
        for product_name, w in price_warnings.items():
            boeck = w["boeck_price_per_kg"]
            ref = w["ref_price_per_kg"]
            diff = w["diff_pct"] * 100
            ref_product = w.get("ref_product", "")
            source = w.get("source", "Supermarkt")
            ref_str = f" _{ref_product}_" if ref_product and ref_product.lower() != product_name.lower() else ""
            lines.append(
                f"  · *{product_name}* {boeck:.2f} €/kg "
                f"vs. {ref:.2f} €/kg bei {source}{ref_str} "
                f"(+{diff:.0f}% teurer)"
            )

    # Warenkorb-Link
    if cart_url:
        lines.append("")
        lines.append(f"🔗 [Warenkorb prüfen]({cart_url})")

    return "\n".join(lines)


def save_order(
    cart_items: list[dict],
    not_found: list[dict],
    too_expensive: list[dict],
    from_inventory: list[dict],
    cart_url: str,
    total: float,
    recipe_names: list[str] | None = None,
    db_path: str | None = None,
) -> int:
    """
    Speichert die Bestellung in orders + order_items.
    Gibt die order_id zurück.
    """
    db = db_path or os.getenv("DB_PATH", "boeck_agent.db")
    conn = get_connection(db)

    try:
        with conn:
            # Bestellung anlegen
            status = "completed" if cart_items else ("partial" if not_found else "failed")
            cursor = conn.execute(
                "INSERT INTO orders (recipes, cart_total, cart_url, status) VALUES (?, ?, ?, ?)",
                (
                    json.dumps(recipe_names or [], ensure_ascii=False),
                    total or None,
                    cart_url or None,
                    status,
                ),
            )
            order_id = cursor.lastrowid

            # Bestellte Artikel
            for item in cart_items:
                conn.execute(
                    "INSERT INTO order_items (order_id, ingredient_name, product_name, product_url, "
                    "price, price_per_kg, quantity, unit, status) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        order_id,
                        item.get("ingredient_name") or item.get("name", ""),
                        item.get("name", ""),
                        item.get("url") or item.get("product_url"),
                        item.get("price"),
                        item.get("price_per_kg"),
                        item.get("quantity"),
                        item.get("unit"),
                        "ordered",
                    ),
                )

            # Nicht gefunden
            for item in not_found:
                conn.execute(
                    "INSERT INTO order_items (order_id, ingredient_name, status) VALUES (?,?,?)",
                    (order_id, item.get("name", ""), "not_found"),
                )

            # Zu teuer
            for item in too_expensive:
                conn.execute(
                    "INSERT INTO order_items (order_id, ingredient_name, product_name, "
                    "price, price_per_kg, status, notes) VALUES (?,?,?,?,?,?,?)",
                    (
                        order_id,
                        item.get("ingredient_name") or item.get("name", ""),
                        item.get("name", ""),
                        item.get("price"),
                        item.get("price_per_kg"),
                        "too_expensive",
                        f"Limit: {item.get('max_price_kg')} €/kg",
                    ),
                )

            # Aus Vorrat
            for item in from_inventory:
                conn.execute(
                    "INSERT INTO order_items (order_id, ingredient_name, quantity, unit, status) "
                    "VALUES (?,?,?,?,?)",
                    (
                        order_id,
                        item.get("name", ""),
                        item.get("quantity"),
                        item.get("unit"),
                        "from_inventory",
                    ),
                )

        logger.info("Bestellung #%d gespeichert (Status: %s)", order_id, status)
        return order_id

    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    from dotenv import load_dotenv
    load_dotenv()
    from db.schema import init_db
    init_db(os.getenv("DB_PATH", "boeck_agent.db"))

    # Test-Report
    report = format_report(
        cart_items=[
            {"name": "Karotten", "price": 1.35, "quantity": 500, "unit": "g"},
            {"name": "Tomaten", "price": 2.10, "quantity": 400, "unit": "g"},
        ],
        not_found=[
            {"name": "Schwarzwurzeln"},
        ],
        too_expensive=[
            {"name": "Trüffel", "price_per_kg": 450.0, "max_price_kg": 30.0},
        ],
        from_inventory=[
            {"name": "Olivenöl", "quantity": 50, "unit": "ml"},
        ],
        cart_url="https://gemuese-bestellen.de/checkout/cart",
        total=3.45,
        recipe_names=["Tomatensalat"],
    )
    print("=== Report-Vorschau ===")
    print(report)

    # In DB speichern
    order_id = save_order(
        cart_items=[{"name": "Karotten", "price": 1.35}],
        not_found=[{"name": "Schwarzwurzeln"}],
        too_expensive=[],
        from_inventory=[],
        cart_url="https://gemuese-bestellen.de/checkout/cart",
        total=1.35,
        recipe_names=["Tomatensalat"],
    )
    print(f"\nBestellung #{order_id} gespeichert.")
