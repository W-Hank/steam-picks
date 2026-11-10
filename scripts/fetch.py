#!/usr/bin/env python3
"""
Steam 折扣精选：抓特价 → 过滤 → 补简介/史低 → 生成 data/games.json

只用标准库。跑在 GitHub Actions 上（国内直连 store 的 443 会超时）。
env:
  ITAD_KEY   可选，IsThereAnyDeal 的 key；没有就跳过史低
  PAGES      每种排序抓几页，默认 2
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
PAGES = int(os.environ.get("PAGES", "2"))
LIMIT = int(os.environ.get("LIMIT", "30"))
CN_TZ = timezone(timedelta(hours=8))

SORTS = ["Reviews_DESC", "Price_DESC", "Price_ASC", "", "Released_DESC"]
MIN_DISCOUNT = 50
MIN_REVIEWS = 100
MIN_PCT = 85


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

        # 折扣优先取 data-discount 属性（新版搜索行里一定有），再退回文本
        dm = re.search(r'data-discount="(\d+)"', block)
        if not dm:
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


def fetch_pool():
    """多种排序各抓几页，尽量覆盖热门折扣，而不是只有 1 块钱的小游戏"""
    seen = {}
    for s in SORTS:
        for p in range(PAGES):
            url = ("https://store.steampowered.com/search/results/?query&start=%d&count=50"
                   "&sort_by=%s&specials=1&infinite=1&cc=cn&l=schinese" % (p * 50, s))
            try:
                j = get_json(url)
            except Exception as e:  # noqa: BLE001
                print("   排序 %-14s 第%d页失败 %s" % (s or "(默认)", p + 1, str(e)[:50]))
                continue
            rows = parse_search_page(j.get("results_html", ""))
            fresh = 0
            for r in rows:
                if r["appid"] not in seen:
                    seen[r["appid"]] = r
                    fresh += 1
            print("   排序 %-14s 第%d页 %2d 条（新增 %2d，累计 %3d）" % (s or "(默认)", p + 1, len(rows), fresh, len(seen)))
            time.sleep(0.7)
    return list(seen.values())


def review_from_store_page(appid):
    """搜索页没带评测信息时，从商店页兜底解析"""
    url = "https://store.steampowered.com/app/%s/?cc=cn&l=schinese" % appid
    html = get(url, timeout=35).decode("utf-8", "replace")
    pct = reviews = None
    pm = re.search(r"(\d+)%[^<]{0,12}为好评", html)
    if pm:
        pct = int(pm.group(1))
    cm = re.search(r"([\d,]+)\s*篇用户评测", html)
    if cm:
        reviews = int(cm.group(1).replace(",", ""))
    return reviews, pct


def appdetails(appid):
    url = "https://store.steampowered.com/api/appdetails?appids=%s&cc=cn&l=schinese" % appid
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
            print("      ITAD 失败 %s: %s" % (appid, str(e)[:50]))
        self.cache[appid] = low
        return low


def verdict(price, low):
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

    print("抓特价池（%d 种排序 × %d 页）…" % (len(SORTS), PAGES))
    pool = fetch_pool()
    print("  去重后 %d 个特价条目" % len(pool))

    cand = [r for r in pool if r["price"] is not None and (r["discount"] or 0) >= MIN_DISCOUNT]
    print("  折扣 ≥%d%%：%d 个" % (MIN_DISCOUNT, len(cand)))
    if not cand:
        print("  [诊断] 抽样 5 条，看看解析到了什么：")
        for r in pool[:5]:
            print("    ", json.dumps(r, ensure_ascii=False)[:220])

    # 搜索页缺评测信息的，补一次商店页（最多 40 个，按折扣排序优先）
    need = [r for r in cand if r.get("reviews") is None][:40]
    if need:
        print("  补评测信息 %d 个…" % len(need))
        for r in need:
            try:
                reviews, pct = review_from_store_page(r["appid"])
                if reviews is not None:
                    r["reviews"] = reviews
                if pct is not None:
                    r["pct"] = pct
            except Exception as e:  # noqa: BLE001
                print("     补失败 %s: %s" % (r["appid"], str(e)[:40]))
            time.sleep(0.6)

    good = [r for r in cand if (r.get("reviews") or 0) >= MIN_REVIEWS and (r.get("pct") or 0) >= MIN_PCT]
    print("  评价 ≥%d 且好评 ≥%d%%：%d 个" % (MIN_REVIEWS, MIN_PCT, len(good)))

    good.sort(key=lambda r: score(r, None), reverse=True)
    good = good[: max(LIMIT * 2, 40)]
    print("  取前 %d 个补简介与史低…" % len(good))

    itad = ITAD(key)
    items = []
    for i, g in enumerate(good, 1):
        try:
            det = appdetails(g["appid"])
        except Exception as e:  # noqa: BLE001
            print("   %2d. 详情失败 %s（%s）" % (i, g["name"][:18], str(e)[:40]))
            det = None
        if det and det.get("type") not in (None, "game"):
            continue
        low = itad.history_low(g["appid"]) if key else None
        tag, note = verdict(g["price"], low)
        item = dict(g)
        item.update({
            "desc": (det or {}).get("desc", ""),
            "genres": (det or {}).get("genres", []),
            "chinese": (det or {}).get("chinese", False),
            "historyLow": low,
            "verdict": tag,
            "verdictNote": note,
            "score": score(g, low),
        })
        items.append(item)
        print("   %2d. %-22s -%d%% ¥%-6s 好评%s%% %s条 %s" % (
            i, g["name"][:22], g["discount"], g["price"], g["pct"], g["reviews"], tag or "-"))
        time.sleep(0.8)

    items.sort(key=lambda x: x["score"], reverse=True)
    items = items[:LIMIT]

    out = {
        "generatedAt": datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M"),
        "count": len(items),
        "withHistory": sum(1 for x in items if x.get("historyLow")),
        "items": items,
    }
    data_dir = os.path.join(ROOT, "data")
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, "games.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

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
            hist[str(it["appid"])] = {"name": it["name"], "low": it["historyLow"], "update": out["generatedAt"]}
    payload = {"_updated": out["generatedAt"]}
    payload.update(hist)
    with open(hist_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=0)

    print("\n写入 games.json：%d 条，其中 %d 条有史低；用时 %.0fs" % (
        out["count"], out["withHistory"], time.time() - t0))


if __name__ == "__main__":
    main()
