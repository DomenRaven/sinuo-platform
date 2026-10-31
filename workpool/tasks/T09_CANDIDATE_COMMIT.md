# T09 候选预览、人工确认与原子提交

- 状态：planned
- 依赖：T04、T08 done
- Owner：候选与领域事务开发
- 基线提交或快照：认领时填写
- 写入范围：候选预览/处置/确认应用服务与 API、`tests/acceptance/test_candidate_commit.py`；共享候选 schema、迁移和错误码由集成 owner 合入
- 对应门禁：G3；消费者：T10

## 目标

用户能看见提案将改哪些对象、编辑其允许修改部分并明确确认；服务端提交的内容必须与该用户看到的预览一致。同一候选最多消费一次，任何并发、来源变化或权限变化都不会写出半张页或重复决定。

## 输入与交付

预览保存 actor、候选修订、当前 access_epoch、规范化 user_patch、effective_proposal、全部来源预条件、到期时间与 preview_hash。提交引用服务端预览；任何正文改变都重新预览。提交时检查当前身份/动作权限、候选状态、预览绑定、全部来源版本/草稿/原件有效性，然后候选状态 CAS 与目标效果同事务提交。

将候选消费、Page/PageVersion/来源边/Decision/Audit/Outbox/幂等回执放进一个短事务；普通节点候选只写草稿与处置，不伪造正式版。stage_blank 与 merge_blank 创建主体空白页，仅承接显式引用；history_branch 保留旧版可编辑起点、来源映射和历史测试限制。未确认草稿上的阶段建议要求先保存正式版并重新检查。

## 验收

创建并执行 `python -m pytest -q tests/acceptance/test_candidate_commit.py`，用真实并发连接验证：

1. 同一候选使用不同请求键、不同操作者同时确认，只有一个成功，另一个返回 CANDIDATE_ALREADY_RESOLVED。
2. 相同键重放返回同一结果；相同键改正文拒绝；回执按当前权限重新投影。
3. 预览后篡改 user_patch/actor/来源/哈希，或使用过期预览均拒绝；先前预览不因重新生成自动替换。
4. 按动作校验来源：stage_blank/merge_blank 的声明当前 HEAD 或所依赖草稿内容改变时拒绝；history_branch 校验确切旧版存在、可读、未墓碑，不因源页 HEAD 继续更新而失效。所有动作均重查证据有效性与当前权限；失败时确认对象零新增。
5. 在候选已消费后目标写入中途注入异常，候选与业务对象一起回滚。
6. 多来源阶段页主体空白并保留全部准确来源；拒绝候选仅保存处置，不改业务图。

## 交接与证据

提供完整 preview/commit/reject 示例和竞争时间线、数据库结果、失败包络。T10 与未来 UI 可基于预览做展示，但 HTTP 业务提交响应才是成功依据；SSE candidate.ready 不代表已写业务对象。
