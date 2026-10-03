"""Render using a short-lived local browser, including on error/cancellation."""

from pathlib import Path


async def render_local(template: str, data: dict, destination: Path, browser_path: str = "") -> None:
    # Remote rendering does not need to import Playwright.
    from jinja2.sandbox import SandboxedEnvironment
    from playwright.async_api import async_playwright

    rendered = SandboxedEnvironment().from_string(template).render(**data)
    async with async_playwright() as playwright:
        options = {"headless": True}
        if browser_path:
            options["executable_path"] = browser_path
        browser = await playwright.chromium.launch(**options)
        try:
            context = await browser.new_context(viewport={"width": 760, "height": 800}, device_scale_factor=1.8)
            await context.set_offline(True)
            page = await context.new_page()
            page.set_default_timeout(60000)
            await page.set_content(rendered, wait_until="load")
            await page.wait_for_function(
                "document.fonts.status === 'loaded' && Array.from(document.images).every(image => image.complete)"
            )
            await page.screenshot(path=str(destination), type="png", full_page=True, timeout=60000)
        finally:
            await browser.close()
