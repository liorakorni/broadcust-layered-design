#!/usr/bin/env python3
"""Phase 1 gate: prove Chromium renders inside the Lambda container.

Checks the three things that actually break in Lambda: the browser launches at
PLAYWRIGHT_BROWSERS_PATH, document.fonts.ready resolves with Heebo really loaded,
and a screenshot comes back non-blank.
"""
import os, pathlib, sys

HTML = """<!doctype html><html><head><meta charset="utf-8">
<style>body { margin:0; }</style></head><body>
<div class="image container" style="opacity:1;width:400px;height:400px;position:relative;overflow:hidden;background:#071B33;">
 <div class="content" style="width:100%;height:100%;position:relative;">
  <div class="box" style="width:100%;height:100%;position:relative;">
   <div class="words d1001 fxs" id="text-1001" data-role="title"
     style="position:absolute;left:20px;top:140px;width:360px;height:120px;font-family:Heebo,Arial;font-size:48px;font-weight:800;color:#D4AF37;text-align:right;direction:rtl;line-height:1.12;box-sizing:border-box;z-index:10;">
    <div class="text">חורף באיטליה</div>
   </div>
  </div>
 </div>
</div></body></html>"""

def main():
    from playwright.sync_api import sync_playwright
    out = pathlib.Path("/tmp/render_probe.png")
    page_html = pathlib.Path("/tmp/probe.html")
    page_html.write_text(HTML, encoding="utf-8")
    print("PLAYWRIGHT_BROWSERS_PATH =", os.environ.get("PLAYWRIGHT_BROWSERS_PATH"))
    with sync_playwright() as p:
        browser = p.chromium.launch(args=[
            "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
            "--single-process", "--no-zygote",
        ])
        page = browser.new_page(viewport={"width": 400, "height": 400})
        page.goto(page_html.as_uri())
        page.evaluate("document.fonts.ready")
        loaded = page.evaluate("document.fonts.status")
        heebo_ok = page.evaluate("document.fonts.check('800 48px Heebo')")
        page.screenshot(path=str(out))
        browser.close()
    data = out.read_bytes()
    from PIL import Image
    import io
    img = Image.open(io.BytesIO(data)).convert("RGB")
    colors = img.getcolors(maxcolors=1_000_000) or []
    print("document.fonts.status =", loaded)
    print("document.fonts.check('800 48px Heebo') =", heebo_ok)
    print("screenshot bytes:", len(data), "size:", img.size, "distinct colors:", len(colors))
    if not heebo_ok:
        print("FAIL: Heebo not resolvable by Chromium", file=sys.stderr); return 1
    if len(colors) < 3:
        print("FAIL: screenshot is effectively blank", file=sys.stderr); return 1
    if pathlib.Path("/out").is_dir():
        import shutil; shutil.copy(out, "/out/render_probe.png")
    print("PASS: Chromium rendered Hebrew RTL text in the Lambda image.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
