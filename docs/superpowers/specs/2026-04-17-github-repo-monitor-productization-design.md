# GitHub Repo Monitor — 产品化设计

- 日期：2026-04-17
- 作者：will + Claude
- 状态：已批准，进入实施计划

## 1. 背景与目标

当前仓库是一个 ~580 行的单文件 Python demo（`src/github_repo_monitor.py`），能跑通"GitHub Search + Trending 采集 → 规则粗筛 → 启发式/OpenAI README 打分 → Telegram 推送"的最小链路，但在稳定性、推荐质量、交互、历史追踪方面都只是示意。

本设计把它升级为**稳健的个人工具**（单用户 / 小圈子），部署在自有 VPS 上，长期自动运行。核心目标：

- **A. 推荐质量**：更丰富的信号、讲究的 LLM prompt、结构化输出
- **B. 运行稳定性**：限流感知、重试、单 repo 失败隔离、心跳告警
- **D. TG 交互**：按钮反馈 + 基础命令，反馈回流到推荐
- **E. 历史复推**：冷却期机制、二次 surge 检测、周报

显式不做：多用户 / 登录 / Web UI / SaaS / 多数据源（HN/Reddit 等）/ Prometheus 级观测。

## 2. 范围

**In scope**
- 单进程 async 守护，systemd 管理
- GitHub 单数据源，单 token
- Anthropic SDK 调 MiniMax 的兼容端点（模型名配置化）
- TG Bot 交互（反馈按钮 + `/top` `/status` `/pause` `/resume` `/reload` `/digest_now`）
- SQLite + WAL，schema migration
- 四类定时任务：`digest_morning` / `digest_evening` / `surge_poll` / `weekly_digest`
- 结构化日志 + healthcheck 独立告警

**Out of scope**
- Web 前端 / 用户系统 / 计费
- 多数据源（HN / Reddit / ProductHunt / Twitter）
- Prometheus / Grafana / OpenTelemetry
- 多 LLM provider 抽象（只对接 Anthropic-兼容端点）
- 自动调节规则权重的 ML 学习（反馈只通过偏好画像注入 LLM + 黑名单生效）

## 3. 架构与进程模型

**单进程 async 守护**，不是多进程 / 不是纯 cron。内部三条并发：

1. **TG Bot 长轮询**（`python-telegram-bot` v21 async）— 命令 + 按钮回调
2. **Scheduler**（`APScheduler` AsyncIO）— 四任务：
   - `digest_morning` 08:00
   - `digest_evening` 20:00
   - `surge_poll` 每 30 分钟
   - `weekly_digest` 周日 21:00
3. **Pipeline 执行器** — 被 scheduler 或 `/digest_now` 触发，`asyncio.Lock` 保证不重入

### 技术栈

- Python 3.11+
- `httpx` (HTTP) / `aiosqlite` (DB) / `anthropic` (LLM)
- `python-telegram-bot` v21 / `APScheduler` 3.x
- `pydantic` (config + schema) / `structlog` / `tenacity`
- 部署：`systemd` + `logrotate`

### 模块结构

```
src/monitor/
  config.py          # pydantic settings, env + json 三层
  db.py              # schema + 迁移 + DAO
  clients/
    github.py        # GitHub API + 限流 + 重试
    llm.py           # Anthropic SDK 封装（指向 MiniMax）
  pipeline/
    collect.py       # search + trending
    filter.py        # 规则 + 黑名单 + cooldown
    enrich.py        # 指标补全，单字段容错
    score.py         # 规则分 + LLM 分 + 融合
    surge.py         # 突发检测
  scoring/
    rules.py
    preference.py    # 偏好画像生成
  bot/
    app.py
    commands.py      # /top /status /pause /resume /reload /digest_now
    feedback.py      # inline button 回调
    render.py        # 消息格式化
  scheduler.py       # 四任务
  notify.py          # TG 推送 + console fallback
  main.py            # 装配 + 生命周期
tests/
  unit/
  integration/
  live/
  fixtures/
```

## 4. 数据流与管道

### Digest 管道

```
 采集              筛选               补全                  打分
[Search]──┐    ┌─────────┐       ┌─────────┐        ┌─────────────┐
[Trending]├───▶│ 黑名单  │──────▶│ events  │        │ 规则分      │
[SurgeHit]┘    │ 规则    │       │ issues  │───────▶│ LLM 分      │
               │ cooldown│       │ contrib │        │ (+偏好画像) │
               └─────────┘       │ readme  │        │ 融合        │
                                 └─────────┘        └─────────────┘
                                                          │
                                          rank → Top N → persist → TG
                                                          │
                                                  user 反馈（按钮）
```

### 关键变化（相对 demo）

**1. 两层过滤**
- 硬过滤：规则（stars/language/age）+ 黑名单（repo/author/topic）
- 软过滤：`is_seen` 改为 cooldown — 未推送/推送 > 14 天 → 进管道；推送 < 14 天 → 扔

**2. Enrich 单字段容错**
- 每子接口独立 try；失败写 `run_log.stats.errors[]`，不阻断 repo
- 失败字段 fallback 到上次采集值

**3. 打分三路融合**
- `final = rule*α + llm*β`，默认 α=0.55 / β=0.45，可配置

**4. 推送即反馈入口**
- 每条消息自带 4 个 inline button：👍 / 👎 / 🚫作者 / 🔕topic
- callback_data 带 `push_id`，按下直接写 `user_feedback` + 编辑原消息显示"已记录"

### Surge 轮询

精简管道，**只跑已入库、未推或 cooldown 到期的 repo**：

```
候选：pushed_at IS NULL OR pushed_at < now()-3d
    │
    ▼
只拉 events（1 call/repo），重算 star_velocity_day
    │
    ▼
相对上次 metrics 最新一行 × 3 倍 且绝对 > 20
    │
    ▼
enrich + score + push，带 🔥 标记
    │
    ▼
surge cooldown 3 天（短于 digest 的 14 天）
```

Surge **不跑 Search/Trending** —— 它只是已知候选池的二次发现。

## 5. LLM 集成

### Client

```python
from anthropic import AsyncAnthropic
client = AsyncAnthropic(
    api_key=config.minimax_api_key,
    base_url=config.minimax_base_url,
)
```

模型名从 config 读，不硬编码。

### 结构化输出：强制 tool use

定义 `submit_repo_score` tool，严格 schema：

```python
SCORE_TOOL = {
    "name": "submit_repo_score",
    "input_schema": {
        "type": "object",
        "required": ["score", "readme_completeness", "summary",
                     "reason", "matched_interests", "red_flags"],
        "properties": {
            "score":                {"type": "number", "minimum": 1, "maximum": 10},
            "readme_completeness":  {"type": "number", "minimum": 0, "maximum": 1},
            "summary":              {"type": "string", "maxLength": 140},
            "reason":               {"type": "string", "maxLength": 240},
            "matched_interests":    {"type": "array", "items": {"type": "string"}},
            "red_flags":            {"type": "array", "items": {"type": "string"}},
        },
    },
}
```

调用时 `tool_choice={"type":"tool","name":"submit_repo_score"}`，解析直接取 `resp.content[0].input` + pydantic 校验。

**兼容性 fallback**：若 MiniMax 的 Anthropic 端点不支持 forced tool use，降级为"JSON schema in prompt + pydantic 解析"路径，代码里预留分支。实现阶段写个一次性脚本真实调一下确定。

### Prompt 结构 + caching

System（所有 repo 复用，开 ephemeral cache）：
- `[1]` 角色 + 评分 rubric（固定）
- `[2]` 用户偏好画像（每 5 条新反馈刷新）

User（每 repo 不同）：
- full_name / description / language / stars / forks / age
- 采集指标（velocity / contributor / issue response）
- README 前 12K 字符（保留 demo 行为）
- interest_tags（config.keywords）

一次 digest ~50 repo，[1][2] 命中 cache，节省 80%+ 输入 token。

### 偏好画像

- 每新反馈 → `feedback_counter++`；到阈值（默认 5）触发重新生成
- 取最近 20 条 👍 和 20 条 👎 的元信息（name/desc/topics/language/当时 summary）拼 prompt
- LLM 输出 ~300 字描述，覆盖写入 `preference_profile`（单行）
- 冷启动画像字段空，prompt 只放 rubric

### 容错

- `tenacity` 3 次指数退避
- 彻底失败 → fallback 到启发式打分器（保留 demo 的 `_heuristic_analysis`）
- 失败数 / 延迟 / token 用量打到 structlog，`/status` 能看到

## 6. 存储模型（SQLite）

### 表

**`repositories`** — repo 主表
```
full_name PK, html_url, description, language, topics JSON,
owner_login, created_at, first_seen_at, last_enriched_at
```

**`repository_metrics`** — 时间序列
```
full_name, collected_at (compound PK),
stars, forks, star_velocity_day, star_velocity_week,
fork_star_ratio, avg_issue_response_hours,
contributor_count, contributor_growth_week, readme_completeness
```

**`pushed_items`** — 推送事件（取代 demo 的 `seen_repositories`）
```
id PK, full_name, pushed_at, push_type (digest|surge),
rule_score, llm_score, final_score,
summary, reason,
tg_chat_id, tg_message_id
```

**`user_feedback`**
```
id PK, push_id FK, action (like|dislike|block_author|block_topic),
created_at, repo_snapshot JSON
```

**`blacklist`**
```
id PK, kind (repo|author|topic), value,
added_at, source (manual|feedback), source_ref
UNIQUE (kind, value)
```

**`preference_profile`** — 单行
```
id=1 PK, profile_text, generated_at, based_on_feedback_count
```

**`llm_score_cache`**
```
full_name, readme_sha256 (compound PK),
score, readme_completeness, summary, reason,
matched_interests JSON, red_flags JSON, cached_at
```

**`run_log`**
```
id PK, kind, started_at, ended_at, status (ok|partial|failed),
stats JSON (repos_scanned, repos_pushed, llm_calls,
             input_tokens, output_tokens, cache_read_tokens, errors[])
```

**`schema_version`**
```
version INTEGER PRIMARY KEY
```

### 索引
- `pushed_items(full_name, pushed_at DESC)`
- `repository_metrics(full_name, collected_at DESC)`
- `blacklist(kind, value)` UNIQUE
- `user_feedback(created_at DESC)`

### 迁移

- 代码常量 `SCHEMA_VERSION = N`
- 启动时对比 `schema_version` 表，差多少跑多少 migration
- `_migrations: list[str]` 列表存每版 DDL
- Migration 001：`seen_repositories` → `pushed_items` 数据迁移；`repository_metrics` 补列；建其他新表

### 连接与维护

- `PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;`
- 整个 app 单 `aiosqlite` 连接（pool size=1）
- 每日 `PRAGMA wal_checkpoint(TRUNCATE)`
- 每周日 04:00 `VACUUM` + 清理老数据：`repository_metrics` > 180 天、`run_log` > 90 天
- 每日 02:00 `sqlite3 .backup` 到 `/var/backups/monitor/`，保留 14 天

## 7. 可靠性 & 运维

### GitHub 限流

- 读响应 header 的 `X-RateLimit-Remaining` / `Reset`，remaining < 50 自动 sleep 到 reset
- `429` / 二级限流：读 `Retry-After` 精确等待
- `search/repositories` 额外限流（30/min）：独立计数器 + 至少 2s 间隔
- 网络错误：`tenacity` 指数退避 `wait_exponential(1, 2, 30)`，最多 4 次

### 日志（structlog，JSON）

- 输出 stdout + `/var/log/monitor/app.log`
- 每条自带 `event` / `run_id` / `repo`（如适用）/ `latency_ms`
- 关键事件：`collect.start` / `enrich.failed` / `llm.call` / `push.sent` / `feedback.received` / `surge.triggered`
- 敏感字段（token / chat_id）过滤器脱敏
- `logrotate` 周级轮转，保留 8 周

### 心跳告警（独立脚本）

- `monitor-healthcheck.py` 由独立 systemd timer 每小时触发
- 查 `run_log` 最近 25 小时内有无 `status='ok'` 的 digest；无 → TG API 直接告警
- 独立于主进程，主进程 crash/hang 也能报警
- LLM 连续失败 5 次（跨 run）也告警

### Systemd

```
/etc/systemd/system/monitor.service         # 主守护
/etc/systemd/system/monitor-health.service  # healthcheck
/etc/systemd/system/monitor-health.timer    # 每小时
```

主 service 关键：
- `Restart=on-failure`, `RestartSec=30s`
- `StartLimitBurst=5`, `StartLimitIntervalSec=600`（连续崩 5 次停等人看）
- `EnvironmentFile=/etc/monitor/monitor.env`（密钥）
- `User=monitor`（非 root）
- `WorkingDirectory=/opt/monitor`, `ReadWritePaths=/var/lib/monitor /var/log/monitor`

### 配置（三层）

1. 代码默认值（pydantic defaults）
2. `/etc/monitor/config.json`（关键词 / 阈值 / 模型名 / 权重 / cooldown / surge 阈值）
3. 环境变量（只放密钥）

`/reload` 热重载 `config.json`，密钥改动要重启。

### 非重入 & 优雅停机

- Digest `asyncio.Lock`；`/digest_now` 撞上运行中 → 回"已有运行中"
- `SIGTERM`：停 scheduler → 等当前 digest 到可中断检查点（每 repo 处理完检查一次）→ 关 TG bot → 关 DB
- 30s 硬超时强杀

## 8. 测试策略 + 周报

### 测试层次（目标 ~60 个测试）

**单元**（`tests/unit/`，离线，CI 必过）
- RuleEngine / Scorer / PreferenceBuilder / SurgeDetector
- DB DAO + migration 001 幂等
- GithubClient（`respx` mock httpx，限流/重试/二级限流）
- LLMClient（mock Anthropic response，tool use 成功 / schema 失败 / 网络异常降级）
- TelegramRender

**集成**（`tests/integration/`，可选 live，默认 mock）
- 端到端 digest：mock GitHub + LLM，真 SQLite（tmp）
- Surge 触发路径
- 反馈按钮 → `user_feedback` + `blacklist`
- `/reload` 后下一轮采集用新关键词

**live smoke**（`tests/live/`，手动，不进 CI）
- 真 GITHUB_TOKEN 最小 search
- 真 MiniMax key 真打分

**Fixtures**
- 5-10 个真实 repo 的 GitHub API 响应落盘 JSON，mock 全用它们

### 每周 digest（scheduler 第四任务）

周日 21:00，**不做新采集**，仅从数据库聚合：

```
📊 本周摘要 (W16)

🔥 新推送 23，你 👍 5 / 👎 3
📈 本周 star 增速 top 3:
  1. ...

🎯 兴趣画像（基于 31 条反馈）
  偏好: AI agent, Rust 系统工具, LLM inference
  不偏好: 教程/awesome-list, README 低完成度

📋 运行统计
  digest 14/14, surge 7 次
  LLM 调用 412, 输入 1.2M tokens (cache hit 78%)
```

SQL 聚合 + format，不走 LLM。

## 9. 迁移 milestones

1. **M1 脚手架**：模块拆分 / 依赖引入 / config + db + migration 001 / demo 测试在新结构下绿
2. **M2 可靠 GitHub 客户端**：`clients/github.py` + 限流重试 + fixture + `collect.py` + `enrich.py` 容错
3. **M3 LLM 打分 + 偏好画像**：`clients/llm.py` + `scoring/` + `llm_score_cache`；CLI 能单次跑完整管道
4. **M4 TG Bot + 反馈闭环**：`bot/` + 按钮反馈 + 命令
5. **M5 调度 + Surge + 周报**：四定时任务 + cooldown
6. **M6 systemd 打包 + healthcheck + 运维**：service 文件 / 备份 / 日志轮转 / 部署文档

每个 milestone 结束时独立可跑。

## 10. 开放问题（实施阶段解决）

- MiniMax 的 Anthropic 兼容端点是否支持 forced tool use + prompt cache —— M3 开始时跑一次性验证脚本确认
- MiniMax 具体模型名 / base_url —— 用户在 M1 阶段填入 config
- 权重 α / β 默认 0.55 / 0.45 是否合适 —— M3 验收时用历史推送复盘调整
- Surge 阈值 "3x 且绝对 > 20" 是否合适 —— M5 上线后观察 1-2 周调优
