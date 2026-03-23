# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Böck Einkaufsagent** – A Telegram bot that receives recipes from a group chat, extracts ingredients, checks inventory, fills a shopping cart at gemuese-bestellen.de (Shopware 6), and sends back a report. **No checkout ever occurs** – the bot stops at cart level.

Pipeline: Telegram → Recipe Parser → Inventory Manager → Böck Shop Agent → Telegram Report

## Tech Stack

- Python 3.11+
- `python-telegram-bot` 20.x (async)
- `Playwright` (async, headless Chromium)
- Claude API `claude-sonnet-4-6` (Vision + Text for OCR and product matching)
- `recipe-scrapers` (Chefkoch + 500+ recipe sites)
- `kptncook` Python lib (KptnCook links via 8-char hex ID)
- SQLite (`boeck_agent.db`) – single file, no server
- `python-dotenv` (all secrets via `.env`)

## Project Structure

```
boeck-agent/
├── main.py              # Entry point
├── .env.example         # Secret template
├── requirements.txt
├── boeck_agent.db       # Auto-created on first run
├── bot/
│   └── telegram_bot.py  # Bot handlers, group logic
├── parser/
│   ├── recipe_parser.py # Dispatcher: text / photo / link / KptnCook
│   ├── ocr.py           # Claude Vision for photo input
│   ├── web_scraper.py   # recipe-scrapers wrapper
│   └── kptncook.py      # KptnCook ID extraction + API
├── inventory/
│   └── manager.py       # Read, deduct, update inventory
├── shop/
│   ├── browser.py       # Playwright session management
│   ├── search.py        # Product search + Claude matching
│   └── cart.py          # Add to cart (NO CHECKOUT)
├── db/
│   └── schema.py        # DB init + migrations
└── utils/
    └── report.py        # Telegram report formatting
```

## Development Workflow

Build modules in this order, testing each before proceeding:
1. `db/schema.py` + `.env.example`
2. `bot/telegram_bot.py` – basic message receive/reply
3. `parser/` – free text + KptnCook (most common)
4. `parser/` – photo (Vision) + web links
5. `inventory/manager.py`
6. `shop/search.py` – search + Claude matching (without cart)
7. `shop/cart.py` – cart filling (with checkout block)
8. `utils/report.py` + full integration
9. VPS deployment + Telegram webhook

## Database (SQLite)

Single file: `boeck_agent.db`. Five tables: `inventory`, `orders`, `order_items`, `price_threshold`, `known_products`.

**Shelf-life deduction logic** (applied automatically after each order):
| `shelf_life` | Examples | Remaining after order | Written off after |
|---|---|---|---|
| `lang` | Oil, pasta, flour | 80% | 60 days |
| `mittel` | Potatoes, carrots, butter | 30% | 7 days |
| `kurz` | Salad, tomatoes, herbs | 0% | 3 days |

**Price thresholds** (configurable in `price_threshold` table):
- Default: €30/kg
- Kräuter: €80/kg
- Pilze: €50/kg

**Product cache** (`known_products`): invalidate entries older than 7 days to refresh Böck prices.

## Recipe Input Types

- **A – Free text**: Claude extracts ingredients from natural language
- **B – Photo**: Claude Vision OCR → ingredient list
- **C – Web link**: `recipe-scrapers` → structured ingredients
- **D – KptnCook link**: extract 8-char hex ID from URL → `kptncook` lib API

KptnCook ID example: `http://mobile.kptncook.com/recipe/.../a1b2c3d4` → ID `a1b2c3d4`

## Security – CRITICAL CONSTRAINTS

### Checkout is HARD-BLOCKED in code (not via prompt/condition)

```python
ALLOWED_URL_PATTERNS = [
    'gemuese-bestellen.de/search',
    'gemuese-bestellen.de/gemuese/',
    'gemuese-bestellen.de/obst/',
    'gemuese-bestellen.de/checkout/cart',  # view only
]

BLOCKED_URL_PATTERNS = [
    'checkout/confirm',
    'checkout/finish',
    'order/complete',
]
```

Playwright session **must terminate** after the last `add-to-cart` call. Return the cart URL; never navigate to checkout.

All secrets go in `.env` only. The Böck account must have **no payment method stored**.

## Telegram Report Format

```
✅ X Artikel im Warenkorb (~YY,YY €)
❌ Nicht bestellbar: [Liste]
💸 Zu teuer übersprungen: [Artikel – Preis – Grund]
🧺 Aus Vorrat entnommen: [Liste]
🔗 Warenkorb prüfen: [URL]
```
