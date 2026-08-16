"""Small Playwright lifecycle wrapper for dynamic job pages.

Resolvers use this instead of creating browsers directly so headed/headless mode
and browser cleanup are controlled consistently from environment variables.
"""

from __future__ import annotations

import os
from types import TracebackType

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)


def env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)

    if value is None:
        return default

    return value.strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


class PlaywrightClient:
    def __init__(
        self,
        headless: bool | None = None,
        timeout_ms: int = 30_000,
    ) -> None:
        self.headless = (
            env_flag("PLAYWRIGHT_HEADLESS", True)
            if headless is None
            else headless
        )
        self.timeout_ms = timeout_ms
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    async def __aenter__(self) -> PlaywrightClient:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    async def start(self) -> None:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
        )
        self._context = await self._browser.new_context(
            viewport={
                "width": 1440,
                "height": 1000,
            },
            user_agent=(
                "Mozilla/5.0 "
                "(Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            ),
        )
        self._context.set_default_timeout(self.timeout_ms)

    async def new_page(self) -> Page:
        if self._context is None:
            raise RuntimeError("PlaywrightClient has not been started.")

        page = await self._context.new_page()
        page.set_default_timeout(self.timeout_ms)

        return page

    async def close(self) -> None:
        if self._context is not None:
            await self._context.close()
            self._context = None

        if self._browser is not None:
            await self._browser.close()
            self._browser = None

        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
