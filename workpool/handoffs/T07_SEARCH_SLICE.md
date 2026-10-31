# T07 授权关键词检索

日期：2026-10-31。T07 状态保持 implementing。本切片不把任务标成 done。用户已决定不做 GLB 和其他 3D 预览，见 `workpool/handoffs/USER_WAIVE_3D.md`。本切片没有做这些。

## 对照

冻结合同 `workpool/handoffs/T07_SEARCH_CONTRACT.md`，版本 `T07-SEARCH-1`。

施工文档 `docs/construction/06_SECURITY_STORAGE_RECOVERY.md`：搜索按 `evidence.read` 重新检查源对象；隐藏对象的标题、片段和计数都不出现。同一节写明，短查询走已授权候选集里有界、转义后的子串检索，不把全库片段先交给模型再删。`docs/construction/01_技术栈与模块边界.md` 和 `02_基线裁决与勘误.md` 里的 FTS5 仍是后续检索，不是这一刀。

本仓库没有单独的文献库。上面这份合同和施工节就是本切片的依据。没有为这一刀新下载论文，也不把几个合成命中写成检索效果。

## 选型

新模块 `src/sinuo/application/retrieval/search.py`。复用现有 `SessionAuthorizer`、`EvidenceAnchor` 和 `evidence.read`，不新写一套权限判断，不新建迁移。

放弃 FTS5 和 trigram：合同把第一刀限制在已有锚点的 `excerpt`，查询最长 40 字，对应文档里的短查询路径。FTS 表要新迁移，本刀明确不做。也不调用模型，不把命中改写成“最新版本”。

## 行为

`search_evidence(session, authorizer, principal, project_id, query, limit)` 只读。

- 空查询、只含空白、超过 40 个字符，以及 `limit` 不是 1 到 20 的整数，都抛出 `ValueError`。布尔值不算合法上限。
- 调用方不是该项目成员时，直接返回空命中、空版本和三项都为 0 的计数，不读取对方项目的摘录。成员但没有 `project.read` 时，仍继续按锚点检查 `evidence.read`。
- 本项目每条锚点先调用 `evidence.read`。`NotFoundOrHidden`、`ActionForbidden`、`SourceRedacted` 的锚点整条丢掉，不进入匹配，也不进入任何计数。`AuthRequired` 继续抛出。
- 匹配只用 `locator_json` 里的 `excerpt` 字符串。SQL 用 `LIKE` 子串，`%`、`_` 和转义字符按字面量处理。摘录以外的字段即使含有相同的词也不算命中。
- 命中返回该锚点自己的 `artifact_version_id` 和解析后的 locator。同一原件的更新版本如果没有自己的命中锚点，不会替换这条版本号。
- 版本上有已授权锚点但没有一条摘录命中：`no_hit`。版本上没有任何锚点：`not_indexed`。只有未授权锚点的版本从结果和计数里去掉。
- 返回的命中不超过 `limit`。超出时 `truncated` 为真，版本状态仍按全部已授权命中计算。
- 函数不调用模型，不 `add`、不 `UPDATE`、不写业务行。

## 证据

先创建目录 `_施工文件/2026-10-31_grok-dev-t07/99_临时文件`，再从仓库根执行：

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_retrieval_methods.py -o cache_dir=_施工文件/2026-10-31_grok-dev-t07/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-dev-t07/99_临时文件/pytest
```

退出码 0。2 passed。

合成查询是「合成竹材弯曲」。有 `evidence.read` 的那一条返回对应原件版本和 locator。同一词出现在其他项目、没有 `evidence.read` 的锚点，或摘录以外的字段时，正文和计数里都没有。摘录不同的已授权锚点是 `no_hit`。没有锚点的版本是 `not_indexed`。查询 `%` 和 `_` 不会把全部摘录配上。

## 还没做

FTS5、中文分词、索引修订、旧版本历史检索、方法卡、`get_page_snapshot`、`get_graph_neighborhood`、`lookup_method`、`compare_versions`，以及撤权后的异步清理。真实模型不跑。GLB 和其他 3D 预览、转换、夹具都不做。T07 不标 done。
