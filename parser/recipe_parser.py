"""
Rezept-Parser Dispatcher.
Erkennt den Eingabetyp (Freitext / Foto / Web-Link / KptnCook)
und delegiert an den passenden Sub-Parser.

Rückgabeformat immer:
  [{"name": "Tomaten", "quantity": 500.0, "unit": "g"}, ...]
"""

import json
import logging
import os
import re

import anthropic

from parser.kptncook import extract_kptncook_id, fetch_kptncook_recipe, is_kptncook_url, resolve_share_url

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

_EXTRACT_SYSTEM_PROMPT = """\
Du bist ein Küchenassistent. Extrahiere alle Zutaten aus dem folgenden Rezept-Text.

Antworte NUR mit einem JSON-Array. Kein weiterer Text, keine Erklärungen.
Format:
[
  {"name": "Tomaten", "quantity": 500, "unit": "g", "category": "default"},
  {"name": "Oregano", "quantity": 1, "unit": "TL", "category": "Gewürze"},
  {"name": "getrocknete Tomaten", "quantity": 100, "unit": "g", "category": "Getrocknet"}
]

Regeln:
- "name": Zutat auf Deutsch, normalisiert (z.B. "Zwiebel" statt "Zwiebeln")
- "quantity": immer eine Zahl (bei "etwas" oder "nach Geschmack" → 1)
- "unit": g, kg, ml, l, EL, TL, Stück, Zehe, Prise, Bund, Packung, Dose
- "category": eine der folgenden Kategorien zuweisen:
  - "Gewürze": Gewürze, Salz, Pfeffer, Chili, Paprikapulver, Kreuzkümmel, etc.
  - "Kräuter": frische Kräuter (Basilikum, Petersilie, Schnittlauch, Oregano frisch etc.)
  - "Getrocknet": getrocknete Früchte/Gemüse/Kräuter, Hülsenfrüchte, Nüsse, Samen, Reis, Nudeln
  - "Pilze": frische oder getrocknete Pilze
  - "default": alles andere (frisches Gemüse, Obst, Milchprodukte, Fleisch, Eier, Öle etc.)
- Mengenangaben skalieren falls der Text eine Portionszahl nennt und
  der User eine andere Anzahl wünscht (z.B. "für 2 statt 4 Personen" → halbieren)
- Gewürze und Öle einschließen
"""


async def parse_recipe(text: str, image_bytes: bytes | None = None) -> list[dict]:
    """
    Hauptfunktion: erkennt Typ und gibt normalisierte Zutatenliste zurück.
    """
    text = text.strip()

    # 1. Foto → OCR (Modul 4, hier als Stub)
    if image_bytes:
        try:
            from parser.ocr import extract_ingredients_from_image  # noqa: PLC0415
            return await extract_ingredients_from_image(image_bytes)
        except ImportError:
            logger.warning("OCR-Modul noch nicht verfügbar")
            # Fallback: Text aus Caption parsen falls vorhanden
            if not text:
                return []

    # 2. KptnCook-URL
    if is_kptncook_url(text):
        kptncook_text = text
        # share.kptncook.com-Kurzlinks erst auflösen
        if "share.kptncook.com" in text:
            url_match = _URL_RE.search(text)
            if url_match:
                kptncook_text = await resolve_share_url(url_match.group(0))
        recipe_id = extract_kptncook_id(kptncook_text)
        if recipe_id:
            logger.info("KptnCook-Rezept erkannt, ID: %s", recipe_id)
            ingredients = fetch_kptncook_recipe(recipe_id)
            if ingredients:
                return ingredients
            logger.warning("KptnCook-Abfrage leer – Fallback auf Freitext")

    # 3. Web-Link → Web-Scraper (Modul 4, hier als Stub)
    url_match = _URL_RE.search(text)
    if url_match:
        url = url_match.group(0)
        try:
            from parser.web_scraper import scrape_recipe  # noqa: PLC0415
            ingredients = await scrape_recipe(url)
            if ingredients:
                return ingredients
        except ImportError:
            logger.warning("Web-Scraper noch nicht verfügbar")

    # 4. Freitext → Claude API
    if text:
        return await _parse_freetext(text)

    return []


async def _parse_freetext(text: str) -> list[dict]:
    """Extrahiert Zutaten aus Freitext via Claude API."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY nicht gesetzt")
        return []

    # max_retries=3 wiederholt automatisch bei 429/529
    client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=3)

    try:
        message = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=_EXTRACT_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": text}],
        )
        raw = message.content[0].text.strip()
        # Claude umhüllt JSON manchmal mit ```json ... ``` – entfernen
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)
            raw = raw.strip()
        ingredients = json.loads(raw)
        return _validate_ingredients(ingredients)

    except json.JSONDecodeError as e:
        logger.error("Claude API hat kein gültiges JSON zurückgegeben: %s", e)
        return []
    except anthropic.APIError as e:
        logger.error("Anthropic API Fehler: %s", e)
        return []


def _validate_ingredients(raw: list) -> list[dict]:
    """Bereinigt und validiert die Zutatenliste."""
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
        category = str(item.get("category", "default")).strip() or "default"
        result.append({"name": name, "quantity": quantity, "unit": unit, "category": category})
    return result


def merge_ingredients(ingredients: list[dict]) -> list[dict]:
    """
    Fasst Zutaten mit gleichem Namen zusammen.
    Gleiche Einheit → Mengen addieren. Verschiedene Einheiten → behalte beide.
    """
    merged: dict[str, dict] = {}
    for item in ingredients:
        key = item["name"].lower().strip()
        if key in merged:
            existing = merged[key]
            if existing["unit"].lower() == item["unit"].lower():
                merged[key] = {**existing, "quantity": existing["quantity"] + item["quantity"]}
            # Verschiedene Einheiten: zweiten Eintrag unter key+unit speichern
            else:
                merged[f"{key}_{item['unit'].lower()}"] = {**item}
        else:
            merged[key] = {**item}
    return list(merged.values())


if __name__ == "__main__":
    import asyncio
    from dotenv import load_dotenv
    load_dotenv()

    async def test():
        print("=== Test 1: KptnCook-URL ===")
        url = "http://mobile.kptncook.com/recipe/pinterest/test/a1b2c3d4"
        result = await parse_recipe(url)
        print(f"Ergebnis: {result or '(KptnCook API nicht erreichbar – erwartet)'}\n")

        print("=== Test 2: Freitext ===")
        text = (
            "Für einen Tomatensalat für 2 Personen brauche ich: "
            "400g Tomaten, 1 rote Zwiebel, 2 EL Olivenöl, "
            "1 EL Balsamico, Salz und Pfeffer nach Geschmack, "
            "etwas frisches Basilikum."
        )
        result = await parse_recipe(text)
        print(f"Erkannte Zutaten ({len(result)}):")
        for item in result:
            print(f"  {item['quantity']} {item['unit']} {item['name']}")

    asyncio.run(test())
