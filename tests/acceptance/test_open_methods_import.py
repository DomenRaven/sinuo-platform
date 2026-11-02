"""Import synthetic open method cards. No downloaded method text."""

import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

from sinuo.application.retrieval.graphrag import TheoryLink, TheoryPage, query_theory_graph
from sinuo.application.retrieval.vectors import search_theory_vectors

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "import_open_methods.py"
QUERY = "按压按钮需要明确的行程反馈"
PLANT = "叶绿体类囊体薄膜上进行光反应"
SKIP = "SYNTHETIC_SKIP"
LIMIT = "SYNTHETIC_LIMIT"
FORBIDDEN = "SYNTHETIC_FORBIDDEN_BODY"
ANCHOR = "18f-anchor-method"
SUPPORT = "18f-support-method"
LONG = "18f-long-title"
SIGNED = "odk-signed-card"
PEER = "odk-signed-peer"

ANCHOR_PAGE = f"""---
layout: card
title: 合成锚点
permalink: /decide/anchor-method/
redirect_from:
  - /anchor-method/
description: {QUERY}
what: {QUERY}
why: |
  {QUERY}
timeRequired: 1 hour
---
# 合成锚点

## How to do it

1. {QUERY}
![skip](not-a-page.png)
{{% link _methods/support-method.md %}}

## Considerations for use in government

{LIMIT}

## Additional resources

{SKIP}
"""

SUPPORT_PAGE = f"""---
layout: card
title: 合成支持
permalink: /discover/support-method/
description: {PLANT}
what: {PLANT}
why: {PLANT}
---
## How to do it

1. {PLANT}
"""

LONG_PAGE = f"""---
layout: card
title: {"测" * 90}
description: {"述" * 200}
what: {"述" * 200}
why: {"述" * 200}
---
## How to do it

长标题只用于检查截断。
"""

TEMPLATE_PAGE = f"""---
layout: card
title: 模板
description: {QUERY}
---
## How to do it

{SKIP}
"""

LOOSE_PAGE = f"""---
title: 不该入库
description: {QUERY}
---
{QUERY}
{SKIP}
"""

SIGNED_PAGE = f"""---
layout: methods
title: 署名卡片
category: explore
teaser: 署名行必须保留许可名称
---
## Why do it:

署名行必须保留许可名称。

## One way to do it:

1. 保留许可名称
[peer](/methods/signed-peer/)

## Learn More:

{SKIP}
"""

PEER_PAGE = """---
layout: methods
title: 邻接卡片
teaser: 邻接卡片只保存连线
---
## One way to do it:

1. 邻接卡片只保存连线
"""

README_PAGE = f"""# 说明

{SKIP}
{QUERY}
"""


def test_download_allowlist_is_the_two_repos() -> None:
    module = _load_script()
    assert module.ALLOWED_REPOS == (
        "https://github.com/18F/methods",
        "https://github.com/open-design-kit/opendesignkit",
    )
    assert module.ARCHIVE_URLS == (
        "https://github.com/18F/methods/archive/refs/heads/main.zip",
        "https://github.com/open-design-kit/opendesignkit/archive/refs/heads/master.zip",
    )
    urls = set(re.findall(r"https://[^\s\"')]+", SCRIPT.read_text(encoding="utf-8")))
    prefixes = (
        "https://github.com/18F/methods",
        "https://github.com/open-design-kit/opendesignkit",
        "https://creativecommons.org/licenses/by/4.0/",
    )
    assert urls
    assert all(any(url.startswith(prefix) for prefix in prefixes) for url in urls)
    folded = " ".join(urls).casefold()
    for banned in ("ideo", "hyperisland", "gov.uk", "iso.org"):
        assert banned not in folded


def test_synthetic_cards_are_searchable(tmp_path: Path) -> None:
    source_18f, source_odk = _write_sources(tmp_path)
    out = tmp_path / "theory-open"
    completed = _run(source_18f, source_odk, out)
    assert completed.returncode == 0
    assert SKIP not in completed.stdout
    assert LIMIT not in completed.stdout
    assert "source=18F Methods license=CC0-1.0 cards=3" in completed.stdout
    assert "source=Open Design Kit license=CC-BY-4.0 cards=2" in completed.stdout

    chunks = json.loads((out / "chunks.json").read_text(encoding="utf-8"))
    graph = json.loads((out / "graph.json").read_text(encoding="utf-8"))
    assert {item["page_id"] for item in chunks} == {ANCHOR, SUPPORT, LONG, SIGNED, PEER}
    assert set(graph["pages"]) == {ANCHOR, SUPPORT, LONG, SIGNED, PEER}
    assert graph["links"] == [
        {
            "source_page_id": ANCHOR,
            "target_page_id": SUPPORT,
            "kind": "related",
        },
        {
            "source_page_id": SIGNED,
            "target_page_id": PEER,
            "kind": "related",
        },
    ]
    by_id = {item["page_id"]: item for item in chunks}
    anchor = by_id[ANCHOR]
    assert anchor["library_key"] == "course"
    assert anchor["course"] == "18F Methods"
    assert anchor["chunk_id"] == f"{ANCHOR}#method"
    assert QUERY in anchor["text"]
    assert LIMIT in anchor["text"]
    assert "18F Methods" in anchor["text"]
    assert "CC0" in anchor["text"]
    assert "https://github.com/18F/methods" in anchor["text"]
    assert anchor["source_path"] == "18f-methods/_methods/anchor-method.md"
    assert "{%" not in anchor["text"]
    assert "<a" not in anchor["text"]
    assert "not-a-page.png" not in anchor["text"]
    signed = by_id[SIGNED]
    assert signed["course"] == "Open Design Kit"
    assert "Open Design Kit" in signed["text"]
    assert "CC BY 4.0" in signed["text"]
    assert "https://github.com/open-design-kit/opendesignkit" in signed["text"]
    assert "https://creativecommons.org/licenses/by/4.0/" in signed["text"]
    assert signed["source_path"] == "open-design-kit/_methods/signed-card.md"
    assert by_id[SUPPORT]["text"].count(PLANT) >= 1
    assert by_id[SUPPORT]["course"] == "18F Methods"
    long_page = graph["pages"][LONG]
    assert long_page["title"] == "测" * 80
    assert long_page["summary"] == "述" * 160
    for item in chunks:
        assert item["library_key"] == "course"
        assert item["text_hash"] == hashlib.sha256(item["text"].encode("utf-8")).hexdigest()
        assert SKIP not in item["text"]
        assert ":" not in item["source_path"]
        assert "\\" not in item["source_path"]
        assert len(item["text"]) <= 8000
        assert len(item["source_path"]) <= 240
        assert item["course"] in {"18F Methods", "Open Design Kit"}
    for body in graph["pages"].values():
        assert body["title"] == body["title"].strip()
        assert body["summary"] == body["summary"].strip()
        assert "\n" not in body["title"]
        assert "\n" not in body["summary"]
        assert body["title"]
        assert body["summary"]
        assert len(body["title"]) <= 80
        assert len(body["summary"]) <= 160

    found = search_theory_vectors(chunks, QUERY, 5)
    assert found.hits
    assert {hit.page_id for hit in found.hits} == {ANCHOR}
    pages = {
        page_id: TheoryPage(body["title"], body["summary"])
        for page_id, body in graph["pages"].items()
    }
    links = [
        TheoryLink(item["source_page_id"], item["target_page_id"], item["kind"])
        for item in graph["links"]
    ]
    walked = query_theory_graph(pages, links, found.hits[0].page_id, QUERY, 2)
    assert ANCHOR in walked.summary
    assert SUPPORT in walked.summary


def test_rejected_license_writes_nothing(tmp_path: Path) -> None:
    source_18f = tmp_path / "bad-18f"
    source_odk = tmp_path / "odk"
    _write_odk(source_odk)
    (source_18f / "_methods").mkdir(parents=True)
    (source_18f / "LICENSE.md").write_text(
        "Attribution-NonCommercial-NoDerivatives 4.0. All rights reserved.\n",
        encoding="utf-8",
    )
    (source_18f / "_methods" / "secret.md").write_text(
        f"---\ntitle: Hidden\n---\n## How to do it\n\n{FORBIDDEN}\n",
        encoding="utf-8",
    )
    out = tmp_path / "out"
    completed = _run(source_18f, source_odk, out)
    assert completed.returncode == 2
    assert FORBIDDEN not in completed.stdout
    assert FORBIDDEN not in completed.stderr
    assert not (out / "chunks.json").exists()
    assert not (out / "graph.json").exists()


def _write_sources(tmp_path: Path) -> tuple[Path, Path]:
    source_18f = tmp_path / "18f"
    (source_18f / "_methods").mkdir(parents=True)
    (source_18f / "LICENSE.md").write_text(
        "CC0 1.0 Universal public domain dedication.\n",
        encoding="utf-8",
    )
    (source_18f / "README.md").write_text(README_PAGE, encoding="utf-8")
    methods = source_18f / "_methods"
    (methods / "anchor-method.md").write_text(ANCHOR_PAGE, encoding="utf-8")
    (methods / "support-method.md").write_text(SUPPORT_PAGE, encoding="utf-8")
    (methods / "long-title.md").write_text(LONG_PAGE, encoding="utf-8")
    (methods / "_template.md").write_text(TEMPLATE_PAGE, encoding="utf-8")
    (methods / "loose-note.md").write_text(LOOSE_PAGE, encoding="utf-8")
    source_odk = tmp_path / "odk"
    _write_odk(source_odk)
    return source_18f, source_odk


def _write_odk(root: Path) -> None:
    methods = root / "_methods"
    methods.mkdir(parents=True)
    (root / "LICENSE").write_text(
        "Creative Commons Attribution 4.0 International Public License.\n",
        encoding="utf-8",
    )
    (methods / "signed-card.md").write_text(SIGNED_PAGE, encoding="utf-8")
    (methods / "signed-peer.md").write_text(PEER_PAGE, encoding="utf-8")


def _run(source_18f: Path, source_odk: Path, out: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--18f",
            str(source_18f),
            "--odk",
            str(source_odk),
            "--out",
            str(out),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _load_script():
    spec = importlib.util.spec_from_file_location("import_open_methods", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("script")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
