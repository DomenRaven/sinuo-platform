"""UI 升级后的全链路实测 + 截图（v3）。"""
import os
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.normpath(os.path.join(HERE, "..", "var", "screenshots_v2"))
PID = "061f59bc-cd9e-49dc-9d8c-cf42ac04e3eb"
PAGE_ID = "8e0711fc-aaf7-4a1d-8c99-eb5d00e34ecf"


def shot(page, name, full=False):
    os.makedirs(OUT, exist_ok=True)
    page.screenshot(path=os.path.join(OUT, name), full_page=full)
    print("  [shot]", name, flush=True)


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME, headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        page.set_default_timeout(30000)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        # 1. 登录
        page.goto(BASE + "/projects", wait_until="domcontentloaded")
        page.wait_for_timeout(1000)
        shot(page, "v01_login.png")
        page.fill("#login-name", "synthetic-lead")
        page.fill("#login-password", "synthetic-pass-lead")
        page.get_by_role("button", name="登录", exact=True).click()
        page.wait_for_selector("a.project-link", timeout=15000)
        page.wait_for_timeout(800)
        shot(page, "v02_shelf.png")

        # 2. 项目地图（章页画布）
        page.goto(f"{BASE}/projects/{PID}", wait_until="domcontentloaded")
        page.wait_for_timeout(1600)
        shot(page, "v03_stages_top.png")
        page.locator(".chapter-canvas").scroll_into_view_if_needed()
        page.wait_for_timeout(500)
        shot(page, "v04_chapter_canvas.png", full=False)

        # 3. 拖拽测试：用 mouse 事件把「用户与场景研究」章第 1 页拖到「设计要求」章
        try:
            page.locator(".chapter-canvas").scroll_into_view_if_needed()
            page.wait_for_timeout(400)
            thumbs = page.locator(".page-thumb")
            print("  thumbs count:", thumbs.count(), flush=True)
            if thumbs.count() >= 2:
                src_grip = thumbs.nth(1).locator(".pt-grip")
                src_grip.scroll_into_view_if_needed()
                page.wait_for_timeout(300)
                grip_box = src_grip.bounding_box()
                target_box = page.locator(".chapter").nth(3).locator(".chapter-pages").bounding_box()
                page.mouse.move(grip_box["x"] + grip_box["width"] / 2, grip_box["y"] + grip_box["height"] / 2)
                page.mouse.down()
                page.mouse.move(target_box["x"] + target_box["width"] / 2, target_box["y"] + target_box["height"] / 2, steps=25)
                page.wait_for_timeout(200)
                page.mouse.up()
                page.wait_for_timeout(600)
                shot(page, "v05_after_drag.png", full=False)
                # 校验目标章现在有几页
                target_count = page.locator(".chapter").nth(3).locator(".page-thumb").count()
                print("  [drag] target chapter pages after drag:", target_count, flush=True)
        except Exception as e:
            print("  [drag] skipped:", e, flush=True)

        # 4. 单页工作台
        page.goto(f"{BASE}/projects/{PID}/pages/{PAGE_ID}", wait_until="domcontentloaded")
        page.wait_for_timeout(1800)
        shot(page, "v06_page_top.png")
        page.locator(".work-split").scroll_into_view_if_needed()
        page.wait_for_timeout(500)
        shot(page, "v07_work_split.png", full=False)

        print("JS errors:", errors[:10] if errors else "none", flush=True)
        browser.close()
    print("DONE ->", OUT, flush=True)


if __name__ == "__main__":
    main()
