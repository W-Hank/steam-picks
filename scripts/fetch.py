#!/usr/bin/env python3
"""
Steam 折扣精选：抓特价 → 过滤 → 补简介/史低 → 生成 data/games.json

只用标准库。本地与 GitHub Actions 都能跑。
env:
  ITAD_KEY   可选，IsThereAnyDeal 的 key；没有就跳过史低
  PAGES      抓多少页搜索结果（每页 50），默认 6
  LIMIT      最终榜单条数，默认 30
"""
import gzip
import json
import math
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
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGES = int(os.environ.get("PAGES", "6"))
LIMIT = int(os.environ.get("LIMIT", "30"))
CN_TZ = timezone(timedelta(hours=8))


def get(url, timeout=30, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                if r.status != 200:
                    raise RuntimeError("HTTP %s" % r.status)
                return body
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.2 * (i + 1))
    raise RuntimeError("%s -> %s" % (url[:70], last))


def get_json(url, **kw):
    return json.loads(get(url, **kw).decode("utf-8", "replace"))


def strip_tags(s):
    s = re.sub(r"<[^>]*>", " ", s)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"')):
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s).strip()


def parse_price(s):
    if not s:
        return None
    m = re.search(r"([\d.,]+)", s.replace(" ", ""))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_search_page(html):
    rows = []
    for block in html.split('<a href="https://store.steampowered.com/app/')[1:]:
        m = re.match(r"(\d+)", block)
        if not m:
            continue
        appid = m.group(1)
        nm = re.search(r'class="title">([^<]+)<', block)
        if not nm:
            continue
        name = strip_tags(nm.group(1))

        dm = re.search(r'discount_pct">-(\d+)%', block)
        discount = int(dm.group(1)) if dm else 0
        fm = re.search(r'discount_final_price"[^>]*>([^<]+)<', block)
        om = re.search(r'discount_original_price"[^>]*>([^<]+)<', block)

        reviews = pct = None
        tm = re.search(r'search_review_summary[^>]*data-tooltip-html="([^"]*)"', block)
        if tm:
            tip = strip_tags(tm.group(1).replace("&lt;br&gt;", " "))
            cm = re.search(r"([\d,]+)\s*篇用户评测", tip)
            pm = re.search(r"(\d+)%", tip)
            if cm:
                reviews = int(cm.group(1).replace(",", ""))
            if pm:
                pct = int(pm.group(1))

        im = re.search(r'<img src="([^"]+)"', block)
        rm = re.search(r"search_released[^>]*>([^<]*)<", block)

        rows.append({
            "appid": appid,
            "name": name,
            "discount": discount,
            "price": parse_price(fm.group(1) if fm else ""),
            "original": parse_price(om.group(1) if om else ""),
            "reviews": reviews,
            "pct": pct,
            "released": strip_tags(rm.group(1)) if rm else "",
            "image": (im.group(1).replace("&amp;", "&") if im else ""),
            "url": "https://store.steampowered.com/app/%s/" % appid,
        })
    return rows


def fetch_specials(pages):
    seen = {}
    for i in range(pages):
        url = ("https://store.steampowered.com/search/results/?query&start=%d&count=50"
               "&sort_by=Price_ASC&specials=1&infinite=1&cc=cn&l=schinese" % (i * 50))
        j = get_json(url)
        rows = parse_search_page(j.get("results_html", ""))
        for r in rows:
            seen.setdefault(r["appid"], r)
        print("  第 %d 页 %d 条，累计去重 %d" % (i + 1, len(rows), len(seen)))
        time.sleep(0.8)
    return list(seen.values())


def appdetails(appid):
    url = ("https://store.steampowered.com/api/appdetails?appids=%s&cc=cn&l=schinese" % appid)
    j = get_json(url)
    d = j.get(appid)
    if not d or not d.get("success"):
        return None
    x = d["data"]
    return {
        "desc": x.get("short_description") or "",
        "genres": [g["description"] for g in x.get("genres", [])],
        "chinese": "简体中文" in (x.get("supported_languages") or ""),
        "type": x.get("type"),
        "release": (x.get("release_date") or {}).get("date"),
    }


class ITAD:
    def __init__(self, key):
        self.key = key
        self.cache = {}

    def history_low(self, appid):
        if not self.key:
            return None
        if appid in self.cache:
            return self.cache[appid]
        low = None
        try:
            lk = get_json("https://api.isthereanydeal.com/games/lookup/v1?key=%s&appid=%s"
                          % (self.key, appid))
            if lk.get("found"):
                gid = lk["game"]["id"]
                hist = get_json("https://api.isthereanydeal.com/games/history/v2?key=%s&id=%s&country=CN"
                                % (self.key, gid))
                prices = [h["deal"]["price"]["amount"] for h in hist if h.get("deal")]
                if prices:
                    low = min(prices)
        except Exception as e:  # noqa: BLE001
            print("    ITAD 失败 %s: %s" % (appid, str(e)[:60]))
        self.cache[appid] = low
        return low


def verdict(price, low):
    """返回 (标签, 说明)"""
    if low is None:
        return None, None
    if price <= low * 1.001:
        return "史低", "持平或低于历史最低"
    if price <= low * 1.12:
        return "接近史低", "只比史低贵一点"
    if price <= low * 1.35:
        return "中位", "比史低贵一些"
    return "偏贵", "离史低还远，可以等"


def score(g, low):
    """榜单排序分：评价质量 45% + 折扣 25% + 热度 20% + 价位甜点 10%"""
    pct = (g.get("pct") or 0) / 100.0
    rev = g.get("reviews") or 0
    disc = min(g.get("discount") or 0, 90) / 90.0
    heat = min(math.log10(rev + 1) / 5.0, 1.0) if rev else 0
    price = g.get("price") or 0
    sweet = 1.0 if 15 <= price <= 80 else (0.75 if price < 15 else 0.6)
    v = pct * 0.45 + disc * 0.25 + heat * 0.20 + sweet * 0.10
    if low and price <= low * 1.02:
        v += 0.06
    return round(v, 4)


def main():
    t0 = time.time()
    key = os.environ.get("ITAD_KEY", "").strip()
    if not key:
        cfg = os.path.join(ROOT, "..", "steam-price", "config.json")
        if os.path.exists(cfg):
            try:
                key = (json.load(open(cfg, encoding="utf-8")).get("itadKey") or "").strip()
            except Exception:  # noqa: BLE001
                pass

    print("抓特价列表（%d 页）…" % PAGES)
    rows = fetch_specials(PAGES)
    print("  共 %d 个特价条目" % len(rows))

    cand = [r for r in rows
            if r["price"] is not None
            and (r["discount"] or 0) >= 50
            and (r["reviews"] or 0) >= 300
            and (r["pct"] or 0) >= 85]
    print("  过筛（-50%% 以上 / 300+ 评价 / 85%%+ 好评）：%d 个" % len(cand))

    cand.sort(key=lambda r: score(r, None), reverse=True)
    cand = cand[:LIMIT]
    print("  取前 %d 个补详情与史低…" % len(cand))

    itad = ITAD(key)
    items = []
    for i, g in enumerate(cand, 1):
        try:
            det = appdetails(g["appid"])
        except Exception as e:  # noqa: BLE001
            print("   %2d. %s 详情失败：%s" % (i, g["name"][:20], str(e)[:40]))
            det = None
        if det and det.get("type") not in (None, "game"):
            print("   %2d. 跳过非游戏：%s" % (i, g["name"][:20]))
            continue
        low = itad.history_low(g["appid"]) if key else None
        tag, note = verdict(g["price"], low)
        g = dict(g)
        g.update({
            "desc": (det or {}).get("desc", ""),
            "genres": (det or {}).get("genres", []),
            "chinese": (det or {}).get("chinese", False),
            "historyLow": low,
            "verdict": tag,
            "verdictNote": note,
            "score": score(g, low),
        })
        items.append(g)
        print("   %2d. %-24s -%d%% ¥%-6s 好评%s%% %s条 %s" % (
            i, g["name"][:24], g["discount"], g["price"], g["pct"], g["reviews"], tag or ""))
        time.sleep(1.0)

    items.sort(key=lambda x: x["score"], reverse=True)

    out = {
        "generatedAt": datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M"),
        "count": len(items),
        "withHistory": sum(1 for x in items if x.get("historyLow")),
        "items": items,
    }
    data_dir = os.path.join(ROOT, "data")
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "games.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    # 史低缓存：给每小时的「临时工」检测用，逐日累积
    hist_path = os.path.join(data_dir, "history.json")
    hist = {}
    if os.path.exists(hist_path):
        try:
            with open(hist_path, encoding="utf-8") as f:
                hist = json.load(f)
        except Exception:  # noqa: BLE001
            hist = {}
    hist.pop("_updated", None)
    for it in items:
        if it.get("historyLow"):
            hist[str(it["appid"])] = {
                "name": it["name"],
                "low": it["historyLow"],
                "update": out["generatedAt"],
            }
    payload = {"_updated": out["generatedAt"]}
    payload.update(hist)
    with open(hist_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=0)

    print("\n写入 %s（%d 条，%d 条有史低）用时 %.0fs" % (
        path, len(items), out["withHistory"], time.time() - t0))


if __name__ == "__main__":
    main()
