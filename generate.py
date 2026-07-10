# -*- coding: utf-8 -*-
"""
生成静态页：抓取六安水利招采数据，套用 template.html，输出 docs/index.html。

- 复用 app.py 里的抓取/清洗/合并/链接逻辑（单一数据源，避免两份代码走样）。
- 抓取成功才覆盖 docs/index.html；失败则原样保留上一次的好页面并以非零码退出，
  这样自动构建偶尔失败也不会把网站刷成空白。

本地手动运行：  python generate.py
（GitHub Actions 会按计划自动运行它。）
"""
import sys
import json
import datetime
import pathlib

import app   # 复用数据逻辑；导入不会启动服务（服务只在 __main__ 下运行）

DAYS = 120           # 抓取最近多少天的数据
ROOT = pathlib.Path(__file__).parent
TEMPLATE = ROOT / "template.html"
OUT = ROOT / "docs" / "index.html"


def main():
    try:
        records = app.fetch_raw(days=DAYS)
        data = app.normalize(records)
    except Exception as e:
        print("[generate] 抓取失败，保留上一次页面：%s" % e, file=sys.stderr)
        return 1
    if not data:
        print("[generate] 抓取到 0 条，疑似被限流，保留上一次页面。", file=sys.stderr)
        return 1

    cap = (datetime.datetime.now(datetime.timezone.utc)
           + datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")
    data_js = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")

    html = TEMPLATE.read_text(encoding="utf-8")
    html = html.replace("__DATA__", data_js).replace("__CAP__", cap)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html, encoding="utf-8")
    print("[generate] 已生成 %s：%d 个项目，数据截至 %s（北京时间）"
          % (OUT, len(data), cap))
    return 0


if __name__ == "__main__":
    sys.exit(main())
