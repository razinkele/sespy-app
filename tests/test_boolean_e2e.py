"""E2E for the Boolean / Laplacian module: the all-off path.

The default sample SES has 17 nodes and a 16-node feedback core, inside the
exact limit of 20 core nodes, so the feedback-core search runs exactly (in a
background task). Its only attractor is the all-off fixed point, so the panel
shows the all-off sentence as an info alert instead of a one-row table:
  - Laplacian eigenvalue plot still renders.
  - Stability summary populates.
  - Boolean tab shows the core_info line and the all-off info alert
    (alert-info), with no table and no warning.
  - No error alert (alert-danger) appears anywhere.

The multi-attractor table is covered by tests/test_boolean_happy_e2e.py and
the sampled fallback by tests/test_boolean_sampled_e2e.py.
"""
import asyncio

from playwright.async_api import async_playwright


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await (await browser.new_context()).new_page()
        await page.set_viewport_size({"width": 1280, "height": 900})
        await page.goto("http://127.0.0.1:8000", wait_until="networkidle")

        await page.wait_for_selector("#sespy_nav_boolean", timeout=60000)
        await page.click("#sespy_nav_boolean")
        # The Laplacian tab's plot output is SUSPENDED while the panel is
        # hidden; when the pane becomes visible it renders a "Click Run"
        # PLACEHOLDER figure that is also an <img>. Wait for that placeholder
        # (strictly stronger than a blind sleep: the <img> exists only after
        # the nav round-trip completed AND the suspended output resumed) and
        # record its src so the post-Run wait can tell the real eigenvalue
        # chart apart from it.
        await page.wait_for_selector("#boolean-eigenvalue_plot img", timeout=60000)
        pre_run_dt = await page.evaluate(
            "() => document.querySelectorAll('#boolean-stability_summary dl dt').length"
        )
        assert pre_run_dt == 0, (
            "plot already shows a post-run figure; placeholder capture invalid "
            f"(stability summary already has {pre_run_dt} <dt>)"
        )
        await page.evaluate(
            "() => { window.__boolPlaceholderSrc ="
            " document.querySelector('#boolean-eigenvalue_plot img').src; }"
        )

        # Run analysis
        await page.click("#boolean-run_boolean")

        # Eigenvalue plot rendered (img tag inside the plot output). An <img>
        # already existed before Run — the "Click Run" placeholder figure — so
        # waiting for the selector alone would pass instantly and prove nothing.
        # Wait instead for the src to CHANGE away from the captured placeholder
        # AND for the stability summary to populate; both only happen after the
        # analysis actually ran. This is the first matplotlib render in the
        # session and the cold import can take several seconds.
        await page.wait_for_function(
            "() => {"
            "  const i = document.querySelector('#boolean-eigenvalue_plot img');"
            "  if (!i || i.src === window.__boolPlaceholderSrc) return false;"
            "  return document.querySelectorAll('#boolean-stability_summary dl dt').length >= 3;"
            "}",
            timeout=60000,
        )
        plot_rendered = await page.evaluate(
            "() => {"
            "  const i = document.querySelector('#boolean-eigenvalue_plot img');"
            "  return !!i && i.src !== window.__boolPlaceholderSrc;"
            "}"
        )
        print(f"eigenvalue plot rendered: {plot_rendered}")
        assert plot_rendered, \
            "eigenvalue plot still shows the pre-run 'Click Run' placeholder"

        # Stability summary populated (a <dl> with at least 3 dt/dd pairs).
        # It renders a tick after the plot, so wait for it to populate rather
        # than asserting immediately.
        await page.wait_for_function(
            "() => document.querySelectorAll('#boolean-stability_summary dl dt').length >= 3",
            timeout=15000,
        )
        n_dt = await page.evaluate(
            "() => document.querySelectorAll('#boolean-stability_summary dl dt').length"
        )
        print(f"stability summary <dt> count: {n_dt}")
        assert n_dt >= 3, f"expected stability summary with >=3 fields, got {n_dt}"

        # Switch to the Boolean attractors tab
        # Scope to this module's navset, never a bare text= match: every panel
        # shares one DOM, so a future label elsewhere would hijack this click.
        await page.click("#boolean-boolean_tabs a[data-value='Boolean attractors']")
        # The search runs in a background task: wait for its result (the
        # all-off info alert), not a fixed delay. Generous budget for a loaded
        # machine; the search itself takes well under a second.
        await page.wait_for_selector("#boolean-attractor_panel .alert-info", timeout=60000)

        info_text = await page.evaluate(
            "() => {"
            "  const w = document.querySelector('#boolean-attractor_panel .alert-info');"
            "  return w ? w.textContent.trim() : null;"
            "}"
        )
        print(f"info alert: {info_text!r}")
        assert info_text is not None, "expected alert-info in attractor panel"
        assert ("no feedback loop keeps any element active" in info_text
                and "all elements off" in info_text), \
            f"info text doesn't match the all-off sentence: {info_text}"

        core_info = await page.evaluate(
            "() => {"
            "  const e = document.querySelector('#boolean-attractor_panel .boolean-core-info');"
            "  return e ? e.textContent.trim() : null;"
            "}"
        )
        print(f"core info: {core_info!r}")
        assert (core_info is not None and "17 nodes" in core_info
                and "16 in the feedback core" in core_info
                and "basin sizes exact" in core_info), \
            f"core_info line missing or wrong: {core_info}"

        # all_off_only hides the single-row table; no cap/too-large warning.
        n_tables = await page.evaluate(
            "() => document.querySelectorAll('#boolean-attractor_panel table').length"
        )
        n_warn = await page.evaluate(
            "() => document.querySelectorAll('#boolean-attractor_panel .alert-warning').length"
        )
        print(f"tables: {n_tables}, alert-warning: {n_warn}")
        assert n_tables == 0, f"all-off result must not render a table, got {n_tables}"
        assert n_warn == 0, f"unexpected warning in attractor panel: {n_warn}"

        # No danger-level error alert anywhere on the page
        danger_count = await page.evaluate(
            "() => document.querySelectorAll('.alert-danger').length"
        )
        print(f"alert-danger count: {danger_count}")
        assert danger_count == 0, f"unexpected error alert(s) present: {danger_count}"

        await page.screenshot(path="tests/screenshots/boolean.png")
        print("\nboolean e2e assertions pass")
        await browser.close()


asyncio.run(main())
