"""浏览器实测：写节点(含内容) → 链式节点图 → 编辑弹层 → 针对节点问智能体。

针对 demo 项目「夜间取物」的「用户与场景研究」页 (daf94e21)。
"""
import os
import time
from playwright.sync_api import sync_playwright

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
BASE = "http://127.0.0.1:8000"
PID = "061f59bc-cd9e-49dc-9d8c-cf42ac04e3eb"
PAGE = "daf94e21-bb1d-42f9-918b-634fdf6df037"  # 用户与场景研究
OUT = "var/screenshots_v3"


def main():
    os.makedirs(OUT, exist_ok=True)
    js_errors = []

    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME, headless=True)
        pg = b.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        pg.set_default_timeout(30000)
        pg.on("pageerror", lambda e: js_errors.append(str(e)))

        def shot(name, full=False):
            pg.screenshot(path=os.path.join(OUT, name), full_page=full)
            print("  shot:", name, flush=True)

        # 1. 登录
        pg.goto(BASE + "/projects", wait_until="domcontentloaded")
        pg.wait_for_timeout(800)
        pg.fill("#login-name", "synthetic-lead")
        pg.fill("#login-password", "synthetic-pass-lead")
        pg.get_by_role("button", name="登录", exact=True).click()
        pg.wait_for_selector("a.project-link", timeout=15000)
        print("[1] 登录成功", flush=True)

        # 2. 进入目标页
        pg.goto(f"{BASE}/projects/{PID}/pages/{PAGE}", wait_until="domcontentloaded")
        pg.wait_for_timeout(1800)
        print("[2] 已进入「用户与场景研究」页", flush=True)

        # 确认关键区域存在（空页允许暂无节点图）
        assert pg.locator("#node-content").count() == 1, "写节点内容输入框缺失"
        assert pg.locator("#chat-input").count() == 1, "对话输入框缺失"
        print("  写节点输入框 / 对话输入框 均存在，初始节点数:", pg.locator(".node-chip").count(), flush=True)

        # 3. 初始截图（空页：写节点表单 + 空提示）
        pg.locator("#node-content").scroll_into_view_if_needed()
        pg.wait_for_timeout(300)
        shot("n01_empty_state.png", full=False)
        print("[3] 初始空页截图完成", flush=True)

        # 4. 写节点：选「洞察」+ 输入内容
        pg.select_option("#node-kind", "insight")
        content_text = "用户夜间取物时希望少移动、少开灯，避免吵醒同住的人"
        pg.fill("#node-content", content_text)
        shot("n02_before_submit.png", full=False)
        pg.get_by_role("button", name="记下这个节点", exact=True).click()
        pg.wait_for_timeout(1500)
        print("[4] 已提交新节点", flush=True)

        # 5. 验证新节点出现在链上（含内容）
        pg.wait_for_selector(".node-graph .node-chip", timeout=10000)
        pg.wait_for_timeout(500)
        chips = pg.locator(".node-chip")
        last = chips.nth(chips.count() - 1)
        last.scroll_into_view_if_needed()
        pg.wait_for_timeout(300)
        shot("n03_after_add.png", full=False)
        last_text = last.inner_text()
        print("  新节点文本片段:", last_text[:80].replace("\n", " | "), flush=True)
        assert content_text in last_text, "新节点未显示输入的内容"
        print("[5] 新节点链式可见且含内容 ✓", flush=True)

        # 6. 点击新节点 → 编辑弹层
        last.click()
        pg.wait_for_selector(".node-editor", timeout=8000)
        pg.wait_for_timeout(400)
        shot("n04_node_editor.png", full=False)
        print("[6] 节点编辑弹层已打开，textarea 值:", pg.locator(".node-editor textarea").input_value()[:40], flush=True)

        # 7. 点「问智能体」→ 验证 chat-input 预填并聚焦
        pg.get_by_role("button", name="问智能体", exact=True).click()
        pg.wait_for_timeout(600)
        chat_val = pg.locator("#chat-input").input_value()
        print("  chat-input 预填:", chat_val, flush=True)
        assert "查证据" in chat_val and "洞察" in chat_val, "问智能体未把节点带入对话"
        shot("n05_ask_focused.png", full=False)
        print("[7] 针对节点问智能体：对话已预填节点上下文 ✓", flush=True)

        # 8. 全页截图（完整工作台）
        shot("n06_full_page.png", full=True)
        print("[8] 全页截图完成，页面高度:", pg.evaluate("document.body.scrollHeight"), flush=True)

        b.close()

    print("=== JS 错误 ===", flush=True)
    if js_errors:
        for e in js_errors:
            print("  ERR:", e, flush=True)
    else:
        print("  无 JS 错误 ✓", flush=True)
    print("done", flush=True)


if __name__ == "__main__":
    main()
