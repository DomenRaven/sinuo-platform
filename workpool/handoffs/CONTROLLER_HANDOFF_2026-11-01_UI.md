# 界面总控交接（2026-11-01 晚）

下一任总控先读本文件。你负责把已经能用的后端做成一个人能点的界面。不要从 `CONTROLLER_HANDOFF_2026-11-01_CLOSE.md` 重做 T02，也不要重做今晚已经测过的上传、证据、启动运行和导出。那份旧交接里的「先改纵向测试」「先导出理论库」已经做过。

本文件只写仓库里核对过的现状。聊天记忆不能覆盖仓库里的符号。

## 两条线

T 线是施工卡，F 线是白皮书功能。它们不是两个项目。

- T01–T04：done。地基、登录权限、数据库、草稿和版本。
- T05、T06、T08、T10、T11：implementing。T07、T09：review。都不要改成 done。G0–G4 不签。
- 任务板开头那句「406 passed、真实模型未执行、OpenAPI 30 条」是今晚施工前的快照。之后又加了八阶段、页内节点、回链、真实模型和公开方法卡。全量没有再跑过一次。不要把 406 或 30 条路径当成现在的数字。

## 用户刚刚定的界面语言

这是最新决定，压过 UI 白皮书第 7.1 节的「安静研究台、少量深蓝、少用大卡片」。

- 参考苹果：简洁、大气、明亮、舒服。
- 组件圆润，可以用大卡片。
- 层次清楚，路径清楚。人要看得出自己在哪一章、哪一页、建议从哪来、确认之后会走到哪。
- 行为仍以页面规范为准。候选还没确认时，画面不能变成「项目已经改了」。没被派到的职责不要画成已经出过场。状态不能只靠颜色。

信息架构和页面清单仍用这两份，不要去 GitHub 另下一套界面技能：

- `docs/reference/思诺智能体辅助设计平台UI白皮书_v1.3.md`：任务、工作台、结果卡。第 7.1 节的具体色值让路给上面的苹果式语言；第 7.2、7.3 节的卡片内容和状态文案继续用。
- `docs/reference/思诺智能体辅助设计平台UI页面实施规范_v1.1.md`：R01–R14 是首版页面。智能体工作主要在 R05 单页工作区、R10 运行与候选、R11 记忆。R15 两方案比较是后做的。

范围原文：`workpool/handoffs/USER_SCOPE_2026-11-01.md`。

## 不要做

- 不要做 GLB 或其他 3D。缩略图也不是必须项，文件名要能看出版本。见 `USER_WAIVE_3D.md` 和用户范围文件。
- 不要改 `run_fake_chain`、`read_fake_chain`、两条 fake-chain 路由、`scripts/seed_night_demo.py`、`workpool/demo-5min/`，除非用户明确要求。两屏兜底是汇报备用，不是要做的产品界面。
- 不要 git commit 或 git push，除非用户明确要求。不要改 git config。不要把 `.env` 加进提交。不要在交接、日志、页面或测试输出里打印密钥。
- 不要把 `F:/工业设计理论库` 的课件，或 Norman、Ulrich 与 Eppinger、Delft、ISO、IDEO Design Kit、Hyper Island 的正文复制进 git。
- 不要为了让界面看起来有数据而编造运行记录或候选。接口没有，就显示还没有。
- 同一时间一个文件只给一个写入者。`src/sinuo/api/app.py` 和迁移由主控收口。

## 仓库

- 根目录：`D:\AAA学习\大三上\设计大数据基础\大作业\sinuo-platform`
- 远程：`https://github.com/DomenRaven/sinuo-platform.git`，分支 `main`，私有库。本轮施工没有提交。
- 解释器：`.venv/Scripts/python.exe`。前缀 `/api/v1`。
- 迁移 head：`0010_evidence_index`。不要改旧迁移。新迁移只能接在这一头后面，并且保持单头。
- 启动：在仓库根复制 `.env.example` 为 `.env` 后，`.\.venv\Scripts\python.exe -m uvicorn sinuo.api.app:create_app --factory --host 127.0.0.1 --port 8000`。本机 `.env` 已有模型开关和密钥，也有 `SINUO_THEORY_ROOT=./var/theory`。测试构造 `Settings` 时继续用 `_env_file=None`。
- 纵向：`tests/acceptance/test_end_to_end.py` 今晚重跑退出码 0，1 passed。它注入假模型。它会登录、建项目、平行页、上传两版、证据、测试记录、问「下一步」、历史、分支、审阅、导出并 `drain_export_jobs`、再恢复。它还不去预览或确认阶段空白页。
- 真实模型：`tests/acceptance/test_live_deepseek.py` 今晚单独跑过，退出码 0。默认全量排除 `live_model`。澄清和证据用 `SINUO_MODEL_NAME`（示例 `deepseek-flash`）。方法和过程批评用 `SINUO_MODEL_NAME_REASONING`（示例 `deepseek-v4-pro`）。模型不提交业务。

## 界面现在有什么

只有两份 HTML，都不是规范里的产品：

- `web/workbench.html`：今晚写的说明页。八个阶段和七类职责是写死的。项目阶段、节点、建议、运行记录、候选都写着「这一块还没有接口」。它只尝试读 `http://127.0.0.1:8000/openapi.json`。
- `workpool/demo-5min/两屏兜底.html`：汇报兜底。不要把它当成界面基线。

产品页面还没做。可用性测试现在不能开始，差的就是这一层。

## 后端里界面可以直接用的

创建项目会写入八个阶段，顺序固定：任务与情境、用户与场景研究、问题及机会定义、设计要求、概念探索、深化与原型、测试与迭代、表达与复盘。响应里的 `stage_id` 和 `page_id` 是第一阶段及其首页。代码在 `src/sinuo/application/versions/projects.py` 的 `PRODUCT_STAGE_NAMES`。

页内节点写在草稿的 `content_json` 里，键只有 `problem`、`evidence`、`insight`、`requirement`、`artifact`、`test`、`decision`。显示名是问题、证据、洞察、要求、作品、测试、决定。职责：问题归澄清，证据和洞察归证据，要求归过程批评，作品、测试、决定归人。旧草稿只有 `node_id` 时仍能读。见 `src/sinuo/domain/page_nodes.py`。

已经挂上、界面会碰到的路径：

- 会话：`POST/GET/DELETE /api/v1/session`
- 项目和页：`POST /api/v1/projects`，`POST /api/v1/projects/{id}/pages`，草稿、检查点、历史、分支
- 上传三条、原件下载
- 证据锚点、测试记录
- `GET /api/v1/projects/{id}/trace`：从要求回到证据位置，测试钉在作品版本上
- `POST /api/v1/projects/{id}/agent-runs`：一次运行。问资料只查证据，问方法只问方法，问下一步是证据加过程批评，简报变化只做澄清。`app.state.agent_provider` 优先；没设置且 `model_enabled` 为真才走 DeepSeek
- 运行事件：`GET /api/v1/agent-runs/{id}/events`
- 候选：预览、确认、驳回。确认前按 `evidence.read` 重查锚点
- 审阅意见、导出登记、`GET /api/v1/exports/{id}/content`
- 建议频率：`GET/PATCH /api/v1/projects/{id}/suggestion-preferences/me`

导出生成没有 HTTP。后端用 `drain_export_jobs`。界面不要假装点一下导出就已经生成完，除非你们同时做了排空。

假模型链的读取端点还在，不要重做那条链。完整四角色要两次运行，第二次用 `parent_run_id`。启动接口一次只开一条运行。`run_model_chain` 在 `src/sinuo/application/agents/model_chain.py`，测试注入假模型。

## 界面还缺的读取

工作台标明没有接口的，现在仍然没有给页面用的列表接口：某个项目的八个阶段、某一页草稿里的节点、一次运行的职责步骤和缺口、待确认候选列表。事件流在，但今晚的页面判断它不够填运行记录。做 R05 和 R10 时，缺的读取由界面总控补一条只读接口，或让页面走已经存在的单条接口。不要编造。

智能体工作可视化要做进第一版，不是以后再加。一次运行要看见：派了哪几个职责、各自读了什么、缺口在哪、提出了哪张待确认卡、人有没有确认。四个职责的界面名称用白皮书的叫法：课题澄清、证据查找、方法建议、过程检查。

## 理论库

`var/theory` 被 git 忽略。里面有课程概念页、方法页，以及 18F Methods（CC0，37 张）和 Open Design Kit（CC BY 4.0，17 张）。合并后大约 98 个片段、100 个页、54 条链接。原始下载在 `var/theory-open/`。运行时读 `SINUO_THEORY_ROOT`。向量函数不读 F 盘。当前环境的编码器名是 `char-ngram-2-3`，不是 BGE。

## 下一窗口的顺序

1. 做一条能点的路径，不要先铺满 R01–R14。顺序：登录，看到八个阶段，打开第一页，上传或看见已有作品，提问「下一步」，看见证据和过程批评分别做了什么，看到待确认卡，确认或拒绝，再看到页有没有变。
2. 外观按本节「界面语言」。行为按页面规范：确认成功之后才改变位置；拒绝只记处置。
3. 日常这条路径走真实 DeepSeek。假模型只留在控制逻辑测试里。密钥不进页面、不进仓库。
4. 纵向测试仍可继续用假模型，避免验收时打到付费接口。不要为了界面把 `test_end_to_end.py` 改成必发 `live_model`。
5. 周一 2026-11-02 18:00 若这条路径还点不到待确认候选，停后端，改用 `workpool/demo-5min/` 两屏兜底。种库脚本是 `scripts/seed_night_demo.py`。

## 今晚各路留下的交接

| 文件 | 内容 |
| --- | --- |
| `WAVE_2026-11-01_CONTRACT.md` | 今晚的文件边界 |
| `WAVE_F01_TEMPLATE.md` | 八阶段。测试 12 passed |
| `WAVE_F03_NODES.md` | 七类节点。测试 5 passed |
| `WAVE_PATH_COMMIT.md` | 分叉、合流、确认前不变。没有改生产代码 |
| `WAVE_F08_TRACE.md` | 回链。主控已把路由挂进 `create_app`。测试 5 passed |
| `WAVE_AGENTS_LIVE.md` | 运行记录、出处、方法卡、过时结论。假模型 52 passed。真实调用成功 |
| `WAVE_THEORY_OPEN.md` | 公开方法卡数量 |
| `WAVE_VISUAL.md` | 工作台只有说明，没有项目数据 |

## 本窗口已做：登录到第一页

2026-11-01 晚。只做了路径的前半，没有做上传、提问和确认。

- 只读接口：`GET /api/v1/projects`，`GET /api/v1/projects/{project_id}/stages`，`GET /api/v1/pages/{page_id}/draft`。读不写。看不见的页不返回标题，只标 `has_hidden_pages`。没有草稿时不把已确认版本里的节点当成草稿。
- 页面：`web/desk.html`。应用把它挂在 `/`、`/projects`、`/projects/{project_id}`、`/projects/{project_id}/pages/{page_id}`。打开 `http://127.0.0.1:8000/`。`web/workbench.html` 仍是说明页。
- 测试：`.\.venv\Scripts\python.exe -m pytest tests/acceptance/test_workspace_outline.py -q`，2 passed。
- 临时库上用浏览器走过：登录，建立项目，看到八个阶段，打开第一页。第一页是进行中草稿，节点是空的。上传、提问「下一步」、确认或拒绝还没有接到这一页。

## 本窗口已做：上传、下一步、确认

同一条路径接到第一页上。没有改假模型链，没有改候选的写入规则，没有把 T05–T11 标成 done。

- 只读：`GET /api/v1/projects/{project_id}/artifacts`，`GET /api/v1/projects/{project_id}/candidates`，`GET /api/v1/agent-runs/{run_id}`。看不见的作品不返回标题。一次运行只列出实际派到的职责、读到的记录、缺口和候选。
- 服务启动时，若还没有设置，把 `var/uploads` 和 `var/objects` 交给已有的上传路由。
- 页面在第一页上可以上传原件、看到「文件名 · 第 N 版」、提问「下一步」、预览后确认、或拒绝。确认成功并且响应里有另一张页时才离开当前页。预览本身不改页。
- 模型若只留下一份还不能解析的草稿决定，卡片写明不能预览，页面不因此改变。模型若留下可解析的阶段建议，预览后确认才会出现新页。
- 测试：`.\.venv\Scripts\python.exe -m pytest tests/acceptance/test_workspace_flow.py tests/acceptance/test_workspace_outline.py -q`，4 passed。
- 浏览器在关掉模型的临时库上走过：上传表单在，提问后看到证据查找没有完成、过程检查已取消，并写明这次没有派到课题澄清和方法建议。草稿修订没有变，也没有待确认卡。临时库已删。


