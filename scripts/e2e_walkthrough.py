"""完整业务流程端到端实测：登录 → 阶段 → 上传证据 → 建锚点 → 写节点 →
读集 → 问下一步（真实 DeepSeek）→ 候选 → 预览 → 确认建新页 → 核对落库。

只用真实 HTTP 层 + 真实数据库 + 真实模型（走 .env 的 model_enabled）。
不调用任何领域内部函数，纯 TestClient。
"""

from __future__ import annotations

import hashlib
import json
import sys
import uuid

from fastapi.testclient import TestClient

from sinuo.api.app import create_app
from sinuo.config import Settings

PID = "061f59bc-cd9e-49dc-9d8c-cf42ac04e3eb"
ORIGIN = "http://127.0.0.1:8000"


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def step(name: str) -> None:
    print(f"\n{'=' * 60}\n▶ {name}")


def ok(name: str, resp, *keys: str) -> dict:
    print(f"  [{resp.status_code}] {name}")
    if resp.status_code >= 400:
        print("   body:", resp.text[:500])
    body = resp.json() if resp.content else {}
    for k in keys:
        if k in body.get("data", {}):
            print(f"   {k} = {body['data'][k]}")
    return body


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    settings = Settings()  # 读 .env：model_enabled=true，走真实 DeepSeek
    print("model_enabled =", settings.model_enabled)
    print("theory_root   =", settings.theory_root)
    app = create_app(settings)
    with TestClient(app) as client:
        H = {"Origin": ORIGIN}

        step("1. 登录")
        r = client.post(
            "/api/v1/session",
            json={"login_name": "Synthetic-Lead", "password": "synthetic-pass-lead"},
            headers=H,
        )
        print(f"  [{r.status_code}] login")
        sess = client.get("/api/v1/session", headers=H).json()["data"]
        csrf = sess["csrf_token"]
        print(f"   account_id = {sess['account_id']}")
        W = {**H, "X-CSRF-Token": csrf}

        step("2. 查看项目与 8 阶段")
        ok("projects", client.get("/api/v1/projects", headers=H))
        stages = client.get(f"/api/v1/projects/{PID}/stages", headers=H).json()["data"]
        for s in stages["stages"]:
            pages = [p["title"] for p in s["pages"]]
            print(f"   [{s['position']}] {s['name']}  pages={pages}")

        step("3. 上传证据（三段式：ticket → content → complete）")
        content = (
            "夜间取物访谈：受访者提到睡前会把手机放在床头柜，"
            "夜里想喝水要摸黑下床，经常碰到床边的东西。希望有一盏能被轻触就亮的小灯。"
        ).encode("utf-8")
        digest = sha256(content)
        r = client.post(
            f"/api/v1/projects/{PID}/artifacts/uploads",
            json={
                "name": "夜间取物访谈记录",
                "media_type": "text/plain",
                "size": len(content),
                "declared_hash": digest,
                "license": "synthetic",
                "sensitivity": "project",
                "ai_processing_allowed": 1,
            },
            headers={**W, "Idempotency-Key": str(uuid.uuid4())},
        )
        ticket = r.json()["data"]
        print(f"  [{r.status_code}] ticket_id = {ticket['ticket_id']}  artifact_id = {ticket['artifact_id']}")
        r = client.put(
            f"/api/v1/uploads/{ticket['ticket_id']}/content",
            content=content,
            headers={**W, "Idempotency-Key": str(uuid.uuid4())},
        )
        print(f"  [{r.status_code}] staged, byte_size = {r.json()['data']['byte_size']}")
        r = client.post(
            f"/api/v1/uploads/{ticket['ticket_id']}/complete",
            json={"declared_hash": digest},
            headers={**W, "Idempotency-Key": str(uuid.uuid4())},
        )
        av_id = r.json()["data"]["artifact_version_id"]
        print(f"  [{r.status_code}] artifact_version_id = {av_id}  version_no = {r.json()['data']['version_no']}")

        step("4. 建证据锚点（回到原件精确位置）")
        r = client.post(
            f"/api/v1/projects/{PID}/evidence-anchors",
            json={
                "artifact_version_id": av_id,
                "excerpt": "希望有一盏能被轻触就亮的小灯",
                "locator": {"page": 1, "start": 60, "end": 76},
            },
            headers={**W, "Idempotency-Key": str(uuid.uuid4())},
        )
        anchor_id = r.json()["data"]["evidence_anchor_id"]
        print(f"  [{r.status_code}] evidence_anchor_id = {anchor_id}")

        step("5. 写节点（problem / requirement / evidence）")
        page_id = "8e0711fc-aaf7-4a1d-8c99-eb5d00e34ecf"
        draft = client.get(f"/api/v1/pages/{page_id}/draft", headers=H).json()["data"]
        base = draft["draft"]
        nodes = [
            n for n in draft["nodes"] if n.get("kind")
        ]
        nodes = [
            {"node_id": n["node_id"], "kind": n["kind"], "duty": n["duty"], "record": n["record"]}
            for n in nodes
        ]
        nodes.append(
            {"node_id": str(uuid.uuid4()), "kind": "evidence", "duty": "evidence", "record": "formal"}
        )
        document = {"schema_version": 1, "nodes": nodes, "edges": []}
        r = client.patch(
            f"/api/v1/pages/{page_id}/draft",
            json={
                "base_version_id": base["base_version_id"],
                "expected_revision": base["content_revision"],
                "content": document,
            },
            headers={**W, "Idempotency-Key": str(uuid.uuid4())},
        )
        new_rev = r.json()["data"]["content_revision"]
        print(f"  [{r.status_code}] content_revision {base['content_revision']} -> {new_rev}")

        step("5b. checkpoint 固化草稿为新 head 版本")
        draft2 = client.get(f"/api/v1/pages/{page_id}/draft", headers=H).json()["data"]["draft"]
        r = client.post(
            f"/api/v1/pages/{page_id}/checkpoints",
            json={
                "expected_head_version_id": base["base_version_id"],
                "expected_content_revision": draft2["content_revision"],
                "expected_content_hash": draft2["content_hash"],
            },
            headers={**W, "Idempotency-Key": str(uuid.uuid4())},
        )
        if r.status_code == 201:
            new_head = r.json()["data"]["page_version_id"]
            print(f"  [{r.status_code}] 新 head 版本 = {new_head}  version_no = {r.json()['data']['version_no']}")
        else:
            print(f"  [{r.status_code}] {r.text[:300]}")
            new_head = base["base_version_id"]
        # checkpoint 后草稿 base 跟随新 head，content_revision 再 +1，重新读取
        draft3 = client.get(f"/api/v1/pages/{page_id}/draft", headers=H).json()["data"]["draft"]
        post_checkpoint_rev = draft3["content_revision"]
        print(f"  checkpoint 后草稿 revision = {post_checkpoint_rev}，base = {draft3['base_version_id'][:12]}")

        step("6. 读集（context-scope，T13 能力）")
        scope = client.get(f"/api/v1/projects/{PID}/context-scope", headers=H).json()["data"]
        pvs = [p["page_version_id"] for p in scope["page_versions"]]
        avs = [a["artifact_version_id"] for a in scope["artifact_versions"] if a["ai_processing_allowed"]]
        print(f"   page_versions = {len(pvs)}  evidence_anchors = {len(scope['evidence_anchors'])}  artifact_versions(ai) = {len(avs)}")

        step("7. 问下一步（真实 DeepSeek，带读集参数，基于新 head 版本）")
        head = new_head
        r = client.post(
            f"/api/v1/projects/{PID}/agent-runs",
            json={
                "intent": "下一步",
                "input_mode": "confirmed",
                "page_version_id": head,
                "page_version_ids": [head],
                "artifact_version_ids": avs,
                "exclusions": [],
            },
            headers={**W, "Idempotency-Key": str(uuid.uuid4())},
        )
        run_id = r.json()["data"]["run_id"]
        print(f"  [{r.status_code}] run_id = {run_id}")

        step("8. 读运行结果（证据命中 + critic 结论 + 来源溯源）")
        run = client.get(f"/api/v1/agent-runs/{run_id}", headers=H).json()["data"]
        print(f"   intent={run['intent']}  status={run['status_label']}")
        for d in run["duties"]:
            print(f"   -- {d['role_label']} [{d['status_label']}]")
            print(f"      summary: {(d['summary'] or '')[:80]}")
            if d["reads"]:
                print(f"      reads: {[(x['target'], x['label']) for x in d['reads']]}")
            if d["gaps"]:
                print(f"      gaps: {d['gaps'][:3]}")

        step("9. 读候选列表")
        cands = client.get(f"/api/v1/projects/{PID}/candidates", headers=H).json()["data"]["candidates"]
        pending = [c for c in cands if c["status"] == "pending"]
        print(f"   候选总数 = {len(cands)}，pending = {len(pending)}")
        for c in pending:
            print(f"   pending: {c['candidate_id'][:12]}  action={c['action']}  page_mode={c.get('page_mode')}  preview_ready={c['preview_ready']}  title={c.get('title')}")
        if not pending:
            print("   !! 无 pending 候选，停止")
            return
        cand = pending[0]
        cid = cand["candidate_id"]

        # 只有 preview_ready 的可执行候选（stage_blank）才走预览→确认→建新页。
        # draft_decision 摘要卡（critic 证据不足）是只读结论，不提供预览/确认。
        if not cand["preview_ready"]:
            print("   !! 该候选是只读摘要卡（preview_ready=False），无法预览/确认。")
            print("      摘要:", cand.get("summary"))
            print("      缺口:", cand.get("gaps"))
            print("      这是白皮书 V16 要求的正确行为：证据不足不给确定性设计结论。")
            return

        step("10. 预览候选")
        r = client.post(
            f"/api/v1/candidates/{cid}/preview",
            json={"expected_page_version_id": head, "expected_draft_content_revision": post_checkpoint_rev},
            headers=W,
        )
        if r.status_code != 200:
            print(f"  [{r.status_code}] {r.text[:300]}")
            return
        pv = r.json()["data"]
        print(f"  [{r.status_code}] preview_id = {pv['preview_id']}")
        print(f"   effective_proposal = {pv['effective_proposal']}")

        step("11. 确认候选 → 建新页")
        r = client.post(
            f"/api/v1/candidates/{cid}/commit",
            json={
                "preview_id": pv["preview_id"],
                "preview_hash": pv["preview_hash"],
                "expected_page_version_id": head,
                "expected_draft_content_revision": post_checkpoint_rev,
            },
            headers={**W, "Idempotency-Key": str(uuid.uuid4())},
        )
        if r.status_code not in (200, 201):
            print(f"  [{r.status_code}] {r.text[:300]}")
            return
        body = r.json()["data"]
        print(f"  [{r.status_code}] 新 page_id = {body.get('page_id')}  page_version_id = {body.get('page_version_id')}")
        if body.get("decision_id"):
            print(f"   decision_id = {body['decision_id']}")

        step("12. 核对：新页已建 + 来源边 + 决策记录")
        stages2 = client.get(f"/api/v1/projects/{PID}/stages", headers=H).json()["data"]
        for s in stages2["stages"]:
            pages = [p["title"] for p in s["pages"]]
            print(f"   [{s['position']}] {s['name']}  pages={pages}")

    print("\n" + "=" * 60)
    print("端到端实测完成")


if __name__ == "__main__":
    main()
