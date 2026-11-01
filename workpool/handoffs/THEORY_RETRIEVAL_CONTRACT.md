# 课程知识检索合同

日期：2026-11-01。用户决定按白皮书第 7.4 节做向量召回和 GraphRAG。语料用本机已有库，不新下载整本教材或论文。假模型链不改。

## 语料

- 课程理论库：`F:/工业设计理论库`。课程是 `design-psychology` 与 `ux-product-innovation`。概念页在 `wiki/concepts`，方法页在 `wiki/methods`，原文在 `raw/`。已有 `scripts/search.py` 和 `index/bm25.json`。
- 本机向量模型：`D:/huggingface/hub/models--BAAI--bge-small-zh-v1.5`。
- `G:/` 没有理论库。`E:/文三路AI馆` 是项目档案，本轮不入库。

仓库里不复制 `raw/` 或课件原文。运行时索引放在调用方给出的目录，默认不进 Git。

## 身份

一块片段必须带 `library_key`、`course`、`page_id`、`chunk_id`、`source_path` 和正文哈希。课程页是 `library_key=course`。项目事实仍只属于该项目。检索先按这个身份过滤，再算相似。

## 分工

- 清单只写 `workpool/handoffs/THEORY_CORPUS_MANIFEST.md`。
- 向量只写 `src/sinuo/application/retrieval/vectors.py` 和 `tests/acceptance/test_theory_vectors.py`。
- 图检索只写 `src/sinuo/application/retrieval/graphrag.py` 和 `tests/acceptance/test_theory_graph.py`。
- 谁都不改 `execute.py`、`fake_chain.py`、迁移和 `app.py`。接入运行是下一步。

## 行为

向量召回返回片段 id、来源和分数，不返回整门课。没有命中就空结果。图检索沿 wiki 页之间已有链接走有限跳，摘要必须引用页 id。没有 RDF 推理，也不称本体推理。测试用临时小语料，不把 F 盘原文抄进测试文件。
