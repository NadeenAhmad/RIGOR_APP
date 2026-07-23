from pathlib import Path
from playwright.sync_api import sync_playwright, expect

APP_URL = "http://127.0.0.1:7860"
OUTPUT = Path("gradio-filled.png")

with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=True,
    )

    page = browser.new_page(
        viewport={"width": 1600, "height": 1000},
        device_scale_factor=2,
    )

    # Do not use networkidle with Gradio.
    page.goto(
        APP_URL,
        wait_until="domcontentloaded",
        timeout=60_000,
    )

    # Wait for Gradio itself to render.
    page.locator("gradio-app").wait_for(
        state="visible",
        timeout=60_000,
    )

    # Replace these selectors with your actual component elem_ids.
    text_input = page.locator("#name-field input")
    checkbox = page.locator(
        "#references-field input[type='checkbox']"
    )

    text_input.fill("Example text")
    checkbox.check()

    expect(text_input).to_have_value("Example text")
    expect(checkbox).to_be_checked()

    # Remove focus from the textbox.
    page.locator("body").press("Escape")

    # Wait for fonts and one final UI repaint.
    page.evaluate("document.fonts.ready")
    page.wait_for_timeout(500)

    page.screenshot(
        path=str(OUTPUT),
        full_page=True,
        animations="disabled",
    )

    browser.close()

print(f"Saved screenshot: {OUTPUT.resolve()}")