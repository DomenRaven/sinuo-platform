# T04 草稿、确认版本与幂等领域命令

- 状态：done
- 依赖：T02、T03 done
- Owner：Grok 主控
- 基线提交或快照：无 Git commit。冻结环境 pytest 113 passed 之后开始。迁移 head `0004_repair_invariants`
- 写入范围：`src/sinuo/application/versions/`、`tests/acceptance/test_versions_commands.py`。入口、迁移和公共合同仍由集成 owner 在需要时改
- 对应门禁：G1；消费者：T05、T06、T09

## 目标

用户能建立项目与平行页、编辑草稿、保存确认版本、查看历史和从旧版创建新路线。两位用户并发修改或重复发送请求时，已确认内容与原路线仍完整，每个成功命令只有一份业务效果。

## 输入与交付

实现创建项目、阶段配置、平行页、草稿 patch、个人视图、checkpoint、历史查询和 history_branch 命令。草稿采用数据库条件 UPDATE 检查 expected base/revision；保存为版本同时检查 HEAD 与草稿哈希。history_branch 克隆可编辑内容并分配新 ID、记录 origin 映射，原件保持准确版本引用，旧测试只作历史依据。

关键写入使用统一幂等执行器；同键同体重放，同键异体 409。授权、业务效果、审计、Outbox 和成功回执处于同一短事务。重放也按当前权限投影，失败回滚不留成功回执。个人布局变化不使内容候选失效。谱系按创建因果保持 DAG；同章页不自动连成先后关系。

## 验收

创建并执行 `python -m pytest -q tests/acceptance/test_versions_commands.py`，用真实独立 SQLite 连接/进程验证：

1. 两请求基于同一草稿修订更新同一内容，仅一方成功，另一方 409；远端与失败方输入可复核。
2. 相同请求键并发/重复 20 次只创建一个页或版本；相同键改正文报 409。
3. 在目标写入后、审计/Outbox/回执前注入异常，整个事务回滚。
4. 保存版本后编辑草稿，历史版哈希与内容不变；多次 GET 不改 HEAD 或 ProjectRevision。
5. 从 V2 分支时原 V3 与后续路线保留，新页有独立 ID 与 origin 映射，旧测试不被重新确认。
6. 当前权限变化后重放命令不泄露受限原响应；不允许通过客户端传 actor 提权。

## 交接与证据

交出命令接口、类型化 patch 格式、错误样例、事务集合及请求键示例。为 T05/T06 冻结业务 Outbox 入口；为 T09 提供可被候选确认复用的“建页/草稿改动”领域函数，函数不自行提交事务，事务由上层统一协调。
