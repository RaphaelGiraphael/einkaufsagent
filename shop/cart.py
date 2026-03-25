"""
Warenkorb-Befüllung für gemuese-bestellen.de.

SICHERHEIT:
- Playwright-Session endet nach dem letzten add-to-cart Aufruf
- Checkout-URLs sind in browser.py hart blockiert
- Diese Funktion gibt NUR die Warenkorb-URL zurück, navigiert nie zum Checkout
"""

import logging
import os

from db.schema import get_connection
from shop.browser import BASE_URL, BrowserSession, _check_url

logger = logging.getLogger(__name__)


def _db_path() -> str:
    return os.getenv("DB_PATH", "boeck_agent.db")


async def fill_cart(session: BrowserSession, items: list[dict]) -> tuple[str, float]:
    """
    Fügt alle Produkte zum Warenkorb hinzu.

    Args:
        session: Eingeloggter BrowserSession (muss bereits eingeloggt sein)
        items: Liste von Produkten mit product_id oder url

    Returns:
        (cart_url, total): URL zum Warenkorb und Gesamtsumme
    """
    added = 0
    total = 0.0
    successfully_added = []

    for item in items:
        success = await _add_item_to_cart(session, item)
        if success:
            added += 1
            if item.get("price"):
                total += float(item["price"])
            successfully_added.append(item)
            logger.info("Zum Warenkorb hinzugefügt: %s", item.get("name", "?"))
        else:
            logger.warning("Konnte nicht zum Warenkorb hinzufügen: %s", item.get("name", "?"))

    logger.info("%d von %d Produkten zum Warenkorb hinzugefügt", added, len(items))

    # In DB-Cart-State speichern (für Duplikat-Erkennung bei nächstem Aufruf)
    _save_to_cart_state(successfully_added)

    # Warenkorb-URL zurückgeben – KEINE weiteren Aktionen
    cart_url = f"{BASE_URL}/checkout/cart"
    return cart_url, total, successfully_added


async def _add_item_to_cart(session: BrowserSession, item: dict) -> bool:
    """
    Fügt ein einzelnes Produkt zum Warenkorb hinzu.

    Strategie:
    1. Produkt-URL aufrufen
    2. Add-to-Cart Form finden und per JS POST abschicken
    """
    product_url = item.get("url") or item.get("product_url")
    product_id = item.get("product_id")

    if not product_url and not product_id:
        logger.warning("Kein URL oder Produkt-ID für '%s'", item.get("name", "?"))
        return False

    # Anzahl Pakete: nur für zählbare Einheiten (Stück, Zehe, Bund …)
    _COUNTABLE_UNITS = {"stück", "stk", "st", "stücke", "zehe", "bund", ""}
    ing_unit = (item.get("ingredient_unit") or "").lower().strip().rstrip(".")
    if ing_unit in _COUNTABLE_UNITS:
        n_packages = max(1, int(float(item.get("quantity") or 1)))
    else:
        n_packages = 1  # g/ml/EL/TL: immer 1 Paket

    try:
        # Zur Produktseite navigieren (URL-Check läuft in goto())
        if product_url:
            await session.goto(product_url, timeout=15000)
            await session.page.wait_for_load_state("domcontentloaded")

        # Strategie 1: Add-to-Cart Form per JS direkt POSTen
        # (robuster als Button-Klick, funktioniert auch ohne sichtbaren Button)
        form = await session.page.query_selector("form[action='/checkout/line-item/add']")

        if form:
            # Produkt-ID aus hidden input lesen falls noch nicht bekannt
            if not product_id:
                hidden = await form.query_selector("input[name*='[id]']")
                if hidden:
                    product_id = await hidden.get_attribute("value")

            # Menge ins Form-Input setzen (Shopware 6: lineItems[id][quantity])
            qty_input = await form.query_selector("input[name*='[quantity]']")
            if qty_input and n_packages > 1:
                await session.page.evaluate(
                    "(el, q) => { el.value = q; }", qty_input, n_packages
                )

            # Form absenden – bleibt auf Produktseite oder zeigt off-canvas Cart
            await session.page.evaluate("form => form.submit()", form)
            await session.page.wait_for_load_state("domcontentloaded", timeout=8000)

            # Prüfen ob wir noch auf einer erlaubten Seite sind
            current_url = session.page.url
            _check_url(current_url)  # Sicherheitscheck
            logger.debug("Nach add-to-cart URL: %s (qty=%d)", current_url, n_packages)
            return True

        # Strategie 2: .btn-buy klicken (Fallback)
        btn = await session.page.query_selector(".btn-buy, button.btn-buy, [data-add-to-cart]")
        if btn:
            await btn.click()
            try:
                await session.page.wait_for_load_state("domcontentloaded", timeout=6000)
            except Exception:
                pass  # Timeout OK – Off-Canvas öffnet sich ohne Navigation
            _check_url(session.page.url)
            return True

        logger.warning("Kein Add-to-Cart Button auf '%s' gefunden", product_url)
        return False

    except Exception as e:
        logger.error("Fehler beim Hinzufügen von '%s': %s", item.get("name", "?"), e)
        return False


# ---------------------------------------------------------------------------
# DB-Cart-State (zuverlässige Duplikat-Erkennung ohne DOM-Abhängigkeit)
# ---------------------------------------------------------------------------

def _save_to_cart_state(items: list[dict]) -> None:
    """Speichert erfolgreich hinzugefügte Produkte in cart_state."""
    if not items:
        return
    conn = get_connection(_db_path())
    try:
        with conn:
            for item in items:
                # Echte Paketgröße speichern wenn bekannt (g/ml),
                # sonst Rezept-Menge als Fallback.
                # Paketgröße: physische Menge im Warenkorb – sorgt dafür dass
                # nächstes Rezept korrekt "wie viel fehlt noch" berechnet.
                pkg_qty = item.get("package_qty")
                pkg_bu = item.get("package_base_unit")
                if pkg_qty and pkg_bu:
                    stored_qty = pkg_qty
                    stored_unit = pkg_bu
                else:
                    stored_qty = item.get("ingredient_qty") or item.get("quantity") or 1
                    stored_unit = item.get("ingredient_unit") or item.get("unit") or "Stk"
                conn.execute(
                    "INSERT INTO cart_state "
                    "(product_name, product_url, ingredient_name, quantity, unit) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        item.get("name", ""),
                        item.get("url", ""),
                        item.get("ingredient_name") or item.get("name", ""),
                        stored_qty,
                        stored_unit,
                    ),
                )
    except Exception as e:
        logger.warning("cart_state Speicherung fehlgeschlagen: %s", e)
    finally:
        conn.close()


def get_cart_state_items() -> list[dict]:
    """Gibt alle nicht-geleerten cart_state-Einträge zurück."""
    conn = get_connection(_db_path())
    try:
        rows = conn.execute(
            "SELECT product_name, product_url, ingredient_name, quantity, unit "
            "FROM cart_state WHERE cleared = 0"
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


def link_cart_state_to_order(order_id: int) -> None:
    """Verknüpft aktuelle cart_state-Einträge (ohne order_id) mit einer Bestellung."""
    conn = get_connection(_db_path())
    try:
        with conn:
            conn.execute(
                "UPDATE cart_state SET order_id = ? WHERE cleared = 0 AND order_id IS NULL",
                (order_id,),
            )
    except Exception as e:
        logger.warning("link_cart_state_to_order fehlgeschlagen: %s", e)
    finally:
        conn.close()


def clear_ingredient_from_cart_state(ingredient_name: str) -> int:
    """Markiert alle cart_state-Einträge für eine bestimmte Zutat als geleert."""
    conn = get_connection(_db_path())
    try:
        with conn:
            cur = conn.execute(
                "UPDATE cart_state SET cleared = 1 WHERE cleared = 0 "
                "AND (lower(ingredient_name) = lower(?) "
                "     OR lower(product_name) LIKE lower(?))",
                (ingredient_name, f"%{ingredient_name}%"),
            )
            return cur.rowcount
    except Exception as e:
        logger.warning("clear_ingredient_from_cart_state fehlgeschlagen: %s", e)
        return 0
    finally:
        conn.close()


def clear_cart_state() -> int:
    """Markiert alle cart_state-Einträge als geleert. Gibt Anzahl zurück."""
    conn = get_connection(_db_path())
    try:
        with conn:
            cur = conn.execute("UPDATE cart_state SET cleared = 1 WHERE cleared = 0")
            return cur.rowcount
    except Exception as e:
        logger.warning("clear_cart_state fehlgeschlagen: %s", e)
        return 0
    finally:
        conn.close()


async def is_cart_empty(session: BrowserSession) -> bool:
    """
    Prüft zuverlässig ob der Warenkorb leer ist – sucht explizit nach dem
    Leer-Indikator, nicht nach fehlenden Artikel-Zeilen.
    Gibt True zurück wenn der Warenkorb definitiv leer ist,
    False wenn Artikel vorhanden ODER Prüfung nicht eindeutig.
    """
    cart_url = f"{BASE_URL}/checkout/cart"
    try:
        await session.goto(cart_url, timeout=15000)
        await session.page.wait_for_load_state("domcontentloaded")
        empty = await session.page.evaluate("""
            () => {
                // Explizite Leer-Indikatoren (Shopware 6 + gängige Themes)
                const selectors = [
                    '.cart-empty', '.is-empty', '[data-empty-cart]',
                    '.checkout-aside-empty', '.cart-is-empty',
                ];
                for (const sel of selectors) {
                    if (document.querySelector(sel)) return true;
                }
                // Text-Fallback: "leer" im Warenkorb-Bereich
                const main = document.querySelector('main, .content-main, #content') || document.body;
                const text = main.innerText.toLowerCase();
                return text.includes('warenkorb ist leer')
                    || text.includes('cart is empty')
                    || text.includes('your cart is empty');
            }
        """)
        return bool(empty)
    except Exception as e:
        logger.debug("is_cart_empty Prüfung fehlgeschlagen: %s", e)
        return False  # Im Zweifel: nicht löschen


async def get_cart_contents(session: BrowserSession) -> dict:
    """
    Liest den aktuellen Warenkorb per JavaScript aus (theme-unabhängig).
    Gibt items (name, product_url) und Gesamtsumme zurück.
    """
    cart_url = f"{BASE_URL}/checkout/cart"
    try:
        await session.goto(cart_url, timeout=15000)
        await session.page.wait_for_load_state("domcontentloaded")

        items = await session.page.evaluate("""
            () => {
                const result = [];
                // Shopware 6: Selektoren in Prioritätsreihenfolge
                const ROW_SELECTORS = [
                    '[data-line-item-id]',
                    '.cart-item-row', '.line-item-row',
                    '.cart-item:not(.cart-item-header)',
                    '.line-item:not(.line-item-header)',
                ];
                let rows = [];
                for (const sel of ROW_SELECTORS) {
                    rows = Array.from(document.querySelectorAll(sel));
                    if (rows.length) break;
                }
                for (const row of rows) {
                    let name = '';
                    for (const sel of [
                        '.line-item-label a', 'a.line-item-label',
                        '.line-item-title', '.cart-item-label',
                        '.line-item-label', 'h4', 'h3'
                    ]) {
                        const el = row.querySelector(sel);
                        if (el && el.textContent.trim()) {
                            name = el.textContent.trim(); break;
                        }
                    }
                    const link = row.querySelector('a[href*=".html"]');
                    const productUrl = link ? link.href : '';
                    if (name) result.push({ name, productUrl });
                }
                return result;
            }
        """)

        total_el = await session.page.query_selector(
            ".cart-footer-total, .summary-total, [class*=grand-total], [class*=cart-total]"
        )
        total_text = (await total_el.inner_text()).strip() if total_el else ""

        logger.info("Warenkorb: %d Artikel gefunden", len(items))
        return {"items": items, "total_text": total_text, "cart_url": cart_url}

    except Exception as e:
        logger.error("Fehler beim Lesen des Warenkorbs: %s", e)
        return {"items": [], "total_text": "", "cart_url": cart_url}


async def remove_cart_item(session: BrowserSession, item_name: str) -> str | None:
    """
    Entfernt einen Artikel aus dem Warenkorb per JS-DOM-Traversal.
    Findet den Delete-Button ohne feste Selektoren (theme-unabhängig).
    Gibt den tatsächlichen Produktnamen zurück oder None wenn nicht gefunden.
    """
    await session.goto(f"{BASE_URL}/checkout/cart", timeout=15000)
    try:
        await session.page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        await session.page.wait_for_load_state("domcontentloaded")

    name_lower = item_name.lower()

    # JavaScript: Menge -1 wenn qty > 1, sonst komplett löschen
    result = await session.page.evaluate(
        """
        (searchTerm) => {
            const lower = searchTerm.toLowerCase();
            const DELETE_SELECTORS = [
                'button[title*="sch"]', 'button[title*="ov"]', 'button[title*="emov"]',
                'button[title*="elet"]', 'button.btn-danger', '.btn-delete',
                'form[action*="line-item/delete"] button',
                '[data-line-item-delete]', 'a[href*="line-item/delete"]',
            ];

            const leaves = Array.from(document.querySelectorAll('*')).filter(
                el => el.children.length === 0 &&
                      el.textContent.toLowerCase().includes(lower) &&
                      el.textContent.trim().length < 200
            );

            for (const leaf of leaves) {
                let el = leaf.parentElement;
                for (let i = 0; i < 12; i++) {
                    if (!el || el === document.body) break;

                    const nameEl = el.querySelector(
                        'a[href*=".html"], .line-item-label, .line-item-title, h3, h4'
                    );
                    const productName = nameEl
                        ? nameEl.textContent.trim()
                        : leaf.textContent.trim();

                    // Mengen-Input vorhanden? → nur um 1 reduzieren
                    const qtyInput = el.querySelector(
                        'input[name*="quantity"], input[type="number"][min]'
                    );
                    if (qtyInput) {
                        const currentQty = parseInt(qtyInput.value) || 1;
                        if (currentQty > 1) {
                            qtyInput.value = currentQty - 1;
                            // Mengen-Update-Form absenden
                            const form = qtyInput.closest('form');
                            if (form) {
                                form.submit();
                                return { success: true, productName, action: 'decreased', newQty: currentQty - 1 };
                            }
                            // Fallback: Update-Button
                            const upBtn = el.querySelector('[data-update-cart], .btn-update, button[type="submit"]');
                            if (upBtn) {
                                upBtn.click();
                                return { success: true, productName, action: 'decreased', newQty: currentQty - 1 };
                            }
                        }
                        // qty == 1 → löschen (unten)
                    }

                    // Delete-Button suchen
                    for (const sel of DELETE_SELECTORS) {
                        const del = el.querySelector(sel);
                        if (del) {
                            const form = del.closest('form[action*="line-item/delete"]');
                            if (form) { form.submit(); }
                            else { del.click(); }
                            return { success: true, productName, action: 'deleted' };
                        }
                    }

                    el = el.parentElement;
                }
            }
            return { success: false, matchCount: leaves.length };
        }
        """,
        name_lower,
    )

    if not result.get("success"):
        logger.warning(
            "'/raus %s': nicht gefunden. Text-Matches: %d",
            item_name, result.get("matchCount", 0),
        )
        return None

    product_name = result.get("productName", item_name)
    action = result.get("action", "deleted")
    new_qty = result.get("newQty")
    try:
        await session.page.wait_for_load_state("domcontentloaded", timeout=8000)
    except Exception:
        pass
    if action == "decreased":
        logger.info("'%s' Menge auf %d reduziert", product_name, new_qty)
    else:
        logger.info("'%s' aus Warenkorb entfernt", product_name)
    # Rückgabe als Tuple: (product_name, action, new_qty)
    return product_name, action, new_qty


# ---------------------------------------------------------------------------
# Standalone-Test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import asyncio
    from dotenv import load_dotenv
    load_dotenv()

    from db.schema import init_db
    init_db(os.getenv("DB_PATH", "boeck_agent.db"))

    # Test-Produkt: Karotten (product_id aus vorheriger Erkundung)
    test_items = [
        {
            "name": "Karotten",
            "url": "https://gemuese-bestellen.de/gemuese/wurzelgemuese/karotten.html",
            "product_id": "e320ee72b986422c8f1ab1fd6f985fa1",
            "price": 1.35,
        }
    ]

    async def test():
        print("=== Test: Warenkorb befüllen ===")
        print(f"Produkt: {test_items[0]['name']}")

        async with BrowserSession(headless=True) as session:
            logged_in = await session.login()
            print(f"Login: {'OK' if logged_in else 'FEHLER (keine Credentials?)'}")

            cart_url, total = await fill_cart(session, test_items)
            print(f"Warenkorb-URL: {cart_url}")
            print(f"Gesamtpreis (geschätzt): {total:.2f} EUR")

            if logged_in:
                contents = await get_cart_contents(session)
                print(f"\nWarenkorb-Inhalt ({len(contents['items'])} Artikel):")
                for item in contents["items"]:
                    print(f"  - {item['name']}: {item['price_text']}")
                print(f"Summe: {contents['total_text']}")

        print("\nSession beendet. Kein Checkout wurde durchgeführt.")

    asyncio.run(test())
