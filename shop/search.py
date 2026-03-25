"""
Produktsuche auf gemuese-bestellen.de + Claude-Matching.

Ablauf:
  1. Cache prüfen (known_products, ≤7 Tage alt)
  2. Falls kein Cache: Playwright-Suche auf /search?search=<term>
  3. Bis zu 5 Kandidaten an Claude API → bestes Produkt auswählen
  4. Preisfilter gegen price_threshold Tabelle
  5. Ergebnis in Cache speichern
"""

import asyncio
import json
import logging
import os
import re
from datetime import date, timedelta

import anthropic

from db.schema import get_connection, get_price_threshold
from shop.browser import BASE_URL, BrowserSession

logger = logging.getLogger(__name__)

CACHE_TTL_DAYS = 7

_MATCH_SYSTEM_PROMPT = """\
Du bist ein Einkaufsassistent. Wähle aus den folgenden Produkten dasjenige aus,
das am besten zur gesuchten Zutat passt.

Antworte NUR mit einer der folgenden Optionen – kein weiterer Text:
- Eine Zahl (1-5): Produkt passt gut zur Zutat
- Eine Zahl gefolgt von "?" (z.B. "2?"): Produkt ist die nächste Option, passt aber nur ungefähr
  (z.B. anderer Verarbeitungsgrad, andere Form, andere Sorte als gewünscht)
- "0": Kein Produkt passt auch nur annähernd

Kriterien (in dieser Reihenfolge):
1. Passt die Zutat inhaltlich genau? (z.B. "Tomaten getrocknet" → kein frischer Tomate)
2. Form/Verarbeitung muss stimmen wenn spezifiziert (frisch, getrocknet, in Öl, etc.)
3. Kleinste passende Packungsgröße bevorzugen
4. Bei Zweifeln: das günstigste passende Produkt
"""


def _db_path() -> str:
    return os.getenv("DB_PATH", "boeck_agent.db")


# ---------------------------------------------------------------------------
# Öffentliche API
# ---------------------------------------------------------------------------

async def search_product(session: BrowserSession, ingredient: dict) -> list[dict]:
    """
    Sucht ein Produkt auf gemuese-bestellen.de.
    Progressiver Fallback: von spezifisch nach allgemein.
    Gibt bis zu 5 Kandidaten zurück.
    """
    name = ingredient["name"]
    attempts = _get_search_attempts(name)

    for attempt in attempts:
        # Cache prüfen
        cached = _get_from_cache(attempt)
        if cached:
            logger.debug("Cache-Hit für '%s'", attempt)
            return [cached]
        # Live-Suche
        candidates = await _search_live(session, attempt)
        if candidates:
            if attempt != name:
                logger.info("Treffer für '%s' (gesucht: '%s')", attempt, name)
            return candidates

    return []


def _get_search_attempts(name: str) -> list[str]:
    """
    Gibt Suchbegriffe in Reihenfolge von spezifisch → allgemein zurück.

    Beispiele:
    - "Tomaten, getrocknet, in Öl" → ["Tomaten getrocknet in Öl", "Tomaten getrocknet", "Tomaten"]
    - "Oregano, frisch"            → ["Oregano frisch", "Oregano"]
    - "Knoblauchzehen"             → ["Knoblauch"]
    - "rote Zwiebeln"              → ["Zwiebeln", "Zwiebel"]
    """
    seen: list[str] = []

    def add(term: str) -> None:
        t = term.strip()
        if t and t.lower() not in [s.lower() for s in seen]:
            seen.append(t)

    if "," in name:
        # Komma-getrennte Qualifier: progressiv von vollständig bis nur Hauptzutat
        # "Tomaten, getrocknet, in Öl" → ["Tomaten getrocknet in Öl", "Tomaten getrocknet", "Tomaten"]
        parts = [p.strip() for p in name.split(",") if p.strip()]
        for count in range(len(parts), 0, -1):
            add(" ".join(parts[:count]))
    else:
        parts = name.split()
        # Original zuerst (spezifisch → allgemein)
        add(name)
        if len(parts) > 1:
            # Erstes Wort als spezifischer Qualifier – nur wenn Substantiv (Großbuchstabe)
            # "Feta Käse" → "Feta";  "rote Zwiebeln" → überspringen
            if parts[0][0].isupper():
                add(parts[0])
            # Letztes Wort als Hauptnomen-Fallback
            # "rote Zwiebeln" → "Zwiebel";  "Feta Käse" → "Käse"
            last_simplified = _simplify_search_term(parts[-1])
            add(last_simplified or parts[-1])
        else:
            # Einwortig: Vereinfachung als Fallback
            # "Knoblauchzehen" → "Knoblauch"
            simplified = _simplify_search_term(name)
            if simplified:
                add(simplified)

    return seen


def _simplify_search_term(name: str) -> str | None:
    """Vereinfacht einen einwortigen oder mehrteiligen Begriff (ohne Kommas)."""
    parts = name.split()
    if len(parts) > 1:
        # Letztes Wort (Hauptnomen) ohne Plural-n: "rote Zwiebeln" → "Zwiebeln"
        last = parts[-1]
        singular = last.rstrip("n") if last.endswith("n") else last
        return singular if singular.lower() != name.lower() else None

    # Einwortiges Kompositum: Suffix entfernen
    _SUFFIXES = [
        "zehen", "scheiben", "blätter", "stücke", "würfel", "flocken",
        "schoten", "gurken", "streifen", "ringe", "hälften", "spalten",
    ]
    lower = name.lower()
    for suffix in _SUFFIXES:
        if lower.endswith(suffix) and len(name) > len(suffix) + 2:
            return name[: -len(suffix)]

    if lower.endswith("en") and len(name) > 4:
        return name[:-1]
    if lower.endswith("n") and len(name) > 3:
        return name[:-1]

    return None


async def match_best_product(
    ingredient: dict,
    candidates: list[dict],
    db_path: str | None = None,
    skip_price_check: bool = False,
) -> dict | None:
    """
    Lässt Claude den besten Treffer auswählen und wendet den Preisfilter an.
    skip_price_check=True: Preislimit ignorieren (z.B. bei explizitem /rein-Befehl).
    Gibt das beste Produkt zurück oder None wenn keines passt / zu teuer.
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        best, is_uncertain = candidates[0], False
    else:
        best, is_uncertain = await _claude_pick(ingredient, candidates)
        if best is None:
            return None

    # Preisfilter (überspringen wenn explizit vom User angefordert)
    if not skip_price_check:
        category = ingredient.get("category", "default")
        max_price_kg = get_price_threshold(db_path or _db_path(), category)
        price_per_kg = best.get("price_per_kg")
        if price_per_kg and price_per_kg > max_price_kg:
            logger.info(
                "'%s' zu teuer: %.2f €/kg > %.2f €/kg (Limit)",
                best["name"], price_per_kg, max_price_kg,
            )
            return {**best, "status": "too_expensive", "max_price_kg": max_price_kg}

    # Cache speichern (nur bei sicheren Matches)
    if not is_uncertain:
        _save_to_cache(ingredient["name"], best)

    status = "uncertain" if is_uncertain else "found"
    return {**best, "status": status}


# ---------------------------------------------------------------------------
# Einheiten-Normalisierung + Interims-Vorrat
# ---------------------------------------------------------------------------

# Einheits-Normalisierung: Alias → Basiseinheit
_UNIT_NORMALIZE: dict[str, str] = {
    # zählbar
    "stück": "stück", "stk": "stück", "st": "stück", "stücke": "stück",
    "stk.": "stück", "st.": "stück", "": "stück",
    # Gewicht → g
    "g": "g", "gr": "g", "gramm": "g",
    "kg": "g",          # × 1000
    "kilogramm": "g",
    # Volumen → ml
    "ml": "ml", "milliliter": "ml",
    "l": "ml",          # × 1000
    "liter": "ml",
    # Koch-Maße (keine Konvertierung)
    "el": "el", "esslöffel": "el",
    "tl": "tl", "teelöffel": "tl",
    # Sonstige
    "zehe": "zehe", "bund": "bund", "prise": "prise",
    "packung": "packung", "pack": "packung",
    "dose": "dose", "büchse": "dose",
}

_UNIT_MULTIPLIERS: dict[str, float] = {
    "kg": 1000.0, "kilogramm": 1000.0,
    "l": 1000.0, "liter": 1000.0,
}

# Zählbare Basiseinheiten: Mengen-Vergleich sinnvoll (1 Zucchini ≠ 2 Zucchini)
# Alles andere (g, ml, EL, TL, Prise …): Presence-Check (1 Flasche Öl reicht für alle EL-Angaben)
_COUNTABLE_BASES = {"stück", "zehe", "bund"}


def _is_countable(unit: str) -> bool:
    """True für Stück/Zehe/Bund – dort ist Mengen-Vergleich sinnvoll."""
    return _to_base(1.0, unit)[1] in _COUNTABLE_BASES


def _to_base(qty: float, unit: str) -> tuple[float, str]:
    """Normalisiert Menge + Einheit auf eine Basiseinheit (z.B. kg → g×1000)."""
    u = unit.lower().strip().rstrip(".")
    base = _UNIT_NORMALIZE.get(u, u)
    factor = _UNIT_MULTIPLIERS.get(u, 1.0)
    return qty * factor, base


def _build_interim_stock(
    ingredients: list[dict],
    inv_qtys: dict[str, float],
    cart_state: list[dict],
) -> dict[str, dict]:
    """
    Baut den Interims-Vorrat auf: Summe aus Inventar + Warenkorb.

    Rückgabe: name_lower → {qty, base_unit, from_inv, from_cart}
    """
    stock: dict[str, dict] = {}

    # Inventar-Anteile
    for ing in ingredients:
        name = ing["name"].lower()
        unit = ing.get("unit", "")
        raw_qty = inv_qtys.get(name, 0.0)
        base_qty, base_unit = _to_base(raw_qty, unit)
        if name not in stock:
            stock[name] = {"qty": 0.0, "base_unit": base_unit, "from_inv": 0.0, "from_cart": 0.0}
        stock[name]["qty"] += base_qty
        stock[name]["from_inv"] += base_qty

    # Warenkorb-Anteile (aus DB-State)
    for cs in cart_state:
        name = (cs.get("ingredient_name") or cs.get("product_name", "")).lower().strip()
        if not name:
            continue
        qty = float(cs.get("quantity") or 1)
        unit = cs.get("unit") or ""
        base_qty, base_unit = _to_base(qty, unit)

        if name in stock:
            if stock[name]["base_unit"] == base_unit:
                stock[name]["qty"] += base_qty
                stock[name]["from_cart"] += base_qty
            # Verschiedene Basiseinheiten: nicht addierbar, ignorieren
        else:
            stock[name] = {
                "qty": base_qty, "base_unit": base_unit,
                "from_inv": 0.0, "from_cart": base_qty,
            }

    return stock


def _parse_package_base_qty(product_unit: str) -> tuple[float | None, str | None]:
    """
    Parst eine Produkteinheit wie '180 g', '500g', '1 kg', '250 ml'
    und gibt (menge_in_basiseinheit, basiseinheit) zurück.
    """
    if not product_unit:
        return None, None
    m = re.search(r"(\d+(?:[,.]\d+)?)\s*(g|kg|ml|l)\b", product_unit, re.IGNORECASE)
    if not m:
        return None, None
    val = float(m.group(1).replace(",", "."))
    u = m.group(2).lower()
    if u == "kg":
        return val * 1000, "g"
    if u == "l":
        return val * 1000, "ml"
    return val, u  # g or ml unverändert


def _check_package_size_hint(
    needed_qty: float,
    ing_unit: str,
    product_unit: str,
    tolerance_low: float = 0.70,
) -> str | None:
    """
    Gibt einen Hinweis zurück, wenn die Packungsgröße kleiner als die benötigte Menge ist
    (aber mindestens tolerance_low davon, also z.B. ≥70%).
    """
    if not product_unit:
        return None
    pkg_base, pkg_bu = _parse_package_base_qty(product_unit)
    if pkg_base is None:
        return None
    needed_base, needed_bu = _to_base(needed_qty, ing_unit)
    if needed_bu != pkg_bu:
        return None
    if pkg_base >= needed_base * 0.99:  # Packung reicht aus (≥99%)
        return None
    if pkg_base < needed_base * tolerance_low:  # Zu klein – kein sinnvoller Hinweis
        return None
    # Packung ist 70–99% der benötigten Menge → Hinweis ausgeben
    def _fmt(qty: float, unit: str) -> str:
        return f"{int(round(qty))} {unit}"
    return (
        f"Hinweis: {_fmt(needed_base, needed_bu)} benötigt, "
        f"kleinste Packung hat {_fmt(pkg_base, pkg_bu)}"
    )


async def find_and_fill_cart(ingredients: list[dict]) -> dict:
    """
    Vollständige Pipeline: Interims-Vorratsliste aus Inventar + Warenkorb aufbauen,
    dann für jede Zutat die fehlende Restmenge bestellen.

    Logik:
      effective_available = inventory_qty + cart_state_qty
      to_order = max(0, recipe_needed - effective_available)
    """
    from inventory.manager import get_available_qtys  # noqa: PLC0415
    from inventory.manager import has_any_inventory  # noqa: PLC0415
    from shop.cart import fill_cart, get_cart_state_items, is_cart_empty, clear_cart_state  # noqa: PLC0415

    cart_state = get_cart_state_items()

    async with BrowserSession() as session:
        await session.login()

        # Auto-Erkennung: Warenkorb auf der Website leer obwohl DB-State gefüllt?
        # Nutzt explizite Leer-Erkennung (Text/CSS) statt fehlender Artikel-Selektoren.
        if cart_state:
            if await is_cart_empty(session):
                logger.info("Warenkorb ist leer – DB-Cart-State wird automatisch geleert")
                clear_cart_state()
                cart_state = []

        # Inventar-Mengen (löst auch Haltbarkeits-Decay aus)
        inv_qtys = get_available_qtys(ingredients)

        # Interims-Vorratsliste: Inventar + Warenkorb
        interim = _build_interim_stock(ingredients, inv_qtys, cart_state)

        cart_items: list[dict] = []
        not_found: list[dict] = []
        too_expensive: list[dict] = []
        already_in_cart: list[dict] = []
        from_inventory: list[dict] = []
        uncertain: list[dict] = []

        # Schnell-Lookup aus Cart-State für Presence-Checks
        cart_ingredient_names = {
            (cs.get("ingredient_name") or cs.get("product_name", "")).lower().strip()
            for cs in cart_state
            if cs.get("ingredient_name") or cs.get("product_name")
        }
        cart_product_urls = {cs["product_url"] for cs in cart_state if cs.get("product_url")}

        logger.info(
            "[DEBUG] cart_state (%d Einträge): %s",
            len(cart_state),
            [(cs.get("ingredient_name"), cs.get("quantity"), cs.get("unit")) for cs in cart_state],
        )
        logger.info("[DEBUG] interim_stock: %s", interim)

        # ── Phase 1: Entscheiden was bestellt werden muss ────────────────────
        search_queue = []  # (ingredient, search_ingredient, is_cnt, needed_raw)

        for ingredient in ingredients:
            name_lower = ingredient["name"].lower()
            needed_raw = float(ingredient.get("quantity") or 1)
            unit = ingredient.get("unit", "")
            is_cnt = _is_countable(unit)

            # Inventar-Anteil bestimmen (in Originaleinheit)
            inv_qty_orig = inv_qtys.get(name_lower, 0.0)
            if inv_qty_orig > 0:
                used_from_inv = min(inv_qty_orig, needed_raw)
                from_inventory.append({**ingredient, "quantity": used_from_inv})

            # ── Nicht-zählbar: g/ml → Mengenvergleich; Rest → Presence-Check ──
            if not is_cnt:
                _, needed_base_unit = _to_base(1.0, unit)
                if needed_base_unit in {"g", "ml"}:
                    # Gewicht/Volumen: wie zählbar – wie viel fehlt noch?
                    orig_factor = _UNIT_MULTIPLIERS.get(unit.lower().strip().rstrip("."), 1.0)
                    needed_base = needed_raw * orig_factor
                    stock = interim.get(name_lower, {})
                    available_base = (
                        stock.get("qty", 0.0)
                        if stock.get("base_unit") == needed_base_unit
                        else 0.0
                    )
                    remaining_base = max(0.0, needed_base - available_base)
                    logger.info(
                        "[DEBUG] '%s' g/ml: needed=%.0f%s avail=%.0f%s remain=%.0f%s cart_stock=%s",
                        ingredient["name"], needed_base, needed_base_unit,
                        available_base, needed_base_unit, remaining_base, needed_base_unit, stock,
                    )
                    if remaining_base <= 0:
                        logger.info("[DEBUG] '%s' → already_in_cart (g/ml gedeckt)", ingredient["name"])
                        already_in_cart.append({**ingredient})
                        continue
                    remaining_orig = remaining_base / orig_factor
                    if remaining_orig < needed_raw:
                        logger.info(
                            "'%s': %.2f %s gedeckt (Inv+WK), bestelle %.2f %s nach",
                            ingredient["name"], needed_raw - remaining_orig,
                            unit, remaining_orig, unit,
                        )
                    search_ingredient = {**ingredient, "quantity": remaining_orig}
                else:
                    # EL, TL, Prise, Packung …: Presence-Check reicht
                    # Vorrat einheitsunabhängig (z.B. "Salz" in g vs. Prise im Rezept)
                    if inv_qty_orig > 0 or has_any_inventory(name_lower):
                        continue
                    if name_lower in cart_ingredient_names:
                        already_in_cart.append({**ingredient})
                        continue
                    search_ingredient = ingredient

            # ── Zählbar (Stück, Zehe, Bund …): nur Inventar zählt ───────────
            # Cart-State wird bewusst NICHT angerechnet: jedes Rezept bekommt
            # eigene Stück-Artikel (Recipe 1 und 2 brauchen je 1 Zucchini → 2 bestellen).
            # Mengen-Deduplication innerhalb eines Rezepts übernimmt merge_ingredients.
            else:
                needed_qty, needed_base = _to_base(needed_raw, unit)
                orig_factor = _UNIT_MULTIPLIERS.get(unit.lower().strip().rstrip("."), 1.0)
                stock = interim.get(name_lower, {})
                if stock.get("base_unit", needed_base) == needed_base:
                    available_base = stock.get("from_inv", 0.0)  # nur Inventar, nicht Cart
                else:
                    available_base = 0.0
                remaining_base = max(0.0, needed_qty - available_base)

                if remaining_base <= 0:
                    if inv_qty_orig >= needed_raw:
                        pass  # vollständig aus Inventar
                    else:
                        already_in_cart.append({**ingredient})
                    continue

                remaining_orig = remaining_base / orig_factor
                if remaining_orig < needed_raw:
                    logger.info(
                        "'%s': %.2f %s gedeckt (Inv+WK), bestelle %.2f %s nach",
                        ingredient["name"], needed_raw - remaining_orig, unit, remaining_orig, unit,
                    )
                search_ingredient = {**ingredient, "quantity": remaining_orig}

            search_queue.append((ingredient, search_ingredient, is_cnt, needed_raw))

        # ── Phase 2: Browser-Suche (sequenziell – eine Session) ──────────────
        search_results = []
        for ingredient, search_ingredient, is_cnt, needed_raw in search_queue:
            candidates = await search_product(session, search_ingredient)
            search_results.append(candidates)

        # ── Phase 3: Claude-Matching parallel ────────────────────────────────
        match_tasks = [
            match_best_product(si, cands)
            for (_, si, _, _), cands in zip(search_queue, search_results)
        ]
        bests = await asyncio.gather(*match_tasks, return_exceptions=True)

        # ── Phase 4: Ergebnisse verarbeiten ──────────────────────────────────
        for (ingredient, search_ingredient, is_cnt, needed_raw), best in zip(search_queue, bests):
            if isinstance(best, Exception):
                logger.error("Matching-Fehler für '%s': %s", ingredient["name"], best)
                not_found.append(ingredient)
                continue

            if best is None:
                not_found.append(ingredient)
            elif best.get("status") == "too_expensive":
                too_expensive.append({**ingredient, **best})
            elif best.get("status") == "uncertain":
                logger.info("'%s' unsicherer Match → Rückfrage", best.get("name"))
                uncertain.append({**ingredient, **best, "ingredient_name": ingredient["name"]})
            else:
                # Für nicht-zählbar: Produkt-URL als Fallback-Presence-Check
                # NUR für Presence-Check-Einheiten (EL, TL, Prise …), NICHT für g/ml
                # (bei g/ml haben wir die Restmenge bereits berechnet)
                _, ing_base_unit = _to_base(1.0, ingredient.get("unit", ""))
                if not is_cnt and ing_base_unit not in {"g", "ml"} and best.get("url", "") in cart_product_urls:
                    already_in_cart.append({**ingredient, **best})
                    continue

                # Paketgröße-Hinweis: wenn Packung kleiner als benötigte Menge
                size_hint = _check_package_size_hint(
                    search_ingredient.get("quantity", needed_raw),
                    ingredient.get("unit", ""),
                    best.get("unit", ""),
                )

                # Echte Paketgröße bestimmen (für cart_state-Tracking)
                pkg_base, pkg_bu = _parse_package_base_qty(best.get("unit", ""))

                cart_items.append({
                    **ingredient, **best,
                    "quantity": search_ingredient.get("quantity", needed_raw),
                    "ingredient_name": ingredient["name"],
                    "ingredient_qty": search_ingredient.get("quantity", needed_raw),
                    "ingredient_unit": ingredient.get("unit", ""),
                    # Physische Paketgröße für cart_state (damit nächstes Rezept korrekt trackt)
                    "package_qty": pkg_base,
                    "package_base_unit": pkg_bu,
                    **({"size_hint": size_hint} if size_hint else {}),
                })

        # Warenkorb befüllen – nur tatsächlich hinzugefügte Artikel zählen
        cart_url = ""
        total = 0.0
        actually_added: list[dict] = []
        if cart_items:
            cart_url, total, actually_added = await fill_cart(session, cart_items)
        elif already_in_cart or cart_state:
            cart_url = f"{BASE_URL}/checkout/cart"

    return {
        "cart_items": actually_added,
        "not_found": not_found,
        "too_expensive": too_expensive,
        "already_in_cart": already_in_cart,
        "from_inventory": from_inventory,
        "uncertain": uncertain,
        "cart_url": cart_url,
        "total": total,
    }


# ---------------------------------------------------------------------------
# Playwright-Suche
# ---------------------------------------------------------------------------

async def _search_live(session: BrowserSession, search_term: str) -> list[dict]:
    """Sucht auf gemuese-bestellen.de und extrahiert bis zu 5 Produkt-Kandidaten."""
    search_url = f"{BASE_URL}/search?search={search_term}"
    try:
        await session.goto(search_url, timeout=15000)
        await session.page.wait_for_load_state("domcontentloaded", timeout=8000)
    except Exception as e:
        logger.error("Suche fehlgeschlagen für '%s': %s", search_term, e)
        return []

    boxes = await session.page.query_selector_all(".product-box")
    if not boxes:
        logger.info("Keine Treffer für '%s'", search_term)
        return []

    candidates = []
    for box in boxes[:5]:
        product = await _extract_product(box, session.page)
        if product:
            candidates.append(product)

    logger.info("'%s': %d Kandidaten gefunden", search_term, len(candidates))
    return candidates


async def _extract_product(box, page) -> dict | None:
    """Extrahiert Produktdaten aus einer .product-box."""
    try:
        # Name – Shopware 6: <a class="product-name">
        name_el = await box.query_selector("a.product-name, .product-name a, h2 a")
        if not name_el:
            return None
        name = (await name_el.inner_text()).strip()
        if not _is_valid_product_name(name):
            return None
        href = await name_el.get_attribute("href") or ""
        if not href.startswith("http"):
            href = BASE_URL + href

        # Preis (Gesamt)
        price = None
        price_el = await box.query_selector(".product-price-wrapper .product-price")
        if price_el:
            price_text = (await price_el.inner_text()).strip()
            price = _parse_price(price_text)

        # Preis pro kg
        price_per_kg = None
        ref_el = await box.query_selector(".price-unit-reference")
        if ref_el:
            ref_text = (await ref_el.inner_text()).strip()
            price_per_kg = _parse_price_per_kg(ref_text)

        # Packungseinheit
        unit = None
        unit_el = await box.query_selector(".price-unit-content")
        if unit_el:
            unit = (await unit_el.inner_text()).strip()

        # Produkt-ID aus Form-Hidden-Input
        product_id = None
        hidden = await box.query_selector("input[name*='lineItems'][name*='[id]']")
        if hidden:
            product_id = await hidden.get_attribute("value")

        if not product_id:
            # Fallback: aus URL extrahieren
            product_id_match = re.search(r"/([a-f0-9]{32})(?:/|$)", href)
            if product_id_match:
                product_id = product_id_match.group(1)

        return {
            "name": name,
            "url": href,
            "price": price,
            "price_per_kg": price_per_kg,
            "unit": unit,
            "product_id": product_id,
        }
    except Exception as e:
        logger.warning("Fehler beim Extrahieren eines Produkts: %s", e)
        return None


def _parse_price(text: str) -> float | None:
    """Extrahiert einen Preis aus '1,35 €*' → 1.35"""
    m = re.search(r"(\d+)[,.](\d+)", text)
    if m:
        return float(f"{m.group(1)}.{m.group(2)}")
    return None


def _parse_price_per_kg(text: str) -> float | None:
    """Extrahiert Preis/kg aus '(2,70 € / 1 Kilogramm)' → 2.70"""
    m = re.search(r"(\d+)[,.](\d+)\s*€\s*/\s*1\s*Kilogramm", text)
    if m:
        return float(f"{m.group(1)}.{m.group(2)}")
    # Versuch mit anderem Format
    m = re.search(r"(\d+)[,.](\d+)", text)
    if m:
        return float(f"{m.group(1)}.{m.group(2)}")
    return None


# ---------------------------------------------------------------------------
# Claude-Matching
# ---------------------------------------------------------------------------

async def _claude_pick(ingredient: dict, candidates: list[dict]) -> tuple[dict | None, bool]:
    """
    Claude wählt den besten Kandidaten aus.
    Gibt (produkt, is_uncertain) zurück.
    is_uncertain=True wenn Claude "N?" antwortet (passt nur ungefähr).
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return candidates[0], False  # Fallback: erster Treffer

    lines = [f"Gesuchte Zutat: {ingredient['name']} ({ingredient.get('quantity', '')} {ingredient.get('unit', '')})"]
    lines.append("\nKandidaten:")
    for i, c in enumerate(candidates, 1):
        price_info = f"{c['name']} – {c['price']} €" if c.get("price") else c['name']
        per_kg = f" ({c['price_per_kg']} €/kg)" if c.get("price_per_kg") else ""
        lines.append(f"{i}. {price_info}{per_kg}")

    client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=3)
    try:
        message = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=10,
            system=_MATCH_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": "\n".join(lines)}],
        )
        raw = message.content[0].text.strip()
        is_uncertain = raw.endswith("?")
        idx = int(re.sub(r"[^0-9]", "", raw))
        if 1 <= idx <= len(candidates):
            return candidates[idx - 1], is_uncertain
        return None, False
    except (ValueError, IndexError, anthropic.APIError) as e:
        logger.warning("Claude-Matching fehlgeschlagen: %s – nehme ersten Treffer", e)
        return (candidates[0], False) if candidates else (None, False)


# ---------------------------------------------------------------------------
# Cache (known_products)
# ---------------------------------------------------------------------------

def _get_from_cache(search_term: str) -> dict | None:
    cutoff = (date.today() - timedelta(days=CACHE_TTL_DAYS)).isoformat()
    conn = get_connection(_db_path())
    try:
        row = conn.execute(
            "SELECT * FROM known_products WHERE lower(search_term) = lower(?) "
            "AND last_checked >= ? AND available = 1",
            (search_term, cutoff),
        ).fetchone()
        if row:
            d = dict(row)
            d.setdefault("name", d.get("product_name", ""))
            d.setdefault("url", d.get("product_url", ""))
            # Ungültige Cache-Einträge ignorieren
            name = d.get("name", "").strip()
            if not name or name in ("-", "–", "?", ""):
                return None
            return d
        return None
    finally:
        conn.close()


def _is_valid_product_name(name: str) -> bool:
    """Prüft ob ein Produktname sinnvoll ist (kein Platzhalter, kein Leerzeichen)."""
    stripped = name.strip()
    return bool(stripped) and stripped not in ("-", "–", "—", "?", "N/A")


def _save_to_cache(search_term: str, product: dict) -> None:
    if not _is_valid_product_name(product.get("name", "")):
        return
    today = date.today().isoformat()
    conn = get_connection(_db_path())
    try:
        with conn:
            conn.execute(
                "INSERT INTO known_products "
                "(search_term, product_name, product_url, price, price_per_kg, unit, available, last_checked) "
                "VALUES (?, ?, ?, ?, ?, ?, 1, ?) "
                "ON CONFLICT DO NOTHING",
                (
                    search_term,
                    product.get("name", ""),
                    product.get("url", ""),
                    product.get("price") or 0.0,
                    product.get("price_per_kg"),
                    product.get("unit"),
                    today,
                ),
            )
    except Exception as e:
        logger.warning("Cache-Speicherung fehlgeschlagen: %s", e)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Standalone-Test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import asyncio, sys
    from dotenv import load_dotenv
    load_dotenv()

    term = sys.argv[1] if len(sys.argv) > 1 else "Karotten"

    async def test():
        print(f"Suche: '{term}'")
        async with BrowserSession() as session:
            candidates = await search_product(session, {"name": term, "quantity": 500, "unit": "g"})
            print(f"{len(candidates)} Kandidaten gefunden:")
            for i, c in enumerate(candidates, 1):
                print(f"  {i}. {c['name']} – {c.get('price')} € ({c.get('price_per_kg')} €/kg)")
                print(f"     URL: {c.get('url', '')[:60]}")

            if candidates:
                best = await match_best_product(
                    {"name": term, "quantity": 500, "unit": "g", "category": "default"},
                    candidates,
                )
                print(f"\nBestes Produkt: {best['name'] if best else 'keins'}")
                if best:
                    print(f"  Preis: {best.get('price')} € | Status: {best.get('status')}")

    asyncio.run(test())
