"""浏览器端完整「用户编辑」写链实测：登录 → 新建项目 → 写节点 → 固化版本。"""
import os
import time
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.normpath(os.path.join(HERE, "..", "var", "screenshots_v2"))


def shot(page, name):
    os.makedirs(OUT, exist_ok=True)
    page.screenshot(path=os.path.join(OUT, name))
    print("  [shot]", name, flush=True)


def main():
    title = "UI 升级实测 " + time.strftime("%H%M%S")
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME, headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        page.set_default_timeout(30000)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        # 1. 登录
        page.goto(BASE + "/projects", wait_until="domcontentloaded")
        page.wait_for_timeout(800)
        page.fill("#login-name", "synthetic-lead")
        page.fill("#login-password", "synthetic-pass-lead")
        page.get_by_role("button", name="登录", exact=True).click()
        page.wait_for_selector("a.project-link", timeout=15000)
        page.wait_for_timeout(600)

        # 2. 新建项目
        page.fill("#project-title", title)
        page.get_by_role("button", name="建立项目", exact=True).click()
        page.wait_for_selector(".chapter-canvas", timeout=15000)
        page.wait_for_timeout(1200)
        print("  新建项目成功：", page.url, flush=True)

        # 3. 进入第一个页（阶段1 的首页）
        first_thumb = page.locator(".page-thumb a").first
        first_thumb.click()
        page.wait_for_selector("#node-kind", timeout=15000)
        page.wait_for_timeout(1200)
        print("  进入首页：", page.url, flush=True)
        shot(page, "w01_page_editor.png")

        # 4. 写节点（选「问题」）
        page.select_option("#node-kind", "problem")
        page.get_by_role("button", name="记下这个节点", exact=True).click()
        page.wait_for_timeout(2500)
        shot(page, "w02_after_add_node.png")
        print("  写节点完成", flush=True)

        # 5. 固化版本
        confirm = page.get_by_role("button", name="确认为正式版本", exact=True)
        confirm.click()
        page.wait_for_timeout(2500)
        shot(page, "w03_after_checkpoint.png")
        print("  固化版本完成", flush=True)

        # 6. 校验：页面出现「已确认第 N 版」badge 且节点图有「问题」节点
        has_badge = page.locator(".badge:has-text('已确认第')").count() > 0
        has_problem = page.locator(".node-chip .n-kind", has_text="问题").count() > 0
        print("  已确认 badge:", has_badge, "| 问题节点:", has_problem, flush=True)

        print("JS errors:", errors[:10] if errors else "none", flush=True)
        browser.close()
    print("DONE ->", OUT, flush=True)


if __name__ == "__main__":
    main()
