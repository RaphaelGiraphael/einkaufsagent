"""
Standalone-Test für Preisvergleich via Claude Web Search.
Ausführen: python test_price.py "Schafsmilch Feta 45%" "Bio Eier"
"""
import asyncio
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(message)s")

from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.path.dirname(__file__))

from shop.price_check import get_reference_price, check_price_markup


async def main():
    terms = sys.argv[1:] if len(sys.argv) > 1 else ["Bio Eier", "Schafsmilch Feta 45%"]
    for term in terms:
        print(f"\n🔍 {term}")
        ref = await get_reference_price(term)
        if ref:
            print(f"  ✅ {ref['price_per_kg']:.2f} €/kg bei {ref['source']} → '{ref['name']}'")
            warning = await check_price_markup(term, ref["price_per_kg"] * 1.2)
            if warning:
                print(f"  ⚠️  Bei +20% wäre Warnung: +{warning['diff_pct']*100:.0f}%")
        else:
            print(f"  ❌ Kein Referenzpreis gefunden")

asyncio.run(main())
