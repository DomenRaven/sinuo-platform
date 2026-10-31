# T06 偏好 HTTP

日期：2026-10-31。建议频率复审 blocking 0 之后挂上入口。T06 仍是 implementing。

`create_app` 增加：

- `GET /api/v1/projects/{project_id}/suggestion-preferences/me`，动作 `project.read`。没有偏好行时返回 manual 默认值，不插入行。
- `PATCH` 同一路径，动作 `run.create`，带 CSRF、Origin 和 `Idempotency-Key`。本人偏好 CAS。冲突码是 `PREFERENCE_REVISION_CONFLICT`，见 ADR-005。成功响应不带 `project_revision`。

## 命令

工作目录 `sinuo-platform`。

```text
.venv/Scripts/python.exe -m ruff check src/sinuo/api/app.py src/sinuo/api/suggestions src/sinuo/api/auth/errors.py src/sinuo/domain/public_contract.py src/sinuo/workers/suggestions.py tests/acceptance/test_suggestion_http.py
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_suggestion_http.py -o cache_dir=_施工文件/2026-10-31_grok-integrate/99_临时文件/pytest-cache-pref --basetemp=_施工文件/2026-10-31_grok-integrate/99_临时文件/pytest-pref
```

ruff 退出码 0。pytest 退出码 0，1 passed。

自动运行取消、真实模型和真实预览仍未做。
