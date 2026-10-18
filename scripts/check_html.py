"""Render fixture reports in installed Edge and verify interactions and four responsive screenshots."""

from collections.abc import Callable
from pathlib import Path
from typing import Literal

from playwright.sync_api import Error, sync_playwright

EXPECTED_RENAMES = 2
THEMES: tuple[Literal["light", "dark"], ...] = ("light", "dark")


def error_collector(errors: list[Error]) -> Callable[[Error], None]:
    def record(error: Error) -> None:
        errors.append(error)

    return record


def main() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        for width, height, label in [(1440, 1050, "desktop"), (390, 844, "phone")]:
            for theme in THEMES:
                page = browser.new_page(viewport={"width": width, "height": height}, color_scheme=theme)
                errors: list[Error] = []
                page.on("pageerror", error_collector(errors))
                page.goto(Path("examples/py-cli.html").resolve().as_uri())
                page.wait_for_function("typeof cy !== 'undefined' && cy.nodes().length > 0")
                assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
                page.screenshot(path=f"examples/py-cli-{label}-{theme}.png", full_page=True)
                page.locator('[data-filter="renamed"]').click()
                assert page.locator(".card:visible").count() == EXPECTED_RENAMES
                page.locator("#reset").click()
                page.locator(".card").first.focus()
                page.keyboard.press("j")
                page.keyboard.press("Enter")
                assert page.locator(".card.selected details[open]").count() == 1
                page.locator("#insights-tab").click()
                assert page.locator("#insights-view").is_visible()
                page.locator("#theme").click()
                assert not errors, errors
                print(f"{label} {theme}: graph, filters, keyboard, themes and overflow passed.")
                page.close()
        browser.close()


if __name__ == "__main__":
    main()
