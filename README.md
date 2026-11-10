# Steam 折扣精选

按「好评率 + 折扣力度 + 历史最低价」筛出来的 Steam 国区折扣榜单，每天自动更新；每小时检查一次疑似标错价。

## 为什么需要 GitHub Actions

国内直连 `store.steampowered.com` 的 443 端口经常超时（时好时坏），本机抓取不可靠。所以抓取放在 Actions 的境外机器上跑，结果提交回仓库，前端读同域的 JSON——**零服务器、零成本、关机也不影响**。

## 结构

```
scripts/fetch.py        每天 04:00 抓特价 → 过滤 → 补简介/史低 → data/games.json
scripts/glitch.py       每小时检查疑似标错价 → data/glitch.json（可推飞书）
index.html              静态榜单页，读 data/games.json
data/games.json         榜单数据（自动生成，别手改）
data/history.json       史低缓存，逐日累积
data/glitch.json        最近一次异常低价检查结果
```

## 筛选规则

- 折扣 ≥ 50%
- 评价 ≥ 300 条
- 好评率 ≥ 85%
- 只要游戏本体（排除 DLC / 原声带）
- 排序：好评率 45% + 折扣 25% + 热度 20% + 价位甜点 10%，史低额外加分

## 判「值不值」

用 IsThereAnyDeal 的国区历史价算史低：

| 当前价 / 史低 | 标签 |
|---|---|
| ≤ 1.00 | 史低 |
| ≤ 1.12 | 接近史低 |
| ≤ 1.35 | 中位 |
| > 1.35 | 偏贵（可以等） |

## 疑似标错价（临时工）

```
折扣 ≥ 80% 且 当前价 ≤ 史低 × 0.7 且 评价 ≥ 500
```

没有史低缓存的游戏直接跳过，不猜、不误报。

## 本地跑

```bash
python scripts/fetch.py          # 需要能连上 store.steampowered.com
python scripts/glitch.py
```

环境变量：`ITAD_KEY`（没有就跳过史低）、`PAGES`、`LIMIT`、`FEISHU_WEBHOOK`。

## 仓库 Secrets

| 名称 | 用途 | 必需 |
|---|---|---|
| `ITAD_KEY` | IsThereAnyDeal 历史价 | 建议 |
| `FEISHU_WEBHOOK` | 异常低价推到飞书 | 可选 |

## 边界

- 只显示 Steam 国区当前价，不做汇率换算
- 史低口径为 ITAD 记录的国区历史价，可能比 SteamDB 略保守
- 第三方平台（小黑盒/杉果/SteamPY）价格不在本仓库范围
