"""浏览器实测：写两个节点 → 拖拽连一条「承接」关系边 → 验证存库与读集。

针对 demo 项目「夜间取物」的「用户与场景研究」页。
"""
import json
import os
import urllib.request
import http.cookiejar
from playwright.sync_api import sync_playwright

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
BASE = "http://127.0.0.1:8000"
PID = "061f59bc-cd9e-49dc-9d8c-cf42ac04e3eb"
PAGE = "daf94e21-bb1d-42f9-918b-634fdf6df037"
OUT = "var/screenshots_v4"


def api_login():
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.open(urllib.request.Request(
        BASE + "/api/v1/session",
        data=json.dumps({"login_name": "synthetic-lead", "password": "synthetic-pass-lead"}).encode(),
        headers={"Content-Type": "application/json", "Origin": "http://127.0.0.1:8000"},
    ))
    return op


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

        # 2. 进入页
        pg.goto(f"{BASE}/projects/{PID}/pages/{PAGE}", wait_until="domcontentloaded")
        pg.wait_for_selector("#node-content", timeout=15000)
        pg.wait_for_timeout(800)
        print("[2] 已进入页，当前节点数:", pg.locator(".node-chip").count(), flush=True)

        # 3. 补齐到至少 2 个节点（写「要求」节点）
        if pg.locator(".node-chip").count() < 2:
            pg.select_option("#node-kind", "requirement")
            pg.fill("#node-content", "夜间取物需单手可完成，且不影响其他物品")
            pg.get_by_role("button", name="记下这个节点", exact=True).click()
            pg.wait_for_timeout(1500)
            pg.wait_for_selector(".node-chip")
            print("[3] 已写第 2 个节点，节点数:", pg.locator(".node-chip").count(), flush=True)

        chips = pg.locator(".node-chip")
        assert chips.count() >= 2, "节点不足 2 个"
        chip_ids = chips.evaluate_all("els => els.map(e => e.dataset.nodeId)")
        print("  节点 ids:", chip_ids, flush=True)

        # 4. 拖拽建边：从第 1 个节点的 anchor 拖到第 2 个节点
        anchor = pg.locator(".n-link-anchor").nth(0)
        target = chips.nth(1)
        abox = anchor.bounding_box()
        tbox = target.bounding_box()
        pg.mouse.move(abox["x"] + abox["width"] / 2, abox["y"] + abox["height"] / 2)
        pg.mouse.down()
        pg.mouse.move(tbox["x"] + tbox["width"] / 2, tbox["y"] + tbox["height"] / 2, steps=12)
        pg.mouse.up()
        pg.wait_for_selector(".kind-picker", timeout=8000)
        shot("e01_kind_picker.png", full=False)
        print("[4] 拖拽后弹出关系类型选择", flush=True)

        # 5. 选「承接」
        pg.locator(".kp-option").filter(has_text="承接").first.click()
        pg.wait_for_timeout(1500)
        shot("e02_after_edge.png", full=False)
        print("[5] 已选「承接」，等待保存", flush=True)

        # 6. 验证边标签出现
        pg.wait_for_selector(".n-edge-tag", timeout=10000)
        tags = pg.locator(".n-edge-tag")
        print("  关系边标签数:", tags.count(), "| 内容:", tags.first.inner_text().replace("\n", " "), flush=True)
        assert "承接" in tags.first.inner_text(), "边标签未显示承接"
        shot("e03_edge_tag.png", full=False)
        print("[6] 边标签可见 ✓", flush=True)

        b.close()

    # 7. 通过 API 验证边已存进 draft（含 kind）
    op = api_login()
    d = json.loads(op.open(f"{BASE}/api/v1/pages/{PAGE}/draft").read())["data"]
    edges = d.get("edges", [])
    print("=== API draft edges ===", flush=True)
    for e in edges:
        print("  ", json.dumps(e, ensure_ascii=False), flush=True)
    assert edges and any(e.get("kind") == "derives" for e in edges), "draft 里没有 derives 边"
    print("[7] API 验证：边已存入草稿，kind=derives ✓", flush=True)

    print("=== JS 错误 ===", flush=True)
    if js_errors:
        for e in js_errors:
            print("  ERR:", e, flush=True)
    else:
        print("  无 JS 错误 ✓", flush=True)
    print("done", flush=True)


if __name__ == "__main__":
    main()
