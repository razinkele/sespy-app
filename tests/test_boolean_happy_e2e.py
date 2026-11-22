"""E2E for the Boolean / Laplacian module — multi-attractor happy path.

Companion to ``tests/test_boolean_e2e.py`` (the default sample's all-off
result) and ``tests/test_boolean_sampled_e2e.py`` (the sampled fallback). This
test loads the ``Minimal Demo`` 5-node template, whose whole network is its
feedback core (a positive 3-cycle plus a mutually activating pair), and checks
the exact feedback-core search end to end:
  - Eigenvalue plot still renders.
  - Stability summary populates.
  - Boolean tab shows the ``core_info`` line and a table with at least two
    attractor rows (no warning, no danger alert). The search runs in a
    background task, so the test waits for its result, not a fixed delay.
"""
import asyncio

from playwright.async_api import async_playwright


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await (await browser.new_context()).new_page()
        await page.set_viewport_size({"width": 1280, "height": 900})
        await page.goto("http://127.0.0.1:8000", wait_until="networkidle")

        # --- Step 1: load the Minimal Demo template via the Templates panel.
        await page.wait_for_selector("#sespy_nav_templates", timeout=60000)
        await page.click("#sespy_nav_templates")
        await page.wait_for_timeout(2500)

        # Locate the Minimal Demo card by name and click its Load button.
        # Templates are alphabetically sorted; index isn't load-bearing here —
        # find the card by header text and click the button beside it.
        cards = await page.evaluate(
            "() => Array.from(document.querySelectorAll('#templates-templates_list h5'))"
            ".map(e => e.textContent.trim())"
        )
        print("templates listed:", cards)
        assert "Minimal Demo" in cards, \
            f"Minimal Demo template missing from list: {cards}"
        minimal_idx = cards.index("Minimal Demo")
        load_btn = f"#templates-load_template_{minimal_idx}"
        # The h5s render before Shiny binds the buttons; an unbound click is lost.
        await page.wait_for_selector(f"{load_btn}.shiny-bound-input", timeout=15000)
        await page.click(load_btn)
        await page.wait_for_timeout(2500)

        # --- Step 2: run the Boolean analysis on the loaded 5-node SES.
        await page.click("#sespy_nav_boolean")
        await page.wait_for_timeout(1500)

        # The pre-run placeholder figure ("Click 'Run analysis' to compute
        # results.") is ALSO an <img> (analysis_boolean.py:136-143), and so is
        # the error branch (144-149) — shiny encodes every Figure as
        # "data:image/png;base64,..." — so "an <img> exists" cannot tell a real
        # render from a placeholder. Stash the placeholder's src IN THE PAGE
        # (keeps ~100 KB off the CDP bridge) and require the src to change.
        # The plot is in the default-visible Laplacian tab, so it renders the
        # placeholder at the nav click above; matplotlib's import happens inside
        # the render function, so allow a generous first-render budget.
        await page.wait_for_selector("#boolean-eigenvalue_plot img", timeout=60000)
        await page.evaluate(
            "() => { window.__sespy_plot_src0 = "
            "document.querySelector('#boolean-eigenvalue_plot img').src; }"
        )

        await page.click("#boolean-run_boolean")

        # Eigenvalue plot re-rendered with real results: render.plot returns a
        # content-derived "data:image/png;base64,..." src, so it changes.
        await page.wait_for_function(
            "() => {"
            "  const i = document.querySelector('#boolean-eigenvalue_plot img');"
            "  return !!i && i.src !== window.__sespy_plot_src0;"
            "}",
            timeout=60000,
        )
        plot_visible = await page.evaluate(
            "() => {"
            "  const i = document.querySelector('#boolean-eigenvalue_plot img');"
            "  return !!i && i.src !== window.__sespy_plot_src0;"
            "}"
        )
        print(f"eigenvalue plot rendered: {plot_visible}")
        assert plot_visible, \
            "eigenvalue plot did not render (still the pre-run placeholder figure)"

        # Stability summary populated. This is the independent non-vacuous
        # witness for a successful run (its pre-run placeholder is a bare <p>,
        # analysis_boolean.py:165) — and it replaces the fixed sleep this block
        # removed, so this change stands alone. Same pattern as
        # tests/test_boolean_e2e.py:47-50.
        await page.wait_for_function(
            "() => document.querySelectorAll('#boolean-stability_summary dl dt').length >= 3",
            timeout=30000,
        )
        n_dt = await page.evaluate(
            "() => document.querySelectorAll('#boolean-stability_summary dl dt').length"
        )
        print(f"stability summary <dt> count: {n_dt}")
        assert n_dt >= 3, f"expected stability summary with >=3 fields, got {n_dt}"

        # Switch to Boolean attractors tab
        # Scope to this module's navset, never a bare text= match: every panel
        # shares one DOM, so a future label elsewhere would hijack this click.
        await page.click("#boolean-boolean_tabs a[data-value='Boolean attractors']")
        # Wait for the background search's FINAL content, not a fixed sleep.
        # Deliberately a neutral gate (a table row OR any alert), not
        # "table tbody tr" alone: gating on the table would pre-empt the
        # alert assertions below, so a warning regressed onto a 5-node network
        # would die on an opaque selector timeout instead of failing with a
        # clear message. The "Computing attractors…" and "Click 'Run
        # analysis'" placeholders are bare <p>s and do not satisfy it.
        await page.wait_for_selector(
            "#boolean-attractor_panel table tbody tr, #boolean-attractor_panel .alert",
            timeout=60000,
        )

        # No warning: the 5-node core is far inside the exact limit.
        warning_count = await page.evaluate(
            "() => document.querySelectorAll('#boolean-attractor_panel .alert-warning').length"
        )
        print(f"alert-warning count (should be 0): {warning_count}")
        assert warning_count == 0, \
            "unexpected warning on a 5-node network"

        # No danger alert anywhere on the page.
        danger_count = await page.evaluate(
            "() => document.querySelectorAll('.alert-danger').length"
        )
        print(f"alert-danger count: {danger_count}")
        assert danger_count == 0, f"unexpected error alert(s) present: {danger_count}"

        # core_info header line: whole network and its feedback core.
        core_info = await page.evaluate(
            "() => {"
            "  const e = document.querySelector('#boolean-attractor_panel .boolean-core-info');"
            "  return e ? e.textContent.trim() : null;"
            "}"
        )
        print(f"core info: {core_info!r}")
        assert (core_info is not None and "5 nodes" in core_info
                and "5 in the feedback core" in core_info
                and "basin sizes exact" in core_info), \
            f"core_info line missing or wrong: {core_info}"

        # Attractor table populated with several attractors (count rows on
        # the table element itself).
        n_rows = await page.evaluate(
            "() => document.querySelectorAll("
            "  '#boolean-attractor_panel table tbody tr'"
            ").length"
        )
        print(f"attractor table rows: {n_rows}")
        assert n_rows >= 2, \
            f"expected >=2 attractor rows in table, got {n_rows}"

        await page.screenshot(path="tests/screenshots/boolean_happy.png")
        print("\nboolean happy-path e2e assertions pass")
        await browser.close()


asyncio.run(main())
