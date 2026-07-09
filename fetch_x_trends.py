#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
X 每日热帖推送
功能：抓取 X 全站热门趋势 TOP20，提取代表推文，DeepSeek 翻译+人话摘要，PushPlus 推送。
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser

# ===================== 配置 =====================
PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
X_BEARER_TOKEN = os.environ.get("X_BEARER_TOKEN", "")
AI_BASE_URL = os.environ.get("AI_BASE_URL", "https://api.deepseek.com")
AI_MODEL = os.environ.get("AI_MODEL", "deepseek-chat")

TREND_REGION = "united-states"  # trends24 区域
TOP_N = 20                      # 热门趋势数量
TWEETS_PER_TREND = 2            # 每个趋势抓几条代表推文
MAX_POST_TEXT_LEN = 600         # 单条推文最大长度

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(REPO_DIR, "history.json")
INDEX_FILE = os.path.join(REPO_DIR, "index.html")


# ===================== 通用 HTTP =====================
def http_get(url, headers=None, timeout=30, retries=2):
    """通用 GET 请求，带重试。"""
    headers = headers or {}
    if "User-Agent" not in headers:
        headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    req = urllib.request.Request(url, headers=headers)
    last_err = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="ignore")
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(1.5 ** attempt)
    raise last_err


def http_post(url, data, headers=None, timeout=60):
    """通用 POST 请求。"""
    headers = headers or {}
    if isinstance(data, dict):
        data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


# ===================== X API v2 路径 =====================
def x_api_request(endpoint, params=None):
    """调用 X API v2，返回 JSON 或 None（失败时）。"""
    if not X_BEARER_TOKEN:
        return None
    url = f"https://api.twitter.com/2{endpoint}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        text = http_get(url, headers={
            "Authorization": f"Bearer {X_BEARER_TOKEN}",
            "User-Agent": "XDailyDigest/1.0",
        }, timeout=20, retries=1)
        return json.loads(text)
    except Exception as e:
        print(f"[X API] {endpoint} failed: {e}")
        return None


def get_x_trends_via_api():
    """
    尝试用 X API 获取趋势。注意： trends 接口通常需要高级权限，
    Free Bearer Token 大概率返回 403，所以失败后会走网页 fallback。
    """
    # 美国 WOEID = 23424977, 全球 = 1
    data = x_api_request("/trends/by_place", {"id": "23424977"})
    if not data or "data" not in data:
        return None
    trends = []
    for t in data.get("data", []):
        name = t.get("name", "").strip()
        if not name or name.startswith("#"):
            continue
        trends.append({
            "name": name,
            "volume": t.get("tweet_volume") or 0,
        })
    return trends[:TOP_N]


def search_x_tweets_via_api(query, max_results=3):
    """用 X API v2 recent search 搜索推文。Free 档可用但有额度限制。"""
    data = x_api_request("/tweets/search/recent", {
        "query": f"{query} -is:retweet lang:en",
        "max_results": max_results,
        "tweet.fields": "created_at,public_metrics,author_id,lang",
    })
    if not data or "data" not in data:
        return []
    tweets = []
    users = {u["id"]: u for u in data.get("includes", {}).get("users", [])}
    for t in data["data"]:
        author = users.get(t.get("author_id", ""), {})
        tweets.append({
            "id": t["id"],
            "text": t.get("text", ""),
            "author": author.get("username", "unknown"),
            "created_at": t.get("created_at", ""),
            "likes": t.get("public_metrics", {}).get("like_count", 0),
            "url": f"https://x.com/{author.get('username', 'i')}/status/{t['id']}",
        })
    return tweets


# ===================== 网页 Fallback 路径 =====================
class SimpleHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.current_attrs = {}
        self.in_script = False

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        self.current_attrs = attrs_dict
        if tag in ("script", "style"):
            self.in_script = True
        if tag == "a" and attrs_dict.get("href"):
            self.links.append(attrs_dict["href"])

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.in_script = False


def get_trends24_trends():
    """从 trends24.in 抓取热门趋势（最新一小时）。"""
    url = f"https://trends24.in/{TREND_REGION}/"
    try:
        html = http_get(url, timeout=30)
    except Exception as e:
        print(f"[trends24] fetch failed: {e}")
        return []

    seen = set()
    trends = []

    # 页面里 <ol class=trend-card__list> 常不带引号，先取最新一个 ol 列表
    first_ol_match = re.search(
        r'<ol[^>]*class=["\']?trend-card__list["\']?[^>]*>(.*?)</ol>',
        html,
        re.S,
    )
    list_html = first_ol_match.group(1) if first_ol_match else html

    for href, text in re.findall(
        r'<a[^>]*href="https://twitter\.com/search\?q=([^"]+)"[^>]*class=["\']?trend-link["\']?[^>]*>(.*?)</a>',
        list_html,
        re.S,
    ):
        name = re.sub(r"<[^>]+>", "", text).strip()
        name = urllib.parse.unquote(name)
        if not name:
            name = urllib.parse.unquote(href).replace("+", " ").strip()
        clean = name.lstrip("#").strip()
        if clean and clean.lower() not in seen and len(clean) > 1:
            seen.add(clean.lower())
            trends.append({"name": clean, "volume": 0})

    # 后备：如果 ol 匹配失败，尝试从 meta description 提取
    if not trends:
        m = re.search(r'<meta name=description content="([^"]+)"', html)
        if m:
            desc = m.group(1)
            if ":" in desc:
                parts = desc.split(":", 1)[1]
                for raw in parts.split(","):
                    clean = raw.strip().lstrip("#")
                    if clean and clean.lower() not in seen and len(clean) > 1:
                        seen.add(clean.lower())
                        trends.append({"name": clean, "volume": 0})

    print(f"[trends24] parsed {len(trends)} trends")
    return trends[:TOP_N]


def duckduckgo_search_x_links(query, max_results=5):
    """用 DuckDuckGo HTML 搜索 X 帖子链接，提取跳转后的真实 x.com/twitter.com 链接。"""
    q = urllib.parse.quote(query)
    url = f"https://html.duckduckgo.com/html/?q={q}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://html.duckduckgo.com/",
    }
    try:
        html = http_get(url, headers=headers, timeout=20)
    except Exception as e:
        print(f"[DDG] search failed for '{query}': {e}")
        return []

    links = []
    seen = set()
    # 匹配跳转链接：//duckduckgo.com/l/?uddg=https%3A%2F%2Fx.com%2F...%2Fstatus%2F...&rut=...
    for href in re.findall(r'href="([^"]+)"', html):
        if "uddg=" not in href:
            continue
        if href.startswith("//"):
            href = "https:" + href
        parsed = urllib.parse.urlparse(href)
        qs = urllib.parse.parse_qs(parsed.query)
        real_url = qs.get("uddg", [None])[0]
        if not real_url:
            continue
        real_url = urllib.parse.unquote(real_url)
        # 只保留 X/Twitter 帖子链接
        if re.search(r"(?:x\.com|twitter\.com)/[^/]+/status/\d+", real_url):
            if real_url not in seen:
                seen.add(real_url)
                links.append(real_url)
        if len(links) >= max_results + 3:
            break
    return links[:max_results]


def parse_x_url(url):
    """把 x.com/twitter.com 链接解析成 username 和 status id。"""
    m = re.search(r"(?:x\.com|twitter\.com)/([^/]+)/status/(\d+)", url)
    if m:
        return m.group(1), m.group(2)
    return None, None


def fetch_fxtwitter(url):
    """用 fxtwitter/vxtwitter API 获取单条推文正文。"""
    username, status_id = parse_x_url(url)
    if not username or not status_id:
        return None

    for api_host in ["api.fxtwitter.com", "api.vxtwitter.com", "api.vx2twitter.com"]:
        api_url = f"https://{api_host}/{username}/status/{status_id}"
        try:
            text = http_get(api_url, timeout=15)
            data = json.loads(text)
            tweet = data.get("tweet", data)  # fxtwitter 在 tweet 字段，vxtwitter 可能直接是 tweet
            if not isinstance(tweet, dict):
                tweet = data
            return {
                "id": status_id,
                "text": tweet.get("text", ""),
                "author": username,
                "created_at": tweet.get("created_at", ""),
                "likes": tweet.get("likes", 0) or tweet.get("favorite_count", 0) or 0,
                "url": f"https://x.com/{username}/status/{status_id}",
            }
        except Exception as e:
            print(f"[{api_host}] failed for {url}: {e}")
            continue
    return None


def fetch_tweets_via_web(trend_name, max_results=2):
    """网页 fallback：搜索 + fxtwitter 获取推文。"""
    links = duckduckgo_search_x_links(f"{trend_name} twitter", max_results=max_results + 3)
    tweets = []
    for link in links:
        if len(tweets) >= max_results:
            break
        tweet = fetch_fxtwitter(link)
        if tweet and tweet.get("text") and len(tweet["text"]) > 10:
            tweets.append(tweet)
        time.sleep(0.3)
    return tweets


# ===================== DeepSeek AI =====================
def clean_tweet_text(text):
    """清理推文里的 t.co 短链、多余换行等。"""
    text = re.sub(r"https?://t\.co/\w+", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def deepseek_chat(messages, temperature=1.1, max_tokens=1200):
    """调用 DeepSeek Chat API。"""
    if not DEEPSEEK_API_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY not set")

    payload = {
        "model": AI_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    resp = http_post(f"{AI_BASE_URL}/chat/completions", payload, headers=headers, timeout=90)
    data = json.loads(resp)
    return data["choices"][0]["message"]["content"]


def summarize_topic(trend_name, tweets):
    """用 DeepSeek 翻译+人话摘要一个话题。"""
    cleaned = []
    for t in tweets:
        txt = clean_tweet_text(t.get("text", ""))
        if txt:
            cleaned.append(f"[@{t.get('author','?')}]: {txt}")

    if not cleaned:
        prompt = (
            f"话题是：{trend_name}\n"
            "请用中文介绍这个 X/Twitter 热门话题大概是什么，为什么会上趋势。"
            "用两句人话总结，语气像朋友圈科普。"
        )
    else:
        prompt = (
            f"X/Twitter 热门话题：{trend_name}\n"
            "下面这个趋势下的英文帖子（已清理短链接）：\n\n"
            + "\n\n".join(cleaned)
            + "\n\n任务：\n"
            "1. 如果帖子是英文，先翻译成自然流畅的中文；\n"
            "2. 用两句充满人话味的总结，说明大家到底在吵什么、看什么、笑什么；\n"
            "3. 给出一个情绪/氛围标签（如：玩梗、争议、正面、负面、中性、吃瓜）。\n"
            "返回严格 JSON：{\"translation\":\"...\", \"summary\":\"...\", \"sentiment\":\"...\"}"
        )

    messages = [
        {"role": "system", "content": "你是中文互联网热评博主，说话像朋友聊天，拒绝官话套话。只输出要求的 JSON。"},
        {"role": "user", "content": prompt},
    ]
    try:
        content = deepseek_chat(messages, temperature=1.2, max_tokens=1000)
        # 提取 JSON
        m = re.search(r"\{[\s\S]*?\}", content)
        if m:
            result = json.loads(m.group(0))
        else:
            result = {}
        return {
            "translation": result.get("translation", ""),
            "summary": result.get("summary", content[:200]),
            "sentiment": result.get("sentiment", "中性"),
        }
    except Exception as e:
        print(f"[DeepSeek] summarize failed for '{trend_name}': {e}")
        return {
            "translation": "",
            "summary": f"{trend_name} 是今天 X 上的热门话题，具体讨论内容待补充。",
            "sentiment": "中性",
        }


# ===================== PushPlus 推送 =====================
def pushplus_send(title, content):
    """推送 markdown 内容到 PushPlus。"""
    if not PUSHPLUS_TOKEN:
        print("[PushPlus] PUSHPLUS_TOKEN not set, skip push")
        return False

    payload = {
        "token": PUSHPLUS_TOKEN,
        "title": title,
        "content": content,
        "template": "markdown",
    }
    try:
        resp = http_post("https://www.pushplus.plus/send", payload, timeout=20)
        data = json.loads(resp)
        print("[PushPlus] response:", data)
        return data.get("code") == 200
    except Exception as e:
        print(f"[PushPlus] push failed: {e}")
        return False


def build_markdown_digest(items, run_at):
    """构建推送用的 markdown。"""
    lines = [
        f"# 🔥 X 每日热帖 TOP{len(items)} | {run_at}",
        "",
        "> 数据来源：X/Twitter 全站热门趋势，英文帖子由 DeepSeek 翻译成中文并提炼人话摘要。",
        "",
    ]
    for i, item in enumerate(items, 1):
        trend = item["trend"]
        summary = item["summary"]
        sentiment = item.get("sentiment", "中性")
        tweets = item.get("tweets", [])
        lines.append(f"## {i}. {trend}")
        lines.append(f"**氛围：{sentiment}**")
        if summary.get("translation"):
            lines.append(f"\n📝 翻译：{summary['translation']}")
        if summary.get("summary"):
            lines.append(f"\n💬 摘要：{summary['summary']}")
        if tweets:
            lines.append("\n🔗 代表帖：")
            for t in tweets[:1]:
                author = t.get("author", "?")
                lines.append(f"- [@{author}]({t.get('url','#')})")
        lines.append("\n---\n")
    return "\n".join(lines)


# ===================== 历史记录 & 展示页 =====================
def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[history] load failed: {e}")
    return []


def save_history(entry):
    history = load_history()
    history.insert(0, entry)
    # 只保留最近 30 天
    history = history[:60]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def render_index_html():
    """生成黑金风格展示页。"""
    history = load_history()
    history_json = json.dumps(history, ensure_ascii=False)

    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>X 每日热帖推送</title>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@300;400;500;700;900&family=Inter:wght@400;600;800&display=swap" rel="stylesheet">
<style>
:root {{
  --bg: #0a0a0b;
  --surface: #121214;
  --surface-2: #1a1a1d;
  --gold: #d4af37;
  --gold-light: #f0d878;
  --gold-dark: #8a6d1f;
  --text: #f5f5f7;
  --text-2: #a1a1a6;
  --border: rgba(212,175,55,0.15);
  --accent: #ff4d6d;
}}
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{
  background: var(--bg);
  color: var(--text);
  font-family: "Noto Sans SC", "Inter", system-ui, sans-serif;
  line-height: 1.6;
  min-height: 100vh;
}}
.noise {{
  position: fixed; inset:0; pointer-events:none; z-index:0;
  opacity: 0.035;
  background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");
}}
.hero {{
  position: relative;
  z-index: 1;
  padding: 120px 24px 80px;
  text-align: center;
  background:
    radial-gradient(ellipse 80% 50% at 50% 0%, rgba(212,175,55,0.12), transparent 60%),
    radial-gradient(ellipse 60% 40% at 80% 20%, rgba(255,77,109,0.06), transparent 50%);
}}
.hero h1 {{
  font-size: clamp(2.2rem, 6vw, 4.5rem);
  font-weight: 900;
  letter-spacing: -0.03em;
  background: linear-gradient(135deg, var(--gold-light) 0%, var(--gold) 50%, var(--gold-dark) 100%);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  margin-bottom: 16px;
}}
.hero p {{
  color: var(--text-2);
  font-size: 1.05rem;
  max-width: 560px;
  margin: 0 auto 28px;
}}
.badge {{
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 8px 18px;
  border-radius: 999px;
  background: rgba(212,175,55,0.08);
  border: 1px solid var(--border);
  color: var(--gold-light);
  font-size: 0.82rem;
  font-weight: 600;
}}
.badge::before {{
  content: "";
  width: 7px; height: 7px; border-radius: 50%;
  background: var(--gold);
  box-shadow: 0 0 10px var(--gold);
  animation: pulse 2s infinite;
}}
@keyframes pulse {{ 0%,100%{{opacity:1;}} 50%{{opacity:0.4;}} }}

.container {{
  position: relative;
  z-index: 1;
  max-width: 880px;
  margin: 0 auto;
  padding: 0 24px 120px;
}}
.date-group {{ margin-bottom: 56px; }}
.date-label {{
  display: flex; align-items: center; gap: 12px;
  margin-bottom: 24px;
  font-size: 0.92rem;
  color: var(--gold-light);
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.12em;
}}
.date-label::before {{
  content: ""; flex: 1; height: 1px;
  background: linear-gradient(90deg, transparent, var(--border), transparent);
}}
.date-label::after {{
  content: ""; flex: 1; height: 1px;
  background: linear-gradient(90deg, transparent, var(--border), transparent);
}}
.card {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 20px;
  padding: 24px;
  margin-bottom: 16px;
  transition: transform 0.3s ease, box-shadow 0.3s ease;
  position: relative;
  overflow: hidden;
}}
.card::before {{
  content: "";
  position: absolute; top:0; left:0; right:0; height: 1px;
  background: linear-gradient(90deg, transparent, rgba(212,175,55,0.4), transparent);
}}
.card:hover {{
  transform: translateY(-3px);
  box-shadow: 0 20px 50px rgba(0,0,0,0.35), 0 0 30px rgba(212,175,55,0.06);
}}
.card-header {{
  display: flex; justify-content: space-between; align-items: flex-start; gap: 16px;
  margin-bottom: 14px;
}}
.card-title {{
  font-size: 1.25rem; font-weight: 800; color: var(--text);
  letter-spacing: -0.01em;
}}
.card-index {{
  font-size: 0.78rem; color: var(--gold);
  font-weight: 700; border: 1px solid var(--border);
  padding: 3px 10px; border-radius: 999px;
}}
.sentiment {{
  display: inline-block;
  padding: 4px 12px;
  border-radius: 999px;
  font-size: 0.75rem;
  font-weight: 700;
  margin-bottom: 14px;
}}
.sentiment-玩梗 {{ background: rgba(212,175,55,0.12); color: var(--gold-light); }}
.sentiment-争议 {{ background: rgba(255,77,109,0.12); color: #ff8fa3; }}
.sentiment-正面 {{ background: rgba(46,213,115,0.12); color: #7ee787; }}
.sentiment-负面 {{ background: rgba(120,120,120,0.15); color: #d1d1d6; }}
.sentiment-中性 {{ background: rgba(100,149,237,0.12); color: #a6c8ff; }}
.sentiment-吃瓜 {{ background: rgba(255,165,0,0.12); color: #ffd166; }}
.translation, .summary {{
  color: var(--text-2);
  font-size: 0.95rem;
  margin-bottom: 10px;
}}
.translation strong, .summary strong {{ color: var(--text); font-weight: 600; }}
.tweet-link {{
  display: inline-flex; align-items: center; gap: 6px;
  color: var(--gold-light);
  text-decoration: none;
  font-size: 0.85rem;
  font-weight: 600;
  margin-top: 12px;
  transition: color 0.2s;
}}
.tweet-link:hover {{ color: var(--gold); }}
.tweet-link svg {{ width: 14px; height: 14px; fill: currentColor; }}
.empty {{
  text-align: center; padding: 120px 24px; color: var(--text-2);
}}
.footer {{
  text-align: center; padding: 40px 24px;
  color: var(--text-2); font-size: 0.82rem;
  border-top: 1px solid var(--border);
}}
@media (max-width: 640px) {{
  .hero {{ padding: 80px 20px 50px; }}
  .card {{ padding: 18px; }}
  .card-title {{ font-size: 1.05rem; }}
}}
</style>
</head>
<body>
<div class="noise"></div>
<header class="hero">
  <div class="badge">每日自动更新 · DeepSeek 翻译摘要</div>
  <h1>X 每日热帖</h1>
  <p>每天中午 12 点，自动抓取 X 全站热门趋势 TOP20，英文帖子翻译成中文，并用一句人话告诉你大家在吵什么。</p>
</header>
<main class="container" id="app"></main>
<footer class="footer">
  Built with GitHub Actions · Powered by DeepSeek · Pushed to PushPlus
</footer>
<script>
const history = {history_json};
const app = document.getElementById('app');

function render() {{
  if (!history || history.length === 0) {{
    app.innerHTML = '<div class="empty">暂无数据，等待今日中午 12:00 首次推送...</div>';
    return;
  }}
  app.innerHTML = history.map(day => `
    <section class="date-group">
      <div class="date-label">${{day.date}}</div>
      ${{day.items.map((item, idx) => `
        <article class="card">
          <div class="card-header">
            <h3 class="card-title">${{idx + 1}}. ${{escapeHtml(item.trend)}}</h3>
            <span class="card-index">#${{idx + 1}}</span>
          </div>
          ${{item.sentiment ? `<span class="sentiment sentiment-${{item.sentiment}}">${{item.sentiment}}</span>` : ''}}
          ${{item.summary && item.summary.translation ? `<div class="translation"><strong>翻译：</strong>${{escapeHtml(item.summary.translation)}}</div>` : ''}}
          ${{item.summary && item.summary.summary ? `<div class="summary"><strong>摘要：</strong>${{escapeHtml(item.summary.summary)}}</div>` : ''}}
          ${{item.tweets && item.tweets[0] ? `<a class="tweet-link" href="${{item.tweets[0].url || '#'}}" target="_blank" rel="noopener">
            <svg viewBox="0 0 24 24"><path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z"/></svg>
            查看原帖
          </a>` : ''}}
        </article>
      `).join('')}}
    </section>
  `).join('');
}}

function escapeHtml(text) {{
  if (!text) return '';
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}}

render();
</script>
</body>
</html>'''
    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        f.write(html)


# ===================== 主流程 =====================
def main():
    beijing_now = datetime.now(timezone(timedelta(hours=8)))
    run_date = beijing_now.strftime("%Y-%m-%d")
    run_time = beijing_now.strftime("%Y-%m-%d %H:%M")
    print(f"=== X 每日热帖推送 {run_time} (北京时间) ===")

    # 1. 获取趋势
    trends = get_x_trends_via_api()
    source = "X API v2"
    if not trends:
        print("[main] X API 不可用，切换到网页 fallback")
        trends = get_trends24_trends()
        source = "trends24.in"

    if not trends:
        print("[main] 无法获取趋势，退出")
        sys.exit(1)

    print(f"[main] 从 {source} 获取 {len(trends)} 条趋势")

    # 2. 获取代表推文并 AI 摘要
    items = []
    for i, trend in enumerate(trends, 1):
        name = trend["name"]
        print(f"\n[{i}/{len(trends)}] 处理趋势: {name}")

        # 先尝试 X API，失败走网页
        tweets = search_x_tweets_via_api(name, max_results=TWEETS_PER_TREND)
        tweet_source = "X API"
        if not tweets:
            tweets = fetch_tweets_via_web(name, max_results=TWEETS_PER_TREND)
            tweet_source = "web fallback"

        print(f"  获取 {len(tweets)} 条推文 ({tweet_source})")

        summary = summarize_topic(name, tweets)
        items.append({
            "trend": name,
            "tweets": tweets,
            "summary": summary,
            "sentiment": summary.get("sentiment", "中性"),
        })
        time.sleep(0.5)

    # 3. 推送
    md = build_markdown_digest(items, run_time)
    title = f"🔥 X 热帖 TOP{len(items)} | {run_date}"
    pushplus_send(title, md)

    # 4. 保存历史 & 生成页面
    entry = {
        "date": run_date,
        "time": run_time,
        "source": source,
        "items": [
            {
                "trend": it["trend"],
                "sentiment": it["sentiment"],
                "summary": it["summary"],
                "tweets": [{"url": t.get("url", "#")} for t in it["tweets"]],
            }
            for it in items
        ],
    }
    save_history(entry)
    render_index_html()
    print("\n[main] 完成。历史已保存，展示页已生成。")


if __name__ == "__main__":
    main()
