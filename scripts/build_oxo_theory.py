"""Merge the OXO Good Grips case into the caller theory directory.

The running agent only embeds chunks whose library_key is ``course``. These
pages use that key and course ``oxo-good-grips``. Texts are original summaries
of public museum, patent, and manufacturer descriptions. They are not field
notes, and they do not copy courseware.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COURSE = "oxo-good-grips"
LIBRARY_KEY = "course"
TITLE_MAX = 80
SUMMARY_MAX = 160

PAGES: tuple[dict[str, object], ...] = (
    {
        "page_id": "oxo-scope",
        "title": "OXO削皮器语料边界",
        "summary": "Good Grips削皮器语料依据馆藏、专利和设计公司公开记述，供演示检索，不是本项目的一手访谈。",
        "links": (("oxo-task", "related"), ("oxo-limits", "related")),
        "text": (
            "这是思诺演示用的OXO Good Grips削皮器资料，不是本项目自己做的用户访谈。"
            "事实来自Cooper Hewitt馆藏说明、美国专利US4974286及其再版USRE34194，以及Smart Design的公开项目记述。"
            "材料加工只写Santoprene这类热塑性硫化橡胶的公开工艺原理，不把今天的牌号参数写成1990年的生产记录。"
            "检索到这些页时，要说明来源是公开记述，不能把馆藏转述说成现场观察。"
        ),
    },
    {
        "page_id": "oxo-task",
        "title": "OXO削皮器的任务情境",
        "summary": "1989年Sam Farber见妻子用金属削皮器手痛，于是委托Smart Design做人人好握的厨房工具。",
        "links": (("oxo-research", "related"), ("oxo-problem", "related"), ("oxo-universal", "related")),
        "text": (
            "1989年，前Copco负责人Sam Farber看到妻子Betsey使用普通金属蔬菜削皮器时握持困难、手部疼痛，"
            "决定做一套让更多人好用的厨房工具，并成立OXO。他找来曾经合作的纽约公司Smart Design，"
            "设计主持是Davin Stowell和Dan Formosa。任务不是只给关节炎患者做一件专用器具，"
            "而是追问：如果工具握起来舒服，普通人会不会也愿意用。公开记述把这一定位称为通用设计。"
            "关键词：削皮器、任务情境、Good Grips、厨房工具。"
        ),
    },
    {
        "page_id": "oxo-research",
        "title": "OXO削皮器的用户研究",
        "summary": "设计团队访谈消费者和厨师，并跟随关节炎群体志愿者，观察真实厨房里的握持困难。",
        "links": (("oxo-motion", "related"), ("method-用户访谈", "uses"), ("concept-用户访谈", "uses")),
        "text": (
            "Smart Design没有把削皮器只交给设计师自己试用。公开记述写到，他们做了实地研究，"
            "了解关节炎和其他动作障碍，与消费者和厨师交谈，并检查已有厨房工具。"
            "ONE杂志的访问还写到，团队花时间跟随纽约一个关节炎群体的志愿者，观察年龄带来的手部灵活度变化。"
            "这些是设计公司事后的公开说法。演示项目里应标成二手资料，不要写成你们采访了Betsey或这些志愿者。"
            "关键词：用户研究、访谈、观察、厨师、关节炎。"
        ),
    },
    {
        "page_id": "oxo-motion",
        "title": "厨房工具的手部动作分类",
        "summary": "团队按手的动作把工具分成推拉、扭转和挤压，两份记述对削皮器落在哪一类并不一致。",
        "links": (("oxo-requirements", "related"), ("method-任务分析", "uses")),
        "text": (
            "研究之后，Smart Design按手腕和手的动作给家用工具分类。馆藏说明的三类是："
            "推拉，例如刀和擦丝器；扭转，例如搅拌勺、削皮器和勺；挤压，例如剪刀和夹子。"
            "ONE杂志的另一处说法把开罐器放进扭转，把削皮器和擦丝器放进推拉。"
            "两份公开记述对削皮器落在哪一类并不完全一致。演示时要保留这个分歧，不要合成成唯一分类。"
            "分类用来决定把手要服务哪一种施力。关键词：手部动作、推拉、扭转、挤压、任务分析。"
        ),
    },
    {
        "page_id": "oxo-problem",
        "title": "金属削皮器的握持问题",
        "summary": "细硬金属柄在湿手和握力不足时打滑、疼痛，削某些食材又离不开这件工具。",
        "links": (("oxo-requirements", "related"),),
        "text": (
            "Cooper Hewitt整理的过程说明写到，Farber通过研究和观察认识到：已有削皮器在潮湿时难用，"
            "关节炎或握力有限的人难以握住，而准备某些食物又需要削皮。"
            "他自己的话被馆藏转述为，普通厨房工具会弄痛手，剪刀环痛，细硬把手也痛。"
            "问题因此不是刀刃不够锋利这一句就能说完，而是手、水、力和工具截面叠在一起。"
            "没有这些证据时，不能把结论写成某一种把手必然正确。关键词：问题定义、湿滑、细柄、握力。"
        ),
    },
    {
        "page_id": "oxo-requirements",
        "title": "Good Grips的设计要求",
        "summary": "委托要求工具耐用、可进洗碗机、好看、价格可接受，并且握持舒服、湿手不滑。",
        "links": (("oxo-concepts", "related"), ("oxo-material", "related")),
        "text": (
            "Smart Design接到的要求，在馆藏样机说明里被写成：耐用、可进洗碗机、有吸引力、价格可接受、握持舒服。"
            "过程说明补充了安全和湿态防滑。这些要求同时约束材料和造型。"
            "软胶如果不能耐受洗碗机的热水和洗涤剂，就通不过要求；只加厚金属柄，又解决不了湿滑和疼痛。"
            "要求是后面判断概念和样机的尺子。关键词：设计要求、洗碗机、防滑、舒适、耐用。"
        ),
    },
    {
        "page_id": "oxo-concepts",
        "title": "削皮器把手的概念分叉",
        "summary": "早期把金属削皮器插入自行车把套，也试过气泡内胆，最后收成软鳍片和椭圆大把手。",
        "links": (("oxo-prototypes", "related"), ("oxo-patent", "related")),
        "lead": "鳍片：削皮器把手两侧的软鳍片叫Fingerprint softspot。按下鳍片可以贴合握法，这是概念分叉里留下的方案。",
        "text": (
            "团队更愿意从实物改，而不是从概念草图开始。最早的削皮器样机之一，是把金属削皮器插进自行车橡胶把套。"
            "他们判断多用途把手要做得比较大，以减少手部紧张，截面用椭圆，避免在手里转动，并留一个逐渐放大的孔方便悬挂。"
            "防滑靠两侧可弯曲的鳍片，按下去贴合各人的握法。Smart Design把这处称为Fingerprint softspot，指纹软点。"
            "另一条早期路线是光滑表面下的可变形气泡。功能上说得通，但被认为不够直觉，"
            "因为人看见自行车把套就知道该捏，藏起来的气泡则看不出来。关键词：概念探索、自行车把套、鳍片、椭圆把手、气泡。"
        ),
    },
    {
        "page_id": "oxo-prototypes",
        "title": "削皮器样机的阶段",
        "summary": "馆藏留下约二十件样机，分成握形、抓握、挂孔和削皮器本体的多轮实物。",
        "links": (("oxo-test", "related"), ("concept-原型", "uses"), ("method-可用性测试", "uses")),
        "text": (
            "2011年Cooper Hewitt从Smart Design选取样机入藏。馆藏说明写，这批约二十件代表过程中的阶段："
            "五件把手形态研究，四件抓握研究，三件孔的研究，以及八个阶段的削皮器发展，"
            "从插在自行车把套里的金属削皮器一直到Santoprene把手和最终削皮器。"
            "入藏的设计图不是最初的概念速写，而是接近制作样机时的制造图。"
            "Stowell用计算机绘制的通用把手图，被记述为他最早完全用计算机生成的设计。"
            "关键词：样机、原型、形态研究、挂孔、Santoprene。"
        ),
    },
    {
        "page_id": "oxo-patent",
        "title": "通用把手专利里的结构",
        "summary": "专利US4974286写椭圆弹性体、内腔套芯杆、两侧凹槽和可按压鳍片，并给出一个粗壮尺寸。",
        "links": (("oxo-material", "related"), ("oxo-molding", "related")),
        "text": (
            "通用把手的美国专利是US4974286，1990年3月26日申请，同年12月4日授权。"
            "发明人是Davin Stowell和Michael Callahan，最初受让人是Smart Design，随后转到OXO International。"
            "再版专利号是USRE34194。专利描述弹性体制成的把手，有缓冲和隔热，内部空腔用来套住工具芯杆。"
            "截面为椭圆。靠近上端的两侧各有凹面，里面固定一排间隔开的柔性鳍片，拇指和食指按下鳍片来加强抓握。"
            "专利举例的材料是氯丁橡胶或合成橡胶，这段文字没有写Santoprene这个牌号。"
            "一个实施例的尺寸是长3又4分之3英寸、宽1又8分之3英寸、厚8分之7英寸，用来说明把手相对粗壮，握不成紧拳的人仍能扣住。"
            "芯杆若是直圆柱，专利写可能需要把芯杆和把手粘住，防止拔出。这是专利文本，不是车间工艺卡。"
            "关键词：专利、芯杆、鳍片、椭圆截面、装配。"
        ),
    },
    {
        "page_id": "oxo-material",
        "title": "削皮器上的Santoprene",
        "summary": "馆藏把约1990年的削皮器材料记为不锈钢和Santoprene热塑性橡胶，整件约17.8厘米长。",
        "links": (("oxo-molding", "related"),),
        "text": (
            "Cooper Hewitt对约1990年Good Grips削皮器实物的著录写明，材料是不锈钢和Santoprene，"
            "并注明它是热塑性橡胶，制造者是OXO International，设计公司是Smart Design。"
            "该件尺寸约为17.8厘米乘2.5厘米乘2.5厘米。"
            "馆藏样机说明还写，设计者最终选用当时孟山都的Santoprene，一种原本用于洗碗机密封件的塑料与橡胶合成材料。"
            "因此洗碗机要求与材料选择在公开记述里是连在一起的。"
            "专利正文用弹性体举例，博物馆用Santoprene著录成品。两处不要合并成一条未经记载的配方。"
            "关键词：Santoprene、不锈钢、热塑性橡胶、洗碗机、材料。"
        ),
    },
    {
        "page_id": "oxo-molding",
        "title": "TPV注塑的加工原理",
        "summary": "Santoprene属于可在热塑性设备上注塑的TPV，流动性更随剪切变化，这不是OXO当年的模具记录。",
        "links": (),
        "lead": "注塑：Santoprene是TPV热塑性硫化橡胶。削皮器的软鳍片把手可以用注塑制造。这不是OXO当年的模具记录。",
        "text": (
            "Santoprene是热塑性硫化橡胶，英文简称TPV。厂商公开说明里，它把已经硫化的橡胶微粒分散在塑料基体中，"
            "所以既能用普通热塑性注塑或挤出设备加工，又保留接近橡胶的手感。"
            "熔体在低剪切时较稠，剪切提高后变稀，升高温度对黏度的影响相对小，因此浇口和注射速度比单纯升温更能帮助充模。"
            "加工前通常建议干燥。公开指南还写它和乙缩醛、PVC不相容。"
            "这些句子用来理解这类材料为什么能做成软鳍片，不能当成OXO在1990年使用的模温、干燥时间或具体牌号。"
            "今天面向洗碗机密封的牌号写明能在常见洗涤剂中保持性能，只说明材料族有这条能力。"
            "关键词：注塑、TPV、热塑性硫化橡胶、模具、剪切、制造。"
        ),
    },
    {
        "page_id": "oxo-universal",
        "title": "这件削皮器上的通用设计",
        "summary": "通用设计追求尽量多的人不用专门改造就能使用，Good Grips用软大把手而不是单独的病人用具。",
        "links": (("oxo-requirements", "related"),),
        "text": (
            "通用设计的公开定义是：尽可能让更多人使用产品和环境，而不必另做专门改造或专用版本。"
            "这一定义来自北卡罗来纳州立大学通用设计中心的公开说明。"
            "OXO和Smart Design把Good Grips放在这个主张下：从握力不足的人那里发现的粗、软、湿手不滑的把手，"
            "同时卖给没有关节炎的家庭厨师。它不是把金属削皮器缩小一号。"
            "演示里可以用这个原则检查概念，但不能把原则细则当成这件产品已经逐条测试过的证据。"
            "常被列举的原则名称是公平使用、弹性使用、简单直觉、信息可感知、容错、低体力负担、适当的尺寸与空间。"
            "关键词：通用设计、包容、握力、软把手。"
        ),
    },
    {
        "page_id": "oxo-test",
        "title": "削皮器样机的试用",
        "summary": "公开记述写样机交给厨师、家庭使用者和关节炎群体，用来比较把手形状和材料。",
        "links": (("method-可用性测试", "uses"), ("concept-可用性测试", "uses"), ("oxo-limits", "related")),
        "text": (
            "Cooper Hewitt的过程说明写，几种迭代样机由厨师、家庭厨师和关节炎群体试用，"
            "以判断哪种材料和形状更省力、更安全。Smart Design的访问则强调，最终的Santoprene鳍片有意接近自行车把套，"
            "因为人看见就想捏。光滑外壳里的气泡方案被放弃，理由是功能成立但不够直觉。"
            "这些测试在公开资料里没有给出样本量、任务脚本或失败次数。"
            "演示项目如果要写测试记录，必须标明这是对公开过程的复述，缺少原始记录，不能补造通过率。"
            "关键词：可用性测试、试用、厨师、关节炎群体、样机迭代。"
        ),
    },
    {
        "page_id": "oxo-stages",
        "title": "削皮器演示可放的阶段",
        "summary": "同一案例可拆成情境、研究、问题、要求、概念分叉、样机、试用和复盘，概念和样机适合各放多页。",
        "links": (
            ("oxo-task", "related"),
            ("oxo-research", "related"),
            ("oxo-concepts", "related"),
            ("oxo-prototypes", "related"),
            ("oxo-test", "related"),
        ),
        "text": (
            "用这件削皮器做思诺演示时，阶段可以这样放。任务与情境放Farber的委托和人人好握的目标。"
            "用户与场景研究可以分成家庭使用、厨师和关节炎群体三页，不要自动连成先后。"
            "问题及机会定义放湿滑和细柄疼痛。设计要求放耐用、洗碗机、价格、舒适和防滑。"
            "概念探索至少放三页：自行车把套直插、被放弃的气泡内胆、椭圆截面加软鳍片。"
            "深化与原型按馆藏的形态、抓握、挂孔和削皮器本体分页。测试与迭代只记录公开说过的试用对象，并写明没有原始数据。"
            "表达与复盘说明它后来扩展成一组厨房工具，以及专利、馆藏和材料牌号各自证明什么。"
            "关键词：设计流程、阶段、平行页、概念分叉、方案迭代。"
        ),
    },
    {
        "page_id": "oxo-limits",
        "title": "削皮器资料里不能外推的数字",
        "summary": "馆藏转述过当时的关节炎人数，演示不能把它当成现在的统计，也不能补造测试通过率。",
        "links": (("oxo-scope", "related"),),
        "text": (
            "Cooper Hewitt的样机说明转引1996年文章，提到Farber曾把美国关节炎人数作为一个规模判断。"
            "那是历史转述，不是本语料测算的现况，演示时不要当成今天的流行病学数字。"
            "样机件数约二十件，来自2011年入藏说明，不是生产线上的全部试模。"
            "专利实施例的英寸尺寸只说明一个粗壮把手，不是馆藏那件17.8厘米削皮器的测量值，两套尺寸不要当成同一件。"
            "公开试用没有样本量。智能体回答测试效果时，要说资料没有给出通过率。"
            "关键词：边界、样本量、尺寸、转述。"
        ),
    },
)


def _chunk(page_id: str, chunk_id: str, text: str) -> dict[str, str]:
    return {
        "library_key": LIBRARY_KEY,
        "course": COURSE,
        "page_id": page_id,
        "chunk_id": chunk_id,
        "text": text,
        "text_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "source_path": f"corpus/oxo/{page_id}.md",
    }


def chunks_and_graph() -> tuple[list[dict[str, str]], dict[str, dict[str, str]], list[dict[str, str]]]:
    chunks: list[dict[str, str]] = []
    pages: dict[str, dict[str, str]] = {}
    links: list[dict[str, str]] = []
    seen: set[str] = set()
    for page in PAGES:
        page_id = str(page["page_id"])
        title = str(page["title"])
        summary = str(page["summary"])
        text = str(page["text"]).strip()
        if page_id in seen:
            raise SystemExit(f"duplicate page {page_id}")
        seen.add(page_id)
        _check_label(page_id, title, summary, text)
        pages[page_id] = {"title": title, "summary": summary}
        bodies = [text]
        lead = page.get("lead")
        if isinstance(lead, str) and lead.strip():
            bodies.insert(0, lead.strip())
        for index, body in enumerate(bodies):
            suffix = "lead" if index == 0 and len(bodies) > 1 else "case"
            chunks.append(_chunk(page_id, f"{page_id}#{suffix}", body))
        for target, kind in page["links"]:  # type: ignore[union-attr]
            links.append(
                {
                    "source_page_id": page_id,
                    "target_page_id": str(target),
                    "kind": str(kind),
                }
            )
    return chunks, pages, links


def merge_into(directory: Path) -> tuple[int, int]:
    directory.mkdir(parents=True, exist_ok=True)
    chunks_path = directory / "chunks.json"
    graph_path = directory / "graph.json"
    old_chunks = json.loads(chunks_path.read_text(encoding="utf-8")) if chunks_path.is_file() else []
    old_graph = json.loads(graph_path.read_text(encoding="utf-8")) if graph_path.is_file() else {"pages": {}, "links": []}
    if not isinstance(old_chunks, list) or not isinstance(old_graph, dict):
        raise SystemExit("existing theory corpus is not the expected shape")
    kept_chunks = [row for row in old_chunks if not _is_oxo(row)]
    old_pages = old_graph.get("pages")
    old_links = old_graph.get("links")
    if not isinstance(old_pages, dict) or not isinstance(old_links, list):
        raise SystemExit("existing theory graph is not the expected shape")
    kept_pages = {key: value for key, value in old_pages.items() if not str(key).startswith("oxo-")}
    kept_links = [row for row in old_links if not _oxo_link(row)]
    new_chunks, new_pages, new_links = chunks_and_graph()
    missing = sorted(
        {
            row["target_page_id"]
            for row in new_links
            if row["target_page_id"] not in kept_pages and row["target_page_id"] not in new_pages
        }
    )
    if missing:
        raise SystemExit("OXO links point at missing pages: " + ", ".join(missing))
    chunks_path.write_text(
        json.dumps(kept_chunks + new_chunks, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    graph_path.write_text(
        json.dumps(
            {"pages": {**kept_pages, **new_pages}, "links": kept_links + new_links},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    _write_sources(ROOT / "corpus" / "oxo")
    return len(new_chunks), len(kept_chunks)


def _write_sources(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for page in PAGES:
        page_id = str(page["page_id"])
        lead = page.get("lead")
        lead_block = f"{lead}\n\n" if isinstance(lead, str) and lead.strip() else ""
        body = (
            f"# {page['title']}\n\n"
            f"{page['summary']}\n\n"
            f"{lead_block}{page['text']}\n\n"
            "来源口径：Cooper Hewitt馆藏与过程说明、US4974286 / USRE34194、"
            "Smart Design公开记述、Santoprene厂商注塑指南、通用设计中心的公开定义。"
            "正文是依据这些公开材料写成的检索摘要，不是原文摘录。\n"
        )
        (directory / f"{page_id}.md").write_text(body, encoding="utf-8")


def _is_oxo(row: object) -> bool:
    return isinstance(row, dict) and (
        str(row.get("course")) == COURSE or str(row.get("page_id", "")).startswith("oxo-")
    )


def _oxo_link(row: object) -> bool:
    if not isinstance(row, dict):
        return False
    return str(row.get("source_page_id", "")).startswith("oxo-") or str(
        row.get("target_page_id", "")
    ).startswith("oxo-")


def _check_label(page_id: str, title: str, summary: str, text: str) -> None:
    if "\n" in title or "\n" in summary or title != title.strip() or summary != summary.strip():
        raise SystemExit(f"{page_id} label has whitespace")
    if len(title) > TITLE_MAX or len(summary) > SUMMARY_MAX or not text:
        raise SystemExit(f"{page_id} label length title={len(title)} summary={len(summary)}")


def main() -> None:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "var" / "theory"
    added, kept = merge_into(target)
    print(f"oxo chunks {added}; kept existing {kept}; directory {target}")


if __name__ == "__main__":
    main()
