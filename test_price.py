"""
Standalone-Test für Preisvergleich mit detailliertem Debug-Output.
Ausführen: python test_price.py "Feta"
"""
import asyncio
import json
import os
import sys
from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.dirname(__file__))

import httpx

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "de-DE,de;q=0.9",
}
PLZ = os.getenv("REWE_PLZ", "10115")


async def debug_rewe(product_name: str):
    print(f"\n{'='*60}")
    print(f"Produkt: {product_name}  |  PLZ: {PLZ}")
    print('='*60)

    # Schritt 1: Markt-ID holen
    print(f"\n[1] Marktsuche für PLZ {PLZ}...")
    market_id = None
    async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
        resp = await client.get(
            "https://shop.rewe.de/api/marketsearch/markets",
            params={"search": PLZ},
            headers=HEADERS,
        )
    print(f"    Status: {resp.status_code}")
    if resp.status_code == 200:
        data = resp.json()
        print(f"    Top-Keys: {list(data.keys())}")
        markets = data.get("_embedded", {}).get("markets", []) or data.get("markets", [])
        print(f"    Märkte gefunden: {len(markets)}")
        if markets:
            m = markets[0]
            print(f"    Erster Markt Keys: {list(m.keys())}")
            print(f"    Erster Markt: {json.dumps(m, ensure_ascii=False, indent=2)[:500]}")
            market_id = m.get("id") or m.get("marketId") or m.get("wwIdent")
            print(f"    → Markt-ID: {market_id}")
    else:
        print(f"    Fehler: {resp.text[:200]}")

    # Schritt 2: Produktsuche mit Markt-ID
    print(f"\n[2] Produktsuche '{product_name}' (marketId={market_id})...")
    params = {
        "search": product_name,
        "page": 1,
        "objectsPerPage": 3,
        "sorting": "RELEVANCE_DESC",
        "locale": "de_DE",
    }
    cookies = {}
    if market_id:
        params["marketId"] = market_id
        cookies["rwc_marketId"] = str(market_id)

    async with httpx.AsyncClient(timeout=8.0, follow_redirects=True, cookies=cookies) as client:
        resp = await client.get(
            "https://shop.rewe.de/api/products",
            params=params,
            headers=HEADERS,
        )
    print(f"    Status: {resp.status_code}")
    if resp.status_code == 200:
        data = resp.json()
        embedded = data.get("_embedded", {})
        products = embedded.get("products", [])
        print(f"    Produkte: {len(products)}")
        for i, p in enumerate(products[:2]):
            name = p.get("productName") or p.get("name", "?")
            articles = p.get("_embedded", {}).get("articles", [])
            print(f"\n    Produkt {i+1}: {name}")
            print(f"    Articles: {len(articles)}")
            if articles:
                a = articles[0]
                print(f"    Article Keys: {list(a.keys())}")
                listing = a.get("listing", {})
                print(f"    Listing Keys: {list(listing.keys()) if isinstance(listing, dict) else listing}")
                print(f"    Listing: {json.dumps(listing, ensure_ascii=False)[:400]}")
            else:
                p_embedded_keys = list(p.get("_embedded", {}).keys())
                print(f"    p._embedded Keys: {p_embedded_keys}")
    else:
        print(f"    Fehler: {resp.text[:300]}")


async def main():
    terms = sys.argv[1:] if len(sys.argv) > 1 else ["Feta"]
    for term in terms:
        await debug_rewe(term)

asyncio.run(main())
