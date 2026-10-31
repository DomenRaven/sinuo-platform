# T05 上传票据过期

版本：`T05-TICKET-EXPIRY-1`。日期：2026-10-31。任务重试复审 blocking 0 之后才做。

新 revision `0008_ticket_expiry`，`down_revision` 是 `0007_run_origin`。只给 `upload_tickets` 增加可空的 `expires_at`。不改 `0001` 到 `0007` 的 SQL。产品表数量仍是 43。现有插入不写这一列时，值为空，表示不过期。

登记时如果 `expires_at` 有值且不晚于本次 `now`，不产生作品版本，不写审计，原因是 `ticket_expired`。还没过期的票据照常登记。

不改预览，不调用模型，不安装 Blender。
