"""
KptnCook-Link Parser.
Extrahiert die 8-stellige Hex-ID aus einer KptnCook-URL und ruft die API ab.
"""

import logging
import re

logger = logging.getLogger(__name__)


async def resolve_share_url(url: str) -> str:
    """
    Folgt dem Redirect eines share.kptncook.com-Kurzlinks und gibt die finale URL zurück.
    """
    try:
        import httpx  # noqa: PLC0415
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.head(url)
            final_url = str(resp.url)
            logger.info("KptnCook Share-Link aufgelöst: %s → %s", url, final_url)
            return final_url
    except Exception as e:
        logger.warning("Konnte Share-Link nicht auflösen: %s", e)
        return url

# Regex für 8-stellige Hex-ID am Ende der URL
_KPTNCOOK_ID_RE = re.compile(r"[0-9a-f]{8}", re.IGNORECASE)
_KPTNCOOK_URL_RE = re.compile(r"kptncook\.com|kptn-cook\.com", re.IGNORECASE)


def is_kptncook_url(text: str) -> bool:
    return bool(_KPTNCOOK_URL_RE.search(text))


def extract_kptncook_id(url: str) -> str | None:
    """Gibt die 8-stellige Hex-ID aus einer KptnCook-URL zurück."""
    # Suche von rechts nach der ersten 8-char Hex-Sequenz
    matches = _KPTNCOOK_ID_RE.findall(url)
    if matches:
        return matches[-1].lower()
    return None


def fetch_kptncook_recipe(recipe_id: str) -> list[dict]:
    """
    Ruft ein KptnCook-Rezept über die kptncook-Bibliothek ab.
    Gibt eine normalisierte Zutatenliste zurück.
    """
    try:
        from kptncook import KptnCookClient, get_recipe_by_id  # noqa: PLC0415
    except ImportError:
        logger.error("kptncook-Bibliothek nicht installiert: pip install kptncook")
        return []

    try:
        client = KptnCookClient()
        recipe = get_recipe_by_id(recipe_id)
        return _normalize_kptncook_ingredients(recipe)
    except SystemExit:
        logger.warning("KptnCook API nicht erreichbar (API-Key oder Netzwerkfehler)")
        return []
    except Exception as e:
        logger.exception("Fehler beim Abrufen von KptnCook-Rezept %s: %s", recipe_id, e)
        return []


def _normalize_kptncook_ingredients(recipe) -> list[dict]:
    """
    Wandelt KptnCook Recipe-Objekt in einheitliches Format um.
    Struktur: recipe.ingredients → list[Ingredient]
      Ingredient.quantity: float | None
      Ingredient.measure: str | None   (Einheit)
      Ingredient.ingredient: IngredientDetails
        IngredientDetails.localized_title: dict | str
    """
    ingredients = []
    try:
        raw = getattr(recipe, "ingredients", []) or []
        for item in raw:
            details = getattr(item, "ingredient", None)
            if details is None:
                continue

            # Name aus localized_title extrahieren (kann dict sein)
            localized = getattr(details, "localized_title", None)
            if isinstance(localized, dict):
                name = localized.get("de") or localized.get("en") or next(iter(localized.values()), "")
            else:
                name = str(localized or "")
            name = name.strip()
            if not name:
                continue

            quantity = float(item.quantity) if item.quantity is not None else 1.0
            unit = str(item.measure or "Stück").strip() or "Stück"

            ingredients.append({"name": name, "quantity": quantity, "unit": unit})
    except Exception as e:
        logger.warning("Fehler beim Normalisieren der KptnCook-Zutaten: %s", e)
    return ingredients


if __name__ == "__main__":
    # Schnelltest
    test_urls = [
        "http://mobile.kptncook.com/recipe/pinterest/leckeres-rezept/a1b2c3d4",
        "https://www.kptncook.com/de/recipe/abc12345",
        "kein kptncook link",
    ]
    for url in test_urls:
        print(f"URL: {url}")
        print(f"  is_kptncook: {is_kptncook_url(url)}")
        print(f"  ID: {extract_kptncook_id(url)}")
