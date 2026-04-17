# GithubRepoMonitor

GitHub 开源项目监控工具（最小可运行版），用于从海量项目中筛选高潜力项目并推送推荐结果。

## 能力覆盖

- **数据采集层**
  - GitHub Search API（关键词 + 语言 + stars）
  - GitHub Events API（WatchEvent 用于 star 增速）
  - GitHub Trending 抓取（补充候选）
- **智能筛选层**
  - 规则引擎粗筛：stars、语言、项目年龄
  - 历史库去重：SQLite `seen_repositories`
  - README 精筛：
    - 默认启发式评分（完整度 + 兴趣标签匹配）
    - 可选 OpenAI 评分（开启 `OPENAI_API_KEY`）
- **推送通知层**
  - Telegram Bot 推送（未配置时回退为控制台输出）

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

## 每日自动化（建议）

可使用 cron 或 GitHub Actions 每天 08:00 / 20:00 触发：

1. 采集候选项目（Search + Trending）
2. 去重（历史库）
3. 规则粗筛
4. README 打分 + 摘要
5. 排序取 Top N
6. 推送 Telegram

## 测试

```bash
source .venv/bin/activate
pytest                                   # 跑全部
pytest tests/unit -v                     # 只跑单元
pytest tests/unit/test_db.py -v          # 单文件
pytest tests/unit/test_db.py::test_fresh_db_runs_all_migrations -v  # 单测试
```
