"""浏览器实测三个新前端入口：新建页 / 建锚点 / 对话带关键词。"""
from playwright.sync_api import sync_playwright
import os, time

CHROME = r'C:\Program Files\Google\Chrome\Application\chrome.exe'
BASE = 'http://127.0.0.1:8000'
OUT = 'var/manual_test'
os.makedirs(OUT, exist_ok=True)

with sync_playwright() as p:
    b = p.chromium.launch(executable_path=CHROME, headless=True)
    pg = b.new_page(viewport={'width': 1440, 'height': 900}, device_scale_factor=2)
    pg.set_default_timeout(15000)
    errs = []
    pg.on('console', lambda m: errs.append(m.text) if m.type == 'error' else None)
    pg.on('pageerror', lambda e: errs.append(str(e)))

    # 登录
    pg.goto(BASE + '/projects', wait_until='domcontentloaded')
    pg.wait_for_timeout(800)
    pg.fill('#login-name', 'synthetic-lead')
    pg.fill('#login-password', 'synthetic-pass-lead')
    pg.get_by_role('button', name='登录', exact=True).click()
    pg.wait_for_selector('a.project-link', timeout=15000)

    # 新建项目
    pg.fill('#project-title', '手动复刻测试-削皮器')
    pg.get_by_role('button', name='建立项目', exact=True).click()
    pg.wait_for_selector('.chapter', timeout=15000)
    pg.wait_for_timeout(1200)
    print('[1] 新建项目 OK，章数量:', pg.locator('.chapter').count())
    print('    新建页按钮数量:', pg.locator('.page-add').count())
    project_url = pg.url
    print('    项目URL:', project_url)
    pg.screenshot(path=os.path.join(OUT, 'm01_new_project.png'))

    # 记录当前 revision（从页面 meta 提取）
    meta = pg.locator('p.meta').first.inner_text()
    print('    项目 meta:', meta)

    # 测试入口1：新建页（第2章加一页）
    pg.locator('.page-add').nth(1).click()  # 第2章的用户与场景研究
    pg.wait_for_selector('.prompt-panel', timeout=5000)
    pg.fill('.prompt-panel input', '厨师的连续削切')
    pg.get_by_role('button', name='建立页', exact=True).click()
    pg.wait_for_timeout(1500)
    pg.wait_for_selector('.chapter', timeout=15000)
    print('[2] 新建页按钮 OK，现在页缩略图数:', pg.locator('.page-thumb').count())
    pg.screenshot(path=os.path.join(OUT, 'm02_after_new_page.png'))

    # 进入首页（第1章唯一页）测试上传 + 建锚点
    pg.locator('.page-thumb a').first.click()
    pg.wait_for_selector('.work-split', timeout=15000)
    pg.wait_for_timeout(1000)
    print('[3] 进入单页工作台 OK')

    # 上传一份文本证据（用 set_input_files 上传本地临时文件）
    txt_path = os.path.abspath(os.path.join(OUT, 'evidence.txt'))
    with open(txt_path, 'w', encoding='utf-8') as f:
        f.write('根据Cooper Hewitt和Smart Design的公开记述整理。细硬金属柄在湿手和握力不足时打滑、疼痛。准备某些食物又需要削皮。')
    pg.set_input_files('#work-file', txt_path)
    pg.fill('#work-license', 'public-account-summary')
    pg.locator('#work-sensitivity').select_option('project')
    pg.check('#work-ai')
    pg.get_by_role('button', name='上传原件', exact=True).click()
    pg.wait_for_timeout(1500)
    print('[4] 上传原件完成，交付物卡片数:', pg.locator('.deliverable').count())
    print('    锚定按钮数:', pg.locator('.dv-anchor').count())

    # 测试入口2：建锚点
    pg.locator('.dv-anchor').first.click()
    pg.wait_for_selector('.prompt-panel', timeout=5000)
    pg.fill('.prompt-panel textarea', '细硬金属柄在湿手和握力不足时打滑、疼痛')
    pg.get_by_role('button', name='锚定', exact=True).click()
    pg.wait_for_timeout(1500)
    # 处理 alert
    try:
        pg.on('dialog', lambda d: d.accept())
    except Exception:
        pass
    print('[5] 建锚点按钮已点击')

    # 写一个节点
    pg.locator('#node-kind').select_option('problem')
    pg.fill('#node-content', '妻子用普通金属削皮器时手痛。任务不是做一件只有病人使用的专用工具。')
    pg.get_by_role('button', name='记下这个节点', exact=True).click()
    pg.wait_for_timeout(1500)
    print('[6] 写节点完成，节点数:', pg.locator('.node-chip').count())

    # 确认版本
    pg.get_by_role('button', name='确认为正式版本', exact=True).click()
    pg.wait_for_timeout(1800)
    print('[7] 确认版本完成')

    # 测试入口3：对话带关键词
    pg.fill('#chat-input', '查证据')
    pg.fill('#chat-query', '细硬金属柄')
    pg.get_by_role('button', name='发送', exact=True).click()
    pg.wait_for_timeout(1500)
    # 等待运行（真实模型可能 30-90s，这里只确认发送成功、按钮变化）
    print('[8] 对话带关键词已发送，等待运行...')
    try:
        pg.wait_for_selector('.role-card, .run-intent', timeout=120000)
        pg.wait_for_timeout(1000)
        print('    运行完成，角色卡数:', pg.locator('.role-card').count())
        pg.screenshot(path=os.path.join(OUT, 'm03_after_run.png'))
    except Exception as e:
        print('    运行等待超时/未出现:', e)
        pg.screenshot(path=os.path.join(OUT, 'm03_run_timeout.png'))

    print('=== JS errors ===')
    for e in errs[:10]:
        print('  ', e)
    b.close()
print('done')
