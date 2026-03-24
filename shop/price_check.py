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

logger = logging.getLogger(__name__)

THRESHOLD = 0.10  # Böck muss mehr als 10% teurer sein um zu warnen

_CACHE: dict[str, tuple[float, dict | None]] = {}  # term → (timestamp, result)
_CACHE_TTL = 1_209_600.0  # 14 Tage


import re as _re
import anthropic as _anthropic


async def _fetch_via_claude_search(product_name: str) -> dict | None:
    """
    Nutzt Claude Web Search um Referenzpreise zu finden.
    Läuft über Anthropics Server → kein IP-Block durch Supermärkte.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        client = _anthropic.AsyncAnthropic(api_key=api_key)
        message = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=200,
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 2,
            }],
            messages=[{
                "role": "user",
                "content": (
                    f"Wie viel kostet '{product_name}' pro kg bei deutschen Supermärkten "
                    f"(Rewe, Edeka, Kaufland, Aldi, Lidl)? "
                    f"Antworte NUR mit einer Zahl in Euro, z.B. '2.49'. "
                    f"Das ist der typische Marktpreis pro kg. Kein Text, nur die Zahl."
                ),
            }],
        )

        # Antwort extrahieren
        for block in message.content:
            if hasattr(block, "text"):
                raw = block.text.strip()
                # Zahl aus Antwort parsen
                m = _re.search(r'(\d+)[,\.](\d+)', raw)
                if m:
                    val = float(f"{m.group(1)}.{m.group(2)}")
                    if 0.30 < val < 800:
                        logger.info("Claude-Search Preis/kg für '%s': %.2f", product_name, val)
                        return {"price_per_kg": val, "name": product_name, "source": "Marktvergleich"}

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
