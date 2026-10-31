# T06 任务重试等待

版本：`T06-TASK-RETRY-1`。日期：2026-10-31。登记审计的失败路径已经补测。GLB 仍不做。

`run_tasks.status` 可以是 `retry_wait`。`jobs` 没有这个状态，不要加。从 `running` 进入 `retry_wait` 必须是一条条件更新，并清空租约。次数到顶、截止已过或不可重试，则进入 `failed`。已经 `failed` 的行不再 UPDATE，因为终态触发器会拒绝。

从 `retry_wait` 回到 `queued` 也是一条条件更新：未到 `available_at` 不能回去；两个连接同时放行，只有一次成功。不调用模型，不改迁移。
