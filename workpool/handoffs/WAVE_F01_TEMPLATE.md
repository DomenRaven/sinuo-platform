# WAVE F01 固定产品设计模板

- 上游：`WAVE_2026-11-01_CONTRACT.md` 的 F01 行，以及 `USER_SCOPE_2026-11-01.md` 的 F01。不标 done，不签 G0–G4。没有 Git commit。
- 做了：`create_project` 仍是创建入口。每次创建写入固定八个阶段，顺序是合同上的名字。返回的 `stage_id` 和 `page_id` 仍是第一阶段「任务与情境」及其首页。其他七个阶段没有页。`template_key` 只照原样保存，不拿它选择另一套阶段。没有模板增删接口。
- 名字放在已有的 `stage_config_versions.goal_json`：`name` 是阶段名，`goal` 仍是项目标题。阶段表没有名称列，本代理不能加迁移。`position` 从 0 到 7。
- 白皮书第 3.1 节正文写「方案深化与原型」「成果表达与复盘」。同节表格的阶段列是「深化与原型」「表达与复盘」。合同和用户范围用表格这八个，创建时写入的是表格这八个。
- 既有测试只改了一处：`test_repeated_parallel_key_creates_one_page_without_next_edge` 里阶段计数从 1 改为 8。同页再建、无 `next` 边、页标题和修订号的断言没放宽。没改 `test_end_to_end.py`。

## 挂载

不需要主控往 `app.py` 加路由。已有 `POST /api/v1/projects` 调用 `create_project`。

## 验证

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_product_stage_template.py tests/acceptance/test_versions_projects.py
```

退出码 0。12 passed，1.23s。其中新测试 2 条，改过的旧文件 10 条。

```text
.venv/Scripts/python.exe -m ruff check src/sinuo/application/versions/projects.py tests/acceptance/test_product_stage_template.py tests/acceptance/test_versions_projects.py
```

退出码 0。

未跑全库。未跑 `test_end_to_end.py`。未跑 `live_model`。

## 没完成

- 用户范围里的「阶段可以重排、跳过或返回」没有新命令。这一行只要求创建时写入这八个名字。
- 全库 pytest 和 `test_end_to_end.py` 没在本代理重跑。
