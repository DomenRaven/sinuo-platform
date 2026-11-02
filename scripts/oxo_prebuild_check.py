"""预搭建结果 UI 核对：登录后进入 ef389cb5 项目，截取地图与关键工作台。"""

from playwright.sync_api import sync_playwright
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "var" / "prebuild-check"
OUT.mkdir(parents=True, exist_ok=True)
PROJECT = "OXO Good Grips 削皮器"


def run() -> None:
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(viewport={"width": 1600, "height": 1000})
        pg = ctx.new_page()
        pg.goto("http://127.0.0.1:8000/", wait_until="networkidle")

        # 登录
        pg.fill("#login-name", "synthetic-lead")
        pg.fill("#login-password", "synthetic-pass-lead")
        pg.click('button[type="submit"]')
        pg.wait_for_load_state("networkidle")
        pg.wait_for_timeout(1200)

        # 进入项目
        pg.click(f"text={PROJECT}")
        pg.wait_for_load_state("networkidle")
        pg.wait_for_timeout(1200)
        pg.screenshot(path=str(OUT / "01-项目地图.png"), full_page=True)

        # 湿滑与细柄（查证据场景页）
        pg.click("text=湿滑与细柄")
        pg.wait_for_load_state("networkidle")
        pg.wait_for_timeout(800)
        pg.screenshot(path=str(OUT / "02-湿滑与细柄.png"), full_page=True)

        # 回地图 → 厨师与关节炎群体试用（检查下一步场景页）
        pg.click("text=查看项目地图")
        pg.wait_for_load_state("networkidle")
        pg.wait_for_timeout(800)
        pg.click("text=厨师与关节炎群体试用")
        pg.wait_for_load_state("networkidle")
        pg.wait_for_timeout(800)
        pg.screenshot(path=str(OUT / "03-厨师与关节炎群体试用.png"), full_page=True)

        # 首页 Farber的委托（简报变化场景页）
        pg.click("text=查看项目地图")
        pg.wait_for_load_state("networkidle")
        pg.wait_for_timeout(800)
        pg.click("text=Farber的委托")
        pg.wait_for_load_state("networkidle")
        pg.wait_for_timeout(800)
        pg.screenshot(path=str(OUT / "04-Farber的委托.png"), full_page=True)

        ctx.close()
        b.close()
        print("done:", OUT)


if __name__ == "__main__":
    run()
