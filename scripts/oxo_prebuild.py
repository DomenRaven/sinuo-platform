"""预搭建：在现有 OXO 项目 ef389cb5 上补齐节点与页（不触发任何智能体）。

只做「建页 + 写节点 + 连承接边 + 确认版本」，把骨架搭满到 8 章 13 页，
刻意留出「表达与复盘」章的智能体建页空位（复盘页），供用户录屏时触发
「检查下一步」由过程检查建出。

本脚本不新建项目、不重复上传证据、不调用 agent-runs。
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import httpx

ORIGIN = "http://127.0.0.1:8000"
ROOT = Path(__file__).resolve().parents[1]
PROJECT_ID = "ef389cb5-1079-43f3-b5f0-98f17f4df597"

DUTY = {
    "problem": "clarification",
    "evidence": "evidence",
    "insight": "evidence",
    "requirement": "critic",
    "artifact": "human",
    "test": "human",
    "decision": "human",
}


class Desk:
    def __init__(self) -> None:
        self.client = httpx.Client(base_url=ORIGIN, timeout=httpx.Timeout(120.0, connect=10.0))
        self.csrf = ""

    def close(self) -> None:
        self.client.close()

    def headers(self, write: bool = False) -> dict[str, str]:
        headers = {"Origin": ORIGIN}
        if write:
            headers["X-CSRF-Token"] = self.csrf
            headers["Idempotency-Key"] = str(uuid.uuid4())
        return headers

    def call(self, method: str, path: str, *, write: bool = False, **kwargs) -> httpx.Response:
        response = self.client.request(method, path, headers=self.headers(write), **kwargs)
        if response.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {response.status_code} {response.text[:600]}")
        return response

    def data(self, response: httpx.Response) -> dict:
        body = response.json()
        payload = body.get("data")
        if not isinstance(payload, dict):
            raise RuntimeError(f"missing data: {response.text[:300]}")
        return payload

    def login(self) -> None:
        self.call("POST", "/api/v1/session", json={"login_name": "synthetic-lead", "password": "synthetic-pass-lead"})
        session = self.data(self.call("GET", "/api/v1/session"))
        self.csrf = str(session["csrf_token"])

    def stages(self, project_id: str) -> dict:
        return self.data(self.call("GET", f"/api/v1/projects/{project_id}/stages"))

    def draft(self, page_id: str) -> dict:
        return self.data(self.call("GET", f"/api/v1/pages/{page_id}/draft"))


def ensure_page(desk: Desk, project_id: str, position: int, title: str) -> str:
    reading = desk.stages(project_id)
    stage = reading["stages"][position]
    for page in stage["pages"]:
        if page["title"] == title:
            return str(page["page_id"])
    created = desk.data(
        desk.call(
            "POST",
            f"/api/v1/projects/{project_id}/pages",
            write=True,
            json={"stage_id": stage["stage_id"], "title": title, "expected_revision": reading["revision"]},
        )
    )
    print(f"  建页 [{stage['name']}] {title}")
    return str(created["page_id"])


def write_page(desk: Desk, page_id: str, nodes: list[tuple[str, str]], edges: list[tuple[int, int]]) -> None:
    stored = [
        {"node_id": str(uuid.uuid4()), "kind": kind, "duty": DUTY[kind], "record": "formal", "content": content}
        for kind, content in nodes
    ]
    edge_rows = [
        {
            "edge_id": str(uuid.uuid4()),
            "source_node_id": stored[source]["node_id"],
            "target_node_id": stored[target]["node_id"],
            "kind": "derives",
        }
        for source, target in edges
    ]
    current = desk.draft(page_id)
    base = current["draft"]
    desk.call(
        "PATCH",
        f"/api/v1/pages/{page_id}/draft",
        write=True,
        json={
            "base_version_id": base["base_version_id"],
            "expected_revision": base["content_revision"],
            "content": {"schema_version": 1, "nodes": stored, "edges": edge_rows},
        },
    )
    updated = desk.draft(page_id)["draft"]
    desk.call(
        "POST",
        f"/api/v1/pages/{page_id}/checkpoints",
        write=True,
        json={
            "expected_head_version_id": base["base_version_id"],
            "expected_content_revision": updated["content_revision"],
            "expected_content_hash": updated["content_hash"],
        },
    )
    print(f"  写节点 {[k for k, _ in nodes]} + {len(edge_rows)} 边  ·  {current['title']}")


# position -> [(title, [(kind, content), ...], [(source_idx, target_idx), ...]), ...]
PLAN: dict[int, list] = {
    1: [  # 用户与场景研究（第 2 章）：补 2 个空页
        (
            "厨师的连续削切",
            [
                ("evidence", "公开记述写团队和厨师谈过，并用厨师试用后来的样机。没有原始访谈稿。"),
                ("insight", "厨师代表长时间、重复的削切，和偶尔做一顿饭的握持不是同一种负荷。"),
            ],
            [(0, 1)],
        ),
        (
            "关节炎群体的握持",
            [
                ("evidence", "公开记述写团队跟随纽约一个关节炎群体的志愿者，观察手部灵活度。"),
                ("insight", "握不成紧拳的人需要较粗的把手。这是后来专利把把手做粗的原因之一。"),
            ],
            [(0, 1)],
        ),
    ],
    2: [  # 问题及机会定义（第 3 章）
        (
            "湿滑与细柄",
            [
                ("problem", "潮湿时难握，握力不足时细硬金属柄会痛，而有些食材又必须削皮。"),
                ("evidence", "两份公开记述对削皮器属于推拉还是扭转并不一致，问题定义先保留这个分歧。"),
                ("insight", "问题叠在手、水、力和截面上，不能收成刀刃不够锋利。"),
            ],
            [(1, 0), (0, 2)],
        ),
    ],
    3: [  # 设计要求（第 4 章）
        (
            "五条握持要求",
            [
                ("requirement", "耐用，可进洗碗机，价格可接受，握持舒服，湿手不滑。好看被公开记述单独提到。"),
                ("decision", "要求同时约束材料和造型：只加厚金属柄解决不了湿滑和疼痛。"),
            ],
            [(0, 1)],
        ),
    ],
    4: [  # 概念探索（第 5 章）
        (
            "自行车把套直插",
            [
                ("insight", "最早的实物概念是把金属削皮器插进自行车橡胶把套。人看见就知道该捏。"),
                ("artifact", "这是概念模型，不是馆藏最终件。"),
            ],
            [(0, 1)],
        ),
        (
            "气泡内胆",
            [
                ("insight", "光滑表面下的可变形气泡在功能上说得通。"),
                ("decision", "公开记述写它被放弃，因为不够直觉。演示不补造当时的评分。"),
            ],
            [(0, 1)],
        ),
        (
            "椭圆截面与软鳍片",
            [
                ("requirement", "把手要大，椭圆截面避免在手里转动，两侧鳍片按下后贴合握法。"),
                ("decision", "这是留下的方向。Fingerprint softspot 是设计公司的叫法。"),
            ],
            [(0, 1)],
        ),
    ],
    5: [  # 深化与原型（第 6 章）
        (
            "形态抓握与挂孔",
            [
                ("artifact", "馆藏约二十件样机分成形态、抓握、挂孔和削皮器本体。这里只记这个阶段，不把件数当成全部试模。"),
                ("insight", "制造图出现在接近做样机的时候。公开记述说他们很少从概念草图开始。"),
            ],
            [(0, 1)],
        ),
        (
            "芯杆与软胶",
            [
                ("artifact", "专利写弹性体内部空腔套住芯杆，两侧凹槽里有可按压鳍片。一个实施例把把手做粗。"),
                ("evidence", "馆藏成品材料是不锈钢和Santoprene。专利正文没有写这个牌号，也没有写模具温度。"),
            ],
            [(0, 1)],
        ),
    ],
    6: [  # 测试与迭代（第 7 章）
        (
            "厨师与关节炎群体试用",
            [
                ("test", "样机交给厨师、家庭使用者和关节炎群体，比较材料和形状。公开资料没有样本量，也没有通过率。"),
                ("decision", "气泡方案因不直觉停止。鳍片被保留，是因为看见就知道该捏。不能补写测试成绩。"),
            ],
            [(0, 1)],
        ),
    ],
    7: [  # 表达与复盘（第 8 章）：只建手动页，留「复盘」给智能体
        (
            "三份资料各自证明什么",
            [
                ("decision", "馆藏证明成品材料和样机阶段。专利证明结构。材料厂商指南只说明TPV可以注塑，不能当成1990年的工艺卡。"),
                ("insight", "这件削皮器后来扩展成一组厨房工具。演示到此停止，不外推今天的关节炎人数。"),
            ],
            [(0, 1)],
        ),
    ],
}


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    desk = Desk()
    try:
        alive = desk.client.get("/healthz").json()
        print("health:", alive)
        desk.login()
        before = desk.stages(PROJECT_ID)
        print(f"项目 {PROJECT_ID[:8]} 修订 {before['revision']}，开始预搭建…\n")
        for position, items in sorted(PLAN.items()):
            for title, nodes, edges in items:
                page_id = ensure_page(desk, PROJECT_ID, position, title)
                write_page(desk, page_id, nodes, edges)
        final = desk.stages(PROJECT_ID)
        print("\n=== 最终大纲 ===")
        total = 0
        for stage in final["stages"]:
            names = [p["title"] for p in stage["pages"]]
            total += len(names)
            print(f"  {stage['name']}: {len(names)} 页 -> {names}")
        print(f"\n共 {len(final['stages'])} 章 {total} 页（预期 8 章 13 页，第 8 章留「复盘」待智能体建）")
        print("修订号:", final["revision"])
    finally:
        desk.close()


if __name__ == "__main__":
    main()
