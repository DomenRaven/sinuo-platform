"""One confirm on the existing OXO project, plus the reads from the topic queries."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("walk", ROOT / "scripts" / "oxo_live_walk.py")
assert spec is not None and spec.loader is not None
walk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(walk)

PROJECT = "bd130115-16aa-452b-b02c-464fcb8d06cf"
VERSIONS = [
    "321fd3f5-cce8-4f78-b598-ad74a3b23d8e",
    "215f2c2b-7f2e-49cf-9600-c8fb8488a418",
    "c312d194-f331-406f-8ca5-019baa32e9b1",
    "cb5369ff-afb9-43bd-a9c1-4c1ebc8fe961",
    "dec3cc9f-9bba-48c2-a12b-f813d5bebef9",
]
REQUIREMENTS = "38cc7d0b-f302-4579-8914-af04d6af88f1"
RUNS = [
    "82f757714434b9e619f391caa2c4fa5edeb98ded3cc10d420f824650dcf435d9",
    "c2080b997de07f644e2f7d37a7e15636eece5ae743d6c0884ab027dfd0e27730",
]


def main() -> None:
    desk = walk.Desk()
    try:
        desk.login()
        for run_id in RUNS:
            run = desk.data(desk.call("GET", f"/api/v1/agent-runs/{run_id}"))
            desk.note(
                "saved-run",
                intent=run["intent"],
                duties=[
                    {"role": duty["role_label"], "summary": duty.get("summary"), "gaps": duty.get("gaps"), "reads": duty.get("reads")}
                    for duty in run["duties"]
                ],
            )
        page = desk.draft(REQUIREMENTS)
        result = walk.ask(desk, PROJECT, page, "下一步", VERSIONS, query="软鳍片")
        fresh = desk.draft(REQUIREMENTS)
        for row in result["pending"]:
            if row.get("page_mode") != "stage_blank" or not row["preview_ready"]:
                desk.note("judgment", candidate_id=row["candidate_id"], decision="leave", reason="不是可确认的空白新页。")
                continue
            if row.get("title") != "概念探索":
                walk._reject(desk, row, "这次只确认进入概念探索的建议。")
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
            desk.note("judgment", candidate_id=row["candidate_id"], decision="confirm", reason="要求已经记下，过程检查建议进入概念探索。预览后确认。", result=committed)
            new_page = committed.get("page_id")
            if isinstance(new_page, str):
                walk.write_page(
                    desk,
                    new_page,
                    [
                        (
                            "decision",
                            "这一页是确认过程检查的阶段建议后新建的。旁边三页仍是按公开过程写下的概念分叉，不把模型的建议当成已经选定的方案。",
                        )
                    ],
                    [],
                )
        outline = [
            {"stage": stage["name"], "pages": [item["title"] for item in stage["pages"]]}
            for stage in desk.stages(PROJECT)["stages"]
        ]
        desk.note("outline", stages=outline)
        path = ROOT / "var" / "oxo-walk" / "walk-confirm.jsonl"
        path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in desk.notes), encoding="utf-8")
        print("saved", path)
    finally:
        desk.close()


if __name__ == "__main__":
    main()
