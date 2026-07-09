# X 每日热帖推送

每天北京时间 12:00 自动抓取 X/Twitter 全站热门趋势 TOP20，提取代表推文，由 DeepSeek 翻译成中文并生成人话摘要，最后通过 PushPlus 推送到微信。

## 数据源

- 优先使用 X API v2 获取趋势和搜索推文
- 当 X API 受限时，自动 fallback 到 `trends24.in` + DuckDuckGo + fxtwitter

## AI 处理

- 英文帖子 → 中文翻译
- 人话摘要：告诉你大家到底在吵什么、看什么、笑什么
- 情绪标签：玩梗 / 争议 / 正面 / 负面 / 中性 / 吃瓜

## 定时任务

GitHub Actions 工作流程：[`.github/workflows/daily-push.yml`](.github/workflows/daily-push.yml)

- 定时：每天 UTC 04:00（北京时间 12:00）
- 也可以手动触发 `workflow_dispatch`

## Secrets

仓库中配置了以下 Secrets：

| Secret | 说明 |
|--------|------|
| `PUSHPLUS_TOKEN` | PushPlus 推送 Token |
| `DEEPSEEK_API_KEY` | DeepSeek API Key |
| `X_BEARER_TOKEN` | X/Twitter Bearer Token |

## 展示页面

- GitHub Pages：`https://heijiao211-star.github.io/x-meiriretie-tuisong/`
- 本地：直接打开 `index.html`

## 目录结构

```
.
├── fetch_x_trends.py          # 核心脚本
├── .github/workflows/daily-push.yml
├── index.html                 # 黑金风格展示页
├── history.json               # 历史数据
└── README.md
```

## 本地测试

```bash
export PUSHPLUS_TOKEN="your_token"
export DEEPSEEK_API_KEY="your_key"
export X_BEARER_TOKEN="your_bearer"
python fetch_x_trends.py
```
