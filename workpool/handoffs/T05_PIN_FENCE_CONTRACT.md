# T05 复用 pin 的 fencing token

版本：`T05-PIN-FENCE-1`。日期：2026-10-31。预览 GET 复审 blocking 0 之后才做。

同项目复用 ready 对象时，`_consume_upload_pin` 的条件更新必须再核对 `object_pins.fencing_token`。这个值是放置 pin 时从该 ready 对象读到的 token。对不上就抛出，让调用方事务回滚，不留下新的作品版本，也不把 pin 标成已消费。

新字节仍走 `install_reserved_original`，不改那条路径的语义。不改迁移，不改预览路由。
