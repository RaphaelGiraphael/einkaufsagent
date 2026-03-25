"""Debug: testet price_check mit dem echten Prompt"""
import asyncio, logging, os, sys
logging.basicConfig(level=logging.DEBUG, format="%(name)s: %(message)s")
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.path.dirname(__file__))

from shop.price_check import get_reference_price, check_price_markup

async def main():
    term = sys.argv[1] if len(sys.argv) > 1 else "Bio Eier"
    print(f"Suche: {term}\n")
    ref = await get_reference_price(term)
    if ref:
        print(f"Referenzpreis: {ref['price']:.2f} €/{ref['unit']}")
        print(f"Produkt:       {ref['name']}")
        print(f"Markt:         {ref['source']}")
    else:
        print("Kein Referenzpreis gefunden.")

asyncio.run(main())
