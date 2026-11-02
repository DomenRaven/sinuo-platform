"""验证两个 demo 修复：
1. 选方法（关节炎群体的握持页）应命中方法卡，而不是"没有匹配的方法"。
2. 检查下一步（厨师与关节炎群体试用页）应产出 suggest_stage_transition（建复盘页），而不是 draft_decision。
验证后拒绝产出的候选卡，避免污染用户录屏环境。
"""

from __future__ import annotations

import json
import sys
import time
import uuid

import httpx

ORIGIN = "http://127.0.0.1:8000"
PROJECT_ID = "ef389cb5-1079-43f3-b5f0-98f17f4df597"


class Desk:
    def __init__(self) -> None:
        self.client = httpx.Client(base_url=ORIGIN, timeout=httpx.Timeout(300.0, connect=10.0))
        self.csrf = ""

    def headers(self, write: bool = False) -> dict[str, str]:
        h = {"Origin": ORIGIN}
        if write:
            h["X-CSRF-Token"] = self.csrf
            h["Idempotency-Key"] = str(uuid.uuid4())
        return h

    def call(self, method, path, *, write=False, **kw):
        r = self.client.request(method, path, headers=self.headers(write), **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {r.status_code} {r.text[:400]}")
        return r

    def data(self, r):
        b = r.json()
        d = b.get("data")
        if not isinstance(d, dict):
            raise RuntimeError(f"missing data: {r.text[:200]}")
        return d

    def login(self):
        self.call("POST", "/api/v1/session", json={"login_name": "synthetic-lead", "password": "synthetic-pass-lead"})
        self.csrf = str(self.data(self.call("GET", "/api/v1/session"))["csrf_token"])


def find_page(desk, title):
    stages = desk.data(desk.call("GET", f"/api/v1/projects/{PROJECT_ID}/stages"))
    for stage in stages["stages"]:
        for page in stage["pages"]:
            if page["title"] == title:
                return page
    return None


def wait_run(desk, run_id):
    for _ in range(120):
        run = desk.data(desk.call("GET", f"/api/v1/agent-runs/{run_id}"))
        status = run.get("execution_status") or run.get("status")
        if status in ("completed", "partial", "failed"):
            return run
        time.sleep(1.5)
    return desk.data(desk.call("GET", f"/api/v1/agent-runs/{run_id}"))


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    desk = Desk()
    desk.login()
    print("已登录\n")

    # --- 修复 1：选方法 ---
    page = find_page(desk, "关节炎群体的握持")
    print(f"[选方法] 页 {page['title']} head={page['head_version_id'][:8]}")
    opened = desk.data(
        desk.call(
            "POST",
            f"/api/v1/projects/{PROJECT_ID}/agent-runs",
            write=True,
            json={
                "intent": "选方法",
                "input_mode": "confirmed",
                "page_version_id": page["head_version_id"],
                "query": "",
                "question": "",
                "artifact_version_ids": [],
                "exclusions": [],
            },
        )
    )
    run = wait_run(desk, opened["run_id"])
    for duty in run.get("duties", []):
        if duty.get("role") == "method":
            lookup = duty.get("output", {}).get("lookup", {})
            print(f"  方法建议 lookup.status = {lookup.get('status')}")
            print(f"  命中卡片 = {[c['name'] for c in lookup.get('cards', [])]}")
            print(f"  method_gap = {duty.get('output', {}).get('method_gap')}")
    print()

    # --- 修复 2：检查下一步 ---
    page2 = find_page(desk, "厨师与关节炎群体试用")
    print(f"[检查下一步] 页 {page2['title']} head={page2['head_version_id'][:8]}")
    opened2 = desk.data(
        desk.call(
            "POST",
            f"/api/v1/projects/{PROJECT_ID}/agent-runs",
            write=True,
            json={
                "intent": "下一步",
                "input_mode": "confirmed",
                "page_version_id": page2["head_version_id"],
                "query": "样机试用",
                "question": "",
                "artifact_version_ids": [],
                "exclusions": [],
            },
        )
    )
    run2 = wait_run(desk, opened2["run_id"])
    candidates = desk.data(desk.call("GET", f"/api/v1/projects/{PROJECT_ID}/candidates"))["candidates"]
    pending = [c for c in candidates if c["run_id"] == opened2["run_id"] and c["status"] == "pending"]
    print(f"  运行 {opened2['run_id'][:8]} 候选卡 {len(pending)} 条：")
    for c in pending:
        print(f"    action={c['action']} page_mode={c.get('page_mode')} title={c.get('title')}")
    # 清理：拒绝本次产生的候选卡
    for c in pending:
        desk.call("POST", f"/api/v1/candidates/{c['candidate_id']}/reject", write=True, json={"reason": "验证后清理"})
        print(f"    已拒绝 {c['candidate_id'][:8]} 以清理")
    print()
    print("验证完成")


if __name__ == "__main__":
    main()
