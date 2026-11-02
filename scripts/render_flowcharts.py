"""把思诺流程图系统 HTML 逐张渲染为高清 PNG（2x）。

用法：.venv/Scripts/python scripts/render_flowcharts.py
"""
import os
from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.normpath(os.path.join(HERE, "..", "var", "diagrams", "思诺流程图系统.html"))
OUT = os.path.normpath(os.path.join(HERE, "..", "var", "diagrams", "png"))
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

NAMES = {
    "d1": "01_书章页节点层级总览.png",
    "d2": "02_一本书两层图.png",
    "d3": "03_系统架构图.png",
    "d4": "04_阶段建议业务流程图.png",
    "d5": "05_版本回溯流程图.png",
    "d6": "06_原件证据处理流程图.png",
    "d7": "07_记忆读集切换流程图.png",
}


def main():
    os.makedirs(OUT, exist_ok=True)
    url = "file:///" + SRC.replace("\\", "/")
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME, headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900}, device_scale_factor=2)
        page.goto(url, wait_until="networkidle")
        page.wait_for_timeout(600)
        for dg_id, fname in NAMES.items():
            el = page.locator(f"#{dg_id}")
            el.scroll_into_view_if_needed()
            page.wait_for_timeout(200)
            path = os.path.join(OUT, fname)
            el.screenshot(path=path)
            print("  [ok]", fname, flush=True)
        browser.close()
    print("DONE ->", OUT, flush=True)


if __name__ == "__main__":
    main()
