"""
Externer Preisvergleich: Prüft ob Böck-Preise mehr als X% über Rewe-Preisen liegen.

Strategie:
  1. Rewe-Shop-API (Haupt-Quelle)
  2. Rewe-Mobile-API (Fallback)
  Scheitern beide → keine Warnung (Feature ist optional)

Ergebnisse werden 24h in-memory gecacht.
"""

import logging
import time

import httpx

logger = logging.getLogger(__name__)

THRESHOLD = 0.10  # Böck muss mehr als 10% teurer sein um zu warnen

_CACHE: dict[str, tuple[float, dict | None]] = {}  # term → (timestamp, result)
_CACHE_TTL = 1_209_600.0  # 14 Tage

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "de-DE,de;q=0.9",
}


import re as _re


async def _fetch_via_duckduckgo(product_name: str) -> dict | None:
    """
    Sucht Preis/kg über DuckDuckGo HTML-Suche.
    Parst €/kg-Angaben aus den Suchergebnissen (Rewe, Edeka, etc.).
    DuckDuckGo blockt keine Server-IPs.
    """
    try:
        query = f"{product_name} Preis €/kg Rewe OR Edeka OR Kaufland"
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers={**_HEADERS, "Accept": "text/html"},
            )
        if resp.status_code != 200:
            logger.debug("DuckDuckGo Status %d für '%s'", resp.status_code, product_name)
            return None

        text = resp.text

        # €/kg-Pattern aus Suchergebnissen extrahieren
        # Matches: "1,99 €/kg", "2.49€/kg", "(3,50 € / kg)"
        patterns = [
            r'(\d+)[,\.](\d+)\s*€\s*/\s*(?:1\s*)?kg',
            r'(\d+)[,\.](\d+)\s*Euro\s*/\s*kg',
        ]
        prices = []
        for pattern in patterns:
            for m in _re.finditer(pattern, text, _re.IGNORECASE):
                try:
                    val = float(f"{m.group(1)}.{m.group(2)}")
                    if 0.30 < val < 800:
                        prices.append(val)
                except (ValueError, IndexError):
                    pass

        if not prices:
            logger.debug("DuckDuckGo: kein €/kg-Preis für '%s'", product_name)
            return None

        # Medianen Preis nehmen (robuster gegen Ausreißer)
        prices.sort()
        median = prices[len(prices) // 2]
        logger.info("DuckDuckGo Preis/kg für '%s': %.2f €/kg (%d Treffer)", product_name, median, len(prices))
        return {"price_per_kg": median, "name": product_name, "source": "Marktvergleich"}

    except Exception as e:
        logger.debug("DuckDuckGo Fehler für '%s': %s", product_name, e)
        return None


async def get_reference_price(product_name: str) -> dict | None:
    """
    Gibt Referenzpreis (€/kg) von Rewe zurück.
    Ergebnis ist 24h gecacht. None = kein Preis verfügbar.
    """
    key = product_name.lower().strip()
    cached = _CACHE.get(key)
    if cached and (time.monotonic() - cached[0]) < _CACHE_TTL:
        return cached[1]

    result = await _fetch_via_duckduckgo(product_name)

    _CACHE[key] = (time.monotonic(), result)
    if result:
        logger.info(
            "Preisvergleich '%s': Rewe %.2f €/kg", product_name, result["price_per_kg"]
        )
    return result


async def check_price_markup(
    product_name: str,
    boeck_price_per_kg: float | None,
    threshold: float = THRESHOLD,
) -> dict | None:
    """
    Vergleicht Böck-Preis/kg mit Rewe-Referenzpreis.
    Gibt Warning-Dict zurück wenn Böck > threshold% teurer, sonst None.

    Warning-Dict:
      boeck_price_per_kg, ref_price_per_kg, ref_product, source, diff_pct
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
