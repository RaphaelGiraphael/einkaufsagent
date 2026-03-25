"""Debug: zeigt rohe Claude-Antwort"""
import asyncio, logging, os, sys
logging.basicConfig(level=logging.DEBUG, format="%(name)s: %(message)s")
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.path.dirname(__file__))

import anthropic

async def main():
    term = sys.argv[1] if len(sys.argv) > 1 else "Bio Eier"
    print(f"Suche: {term}\n")
    client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    try:
        msg = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 2}],
            messages=[{"role": "user", "content": f"Was kostet {term} pro kg bei Rewe? Antworte mit PREIS: X.XX"}],
        )
        print(f"Stop-Reason: {msg.stop_reason}")
        for i, block in enumerate(msg.content):
            print(f"\nBlock {i}: type={block.type}")
            if hasattr(block, "text"):
                print(f"Text: {block.text}")
            else:
                print(f"Attrs: {[a for a in dir(block) if not a.startswith('_')]}")
    except Exception as e:
        print(f"Fehler: {type(e).__name__}: {e}")

asyncio.run(main())
