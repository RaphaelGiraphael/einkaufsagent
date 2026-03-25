"""
Externer Preisvergleich via Claude Web Search.

Claude sucht den Referenzpreis für ein Produkt bei deutschen Supermärkten
(Rewe, Edeka, Kaufland, Aldi, Lidl) und vergleicht mit dem Böck-Preis.
Preiseinheit wird automatisch gewählt (€/kg, €/Stück, €/Liter).
Ergebnisse werden 14 Tage in-memory gecacht.
"""

import logging
import os
import re as _re
import time

import anthropic as _anthropic

logger = logging.getLogger(__name__)

THRESHOLD = 0.10  # Böck muss mehr als 10% teurer sein um zu warnen

_CACHE: dict[str, tuple[float, dict | None]] = {}  # term → (timestamp, result)
_CACHE_TTL = 1_209_600.0  # 14 Tage

# Normalisierung von Böck-Einheiten auf Claude-Einheiten
_STUECK_UNITS = {"stück", "st", "stk", "stke", "stücke", "stk.", "st."}
_LITER_UNITS = {"l", "liter"}
_ML_UNITS = {"ml"}


def _boeck_price_for_unit(
    ref_unit: str,
    boeck_price_per_kg: float | None,
    boeck_item: dict | None,
) -> float | None:
    """Gibt den Böck-Preis in der vom Referenz angegebenen Einheit zurück."""
    if ref_unit == "kg":
        return boeck_price_per_kg
    if not boeck_item:
        return None
    price = boeck_item.get("price")
    qty = boeck_item.get("quantity") or 1
    # "10 Stück" → "stück", "6 St." → "st"
    raw_unit = _re.sub(r'^\d+\s*', '', (boeck_item.get("unit") or "").lower()).strip(".")
    if not price or not qty:
        return None
    if ref_unit == "Stück" and raw_unit in _STUECK_UNITS:
        return price / qty
    if ref_unit == "Liter":
        if raw_unit in _LITER_UNITS:
            return price / qty
        if raw_unit in _ML_UNITS:
            return price / (qty / 1000)
    return None


async def _fetch_via_claude_search(product_name: str) -> dict | None:
    """
    Nutzt Claude Web Search um Referenzpreise zu finden.
    Läuft über Anthropics Server → kein IP-Block durch Supermärkte.
    Sucht zuerst nach gleicher Marke, dann nach qualitativ ähnlichem Produkt.
    Wählt die sinnvollste Preiseinheit (kg / Stück / Liter).
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        client = _anthropic.AsyncAnthropic(api_key=api_key)
        message = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 2,
            }],
            messages=[{
                "role": "user",
                "content": (
                    f"Suche den aktuellen Preis für '{product_name}' bei einem deutschen "
                    f"Supermarkt (Rewe, Edeka, Kaufland, Aldi oder Lidl).\n\n"
                    f"Vorgehen:\n"
                    f"1. Enthält der Produktname eine Marke (z.B. 'Andechser', 'Berchtesgadener Land', "
                    f"'Demeter', 'Bioland')? → Suche NUR nach dieser Marke. "
                    f"Ist die Marke in deutschen Supermärkten nicht erhältlich: 'KEIN TREFFER'.\n"
                    f"2. Kein Markenname erkennbar → suche nach qualitativ ähnlichstem Produkt "
                    f"(Bio↔Bio, gleicher Fettgehalt, gleiche Herkunft wenn relevant)\n"
                    f"3. Vergleiche NICHT konventionell mit Bio oder umgekehrt\n\n"
                    f"Wähle die sinnvollste Preiseinheit:\n"
                    f"- Eier, Einzelstücke → Stück\n"
                    f"- Flüssigkeiten (Öl, Milch, Saft) → Liter\n"
                    f"- Alles andere (Gemüse, Käse, Fleisch) → kg\n\n"
                    f"Antworte in genau diesem Format:\n"
                    f"PREIS: <zahl>\n"
                    f"EINHEIT: <kg|Stück|Liter>\n"
                    f"PRODUKT: <exakter Produktname den du gefunden hast>\n"
                    f"MARKT: <Marktname>\n"
                    f"Falls kein vergleichbares Produkt gefunden: nur 'KEIN TREFFER'"
                ),
            }],
        )

        for block in message.content:
            if not hasattr(block, "text"):
                continue
            raw = block.text.strip()
            if "KEIN TREFFER" in raw:
                logger.debug("Kein Treffer für '%s'", product_name)
                return None

            # Matches: "PREIS: 6.54", "PREIS: **6,54**", "PREIS: 0,39"
            price_m = _re.search(r'PREIS:\s*\*{0,2}(\d+)[,\.](\d+)', raw)
            unit_m = _re.search(r'EINHEIT:\s*\*{0,2}(\w+)', raw)
            product_m = _re.search(r'PRODUKT:\s*(.+)', raw)
            market_m = _re.search(r'MARKT:\s*(.+)', raw)

            if price_m:
                val = float(f"{price_m.group(1)}.{price_m.group(2)}")
                if 0.01 < val < 5000:
                    unit = unit_m.group(1).strip() if unit_m else "kg"
                    # Normalisiere Einheit
                    unit_lower = unit.lower()
                    if unit_lower in ("stück", "stuck", "stk", "stücke", "stueck"):
                        unit = "Stück"
                    elif unit_lower in ("liter", "l"):
                        unit = "Liter"
                    else:
                        unit = "kg"
                    found_product = product_m.group(1).strip() if product_m else product_name
                    found_market = market_m.group(1).strip() if market_m else "Supermarkt"
                    logger.info(
                        "Preisvergleich '%s': %.2f €/%s bei %s ('%s')",
                        product_name, val, unit, found_market, found_product,
                    )
                    return {
                        "price": val,
                        "unit": unit,
                        "name": found_product,
                        "source": found_market,
                    }

        return None
    except Exception as e:
        logger.debug("Claude-Search Fehler für '%s': %s", product_name, e)
        return None


async def get_reference_price(product_name: str) -> dict | None:
    """
    Gibt Referenzpreis aus dem Web zurück (14 Tage gecacht).
    Dict mit price, unit, name, source. None = kein Preis verfügbar.
    """
    key = product_name.lower().strip()
    cached = _CACHE.get(key)
    if cached and (time.monotonic() - cached[0]) < _CACHE_TTL:
        return cached[1]

    result = await _fetch_via_claude_search(product_name)
    _CACHE[key] = (time.monotonic(), result)
    return result


async def check_price_markup(
    product_name: str,
    boeck_price_per_kg: float | None,
    boeck_item: dict | None = None,
    threshold: float = THRESHOLD,
) -> dict | None:
    """
    Vergleicht Böck-Preis mit Markt-Referenzpreis in der passenden Einheit.
    Gibt Warning-Dict zurück wenn Böck > threshold% teurer, sonst None.
    boeck_item: vollständiges Item-Dict mit price, quantity, unit (für Stück/Liter).
    """
    ref = await get_reference_price(product_name)
    if not ref or not ref.get("price"):
        return None

    ref_price = ref["price"]
    ref_unit = ref.get("unit", "kg")
    if ref_price <= 0:
        return None

    boeck_price = _boeck_price_for_unit(ref_unit, boeck_price_per_kg, boeck_item)
    if not boeck_price or boeck_price <= 0:
        return None

    diff_pct = (boeck_price - ref_price) / ref_price
    if diff_pct > threshold:
        return {
            "boeck_price": boeck_price,
            "ref_price": ref_price,
            "unit": ref_unit,
            "ref_product": ref["name"],
            "source": ref["source"],
            "diff_pct": diff_pct,
        }
    return None
