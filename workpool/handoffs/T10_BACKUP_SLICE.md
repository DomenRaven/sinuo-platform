# HANDOFF-T10 备份与恢复

- 上游任务 / owner：T10 第三段，只要备份与恢复。T10 是 implementing，不标 done。T05 仍是 implementing，T09 仍是 review，所以 T10 不能标 done。T05、T06、T08 仍是 implementing。T07、T09 仍是 review，不标 done。
- 没做：DOCX、普遍团队评论、交接、3D、`live_model`、Git commit。没改假模型链、`scripts/seed_night_demo.py`、`workpool/demo-5min/`。没改审阅意见和快照导出的写入规则。没有占用 `job_kind='export'`。没有新错误码。
- 没有新迁移。没有改更早的 SQL。`alembic heads` 退出码 0，只有 `0010_evidence_index (head)`。

## 做法

可调用函数在 `sinuo.application.recovery`：`backup_snapshot`、`restore_snapshot`。本地命令：

```text
.venv/Scripts/python.exe -m sinuo.application.recovery backup --database <库> --objects <原件根> --dest <备份目录>
.venv/Scripts/python.exe -m sinuo.application.recovery restore --backup <备份目录> --dest <空的隔离目录> --live-database <当前库>
```

备份用 SQLite Online Backup API 读正在使用的库，把 WAL 折进一份单独的 `database.sqlite`。不直接复制还带着 WAL 的 `.db`。清单记下 schema revision、库哈希、ProjectRevision、可达原件的键/哈希/大小、备份时间、格式版本 1、墓碑纪元。对象库键是 `objects/<project>/sha256/<hash>/<generation>/original` 的才打包。身份夹具里的 `synthetic/...` 占位键不是对象库路径，只记在 `non_store_object_keys`，不假装有原件字节。

恢复只写调用方给出的新目录，不写回源库。先对上格式版本和 `0010_evidence_index`，再核库哈希、完整性、外键和清单里每个已打包原件。会话全部标成撤销。运行中的 job、run task 去掉旧 owner，状态回到已有的 `queued`；租约中的 outbox 回到 `pending`。没有新任务状态。

当前库只读。读到的墓碑和撤权补到隔离副本上。已删除的原件不放进隔离目录的 `objects/`。下载会碰到墓碑，不会把旧备份里的字节再交出去。删清单读不到时，副本保持未开放，报告写明不能当成全自动恢复成功。

搜索索引在恢复后清空再重建，报告写「已重建」，不写「已恢复」。预览文件不进备份，报告写 `pending_rebuild`，`restored` 为 false。

失败原因沿用这三个词，不是新的 `ErrorCode`：`corrupt`（备份已损坏）、`missing_original`（原件缺失）、`incompatible_version`（版本不兼容）。这三种都不返回成功。

## 这一次的耗时

测量的是 `test_isolated_restore_matches_history_branch_decision_hash_and_permissions` 里那一次函数调用，不是整个 pytest 的 2.38 秒。库是验收夹具种出来的 SQLite，快照文件 704512 字节，源库 `journal_mode` 是 `wal`。

- 备份：0.05348549998598173 秒
- 恢复：0.044866500014904886 秒
- 环境：Python 3.14.3，Windows-11-10.0.26200-SP0，SQLite 3.50.4

这次备份返回之后、恢复开始之前没有再提交普通写入，所以这次测量里看不到新的业务丢失。快照本来就不含备份完成之后才提交的普通写入。恢复时如果读到了更新的墓碑和撤权，会补到隔离副本上。上面两个数只说明这一次、这一台机器、这一份小库，不能外推成生产 RPO、RTO 或恢复时间承诺。

## 验证

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_review_export_recovery.py -o cache_dir=_施工文件/2026-11-01_t10backup/99_临时文件/pytest-cache --basetemp=_施工文件/2026-11-01_t10backup/99_临时文件/pytest
```

退出码 0。8 passed，1 warning。警告是既有的 Starlette/httpx 弃用。原先三个导出测试还在，没有改它们的断言。未跑全库，未跑 `live_model`。

新增测试名：

- `test_isolated_restore_matches_history_branch_decision_hash_and_permissions`
- `test_corrupt_backup_fails`
- `test_missing_original_fails`
- `test_incompatible_version_fails`
- `test_old_backup_does_not_revive_tombstone_or_revoked_grant`

## 还没做

T10 仍不标 done。T05 和 T09 还没 done。普遍团队评论和交接仍是 P1。预览没有在恢复时生成，报告里写明尚未重建。
