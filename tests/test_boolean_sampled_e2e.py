"""E2E for the Boolean / Laplacian module — sampled fallback.

Companion to ``tests/test_boolean_e2e.py`` (all-off) and
``tests/test_boolean_happy_e2e.py`` (exact multi-attractor table). This test
loads the Caribbean MarineSABRES template (78 elements; a 64-node feedback
core, above the exact limit of 20 core nodes), so the background task falls
back to the sampled whole-network search with the fixed arguments
(2000 random starts, seed 0). It checks:
  - the ``core_info`` line names the whole network and its core;
  - the ``core_too_large`` warning names the core size and the limit;
  - the ``sampled_note`` paragraph is shown;
  - the sampled table has at least one attractor row;
  - no danger alert anywhere.

Every selector is scoped to the module's namespaced ids (``#boolean-...``):
all nav panels share one DOM, so a bare ``text=`` match could hit another
panel.
"""
import asyncio

from playwright.async_api import async_playwright

CARIBBEAN = "Caribbean Island - Comprehensive SES Template (MarineSABRES)"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await (await browser.new_context()).new_page()
        await page.set_viewport_size({"width": 1280, "height": 900})
        await page.goto("http://127.0.0.1:8000", wait_until="networkidle")

        # --- Step 1: load the Caribbean template via the Templates panel.
        await page.wait_for_selector("#sespy_nav_templates", timeout=60000)
        await page.click("#sespy_nav_templates")
        await page.wait_for_selector("#templates-templates_list h5", timeout=30000)

        # The card order (h5s, MarineSABRES group first) is the order of the
        # load_template_<i> buttons; find the card by its name.
        cards = await page.evaluate(
            "() => Array.from(document.querySelectorAll('#templates-templates_list h5'))"
            ".map(e => e.textContent.trim())"
        )
        print("templates listed:", cards)
        assert CARIBBEAN in cards, f"Caribbean template missing from list: {cards}"
        load_btn = f"#templates-load_template_{cards.index(CARIBBEAN)}"
        # The h5s render before Shiny binds the buttons; an unbound click is lost.
        await page.wait_for_selector(f"{load_btn}.shiny-bound-input", timeout=15000)
        await page.click(load_btn)
        await page.wait_for_timeout(2500)

        # --- Step 2: run the analysis on the loaded 78-node SES.
        await page.click("#sespy_nav_boolean")
        await page.wait_for_selector("#boolean-run_boolean.shiny-bound-input", timeout=30000)
        await page.click("#boolean-run_boolean")

        # The Laplacian part is synchronous: its stability summary is the
        # witness that this Run reached the server.
        await page.wait_for_function(
            "() => document.querySelectorAll('#boolean-stability_summary dl dt').length >= 3",
            timeout=60000,
        )

        # Scope to this module's navset, never a bare text= match.
        await page.click("#boolean-boolean_tabs a[data-value='Boolean attractors']")

        # Wait for the background task's result (a table row or any alert),
        # not a fixed delay. The sampled search takes about half a second
        # idle; the budget allows for a loaded machine.
        await page.wait_for_selector(
            "#boolean-attractor_panel table tbody tr, #boolean-attractor_panel .alert-danger",
            timeout=90000,
        )

        danger_count = await page.evaluate(
            "() => document.querySelectorAll('.alert-danger').length"
        )
        print(f"alert-danger count: {danger_count}")
        assert danger_count == 0, f"unexpected error alert(s) present: {danger_count}"

        core_info = await page.evaluate(
            "() => {"
            "  const e = document.querySelector('#boolean-attractor_panel .boolean-core-info');"
            "  return e ? e.textContent.trim() : null;"
            "}"
        )
        print(f"core info: {core_info!r}")
        assert (core_info is not None and "78 nodes" in core_info
                and "64 in the feedback core" in core_info), \
            f"core_info line missing or wrong: {core_info}"

        warning = await page.evaluate(
            "() => {"
            "  const w = document.querySelector('#boolean-attractor_panel .alert-warning');"
            "  return w ? w.textContent.trim() : null;"
            "}"
        )
        print(f"core_too_large warning: {warning!r}")
        assert warning is not None, "expected the core_too_large warning"
        assert ("feedback core has 64 nodes" in warning
                and "exact limit of 20" in warning
                and "sampled search" in warning), \
            f"warning text doesn't match core_too_large: {warning}"

        note = await page.evaluate(
            "() => {"
            "  const ps = Array.from(document.querySelectorAll("
            "    '#boolean-attractor_panel p.text-muted'));"
            "  const p = ps.find(e => e.textContent.trim().startsWith('Sampled search:'));"
            "  return p ? p.textContent.trim() : null;"
            "}"
        )
        print(f"sampled note: {note!r}")
        assert note is not None, "expected the sampled_note paragraph"
        assert ("2000 random start states (seed 0)" in note
                and "within 1000 steps" in note), \
            f"sampled note doesn't show the fixed arguments: {note}"

        # Sampled table: count rows on the table element itself.
        n_rows = await page.evaluate(
            "() => document.querySelectorAll("
            "  '#boolean-attractor_panel table tbody tr'"
            ").length"
        )
        print(f"sampled attractor table rows: {n_rows}")
        assert n_rows >= 1, f"expected >=1 attractor row in table, got {n_rows}"

        headers = await page.evaluate(
            "() => Array.from(document.querySelectorAll("
            "  '#boolean-attractor_panel table thead th')).map(e => e.textContent.trim())"
        )
        print(f"sampled table headers: {headers}")
        assert "Hits" in headers and "basin_size" not in headers, \
            f"sampled table should show hits and no basin size: {headers}"

        await page.screenshot(path="tests/screenshots/boolean_sampled.png")
        print("\nboolean sampled-path e2e assertions pass")
        await browser.close()


asyncio.run(main())
