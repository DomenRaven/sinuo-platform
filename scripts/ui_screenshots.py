"""用真实 Chrome headless 走完整 U 任务链，分段高清截图（v2）。

修正点：
- 精确按钮匹配（get_by_role exact），避免「确认」误匹配「确认为正式版本」。
- 写节点 / 固化后等待 location.reload() 完成。
- 用 viewport 分段截图（非 full_page），清晰可读。
"""
import os
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8021"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.normpath(os.path.join(HERE, "..", "var", "screenshots"))
PID = "061f59bc-cd9e-49dc-9d8c-cf42ac04e3eb"
PAGE_ID = "8e0711fc-aaf7-4a1d-8c99-eb5d00e34ecf"


def shot(page, name, full=False):
    path = os.path.join(OUT, name)
    page.screenshot(path=path, full_page=full)
    print(f"  [shot] {name}", flush=True)


def scroll_to(page, selector):
    try:
        page.locator(selector).first.scroll_into_view_if_needed()
        page.wait_for_timeout(400)
    except Exception as e:
        print("  (scroll跳过)", selector, e, flush=True)


def main():
    os.makedirs(OUT, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME, headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        page.set_default_timeout(25000)

        # 1. 登录页
        print("S1 登录页", flush=True)
        page.goto(BASE + "/projects", wait_until="domcontentloaded")
        page.wait_for_timeout(1200)
        shot(page, "s01_login.png")

        # 2. 登录
        print("S2 登录", flush=True)
        page.fill("#login-name", "synthetic-lead")
        page.fill("#login-password", "synthetic-pass-lead")
        page.get_by_role("button", name="登录", exact=True).click()
        page.wait_for_selector("a.project-link", timeout=15000)
        page.wait_for_timeout(800)
        shot(page, "s02_projects.png")

        # 3. 项目概览
        print("S3 项目概览", flush=True)
        page.goto(f"{BASE}/projects/{PID}", wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        shot(page, "s03_overview_top.png")
        scroll_to(page, ".matrix")
        shot(page, "s04_overview_matrix.png")
        scroll_to(page, ".bom")
        shot(page, "s05_overview_bom.png")

        # 4. 单页工作台
        print("S4 单页工作台", flush=True)
        page.goto(f"{BASE}/projects/{PID}/pages/{PAGE_ID}", wait_until="domcontentloaded")
        page.wait_for_timeout(1800)
        shot(page, "s06_page_top.png")

        # 5. 写节点
        print("S5 写节点", flush=True)
        scroll_to(page, "#node-kind")
        page.select_option("#node-kind", "insight")
        page.get_by_role("button", name="记下这个节点", exact=True).click()
        page.wait_for_timeout(2500)  # PATCH + reload + 渲染
        shot(page, "s07_write_node.png")

        # 6. 固化版本
        print("S6 固化版本", flush=True)
        scroll_to(page, "button:has-text('确认为正式版本')")
        page.get_by_role("button", name="确认为正式版本", exact=True).click()
        page.wait_for_timeout(2500)  # POST checkpoints + reload
        shot(page, "s08_checkpoint.png")

        # 7. 问下一步（真实 DeepSeek）
        print("S7 问下一步（真实模型）", flush=True)
        page.locator("button.quick-card", has_text="检查下一步").scroll_into_view_if_needed()
        page.wait_for_timeout(300)
        page.locator("button.quick-card", has_text="检查下一步").click()
        page.wait_for_selector(".role-card", timeout=150000)
        page.wait_for_timeout(800)
        shot(page, "s09_run_result.png")

        # 8. 候选卡
        print("S8 候选卡", flush=True)
        scroll_to(page, "article.card")
        page.wait_for_timeout(600)
        shot(page, "s10_candidate.png")

        # 9. 预览
        print("S9 预览", flush=True)
        preview_btn = page.locator("article.card button", has_text="预览")
        if preview_btn.count() == 0:
            print("  无预览按钮（无候选或只读摘要卡）", flush=True)
        else:
            preview_btn.first.click()
            page.wait_for_timeout(1800)
            shot(page, "s11_preview.png")

            # 10. 确认建新页（精确匹配「确认」，避开「确认为正式版本」）
            print("S10 确认建新页", flush=True)
            confirm_btn = page.get_by_role("button", name="确认", exact=True)
            if confirm_btn.count() == 0:
                print("  无确认按钮", flush=True)
            else:
                confirm_btn.first.click()
                page.wait_for_timeout(3000)
                shot(page, "s12_confirm.png")

        browser.close()
    print("DONE ->", OUT, flush=True)


if __name__ == "__main__":
    main()
