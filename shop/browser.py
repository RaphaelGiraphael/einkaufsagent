"""
Playwright Browser-Session für gemuese-bestellen.de.

Sicherheitsschicht: Alle page.goto()-Aufrufe laufen durch _check_url().
Checkout-URLs sind hart blockiert – keine Ausnahmen.
"""

import logging
import os
from types import TracebackType
from typing import Self

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://gemuese-bestellen.de"

ALLOWED_URL_PATTERNS = [
    "gemuese-bestellen.de/search",
    "gemuese-bestellen.de/gemuese/",
    "gemuese-bestellen.de/obst/",
    "gemuese-bestellen.de/partner-produkte/",
    "gemuese-bestellen.de/ur-",
    "gemuese-bestellen.de/checkout/cart",       # NUR zum Anzeigen
    "gemuese-bestellen.de/checkout/line-item/add",  # Warenkorb befüllen
    "gemuese-bestellen.de/account/login",
    "gemuese-bestellen.de/",                    # Startseite + allgemeine Produktseiten
]

BLOCKED_URL_PATTERNS = [
    "checkout/confirm",
    "checkout/finish",
    "checkout/order",
    "order/complete",
    "order/finish",
]


class CheckoutBlockedError(Exception):
    """Wird geworfen wenn versucht wird eine Checkout-URL aufzurufen."""


def _check_url(url: str) -> None:
    """
    Prüft ob eine URL erlaubt ist.
    Raises CheckoutBlockedError bei blockierten Checkout-URLs.
    """
    url_lower = url.lower()
    for blocked in BLOCKED_URL_PATTERNS:
        if blocked in url_lower:
            raise CheckoutBlockedError(
                f"SICHERHEIT: Checkout-URL blockiert: {url}\n"
                "Der Bot darf keinen Checkout durchführen."
            )


class BrowserSession:
    """
    Async Context Manager für eine Playwright-Session auf gemuese-bestellen.de.

    Verwendung:
        async with BrowserSession() as session:
            products = await session.search("Karotten")
    """

    def __init__(self, headless: bool | None = None):
        env_headless = os.getenv("PLAYWRIGHT_HEADLESS", "true").lower()
        self.headless = headless if headless is not None else (env_headless != "false")
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self.page: Page | None = None
        self._logged_in = False

    async def __aenter__(self) -> Self:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self.headless)
        self._context = await self._browser.new_context(
            user_agent="Mozilla/5.0 (compatible; BoeckAgent/1.0)",
            locale="de-DE",
        )
        self.page = await self._context.new_page()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.debug("Browser-Session beendet")

    async def goto(self, url: str, **kwargs) -> None:
        """Navigiert zu einer URL – blockiert Checkout-Seiten."""
        _check_url(url)
        await self.page.goto(url, **kwargs)

    async def login(self) -> bool:
        """
        Meldet sich mit den Böck-Credentials aus .env an.
        Gibt True zurück wenn Login erfolgreich.
        """
        if self._logged_in:
            return True

        username = os.getenv("BOECK_USERNAME")
        password = os.getenv("BOECK_PASSWORD")

        if not username or not password:
            logger.warning("BOECK_USERNAME oder BOECK_PASSWORD nicht gesetzt – nicht eingeloggt")
            return False

        try:
            await self.goto(f"{BASE_URL}/account/login", timeout=15000)
            await self.page.wait_for_load_state("domcontentloaded")

            # Cookie-Consent-Modal wegklicken (blockiert sonst alle Klicks)
            await self._dismiss_cookie_consent()

            # Shopware 6 Login-Formular (username-Feld heißt "username")
            await self.page.fill('input[name="username"]', username)
            await self.page.fill('input[name="password"]', password)
            # Nur Login-Button klicken, nicht den Header-Such-Button
            await self.page.click('form[action="/account/login"] button[type="submit"]')
            await self.page.wait_for_load_state("domcontentloaded")

            # Prüfen ob Login erfolgreich (Shopware leitet auf /account weiter)
            current_url = self.page.url
            if "/account" in current_url and "login" not in current_url:
                self._logged_in = True
                logger.info("Login erfolgreich")
                return True
            else:
                logger.warning("Login fehlgeschlagen (URL: %s)", current_url)
                return False

        except Exception as e:
            logger.error("Login-Fehler: %s", e)
            return False

    async def _dismiss_cookie_consent(self) -> None:
        """Klickt den Cookie-Consent-Dialog weg, falls vorhanden."""
        consent_selectors = [
            # Acris Cookie Consent (auf gemuese-bestellen.de)
            "button[data-cookie-accept-all]",
            ".acris-cookie-consent button.btn-primary",
            "#ccAcivateModal button.btn-primary",
            "button.js-accept-all-cookie",
            # Generische Fallbacks
            "button[data-testid='uc-accept-all-button']",
            ".cookie-bar button, .cookie-consent button",
        ]
        for sel in consent_selectors:
            try:
                btn = await self.page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click(timeout=3000)
                    await self.page.wait_for_load_state("domcontentloaded", timeout=3000)
                    logger.debug("Cookie-Consent geschlossen: %s", sel)
                    return
            except Exception:
                continue

        # Fallback: Modal per JS ausblenden
        try:
            await self.page.evaluate("""
                document.querySelectorAll('.modal-backdrop, .acris-cookie-consent').forEach(el => el.remove());
                document.body.classList.remove('modal-open');
            """)
        except Exception:
            pass

    async def get_cart_url(self) -> str:
        """Gibt die URL zum aktuellen Warenkorb zurück."""
        return f"{BASE_URL}/checkout/cart"
