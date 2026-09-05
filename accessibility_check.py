"""Browser accessibility smoke test using installed Chrome or Edge."""
from __future__ import annotations
import argparse, json
from pathlib import Path
from playwright.sync_api import sync_playwright

CANDIDATES=[Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")]
def run(url:str)->dict:
    executable=next((path for path in CANDIDATES if path.exists()),None)
    if not executable: raise RuntimeError("Chrome or Edge is required")
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,executable_path=str(executable));page=browser.new_page(viewport={"width":390,"height":844})
        page.goto(url,wait_until="domcontentloaded");checks={
            "lang":page.locator("html").get_attribute("lang") in {"en","es"},
            "heading":page.get_by_role("heading",level=1).count()==1,
            "label":page.locator('label[for="message"]').count()==1 and page.locator("#message").count()==1,
            "live_log":page.locator('[role="log"][aria-live="polite"]').count()==1,
            "skip_link":page.locator('a.skip').count()==1,
            "no_horizontal_overflow":page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth"),
        }
        page.locator("#message").focus();checks["keyboard_focus"]=page.evaluate("document.activeElement.id === 'message'")
        browser.close()
    return {"passed":all(checks.values()),"checks":checks}
def main()->int:
    p=argparse.ArgumentParser();p.add_argument("--url",default="http://127.0.0.1:8766/");a=p.parse_args();report=run(a.url);print(json.dumps(report,indent=2));return 0 if report["passed"] else 1
if __name__=="__main__":raise SystemExit(main())
