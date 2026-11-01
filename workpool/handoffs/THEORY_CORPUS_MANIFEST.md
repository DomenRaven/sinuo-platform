# 课程语料清单

日期：2026-11-01。只统计本机 `F:/工业设计理论库`，不把课件或抽取正文拷进仓库。本文件没有原文、没有向量、也没有新下载的文献。

库根：`F:/工业设计理论库`。`scripts/search.py` 的 `ROOT` 指向同一路径。

## 课程

| 课程 ID | 课程名 | 讲次笔记 | 课程目录文件 | raw 讲次目录 | raw 文件 |
| --- | --- | --- | --- | --- | --- |
| `design-psychology` | 设计心理学 | 18 | 19（含 `README.md`） | 18 | 72 |
| `ux-product-innovation` | 用户体验与产品创新设计 | 11 | 12（含 `README.md`） | 11 | 44 |

课程名来自 `wiki/courses/<course>/README.md` 的 `title`。两门课之外，`raw/` 没有第三门课。

## 页数

| 位置 | 文件数 | 说明 |
| --- | --- | --- |
| `wiki/concepts` | 39 | 全部是 `concept-*.md`，无子目录 |
| `wiki/methods` | 7 | 全部是 `method-*.md`，无子目录 |
| `wiki/courses` | 31 | 29 份讲次笔记 + 2 份课程 `README.md` |

概念页 39，方法页 7。文件名主干与 frontmatter `id` 一致。

## 方法页

| 文件名 | id | status | cite_tier | course |
| --- | --- | --- | --- | --- |
| `method-亲和图.md` | `method-亲和图` | verified | narrow | design-psychology |
| `method-任务分析.md` | `method-任务分析` | verified | cite | design-psychology, ux-product-innovation |
| `method-卡片分类.md` | `method-卡片分类` | index_only | source | design-psychology |
| `method-可用性测试.md` | `method-可用性测试` | verified | narrow | design-psychology, ux-product-innovation |
| `method-启发式评估.md` | `method-启发式评估` | verified | cite | design-psychology, ux-product-innovation |
| `method-用户访谈.md` | `method-用户访谈` | verified | narrow | design-psychology, ux-product-innovation |
| `method-问卷.md` | `method-问卷` | verified | cite | design-psychology, ux-product-innovation |

`index_only` 的有效分级按 `source`。上表不包含步骤或产出物正文。

## raw 文件构成

每讲一个目录，固定带 `extract.md`、`extract_stats.json`、`meta.json`，再加一份课件。

| 课程 | extract.md | extract_stats.json | meta.json | pdf | ppt | pptx | 合计 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| design-psychology | 18 | 18 | 18 | 5 | 4 | 9 | 72 |
| ux-product-innovation | 11 | 11 | 11 | 0 | 1 | 10 | 44 |
| 合计 | 29 | 29 | 29 | 5 | 5 | 19 | 116 |

`raw/` 共 116 个文件。这些文件留在 F 盘，不入库。

## 现成索引

`index/` 只有三个文件，没有向量索引。

| 文件 | 类型 | 规模 | 谁在用 |
| --- | --- | --- | --- |
| `index/pages.jsonl` | 词法页记录 | 80 行 | `search.py` 每次都读 |
| `index/bm25.json` | BM25 词项 | 80 条，与页记录对齐 | `bm25`、`hybrid` |
| `index/lectures.json` | 讲次清单 | 29 条 | `search.py` 不读 |

`pages.jsonl` 由 `scripts/build_index.py` 写出。每行有 `id`、`type`、`title`、`aliases`、`tags`、`course`、`lecture_ids`、`status`、`cite_tier`、`cite_note`、`path`、`sources`、`evidence_score`、`best_def_score`、`quality_weight`、`body`、`search_text`。其中 `body` 和 `search_text` 含页面正文，不能拷进仓库。80 行的类型是：concept 39、method 7、lecture 29、moc 4、index 1。

`bm25.json` 的键是 `ids`、`paths`、`tokens`。`ids` 与 `pages.jsonl` 的 `id` 顺序一致。

`lectures.json` 是课件入库清单，29 条对应 18 + 11 个讲次目录。字段是 `course`、`course_title`、`lecture_id`、`title`、`source_original`、`source_raw`、`ext`、`bytes`。

现有记录还没有合同要求的 `library_key`、`chunk_id`、正文哈希。`id` 可作 `page_id`，`path` 是 wiki 相对路径，`sources` 指向 raw。

## search.py 的 mode

`scripts/search.py` 的 `--mode` 取值：`exact`、`fuzzy`、`bm25`、`hybrid`。默认 `hybrid`。

| mode | 读哪些索引 | 行为 |
| --- | --- | --- |
| `exact` | `pages.jsonl` | 标题、别名或 `search_text` 的子串 |
| `fuzzy` | `pages.jsonl` | rapidfuzz，分数低于 0.45 丢掉 |
| `bm25` | `pages.jsonl` + `bm25.json` | `BM25Okapi`，分数按当次最高分归一 |
| `hybrid` | `pages.jsonl` + `bm25.json` | `0.45 * fuzzy + 0.35 * bm25 + 0.20 * exact`，再乘 `quality_weight` |

另有 `--limit`（默认 8）和 `--json`。没有向量或图检索模式。
