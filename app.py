# -*- coding: utf-8 -*-
"""
六安市水利行业招采信息监测
================================
数据来源：六安市公共资源交易中心（ggzy.luan.gov.cn）全文检索接口。
本程序做一件事：把全市（金安/裕安/叶集/霍邱/舒城/金寨/霍山 + 市直）
各交易类别里"水利行业"相关的招标、采购、中标、成交等公告聚合到一个页面，
支持按地区、公告阶段、关键词、时间窗口筛选，并可导出 CSV。

为什么需要后端：政府接口没有开放跨域（CORS），浏览器网页无法直接抓，
所以用这个很轻的 Flask 服务在服务端代理抓取，再把干净的数据交给前端。

运行：
    pip install flask requests
    python app.py
然后浏览器打开 http://127.0.0.1:5000
"""

import re
import csv
import io
import time
import threading
import datetime as dt
from collections import Counter

import requests
from flask import Flask, jsonify, request, Response

# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
BASE = "https://ggzy.luan.gov.cn"
API = BASE + "/inteligentsearch/rest/esinteligentsearch/getFullTextDataNew"

# 水利行业关键词（对公告标题做"包含任意一个"的精确匹配）。
# 想扩大/缩小范围，直接改这个列表即可。
KEYWORDS = [
    "水利", "水库", "河道", "河湖", "灌溉", "防洪", "泵站", "堤防", "渠道",
    "清淤", "幸福河", "水系", "圩堤", "闸站", "供水", "排涝", "引水", "塘坝",
    "山洪", "中小河流", "淠河", "淠史杭", "水环境", "水毁", "农田水利",
    "河流治理", "水资源", "灌区", "水厂", "污水处理",
]

# 六安市行政区划代码 -> 名称
XIAQU = {
    "341501": "市直",
    "341502": "金安区",
    "341503": "裕安区",
    "341504": "叶集区",
    "341522": "霍邱县",
    "341523": "舒城县",
    "341524": "金寨县",
    "341525": "霍山县",
}
# 标题里出现的区县名（比区划代码更可靠，尤其是"非进场"小项目）
REGION_NAMES = ["金安区", "裕安区", "叶集区", "霍邱县", "舒城县", "金寨县", "霍山县",
                "金安", "裕安", "叶集", "霍邱", "舒城", "金寨", "霍山"]
REGION_CANON = {"金安": "金安区", "裕安": "裕安区", "叶集": "叶集区",
                "霍邱": "霍邱县", "舒城": "舒城县", "金寨": "金寨县", "霍山": "霍山县"}

HEADERS = {
    "Content-Type": "application/json;charset=utf-8",
    "Referer": BASE + "/jyxx/001001/jysearch.html",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) LuanWaterMonitor/1.0",
}

TAG_RE = re.compile(r"<[^>]+>")


def clean(s):
    """去掉接口返回标题里的高亮 <em> 等标签。"""
    return TAG_RE.sub("", s or "").strip()


def clean_summary(s, limit=240):
    """清洗正文摘要：去标签、解实体、压空白、截断。"""
    s = TAG_RE.sub("", s or "")
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
          .replace("&gt;", ">").replace("&quot;", '"').replace("&#160;", " "))
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit] + ("…" if len(s) > limit else "")


GONGGAO = "招标·采购公告"
HOUXUAN = "中标候选人"
RESULT = "中标·成交结果"
CHANGE = "澄清·变更"
OTHER = "其他"

# 已知类别号后 3 位 -> 阶段（各交易类型下含义基本一致）
STAGE_BY_TAIL = {
    "002": GONGGAO,   # 招标/采购公告、成交公示(001008002)——由标题再校正
    "001": GONGGAO,   # 采购公告 / 发包公告 / 交易公告
    "009": GONGGAO,   # 项目信息（项目登记，视为机会）
    "005": GONGGAO,   # 项目信息 / 发包公告变更——由标题校正
    "004": HOUXUAN,   # 中标候选人公示
    "003": RESULT,    # 成交公告 / 成交结果公示 / 澄清——由标题校正
}
# 明显属于"附属信息"、不是投标机会的标题特征
ANCILLARY = ("履约", "合同信息", "合同签订", "保证金", "开评标", "评标委员会",
             "中标通知书", "验收", "标后")


def classify_stage(title, categorynum):
    """判断公告阶段：标题里的明确措辞优先，其次按类别号，附属信息归'其他'。"""
    t = title
    if "中标候选人" in t:
        return HOUXUAN
    if any(k in t for k in ("中标结果", "成交结果", "成交公告", "成交公示",
                            "中标公示", "中标结果公告", "中标通知")):
        return RESULT
    if any(k in t for k in ("澄清", "变更", "更正", "答疑", "补遗", "终止", "废标", "流标")):
        return CHANGE
    if any(k in t for k in ("招标公告", "采购公告", "竞争性磋商", "竞争性谈判",
                            "询价", "询比", "磋商公告", "谈判公告", "交易公告",
                            "发包公告", "招标", "邀请")):
        return GONGGAO
    if any(k in t for k in ANCILLARY):
        return OTHER
    # 兜底：按类别号后三位归类；未知则视为项目/发包公告（可投机会）
    tail = categorynum[6:9] if len(categorynum) >= 9 else ""
    return STAGE_BY_TAIL.get(tail, GONGGAO)


def guess_region(title, xiaqucode):
    """判断项目所属区县：先看标题里的区县名，再看区划代码。"""
    for name in REGION_NAMES:
        if name in title:
            return REGION_CANON.get(name, name)
    return XIAQU.get(str(xiaqucode), "其他")


def matched_keywords(title):
    return [k for k in KEYWORDS if k in title]


# --------------------------------------------------------------------------- #
# 抓取（带重试 + 简单内存缓存）
# --------------------------------------------------------------------------- #
_cache = {}            # key -> (timestamp, data)
_cache_lock = threading.Lock()
CACHE_TTL = 600        # 10 分钟


def build_payload(days, rn):
    union = [{"fieldName": "title", "equal": k, "isLike": True, "likeType": 2}
             for k in KEYWORDS]
    payload = {
        "token": "", "pn": 0, "rn": rn, "wd": "", "fields": "title",
        "cnum": "001", "sort": "{'webdate':'0'}", "ssort": "", "cl": 60,
        "terminal": "", "condition": None, "time": None, "highlights": "",
        "statistics": None, "unionCondition": union, "accuracy": "",
        "noParticiple": "1", "searchRange": None, "isBusiness": "1",
    }
    if days and int(days) > 0:
        start = (dt.date.today() - dt.timedelta(days=int(days)))
        payload["time"] = [{
            "fieldName": "webdate",
            "startTime": start.strftime("%Y-%m-%d") + " 00:00:00",
            "endTime": dt.date.today().strftime("%Y-%m-%d") + " 23:59:59",
        }]
    return payload


def fetch_raw(days=60, rn=None, retries=5):
    if rn is None:
        # 抓取量随时间窗口放大，避免长时段被截断（上限从严，减轻对方站点压力）
        rn = min(1500, max(500, int(days) * 12))
    payload = build_payload(days, rn)
    last_err = None
    backoff = 2
    for _ in range(retries):
        try:
            r = requests.post(API, json=payload, headers=HEADERS, timeout=35)
            body = r.text.lstrip()
            # 交易站有 WAF：被拦时返回 403 或一段 HTML 挑战页，而非 JSON。
            # 注意正常数据的 Content-Type 是 text/plain，故只按状态码和内容判断。
            if r.status_code == 403 or body[:1] in ("<",) or not body:
                raise RuntimeError(
                    "被交易网站临时限流（HTTP %s）。请稍等几分钟再试，"
                    "并避免频繁点击刷新——本程序已自动缓存 10 分钟。" % r.status_code)
            data = r.json()
            return data.get("result", {}).get("records", []) or []
        except Exception as e:      # DNS 抖动 / 超时 / 限流，退避后重试
            last_err = e
            time.sleep(backoff)
            backoff = min(backoff * 2, 12)
    raise RuntimeError(str(last_err))


def normalize(records):
    out, seen = [], set()
    for x in records:
        infoid = x.get("infoid")
        title = clean(x.get("title"))
        if not title:
            continue
        # 用 infoid + 类别去重（同一公告可能在多类别下重复出现）
        key = (infoid, x.get("categorynum"))
        if key in seen:
            continue
        seen.add(key)
        cat = x.get("categorynum", "")
        link = x.get("linkurl") or ""
        infoid = x.get("infoid") or ""
        rel = x.get("relationguid") or ""
        # 正确的详情页（网站自用）：所有类别统一用 jyxxparentDetail，按 infoid 动态加载
        if infoid:
            url = (BASE + "/jyxxparentDetail.html?infoid=%s&categorynum=%s&relationguid=%s"
                   % (infoid, cat, rel))
        else:
            url = (BASE + link) if link.startswith("/") else (link or BASE)
        date = (x.get("infodatepx") or x.get("webdate") or x.get("infodate") or "")[:10]
        out.append({
            "title": title,
            "date": date,
            "region": guess_region(title, x.get("xiaqucode")),
            "stage": classify_stage(title, cat),
            "keywords": matched_keywords(title),
            "summary": clean_summary(x.get("content")),
            "url": url,
            "categorynum": cat,
        })
    out = collapse_projects(out)
    out.sort(key=lambda r: r["date"], reverse=True)
    return out


# ---- 把同一项目的所有环节文件合并成一行（列表不再出现重复项目名）---------- #
# 结尾的阶段性措辞，用于还原"项目核心名"。注意不删除"第X包/标段"等分包信息。
_STAGE_SUFFIX = [
    "中标候选人公示", "中标候选人公告", "中标结果公示", "中标结果公告",
    "成交结果公告", "成交结果公示", "成交公告", "成交公示", "中标公示", "中标公告",
    "评标结果公示", "结果公告", "结果公示", "中标候选人", "招标公告", "采购公告",
    "竞争性磋商公告", "竞争性谈判公告", "询价公告", "询比公告", "发包公告",
    "更正公告", "澄清公告", "变更公告", "终止公告", "中标通知书",
]
_BRACKET_END = re.compile(r"[\[【][^\]】]*[\]】]\s*$")     # 结尾的 [中标通知书] 等标签
_RETRY_END = re.compile(r"[（(]第[一二三四五六七八九十\d]+次[)）]\s*$")  # (第N次)
# 阶段推进程度：数值越大越"靠后"，用于确定项目当前状态
_STAGE_ADV = {"其他": 0, "招标·采购公告": 1, "澄清·变更": 2,
              "中标候选人": 3, "中标·成交结果": 4}


def project_core(title):
    """还原项目核心名：去掉结尾的阶段措辞、方括号标签、(第N次)。保留分包信息。"""
    t = title.strip()
    t = _BRACKET_END.sub("", t)
    t = _RETRY_END.sub("", t)
    for _ in range(3):
        for suf in _STAGE_SUFFIX:
            if t.endswith(suf):
                t = t[:-len(suf)]
        t = t.strip("（）() 　-—－·、,，。.")
    return t or title.strip()


def collapse_projects(items):
    groups = {}
    for x in items:
        groups.setdefault(project_core(x["title"]), []).append(x)
    out = []
    for core, g in groups.items():
        # 当前状态 = 日期最新、其次阶段最靠后的那条
        head = sorted(g, key=lambda r: (r["date"], _STAGE_ADV.get(r["stage"], 0)))[-1]
        # 进度时间线：按(日期,阶段)去重后按时间排序
        seen, timeline = set(), []
        for r in sorted(g, key=lambda r: r["date"]):
            k = (r["date"], r["stage"])
            if k in seen:
                continue
            seen.add(k)
            timeline.append({"date": r["date"], "stage": r["stage"], "url": r["url"]})
        kws = sorted({k for r in g for k in r["keywords"]},
                     key=lambda k: -len(k))
        region = Counter(r["region"] for r in g).most_common(1)[0][0]
        out.append({
            "title": core,
            "date": head["date"],
            "region": region,
            "stage": head["stage"],           # 当前状态
            "keywords": kws,
            "summary": max((r["summary"] for r in g), key=len, default=""),
            "url": head["url"],               # 指向当前状态的公告
            "docs": len(g),
            "timeline": timeline,
        })
    return out


def get_projects(days=60, force=False):
    ckey = "days=%s" % days
    now = time.time()
    with _cache_lock:
        hit = _cache.get(ckey)
        if hit and not force and (now - hit[0] < CACHE_TTL):
            return hit[1], hit[0]
    data = normalize(fetch_raw(days=days))
    with _cache_lock:
        _cache[ckey] = (now, data)
    return data, now


# --------------------------------------------------------------------------- #
# Flask
# --------------------------------------------------------------------------- #
app = Flask(__name__)


@app.route("/api/projects")
def api_projects():
    days = request.args.get("days", "60")
    force = request.args.get("refresh") == "1"
    try:
        data, ts = get_projects(days=days, force=force)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    return jsonify({
        "ok": True,
        "updated": dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(data),
        "items": data,
    })


@app.route("/api/export.csv")
def api_export():
    days = request.args.get("days", "60")
    data, _ = get_projects(days=days)
    buf = io.StringIO()
    buf.write("\ufeff")  # BOM，Excel 打开中文不乱码
    w = csv.writer(buf)
    w.writerow(["发布日期", "地区", "公告阶段", "命中关键词", "项目/公告标题", "详情链接"])
    for r in data:
        w.writerow([r["date"], r["region"], r["stage"],
                    " ".join(r["keywords"]), r["title"], r["url"]])
    fname = "luan_water_tender_%s.csv" % dt.date.today().strftime("%Y%m%d")
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=%s" % fname})


@app.route("/")
def index():
    return Response(PAGE, mimetype="text/html; charset=utf-8")


# --------------------------------------------------------------------------- #
# 前端页面（内联，无外部 CDN/字体依赖，可离线运行）
# --------------------------------------------------------------------------- #
PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>六安水利招采监测</title>
<style>
  :root{
    --ink:#0e2b33; --ink2:#12454f; --teal:#0f7d8c; --teal-d:#0a5e6a;
    --paper:#f5f8f9; --card:#ffffff; --line:#dde6e8; --muted:#5b7178;
    --active:#0f9d76; --result:#7a5cc4; --amber:#c07a17; --grey:#8a9aa0;
    --shadow:0 1px 2px rgba(14,43,51,.06),0 6px 20px rgba(14,43,51,.05);
  }
  *{box-sizing:border-box}
  html,body{margin:0}
  body{
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
      "Hiragino Sans GB","Microsoft YaHei","Source Han Sans SC","Noto Sans CJK SC",sans-serif;
    color:var(--ink); background:var(--paper);
    font-size:14px; line-height:1.55; -webkit-font-smoothing:antialiased;
  }
  a{color:inherit;text-decoration:none}
  .tnum{font-variant-numeric:tabular-nums;font-feature-settings:"tnum"}

  /* header */
  header{
    position:sticky;top:0;z-index:20;
    background:linear-gradient(180deg,var(--ink) 0%,var(--ink2) 100%);
    color:#eaf3f4; box-shadow:var(--shadow);
  }
  .hwrap{max-width:1120px;margin:0 auto;padding:14px 20px;
    display:flex;align-items:center;gap:16px;flex-wrap:wrap}
  .brand{display:flex;align-items:baseline;gap:10px}
  .brand h1{font-size:19px;margin:0;font-weight:700;letter-spacing:.5px}
  .brand .sub{font-size:12px;color:#9fc2c7}
  .hstat{margin-left:auto;display:flex;align-items:center;gap:18px;font-size:12px;color:#bcd6d9}
  .hstat b{color:#fff;font-size:15px}
  .hbtn{
    border:1px solid rgba(255,255,255,.25);background:rgba(255,255,255,.06);
    color:#eaf3f4;padding:6px 13px;border-radius:7px;cursor:pointer;font-size:13px;
    display:inline-flex;align-items:center;gap:6px;transition:.15s;
  }
  .hbtn:hover{background:rgba(255,255,255,.16)}
  .hbtn:disabled{opacity:.5;cursor:default}

  /* toolbar */
  .tools{max-width:1120px;margin:18px auto 0;padding:0 20px}
  .searchbar{display:flex;gap:10px;margin-bottom:12px;flex-wrap:wrap}
  .searchbar input{
    flex:1;min-width:200px;border:1px solid var(--line);border-radius:9px;
    padding:10px 14px;font-size:14px;background:var(--card);color:var(--ink);
  }
  .searchbar input:focus{outline:none;border-color:var(--teal);
    box-shadow:0 0 0 3px rgba(15,125,140,.12)}
  select{
    border:1px solid var(--line);border-radius:9px;padding:10px 12px;
    font-size:13px;background:var(--card);color:var(--ink);cursor:pointer;
  }
  .expbtn{border:1px solid var(--teal);background:var(--teal);color:#fff;
    padding:10px 15px;border-radius:9px;cursor:pointer;font-size:13px;font-weight:600}
  .expbtn:hover{background:var(--teal-d)}

  .chips{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:9px}
  .chips .lab{font-size:12px;color:var(--muted);align-self:center;margin-right:2px;min-width:56px}
  .chip{
    border:1px solid var(--line);background:var(--card);color:var(--muted);
    padding:5px 12px;border-radius:20px;cursor:pointer;font-size:13px;transition:.12s;
  }
  .chip:hover{border-color:var(--teal);color:var(--teal)}
  .chip.on{background:var(--ink2);border-color:var(--ink2);color:#fff}
  .chip .c{opacity:.65;font-size:11px;margin-left:3px}

  /* list */
  main{max-width:1120px;margin:16px auto 60px;padding:0 20px}
  .count{font-size:12px;color:var(--muted);margin:6px 2px 12px}
  .row{
    display:flex;align-items:flex-start;gap:14px;background:var(--card);
    border:1px solid var(--line);border-left:4px solid var(--grey);
    border-radius:10px;padding:13px 16px;margin-bottom:9px;box-shadow:var(--shadow);
    transition:.12s;cursor:pointer;
  }
  .row:hover{border-color:var(--teal);border-left-color:var(--teal);transform:translateY(-1px)}
  .row.s-gonggao{border-left-color:var(--active)}
  .row.s-houxuan{border-left-color:var(--amber)}
  .row.s-result{border-left-color:var(--result)}
  .row.s-change{border-left-color:var(--grey)}
  .date{min-width:82px;color:var(--muted);font-size:12.5px;padding-top:2px}
  .body{flex:1;min-width:0}
  .title{font-size:14.5px;font-weight:600;color:var(--ink);line-height:1.5}
  .row:hover .title{color:var(--teal-d)}
  .meta{display:flex;gap:7px;flex-wrap:wrap;margin-top:7px;align-items:center}
  .tag{font-size:11.5px;padding:2px 9px;border-radius:5px;white-space:nowrap}
  .tag.region{background:#eef4f5;color:var(--ink2);border:1px solid #dce8ea}
  .tag.stage{color:#fff}
  .stage-gonggao{background:var(--active)}
  .stage-houxuan{background:var(--amber)}
  .stage-result{background:var(--result)}
  .stage-change{background:var(--grey)}
  .stage-other{background:#6b8288}
  .kw{font-size:11px;color:var(--teal);background:#e7f4f5;padding:1px 7px;border-radius:4px}
  .go{color:var(--grey);font-size:18px;align-self:center;padding-left:4px}
  .row:hover .go{color:var(--teal)}

  .msg{text-align:center;padding:60px 20px;color:var(--muted)}
  .msg.err{color:#b4443a}
  .pager{display:flex;justify-content:center;align-items:center;gap:6px;margin-top:20px;flex-wrap:wrap}
  .pager button{border:1px solid var(--line);background:var(--card);color:var(--ink);
    min-width:36px;height:34px;border-radius:8px;cursor:pointer;font-size:13px;padding:0 10px}
  .pager button:hover:not(:disabled){border-color:var(--teal);color:var(--teal)}
  .pager button.on{background:var(--ink2);border-color:var(--ink2);color:#fff}
  .pager button:disabled{opacity:.4;cursor:default}
  .spin{width:22px;height:22px;border:3px solid var(--line);border-top-color:var(--teal);
    border-radius:50%;animation:sp .7s linear infinite;display:inline-block;vertical-align:middle}
  @keyframes sp{to{transform:rotate(360deg)}}
  @media(max-width:640px){
    .date{min-width:0;font-size:12px}
    .row{gap:10px;padding:12px 13px}
    .hstat{width:100%;order:3}
  }
</style>
</head>
<body>
<header>
  <div class="hwrap">
    <div class="brand">
      <h1>六安水利招采监测</h1>
      <span class="sub">全市水利行业 招标 · 采购 · 中标 · 成交</span>
    </div>
    <div class="hstat">
      <span>命中 <b id="hcount" class="tnum">–</b> 条</span>
      <span>更新 <b id="hupd" class="tnum">–</b></span>
      <button class="hbtn" id="refresh" onclick="load(true)">↻ 刷新</button>
    </div>
  </div>
</header>

<div class="tools">
  <div class="searchbar">
    <input id="q" placeholder="在标题中搜索关键词，如：水库、河道、泵站、金寨…" oninput="apply()">
    <select id="days" onchange="load(false)">
      <option value="30">近 30 天</option>
      <option value="60" selected>近 60 天</option>
      <option value="90">近 90 天</option>
      <option value="180">近半年</option>
      <option value="365">近一年</option>
    </select>
    <select id="sort" onchange="apply()">
      <option value="desc">最新在前</option>
      <option value="asc">最早在前</option>
    </select>
    <button class="expbtn" onclick="exportCsv()">导出 CSV</button>
  </div>
  <div class="chips" id="regionChips">
    <span class="lab">地区</span>
  </div>
  <div class="chips" id="stageChips">
    <span class="lab">阶段</span>
  </div>
</div>

<main>
  <div class="count" id="count"></div>
  <div id="list"><div class="msg"><span class="spin"></span></div></div>
  <div class="pager" id="pager"></div>
</main>

<script>
const REGIONS = ["全部","金安区","裕安区","叶集区","霍邱县","舒城县","金寨县","霍山县","市直","其他"];
const STAGES  = ["全部","招标·采购公告","中标候选人","中标·成交结果","澄清·变更","其他"];
const PAGE_SIZE = 25;

let ALL = [];          // 当前时间窗口内全部数据
let VIEW = [];         // 筛选后的数据
let region = "全部", stage = "全部", page = 1;

function stageClass(s){
  return s==="招标·采购公告" ? "gonggao"
       : s==="中标候选人"     ? "houxuan"
       : s==="中标·成交结果"  ? "result"
       : s==="澄清·变更"      ? "change" : "other";
}
function esc(s){return (s||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}

function buildChips(){
  const rc = document.getElementById("regionChips");
  const sc = document.getElementById("stageChips");
  rc.querySelectorAll(".chip").forEach(e=>e.remove());
  sc.querySelectorAll(".chip").forEach(e=>e.remove());
  REGIONS.forEach(r=>{
    const n = r==="全部"?ALL.length:ALL.filter(x=>x.region===r).length;
    if(r!=="全部" && n===0) return;
    const el=document.createElement("span");
    el.className="chip"+(r===region?" on":"");
    el.innerHTML=esc(r)+'<span class="c tnum">'+n+'</span>';
    el.onclick=()=>{region=r;page=1;syncChips();apply();};
    rc.appendChild(el);
  });
  STAGES.forEach(s=>{
    const n = s==="全部"?ALL.length:ALL.filter(x=>x.stage===s).length;
    if(s!=="全部" && n===0) return;
    const el=document.createElement("span");
    el.className="chip"+(s===stage?" on":"");
    el.innerHTML=esc(s)+'<span class="c tnum">'+n+'</span>';
    el.onclick=()=>{stage=s;page=1;syncChips();apply();};
    sc.appendChild(el);
  });
}
function syncChips(){
  document.querySelectorAll("#regionChips .chip").forEach(e=>{
    e.classList.toggle("on", e.textContent.replace(/\d+$/,"")===region);
  });
  document.querySelectorAll("#stageChips .chip").forEach(e=>{
    e.classList.toggle("on", e.textContent.replace(/\d+$/,"")===stage);
  });
}

function apply(){
  const q = document.getElementById("q").value.trim();
  const sort = document.getElementById("sort").value;
  VIEW = ALL.filter(x=>{
    if(region!=="全部" && x.region!==region) return false;
    if(stage!=="全部"  && x.stage!==stage)  return false;
    if(q && x.title.indexOf(q)===-1) return false;
    return true;
  });
  VIEW.sort((a,b)=> sort==="asc" ? (a.date<b.date?-1:1) : (a.date>b.date?-1:1));
  render();
}

function render(){
  const list = document.getElementById("list");
  const total = VIEW.length;
  const pages = Math.max(1, Math.ceil(total/PAGE_SIZE));
  if(page>pages) page=pages;
  const start=(page-1)*PAGE_SIZE, slice=VIEW.slice(start, start+PAGE_SIZE);

  document.getElementById("count").textContent =
    total ? `共 ${total} 条，显示第 ${start+1}–${Math.min(start+PAGE_SIZE,total)} 条` : "";

  if(!total){
    list.innerHTML='<div class="msg">该条件下暂无水利相关公告。可放宽时间窗口，或清空搜索词与筛选。</div>';
    document.getElementById("pager").innerHTML=""; return;
  }
  list.innerHTML = slice.map(x=>{
    const sc = stageClass(x.stage);
    const kw = (x.keywords||[]).slice(0,3).map(k=>`<span class="kw">${esc(k)}</span>`).join("");
    return `<div class="row s-${sc}" onclick="window.open('${x.url}','_blank')">
      <div class="date tnum">${esc(x.date)}</div>
      <div class="body">
        <div class="title">${esc(x.title)}</div>
        <div class="meta">
          <span class="tag region">${esc(x.region)}</span>
          <span class="tag stage stage-${sc}">${esc(x.stage)}</span>
          ${kw}
        </div>
      </div>
      <div class="go">›</div>
    </div>`;
  }).join("");
  renderPager(pages);
}

function renderPager(pages){
  const p=document.getElementById("pager");
  if(pages<=1){p.innerHTML="";return;}
  let h=`<button ${page===1?"disabled":""} onclick="go(${page-1})">‹</button>`;
  const win=2;
  for(let i=1;i<=pages;i++){
    if(i===1||i===pages||(i>=page-win&&i<=page+win)){
      h+=`<button class="${i===page?"on":""}" onclick="go(${i})">${i}</button>`;
    }else if(i===page-win-1||i===page+win+1){ h+=`<span style="color:var(--muted)">…</span>`; }
  }
  h+=`<button ${page===pages?"disabled":""} onclick="go(${page+1})">›</button>`;
  p.innerHTML=h;
}
function go(n){page=n;render();window.scrollTo({top:0,behavior:"smooth"});}

async function load(force){
  const days=document.getElementById("days").value;
  const btn=document.getElementById("refresh"); btn.disabled=true;
  document.getElementById("list").innerHTML='<div class="msg"><span class="spin"></span></div>';
  try{
    const r=await fetch(`/api/projects?days=${days}${force?"&refresh=1":""}`);
    const j=await r.json();
    if(!j.ok) throw new Error(j.error||"接口返回错误");
    ALL=j.items; page=1;
    document.getElementById("hcount").textContent=j.count;
    document.getElementById("hupd").textContent=(j.updated||"").slice(5,16);
    buildChips(); apply();
  }catch(e){
    document.getElementById("list").innerHTML=
      `<div class="msg err">抓取失败：${esc(e.message)}<br><br>请确认网络能访问 ggzy.luan.gov.cn，稍后点右上角刷新重试。</div>`;
    document.getElementById("count").textContent="";
    document.getElementById("pager").innerHTML="";
  }finally{ btn.disabled=false; }
}
function exportCsv(){
  const days=document.getElementById("days").value;
  window.open(`/api/export.csv?days=${days}`,"_blank");
}
load(false);
</script>
</body>
</html>"""


if __name__ == "__main__":
    print("六安水利招采监测已启动 ->  http://127.0.0.1:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
