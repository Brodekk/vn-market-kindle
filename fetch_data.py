#!/usr/bin/env python3
"""Lấy số liệu thị trường chứng khoán Việt Nam và dựng trang HTML tĩnh cho Kindle (e-ink).

Chỉ dùng thư viện chuẩn. Mỗi chỉ số thử nhiều nguồn công khai (không chính thức),
nguồn nào lỗi thì bỏ qua và ghi lại vào data.json -> sources/errors.
"""
import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=7))
UA = {"User-Agent": "Mozilla/5.0 (compatible; vn-market-kindle)"}

# (mã chính, tên hiển thị, các mã thay thế trên nguồn intraday)
INDICES = [
    ("VNINDEX", "VN-Index", ["VNINDEX"]),
    ("VN30", "VN30", ["VN30"]),
    ("HNXINDEX", "HNX-Index", ["HNXINDEX", "HNX"]),
    ("UPCOMINDEX", "UPCoM-Index", ["UPCOMINDEX", "HNXUPCOMINDEX", "UPCOM"]),
]

errors = []


def get_json(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def log_err(where, exc):
    msg = "%s: %s" % (where, exc)
    errors.append(msg)
    print("WARN " + msg, file=sys.stderr)


def daily_vndirect(code):
    url = "https://finfo-api.vndirect.com.vn/v4/stock_prices?" + urllib.parse.urlencode(
        {"sort": "date", "q": "code:" + code, "size": "30", "page": "1"}
    )
    rows = get_json(url).get("data", [])
    out = []
    for r in rows:
        close = r.get("close")
        if close is None:
            continue
        out.append({"date": str(r.get("date"))[:10], "close": float(close),
                    "value": r.get("nmValue")})
    out.sort(key=lambda x: x["date"])
    return out


def daily_tcbs(code):
    to = int(datetime.now(TZ).timestamp())
    url = ("https://apipubaws.tcbs.com.vn/stock-insight/v1/stock/bars-long-term?"
           + urllib.parse.urlencode({"ticker": code, "type": "index", "resolution": "D",
                                     "countBack": "30", "to": str(to)}))
    rows = get_json(url).get("data", [])
    out = [{"date": str(r["tradingDate"])[:10], "close": float(r["close"]), "value": None}
           for r in rows if r.get("close") is not None]
    out.sort(key=lambda x: x["date"])
    return out


def history_vps(symbol, resolution, start_ts, end_ts):
    url = ("https://histdatafeed.vps.com.vn/tradingview/history?"
           + urllib.parse.urlencode({"symbol": symbol, "resolution": resolution,
                                     "from": str(start_ts), "to": str(end_ts)}))
    d = get_json(url)
    if d.get("s") != "ok":
        raise ValueError("status %s" % d.get("s"))
    return list(zip(d["t"], d["c"]))


def daily_vps(symbols):
    now = datetime.now(TZ)
    start = int((now - timedelta(days=60)).timestamp())
    for sym in symbols:
        try:
            pts = history_vps(sym, "D", start, int(now.timestamp()) + 86400)
            out = [{"date": datetime.fromtimestamp(t, TZ).strftime("%Y-%m-%d"),
                    "close": float(c), "value": None} for t, c in pts][-30:]
            if out:
                return out
        except Exception as exc:  # noqa: BLE001
            log_err("daily_vps(%s)" % sym, exc)
    return []


def intraday_vps(symbols):
    now = datetime.now(TZ)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for sym in symbols:
        try:
            pts = history_vps(sym, "5", int(midnight.timestamp()), int(now.timestamp()) + 3600)
            vals = [float(c) for t, c in pts if t >= midnight.timestamp()]
            if vals:
                return vals
        except Exception as exc:  # noqa: BLE001
            log_err("intraday_vps(%s)" % sym, exc)
    return []


def build_index(code, name, vps_symbols):
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    daily, source = [], None
    for label, fn in (("vndirect", lambda: daily_vndirect(code)),
                      ("tcbs", lambda: daily_tcbs(code)),
                      ("vps", lambda: daily_vps(vps_symbols))):
        try:
            daily = fn()
        except Exception as exc:  # noqa: BLE001
            log_err("daily_%s(%s)" % (label, code), exc)
            daily = []
        if daily:
            source = label
            break

    intraday = intraday_vps(vps_symbols)
    item = {"code": code, "name": name, "source": source, "last": None, "ref": None,
            "change": None, "pct": None, "value_bn": None,
            "spark": [], "spark_kind": None}
    if not daily and not intraday:
        return item

    if intraday and daily:
        item["last"] = intraday[-1]
        item["ref"] = daily[-2]["close"] if (daily[-1]["date"] == today and len(daily) >= 2) \
            else daily[-1]["close"]
        item["spark"], item["spark_kind"] = intraday, "Trong phiên"
    elif daily:
        item["last"] = daily[-1]["close"]
        if len(daily) >= 2:
            item["ref"] = daily[-2]["close"]
        item["spark"] = [d["close"] for d in daily]
        item["spark_kind"] = "30 phiên"
    elif intraday:
        item["last"] = intraday[-1]
        item["spark"], item["spark_kind"] = intraday, "Trong phiên"

    if item["last"] is not None and item["ref"]:
        item["change"] = item["last"] - item["ref"]
        item["pct"] = item["change"] / item["ref"] * 100.0
    if daily and daily[-1].get("value"):
        try:
            item["value_bn"] = float(daily[-1]["value"]) / 1e9
        except (TypeError, ValueError):
            pass
    if len(item["spark"]) < 2 and daily:  # không có đường intraday thì dùng 30 phiên
        item["spark"] = [d["close"] for d in daily]
        item["spark_kind"] = "30 phiên"
    return item


def fnum(x, d=2):
    if x is None:
        return "—"
    s = "{:,.{d}f}".format(x, d=d)
    return s.replace(",", "§").replace(".", ",").replace("§", ".")


def spark_svg(vals, w=320, h=70):
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    step = w / float(len(vals) - 1)
    pts = " ".join("%.1f,%.1f" % (i * step, h - 4 - (v - lo) / span * (h - 8))
                   for i, v in enumerate(vals))
    return ('<svg width="%d" height="%d" viewBox="0 0 %d %d" xmlns="http://www.w3.org/2000/svg">'
            '<polyline fill="none" stroke="#000" stroke-width="3" points="%s"/></svg>'
            % (w, h, w, h, pts))


def render(items, now):
    cards = []
    for it in items:
        if it["change"] is None:
            arrow, chg = "", "—"
        else:
            arrow = "▲" if it["change"] > 0 else ("▼" if it["change"] < 0 else "■")
            chg = "%s %s (%s%%)" % (arrow, fnum(abs(it["change"])), fnum(it["pct"]))
        gt = ("GTGD: %s tỷ" % fnum(it["value_bn"], 0)) if it["value_bn"] else ""
        cards.append(
            '<div class="card"><div class="row"><span class="name">%s</span>'
            '<span class="kind">%s</span></div>'
            '<div class="big">%s</div><div class="chg">%s</div>'
            '<div class="spark">%s</div><div class="sub">%s</div></div>'
            % (it["name"], it["spark_kind"] or "", fnum(it["last"]), chg,
               spark_svg(it["spark"]), gt))
    trading = now.weekday() < 5 and 9 <= now.hour < 15
    status = "Đang giao dịch" if trading else "Ngoài giờ giao dịch"
    return """<!DOCTYPE html>
<html lang="vi"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="300">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Thị trường VN</title>
<style>
body{margin:0;padding:14px;background:#fff;color:#000;font-family:Georgia,serif}
h1{font-size:30px;margin:0 0 4px 0;border-bottom:4px solid #000;padding-bottom:6px}
.meta{font-size:20px;margin:6px 0 14px 0}
.card{border:3px solid #000;padding:10px 14px;margin-bottom:14px}
.row{display:flex;justify-content:space-between;align-items:baseline}
.name{font-size:28px;font-weight:bold}.kind{font-size:18px}
.big{font-size:56px;font-weight:bold;line-height:1.1}
.chg{font-size:28px;margin-bottom:4px}.sub{font-size:20px}
.foot{font-size:16px;margin-top:8px}
</style></head><body>
<h1>THỊ TRƯỜNG VIỆT NAM</h1>
<div class="meta">@@STATUS@@ · Cập nhật @@TIME@@</div>
@@CARDS@@
<div class="foot">Dữ liệu công khai, có thể trễ. Không phải khuyến nghị đầu tư.</div>
</body></html>
""".replace("@@STATUS@@", status).replace("@@TIME@@", now.strftime("%H:%M %d/%m/%Y")).replace("@@CARDS@@", "\n".join(cards))


def main():
    now = datetime.now(TZ)
    items = [build_index(code, name, syms) for code, name, syms in INDICES]
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump({"updated": now.isoformat(), "items": items, "errors": errors},
                  f, ensure_ascii=False, indent=1)
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(render(items, now))
    ok = sum(1 for i in items if i["last"] is not None)
    print("OK %d/%d chỉ số có dữ liệu; lỗi nguồn: %d" % (ok, len(items), len(errors)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
