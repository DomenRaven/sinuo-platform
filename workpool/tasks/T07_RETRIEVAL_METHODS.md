# T07 证据锚点、检索与方法库

- 状态：implementing。2026-11-01 仍不标 done。已有 `search_evidence`、`get_graph_neighborhood` 和 `get_page_snapshot`。没有 `lookup_method`、`compare_versions`、FTS5，也没有方法卡表。
- 依赖：T05 done；T06 的实际索引任务在 G2 联合验收时接通
- Owner：Grok retrieval
- 基线提交或快照：认领时填写
- 写入范围：证据/抽取/索引/方法库模块及 `tests/acceptance/test_retrieval_methods.py`；公共工具 schema 和迁移由集成 owner 管理
- 对应门禁：G2；消费者：T08

## 目标

从授权资料找到能够回到确切原件版本和位置的片段，说明支持、反例、冲突与未查到的范围。方法库提供有来源、适用条件和可执行步骤的方法卡。索引和摘要可以重建，不能取代原件与已确认事实。

## 输入与交付

建立 EvidenceAnchor 的版本、locator、摘录哈希、抽取器版本与权限继承。文本抽取与 OCR 修正形成派生修订，引用仍回原件。首版检索使用关键词/SQLite FTS5，选择经中文题集验证的分词或回退；不假设 DeepSeek 提供 embedding。任何向量扩展先登记价值和依赖，不能阻塞 P0。

实现 search_evidence、get_page_snapshot、get_graph_neighborhood、lookup_method、compare_versions 的受限读服务，工具网关在 T08 接入。范围先被授权裁剪，结果再校验锚点/版本，输出包括实际索引修订、已查/未索引范围、截断和无命中。来源更新或撤权后旧索引不可旁路读取；历史检索必须显式选择旧版本。

## 验收

创建并执行 `python -m pytest -q tests/acceptance/test_retrieval_methods.py`：

1. 一组中文问题能命中预先标注的小型资料；输出原件版本、页码/时码/区域可打开核对。
2. 关键词相同但属于其他项目、无权字段或已撤权资料的标题/片段/数量均不泄露。
3. 无命中与尚未索引分别返回真实状态，资料不足不会被编成答案。
4. 原件新版本、索引重建和旧版本查询保持来源准确，不把原引用改指“最新”。
5. 方法卡带来源、适用条件、输入、步骤、产物和限制；不适用问题不会无条件强推方法。
6. 图邻域与检索返回上限、截断和预算可测，越界参数被拒绝。

## 交接与证据

交出来源可授权的合成或已批准小夹具、中文查询金标准、工具输入/输出样例和索引重建命令。手工标注题集与真实精度/召回结果分开记录，不把几个成功例子称为全面检索效果。向 T08 交付真实只读服务及每次读取所需的 manifest 字段。

## 授权关键词检索证据（2026-10-31）

状态仍是 implementing。本轮只做 `search_evidence`：在已有 `evidence_anchors` 上，按当前 `evidence.read` 裁剪后匹配 `locator_json` 的 `excerpt`。空查询、超过 40 字、`limit` 不在 1..20 会拒绝。命中带回原件 `artifact_version_id` 和 locator。有锚点但摘录不同是 `no_hit`，没有锚点是 `not_indexed`。其他项目和没有 `evidence.read` 的锚点不出现在正文里，也不进入计数。不调用模型，不写业务行，不新建迁移。FTS5、方法卡、图邻域、页面快照和版本对比都没有做。GLB 和 3D 预览没有做。T07 不标 done。

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_retrieval_methods.py -o cache_dir=_施工文件/2026-10-31_grok-dev-t07/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-dev-t07/99_临时文件/pytest
```

退出码 0。2 passed。交接见 `workpool/handoffs/T07_SEARCH_SLICE.md`。

## 谱系图邻域证据（2026-10-31）

状态仍是 implementing。本轮增加 `get_graph_neighborhood`。它只读取 `page_edges`。库里的边种类只有 `branches_from`、`stage_source`、`merge_source`。`depth` 只接受 0 到 2，`max_nodes` 只接受 1 到 30。起点或其他项目无权时返回空，不带出版本 id。没有 `page.read` 的版本不进入节点和边，也不从它继续走。放不下的已授权节点令 `truncated` 为 true。返回值只有版本 id、边 id、种类和两个端点，没有页标题，也没有 `content_json`。不调用模型，不写业务行，不新建迁移。

`tests/acceptance/test_retrieval_methods.py` 现有 4 个测试。后两个是图邻域。全量在这条切片之后重跑过。

```text
.venv/Scripts/python.exe -m pytest -q -o cache_dir=_施工文件/2026-10-31_grok-audit-after-graph/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-audit-after-graph/99_临时文件/pytest
```

退出码 0。301 passed，2 warnings，55.22s。警告见 `workpool/handoffs/COMPLETION_LEDGER_2026-10-31.md`。T07 不标 done。

## 页面快照证据（2026-11-01）

状态仍是 implementing。本轮增加只读的 `get_page_snapshot`。它先对确切 `page_version_id` 做 `page.read`，通过之后才读取标题和 `content_json`。无权、撤权、其他项目或缺失版本返回空快照，标题和正文都不在返回值里，拒绝路径的 SQL 也不选择 `content_json`。有权时返回这一版的标题、版本号、哈希、正文，以及内容里的节点、边和作品版本引用，不用同一页的更新版本替换。不调用模型，不写业务行，不新建迁移。`lookup_method`、`compare_versions`、FTS5 和方法卡表都没有做。T07 不标 done。

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_retrieval_methods.py tests/acceptance/test_agent_runs.py -o cache_dir=_施工文件/2026-11-01_grok/99_临时文件/pytest-cache --basetemp=_施工文件/2026-11-01_grok/99_临时文件/pytest
```

退出码 0。10 passed，0.92s。其中检索文件 6 个，页面快照是后两个。交接见 `workpool/handoffs/T07_PAGE_SNAPSHOT_SLICE.md`。
