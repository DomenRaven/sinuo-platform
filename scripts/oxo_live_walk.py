"""Drive the live Sinuo server through the public OXO peeler process.

Human pages, nodes, uploads, and confirmations are sent over HTTP.
Agent runs use whatever model the server already has enabled.
Judgments are printed. This script does not invent test scores.
"""

from __future__ import annotations

import hashlib
import json
import sys
import uuid
from pathlib import Path

import httpx

ORIGIN = "http://127.0.0.1:8000"
ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "var" / "oxo-walk" / "walk.jsonl"
DUTY = {
    "problem": "clarification",
    "evidence": "evidence",
    "insight": "evidence",
    "requirement": "critic",
    "artifact": "human",
    "test": "human",
    "decision": "human",
}
INVENTED = ("通过率为", "样本量为", "百分之", "显著提高", "临床试验")


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class Desk:
    def __init__(self) -> None:
        self.client = httpx.Client(base_url=ORIGIN, timeout=httpx.Timeout(240.0, connect=10.0))
        self.csrf = ""
        self.notes: list[dict[str, object]] = []

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
            raise RuntimeError(f"{method} {path} -> {response.status_code} {response.text[:500]}")
        return response

    def data(self, response: httpx.Response) -> dict:
        body = response.json()
        payload = body.get("data")
        if not isinstance(payload, dict):
            raise RuntimeError(f"missing data: {response.text[:300]}")
        return payload

    def login(self) -> None:
        self.call(
            "POST",
            "/api/v1/session",
            json={"login_name": "synthetic-lead", "password": "synthetic-pass-lead"},
        )
        session = self.data(self.call("GET", "/api/v1/session"))
        self.csrf = str(session["csrf_token"])

    def stages(self, project_id: str) -> dict:
        return self.data(self.call("GET", f"/api/v1/projects/{project_id}/stages"))

    def draft(self, page_id: str) -> dict:
        return self.data(self.call("GET", f"/api/v1/pages/{page_id}/draft"))

    def note(self, kind: str, **payload: object) -> None:
        row = {"kind": kind, **payload}
        self.notes.append(row)
        print(f"\n## {kind}")
        print(json.dumps(payload, ensure_ascii=False)[:1200])


def upload(desk: Desk, project_id: str, name: str, text: str, excerpt: str) -> str:
    raw = text.encode("utf-8")
    digest = sha256(raw)
    ticket = desk.data(
        desk.call(
            "POST",
            f"/api/v1/projects/{project_id}/artifacts/uploads",
            write=True,
            json={
                "name": name,
                "media_type": "text/plain",
                "size": len(raw),
                "declared_hash": digest,
                "license": "public-account-summary",
                "sensitivity": "project",
                "ai_processing_allowed": 1,
            },
        )
    )
    desk.call(
        "PUT",
        f"/api/v1/uploads/{ticket['ticket_id']}/content",
        write=True,
        content=raw,
    )
    registered = desk.data(
        desk.call(
            "POST",
            f"/api/v1/uploads/{ticket['ticket_id']}/complete",
            write=True,
            json={"declared_hash": digest},
        )
    )
    start = text.find(excerpt)
    if start < 0:
        raise RuntimeError(f"excerpt missing from {name}")
    desk.call(
        "POST",
        f"/api/v1/projects/{project_id}/evidence-anchors",
        write=True,
        json={
            "artifact_version_id": registered["artifact_version_id"],
            "excerpt": excerpt,
            "locator": {"page": 1, "start": start, "end": start + len(excerpt)},
        },
    )
    desk.note("evidence", name=name, artifact_version_id=registered["artifact_version_id"], excerpt=excerpt)
    return str(registered["artifact_version_id"])


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
            json={
                "stage_id": stage["stage_id"],
                "title": title,
                "expected_revision": reading["revision"],
            },
        )
    )
    desk.note("page", stage=stage["name"], title=title, page_id=created["page_id"])
    return str(created["page_id"])


def write_page(desk: Desk, page_id: str, nodes: list[tuple[str, str]], edges: list[tuple[int, int]]) -> dict:
    stored = []
    for kind, content in nodes:
        stored.append(
            {
                "node_id": str(uuid.uuid4()),
                "kind": kind,
                "duty": DUTY[kind],
                "record": "formal",
                "content": content,
            }
        )
    edge_rows = []
    for source, target in edges:
        edge_rows.append(
            {
                "edge_id": str(uuid.uuid4()),
                "source_node_id": stored[source]["node_id"],
                "target_node_id": stored[target]["node_id"],
                "kind": "derives",
            }
        )
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
    checkpoint = desk.data(
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
    )
    fresh = desk.draft(page_id)
    desk.note(
        "checkpoint",
        page=current["title"],
        version_no=checkpoint.get("version_no"),
        page_version_id=checkpoint["page_version_id"],
        nodes=[item[0] for item in nodes],
    )
    return fresh


def ask(
    desk: Desk,
    project_id: str,
    page: dict,
    intent: str,
    versions: list[str],
    *,
    query: str = "",
    question: str = "",
) -> dict:
    head = page["head_version_id"]
    opened = desk.data(
        desk.call(
            "POST",
            f"/api/v1/projects/{project_id}/agent-runs",
            write=True,
            json={
                "intent": intent,
                "input_mode": "confirmed",
                "page_version_id": head,
                "query": query,
                "question": question,
                "artifact_version_ids": versions,
                "exclusions": [],
            },
        )
    )
    run = desk.data(desk.call("GET", f"/api/v1/agent-runs/{opened['run_id']}"))
    candidates = desk.data(desk.call("GET", f"/api/v1/projects/{project_id}/candidates"))["candidates"]
    pending = [row for row in candidates if row["run_id"] == opened["run_id"] and row["status"] == "pending"]
    desk.note(
        "run",
        intent=intent,
        page=page["title"],
        run_id=opened["run_id"],
        duties=[
            {
                "role": duty["role_label"],
                "status": duty["status_label"],
                "summary": duty.get("summary"),
                "gaps": duty.get("gaps"),
            }
            for duty in run["duties"]
        ],
        pending=[
            {
                "candidate_id": row["candidate_id"],
                "action": row["action"],
                "page_mode": row["page_mode"],
                "title": row.get("title"),
                "summary": row.get("summary"),
                "gaps": row.get("gaps"),
                "preview_ready": row["preview_ready"],
                "target_stage_id": row.get("target_stage_id"),
            }
            for row in pending
        ],
    )
    return {"page": page, "run": run, "pending": pending}


def invented(row: dict) -> bool:
    text = " ".join(
        [
            str(row.get("summary") or ""),
            str(row.get("title") or ""),
            " ".join(row.get("gaps") or []),
        ]
    )
    return any(token in text for token in INVENTED)


def judge(desk: Desk, project_id: str, result: dict) -> None:
    reading = desk.stages(project_id)
    by_id = {stage["stage_id"]: stage for stage in reading["stages"]}
    current = next(stage for stage in reading["stages"] if any(page["page_id"] == result["page"]["page_id"] for page in stage["pages"]))
    fresh = desk.draft(result["page"]["page_id"])
    for row in result["pending"]:
        if invented(row) and row["preview_ready"]:
            _reject(desk, row, "公开资料没有给出通过率或样本量，这条候选不能确认。")
            continue
        if row.get("page_mode") != "stage_blank" or not row["preview_ready"]:
            desk.note("judgment", candidate_id=row["candidate_id"], decision="leave", reason="不是可确认的空白新页，保留为过程记录。")
            continue
        target = by_id.get(row.get("target_stage_id"))
        if target is None:
            _reject(desk, row, "目标阶段对不上项目里的阶段。")
            continue
        if target["pages"]:
            _reject(desk, row, "这一阶段已经有按公开过程写下的页，不重复开一页。")
            continue
        if target["position"] <= current["position"]:
            _reject(desk, row, "公开过程的下一步不是停在当前阶段或退回更早阶段。")
            continue
        preview = desk.data(
            desk.call(
                "POST",
                f"/api/v1/candidates/{row['candidate_id']}/preview",
                write=True,
                json={
                    "expected_page_version_id": fresh["head_version_id"],
                    "expected_draft_content_revision": fresh["draft"]["content_revision"],
                },
            )
        )
        committed = desk.data(
            desk.call(
                "POST",
                f"/api/v1/candidates/{row['candidate_id']}/commit",
                write=True,
                json={
                    "preview_id": preview["preview_id"],
                    "preview_hash": preview["preview_hash"],
                    "expected_page_version_id": fresh["head_version_id"],
                    "expected_draft_content_revision": fresh["draft"]["content_revision"],
                },
            )
        )
        desk.note(
            "judgment",
            candidate_id=row["candidate_id"],
            decision="confirm",
            reason=f"预览后确认进入{target['name']}。该阶段当时还没有页。",
            result=committed.get("page_id") or committed.get("status"),
        )


def _reject(desk: Desk, row: dict, reason: str) -> None:
    desk.call(
        "POST",
        f"/api/v1/candidates/{row['candidate_id']}/reject",
        write=True,
        json={"reason": reason},
    )
    desk.note("judgment", candidate_id=row["candidate_id"], decision="reject", reason=reason)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    desk = Desk()
    try:
        alive = desk.client.get("/healthz").json()
        print("health", alive)
        desk.login()
        created = desk.data(
            desk.call(
                "POST",
                "/api/v1/projects",
                write=True,
                json={
                    "title": "OXO Good Grips 削皮器",
                    "page_title": "Farber的委托",
                    "brief": "根据馆藏、专利和设计公司公开记述复刻削皮器的设计过程。不是一手访谈。",
                },
            )
        )
        project_id = str(created["project_id"])
        desk.note("project", project_id=project_id, home_page_id=created["page_id"])
        versions = [
            upload(
                desk,
                project_id,
                "公开记述-任务与握持",
                "根据Cooper Hewitt和Smart Design的公开记述整理。1989年Sam Farber看到妻子Betsey用金属削皮器时手痛。"
                "细硬金属柄在湿手和握力不足时打滑、疼痛。准备某些食物又需要削皮。",
                "细硬金属柄在湿手和握力不足时打滑、疼痛",
            ),
            upload(
                desk,
                project_id,
                "公开记述-研究与动作",
                "公开记述写设计团队访谈消费者和厨师，并跟随纽约关节炎群体的志愿者。"
                "手部动作被分成推拉、扭转、挤压。两份记述对削皮器落在哪一类并不一致。",
                "两份记述对削皮器落在哪一类并不一致",
            ),
            upload(
                desk,
                project_id,
                "公开记述-要求与概念",
                "委托要求耐用、可进洗碗机、有吸引力、价格可接受、握持舒服，并且湿手不滑。"
                "早期把金属削皮器插入自行车把套。气泡内胆因为不够直觉被放弃。留下椭圆截面和软鳍片。",
                "气泡内胆因为不够直觉被放弃",
            ),
            upload(
                desk,
                project_id,
                "公开记述-样机专利与材料",
                "馆藏留下约二十件样机，包括形态、抓握、挂孔和削皮器本体。"
                "专利US4974286写椭圆弹性体、内腔套芯杆和可按压鳍片。馆藏成品材料记为不锈钢和Santoprene。"
                "专利没有写Santoprene牌号，也没有写模具温度。",
                "专利没有写Santoprene牌号，也没有写模具温度",
            ),
            upload(
                desk,
                project_id,
                "公开记述-试用边界",
                "样机交给厨师、家庭使用者和关节炎群体比较形状和材料。"
                "公开资料没有样本量，也没有通过率。不能把1996年转述的关节炎人数当成现在的统计。",
                "公开资料没有样本量，也没有通过率",
            ),
        ]
        home = write_page(
            desk,
            str(created["page_id"]),
            [
                ("problem", "妻子用普通金属削皮器时手痛。任务不是做一件只有病人使用的专用工具。"),
                ("insight", "如果握起来舒服，没有关节炎的人可能也会用。这是公开记述里的通用设计判断，不是本次访谈。"),
            ],
            [(0, 1)],
        )
        judge(desk, project_id, ask(desk, project_id, home, "简报变化", versions, question="妻子用金属削皮器手痛，是否做成专用工具"))
        home = desk.draft(str(created["page_id"]))
        judge(desk, project_id, ask(desk, project_id, home, "下一步", versions, query="细硬金属柄"))

        research = [
            (
                "家庭厨房里的削皮",
                [
                    ("evidence", "公开记述的起点是家庭里的金属削皮器：湿手、细柄、手痛。"),
                    ("insight", "家庭场景说明问题不是只出现在专业厨房。"),
                ],
            ),
            (
                "厨师的连续削切",
                [
                    ("evidence", "公开记述写团队和厨师谈过，并用厨师试用后来的样机。没有原始访谈稿。"),
                    ("insight", "厨师代表长时间、重复的削切，和偶尔做一顿饭的握持不是同一种负荷。"),
                ],
            ),
            (
                "关节炎群体的握持",
                [
                    ("evidence", "公开记述写团队跟随纽约一个关节炎群体的志愿者，观察手部灵活度。"),
                    ("insight", "握不成紧拳的人需要较粗的把手。这是后来专利把把手做粗的原因之一。"),
                ],
            ),
        ]
        stage_pages = desk.stages(project_id)["stages"][1]["pages"]
        assigned: list[tuple[str, str, list]] = []
        used: set[str] = set()
        for title, nodes in research:
            match = next((page for page in stage_pages if page["title"] == title), None)
            if match is not None:
                assigned.append((str(match["page_id"]), title, nodes))
                used.add(str(match["page_id"]))
        leftovers = [page for page in stage_pages if str(page["page_id"]) not in used]
        for title, nodes in research:
            if any(item[1] == title for item in assigned):
                continue
            if leftovers:
                adopted = leftovers.pop(0)
                desk.note("adopt", stage="用户与场景研究", kept_title=adopted["title"], content=title)
                assigned.append((str(adopted["page_id"]), str(adopted["title"]), nodes))
            else:
                assigned.append((ensure_page(desk, project_id, 1, title), title, nodes))
        research_pages = [write_page(desk, page_id, nodes, [(0, 1)]) for page_id, _title, nodes in assigned]
        judge(desk, project_id, ask(desk, project_id, research_pages[2], "选方法", versions, question="用户访谈和可用性测试"))

        problem = write_page(
            desk,
            ensure_page(desk, project_id, 2, "湿滑与细柄"),
            [
                ("problem", "潮湿时难握，握力不足时细硬金属柄会痛，而有些食材又必须削皮。"),
                ("evidence", "两份公开记述对削皮器属于推拉还是扭转并不一致，问题定义先保留这个分歧。"),
                ("insight", "问题叠在手、水、力和截面上，不能收成刀刃不够锋利。"),
            ],
            [(1, 0), (0, 2)],
        )
        judge(desk, project_id, ask(desk, project_id, problem, "查证据", versions, query="细硬金属柄"))

        requirements = write_page(
            desk,
            ensure_page(desk, project_id, 3, "五条握持要求"),
            [
                ("requirement", "耐用，可进洗碗机，价格可接受，握持舒服，湿手不滑。好看被公开记述单独提到。"),
                ("decision", "要求同时约束材料和造型：只加厚金属柄解决不了湿滑和疼痛。"),
            ],
            [(0, 1)],
        )
        judge(desk, project_id, ask(desk, project_id, requirements, "下一步", versions, query="软鳍片"))

        concepts = [
            (
                "自行车把套直插",
                [
                    ("insight", "最早的实物概念是把金属削皮器插进自行车橡胶把套。人看见就知道该捏。"),
                    ("artifact", "这是概念模型，不是馆藏最终件。"),
                ],
            ),
            (
                "气泡内胆",
                [
                    ("insight", "光滑表面下的可变形气泡在功能上说得通。"),
                    ("decision", "公开记述写它被放弃，因为不够直觉。演示不补造当时的评分。"),
                ],
            ),
            (
                "椭圆截面与软鳍片",
                [
                    ("requirement", "把手要大，椭圆截面避免在手里转动，两侧鳍片按下后贴合握法。"),
                    ("decision", "这是留下的方向。Fingerprint softspot 是设计公司的叫法。"),
                ],
            ),
        ]
        for title, nodes in concepts:
            write_page(desk, ensure_page(desk, project_id, 4, title), nodes, [(0, 1)])

        write_page(
            desk,
            ensure_page(desk, project_id, 5, "形态抓握与挂孔"),
            [
                ("artifact", "馆藏约二十件样机分成形态、抓握、挂孔和削皮器本体。这里只记这个阶段，不把件数当成全部试模。"),
                ("insight", "制造图出现在接近做样机的时候。公开记述说他们很少从概念草图开始。"),
            ],
            [(1, 0)],
        )
        write_page(
            desk,
            ensure_page(desk, project_id, 5, "芯杆与软胶"),
            [
                ("artifact", "专利写弹性体内部空腔套住芯杆，两侧凹槽里有可按压鳍片。一个实施例把把手做粗。"),
                ("evidence", "馆藏成品材料是不锈钢和Santoprene。专利正文没有写这个牌号，也没有写模具温度。"),
            ],
            [(1, 0)],
        )
        tested = write_page(
            desk,
            ensure_page(desk, project_id, 6, "厨师与关节炎群体试用"),
            [
                ("test", "样机交给厨师、家庭使用者和关节炎群体，比较材料和形状。公开资料没有样本量，也没有通过率。"),
                ("decision", "气泡方案因不直觉停止。鳍片被保留，是因为看见就知道该捏。不能补写测试成绩。"),
            ],
            [(0, 1)],
        )
        judge(desk, project_id, ask(desk, project_id, tested, "下一步", versions, query="样机试用"))
        write_page(
            desk,
            ensure_page(desk, project_id, 7, "三份资料各自证明什么"),
            [
                ("decision", "馆藏证明成品材料和样机阶段。专利证明结构。材料厂商指南只说明TPV可以注塑，不能当成1990年的工艺卡。"),
                ("insight", "这件削皮器后来扩展成一组厨房工具。演示到此停止，不外推今天的关节炎人数。"),
            ],
            [(0, 1)],
        )
        final = desk.stages(project_id)
        outline = [
            {"stage": stage["name"], "pages": [page["title"] for page in stage["pages"]]}
            for stage in final["stages"]
        ]
        desk.note("outline", project_id=project_id, stages=outline)
        LOG.parent.mkdir(parents=True, exist_ok=True)
        LOG.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in desk.notes), encoding="utf-8")
        print("\nlog", LOG)
    finally:
        desk.close()


if __name__ == "__main__":
    main()
