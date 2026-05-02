# MemeRadar Collector v7 — 低延迟叙事雷达

双源交叉验证信号采集器，实时扫描 GMGN + OKX 聪明钱/KOL 交易，通过 Hermes Agent 快速分析叙事后推送 Telegram。

## v7 核心升级

| 优化项 | v6 | v7 | 提升 |
|--------|-----|-----|------|
| 聚类窗口 | 120s | **30s** | -90s 等待 |
| 叙事分析 | web 搜索（新闻/推特） | **GMGN 页面**（社交链接） | -30-150s |
| Hermes 调用 | 串行 | **并行** ThreadPoolExecutor | 2 token 同时处理 |
| Hermes timeout | 300s | **60s** | 快速失败 |
| Hermes retry | 2次重试 | **去掉** | 不浪费时间 |
| 端到端延迟 | 150-310s | **~60s** | **4-5x** |

## 架构

```
GMGN (sm+kol trades)  ──┐
                         ├──► 聚类30s + 打分 ──► Hermes 并行分析 ──► ★★★ → TG 推送
OKX (smart_money trades) ─┘                     (GMGN页面社交链接)
```

## 推送策略

| 级别 | 条件 | 推送 |
|------|------|------|
| ★★★ | 有叙事/热度/KOL 提及 | ✅ 推 @memeranderbot |
| SKIP | 无叙事/死币/跑路 | ❌ 跳过 |

## 安装与配置

```bash
# 1. 安装依赖
pip install requests python-dotenv

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env 填入你的 TG bot token 和 chat_id

# 3. 运行
python3 collector.py
```

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `TG_PUSH_TOKEN` | Telegram Bot Token | - |
| `TG_PUSH_CHAT` | 推送目标 Chat ID | - |
| `OLD_TG_TOKEN` | 旧 Bot Token（双推） | - |
| `HERMES_API` | Hermes API 地址 | `http://127.0.0.1:8642/v1/chat/completions` |
| `MAX_BUFFER_PER_TOKEN` | 每个代币最大缓存交易数 | `50` |

## 数据源

- **GMGN CLI**：聪明钱 + KOL 买入交易
- **OKX OnchainOS**：smart_money tracker 交叉验证
- **Hermes Agent**：打开 GMGN 页面查社交链接，快速判★★★/SKIP

## 文件结构

```
~/crypto-trading/
├── collector.py           # 主程序
├── collector_launcher.sh  # 启动脚本
├── .env.example           # 环境变量模板
├── .env                   # 环境变量（不提交 git）
├── .gitignore
├── collector.log          # 运行日志
└── README.md
```

## License

MIT
