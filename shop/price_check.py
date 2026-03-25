"""
Externer Preisvergleich via Claude Web Search.

Claude sucht den Referenzpreis für ein Produkt bei deutschen Supermärkten
(Rewe, Edeka, Kaufland, Aldi, Lidl) und vergleicht mit dem Böck-Preis.
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


async def _fetch_via_claude_search(product_name: str) -> dict | None:
    """
    Nutzt Claude Web Search um Referenzpreise zu finden.
    Läuft über Anthropics Server → kein IP-Block durch Supermärkte.
    Sucht zuerst nach gleicher Marke, dann nach qualitativ ähnlichem Produkt.
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
                    f"Suche den aktuellen Preis pro kg für '{product_name}' bei einem deutschen "
                    f"Supermarkt (Rewe, Edeka, Kaufland, Aldi oder Lidl).\n\n"
                    f"Vorgehen:\n"
                    f"1. Suche zuerst nach exakt diesem Produkt inkl. Marke (falls Marke enthalten)\n"
                    f"2. Falls nicht gefunden: suche nach dem qualitativ ähnlichsten Produkt "
                    f"(Bio↔Bio, gleicher Fettgehalt, gleiche Herkunft wenn relevant)\n"
                    f"3. Vergleiche NICHT konventionell mit Bio oder umgekehrt\n\n"
                    f"Antworte in genau diesem Format:\n"
                    f"PREIS: <zahl in Euro pro kg>\n"
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
                return None

            # Matches: "PREIS: 6.54", "PREIS: **6,54**", "PREIS: 6,54€/kg"
            price_m = _re.search(r'PREIS:\s*\*{0,2}(\d+)[,\.](\d+)', raw)
            product_m = _re.search(r'PRODUKT:\s*(.+)', raw)
            market_m = _re.search(r'MARKT:\s*(.+)', raw)

            if price_m:
                val = float(f"{price_m.group(1)}.{price_m.group(2)}")
                if 0.30 < val < 800:
                    found_product = product_m.group(1).strip() if product_m else product_name
                    found_market = market_m.group(1).strip() if market_m else "Supermarkt"
                    logger.info(
                        "Preisvergleich '%s': %.2f €/kg bei %s ('%s')",
                        product_name, val, found_market, found_product,
                    )
                    return {
                        "price_per_kg": val,
                        "name": found_product,
                        "source": found_market,
                    }

        return None
    except Exception as e:
        logger.debug("Claude-Search Fehler für '%s': %s", product_name, e)
        return None


async def get_reference_price(product_name: str) -> dict | None:
    """
    Gibt Referenzpreis (€/kg) aus dem Web zurück (14 Tage gecacht).
    None = kein Preis verfügbar.
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
    threshold: float = THRESHOLD,
) -> dict | None:
    """
    Vergleicht Böck-Preis/kg mit Markt-Referenzpreis.
    Gibt Warning-Dict zurück wenn Böck > threshold% teurer, sonst None.
    """
    if not boeck_price_per_kg or boeck_price_per_kg <= 0:
        return None

    ref = await get_reference_price(product_name)
    if not ref or not ref.get("price_per_kg"):
        return None

    ref_price = ref["price_per_kg"]
    if ref_price <= 0:
        return None

    diff_pct = (boeck_price_per_kg - ref_price) / ref_price
    if diff_pct > threshold:
        return {
            "boeck_price_per_kg": boeck_price_per_kg,
            "ref_price_per_kg": ref_price,
            "ref_product": ref["name"],
            "source": ref["source"],
            "diff_pct": diff_pct,
        }
    return None
