"""
Web-Rezept-Scraper.
Nutzt recipe-scrapers für 500+ bekannte Rezeptseiten.
Fallback: Claude API mit dem Seiten-Text.
"""

import json
import logging
import os

import anthropic

logger = logging.getLogger(__name__)

_FALLBACK_SYSTEM_PROMPT = """\
Du bist ein Küchenassistent. Extrahiere alle Zutaten aus dem folgenden Webseitentext.

Antworte NUR mit einem JSON-Array. Kein weiterer Text.
Format:
[
  {"name": "Tomaten", "quantity": 500, "unit": "g"},
  {"name": "Olivenöl", "quantity": 2, "unit": "EL"}
]

Regeln:
- "name": Zutat auf Deutsch, normalisiert
- "quantity": immer eine Zahl (bei "etwas" → 1)
- "unit": g, kg, ml, l, EL, TL, Stück, Zehe, Prise, Bund, Packung, Dose
- Falls keine Zutaten erkennbar: []
"""


async def scrape_recipe(url: str) -> list[dict]:
    """
    Scrapet Zutaten von einer Rezept-URL.
    1. Versucht recipe-scrapers (strukturiert, schnell)
    2. Fallback: Seiten-Text via httpx + Claude API
    """
    # 1. recipe-scrapers
    ingredients = _try_recipe_scrapers(url)
    if ingredients:
        return ingredients

    # 2. Fallback: rohen Text holen + Claude
    logger.info("recipe-scrapers hat nichts gefunden, versuche Fallback für %s", url)
    page_text = await _fetch_page_text(url)
    if page_text:
        return await _extract_from_text(url, page_text)

    return []


def _try_recipe_scrapers(url: str) -> list[dict]:
    """Versucht Zutaten mit der recipe-scrapers Library zu extrahieren."""
    try:
        from recipe_scrapers import scrape_me  # noqa: PLC0415
    except ImportError:
        logger.warning("recipe-scrapers nicht installiert")
        return []

    try:
        scraper = scrape_me(url)
        raw_ingredients = scraper.ingredients()
        if not raw_ingredients:
            return []
        return _parse_ingredient_strings(raw_ingredients)
    except Exception as e:
        logger.info("recipe-scrapers konnte %s nicht parsen: %s", url, e)
        return []


def _parse_ingredient_strings(raw: list[str]) -> list[dict]:
    """
    Wandelt Strings wie "500 g Tomaten" oder "2 EL Olivenöl" in Dicts um.
    Einfache Heuristik: erste Token als Menge, zweite als Einheit, Rest als Name.
    """
    known_units = {
        "g", "kg", "ml", "l", "el", "tl", "stück", "zehe", "zehen",
        "prise", "bund", "packung", "dose", "scheibe", "scheiben",
        "stk", "st", "liter", "gramm", "kilo",
    }
    result = []
    for line in raw:
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 2)  # max 3 Teile
        if not parts:
            continue

        quantity = 1.0
        unit = "Stück"
        name = line

        if len(parts) >= 1:
            try:
                # Komma als Dezimaltrennzeichen unterstützen
                quantity = float(parts[0].replace(",", "."))
                if len(parts) >= 2:
                    if parts[1].lower().rstrip(".") in known_units:
                        unit = parts[1]
                        name = parts[2] if len(parts) > 2 else parts[1]
                    else:
                        unit = "Stück"
                        name = " ".join(parts[1:])
            except ValueError:
                # Kein numerischer Anfang → komplette Zeile als Name
                name = line
                quantity = 1.0
                unit = "Stück"

        result.append({
            "name": name.strip(),
            "quantity": quantity,
            "unit": unit,
        })
    return result


async def _fetch_page_text(url: str) -> str:
    """Lädt den Rohtext einer Seite via httpx."""
    try:
        import httpx  # noqa: PLC0415
    except ImportError:
        logger.warning("httpx nicht installiert – installiere mit: pip install httpx")
        return ""

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            headers = {"User-Agent": "Mozilla/5.0 (compatible; BoeckAgent/1.0)"}
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            # Nur Text extrahieren, HTML-Tags entfernen
            import re  # noqa: PLC0415
            text = re.sub(r"<[^>]+>", " ", resp.text)
            text = re.sub(r"\s+", " ", text)
            # Auf 4000 Zeichen kürzen (reicht für Zutaten)
            return text[:4000]
    except Exception as e:
        logger.warning("Fehler beim Laden von %s: %s", url, e)
        return ""


async def _extract_from_text(url: str, page_text: str) -> list[dict]:
    """Extrahiert Zutaten aus Rohtext via Claude API."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return []

    client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=3)
    try:
        message = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=_FALLBACK_SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": f"URL: {url}\n\nSeiteninhalt:\n{page_text}",
            }],
        )
        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            import re  # noqa: PLC0415
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)
            raw = raw.strip()
        ingredients = json.loads(raw)
        return _validate_ingredients(ingredients)
    except (json.JSONDecodeError, anthropic.APIError) as e:
        logger.error("Fallback-Extraktion fehlgeschlagen für %s: %s", url, e)
        return []


def _validate_ingredients(raw: list) -> list[dict]:
    result = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        try:
            quantity = float(item.get("quantity", 1))
        except (ValueError, TypeError):
            quantity = 1.0
        unit = str(item.get("unit", "Stück")).strip() or "Stück"
        result.append({"name": name, "quantity": quantity, "unit": unit})
    return result


if __name__ == "__main__":
    import asyncio
    import sys
    from dotenv import load_dotenv
    load_dotenv()

    url = sys.argv[1] if len(sys.argv) > 1 else "https://www.chefkoch.de/rezepte/1083751213276966/Tomatensalat.html"

    async def test():
        print(f"Scrape: {url}")
        result = await scrape_recipe(url)
        print(f"Zutaten ({len(result)}):")
        for item in result:
            print(f"  {item['quantity']} {item['unit']} {item['name']}")

    asyncio.run(test())
