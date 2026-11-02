"""Rebuild the submission Word document from the current OXO demo."""

from __future__ import annotations

import os
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "deliverables" / "assets"
DOC_PATH = ROOT / "deliverables" / "3240103625_张玮勐_项目说明.docx"
MAP = ASSETS / "oxo_map_r39.png"
PAGE = ASSETS / "oxo_page_top.png"
AGENT = ASSETS / "agent_output.png"
CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
PROJECT = "faf5436c-347f-47bc-84d6-a6f2f9f68a1d"
REQUIREMENTS = "64d82d22-3ae2-4442-a0b8-1e1d8b7d27c0"
BASE = "http://127.0.0.1:8000"


def capture() -> None:
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    os.environ["no_proxy"] = "127.0.0.1,localhost"
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        os.environ.pop(key, None)
    ASSETS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(CHROME),
            headless=True,
        )
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(BASE + "/", wait_until="networkidle")
        if page.locator("#login-name").count():
            page.fill("#login-name", "synthetic-lead")
            page.fill("#login-password", "synthetic-pass-lead")
            page.get_by_role("button", name="登录", exact=True).click()
            page.wait_for_url("**/projects**")
        page.goto(f"{BASE}/projects/{PROJECT}", wait_until="networkidle")
        page.get_by_text("项目修订 39").wait_for()
        page.screenshot(path=str(MAP), full_page=True)
        page.goto(
            f"{BASE}/projects/{PROJECT}/pages/{REQUIREMENTS}",
            wait_until="networkidle",
        )
        page.get_by_role("heading", name="五条握持要求").wait_for()
        page.screenshot(path=str(PAGE), full_page=True)
        browser.close()


def _east(run, name: str, size: int, bold: bool = False, color: str | None = None) -> None:
    run.bold = bold
    run.font.size = Pt(size)
    run.font.name = "Times New Roman"
    run._element.rPr.rFonts.set(qn("w:eastAsia"), name)
    if color:
        run.font.color.rgb = RGBColor.from_string(color)


def _set_run_font(paragraph, east: str, size: int) -> None:
    for run in paragraph.runs:
        _east(run, east, size)


def add_page_field(paragraph) -> None:
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = " PAGE "
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.append(begin)
    run2 = paragraph.add_run()
    run2._r.append(instr)
    run3 = paragraph.add_run()
    run3._r.append(separate)
    run3.add_text("1")
    run4 = paragraph.add_run()
    run4._r.append(end)
    for item in (run, run2, run3, run4):
        _east(item, "宋体", 9, color="666666")


def add_toc(paragraph) -> None:
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = r' TOC \o "1-2" \h \z \u '
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.append(begin)
    paragraph.add_run()._r.append(instr)
    paragraph.add_run()._r.append(separate)
    placeholder = paragraph.add_run("目录将在打开文档时按标题生成。")
    _east(placeholder, "宋体", 12)
    paragraph.add_run()._r.append(end)


def enable_update_fields(document: Document) -> None:
    settings = document.settings.element
    node = OxmlElement("w:updateFields")
    node.set(qn("w:val"), "true")
    settings.append(node)


def heading(document: Document, text: str, level: int) -> None:
    paragraph = document.add_heading(text, level=level)
    east = "黑体"
    size = {1: 16, 2: 14}.get(level, 12)
    _set_run_font(paragraph, east, size)


def body(document: Document, text: str) -> None:
    paragraph = document.add_paragraph()
    paragraph.paragraph_format.first_line_indent = Cm(0.74)
    paragraph.paragraph_format.line_spacing_rule = WD_LINE_SPACING.ONE_POINT_FIVE
    paragraph.paragraph_format.space_after = Pt(6)
    run = paragraph.add_run(text)
    _east(run, "宋体", 12)


def bullet(document: Document, text: str) -> None:
    paragraph = document.add_paragraph(style="List Bullet")
    paragraph.paragraph_format.line_spacing_rule = WD_LINE_SPACING.ONE_POINT_FIVE
    paragraph.paragraph_format.space_after = Pt(3)
    if paragraph.runs:
        paragraph.runs[0].text = ""
    run = paragraph.add_run(text)
    _east(run, "宋体", 12)


def caption(document: Document, text: str) -> None:
    paragraph = document.add_paragraph()
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    paragraph.paragraph_format.space_after = Pt(12)
    run = paragraph.add_run(text)
    _east(run, "宋体", 10, color="444444")


def picture(document: Document, path: Path, cap: str) -> None:
    paragraph = document.add_paragraph()
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = paragraph.add_run()
    run.add_picture(str(path), width=Cm(15.5))
    caption(document, cap)


def build() -> None:
    document = Document()
    section = document.sections[0]
    section.top_margin = Cm(2.54)
    section.bottom_margin = Cm(2.54)
    section.left_margin = Cm(2.8)
    section.right_margin = Cm(2.8)
    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    add_page_field(footer)

    normal = document.styles["Normal"]
    normal.font.name = "Times New Roman"
    normal.font.size = Pt(12)
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")

    for _ in range(3):
        document.add_paragraph()
    title = document.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _east(title.add_run("思诺（Seno）"), "黑体", 28, bold=True)
    subtitle = document.add_paragraph()
    subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _east(subtitle.add_run("AI 辅助设计过程管理平台"), "黑体", 18)
    kind = document.add_paragraph()
    kind.alignment = WD_ALIGN_PARAGRAPH.CENTER
    kind.paragraph_format.space_before = Pt(18)
    _east(kind.add_run("项目说明"), "宋体", 16)
    for line in (
        "学号：3240103625",
        "姓名：张玮勐",
        "项目名称：思诺（Seno）",
        "2026年11月",
    ):
        row = document.add_paragraph()
        row.alignment = WD_ALIGN_PARAGRAPH.CENTER
        row.paragraph_format.space_after = Pt(2)
        _east(row.add_run(line), "宋体", 14)
    document.add_page_break()

    heading(document, "目录", 1)
    toc = document.add_paragraph()
    add_toc(toc)
    document.add_page_break()

    heading(document, "一、项目目标", 1)
    body(
        document,
        "思诺面向承担工业设计项目的学生，以及审阅这些项目的教师。一个课题从委托、用户研究、问题定义、设计要求、概念探索、原型、测试走到复盘，判断往往散在聊天记录、文件夹和最终效果图里。教师看到成品时，很难沿一条链回答：这个要求从哪条材料来，试过哪些方向，测试对应哪一版作品。",
    )
    body(
        document,
        "思诺把一个项目收成一本书。章是八个固定阶段：任务与情境、用户与场景研究、问题及机会定义、设计要求、概念探索、深化与原型、测试与迭代、表达与复盘。同一阶段可以有多张平行页。页内节点记录问题、证据、洞察、要求、作品、测试和决定，节点可以带正文。节点之间用承接、支持、反例、引用四类关系相连。每次固化留下一版快照，旧版本仍可打开。",
    )
    body(
        document,
        "学生写下节点，并决定是否采纳智能体的提议。四个智能体按当前问题分工：课题澄清整理任务目标，证据查找从已上传原件中检索并锚到摘录位置，方法建议对照方法卡，过程检查阅读当前页和阶段表后提出能否进入下一阶段。一次运行最多派出其中三个。提议先成为待确认卡，预览之后确认，页和阶段才改变。",
    )

    heading(document, "二、实现方法", 1)
    heading(document, "2.1 数据与材料", 2)
    body(
        document,
        "过程数据包括项目、阶段、页、节点、关系边和版本快照。作品以原件保存，每次新内容形成新的作品版本，并记录哈希。证据锚点指向某一作品版本上的摘录，全文索引使用 SQLite FTS5 的 trigram 分词。理论材料放在本机理论库中，包含概念页、公开方法摘要，以及 15 页 OXO Good Grips 削皮器记述。方法库现有四张方法卡：用户访谈、任务分析、可用性测试、原型。",
    )
    body(
        document,
        "削皮器演示使用公开材料整理而成：Cooper Hewitt 馆藏中的样机与成品著录，美国专利 US4974286 及其再版 USRE34194，Smart Design 的公开设计说明，以及 Santoprene 作为热塑性硫化橡胶的加工说明。这些文字写进项目里的原件和理论页，供证据查找与过程检查读取。",
    )
    heading(document, "2.2 工具与运行结构", 2)
    body(
        document,
        "服务端使用 Python 3.14、FastAPI 和 Uvicorn，数据层使用 SQLAlchemy 2 与 SQLite WAL，结构变更由 Alembic 管理，当前库共 51 张表，迁移头为 0010_evidence_index。本次全量测试在 Python 3.14.3 上完成。理论检索在本环境使用字符 n-gram，相似度下限 0.12。澄清与证据查找调用环境变量 SINUO_MODEL_NAME 指定的 DeepSeek 模型，方法建议与过程检查调用 SINUO_MODEL_NAME_REASONING 指定的模型。",
    )
    body(
        document,
        "智能体编排是自研的 asyncio 有界 DAG。每个任务的输入在运行期间保持原样，依赖任务完成后再继续，单次运行的职责数量上限为 3。界面集中在 web/desk.html，当前 2535 行。页面背景为 #f7f1dd，卡片圆角，项目地图用蓝色泳道区分八个阶段。",
    )
    heading(document, "2.3 主要流程", 2)
    for line in (
        "登录后建立项目，系统写入八个阶段和第一页。",
        "在页上选择节点类型、写下正文，并在节点之间连上关系。此时改动停在草稿。",
        "点击「确认为正式版本」，当前草稿成为新的页版本。",
        "在对话中输入「查证据」「选方法」「检查下一步」或「简报变化」。系统按这句话派出对应职责。",
        "运行结束后面上出现待确认卡。预览后确认，新页、新节点或新关系才写入；拒绝则只留下处置记录。",
    ):
        bullet(document, line)

    heading(document, "三、最终成果", 1)
    heading(document, "3.1 已经可以操作的功能", 2)
    for line in (
        "项目书架列出当前账号可见的项目，并可以新建项目。新建时写入固定的八个阶段。",
        "项目地图按章显示页缩略图和跨章承接。拖动缩略图只改变这台浏览器里的排列。",
        "单页工作台分成两侧。左侧写节点、查看页内推理图、上传交付物并固化版本。右侧选择下一次运行要读的邻页和作品，输入问题，查看角色卡和待确认卡。",
        "七类节点都可以带正文。推理图上可以拖出承接、支持、反例、引用。",
        "上传原件后可以建证据锚点。建锚点时重建全文索引，随后的证据查找能按摘录检索。",
        "候选卡在预览通过后只消费一次。同一张卡的重复确认由条件更新拦住。",
        "本次读取范围可以勾选邻页版本和作品版本，也可以跳过理论库或方法卡。勾选只作用于下一次运行。",
    ):
        bullet(document, line)
    heading(document, "3.2 削皮器项目上的运行效果", 2)
    body(
        document,
        "本地启动服务后，浏览器打开 http://127.0.0.1:8000 ，使用账号 synthetic-lead 进入。演示项目标题为「OXO Good Grips 削皮器」。打开后，用户与场景研究的第一页标题是「谁在使用削皮器，他们在什么场景下遇到手痛问题？」。该项目当前修订号为 39，八个阶段共 14 页。",
    )
    picture(document, MAP, "图 1  项目地图：八个阶段、14 页，以及跨章承接")
    picture(document, PAGE, "图 2  「五条握持要求」的单页工作台")
    body(
        document,
        "在「查证据」运行中，证据查找把当前页「五条握持要求」列为依据，并在角色卡中写出缺口「尚无足够证据」。同一张待确认卡给出的新页标题是「谁在使用削皮器，他们在什么场景下遇到手痛问题？」。另一次带检索词「细硬金属柄」的证据查找，读到了原件摘录「细硬金属柄在湿手和握力不足时打滑、疼痛」。方法建议在提问「用户访谈和可用性测试」时命中用户访谈、可用性测试两张方法卡。",
    )
    picture(document, AGENT, "图 3  查证据之后的角色卡、工具链和待确认卡")
    body(
        document,
        "过程检查在要求页和试用页之后各给出一次可预览的阶段建议。预览后确认，分别在用户与场景研究、表达与复盘建出新页。后一页的标题是「复盘：如何记录测试结果与迭代决策？」。概念章另有三张由人写下的平行页：自行车把套直插、气泡内胆、椭圆截面与软鳍片。",
    )

    heading(document, "四、验证与改进", 1)
    heading(document, "4.1 测试与案例", 2)
    body(
        document,
        "2026年11月2日在本机执行 .venv\\Scripts\\python.exe -m pytest -q ，结果为 455 passed、2 deselected、1 warning，用时 45.23 秒。被排除的 2 条标记为 live_model，默认套件把真实模型调用分开。58 个测试文件覆盖登录与授权、草稿和版本的条件更新、候选的一次确认、证据索引、理论检索，以及从登录走到导出的纵向场景。",
    )
    body(
        document,
        "削皮器项目把上述能力放在同一条公开设计过程里检查。证据查找能回到「细硬金属柄在湿手和握力不足时打滑、疼痛」这句摘录。方法建议能返回用户访谈和可用性测试。过程检查给出的新页标题是具体问题，确认之后才出现在对应阶段。公开记述写明试用对象是厨师、家庭使用者和关节炎群体，并写明气泡方案被停下、软鳍片因看见就知道该捏而被保留。",
    )
    heading(document, "4.2 下一步", 2)
    for line in (
        "理论检索下一步改到带 torch 的 Python 3.12 或 3.13，接入 BGE 小模型，补上同义词和词序不同时的召回。",
        "审阅、导出、历史谱系和资源库的接口已经挂在服务上。下一步在 desk.html 中做成独立页面，并与现有三屏互相到达。",
        "每次运行下一步记下索引修订和摘要哈希，使同一次提问可以按当时的检索清单重放。",
        "请 3 至 5 名设计师用一个具体课题走建页、加证据、上传作品、从旧版本开新分支和导出，记录停顿与误解，再改交互。",
    ):
        bullet(document, line)

    heading(document, "五、AI 编程工具的使用", 1)
    body(
        document,
        "项目按「说清要看到的结果、生成实现、在浏览器里走通、再改」的循环完成。Grok 先搭起配置、数据库迁移、幂等命令和有界 DAG。Cursor 接上八阶段模板、页内节点、候选确认、读取范围、理论库和项目地图。WorkBuddy 接上界面中的章页画布、节点正文、关系边，并在真实 DeepSeek 上走完削皮器这条过程。",
    )
    body(
        document,
        "一次典型修改是证据检索。浏览器里新建锚点之后，用主题词搜索得到空结果。检查后看到两处原因：建锚点的请求当时把索引留在旧内容上；提问「下一步」时，检索词直接用了这句意图，而意图词对不上原件里的主题。随后改为建锚点时重建索引，并在检索词就是意图词时改用页面标题。改完后用 pytest 回归，再用削皮器原件里的「细硬金属柄」复测，摘录能够返回。",
    )

    heading(document, "六、仓库、运行与演示", 1)
    body(
        document,
        "代码仓库为 https://github.com/DomenRaven/sinuo-platform ，已设为公开。仓库内有源代码和 README 中的运行步骤。老师打开该链接即可阅读代码。",
    )
    body(
        document,
        "本机使用 Python 3.12 及以上。下面的命令在 Python 3.14.3 上完成过安装与测试。在 sinuo-platform 目录中执行：",
    )
    code = document.add_paragraph()
    code.paragraph_format.left_indent = Cm(0.75)
    code.paragraph_format.line_spacing = 1.15
    command = (
        "py -3.14 -m venv .venv\n"
        ".\\.venv\\Scripts\\python -m pip install -e \".[dev]\"\n"
        "Copy-Item .env.example .env\n"
        ".\\.venv\\Scripts\\alembic upgrade head\n"
        ".\\.venv\\Scripts\\uvicorn sinuo.api.app:create_app --factory --host 127.0.0.1 --port 8000"
    )
    _east(code.add_run(command), "Consolas", 10)
    body(
        document,
        "浏览器访问 http://127.0.0.1:8000 。演示账号为 synthetic-lead，密码为 synthetic-pass-lead。登录后打开「OXO Good Grips 削皮器」，并以用户研究章第一页「谁在使用削皮器，他们在什么场景下遇到手痛问题？」确认进入的是这一份 14 页演示。",
    )

    heading(document, "七、依据材料", 1)
    body(
        document,
        "产品与界面的定义写在仓库 docs/reference 中的《思诺智能体辅助设计平台产品白皮书》定稿 2.1，以及《思诺智能体辅助设计平台 UI 白皮书》1.3。削皮器演示中的馆藏事实来自 Cooper Hewitt 对 Good Grips 样机和成品的著录，结构事实来自 US4974286 与 USRE34194，设计过程事实来自 Smart Design 的公开说明，材料加工事实来自 Santoprene 厂商对热塑性硫化橡胶注塑的公开说明。",
    )
    enable_update_fields(document)
    try:
        document.save(DOC_PATH)
        print(DOC_PATH)
    except PermissionError:
        alt = DOC_PATH.with_name("3240103625_张玮勐_项目说明-新.docx")
        document.save(alt)
        print(alt)


if __name__ == "__main__":
    capture()
    build()
