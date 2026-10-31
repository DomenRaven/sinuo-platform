# T07 页面快照

日期：2026-11-01。T07 状态保持 implementing。本切片不把任务标成 done。不新建迁移，不调用模型，不做方法卡，不做 FTS5。

## 行为

`get_page_snapshot(session, authorizer, principal, project_id, page_version_id)` 只读。

- 空 id、超长 id 和非字符串抛 `ValueError`。未知账号抛 `AuthRequired`。
- 先按 `page.read` 检查这个确切页版本。`ActionForbidden`、`NotFoundOrHidden`、`SourceRedacted` 返回空快照。空快照的标题、正文、版本号和节点都是空。拒绝路径不执行选择 `content_json` 的语句。
- 通过之后才连接 `pages.title` 和该版本的 `content_json`。同一页的更新版本不会替换被请求的版本。
- 内容里的 `body` 字符串原样返回。`nodes` / `edges` 只收下有 id 的对象，并带出 `artifact_version_id`。
- 函数不 `INSERT`、不 `UPDATE`、不 `DELETE`。

## 证据

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_retrieval_methods.py tests/acceptance/test_agent_runs.py -o cache_dir=_施工文件/2026-11-01_grok/99_临时文件/pytest-cache --basetemp=_施工文件/2026-11-01_grok/99_临时文件/pytest
```

退出码 0。10 passed，0.92s。页面快照用例是 `test_page_snapshot_returns_the_authorized_version_only` 和 `test_page_snapshot_rejects_bad_ids_and_unknown_account`。

有权的第一版正文是「合成第一版正文」，第二版是「合成第二版正文」，互不替换。无权页标题「合成无权标题」和正文「合成无权正文」不出现。撤权页和其他项目同样不出现。

## 还没做

`lookup_method`、`compare_versions`、FTS5、方法卡表、草稿快照。T07 不标 done。
