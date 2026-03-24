"""
Externer Preisvergleich: Prüft ob Böck-Preise mehr als X% über Rewe-Preisen liegen.

Strategie:
  1. Rewe-Shop-API (Haupt-Quelle)
  2. Rewe-Mobile-API (Fallback)
  Scheitern beide → keine Warnung (Feature ist optional)

Ergebnisse werden 24h in-memory gecacht.
"""

import logging
import os

import httpx
import time

logger = logging.getLogger(__name__)

THRESHOLD = 0.10  # Böck muss mehr als 10% teurer sein um zu warnen

_CACHE: dict[str, tuple[float, dict | None]] = {}  # term → (timestamp, result)
_CACHE_TTL = 1_209_600.0  # 14 Tage

_REWE_PLZ = os.getenv("REWE_PLZ", "10115")  # Berlin Mitte als Default, via .env konfigurierbar
_REWE_MARKET_ID: str | None = None  # wird beim ersten Aufruf gesetzt

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "de-DE,de;q=0.9",
}


import re as _re
import anthropic as _anthropic


async def _get_rewe_market_id() -> str | None:
    """Gibt Rewe Markt-ID für die konfigurierte PLZ zurück (gecacht in Modul-Variable)."""
    global _REWE_MARKET_ID
    if _REWE_MARKET_ID:
        return _REWE_MARKET_ID
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            resp = await client.get(
                "https://shop.rewe.de/api/marketsearch/markets",
                params={"search": _REWE_PLZ},
                headers=_HEADERS,
            )
        if resp.status_code != 200:
            logger.debug("Rewe Marktsuche Status %d für PLZ %s", resp.status_code, _REWE_PLZ)
            return None
        data = resp.json()
        # HAL-Format: _embedded.markets oder direkt markets
        markets = (
            data.get("_embedded", {}).get("markets", [])
            or data.get("markets", [])
        )
        if markets:
            mid = (
                markets[0].get("id")
                or markets[0].get("marketId")
                or markets[0].get("wwIdent")
            )
            if mid:
                _REWE_MARKET_ID = str(mid)
                logger.info("Rewe Markt-ID für PLZ %s: %s", _REWE_PLZ, _REWE_MARKET_ID)
                return _REWE_MARKET_ID
        logger.debug("Rewe Marktsuche: keine Markt-ID gefunden. Keys: %s", list(data.keys()))
    except Exception as e:
        logger.debug("Rewe Marktsuche Fehler: %s", e)
    return None


async def _fetch_rewe_delivery(product_name: str) -> dict | None:
    """
    Fragt Rewe Lieferservice-API mit Markt-ID ab → liefert Preise.
    Markt-ID wird via PLZ (REWE_PLZ in .env) ermittelt.
    """
    market_id = await _get_rewe_market_id()
    try:
        params = {
            "search": product_name,
            "page": 1,
            "objectsPerPage": 5,
            "sorting": "RELEVANCE_DESC",
            "locale": "de_DE",
        }
        cookies = {}
        if market_id:
            params["marketId"] = market_id
            cookies["rwc_marketId"] = market_id

        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True, cookies=cookies) as client:
            resp = await client.get(
                "https://shop.rewe.de/api/products",
                params=params,
                headers=_HEADERS,
            )
        if resp.status_code != 200:
            logger.debug("Rewe Lieferservice Status %d für '%s'", resp.status_code, product_name)
            return None

        data = resp.json()
        embedded = data.get("_embedded", {})
        products = embedded.get("products", [])
        if not products:
            return None

        # Artikel mit Preis/kg suchen
        for p in products:
            name = p.get("productName") or p.get("name", product_name)
            articles = p.get("_embedded", {}).get("articles", [])
            for article in articles:
                listing = article.get("listing", {})
                grammage = listing.get("grammagePrice", {})
                val = grammage.get("value")
                if val and 0.30 < float(val) < 800:
                    logger.info("Rewe Lieferservice Preis/kg für '%s': %.2f", product_name, float(val))
                    return {"price_per_kg": float(val), "name": name, "source": "Rewe"}

        return None
    except Exception as e:
        logger.debug("Rewe Lieferservice Fehler für '%s': %s", product_name, e)
        return None


async def _fetch_via_claude_search(product_name: str) -> dict | None:
    """
    Nutzt Claude Web Search um Referenzpreise zu finden.
    Läuft über Anthropics Server → kein IP-Block durch Supermärkte.
    Claude gibt Preis + gefundenes Vergleichsprodukt zurück.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        client = _anthropic.AsyncAnthropic(api_key=api_key)
        message = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
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

        # Antwort extrahieren
        for block in message.content:
            if not hasattr(block, "text"):
                continue
            raw = block.text.strip()
            if "KEIN TREFFER" in raw:
                return None

            price_m = _re.search(r'PREIS:\s*(\d+)[,\.](\d+)', raw)
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
    Gibt Referenzpreis (€/kg) von Rewe zurück.
    Ergebnis ist 24h gecacht. None = kein Preis verfügbar.
    """
    key = product_name.lower().strip()
    cached = _CACHE.get(key)
    if cached and (time.monotonic() - cached[0]) < _CACHE_TTL:
        return cached[1]

    result = await _fetch_rewe_delivery(product_name)
    if not result:
        result = await _fetch_via_claude_search(product_name)

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
