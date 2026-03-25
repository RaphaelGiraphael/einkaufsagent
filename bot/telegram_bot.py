"""
Telegram Bot Handler für den Böck Einkaufsagenten.
Empfängt Nachrichten aus erlaubten Gruppen und leitet sie an den Recipe Parser weiter.
"""

import asyncio
import logging
import os
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logger = logging.getLogger(__name__)


def _get_allowed_chat_ids() -> set[int]:
    raw = os.getenv("ALLOWED_CHAT_IDS", "")
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if part:
            try:
                ids.add(int(part))
            except ValueError:
                logger.warning("Ungültige Chat-ID in ALLOWED_CHAT_IDS: %s", part)
    return ids


def _is_allowed(update: Update) -> bool:
    allowed = _get_allowed_chat_ids()
    chat_id = update.effective_chat.id
    logger.info("Nachricht von Chat-ID: %d | Erlaubt: %s", chat_id, allowed)
    if not allowed:
        logger.warning("ALLOWED_CHAT_IDS nicht gesetzt – alle Chats erlaubt!")
        return True
    result = chat_id in allowed
    if not result:
        logger.warning("Chat-ID %d NICHT in ALLOWED_CHAT_IDS %s – ignoriert", chat_id, allowed)
    return result


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    await update.message.reply_text(
        "👋 Böck Einkaufsagent bereit!\n\n"
        "Schick mir ein Rezept – als Text, Foto, Chefkoch-Link oder KptnCook-Link – "
        "und ich befülle den Warenkorb bei gemuese-bestellen.de.\n\n"
        "Kein Checkout: du schaust den Warenkorb selbst durch, bevor du bestellst."
    )


async def cmd_hilfe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    await update.message.reply_text(
        "📖 *Böck Einkaufsagent – Hilfe*\n\n"
        "*Rezept senden:*\n"
        "Einfach ins Gespräch schicken – kein Befehl nötig:\n"
        "• Freitext mit Zutaten\n"
        "• Foto eines Rezepts\n"
        "• Web-Link (Chefkoch, etc.)\n"
        "• KptnCook-Link\n\n"
        "*Befehle:*\n"
        "/warenkorb – Aktuellen Warenkorb-Inhalt anzeigen\n"
        "/liste – Zutaten die noch separat gekauft werden müssen\n"
        "/rein _Artikel_ – Artikel manuell in den Warenkorb legen (Preislimit ignoriert)\n"
        "/raus _Artikel_ – Artikel einmal aus dem Warenkorb nehmen (Menge −1)\n"
        "/vorrat – Alle Vorräte anzeigen und verwalten (➕ ➖ 🗑️)\n"
        "/vorrat _[Menge] [Einheit] Artikel_ – Artikel zum Vorrat hinzufügen\n"
        "/vorrat del _Artikel_ – Artikel aus dem Vorrat löschen\n"
        "/vorrat set _Menge [Einheit] Artikel_ – Vorratsmenge direkt setzen\n"
        "/hilfe – Diese Übersicht\n\n"
        "*Beispiele:*\n"
        "`/rein Oregano`\n"
        "`/raus Zucchini`\n"
        "`/vorrat` → Übersicht\n"
        "`/vorrat 2 Zucchini`\n"
        "`/vorrat set 500 g Tomaten`\n"
        "`/vorrat del Salz`\n\n"
        "⚠️ Kein automatischer Checkout – der Warenkorb wird nur befüllt.",
        parse_mode="Markdown",
    )


_UNIT_RE = r"(g|kg|ml|l|EL|TL|St(?:ü|u)ck|Stk|Zehe|Bund|Prise|Packung|Dose)"


def _parse_vorrat_args(text: str) -> tuple[float, str, str]:
    """
    Parst Menge, Einheit und Name – unterstützt beide Reihenfolgen:
    - "500g Tomaten" / "500 g Tomaten" / "2 Stück Zucchini"  → qty first
    - "Tomaten 500g" / "Zucchini 2"    / "Salz 1 kg"         → name first
    - "2 Zucchini"                                            → qty + name (Stück)
    - "Salz"                                                  → nur Name
    """
    import re
    text = text.strip()

    # Qty zuerst: "500g Name" / "500 g Name" / "2 Stück Name"
    m = re.match(rf"^(\d+(?:[.,]\d+)?)\s*{_UNIT_RE}\s+(.+)$", text, re.IGNORECASE)
    if m:
        return float(m.group(1).replace(",", ".")), m.group(2), m.group(3).strip()

    # Qty zuerst, keine Einheit: "2 Zucchini"
    m = re.match(r"^(\d+(?:[.,]\d+)?)\s+(.+)$", text)
    if m:
        return float(m.group(1).replace(",", ".")), "Stück", m.group(2).strip()

    # Name zuerst: "Salz 1kg" / "Zucchini 2" / "Olivenöl 0.5 l"
    m = re.match(rf"^(.+?)\s+(\d+(?:[.,]\d+)?)\s*{_UNIT_RE}?$", text, re.IGNORECASE)
    if m:
        name = m.group(1).strip()
        qty = float(m.group(2).replace(",", "."))
        unit = m.group(3) if m.group(3) else "Stück"
        return qty, unit, name

    # Nur Name
    return 1.0, "", text


def _get_inv_delta(unit: str) -> float:
    """Sinnvolle Schrittweite für ➕/➖ je nach Einheit."""
    u = unit.lower()
    if u == "g":          return 100.0
    if u in ("kg", "l"):  return 0.5
    if u in ("el", "tl"): return 1.0
    return 1.0  # Stück, Zehe, Bund …


def _build_inventory_keyboard(items: list[dict]) -> InlineKeyboardMarkup:
    """Inline-Tastatur: eine Zeile pro Vorrats-Eintrag.
    Schrittweite der ➕/➖-Buttons passt sich der Einheit an."""
    rows = []
    for item in items:
        iid = item["id"]
        name = item["name"][:15]          # kurz für Mobile
        delta = _get_inv_delta(item["unit"])
        d = f"{delta:g}"
        rows.append([
            InlineKeyboardButton(name, callback_data="inv_noop"),
            InlineKeyboardButton(f"➕{d}", callback_data=f"inv_add_{iid}"),
            InlineKeyboardButton(f"➖{d}", callback_data=f"inv_sub_{iid}"),
            InlineKeyboardButton("🗑️", callback_data=f"inv_del_{iid}"),
        ])
    return InlineKeyboardMarkup(rows)


def _format_inventory_text(items: list[dict]) -> str:
    """Nachrichtentext mit allen Mengen (steht über den Buttons)."""
    lines = [f"📦 *Aktueller Vorrat ({len(items)} Artikel):*\n"]
    for item in items:
        delta = _get_inv_delta(item["unit"])
        lines.append(f"  · {item['name']} – {item['quantity']:g} {item['unit']}")
    lines.append("\n_Schaltflächen: ➕/➖ passen die Menge an · 🗑️ löscht den Eintrag_")
    return "\n".join(lines)


# ASCII-Keys für Einheiten (Telegram verträgt kein ü in callback_data zuverlässig)
_INV_UNIT_KEY = {"stueck": "Stück", "g": "g", "kg": "kg", "l": "l", "el": "EL", "tl": "TL"}
_INV_KEY_UNIT = {v: k for k, v in _INV_UNIT_KEY.items()}


def _build_new_inv_keyboard(qty: float, unit: str) -> InlineKeyboardMarkup:
    """Picker-Tastatur für einen neuen Vorrats-Eintrag.
    Einheiten: Stück · g · kg · l · EL · TL  (kein ml – zu kleinteilig)."""
    delta = _get_inv_delta(unit)
    d = f"{delta:g}"
    unit_rows = [
        InlineKeyboardButton(label, callback_data=f"invn_u_{key}")
        for key, label in _INV_UNIT_KEY.items()
    ]
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"➖{d}", callback_data="invn_sub"),
            InlineKeyboardButton(f"{qty:g} {unit}", callback_data="inv_noop"),
            InlineKeyboardButton(f"➕{d}", callback_data="invn_add"),
        ],
        unit_rows,
        [
            InlineKeyboardButton("✅ Speichern",            callback_data="invn_save"),
            InlineKeyboardButton("🛒 Nur aus WK",           callback_data="invn_cart"),
            InlineKeyboardButton("❌",                      callback_data="invn_cancel"),
        ],
    ])


async def _send_inventory(target, edit: bool = False) -> None:
    """Sendet oder aktualisiert die Vorrats-Übersicht."""
    from inventory.manager import list_inventory
    items = list_inventory()
    if not items:
        text = "📦 *Vorrat ist leer.*\n\nHinzufügen mit: `/vorrat [Menge] [Einheit] Artikel`"
        if edit:
            await target.edit_message_text(text, parse_mode="Markdown")
        else:
            await target.reply_text(text, parse_mode="Markdown")
        return
    text = _format_inventory_text(items)
    keyboard = _build_inventory_keyboard(items)
    if edit:
        await target.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)
    else:
        await target.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def cmd_vorrat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Vorrat anzeigen (kein Arg), bearbeiten (del/set) oder hinzufügen."""
    if not _is_allowed(update):
        return

    args_str = " ".join(context.args) if context.args else ""

    # Kein Argument → Übersicht anzeigen
    if not args_str:
        await _send_inventory(update.message)
        return

    parts = args_str.split(None, 1)
    sub = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""

    # /vorrat del <Name>  – Eintrag löschen
    if sub in ("del", "löschen", "entfernen", "remove"):
        if not rest:
            await update.message.reply_text("Syntax: `/vorrat del Artikelname`", parse_mode="Markdown")
            return
        from inventory.manager import delete_inventory_by_name
        count = delete_inventory_by_name(rest)
        msg = f"🗑️ *{rest}* aus dem Vorrat entfernt." if count else f"❓ '{rest}' nicht im Vorrat gefunden."
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    # /vorrat set <Menge> [Einheit] <Name>  – Menge direkt setzen
    if sub in ("set", "setze", "setzen"):
        if not rest:
            await update.message.reply_text(
                "Syntax: `/vorrat set 3 Zucchini` oder `/vorrat set 500 g Tomaten`",
                parse_mode="Markdown",
            )
            return
        qty, unit, name = _parse_vorrat_args(rest)
        unit_str = unit if unit else "Stück"
        from inventory.manager import list_inventory, set_inventory_qty, add_to_inventory
        matching = [i for i in list_inventory() if i["name"].lower() == name.lower()]
        if matching:
            for item in matching:
                set_inventory_qty(item["id"], qty)
            await update.message.reply_text(
                f"✅ *{name}* auf {qty:g} {unit_str} gesetzt.", parse_mode="Markdown"
            )
        else:
            add_to_inventory([{"name": name, "category": "Sonstiges", "quantity": qty,
                               "unit": unit_str, "shelf_life": "mittel"}])
            await update.message.reply_text(
                f"✅ *{name}* mit {qty:g} {unit_str} neu angelegt.", parse_mode="Markdown"
            )
        return

    # Standard: Artikel hinzufügen + ggfs. aus Warenkorb entfernen
    qty, unit, name = _parse_vorrat_args(args_str)
    unit_str = unit if unit else "Stück"

    # Kein Mengenwert angegeben → interaktiven Picker zeigen
    if qty == 1.0 and not unit:
        context.user_data["new_inv"] = {"name": name, "qty": 0.0, "unit": "Stück"}
        await update.message.reply_text(
            f"📦 *{name} hinzufügen* – Menge festlegen:",
            parse_mode="Markdown",
            reply_markup=_build_new_inv_keyboard(0.0, "Stück"),
        )
        return

    from inventory.manager import add_to_inventory
    add_to_inventory([{
        "name": name, "category": "Sonstiges",
        "quantity": qty, "unit": unit_str, "shelf_life": "mittel",
    }], exact=True)

    from shop.cart import get_cart_state_items, clear_ingredient_from_cart_state
    cart_state = get_cart_state_items()
    matching = [
        cs for cs in cart_state
        if cs.get("ingredient_name", "").lower() == name.lower()
        or name.lower() in cs.get("product_name", "").lower()
    ]

    if not matching:
        qty_str = f"{qty:g} {unit_str} " if unit else ""
        await update.message.reply_text(
            f"✅ *{qty_str}{name}* zum Vorrat hinzugefügt.\n"
            "_Kein entsprechender Artikel im Warenkorb gefunden._",
            parse_mode="Markdown",
        )
        return

    product_name = matching[0].get("product_name", name)
    await update.message.reply_text(
        f"⏳ Vorrat aktualisiert – entferne *{product_name}* aus dem Warenkorb...",
        parse_mode="Markdown",
    )

    from shop.browser import BrowserSession
    from shop.cart import remove_cart_item
    try:
        async with BrowserSession() as session:
            await session.login()
            result = await remove_cart_item(session, product_name)

        clear_ingredient_from_cart_state(name)

        if result:
            product_name_actual, action, new_qty = result
            if action == "decreased":
                msg = (f"✅ *{name}* zum Vorrat hinzugefügt.\n"
                       f"Warenkorb: _{product_name_actual}_ Menge auf {new_qty} reduziert.")
            else:
                msg = (f"✅ *{name}* zum Vorrat hinzugefügt "
                       f"und *{product_name_actual}* aus dem Warenkorb entfernt.")
        else:
            msg = (f"✅ *{name}* zum Vorrat hinzugefügt.\n"
                   "⚠️ Konnte nicht aus dem Warenkorb entfernen – bitte manuell prüfen.")

        await update.message.reply_text(msg, parse_mode="Markdown")

    except Exception as e:
        logger.exception("Fehler in /vorrat")
        await update.message.reply_text(
            f"✅ Vorrat aktualisiert. Fehler beim Warenkorb-Update: `{e}`",
            parse_mode="Markdown",
        )


async def handle_inventory_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Verarbeitet ➕ ➖ 🗑️ Klicks in der Vorrats-Übersicht."""
    query = update.callback_query
    await query.answer()
    if query.data == "inv_noop":
        return

    # "inv_add_5" / "inv_sub_5" / "inv_del_5"
    _, action, item_id_str = query.data.split("_", 2)
    item_id = int(item_id_str)

    from inventory.manager import update_inventory_qty, delete_inventory_item

    if action == "del":
        name = delete_inventory_item(item_id)
        if name:
            await query.answer(f"🗑️ {name} gelöscht", show_alert=False)
    else:
        # Einheit aus DB lesen → unit-passende Schrittweite
        result = update_inventory_qty(item_id, 0)  # peek: delta=0 → nur lesen
        unit = result[2] if result else "Stück"
        delta = _get_inv_delta(unit) * (1 if action == "add" else -1)
        result = update_inventory_qty(item_id, delta)
        if result:
            name, new_qty, unit = result
            await query.answer(f"{name}: {new_qty:g} {unit}", show_alert=False)

    await _send_inventory(query, edit=True)


async def handle_new_inv_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Verarbeitet den interaktiven Mengen-Picker für neue Vorrats-Einträge."""
    query = update.callback_query
    await query.answer()
    data = query.data  # invn_add / invn_sub / invn_save / invn_cart / invn_cancel / invn_u_kg ...

    state = context.user_data.get("new_inv")
    if not state:
        await query.edit_message_text("⚠️ Sitzung abgelaufen – bitte erneut eingeben.")
        return

    name = state["name"]
    qty  = state["qty"]
    unit = state["unit"]

    if data == "invn_cancel":
        context.user_data.pop("new_inv", None)
        await query.edit_message_text(f"❌ Abgebrochen.")
        return

    if data.startswith("invn_u_"):
        key = data[len("invn_u_"):]
        unit = _INV_UNIT_KEY.get(key, key)   # ASCII-Key → lesbarer Name
        # qty auf neues Delta runden (damit kein krummer Wert bleibt)
        delta = _get_inv_delta(unit)
        qty = round(qty / delta) * delta if qty > 0 else 0.0

    elif data == "invn_add":
        qty = max(0.0, qty + _get_inv_delta(unit))

    elif data == "invn_sub":
        qty = max(0.0, qty - _get_inv_delta(unit))

    elif data == "invn_save":
        if qty <= 0:
            await query.answer("Bitte zuerst eine Menge > 0 eingeben.", show_alert=True)
            return
        from inventory.manager import add_to_inventory
        add_to_inventory([{
            "name": name, "category": "Sonstiges",
            "quantity": qty, "unit": unit, "shelf_life": "mittel",
        }], exact=True)
        context.user_data.pop("new_inv", None)

        # Warenkorb prüfen und Artikel ggf. entfernen
        from shop.cart import get_cart_state_items, clear_ingredient_from_cart_state
        cart_state = get_cart_state_items()
        matching = [cs for cs in cart_state
                    if cs.get("ingredient_name", "").lower() == name.lower()
                    or name.lower() in cs.get("product_name", "").lower()]

        if matching:
            product_name = matching[0].get("product_name", name)
            await query.edit_message_text(
                f"✅ *{name}* gespeichert – entferne *{product_name}* aus dem Warenkorb...",
                parse_mode="Markdown",
            )
            from shop.browser import BrowserSession
            from shop.cart import remove_cart_item
            try:
                async with BrowserSession() as session:
                    await session.login()
                    await remove_cart_item(session, product_name)
                clear_ingredient_from_cart_state(name)
            except Exception as e:
                logger.warning("Warenkorb-Entfernung nach Picker-Save fehlgeschlagen: %s", e)

        await query.answer(f"✅ {name} gespeichert", show_alert=False)
        await _send_inventory(query, edit=True)
        return

    elif data == "invn_cart":
        # Nur aus Warenkorb entfernen, nicht zum Vorrat hinzufügen
        context.user_data.pop("new_inv", None)
        from shop.cart import get_cart_state_items, clear_ingredient_from_cart_state
        cart_state = get_cart_state_items()
        matching = [cs for cs in cart_state
                    if cs.get("ingredient_name", "").lower() == name.lower()
                    or name.lower() in cs.get("product_name", "").lower()]
        if not matching:
            await query.edit_message_text(f"❓ *{name}* nicht im Warenkorb gefunden.", parse_mode="Markdown")
            return
        product_name = matching[0].get("product_name", name)
        await query.edit_message_text(f"⏳ Entferne *{product_name}* aus dem Warenkorb...", parse_mode="Markdown")
        from shop.browser import BrowserSession
        from shop.cart import remove_cart_item
        try:
            async with BrowserSession() as session:
                await session.login()
                result = await remove_cart_item(session, product_name)
            clear_ingredient_from_cart_state(name)
            msg = (f"✅ *{product_name}* aus dem Warenkorb entfernt."
                   if result else f"⚠️ Konnte *{product_name}* nicht aus dem Warenkorb entfernen.")
            await query.edit_message_text(msg, parse_mode="Markdown")
        except Exception as e:
            await query.edit_message_text(f"❌ Fehler: {e}")
        return

    # Zustand aktualisieren und Picker neu zeichnen
    context.user_data["new_inv"] = {"name": name, "qty": qty, "unit": unit}
    await query.edit_message_text(
        f"📦 *{name} hinzufügen* – Menge festlegen:",
        parse_mode="Markdown",
        reply_markup=_build_new_inv_keyboard(qty, unit),
    )


async def cmd_liste(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Zeigt aggregiert alle Zutaten, die noch separat gekauft werden müssen."""
    if not _is_allowed(update):
        return

    from inventory.manager import get_missing_items
    items = get_missing_items()

    if not items:
        await update.message.reply_text(
            "✅ *Keine fehlenden Artikel* – alles im Warenkorb oder Vorrat!"
            "\n\n_Falls du gerade erst bestellt hast, wird die Liste nach dem nächsten Rezept gefüllt._",
            parse_mode="Markdown",
        )
        return

    not_found = [i for i in items if i["status"] == "not_found"]
    too_expensive = [i for i in items if i["status"] == "too_expensive"]

    lines = ["🛒 *Noch separat kaufen:*"]

    if not_found:
        lines.append("\n*Nicht im Böck-Shop gefunden:*")
        for i in not_found:
            lines.append(f"  · {i['ingredient_name']}")

    if too_expensive:
        lines.append("\n*Zu teuer / übersprungen:*")
        for i in too_expensive:
            note = f" _({i['notes']})_" if i.get("notes") else ""
            lines.append(f"  · {i['ingredient_name']}{note}")

    lines.append(
        "\n_Mit `/rein Artikelname` trotzdem in den Warenkorb legen._"
    )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_leeren(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Leert den internen Cart-State (nach einer manuell platzierten Bestellung aufrufen)."""
    if not _is_allowed(update):
        return
    from shop.cart import clear_cart_state
    count = clear_cart_state()
    if count:
        await update.message.reply_text(
            f"🗑 Cart-Protokoll geleert ({count} Einträge).\n"
            "Der Bot merkt sich jetzt keine vorherigen Artikel mehr – beim nächsten Rezept startet er frisch."
        )
    else:
        await update.message.reply_text("ℹ️ Cart-Protokoll war bereits leer.")


async def cmd_warenkorb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Zeigt was der Bot aktuell als 'im Warenkorb' gespeichert hat."""
    if not _is_allowed(update):
        return
    from shop.cart import get_cart_state_items
    items = get_cart_state_items()
    if not items:
        await update.message.reply_text(
            "📭 Kein Cart-Protokoll vorhanden.\n"
            "Entweder wurde noch nichts bestellt oder der Cart wurde mit /leeren geleert."
        )
        return
    lines = [f"🛒 *{len(items)} Artikel im Bot-Protokoll:*"]
    for item in items:
        lines.append(f"  · {item['product_name']}")
    lines.append("\n_Tipp: Nach manueller Bestellung /leeren aufrufen._")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_raus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Entfernt einen Artikel aus dem Warenkorb. Syntax: /raus <Artikelname>"""
    if not _is_allowed(update):
        return
    name = " ".join(context.args) if context.args else ""
    if not name:
        await update.message.reply_text("Bitte Artikelname angeben, z.B.: /raus Knoblauch")
        return

    await update.message.reply_text(f"🗑 Entferne '{name}' aus dem Warenkorb...")

    from shop.browser import BrowserSession
    from shop.cart import remove_cart_item

    try:
        async with BrowserSession() as session:
            await session.login()
            result = await remove_cart_item(session, name)
        if result:
            product_name, action, new_qty = result
            if action == "decreased":
                await update.message.reply_text(
                    f"✅ Menge von '{product_name}' auf {new_qty} reduziert."
                )
            else:
                await update.message.reply_text(
                    f"✅ '{product_name}' wurde aus dem Warenkorb entfernt."
                )
        else:
            await update.message.reply_text(
                f"❌ Kein Artikel mit '{name}' im Warenkorb gefunden.\n"
                "Bitte überprüfe den Namen (Groß/Kleinschreibung egal)."
            )
    except Exception as e:
        logger.exception("Fehler bei /raus")
        await update.message.reply_text(f"❌ Fehler: {e}")


async def cmd_rein(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sucht einen Artikel und legt ihn in den Warenkorb. Syntax: /rein <Zutat>"""
    if not _is_allowed(update):
        return
    name = " ".join(context.args) if context.args else ""
    if not name:
        await update.message.reply_text("Bitte Artikelname angeben, z.B.: /rein Karotten")
        return

    await update.message.reply_text(f"🔍 Suche '{name}' und füge zum Warenkorb hinzu...")

    from shop.browser import BrowserSession
    from shop.search import search_product, match_best_product
    from shop.cart import fill_cart

    ingredient = {"name": name, "quantity": 1, "unit": "Stk", "category": "default"}

    try:
        async with BrowserSession() as session:
            await session.login()
            candidates = await search_product(session, ingredient)
            if not candidates:
                await update.message.reply_text(f"❌ Kein Produkt für '{name}' gefunden.")
                return
            # Preisfilter absichtlich umgehen – User hat explizit bestellt
            best = await match_best_product(ingredient, candidates, skip_price_check=True)
            if not best:
                await update.message.reply_text(f"❌ Kein passendes Produkt für '{name}' gefunden.")
                return
            cart_url, _, __ = await fill_cart(session, [best])

        price_str = f"{best.get('price')} €" if best.get("price") else "Preis unbekannt"
        await update.message.reply_text(
            f"✅ *{best['name']}* zum Warenkorb hinzugefügt.\n"
            f"Preis: {price_str}\n"
            f"🔗 [Warenkorb prüfen]({cart_url})",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.exception("Fehler bei /rein")
        await update.message.reply_text(f"❌ Fehler: {e}")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return

    text = update.message.text or ""
    logger.info("Textnachricht von Chat %d: %s", update.effective_chat.id, text[:80])

    await update.message.reply_text("🔄 Rezept empfangen, wird verarbeitet...")

    # Pipeline aufrufen (wird in späteren Modulen implementiert)
    await _process_recipe(update, context, text=text, image_bytes=None)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return

    logger.info("Foto von Chat %d empfangen", update.effective_chat.id)
    await update.message.reply_text("🔄 Foto empfangen, wird analysiert...")

    # Größtes verfügbares Foto herunterladen
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    image_bytes = await file.download_as_bytearray()

    caption = update.message.caption or ""
    await _process_recipe(update, context, text=caption, image_bytes=bytes(image_bytes))


async def _send_price_warnings(update: Update, cart_items: list[dict]) -> None:
    """Läuft im Hintergrund: Preisvergleich via Claude Web Search, dann Folgenachricht."""
    import asyncio as _asyncio
    from shop.price_check import check_price_markup

    # Artikel mit Preis (price_per_kg ODER konkretem Stückpreis berechenbar)
    candidates = [i for i in cart_items if i.get("price_per_kg") or i.get("price")]
    if not candidates:
        return

    tasks = [
        check_price_markup(i["name"], i.get("price_per_kg"), boeck_item=i)
        for i in candidates
    ]
    results = await _asyncio.gather(*tasks, return_exceptions=True)

    warnings = {
        item["name"]: w
        for item, w in zip(candidates, results)
        if isinstance(w, dict)
    }
    if not warnings:
        return

    # Nur den Preisvergleich-Block ausgeben
    lines = ["📊 *Preisvergleich:*"]
    for product_name, w in warnings.items():
        boeck = w["boeck_price"]
        ref = w["ref_price"]
        unit = w.get("unit", "kg")
        diff = w["diff_pct"] * 100
        ref_product = w.get("ref_product", "")
        source = w.get("source", "Supermarkt")
        ref_str = f"\n    ↳ _{ref_product}_" if ref_product and ref_product.lower() != product_name.lower() else ""
        lines.append(
            f"  · *{product_name}*: {boeck:.2f} €/{unit} bei Böck "
            f"vs. {ref:.2f} €/{unit} bei {source} (+{diff:.0f}%){ref_str}"
        )

    try:
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        logger.warning("Preisvergleich-Nachricht fehlgeschlagen: %s", e)


async def _process_recipe(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    image_bytes: bytes | None,
) -> None:
    """
    Vollständige Pipeline: Parser → Vorrat → Shop → Report → DB.
    """
    from parser.recipe_parser import parse_recipe, merge_ingredients
    from inventory.manager import deduct_inventory
    from shop.search import find_and_fill_cart
    from utils.report import format_report, save_order

    try:
        # 1. Zutaten parsen + zusammenführen (verhindert Doppelbestellungen)
        ingredients = merge_ingredients(await parse_recipe(text, image_bytes))
        if not ingredients:
            await update.message.reply_text(
                "❌ Ich konnte keine Zutaten in deiner Nachricht finden.\n"
                "Bitte schick ein Rezept als Text, Foto oder Link."
            )
            return

        # Vorschau – alle Zutaten anzeigen
        preview_lines = [f"• {i['quantity']} {i['unit']} {i['name']}" for i in ingredients]
        await update.message.reply_text(
            f"✅ *{len(ingredients)} Zutaten erkannt:*\n" + "\n".join(preview_lines) +
            "\n\n⏳ Prüfe Vorrat und befülle Warenkorb...",
            parse_mode="Markdown",
        )

        # 2+3. Interims-Vorrat + Shop (unified):
        # find_and_fill_cart prüft Inventar + Warenkorb und bestellt nur Fehlmengen
        result = await find_and_fill_cart(ingredients)
        from_inventory = result.get("from_inventory", [])
        if from_inventory:
            logger.info("Aus Vorrat: %s", ", ".join(i["name"] for i in from_inventory))

        # 4. Vorrat abziehen
        if from_inventory:
            deduct_inventory(from_inventory)

        # 5. Report sofort schicken (ohne Preisvergleich – der kommt als Folgenachricht)
        report = format_report(
            cart_items=result.get("cart_items", []),
            not_found=result.get("not_found", []),
            too_expensive=result.get("too_expensive", []),
            from_inventory=from_inventory,
            cart_url=result.get("cart_url", ""),
            total=result.get("total", 0.0),
            already_in_cart=result.get("already_in_cart", []),
        )

        # 6. Bestellung in DB speichern + cart_state verknüpfen
        order_id = save_order(
            cart_items=result.get("cart_items", []),
            not_found=result.get("not_found", []),
            too_expensive=result.get("too_expensive", []),
            from_inventory=from_inventory,
            cart_url=result.get("cart_url", ""),
            total=result.get("total", 0.0),
        )
        from shop.cart import link_cart_state_to_order
        link_cart_state_to_order(order_id)

        await update.message.reply_text(report, parse_mode="Markdown")

        # 7. Preisvergleich im Hintergrund – kommt als separate Folgenachricht
        cart_items = result.get("cart_items", [])
        if cart_items:
            asyncio.create_task(
                _send_price_warnings(update, cart_items)
            )

        # Unsichere Artikel: Inline-Buttons zur Bestätigung
        uncertain = result.get("uncertain", [])
        if uncertain:
            context.bot_data.setdefault("pending_items", {})
            for item in uncertain:
                uid = uuid.uuid4().hex[:12]
                context.bot_data["pending_items"][uid] = item

                ingredient_name = item.get("ingredient_name", item.get("name", "?"))
                product_name = item.get("name", "?")
                price = item.get("price")
                price_str = f" – {str(price).replace('.', ',')} €" if price else ""

                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Hinzufügen", callback_data=f"cart_add_{uid}"),
                    InlineKeyboardButton("❌ Überspringen", callback_data=f"cart_skip_{uid}"),
                ]])
                await update.message.reply_text(
                    f"❓ *Passt das?*\n"
                    f"Gesucht: _{ingredient_name}_\n"
                    f"Gefunden: *{product_name}*{price_str}",
                    parse_mode="Markdown",
                    reply_markup=keyboard,
                )

    except Exception as e:
        logger.exception("Fehler in der Pipeline")
        await update.message.reply_text(
            f"❌ Fehler bei der Verarbeitung:\n`{e}`\n\n"
            "Bitte versuche es erneut oder kontaktiere den Admin.",
            parse_mode="Markdown",
        )


async def handle_cart_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Verarbeitet Inline-Button-Klicks für unsichere Artikel (Hinzufügen / Überspringen)."""
    query = update.callback_query
    await query.answer()

    data = query.data  # "cart_add_<uid>" oder "cart_skip_<uid>"
    parts = data.split("_", 2)
    action = parts[1]
    uid = parts[2]

    pending = context.bot_data.get("pending_items", {})
    item = pending.pop(uid, None)  # Aus Dict entfernen → kein Memory-Leak
    if item is None:
        await query.edit_message_text("❌ Artikel nicht mehr verfügbar (Bot neu gestartet?).")
        return

    product_name = item.get("name", "?")

    if action == "skip":
        await query.edit_message_text(f"❌ _{product_name}_ – übersprungen.", parse_mode="Markdown")
        return

    # Hinzufügen
    await query.edit_message_text(f"⏳ Füge _{product_name}_ zum Warenkorb hinzu...", parse_mode="Markdown")

    from shop.browser import BrowserSession
    from shop.cart import fill_cart

    try:
        async with BrowserSession() as session:
            await session.login()
            cart_url, _, __ = await fill_cart(session, [item])

        price = item.get("price")
        price_str = f" – {str(price).replace('.', ',')} €" if price else ""
        await query.edit_message_text(
            f"✅ *{product_name}*{price_str} zum Warenkorb hinzugefügt.\n"
            f"🔗 [Warenkorb prüfen]({cart_url})",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.exception("Fehler beim Callback-Hinzufügen")
        await query.edit_message_text(f"❌ Fehler: {e}")


def build_application(token: str) -> Application:
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("hilfe", cmd_hilfe))
    app.add_handler(CommandHandler("help", cmd_hilfe))
    app.add_handler(CommandHandler("vorrat", cmd_vorrat))
    app.add_handler(CommandHandler("liste", cmd_liste))
    app.add_handler(CommandHandler("raus", cmd_raus))
    app.add_handler(CommandHandler("rein", cmd_rein))
    app.add_handler(CommandHandler("leeren", cmd_leeren))
    app.add_handler(CommandHandler("warenkorb", cmd_warenkorb))
    app.add_handler(CallbackQueryHandler(handle_cart_callback, pattern=r"^cart_(add|skip)_\d+$"))
    app.add_handler(CallbackQueryHandler(handle_inventory_callback, pattern=r"^inv_(add|sub|del|noop)_?\d*$"))
    app.add_handler(CallbackQueryHandler(handle_new_inv_callback,   pattern=r"^invn_"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    return app
