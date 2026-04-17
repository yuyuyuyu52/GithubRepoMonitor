# GithubRepoMonitor

GitHub 开源项目监控工具（最小可运行版），用于从海量项目中筛选高潜力项目并推送推荐结果。

## 能力覆盖

> 注：以下能力目前由 `monitor.legacy`（单文件 demo）承担，将在 M2-M5 逐步迁移到 `src/monitor/` 包结构。详见 `docs/superpowers/specs/2026-04-17-github-repo-monitor-productization-design.md`。

- **数据采集层**
  - GitHub Search API（关键词 + 语言 + stars）
  - GitHub Events API（WatchEvent 用于 star 增速）
  - GitHub Trending 抓取（补充候选）
- **智能筛选层**
  - 规则引擎粗筛：stars、语言、项目年龄
  - 历史库去重与 14 天 cooldown 复推：SQLite `pushed_items`
  - 统一黑名单：repo / 作者 / topic 三维度（来自用户反馈或手工）
  - README 精筛：
    - 启发式评分（完整度 + 兴趣标签匹配）作为 fallback
    - LLM 评分（Anthropic SDK 指向 MiniMax 兼容端点，结构化输出 + prompt caching）
- **推送通知层**
  - Telegram Bot 推送（带 👍/👎/🚫/🔕 按钮）+ 控制台 fallback
  - `/top` `/status` `/pause` `/resume` `/reload` 交互命令

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
