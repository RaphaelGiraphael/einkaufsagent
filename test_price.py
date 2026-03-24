"""
Standalone-Test für Preisvergleich.
Ausführen: python test_price.py "Schalotten"
           python test_price.py "Bio Eier" "Feta"
"""
import asyncio
import sys
import os
from dotenv import load_dotenv
load_dotenv()

# Pfad setzen damit Imports funktionieren
sys.path.insert(0, os.path.dirname(__file__))

from shop.price_check import get_reference_price, check_price_markup


async def main():
    terms = sys.argv[1:] if len(sys.argv) > 1 else ["Karotten", "Bio Eier", "Feta"]
    for term in terms:
        print(f"\n🔍 Suche Referenzpreis für: {term}")
        ref = await get_reference_price(term)
        if ref:
            print(f"  ✅ {ref['source']}: {ref['price_per_kg']:.2f} €/kg  ({ref['name']})")
        else:
            print(f"  ❌ Kein Referenzpreis gefunden")

        # Beispiel: Böck-Preis 20% teurer
        if ref:
            boeck_price = ref["price_per_kg"] * 1.2
            warning = await check_price_markup(term, boeck_price)
            if warning:
                print(f"  ⚠️  Böck {boeck_price:.2f} €/kg wäre +{warning['diff_pct']*100:.0f}% teurer → Warnung!")

asyncio.run(main())
