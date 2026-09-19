# -*- coding: utf-8 -*-
"""端到端验证：起真实 App（GUI）+ 真实监控循环，只替换截图与 OCR 引擎，
然后通过 HTTP 接口验证记录是否按预期产生。

安全约定：
  - 记录文件指向临时文件，不碰用户的 屏幕文字记录.txt
  - 配置文件指向临时文件，不碰用户的 config.json
  - 关闭文字驱动器，不会给 ESP32 发任何指令

用法（项目根目录）：
    .venv\\Scripts\\python.exe tools\\api_e2e.py
"""
import json
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tkinter as tk          # noqa: E402
from PIL import Image         # noqa: E402

import main as M              # noqa: E402

PORT = M.API_DEFAULT_PORT
BASE = "http://127.0.0.1:%d" % PORT

# 每轮“屏幕上”应该出现的文字（模拟评论区逐条弹字）
FRAMES = [
    ["欢迎来到直播间"],
    ["欢迎来到直播间", "主播好帅"],
    ["欢迎来到直播间", "主播好帅"],            # 第 3 轮 → “主播好帅”确认落盘
    ["主播好帅", "谢谢老板的礼物"],
    ["主播好帅", "谢谢老板的礼物"],            # 第 5 轮 → “谢谢老板的礼物”确认落盘
]
# 注意：第一帧的所有文字都会被当作“新增”（程序既有行为——prev_counts 初始为空），
# 所以“欢迎来到直播间”也会成为第 1 条记录。调用方需要知道这一点。
EXPECTED = ["欢迎来到直播间", "主播好帅", "谢谢老板的礼物"]

_state = {"i": 0, "n": 0}
PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  [OK]   %s" % name)
    else:
        FAIL.append((name, detail))
        print("  [FAIL] %s   %s" % (name, detail))


def fake_capture(x, y, w, h, sct=None):
    """每次返回颜色不同的图，模拟“屏幕一直在变”，让监控循环真的跑起来。"""
    _state["n"] += 1
    return Image.new("RGB", (max(int(w), 1), max(int(h), 1)),
                     (_state["n"] % 200 + 30, 40, 60))


def fake_ocr(ocr, img):
    """按脚本返回 OCR 文本；轮数用完后停在最后一帧。"""
    i = min(_state["i"], len(FRAMES) - 1)
    _state["i"] += 1
    return "\n".join(FRAMES[i])


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def spin(root, seconds):
    """驱动 Tk 事件循环，让界面不卡死。"""
    t0 = time.time()
    while time.time() - t0 < seconds:
        root.update()
        time.sleep(0.02)


def main():
    tmp_dir = Path(tempfile.gettempdir())
    tmp_log = tmp_dir / "_dupingmu_e2e.txt"
    tmp_cfg = tmp_dir / "_dupingmu_e2e_config.json"
    for p in (tmp_log, tmp_cfg):
        if p.exists():
            p.unlink()

    # 隔离副作用：不碰用户的数据文件和配置
    M.CONFIG_FILE = tmp_cfg
    M.capture_region = fake_capture
    M.ocr_image = fake_ocr

    root = tk.Tk()
    app = M.ScreenTextMonitorApp(root)

    app.log_var.set(str(tmp_log))
    app.interval_var.set(0.5)
    app.confirm_polls_var.set(2)
    app.driver_enabled_var.set(False)     # 不发 ESP32 指令
    app.vision_enabled_var.set(False)
    app.region = (0, 0, 200, 60)          # 截图已被替换，坐标只用于凑参数

    print("接口地址：%s\n" % app.api_server.base_url)

    print("-- 1 接口随程序自动启动 --")
    check("接口自动启动", app.api_server.running is True)
    check("监听端口 = %d" % PORT, app.api_server.port == PORT)
    h0 = get("/health")
    check("/health 可访问", h0["data"]["service"] == "dupingmu")

    print("-- 2 跑真实监控循环（OCR 模型真加载） --")
    app.start_monitor()
    check("监控已启动", app.running is True)

    deadline = time.time() + 30
    while time.time() < deadline:
        root.update()
        time.sleep(0.02)
        if len(app.api_state.records) >= len(EXPECTED):
            break
    spin(root, 1.0)                       # 再跑一会儿让状态稳定

    print("-- 3 接口读到真实监控结果 --")
    h = get("/health")
    check("/health monitoring=True", h["data"]["monitoring"] is True)
    check("/health ocr_ready=True", h["data"]["ocr_ready"] is True)
    check("/health region 从配置读回",
          h["data"]["region"] == [0, 0, 200, 60], h["data"]["region"])
    check("/health interval=0.5", h["data"]["interval"] == 0.5, h["data"]["interval"])
    check("/health confirm_polls=2", h["data"]["confirm_polls"] == 2)

    cur = get("/current")
    check("/current 读到模拟屏幕文字", cur["data"]["lines"] == FRAMES[-1],
          cur["data"]["lines"])
    check("/current frame_age_ms 是数字",
          isinstance(cur["data"]["frame_age_ms"], int))

    rec = get("/records?since=0")
    got = [r["text"] for r in rec["data"]["records"]]
    check("按出现顺序产生记录 %s" % EXPECTED, got == EXPECTED, got)
    check("seq 从 1 连续递增",
          [r["seq"] for r in rec["data"]["records"]] == [1, 2, 3],
          [r["seq"] for r in rec["data"]["records"]])
    check("首帧文字也算新增（既有行为，调用方需知晓）",
          rec["data"]["records"][0]["text"] == "欢迎来到直播间",
          rec["data"]["records"][0]["text"])

    if len(rec["data"]["records"]) >= 2:
        r1 = rec["data"]["records"][0]
        check("记录带 first_seen_ms", isinstance(r1["first_seen_ms"], int))
        check("first_seen_ms <= ts_ms（确认轮数带来延迟）",
              r1["first_seen_ms"] <= r1["ts_ms"], (r1["first_seen_ms"], r1["ts_ms"]))
        check("记录带 screen 整屏上下文", "\n" in r1["screen"], repr(r1["screen"]))
        check("记录 ts 是 19 位时间串", len(r1["ts"]) == 19, r1["ts"])

    print("-- 4 增量语义 --")
    last = rec["data"]["last_seq"]
    rec2 = get("/records?since=%d" % last)
    check("带上 last_seq 后无新数据", rec2["data"]["count"] == 0, rec2["data"])
    latest = get("/latest?limit=1")
    check("/latest 返回最近一句",
          latest["data"]["records"][0]["text"] == EXPECTED[-1])
    txt = urllib.request.urlopen(BASE + "/text?mode=latest", timeout=10).read().decode()
    check("/text 纯文本模式", txt == EXPECTED[-1], repr(txt))

    rec_r = get("/recent")
    check("/recent 返回全部 %d 条" % len(EXPECTED),
          rec_r["data"]["count"] == len(EXPECTED), rec_r["data"]["count"])
    check("/recent 内容与 /records 一致",
          [x["text"] for x in rec_r["data"]["records"]] == EXPECTED,
          [x["text"] for x in rec_r["data"]["records"]])
    check("/recent truncated=False", rec_r["data"]["truncated"] is False)
    check("/recent minutes=1 覆盖刚产生的记录",
          get("/recent?minutes=1")["data"]["count"] == len(EXPECTED))
    check("/recent format=text 可直读",
          urllib.request.urlopen(BASE + "/recent?format=text", timeout=10)
          .read().decode().count("\n") == len(EXPECTED) - 1)
    check("/status memory 统计到记录",
          get("/status")["data"]["memory"]["in_memory"] == len(EXPECTED),
          get("/status")["data"]["memory"])

    print("-- 5 txt 通道与接口并存 --")
    body = tmp_log.read_text(encoding="utf-8") if tmp_log.exists() else ""
    check("txt 记录文件同时写入", "主播好帅" in body and "谢谢老板的礼物" in body,
          repr(body[:100]))
    check("txt 带时间戳", body.startswith("["), repr(body[:40]))

    print("-- 6 停止监控 --")
    boot1 = h["boot_id"]
    app.stop_monitor()
    t1 = time.time()
    while time.time() - t1 < 4 and app.running:
        root.update()
        time.sleep(0.02)
    spin(root, 0.5)
    check("停止监控生效", app.running is False)

    h2 = get("/health")
    check("停止后 monitoring=False", h2["data"]["monitoring"] is False)
    check("停止后 boot_id 不变（同一进程）", h2["boot_id"] == boot1)
    check("停止后历史记录仍可查",
          get("/records?since=0")["data"]["count"] == len(EXPECTED))

    print("-- 7 界面操作：换端口 / 关接口 --")
    app.api_port_var.set("18073")
    app._apply_api_settings(True)
    spin(root, 0.3)
    check("换端口后服务重启到新端口", app.api_server.port == 18073,
          app.api_server.port)
    ok_new = False
    try:
        with urllib.request.urlopen("http://127.0.0.1:18073/health", timeout=5) as r:
            ok_new = json.loads(r.read().decode("utf-8"))["data"]["service"] == "dupingmu"
    except Exception as e:
        print("    (新端口请求失败: %s)" % e)
    check("新端口接口可访问", ok_new)
    old_gone = False
    try:
        urllib.request.urlopen("http://127.0.0.1:18072/health", timeout=3)
    except Exception:
        old_gone = True
    check("旧端口已释放", old_gone)

    app.api_enabled_var.set(False)
    app._apply_api_settings(False)
    check("取消勾选后接口关闭", app.api_server.running is False)

    print("-- 8 关窗清理 --")
    app._on_close()
    check("关窗后接口已关闭", app.api_server.running is False)

    for p in (tmp_log, tmp_cfg):
        if p.exists():
            p.unlink()

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        for name, detail in FAIL:
            print("  FAIL: %s   %s" % (name, detail))
        return 1
    print("端到端验证通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
