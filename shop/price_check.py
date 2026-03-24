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


async def _fetch_rewe_shop(product_name: str) -> dict | None:
    """Haupt-Endpunkt: Rewe Desktop-Shop API."""
    try:
        async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as client:
            resp = await client.get(
                "https://shop.rewe.de/api/products",
                params={
                    "search": product_name,
                    "page": 1,
                    "objectsPerPage": 3,
                    "sorting": "RELEVANCE_DESC",
                    "locale": "de_DE",
                },
                headers=_HEADERS,
            )
        if resp.status_code != 200:
            logger.info("Rewe-Shop-API Status %d für '%s'", resp.status_code, product_name)
            return None
        data = resp.json()

        # Debug: Struktur loggen (einmalig pro Key)
        top_keys = list(data.keys())[:8]
        logger.info("Rewe-API Top-Keys für '%s': %s", product_name, top_keys)

        # Format A: _embedded.products (HAL)
        products = data.get("_embedded", {}).get("products", [])
        # Format B: products direkt
        if not products:
            products = data.get("products", [])
        # Format C: items
        if not products:
            products = data.get("items", [])
        if not products:
            logger.info("Rewe-API: Keine Produkte in Antwort. Keys: %s", top_keys)
            return None

        p = products[0]
        logger.info("Rewe erstes Produkt Keys: %s", list(p.keys())[:10])

        name = p.get("name", "")

        # Preis/kg aus verschiedenen möglichen Feldern
        pricing = p.get("pricing", p.get("price", {}))
        if isinstance(pricing, dict):
            grammage = pricing.get("grammage", {})
            price_per_kg = grammage.get("price") or pricing.get("pricePerUnit")
        else:
            price_per_kg = None

        # Direktfeld pricePerUnit
        if not price_per_kg:
            price_per_kg = p.get("pricePerUnit") or p.get("unitPrice") or p.get("basePrice")

        if price_per_kg:
            if float(price_per_kg) > 500:
                price_per_kg = float(price_per_kg) / 100
            return {"price_per_kg": float(price_per_kg), "name": name, "source": "Rewe"}

        logger.info("Rewe: kein Preis/kg für '%s'. Produkt-Keys: %s", product_name, list(p.keys())[:10])
        return None

    except Exception as e:
        logger.info("Rewe-Shop-API Fehler für '%s': %s", product_name, e)
        return None


async def _fetch_rewe_mobile(product_name: str) -> dict | None:
    """Fallback: Rewe Mobile-App API."""
    try:
        async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as client:
            resp = await client.get(
                "https://mobile.rewe.de/api/v3/products",
                params={"query": product_name, "page": 1},
                headers=_HEADERS,
            )
        if resp.status_code != 200:
            return None
        data = resp.json()
        products = data.get("products", [])
        if not products:
            return None

        p = products[0]
        name = p.get("name", "")
        price_per_kg = p.get("pricePerUnit") or p.get("unitPrice")
        if price_per_kg:
            if price_per_kg > 500:
                price_per_kg /= 100
            return {"price_per_kg": float(price_per_kg), "name": name, "source": "Rewe"}
        return None

    except Exception as e:
        logger.debug("Rewe-Mobile-API fehlgeschlagen für '%s': %s", product_name, e)
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

    result = await _fetch_rewe_shop(product_name)
    if not result:
        result = await _fetch_rewe_mobile(product_name)

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
