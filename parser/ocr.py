"""
Foto-Rezept-Parser via Claude Vision API.
Empfängt Bild-Bytes und gibt eine normalisierte Zutatenliste zurück.
"""

import base64
import json
import logging
import os
import re

import anthropic

logger = logging.getLogger(__name__)

_OCR_SYSTEM_PROMPT = """\
Du bist ein Küchenassistent. Auf dem Bild ist ein Rezept zu sehen.
Extrahiere alle Zutaten aus dem Rezept auf dem Bild.

Antworte NUR mit einem JSON-Array. Kein weiterer Text, keine Erklärungen.
Format:
[
  {"name": "Tomaten", "quantity": 500, "unit": "g"},
  {"name": "Olivenöl", "quantity": 2, "unit": "EL"}
]

Regeln:
- "name": Zutat auf Deutsch, normalisiert
- "quantity": immer eine Zahl (bei "etwas" oder "nach Geschmack" → 1)
- "unit": g, kg, ml, l, EL, TL, Stück, Zehe, Prise, Bund, Packung, Dose
- Falls kein Rezept erkennbar: leeres Array [] zurückgeben
"""


async def extract_ingredients_from_image(image_bytes: bytes) -> list[dict]:
    """
    Analysiert ein Rezept-Foto via Claude Vision und gibt Zutaten zurück.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY nicht gesetzt")
        return []

    # Bildformat erkennen
    media_type = _detect_media_type(image_bytes)

    client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=3)
    image_data = base64.standard_b64encode(image_bytes).decode("utf-8")

    try:
        message = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=_OCR_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": image_data,
                            },
                        },
                        {
                            "type": "text",
                            "text": "Bitte extrahiere alle Zutaten aus diesem Rezept-Bild.",
                        },
                    ],
                }
            ],
        )
        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)
            raw = raw.strip()
        ingredients = json.loads(raw)
        return _validate_ingredients(ingredients)

    except json.JSONDecodeError as e:
        logger.error("Claude Vision hat kein gültiges JSON zurückgegeben: %s", e)
        return []
    except anthropic.APIError as e:
        logger.error("Anthropic API Fehler (Vision): %s", e)
        return []


def _detect_media_type(image_bytes: bytes) -> str:
    """Erkennt das Bildformat anhand der Magic Bytes."""
    if image_bytes[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if image_bytes[:4] in (b"GIF8", b"GIF9"):
        return "image/gif"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    # Fallback
    return "image/jpeg"


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

    if len(sys.argv) < 2:
        print("Verwendung: python -m parser.ocr <bild.jpg>")
        sys.exit(1)

    async def test():
        with open(sys.argv[1], "rb") as f:
            image_bytes = f.read()
        print(f"Bild geladen: {len(image_bytes)} Bytes")
        result = await extract_ingredients_from_image(image_bytes)
        print(f"Erkannte Zutaten ({len(result)}):")
        for item in result:
            print(f"  {item['quantity']} {item['unit']} {item['name']}")

    asyncio.run(test())
