# -*- coding: utf-8 -*-
"""对外接口自检：不启动 GUI，用假 app 起接口服务，逐个打接口验证。

用法（项目根目录）：
    .venv\\Scripts\\python.exe tools\\api_selftest.py

覆盖：全部 10 个接口、参数校验、错误码、长轮询（超时/唤醒/不阻塞）、
令牌鉴权、端口占用、索引页、内存裁剪。
"""
import json
import queue
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import main as M  # noqa: E402

PORT = 18099
TOKEN_PORT = 18098
BASE = "http://127.0.0.1:%d" % PORT


class FakeApp:
    """鸭子类型的假 App，只提供接口层会读的字段。"""

    def __init__(self):
        self.api_state = M.ApiState()
        self.running = True
        self.region = (144, 953, 609, 33)
        self.ocr = object()
        self.rules = [{"text": "火箭", "kw": "", "on": "008on",
                       "off": "008off", "delay": 20}]
        self._active_interval = 1.0
        self._active_confirm_polls = 2
        self._active_log = r"D:\dupingmu\屏幕文字记录.txt"
        self._active_driver_enabled = True
        self._active_vision_enabled = False
        self._active_driver_cfg = {"cmd_prefix": "001", "cmd_suffix": ""}
        self._send_queue = queue.Queue()


PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  [OK]   %s" % name)
    else:
        FAIL.append((name, detail))
        print("  [FAIL] %s   %s" % (name, detail))


def call(path, method="GET", body=None, token=None, timeout=40):
    """请求 JSON 接口，返回 (状态码, 解析后的 dict)。"""
    headers = {}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["X-Auth-Token"] = token
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw}


def raw(path, timeout=40):
    """请求纯文本/HTML 接口，返回 (状态码, 文本, Content-Type)。"""
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8"), r.headers.get("Content-Type", "")


def main():
    global BASE
    app = FakeApp()
    srv = M.ApiServer(app)
    url = srv.start("127.0.0.1", PORT, "", False)
    print("接口服务已启动：%s\n" % url)
    boot = app.api_state.boot_id

    print("-- 1 基础信息 --")
    st, r = call("/health")
    check("/health 200", st == 200, st)
    check("/health service=dupingmu", r["data"]["service"] == "dupingmu")
    check("/health aliases 含 dupingmu", "dupingmu" in r["data"]["aliases"])
    check("/health boot_id 一致", r["boot_id"] == boot)
    check("/health monitoring=True", r["data"]["monitoring"] is True)
    check("/health last_seq=0", r["data"]["last_seq"] == 0)
    check("/health 外壳含 server", r["server"] == "dupingmu")

    st, r = call("/status")
    check("/status 200", st == 200)
    check("/status interval", r["data"]["interval"] == 1.0)
    check("/status region", r["data"]["region"] == [144, 953, 609, 33])
    check("/status confirm_polls", r["data"]["confirm_polls"] == 2)

    print("-- 2 空数据（没新数据不是错误） --")
    st, r = call("/current")
    check("/current 空 text", r["data"]["text"] == "")
    check("/current frame_ts_ms=0", r["data"]["frame_ts_ms"] == 0)
    st, r = call("/records")
    check("/records 不带 since 不吐历史", r["data"]["records"] == [])
    check("/records 带 last_seq", r["data"]["last_seq"] == 0)
    st, r = call("/latest")
    check("/latest 无数据为空数组", r["data"]["records"] == [])
    st, r = call("/pending")
    check("/pending 空", r["data"]["count"] == 0)

    print("-- 3 帧与候选 --")
    app.api_state.set_frame("主播好帅\n欢迎来到直播间",
                            ["主播好帅", "欢迎来到直播间"], True)
    st, r = call("/current")
    check("/current 读到整屏", r["data"]["lines"] == ["主播好帅", "欢迎来到直播间"])
    check("/current frame_age_ms 有值", isinstance(r["data"]["frame_age_ms"], int))
    check("/current changed=True", r["data"]["changed"] is True)

    app.api_state.set_pending([{"text": "谢谢老板", "seen_count": 1, "need": 2,
                                "first_seen_ms": int(time.time() * 1000)}])
    st, r = call("/pending")
    check("/pending 读到候选",
          r["data"]["count"] == 1 and r["data"]["pending"][0]["text"] == "谢谢老板")
    check("/pending 带 age_ms", "age_ms" in r["data"]["pending"][0])

    print("-- 4 记录与增量拉取 --")
    t0 = int(time.time() * 1000) - 500
    app.api_state.push_record("第一条", screen="第一条", first_seen_ms=t0)
    app.api_state.push_record("第二条", screen="第一条\n第二条")
    app.api_state.push_record("第三条")

    st, r = call("/records?since=0")
    check("/records?since=0 拿到 3 条", r["data"]["count"] == 3, r["data"]["count"])
    check("/records seq 升序", [x["seq"] for x in r["data"]["records"]] == [1, 2, 3])
    check("/records last_seq=3", r["data"]["last_seq"] == 3)
    check("/records first_seen_ms 生效", r["data"]["records"][0]["first_seen_ms"] == t0)
    check("/records screen 字段带整屏",
          r["data"]["records"][1]["screen"] == "第一条\n第二条")
    check("/records ts 格式 19 位", len(r["data"]["records"][0]["ts"]) == 19)

    st, r = call("/records?since=3")
    check("/records 增量：无新数据返回空", r["data"]["records"] == [])
    st, r = call("/records?since=1")
    check("/records 增量：拿到 2 条", r["data"]["count"] == 2)
    st, r = call("/records?since=0&limit=2")
    check("/records limit 生效且 has_more",
          r["data"]["count"] == 2 and r["data"]["has_more"] is True, r["data"])

    st, r = call("/latest?limit=1")
    check("/latest 最近一句", r["data"]["records"][0]["text"] == "第三条")
    st, r = call("/latest?limit=2")
    check("/latest 最近两句按 seq 升序",
          [x["text"] for x in r["data"]["records"]] == ["第二条", "第三条"])

    print("-- 5 纯文本接口 --")
    st, txt, ct = raw("/text?mode=latest")
    check("/text latest", txt == "第三条", repr(txt))
    st, txt, ct = raw("/text?mode=all")
    check("/text all 三行", txt.count("\n") == 2, repr(txt))
    st, txt, ct = raw("/text?mode=current")
    check("/text current 整屏", "欢迎来到直播间" in txt)
    check("/text Content-Type", ct.startswith("text/plain"), ct)
    st, txt, ct = raw("/text?mode=latest&format=text")
    check("/text 无数据时返回空串不报错", isinstance(txt, str))

    print("-- 6 参数校验 --")
    st, r = call("/records?since=abc")
    check("since=abc -> 400 bad_param",
          st == 400 and r["error"]["code"] == "bad_param", (st, r.get("error")))
    st, r = call("/records?limit=999")
    check("limit 超上限 -> 400", st == 400)
    st, r = call("/records?limit=0")
    check("limit=0 -> 400", st == 400)
    st, r = call("/wait?timeout=5")
    check("/wait 缺 since -> 400", st == 400)
    st, r = call("/wait?since=3&timeout=99")
    check("/wait timeout 超上限 -> 400", st == 400)

    print("-- 7 错误路径 --")
    st, r = call("/nope")
    check("未知路径 -> 404", st == 404 and r["error"]["code"] == "not_found")
    st, r = call("/records", method="POST", body={})
    check("POST /records -> 405", st == 405)
    st, r = call("/command")
    check("GET /command -> 405", st == 405)
    st, r = call("/command", method="POST", body={"code": "001on"})
    check("/command 未开启 -> 403",
          st == 403 and r["error"]["code"] == "forbidden", (st, r.get("error")))

    print("-- 8 长轮询 --")
    t0 = time.time()
    st, r = call("/wait?since=3&timeout=1")
    el = time.time() - t0
    check("/wait 超时返回空", r["data"]["timed_out"] is True and
          r["data"]["records"] == [], r["data"])
    check("/wait 超时耗时约 1 秒", 0.8 <= el <= 2.5, "%.2fs" % el)

    threading.Thread(target=lambda: (time.sleep(1.0),
                                     app.api_state.push_record("第四条")),
                     daemon=True).start()
    t0 = time.time()
    st, r = call("/wait?since=3&timeout=10")
    el = time.time() - t0
    check("/wait 被新记录唤醒",
          r["data"]["count"] == 1 and r["data"]["records"][0]["text"] == "第四条",
          r["data"])
    check("/wait 唤醒及时（<3s）", el < 3.0, "%.2fs" % el)
    check("/wait timed_out=False", r["data"]["timed_out"] is False)

    threading.Thread(target=lambda: call("/wait?since=4&timeout=3"), daemon=True).start()
    time.sleep(0.2)
    t0 = time.time()
    st, r = call("/health")
    el = time.time() - t0
    check("长轮询期间 /health 不被阻塞", st == 200 and el < 0.6, "%.3fs" % el)

    print("-- 9 最近 N 分钟（/recent） --")
    st, r = call("/recent")
    check("/recent 默认 30 分钟", r["data"]["minutes"] == 30, r["data"].get("minutes"))
    check("/recent 返回全部 4 条", r["data"]["count"] == 4, r["data"]["count"])
    check("/recent truncated=False", r["data"]["truncated"] is False)
    check("/recent 带时间窗口字段",
          isinstance(r["data"]["from_ms"], int) and isinstance(r["data"]["to_ms"], int))
    st, r = call("/recent?limit=2")
    check("/recent limit 取最新两条",
          [x["text"] for x in r["data"]["records"]] == ["第三条", "第四条"],
          [x["text"] for x in r["data"]["records"]])
    check("/recent limit 截断标 truncated", r["data"]["truncated"] is True)
    check("/recent 截断时给出 note", "note" in r["data"], r["data"].get("note"))
    st, r = call("/recent?minutes=1")
    check("/recent minutes=1 拿到刚产生的记录", r["data"]["count"] == 4,
          r["data"]["count"])
    st, r = call("/recent?minutes=0")
    check("/recent minutes=0 -> 400", st == 400)
    st, r = call("/recent?minutes=2000")
    check("/recent minutes 超上限 -> 400", st == 400)
    st, r = call("/recent?limit=0")
    check("/recent limit=0 -> 400", st == 400)
    st, txt, ct = raw("/recent?format=text")
    check("/recent format=text 纯文本", txt.count("\n") == 3, repr(txt))
    st, txt, ct = raw("/text?mode=recent")
    check("/text mode=recent", txt.count("\n") == 3, repr(txt))

    st, r = call("/status")
    check("/status 含 memory 统计块", isinstance(r["data"].get("memory"), dict))
    check("/status memory.in_memory", r["data"]["memory"]["in_memory"] == 4,
          r["data"]["memory"])
    check("/status recent_30min_count", r["data"]["recent_30min_count"] == 4,
          r["data"]["recent_30min_count"])
    check("/status memory.capacity 默认 10000",
          r["data"]["memory"]["capacity"] == 10000, r["data"]["memory"]["capacity"])

    print("-- 10 规则与索引页 --")
    st, r = call("/rules")
    check("/rules 返回规则",
          r["data"]["count"] == 1 and r["data"]["rules"][0]["on"] == "008on")

    st, txt, ct = raw("/")
    check("/ 索引页返回 HTML", st == 200 and ct.startswith("text/html"), ct)
    check("/ 索引页含接口列表", "/records" in txt and "dupingmu" in txt)
    check("/ 索引页含 boot_id", boot in txt)

    check("boot_id 是 8 位十六进制",
          len(boot) == 8 and all(c in "0123456789abcdef" for c in boot), boot)

    print("-- 11 /command 开启后可用 --")
    srv.stop()
    srv2 = M.ApiServer(app)
    srv2.start("127.0.0.1", PORT, "", True)
    st, r = call("/command", method="POST", body={"code": "001on", "label": "自检"})
    check("/command 开启后 200", st == 200 and r["data"]["queued"] is True, (st, r))
    check("/command 自动拼前缀", r["data"]["sent"] == "001001on", r["data"])
    try:
        q = app._send_queue.get_nowait()
        check("/command 进发送队列", q[0] == "001001on" and q[2] == "自检", q)
    except queue.Empty:
        check("/command 进发送队列", False, "队列为空")
    st, r = call("/command", method="POST", body={"code": ""})
    check("/command 空 code -> 400", st == 400)
    st, r = call("/command", method="POST", body={"code": "001on", "label": "x"},
                 timeout=10)
    srv2.stop()

    print("-- 12 令牌鉴权 --")
    srv3 = M.ApiServer(app)
    srv3.start("127.0.0.1", TOKEN_PORT, "secret123", False)
    old_base = BASE
    BASE = "http://127.0.0.1:%d" % TOKEN_PORT
    st, r = call("/health")
    check("令牌缺失 -> 401",
          st == 401 and r["error"]["code"] == "unauthorized", (st, r.get("error")))
    st, r = call("/health", token="wrong")
    check("令牌错误 -> 401", st == 401)
    st, r = call("/health", token="secret123")
    check("令牌正确（Header）-> 200", st == 200)
    st, r = call("/health?token=secret123")
    check("令牌正确（URL 参数）-> 200", st == 200)
    srv3.stop()
    BASE = old_base

    print("-- 13 端口占用与内存裁剪 --")
    srv4 = M.ApiServer(app)
    srv4.start("127.0.0.1", PORT, "", False)
    srv5 = M.ApiServer(app)
    try:
        srv5.start("127.0.0.1", PORT, "", False)
        check("端口占用抛 OSError", False, "没有抛异常")
    except OSError:
        check("端口占用抛 OSError", True)
    srv4.stop()
    srv5.stop()

    # 关闭后立刻重新绑定同一端口必须成功（关掉 SO_REUSEADDR 后不能影响重启）
    srv7 = M.ApiServer(app)
    try:
        srv7.start("127.0.0.1", PORT, "", False)
        check("关闭后立刻重启同端口成功", True)
    except OSError as e:
        check("关闭后立刻重启同端口成功", False, str(e))
    srv7.stop()

    app.api_state.resize(5)
    check("resize 有下限保护（最小 10）", app.api_state.records.maxlen == 10,
          app.api_state.records.maxlen)
    for i in range(20):
        app.api_state.push_record("压测%d" % i)
    check("超出容量只留 maxlen 条", len(app.api_state.records) == 10,
          len(app.api_state.records))
    app.api_state.resize(30)
    check("resize 调大生效", app.api_state.records.maxlen == 30)

    print("-- 14 内存淘汰与 truncated 判定 --")
    # 时间淘汰：超过保留时间的记录会被自动清掉
    with app.api_state.lock:
        app.api_state.records.clear()
    app.api_state.push_record("最老的一条")
    with app.api_state.lock:
        app.api_state.records[0]["ts_ms"] = int(time.time() * 1000) - 2 * 3600 * 1000
    app.api_state.push_record("新来的一条")
    left = [r["text"] for r in app.api_state.records]
    check("超过保留时间(60分钟)的记录被清理", "最老的一条" not in left, left)
    check("新记录保留", "新来的一条" in left, left)

    # truncated 判定：队列已满 + 窗口起点被淘汰
    st2 = M.ApiState(max_records=12, max_age_minutes=60)
    for i in range(15):
        st2.push_record("r%02d" % i)
    recs, trunc, oldest, cutoff = st2.recent(30, 100)
    check("队列满且窗口起点被淘汰 -> truncated=True", trunc is True, trunc)
    check("只保留最新 12 条",
          [r["text"] for r in recs] == ["r%02d" % i for i in range(3, 15)],
          [r["text"] for r in recs])
    recs, trunc, _o, _c = st2.recent(30, 3)
    check("limit 生效取最新 3 条",
          [r["text"] for r in recs] == ["r12", "r13", "r14"],
          [r["text"] for r in recs])
    check("limit 截断也标 truncated", trunc is True)

    st3 = M.ApiState()
    for i in range(5):
        st3.push_record("x%d" % i)
    recs, trunc, _o, _c = st3.recent(30, 100)
    check("数据未满时 truncated=False", trunc is False, trunc)
    check("未满时返回全部 5 条", len(recs) == 5, len(recs))
    s3 = st3.stats()
    check("stats in_memory", s3["in_memory"] == 5, s3)
    check("stats recent_30min", s3["recent_30min"] == 5, s3)
    check("stats span_seconds 是数字", isinstance(s3["span_seconds"], float), s3)
    check("stats 默认 max_age_minutes=60", s3["max_age_minutes"] == 60, s3)
    check("stats 默认 capacity=10000", s3["capacity"] == 10000, s3)

    print("-- 15 半小时数据量实测 --")
    bulk = M.ApiState()
    # 用 28 分钟跨度而不是 30 分钟：/recent 的窗口起点是"调用时刻往前推 30 分钟"，
    # 卡在边界上的话，构造数据这几毫秒就会让最早一条滑出窗口。
    span_ms = 28 * 60 * 1000
    base_ms = int(time.time() * 1000) - span_ms
    step = span_ms // 2000
    for i in range(2000):
        bulk.push_record("弹幕第%d条" % i)
        with bulk.lock:
            bulk.records[-1]["ts_ms"] = base_ms + i * step
    t0 = time.time()
    recs, trunc, _o, _c = bulk.recent(30, 10000)
    el = time.time() - t0
    check("半小时 2000 条全部返回", len(recs) == 2000, len(recs))
    check("半小时数据不标 truncated", trunc is False, trunc)
    check("取半小时数据耗时 < 50ms", el < 0.05, "%.1fms" % (el * 1000))
    check("按时间升序返回",
          recs[0]["text"] == "弹幕第0条" and recs[-1]["text"] == "弹幕第1999条",
          (recs[0]["text"], recs[-1]["text"]))
    recs_1m, _t, _o, _c = bulk.recent(1, 10000)
    check("minutes=1 只取最后一分钟", 0 < len(recs_1m) < 2000, len(recs_1m))

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        for name, detail in FAIL:
            print("  FAIL: %s   %s" % (name, detail))
        return 1
    print("对外接口自检全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
