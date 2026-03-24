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


async def _fetch_rewe_html(product_name: str) -> dict | None:
    """
    Scrapet die Rewe-Suchseite und extrahiert Preise aus dem eingebetteten JSON.
    Funktioniert ohne Markt-ID.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(
                "https://www.rewe.de/suche/",
                params={"search": product_name},
                headers={**_HEADERS, "Accept": "text/html,application/xhtml+xml"},
            )
        if resp.status_code != 200:
            return None

        text = resp.text

        # Rewe bettet Preisdaten als JSON-LD oder in data-Attributen ein
        # Versuch 1: application/json script-Tags
        for script_match in _re.finditer(
            r'<script[^>]+type=["\']application/json["\'][^>]*>(.*?)</script>',
            text, _re.DOTALL
        ):
            try:
                blob = json.loads(script_match.group(1))
                result = _extract_price_from_blob(blob, product_name)
                if result:
                    return result
            except Exception:
                continue

        # Versuch 2: __NEXT_DATA__ (Next.js)
        nd_match = _re.search(
            r'<script id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', text, _re.DOTALL
        )
        if nd_match:
            try:
                blob = json.loads(nd_match.group(1))
                result = _extract_price_from_blob(blob, product_name)
                if result:
                    return result
            except Exception:
                pass

        # Versuch 3: Preis-Pattern direkt im HTML (€/kg oder €/100g)
        # Sucht z.B. "2,49 €/kg" oder "(1,99 €/100g)"
        kg_matches = _re.findall(r'(\d+[,\.]\d+)\s*€\s*/\s*(?:1\s*)?kg', text, _re.IGNORECASE)
        if kg_matches:
            # Günstigsten Preis nehmen (erster plausibler Wert)
            prices = []
            for m in kg_matches[:10]:
                try:
                    prices.append(float(m.replace(",", ".")))
                except ValueError:
                    pass
            if prices:
                price = min(p for p in prices if 0.5 < p < 500)
                if price:
                    logger.info("Rewe HTML-Preis/kg für '%s': %.2f", product_name, price)
                    return {"price_per_kg": price, "name": product_name, "source": "Rewe"}

        return None

    except Exception as e:
        logger.debug("Rewe-HTML-Scraping Fehler für '%s': %s", product_name, e)
        return None


def _extract_price_from_blob(blob: dict | list, product_name: str) -> dict | None:
    """Durchsucht rekursiv ein JSON-Objekt nach Preis/kg-Feldern."""
    if isinstance(blob, list):
        for item in blob[:20]:
            result = _extract_price_from_blob(item, product_name)
            if result:
                return result
        return None
    if not isinstance(blob, dict):
        return None

    # Typische Rewe-Felder für Preis/kg
    for key in ("grammagePrice", "pricePerUnit", "unitPrice", "basePrice"):
        val = blob.get(key)
        if val and isinstance(val, (int, float)) and 0.5 < val < 500:
            name = blob.get("name") or blob.get("productName") or product_name
            return {"price_per_kg": float(val), "name": name, "source": "Rewe"}
        if isinstance(val, dict):
            inner = val.get("value") or val.get("amount")
            if inner and isinstance(inner, (int, float)) and 0.5 < float(inner) < 500:
                name = blob.get("name") or blob.get("productName") or product_name
                return {"price_per_kg": float(inner), "name": name, "source": "Rewe"}

    # Rekursiv in verschachtelte Dicts
    for v in blob.values():
        if isinstance(v, (dict, list)):
            result = _extract_price_from_blob(v, product_name)
            if result:
                return result
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

    result = await _fetch_rewe_html(product_name)

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
