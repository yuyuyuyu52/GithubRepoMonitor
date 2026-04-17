# GithubRepoMonitor

GitHub 开源项目监控工具（最小可运行版），用于从海量项目中筛选高潜力项目并推送推荐结果。

## 能力覆盖

产品化分 6 个 milestone（M1–M6）。下面按交付状态分层列出，避免"设计存在 ≠ 运行态已接入"造成的误解。详见 `docs/superpowers/specs/2026-04-17-github-repo-monitor-productization-design.md`。

### ✅ 已交付

- **`monitor.legacy`（过渡期单文件 demo）**：GitHub Search + Events + Trending 采集；规则粗筛（stars / 语言 / 年龄）；`seen_repositories` 一次性去重；启发式或 OpenAI 的 README 打分；Telegram 直推（无按钮、无命令）。将在 M4 完全被 `src/monitor/` 替代后删除。
- **`src/monitor/`（M1 脚手架）**：常驻守护进程入口 + pydantic 配置三层 + SQLite schema v1 + 结构化日志 + SIGTERM 优雅退出。DB schema 已建全套表（`pushed_items` / `blacklist` / `user_feedback` / `preference_profile` / `llm_score_cache` / `run_log`），但**尚未被运行态逻辑接入**。

### 🚧 规划中

- **M2（当前进行）**：`httpx` async GitHub 客户端（限流 + 重试 + 失败隔离）替换 legacy 的 urllib；`pipeline/collect` + `pipeline/enrich` 替代 legacy 的同步打分前置。
- **M3**：LLM 打分迁到 Anthropic SDK 指向 MiniMax 兼容端点（结构化输出 tool use + prompt caching）；偏好画像注入 prompt；启发式仍作 fallback。
- **M4**：Telegram Bot（带 👍 / 👎 / 🚫 作者 / 🔕 topic inline 按钮）+ `/top` `/status` `/pause` `/resume` `/reload` 交互命令；反馈回流到 `blacklist` + `preference_profile`。
- **M5**：APScheduler 四任务（上午/晚间 digest + 30 分钟 surge 轮询 + 周报）；M1 已建的 `pushed_items` 14 天 cooldown + 复推在此接入。
- **M6**：systemd service + healthcheck + 日志轮转 + 备份。

## 指标采集

每个候选项目会补充并入库以下指标：

- Star 增速（日/周）
- Fork / Star 比
- 最近活跃度（基于 `pushed_at`）
- Issue 响应速度（closed issue 平均耗时）
- Contributor 数量与新增贡献者近似增长
- README 完整度

## 运行方式

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# 常驻守护进程（当前仅装载配置 + 跑 DB 迁移 + 等 SIGTERM；M2+ 会加载采集/打分/推送）
python -m monitor

# 旧版 demo 仍可直接跑（会被后续 milestone 逐步替换）
python -m monitor.legacy
```

可选配置文件：

```bash
MONITOR_CONFIG=/absolute/path/config.json python -m monitor
```

支持环境变量（部分）：

- `MONITOR_DB_PATH=/absolute/path/monitor.db`
- `MONITOR_LOG_PATH=/absolute/path/monitor.log`
- `MONITOR_CONFIG=/absolute/path/config.json`
- `GITHUB_TOKEN=...`
- `MINIMAX_API_KEY=...`
- `TELEGRAM_BOT_TOKEN=...` + `TELEGRAM_CHAT_ID=...`（未配置时会 fallback 到控制台输出）

## 自动化调度

守护进程常驻，内部用 APScheduler 挂 4 个任务：

1. `digest_morning` 08:00 完整采集 + 推送
2. `digest_evening` 20:00 完整采集 + 推送
3. `surge_poll` 每 30 分钟扫描已入库 repo 的热度突发，命中即推
4. `weekly_digest` 周日 21:00 从 DB 聚合输出周报（不采集）

生产部署走 systemd，见 M6（`docs/superpowers/specs/2026-04-17-github-repo-monitor-productization-design.md` 第 7 节）。

## 测试

```bash
source .venv/bin/activate
pytest                                   # 跑全部
pytest tests/unit -v                     # 只跑单元
pytest tests/unit/test_db.py -v          # 单文件
pytest tests/unit/test_db.py::test_fresh_db_runs_all_migrations -v  # 单测试
```
