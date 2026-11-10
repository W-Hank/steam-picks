#!/usr/bin/env python3
"""
每小时跑一次：找「临时工」——疑似标错价的异常低价。

判定：折扣 ≥80%、当前价 ≤ 史低 × 0.7、评价 ≥500 条
没有史低缓存的游戏直接跳过（不猜，避免误报）。

命中就写 data/glitch.json；若配置了 FEISHU_WEBHOOK，推一条飞书消息。
"""
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9", "Accept-Encoding": "gzip, deflate"}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
CN_TZ = timezone(timedelta(hours=8))
PAGES = int(os.environ.get("PAGES", "3"))


def get(url, timeout=30, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                return body
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(str(last)[:80])


def strip_tags(s):
    s = re.sub(r"<[^>]*>", " ", s)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"')):
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s).strip()


def parse_rows(html):
    rows = []
    for block in html.split('<a href="https://store.steampowered.com/app/')[1:]:
        m = re.match(r"(\d+)", block)
        nm = re.search(r'class="title">([^<]+)<', block)
        if not m or not nm:
            continue
        dm = re.search(r'discount_pct">-(\d+)%', block)
        fm = re.search(r'discount_final_price"[^>]*>([^<]+)<', block)
        price = None
        if fm:
            pm = re.search(r"([\d.,]+)", fm.group(1).replace(" ", ""))
            if pm:
                try:
                    price = float(pm.group(1).replace(",", ""))
                except ValueError:
                    price = None
        reviews = None
        tm = re.search(r'search_review_summary[^>]*data-tooltip-html="([^"]*)"', block)
        if tm:
            tip = strip_tags(tm.group(1).replace("&lt;br&gt;", " "))
            cm = re.search(r"([\d,]+)\s*篇用户评测", tip)
            if cm:
                reviews = int(cm.group(1).replace(",", ""))
        rows.append({
            "appid": m.group(1),
            "name": strip_tags(nm.group(1)),
            "discount": int(dm.group(1)) if dm else 0,
            "price": price,
            "reviews": reviews,
            "url": "https://store.steampowered.com/app/%s/" % m.group(1),
        })
    return rows


def load_history():
    p = os.path.join(DATA, "history.json")
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8") as f:
        h = json.load(f)
    h.pop("_updated", None)
    return h


def post_feishu(text):
    hook = os.environ.get("FEISHU_WEBHOOK", "").strip()
    if not hook:
        print("  （未配置 FEISHU_WEBHOOK，跳过推送）")
        return False
    body = json.dumps({"msg_type": "text", "content": {"text": text}}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(hook, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            print("  飞书推送:", r.status, r.read()[:80].decode("utf-8", "replace"))
        return True
    except Exception as e:  # noqa: BLE001
        print("  飞书推送失败:", str(e)[:80])
        return False


def main():
    hist = load_history()
    print("史低缓存 %d 条" % len(hist))

    seen = {}
    for i in range(PAGES):
        url = ("https://store.steampowered.com/search/results/?query&start=%d&count=50"
               "&sort_by=Price_ASC&specials=1&infinite=1&cc=cn&l=schinese" % (i * 50))
        try:
            html = json.loads(get(url).decode("utf-8", "replace")).get("results_html", "")
        except Exception as e:  # noqa: BLE001
            print("  第 %d 页失败：%s" % (i + 1, str(e)[:60]))
            continue
        for r in parse_rows(html):
            seen.setdefault(r["appid"], r)
        time.sleep(0.8)
    print("本轮特价 %d 个" % len(seen))

    alerts = []
    for r in seen.values():
        h = hist.get(r["appid"])
        if not h or r["price"] is None:
            continue
        low = h.get("low") or 0
        if low <= 0:
            continue
        if (r["discount"] or 0) >= 80 and r["price"] <= low * 0.7 and (r["reviews"] or 0) >= 500:
            alerts.append({
                "appid": r["appid"],
                "name": r["name"],
                "price": r["price"],
                "historyLow": low,
                "discount": r["discount"],
                "reviews": r["reviews"],
                "url": r["url"],
                "ratio": round(r["price"] / low, 3),
            })

    now = datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M")
    out = {"checkedAt": now, "scanned": len(seen), "alerts": alerts}
    os.makedirs(DATA, exist_ok=True)
    with open(os.path.join(DATA, "glitch.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print("疑似标错价 %d 个" % len(alerts))
    for a in alerts:
        print("  ¥%s（史低 ¥%s，%.0f%%）%s" % (a["price"], a["historyLow"], a["ratio"] * 100, a["name"][:24]))

    if alerts:
        lines = ["🍔 疑似标错价 / 异常低价 %s" % now]
        for a in alerts[:8]:
            lines.append("· %s ¥%s（史低 ¥%s，-%d%%）\n  %s" % (
                a["name"][:30], a["price"], a["historyLow"], a["discount"], a["url"]))
        lines.append("抢之前先确认：能买、能激活、能退——标错价常被撤销。")
        post_feishu("\n".join(lines))


if __name__ == "__main__":
    main()
