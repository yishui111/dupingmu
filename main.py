# -*- coding: utf-8 -*-
"""
屏幕文字识别监控 + 文字驱动器 + DeepSeek 视觉
=============================================
一、屏幕文字监控
1. 运行后点击“框选区域”，用鼠标拖拽框选屏幕上要监控的一块区域；
2. 程序每隔设定的秒数截取该区域，用离线 OCR（RapidOCR + ONNX Runtime）识别文字，全程不联网；
3. 当区域里的文字发生变化时，自动把新文字（带时间）追加写入记录文件（txt 记事本）。

二、文字驱动器（通过屏幕文字触发指令，控制 ESP32 等设备）
1. 在“文字驱动器”页签里添加规则，或在 指令规则.txt 里手工编辑：
      触发文字|命中指令|关闭指令|延时秒|画面关键词
      例：    打开灯 | 001on | 001off | 5 |
2. 监控时，如果识别到的屏幕文字包含“触发文字”，立即通过接口发送：
      指令前缀 + 命中指令   （默认前缀 001 → 发送 001001on）
   并在这条规则设定的“延时秒”之后发送：
      指令前缀 + 关闭指令   （→ 发送 001001off）
3. 发送接口支持：串口（USB 连接 ESP32）或 网络 TCP（ESP32 做服务器，连同一 WiFi）。

三、DeepSeek 视觉理解（联网，需 API Key）
1. 屏幕文字变化时（或点“识别当前区域并描述”），把截图发给 DeepSeek 视觉模型，
   得到自然语言描述，写入 屏幕画面描述.txt 并在界面显示；
2. 规则里的“画面关键词”如果出现在视觉描述中，同样会触发指令（智能指令匹配）。

两种使用方式：
- 源码运行：  python main.py
- 打包成 exe：双击 build.bat（首次需要联网安装依赖），产物在 dist 目录，
  把整个文件夹复制到任何 Windows 电脑即可直接双击使用。
- 命令行自检：python main.py --selftest   （离线 OCR 是否可用的自检）
"""

import collections
import ctypes
import json
import os
import queue
import random
import socket
import sys
import threading
import time
import traceback
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import logging
from logging.handlers import RotatingFileHandler

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# ---- 兼容打包后与源码运行两种场景，取“程序所在目录” ----
if getattr(sys, "frozen", False):            # 打包成 exe 后
    APP_DIR = Path(sys.executable).resolve().parent
else:                                        # 源码运行时
    APP_DIR = Path(__file__).resolve().parent

CONFIG_FILE = APP_DIR / "config.json"
DEFAULT_LOG_FILE = APP_DIR / "屏幕文字记录.txt"
ERROR_LOG_FILE = APP_DIR / "程序错误日志.txt"
RULES_FILE = APP_DIR / "指令规则.txt"
ACTION_LOG_FILE = APP_DIR / "指令发送记录.txt"
LOG_FILE = APP_DIR / "logs" / "app.log"
DEFAULT_VISION_LOG_FILE = APP_DIR / "屏幕画面描述.txt"

VISION_COOLDOWN_SECONDS = 30   # 视觉自动调用的最小间隔（防高频变化时请求堆积）
DRIVER_CONFIRM_POLLS = 2       # 驱动器触发文字需连续命中的 OCR 轮数（防抖动重复发指令）

# ---- 对外接口（HTTP，只读内存快照）----
API_VERSION = "1.0"
API_DEFAULT_PORT = 18072        # 避开系统动态端口池(1024~15000)，80xx 段会被临时连接抢占
API_DEFAULT_MAX_RECORDS = 10000  # 内存保留的记录条数上限（超出丢最旧的，老数据仍在 txt 里）
API_DEFAULT_MAX_AGE_MINUTES = 60  # 内存保留的最长时间跨度（分钟）；与条数上限双重限制
API_ENDPOINTS = [
    ("GET", "/health", "存活探测，判断服务在不在"),
    ("GET", "/status", "运行状态：是否监控中、区域、间隔、延迟"),
    ("GET", "/current", "当前屏幕区域的完整文字"),
    ("GET", "/records?since=&limit=", "按 seq 增量拉取记录（1 秒轮询用这个）"),
    ("GET", "/recent?minutes=30", "最近 N 分钟的全部记录（默认 30 分钟）"),
    ("GET", "/latest?limit=", "最近 N 条记录（默认 1 = 最近一句）"),
    ("GET", "/pending", "正在确认中的候选文字"),
    ("GET", "/text?mode=", "纯文本简版（latest / current / all）"),
    ("GET", "/wait?since=&timeout=", "长轮询：有新记录立即返回，否则挂起到超时"),
    ("GET", "/rules", "当前指令规则列表"),
    ("POST", "/command", "主动发一条指令（需在程序里开启）"),
]


def append_text_capped(path, text, max_bytes=2_000_000):
    """追加写入文本文件；超过 max_bytes 时先把旧文件滚动为 .old（只留一份）。

    用于错误日志、指令发送记录这类只增不减的文件，防止长期挂机无限膨胀。
    监控记录文件（屏幕文字记录）不在这里截断——用户数据不能丢，要分文件用界面选项。
    """
    path = Path(path)
    try:
        if path.exists() and path.stat().st_size > max_bytes:
            old = path.with_name(path.name + ".old")
            if old.exists():
                old.unlink()
            path.replace(old)
    except Exception:
        pass
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)

def write_error_log(exc_text):
    """把后台线程里的异常写入错误日志文件，方便打包后排查（超 2MB 自动滚动）。"""
    try:
        logging.getLogger().error("后台异常: %s", exc_text[:2000])
    except Exception:
        pass
    try:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        append_text_capped(ERROR_LOG_FILE, f"[{stamp}]\n{exc_text}\n\n")
    except Exception:
        pass

def _setup_logging():
    """初始化滚动日志：logs/app.log（保留约 1MB）。"""
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        h = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=1, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(funcName)s:%(lineno)d %(message)s"))
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        root.addHandler(h)
    except Exception:
        pass

def _install_excepthook():
    """未捕获异常自动写入日志。"""
    def hook(exc_type, exc_value, exc_tb):
        try:
            write_error_log("".join(traceback.format_exception(exc_type, exc_value, exc_tb)))
        except Exception:
            pass
    sys.excepthook = hook

# ---- 单实例锁：防止重复启动（双开会同时写记录、抢串口） ----
_single_instance_mutex = None

def _acquire_single_instance():
    """尝试获取程序级互斥锁；已有实例在跑时返回 False。

    锁句柄保存在模块全局里，进程存活期间一直持有，退出时由系统回收。
    """
    global _single_instance_mutex
    if sys.platform != "win32":
        return True
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _single_instance_mutex = k32.CreateMutexW(
            None, False, "Local\\dupingmu_screen_text_monitor")
        return ctypes.get_last_error() != 183      # 183 = ERROR_ALREADY_EXISTS
    except Exception:
        return True                                # 拿不到锁就不拦截，保证能用

# ---- Windows 下开启 DPI 感知：让框选坐标与截图坐标一致（高分屏/缩放必做） ----
if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)      # 系统 DPI 感知
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


# ============================================================
# 离线 OCR（RapidOCR，模型随程序打包，完全离线）
# ============================================================
def create_ocr():
    """创建离线 OCR 引擎：优先新版 rapidocr，回退旧版 rapidocr_onnxruntime。"""
    try:
        from rapidocr import RapidOCR
    except ImportError:
        from rapidocr_onnxruntime import RapidOCR
    return RapidOCR()


def capture_region(x, y, w, h, sct=None):
    """截取屏幕指定区域，自动钳制到屏幕内，返回 PIL 图像。

    sct 可传入复用的 mss 实例（监控循环复用它，避免每轮重建 GDI 资源）；
    不传则临时创建（手动识别等低频场景）。
    """
    import mss
    from PIL import Image
    own = sct is None
    if own:
        sct = mss.mss()
    try:
        v = sct.monitors[0]
        vx0, vy0 = int(v["left"]), int(v["top"])
        vx1, vy1 = vx0 + int(v["width"]), vy0 + int(v["height"])
        x0 = max(vx0, int(x))
        y0 = max(vy0, int(y))
        x1 = min(vx1, int(x) + max(int(w), 1))
        y1 = min(vy1, int(y) + max(int(h), 1))
        if x1 <= x0 or y1 <= y0:
            raise ValueError("监控区域超出屏幕范围，请重新框选: " + str((int(x), int(y), int(w), int(h))))
        shot = sct.grab({"left": x0, "top": y0,
                         "width": x1 - x0, "height": y1 - y0})
        return Image.frombytes("RGB", shot.size, shot.rgb)
    finally:
        if own:
            sct.close()


def ocr_image(ocr, img):
    """对图像做 OCR，返回多行文本；识别不到则返回空字符串。

    兼容 rapidocr 新旧两版 API：
    - 3.x：返回 RapidOCROutput 对象（.txts 为文本元组）
    - 1.x：返回 (result, elapse) 元组，result 为 [box, text, score] 列表
    """
    out = ocr(img)
    if isinstance(out, tuple):                    # rapidocr 1.x
        result = out[0] or []
        return "\n".join(line[1] for line in result if line and len(line) > 1)
    txts = getattr(out, "txts", None)             # rapidocr 3.x
    if not txts:
        return ""
    return "\n".join(txts)


def diff_new_lines(prev_counts, lines):
    """对比上一帧与当前帧的文字行，返回“新出现的行”（保持出现顺序）和当前行计数。

    prev_counts 是上一帧各行出现次数的字典（行 -> 出现次数）。
    适用于评论区滚动/累积/清空等场景：只有“多出来的那部分”算新增，
    旧行重复出现不会重复记录；同一内容若曾消失再出现，则算新一次出现。
    """
    cur_counts = {}
    for line in lines:
        cur_counts[line] = cur_counts.get(line, 0) + 1
    remaining = dict(prev_counts)
    additions = []
    for line in lines:
        if remaining.get(line, 0) > 0:
            remaining[line] -= 1
        else:
            additions.append(line)
    return additions, cur_counts


# ============================================================
# 文字驱动器：规则文件读写 + 指令发送
# ============================================================
RULES_HEADER = """# 文字驱动器规则文件（每行一条规则，用 | 分隔，支持 # 注释）
# 格式：触发文字|命中指令|关闭指令|延时秒|画面关键词
# 触发文字：屏幕 OCR 文字包含它即命中（可留空，仅用画面关键词）
# 画面关键词：DeepSeek 视觉描述文本包含它即命中（可留空，仅用触发文字）
# 命中后发送“指令前缀+命中指令”；延时秒后发送“指令前缀+关闭指令”（延时0或不填关闭指令则不发送）
# ★ 修改话术：记事本改本文件 → 程序里点“重新加载规则文件”；或程序里“编辑规则”
# 开关对应：1号=灯(GPIO14) 2号=风扇 3号=空调 4号=电视 5号=电脑 6号=热水器 7号=窗帘 8号=水泵
# 直播刷礼物示例：礼物出现→开，N秒后自动关（延时改成0则一直亮等关闭话术）"""

RULES_TEMPLATE = RULES_HEADER + """
小心心|001on|001off|15|
爱心|001on|001off|15|
棒棒糖|002on|002off|15|
玫瑰|003on|003off|15|
玫瑰花|003on|003off|15|
啤酒|004on|004off|15|
气球|005on|005off|15|
甜甜圈|006on|006off|15|
荧光棒|007on|007off|15|
跑车|008on|008off|20|
火箭|008on|008off|20|
"""


def load_rules(path=RULES_FILE):
    """解析 指令规则.txt，返回规则列表 [{"text","on","off","delay","kw"}, ...]。

    兼容旧版 4 段格式（触发文字|命中|关闭|延时）。
    """
    path = Path(path)
    rules = []
    try:
        if not path.exists():
            path.write_text(RULES_TEMPLATE, encoding="utf-8")
            return rules
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 2 or not parts[0]:
                continue
            text, on_code = parts[0], parts[1]
            off_code = parts[2] if len(parts) > 2 else ""
            try:
                delay = int(parts[3]) if len(parts) > 3 and parts[3].strip() else 0
            except ValueError:
                delay = 0
            kw = parts[4] if len(parts) > 4 else ""
            rules.append({"text": text, "on": on_code, "off": off_code,
                          "delay": max(delay, 0), "kw": kw})
    except Exception as e:
        write_error_log(f"读取规则文件失败：{e}\n{traceback.format_exc()}")
    return rules


def save_rules(rules, path=RULES_FILE):
    """把规则列表写回 指令规则.txt（GUI 增删改后调用；保留文件头的说明注释）。"""
    path = Path(path)
    lines = RULES_HEADER.splitlines()
    for r in rules:
        lines.append(f"{r['text']}|{r['on']}|{r['off']}|{r['delay']}|{r.get('kw', '')}")
    try:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as e:
        write_error_log(f"写入规则文件失败：{e}\n{traceback.format_exc()}")


def send_command(cmd, cfg):
    """按配置发送一条指令：serial=串口 / tcp=网络，cfg 为接口设置字典。"""
    data = cmd.encode("utf-8")
    mode = cfg.get("send_mode", "serial")
    if mode == "tcp":
        host = cfg.get("tcp_host") or "127.0.0.1"
        port = int(cfg.get("tcp_port") or 8085)
        with socket.create_connection((host, port), timeout=3) as s:
            s.sendall(data)
    else:
        import serial
        port = cfg.get("serial_port") or "COM3"
        baud = int(cfg.get("baudrate") or 115200)
        with serial.Serial(port, baud, timeout=2) as ser:
            ser.write(data)


# ============================================================
# DeepSeek 视觉：调用官方 API（OpenAI 兼容格式，联网）
# ============================================================
def encode_image_b64(img, fmt="JPEG", quality=85):
    """把 PIL 图像编码为 base64 字符串。"""
    import base64
    import io
    buf = io.BytesIO()
    img.save(buf, format=fmt, quality=quality)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def vision_describe(img, cfg, prompt=None):
    """调用 DeepSeek 视觉 API 描述屏幕截图，返回描述文本。

    cfg 需要字段：api_key、base_url（默认 https://api.deepseek.com）、model。
    格式为 OpenAI 兼容的 chat/completions + image_url(base64)。
    """
    import requests
    base_url = (cfg.get("base_url") or "https://api.deepseek.com").rstrip("/")
    api_key = cfg.get("api_key") or ""
    model = cfg.get("model") or "deepseek-v4-pro"
    if not api_key:
        raise RuntimeError("未配置 API Key")
    prompt = prompt or "请用中文简要描述这张屏幕截图的内容，包括画面里的文字和界面元素。"
    b64 = encode_image_b64(img)
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url",
                 "image_url": {"url": "data:image/jpeg;base64," + b64}},
                {"type": "text", "text": prompt},
            ],
        }],
        "max_tokens": 300,
    }
    resp = requests.post(
        base_url + "/chat/completions",
        json=payload,
        headers={"Authorization": "Bearer " + api_key,
                 "Content-Type": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"].strip()


# ============================================================
# 区域框选：全屏半透明遮罩 + 鼠标拖拽
# ============================================================
class RegionSelector:
    """弹出一个全屏遮罩窗口，用户按住左键拖拽框选区域，松开即完成。"""

    def __init__(self, root, on_done, on_cancel=None):
        self.on_done = on_done
        self.on_cancel = on_cancel

        self.top = tk.Toplevel(root)
        self.top.attributes("-fullscreen", True)
        self.top.attributes("-topmost", True)
        self.top.attributes("-alpha", 0.35)
        self.top.configure(bg="black")

        self.canvas = tk.Canvas(self.top, cursor="cross", bg="black",
                                highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self.canvas.create_text(
            12, 12, anchor="nw", fill="white",
            font=("Microsoft YaHei UI", 14),
            text="按住鼠标左键拖拽框选监控区域，松开鼠标完成；按 Esc 取消")

        self.start = None
        self.rect_id = None
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Escape>", lambda e: self._cancel())
        self.top.focus_force()

    def _on_press(self, event):
        self.start = (event.x, event.y)
        if self.rect_id is not None:
            self.canvas.delete(self.rect_id)
        self.rect_id = self.canvas.create_rectangle(
            event.x, event.y, event.x, event.y, outline="red", width=3)

    def _on_drag(self, event):
        if self.start is None:
            return
        x1, y1 = self.start
        self.canvas.coords(self.rect_id, x1, y1, event.x, event.y)

    def _on_release(self, event):
        if self.start is None:
            return
        x1, y1 = self.start
        x2, y2 = event.x, event.y
        x, y = min(x1, x2), min(y1, y2)
        w, h = abs(x2 - x1), abs(y2 - y1)
        if w < 5 or h < 5:
            self._cancel()
            return
        self.top.destroy()
        self.on_done(int(x), int(y), int(w), int(h))

    def _cancel(self):
        self.top.destroy()
        if self.on_cancel:
            self.on_cancel()


# ============================================================
# 对外接口：HTTP 只读内存快照（纯标准库，不引入任何新依赖）
# ============================================================
# 设计要点：接口只读内存，绝不触发截图或 OCR。识别仍由 _monitor_loop 按固定节奏跑，
# HTTP 线程只负责把最新状态序列化返回。理由：
#   ① RapidOCR 不是线程安全的，并发请求会直接把程序搞崩；
#   ② 请求驱动的采样率与屏幕变化无关，屏幕没变也要白跑一遍推理，纯浪费算力。
# 这样 1 秒请求 100 次也不增加负担，响应稳定在毫秒级，OCR 再慢也不拖慢接口。


class ApiState:
    """对外接口的共享内存状态。所有读写都必须持 self.lock。"""

    def __init__(self, max_records=API_DEFAULT_MAX_RECORDS,
                 max_age_minutes=API_DEFAULT_MAX_AGE_MINUTES):
        self.lock = threading.Lock()
        self.boot_id = "%08x" % random.getrandbits(32)  # 每次启动变化，调用方据此识别重启
        self.started_at = time.time()
        self.seq = 0                                    # 记录序号，进程内单调递增
        self.total = 0                                  # 累计记录数（不随队列淘汰减少）
        self.max_age_ms = max(int(max_age_minutes), 1) * 60000
        self.records = collections.deque(maxlen=max(int(max_records), 10))
        self.frame = {"text": "", "lines": [], "ts_ms": 0, "changed": False}
        self.pending = []
        self.last_error = None

    # ---- 写入（监控线程调用）----
    def set_frame(self, text, lines, changed):
        """更新“最新一帧整屏文字”。"""
        with self.lock:
            self.frame = {"text": text, "lines": list(lines),
                          "ts_ms": int(time.time() * 1000),
                          "changed": bool(changed)}

    def set_pending(self, items):
        """更新“正在确认中的候选行”。"""
        with self.lock:
            self.pending = list(items)

    def push_record(self, text, screen="", first_seen_ms=None, source="ocr"):
        """新增一条已确认记录，返回记录对象。"""
        with self.lock:
            self.seq += 1
            self.total += 1
            now_ms = int(time.time() * 1000)
            rec = {
                "seq": self.seq,
                "text": text,
                "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "ts_ms": now_ms,
                "first_seen_ms": int(first_seen_ms) if first_seen_ms else now_ms,
                "screen": screen,
                "source": source,
            }
            self.records.append(rec)
            # 按时间清理：只保留最近 max_age_ms 内的记录（与条数上限双重限制）
            cutoff = now_ms - self.max_age_ms
            while self.records and self.records[0]["ts_ms"] < cutoff:
                self.records.popleft()
            return rec

    def set_error(self, msg):
        with self.lock:
            self.last_error = msg

    def resize(self, n, max_age_minutes=None):
        """调整内存保留条数与时间跨度（改配置时调用）。"""
        n = max(int(n), 10)
        with self.lock:
            if max_age_minutes is not None:
                self.max_age_ms = max(int(max_age_minutes), 1) * 60000
            if n == self.records.maxlen:
                return
            self.records = collections.deque(list(self.records)[-n:], maxlen=n)

    # ---- 读取（HTTP 线程调用）----
    def fetch(self, since, limit):
        """返回 (records, last_seq, has_more)。since=None 表示只对齐进度、不返回历史。"""
        with self.lock:
            last_seq = self.seq
            if since is None:
                return [], last_seq, False
            buf = [r for r in self.records if r["seq"] > since]
            return buf[:limit], last_seq, len(buf) > limit

    def tail(self, limit):
        with self.lock:
            return list(self.records)[-limit:], self.seq

    def pending_snapshot(self):
        with self.lock:
            return [dict(p) for p in self.pending]

    def recent(self, minutes, limit):
        """返回最近 minutes 分钟内的记录（按时间升序）。

        返回 (records, truncated, oldest_available_ms, cutoff_ms)。
        truncated=True 表示返回的不是窗口内的全部数据（内存淘汰，或超过 limit），
        调用方据此判断"这半小时的记录到底完不完整"。
        """
        cutoff = int((time.time() - minutes * 60) * 1000)
        with self.lock:
            recs = list(self.records)
            cap = self.records.maxlen or 0
        if not recs:
            return [], False, None, cutoff
        oldest = recs[0]["ts_ms"]
        # 队列已满说明发生过淘汰；此时最早一条仍落在窗口内，说明窗口起点已被丢掉
        truncated = bool(cap) and len(recs) >= cap and oldest > cutoff
        picked = [r for r in recs if r["ts_ms"] >= cutoff]
        if limit and len(picked) > limit:
            picked = picked[-limit:]          # 保留最新的 limit 条
            truncated = True
        return picked, truncated, oldest, cutoff

    def stats(self):
        """内存里记录的时间跨度与条数，供 /status 展示。"""
        with self.lock:
            recs = list(self.records)
            cap = self.records.maxlen or 0
            total = self.total
            max_age_ms = self.max_age_ms
        now_ms = int(time.time() * 1000)
        if not recs:
            return {"in_memory": 0, "capacity": cap, "records_total": total,
                    "oldest_ms": None, "newest_ms": None, "span_seconds": 0,
                    "max_age_minutes": max_age_ms // 60000, "recent_30min": 0}
        cutoff30 = now_ms - 30 * 60 * 1000
        return {
            "in_memory": len(recs),
            "capacity": cap,
            "records_total": total,
            "oldest_ms": recs[0]["ts_ms"],
            "newest_ms": recs[-1]["ts_ms"],
            "span_seconds": round((recs[-1]["ts_ms"] - recs[0]["ts_ms"]) / 1000.0, 1),
            "max_age_minutes": max_age_ms // 60000,
            "recent_30min": sum(1 for r in recs if r["ts_ms"] >= cutoff30),
        }

    def wait_for(self, since, timeout):
        """长轮询：等到 seq > since 或超时，返回是否等到了新记录。"""
        deadline = time.time() + timeout
        while True:
            with self.lock:
                if self.seq > since:
                    return True
            remain = deadline - time.time()
            if remain <= 0:
                return False
            time.sleep(min(1.0, remain))


def _parse_int(qs, key, default=None, lo=None, hi=None):
    """从 query string 解析整数，返回 (值, 错误信息)。未传该参数则返回 default。"""
    if key not in qs:
        return default, None
    raw = (qs[key][0] or "").strip()
    if raw == "":
        return default, None
    try:
        v = int(raw)
    except ValueError:
        return None, "参数 %s 必须是整数" % key
    if lo is not None and v < lo:
        return None, "参数 %s 不能小于 %s" % (key, lo)
    if hi is not None and v > hi:
        return None, "参数 %s 不能大于 %s" % (key, hi)
    return v, None


def _parse_float(qs, key, default=None, lo=None, hi=None):
    if key not in qs:
        return default, None
    raw = (qs[key][0] or "").strip()
    if raw == "":
        return default, None
    try:
        v = float(raw)
    except ValueError:
        return None, "参数 %s 必须是数字" % key
    if lo is not None and v < lo:
        return None, "参数 %s 不能小于 %s" % (key, lo)
    if hi is not None and v > hi:
        return None, "参数 %s 不能大于 %s" % (key, hi)
    return v, None


class _ApiHTTPServer(ThreadingHTTPServer):
    """对外接口用的 HTTP 服务。

    关键点：关掉 SO_REUSEADDR。Windows 的 SO_REUSEADDR 语义是“允许绑定到已被
    占用的端口”，会让端口占用检测彻底失效——服务看似启动成功，实际收不到请求。
    监听 socket 关闭不会进 TIME_WAIT，所以关掉它不影响改端口后立刻重启。
    """

    allow_reuse_address = False
    daemon_threads = True


class ApiServer:
    """对外只读 HTTP 接口服务。

    start() 绑定端口并起线程，stop() 关闭。端口被占用会抛 OSError，
    由界面提示用户换端口，不影响监控本身。
    """

    def __init__(self, app):
        self.app = app
        self.httpd = None
        self.thread = None
        self.host = ""
        self.port = 0

    @property
    def running(self):
        return self.httpd is not None

    @property
    def base_url(self):
        host = "127.0.0.1" if self.host in ("", "0.0.0.0") else self.host
        return "http://%s:%d" % (host, self.port)

    def status_data(self):
        """/health 与 /status 的公共数据。"""
        app = self.app
        st = app.api_state
        with st.lock:
            frame = dict(st.frame)
            last_seq, total = st.seq, st.total
            last_rec = dict(st.records[-1]) if st.records else None
            pending_n = len(st.pending)
            last_error = st.last_error
            uptime = time.time() - st.started_at
        now_ms = int(time.time() * 1000)
        mem = st.stats()
        return {
            "service": "dupingmu",
            "aliases": ["dupingmu", "screen-text-monitor"],
            "version": API_VERSION,
            "uptime_seconds": round(uptime, 1),
            "monitoring": bool(app.running),
            "memory": mem,
            "recent_30min_count": mem.get("recent_30min", 0),
            "region": list(app.region) if app.region else None,
            "interval": getattr(app, "_active_interval", None),
            "confirm_polls": getattr(app, "_active_confirm_polls", None),
            "ocr_ready": getattr(app, "ocr", None) is not None,
            "frame_age_ms": (now_ms - frame["ts_ms"]) if frame["ts_ms"] else None,
            "pending_count": pending_n,
            "records_total": total,
            "last_seq": last_seq,
            "last_record": last_rec,
            "last_error": last_error,
            "log_file": getattr(app, "_active_log", None),
            "driver_enabled": bool(getattr(app, "_active_driver_enabled", False)),
            "vision_enabled": bool(getattr(app, "_active_vision_enabled", False)),
        }

    def start(self, host, port, token, allow_command):
        """启动服务并返回访问地址。端口占用等错误向上抛 OSError。"""
        self.stop()
        app = self.app
        st = app.api_state
        srv = self
        port = int(port)

        def ok(data):
            return {"ok": True, "server": "dupingmu",
                    "boot_id": st.boot_id, "data": data}

        def err(code, message):
            return {"ok": False, "server": "dupingmu", "boot_id": st.boot_id,
                    "error": {"code": code, "message": message}}

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"   # 每个请求独立连接，简单可靠

            def log_message(self, fmt, *args):
                logging.getLogger().debug("api %s %s", self.address_string(), fmt % args)

            # ---- 响应 ----
            def _send(self, body, ctype, status=200):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, obj, status=200):
                self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8", status)

            def _plain(self, s, status=200):
                self._send(s.encode("utf-8"), "text/plain; charset=utf-8", status)

            def _html(self, s, status=200):
                self._send(s.encode("utf-8"), "text/html; charset=utf-8", status)

            # ---- 入口 ----
            def do_GET(self):
                self._dispatch("GET")

            def do_POST(self):
                self._dispatch("POST")

            def _dispatch(self, method):
                try:
                    self._route(method)
                except (BrokenPipeError, ConnectionResetError):
                    pass                        # 调用方主动断开（长轮询常见），不算错误
                except Exception as e:
                    write_error_log("接口处理异常：%s\n%s" % (e, traceback.format_exc()))
                    st.set_error(str(e))
                    try:
                        self._json(err("internal_error", str(e)), 500)
                    except Exception:
                        pass

            def _route(self, method):
                parsed = urlparse(self.path)
                path = parsed.path.rstrip("/") or "/"
                qs = parse_qs(parsed.query)

                if not self._auth(qs):
                    return self._json(err("unauthorized", "令牌缺失或错误"), 401)

                table = {
                    "/": self._h_index,
                    "/health": self._h_health,
                    "/status": self._h_status,
                    "/current": self._h_current,
                    "/records": self._h_records,
                    "/recent": self._h_recent,
                    "/latest": self._h_latest,
                    "/pending": self._h_pending,
                    "/text": self._h_text,
                    "/wait": self._h_wait,
                    "/rules": self._h_rules,
                    "/command": self._h_command,
                }
                fn = table.get(path)
                if fn is None:
                    return self._json(err("not_found", "未知路径：" + path), 404)
                if path == "/command":
                    if method != "POST":
                        return self._json(
                            err("method_not_allowed", "/command 只支持 POST"), 405)
                elif method != "GET":
                    return self._json(
                        err("method_not_allowed", path + " 只支持 GET"), 405)
                return fn(qs)

            def _auth(self, qs):
                if not token:
                    return True
                got = (self.headers.get("X-Auth-Token") or "").strip()
                if not got:
                    got = (qs.get("token", [""])[0] or "").strip()
                return got == token

            # ---- 各接口 ----
            def _h_index(self, qs):
                rows = "".join(
                    "<tr><td><code>%s</code></td><td><code>%s</code></td>"
                    "<td>%s</td></tr>" % e for e in API_ENDPOINTS)
                # 注意：这里不能用 % 格式化——CSS 里的 width:100%} 会被当成格式符。
                # 用占位符替换，彻底绕开 % 和 {} 的转义问题。
                html = (
                    "<!doctype html><meta charset='utf-8'>"
                    "<title>屏幕文字识别监控 · 对外接口</title>"
                    "<style>body{font-family:system-ui,'Microsoft YaHei UI',sans-serif;"
                    "margin:32px;max-width:780px;line-height:1.6}"
                    "table{border-collapse:collapse;width:100%}"
                    "td,th{border:1px solid #bbb;padding:6px 10px;text-align:left}"
                    "code{background:#f2f2f2;padding:1px 5px;border-radius:3px}</style>"
                    "<h2>屏幕文字识别监控 · 对外接口</h2>"
                    "<p>服务标识 <code>dupingmu</code>　启动 ID <code>@@BOOT@@</code>"
                    "　版本 <code>@@VER@@</code></p>"
                    "<table><tr><th>方法</th><th>路径</th><th>说明</th></tr>"
                    "@@ROWS@@</table>"
                    "<p>轮询取新话：<code>GET /records?since={上次的 last_seq}</code><br>"
                    "最近半小时全部：<code>GET /recent?minutes=30</code><br>"
                    "取最近一句：<code>GET /latest?limit=1</code>　"
                    "取整屏文字：<code>GET /current</code><br>"
                    "低延迟等待：<code>GET /wait?since={last_seq}&amp;timeout=10</code></p>"
                    "<p>完整说明见程序目录下的 <code>接口文档.md</code></p>"
                ).replace("@@BOOT@@", st.boot_id).replace(
                    "@@VER@@", API_VERSION).replace("@@ROWS@@", rows)
                self._html(html)

            def _h_health(self, qs):
                self._json(ok(srv.status_data()))

            def _h_status(self, qs):
                self._json(ok(srv.status_data()))

            def _h_current(self, qs):
                with st.lock:
                    frame = dict(st.frame)
                now_ms = int(time.time() * 1000)
                data = {
                    "text": frame["text"],
                    "lines": list(frame["lines"]),
                    "frame_ts_ms": frame["ts_ms"],
                    "frame_age_ms": (now_ms - frame["ts_ms"]) if frame["ts_ms"] else None,
                    "changed": frame["changed"],
                }
                if frame["ts_ms"]:
                    data["frame_ts"] = datetime.fromtimestamp(
                        frame["ts_ms"] / 1000.0).strftime("%Y-%m-%d %H:%M:%S")
                if not app.running:
                    data["note"] = "监控未启动"
                self._json(ok(data))

            def _h_records(self, qs):
                since, e1 = _parse_int(qs, "since", None, lo=0)
                if e1:
                    return self._json(err("bad_param", e1), 400)
                limit, e2 = _parse_int(qs, "limit", 50, lo=1, hi=500)
                if e2:
                    return self._json(err("bad_param", e2), 400)
                fmt = (qs.get("format", ["json"])[0] or "json").strip().lower()
                recs, last_seq, has_more = st.fetch(since, limit)
                if fmt == "text":
                    return self._plain("\n".join(r["text"] for r in recs))
                with st.lock:
                    pending_n = len(st.pending)
                self._json(ok({"records": recs, "count": len(recs),
                               "last_seq": last_seq, "has_more": has_more,
                               "pending_count": pending_n,
                               "monitoring": bool(app.running)}))

            def _h_recent(self, qs):
                minutes, e1 = _parse_int(qs, "minutes", 30, lo=1, hi=1440)
                if e1:
                    return self._json(err("bad_param", e1), 400)
                limit, e2 = _parse_int(qs, "limit", 2000, lo=1, hi=10000)
                if e2:
                    return self._json(err("bad_param", e2), 400)
                fmt = (qs.get("format", ["json"])[0] or "json").strip().lower()
                recs, truncated, oldest, cutoff = st.recent(minutes, limit)
                if fmt == "text":
                    return self._plain("\n".join(r["text"] for r in recs))
                with st.lock:
                    cap = st.records.maxlen or 0
                    max_age = st.max_age_ms // 60000
                    last_seq = st.seq
                data = {
                    "records": recs,
                    "count": len(recs),
                    "minutes": minutes,
                    "from_ms": cutoff,
                    "to_ms": int(time.time() * 1000),
                    "truncated": truncated,
                    "oldest_available_ms": oldest,
                    "last_seq": last_seq,
                    "monitoring": bool(app.running),
                }
                if truncated:
                    data["note"] = (
                        "返回的不是该时间窗口内的全部记录：内存只保留最近 %d 条 / %d 分钟，"
                        "更早的历史请读记录 txt 文件。" % (cap, max_age))
                self._json(ok(data))

            def _h_latest(self, qs):
                limit, e = _parse_int(qs, "limit", 1, lo=1, hi=500)
                if e:
                    return self._json(err("bad_param", e), 400)
                recs, last_seq = st.tail(limit)
                self._json(ok({"records": recs, "count": len(recs),
                               "last_seq": last_seq}))

            def _h_pending(self, qs):
                items = st.pending_snapshot()
                now_ms = int(time.time() * 1000)
                for it in items:
                    it["age_ms"] = now_ms - int(it.get("first_seen_ms") or now_ms)
                self._json(ok({"pending": items, "count": len(items)}))

            def _h_text(self, qs):
                mode = (qs.get("mode", ["latest"])[0] or "latest").strip().lower()
                if mode == "current":
                    with st.lock:
                        s = st.frame["text"]
                elif mode == "all":
                    recs, _ = st.tail(10)
                    s = "\n".join(r["text"] for r in recs)
                elif mode == "recent":
                    minutes, e = _parse_int(qs, "minutes", 30, lo=1, hi=1440)
                    if e:
                        return self._plain("")
                    recs, _t, _o, _c = st.recent(minutes, 10000)
                    s = "\n".join(r["text"] for r in recs)
                else:
                    recs, _ = st.tail(1)
                    s = recs[0]["text"] if recs else ""
                self._plain(s)

            def _h_wait(self, qs):
                since, e1 = _parse_int(qs, "since", None, lo=0)
                if e1:
                    return self._json(err("bad_param", e1), 400)
                if since is None:
                    return self._json(err("bad_param", "必须提供 since 参数"), 400)
                tmo, e2 = _parse_float(qs, "timeout", 10.0, lo=0.0, hi=30.0)
                if e2:
                    return self._json(err("bad_param", e2), 400)
                t0 = time.time()
                got = st.wait_for(since, tmo)
                recs, last_seq, has_more = st.fetch(since, 500)
                self._json(ok({"records": recs, "count": len(recs),
                               "last_seq": last_seq, "has_more": has_more,
                               "waited_ms": int((time.time() - t0) * 1000),
                               "timed_out": not got}))

            def _h_rules(self, qs):
                rules = [dict(r) for r in list(app.rules)]
                self._json(ok({"rules": rules, "count": len(rules)}))

            def _h_command(self, qs):
                if not allow_command:
                    return self._json(err(
                        "forbidden",
                        "服务端未开启 /command（在程序“对外接口”页签勾选后重试）"), 403)
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    body_raw = self.rfile.read(n) if n > 0 else b""
                    body = json.loads(body_raw.decode("utf-8")) if body_raw else {}
                except Exception:
                    return self._json(err("bad_param", "请求体不是合法 JSON"), 400)
                code = str(body.get("code") or "").strip()
                if not code:
                    return self._json(err("bad_param", "缺少 code 字段"), 400)
                if not app.running:
                    return self._json(err(
                        "not_monitoring", "监控未启动，请先在程序里点开始监控"), 409)
                cfg = app._active_driver_cfg
                if not cfg:
                    return self._json(err("not_ready", "发送接口设置未就绪"), 409)
                label = str(body.get("label") or "外部触发").strip()
                cmd = cfg.get("cmd_prefix", "") + code + cfg.get("cmd_suffix", "")
                app._send_queue.put((cmd, cfg, label))
                self._json(ok({"sent": cmd, "queued": True}))

        httpd = _ApiHTTPServer((host, port), Handler)
        self.httpd = httpd
        self.host = host
        self.port = port
        self.thread = threading.Thread(target=httpd.serve_forever, daemon=True,
                                       name="api-server")
        self.thread.start()
        return self.base_url

    def stop(self):
        """关闭服务（可重复调用）。"""
        if self.httpd is not None:
            try:
                self.httpd.shutdown()
            except Exception:
                pass
            try:
                self.httpd.server_close()
            except Exception:
                pass
            self.httpd = None
        if self.thread is not None:
            try:
                self.thread.join(timeout=2.0)
            except Exception:
                pass
            self.thread = None


# ============================================================
# 主程序
# ============================================================
class ScreenTextMonitorApp:
    def __init__(self, root):
        self.root = root
        self.root.title("屏幕文字识别监控 · 文字驱动器 · DeepSeek 视觉")
        self.root.geometry("700x660")
        self.root.minsize(620, 540)

        self.region = None            # (x, y, w, h)
        self.running = False
        self.monitor_thread = None
        self.msg_queue = queue.Queue()
        self.ocr = None
        self._active_log = str(DEFAULT_LOG_FILE)   # 监控期间使用的记录文件（启动时快照）
        self._active_interval = 2                  # 监控期间使用的识别间隔（启动时快照）
        self._active_log_daily = False             # 监控期间是否按日期分文件（快照）
        self._active_confirm_polls = 2             # 监控期间的新文字确认轮数（快照）
        # 以下均为运行快照：后台线程一律用快照，不直接读 Tk 变量（Tkinter 非线程安全）
        self._active_driver_enabled = True
        self._active_driver_cfg = {}
        self._active_vision_enabled = False
        self._active_vision_auto = True
        self._active_vision_cfg = {}
        self._active_vision_log = str(DEFAULT_VISION_LOG_FILE)
        self._vision_lock = threading.Lock()
        self._vision_busy = False                  # 视觉请求在飞标志（防并发堆积）
        self._vision_ts = 0.0                      # 上次视觉调用时间（冷却用）
        self._serial = None                        # 常驻串口连接（避免每次发送重开端口复位 ESP32）
        self._serial_key = None
        self._ocr_lock = threading.Lock()          # OCR 引擎只创建一次（防手动识别与监控并发创建）
        self._err_throttle = {}                    # 监控循环重复报错节流 {key: (msg, ts)}
        self.api_state = ApiState()                # 对外接口的内存快照（供其他项目读取）
        self.api_server = ApiServer(self)          # 对外 HTTP 接口服务（纯标准库实现）

        self.rules = []               # 文字驱动器规则列表
        self._driver_state = []       # 每条规则的运行状态 {"text_hit","kw_hit","active","off_due"}

        self.interval_var = tk.DoubleVar(value=0.5)
        self.log_var = tk.StringVar(value=str(DEFAULT_LOG_FILE))
        self.log_daily_var = tk.BooleanVar(value=False)     # 记录按日期分文件
        self.confirm_polls_var = tk.IntVar(value=2)         # 新文字连续出现几轮才落盘
        self.region_var = tk.StringVar(value="（未选择）")
        self.status_var = tk.StringVar(value="就绪：请先框选要监控的屏幕区域")

        self._build_ui()
        self._load_config()
        self._load_driver_rules(announce=False)
        self._refresh_active_settings()
        self._apply_api_settings(announce=False)   # 按配置自动启停对外接口
        self._send_queue = queue.Queue()
        threading.Thread(target=self._sender_loop, daemon=True,
                         name="cmd-sender").start()   # 指令发送线程：串口/TCP 超时不阻塞监控
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(200, self._poll_queue)

    # ================= 界面 =================
    def _build_ui(self):
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True)
        self.tab_monitor = ttk.Frame(self.nb)
        self.tab_driver = ttk.Frame(self.nb)
        self.tab_vision = ttk.Frame(self.nb)
        self.tab_api = ttk.Frame(self.nb)
        self.nb.add(self.tab_monitor, text="  屏幕监控  ")
        self.nb.add(self.tab_driver, text="  文字驱动器  ")
        self.nb.add(self.tab_vision, text="  DeepSeek 视觉  ")
        self.nb.add(self.tab_api, text="  对外接口  ")
        self._build_monitor_tab()
        self._build_driver_tab()
        self._build_vision_tab()
        self._build_api_tab()

    # ---------------- 页签一：屏幕监控 ----------------
    def _build_monitor_tab(self):
        pad = {"padx": 10, "pady": 6}

        frm_region = tk.LabelFrame(self.tab_monitor, text="监控区域", padx=10, pady=8)
        frm_region.pack(fill="x", **pad)
        tk.Label(frm_region, textvariable=self.region_var).pack(side="left")
        self.btn_region = tk.Button(frm_region, text="框选区域", command=self.choose_region)
        self.btn_region.pack(side="right")

        frm_set = tk.LabelFrame(self.tab_monitor, text="设置", padx=10, pady=8)
        frm_set.pack(fill="x", **pad)
        tk.Label(frm_set, text="识别间隔（秒）：").pack(side="left")
        self.interval_spin = tk.Spinbox(frm_set, from_=0.5, to=60, increment=0.5,
                                        format="%.1f",
                                        textvariable=self.interval_var, width=6)
        self.interval_spin.pack(side="left")
        tk.Label(frm_set, text="确认轮数(1=出现即记)：").pack(side="left", padx=(14, 0))
        self.confirm_spin = tk.Spinbox(frm_set, from_=1, to=5, increment=1,
                                       textvariable=self.confirm_polls_var, width=3)
        self.confirm_spin.pack(side="left")
        self.btn_test = tk.Button(frm_set, text="立即识别一次", command=self.test_recognize)
        self.btn_test.pack(side="right", padx=4)
        self.btn_copylog = tk.Button(frm_set, text="复制日志(报错用)", command=self.copy_logs_for_dev)
        self.btn_copylog.pack(side="right", padx=4)

        frm_log = tk.LabelFrame(self.tab_monitor, text="记录文件（记事本）", padx=10, pady=8)
        frm_log.pack(fill="x", **pad)
        self.log_entry = tk.Entry(frm_log, textvariable=self.log_var)
        self.log_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.btn_log = tk.Button(frm_log, text="选择…", command=self.choose_log_file)
        self.btn_log.pack(side="left")
        self.chk_log_daily = tk.Checkbutton(frm_log, text="按日期分文件（每天一个新 txt）",
                                            variable=self.log_daily_var)
        self.chk_log_daily.pack(side="left", padx=(10, 0))

        frm_ctrl = tk.Frame(self.tab_monitor)
        frm_ctrl.pack(fill="x", **pad)
        self.btn_start = tk.Button(frm_ctrl, text="开始监控", command=self.start_monitor, width=12)
        self.btn_start.pack(side="left")
        self.btn_stop = tk.Button(frm_ctrl, text="停止监控", command=self.stop_monitor,
                                  width=12, state="disabled")
        self.btn_stop.pack(side="left", padx=8)
        tk.Button(frm_ctrl, text="打开记录文件", command=self.open_log_file).pack(side="right")

        tk.Label(self.tab_monitor, textvariable=self.status_var, fg="#1a66cc",
                 anchor="w", wraplength=640).pack(fill="x", **pad)

        tk.Label(self.tab_monitor, text="最近识别内容：", anchor="w").pack(fill="x", padx=10)
        self.txt = tk.Text(self.tab_monitor, height=9, state="normal", wrap="word")
        self.txt.insert("1.0", "（尚未识别：点“立即识别一次”可测试当前区域，或框选区域后点“开始监控”，识别结果会显示在这里）")
        self.txt.config(state="disabled")
        self.txt.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    # ---------------- 页签二：文字驱动器 ----------------
    def _build_driver_tab(self):
        pad = {"padx": 8, "pady": 4}

        top = tk.Frame(self.tab_driver)
        top.pack(fill="x", **pad)
        self.driver_enabled_var = tk.BooleanVar(value=True)
        self.chk_driver_enabled = tk.Checkbutton(
            top, text="启用文字驱动器（监控/识别时自动匹配屏幕文字并发送指令）",
            variable=self.driver_enabled_var)
        self.chk_driver_enabled.pack(side="left")
        tk.Label(top, text="规则文件：指令规则.txt（也可用记事本直接编辑）",
                 fg="gray").pack(side="right")

        # 规则列表
        tbl_frame = tk.Frame(self.tab_driver)
        tbl_frame.pack(fill="both", expand=True, **pad)
        cols = ("text", "kw", "on", "off", "delay")
        self.rule_tree = ttk.Treeview(tbl_frame, columns=cols, show="headings", height=8)
        headings = {"text": "触发文字", "kw": "画面关键词", "on": "命中指令",
                    "off": "关闭指令", "delay": "延时(秒)"}
        widths = {"text": 140, "kw": 140, "on": 90, "off": 90, "delay": 70}
        for c in cols:
            self.rule_tree.heading(c, text=headings[c])
            self.rule_tree.column(c, width=widths[c], anchor="w")
        sb = ttk.Scrollbar(tbl_frame, orient="vertical", command=self.rule_tree.yview)
        self.rule_tree.configure(yscrollcommand=sb.set)
        self.rule_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        btns = tk.Frame(self.tab_driver)
        btns.pack(fill="x", **pad)
        self.btn_rule_add = tk.Button(btns, text="添加规则", command=self._on_add_rule)
        self.btn_rule_add.pack(side="left")
        self.btn_rule_edit = tk.Button(btns, text="编辑规则", command=self._on_edit_rule)
        self.btn_rule_edit.pack(side="left", padx=6)
        self.btn_rule_del = tk.Button(btns, text="删除规则", command=self._on_del_rule)
        self.btn_rule_del.pack(side="left")
        self.btn_rule_reload = tk.Button(btns, text="重新加载规则文件",
                                         command=self._reload_rules)
        self.btn_rule_reload.pack(side="left", padx=6)
        self.btn_rule_test = tk.Button(btns, text="测试发送(选中规则)",
                                       command=self._test_send_selected)
        self.btn_rule_test.pack(side="right")

        # 接口设置
        frm_if = tk.LabelFrame(self.tab_driver, text="发送接口设置", padx=10, pady=6)
        frm_if.pack(fill="x", **pad)
        frm_if.columnconfigure(1, weight=1)

        tk.Label(frm_if, text="发送方式：").grid(row=0, column=0, sticky="w", pady=2)
        self.send_mode_var = tk.StringVar(value="tcp")
        self.cmb_mode = ttk.Combobox(frm_if, textvariable=self.send_mode_var,
                                     values=("serial", "tcp"), state="readonly", width=8)
        self.cmb_mode.grid(row=0, column=1, sticky="w", pady=2)
        tk.Label(frm_if, text="串口：").grid(row=0, column=2, sticky="e", padx=(14, 2))
        self.serial_port_var = tk.StringVar(value="COM3")
        self.ent_serial = tk.Entry(frm_if, textvariable=self.serial_port_var, width=8)
        self.ent_serial.grid(row=0, column=3, sticky="w", pady=2)
        tk.Label(frm_if, text="波特率：").grid(row=0, column=4, sticky="e", padx=(14, 2))
        self.baudrate_var = tk.StringVar(value="115200")
        self.ent_baud = tk.Entry(frm_if, textvariable=self.baudrate_var, width=8)
        self.ent_baud.grid(row=0, column=5, sticky="w", pady=2)

        tk.Label(frm_if, text="主机(IP)：").grid(row=1, column=0, sticky="w", pady=2)
        self.tcp_host_var = tk.StringVar(value="192.168.1.21")
        self.ent_host = tk.Entry(frm_if, textvariable=self.tcp_host_var, width=14)
        self.ent_host.grid(row=1, column=1, sticky="w", pady=2)
        tk.Label(frm_if, text="端口：").grid(row=1, column=2, sticky="e", padx=(14, 2))
        self.tcp_port_var = tk.StringVar(value="8085")
        self.ent_port = tk.Entry(frm_if, textvariable=self.tcp_port_var, width=8)
        self.ent_port.grid(row=1, column=3, sticky="w", pady=2)
        tk.Label(frm_if, text="指令前缀：").grid(row=1, column=4, sticky="e", padx=(14, 2))
        self.cmd_prefix_var = tk.StringVar(value="001")
        self.ent_prefix = tk.Entry(frm_if, textvariable=self.cmd_prefix_var, width=8)
        self.ent_prefix.grid(row=1, column=5, sticky="w", pady=2)
        tk.Label(frm_if, text="指令后缀：").grid(row=1, column=6, sticky="e", padx=(14, 2))
        self.cmd_suffix_var = tk.StringVar(value="")
        self.ent_suffix = tk.Entry(frm_if, textvariable=self.cmd_suffix_var, width=8)
        self.ent_suffix.grid(row=1, column=7, sticky="w", pady=2)

        frm_if2 = tk.Frame(self.tab_driver)
        frm_if2.pack(fill="x", **pad)
        self.btn_save_cfg = tk.Button(frm_if2, text="保存接口设置", command=self._save_driver_settings)
        self.btn_save_cfg.pack(side="left")
        tk.Label(frm_if2, text="发送示例：前缀001 + 命中指令001on = 001001on；指令后缀可加 \\r\\n 等结束符",
                 fg="gray").pack(side="left", padx=10)

        self.driver_status_var = tk.StringVar(value="文字驱动器就绪")
        tk.Label(self.tab_driver, textvariable=self.driver_status_var, fg="#1a66cc",
                 anchor="w", wraplength=640).pack(fill="x", **pad)

    # ---------------- 页签三：DeepSeek 视觉 ----------------
    def _build_vision_tab(self):
        pad = {"padx": 8, "pady": 4}

        top = tk.Frame(self.tab_vision)
        top.pack(fill="x", **pad)
        self.vision_enabled_var = tk.BooleanVar(value=False)
        self.chk_vision_enabled = tk.Checkbutton(
            top, text="启用 DeepSeek 视觉理解",
            variable=self.vision_enabled_var,
            command=self._vision_enabled_changed)
        self.chk_vision_enabled.pack(side="left")
        tk.Label(top, text="需要联网 + DeepSeek API Key（api.deepseek.com 申请）",
                 fg="gray").pack(side="right")

        frm = tk.LabelFrame(self.tab_vision, text="API 设置", padx=10, pady=6)
        frm.pack(fill="x", **pad)
        tk.Label(frm, text="API Key：").grid(row=0, column=0, sticky="w", pady=2)
        self.vision_key_var = tk.StringVar()
        self.ent_vis_key = tk.Entry(frm, textvariable=self.vision_key_var,
                                    width=44, show="*")
        self.ent_vis_key.grid(row=0, column=1, sticky="w", pady=2)
        tk.Label(frm, text="API 地址：").grid(row=1, column=0, sticky="w", pady=2)
        self.vision_url_var = tk.StringVar(value="https://api.deepseek.com")
        self.ent_vis_url = tk.Entry(frm, textvariable=self.vision_url_var, width=44)
        self.ent_vis_url.grid(row=1, column=1, sticky="w", pady=2)
        tk.Label(frm, text="模型名：").grid(row=2, column=0, sticky="w", pady=2)
        self.vision_model_var = tk.StringVar(value="deepseek-v4-pro")
        self.ent_vis_model = tk.Entry(frm, textvariable=self.vision_model_var, width=44)
        self.ent_vis_model.grid(row=2, column=1, sticky="w", pady=2)
        tk.Label(frm, text="默认 deepseek-v4-pro（识图模型），以官方实际模型名为准；调不通时看 程序错误日志.txt 里的报错",
                 fg="gray", wraplength=560, justify="left").grid(
            row=3, column=0, columnspan=2, sticky="w")

        frm2 = tk.LabelFrame(self.tab_vision, text="描述设置", padx=10, pady=6)
        frm2.pack(fill="x", **pad)
        self.vision_auto_var = tk.BooleanVar(value=True)
        self.chk_vision_auto = tk.Checkbutton(
            frm2, text="屏幕文字变化时自动调用视觉模型描述画面",
            variable=self.vision_auto_var)
        self.chk_vision_auto.pack(side="left")
        tk.Label(frm2, text="记录文件：").pack(side="left", padx=(16, 2))
        self.vision_log_var = tk.StringVar(value=str(DEFAULT_VISION_LOG_FILE))
        self.ent_vis_log = tk.Entry(frm2, textvariable=self.vision_log_var, width=28)
        self.ent_vis_log.pack(side="left")
        tk.Button(frm2, text="选择…", command=self.choose_vision_log_file).pack(side="left", padx=4)
        self.btn_vis_test = tk.Button(frm2, text="识别当前区域并描述", command=self.test_vision)
        self.btn_vis_test.pack(side="right")

        self.vision_status_var = tk.StringVar(value="视觉理解未启用")
        tk.Label(self.tab_vision, textvariable=self.vision_status_var, fg="#1a66cc",
                 anchor="w", wraplength=640).pack(fill="x", **pad)

        tk.Label(self.tab_vision, text="最近视觉描述：", anchor="w").pack(fill="x", padx=8)
        self.vis_txt = tk.Text(self.tab_vision, height=10, state="normal", wrap="word")
        self.vis_txt.insert("1.0", "（未描述：填写 API Key 并勾选启用后，点“识别当前区域并描述”测试）")
        self.vis_txt.config(state="disabled")
        self.vis_txt.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    # ---------------- 页签四：对外接口 ----------------
    def _build_api_tab(self):
        pad = {"padx": 10, "pady": 6}

        frm = tk.LabelFrame(self.tab_api, text="服务设置（只读接口，其他项目用它读取屏幕文字）",
                            padx=10, pady=8)
        frm.pack(fill="x", **pad)

        self.api_enabled_var = tk.BooleanVar(value=True)
        tk.Checkbutton(frm, text="启用对外接口（HTTP，只读内存，不触发截图/OCR）",
                       variable=self.api_enabled_var).grid(
            row=0, column=0, columnspan=4, sticky="w")

        tk.Label(frm, text="端口：").grid(row=1, column=0, sticky="w", pady=3)
        self.api_port_var = tk.StringVar(value=str(API_DEFAULT_PORT))
        self.api_port_entry = tk.Entry(frm, textvariable=self.api_port_var, width=8)
        self.api_port_entry.grid(row=1, column=1, sticky="w")

        self.api_lan_var = tk.BooleanVar(value=False)
        self.api_lan_chk = tk.Checkbutton(
            frm, text="允许局域网访问（默认仅本机；开启后建议设令牌）",
            variable=self.api_lan_var)
        self.api_lan_chk.grid(row=1, column=2, columnspan=2, sticky="w", padx=(16, 0))

        tk.Label(frm, text="访问令牌：").grid(row=2, column=0, sticky="w", pady=3)
        self.api_token_var = tk.StringVar(value="")
        self.api_token_entry = tk.Entry(frm, textvariable=self.api_token_var, width=20)
        self.api_token_entry.grid(row=2, column=1, sticky="w")

        self.api_allow_cmd_var = tk.BooleanVar(value=False)
        self.api_allow_cmd_chk = tk.Checkbutton(
            frm, text="允许 POST /command 主动发指令（默认关闭）",
            variable=self.api_allow_cmd_var)
        self.api_allow_cmd_chk.grid(row=2, column=2, columnspan=2, sticky="w", padx=(16, 0))

        tk.Label(frm, text="内存保留：").grid(row=3, column=0, sticky="w", pady=3)
        self.api_maxrec_var = tk.StringVar(value=str(API_DEFAULT_MAX_RECORDS))
        self.api_maxrec_entry = tk.Entry(frm, textvariable=self.api_maxrec_var, width=8)
        self.api_maxrec_entry.grid(row=3, column=1, sticky="w")
        tk.Label(frm, text="条（供 /recent 回溯用，超出丢最旧的）", fg="gray").grid(
            row=3, column=2, columnspan=2, sticky="w", padx=(16, 0))

        frm2 = tk.Frame(self.tab_api)
        frm2.pack(fill="x", **pad)
        tk.Button(frm2, text="应用并重启接口",
                  command=lambda: self._apply_api_settings(True)).pack(side="left")
        tk.Button(frm2, text="在浏览器打开接口首页",
                  command=self._open_api_home).pack(side="left", padx=8)
        tk.Button(frm2, text="打开接口文档",
                  command=self._open_api_doc).pack(side="left")

        self.api_state_var = tk.StringVar(value="未启动")
        tk.Label(self.tab_api, textvariable=self.api_state_var, fg="#1a66cc",
                 anchor="w", wraplength=660).pack(fill="x", **pad)

        tk.Label(self.tab_api, text="接口速查：", anchor="w").pack(fill="x", padx=10)
        self.api_txt = tk.Text(self.tab_api, height=12, wrap="none",
                               font=("Consolas", 9))
        lines = ["%-4s %-32s %s" % (m, p, d) for m, p, d in API_ENDPOINTS]
        lines += [
            "",
            "轮询取新话（1 秒 1 次就用它）：",
            "    GET /records?since={上次返回的 last_seq}",
            "最近半小时全部记录：GET /recent?minutes=30",
            "取最近一句话：      GET /latest?limit=1",
            "取整屏文字：        GET /current",
            "低延迟（挂起等待）：GET /wait?since={last_seq}&timeout=10",
            "",
            "注意：程序重启后 seq 会归零。请比对响应里的 boot_id，变了就重新从 0 开始。",
        ]
        self.api_txt.insert("1.0", "\n".join(lines))
        self.api_txt.config(state="disabled")
        self.api_txt.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    def _api_port_or_default(self):
        try:
            p = int(str(self.api_port_var.get()).strip())
            return p if 1 <= p <= 65535 else API_DEFAULT_PORT
        except Exception:
            return API_DEFAULT_PORT

    def _api_maxrec_or_default(self):
        try:
            n = int(str(self.api_maxrec_var.get()).strip())
            return n if 100 <= n <= 200000 else API_DEFAULT_MAX_RECORDS
        except Exception:
            return API_DEFAULT_MAX_RECORDS

    def _apply_api_settings(self, announce=True):
        """按界面设置启停接口服务。启动失败（端口占用等）只提示，不影响监控。"""
        if not self.api_enabled_var.get():
            self.api_server.stop()
            self.api_state_var.set("已关闭（其他项目无法读取）")
            return
        host = "0.0.0.0" if self.api_lan_var.get() else "127.0.0.1"
        port = self._api_port_or_default()
        self.api_port_var.set(str(port))
        self.api_state.resize(self._api_maxrec_or_default())
        token = self.api_token_var.get().strip()
        allow_command = bool(self.api_allow_cmd_var.get())
        try:
            url = self.api_server.start(host, port, token, allow_command)
            self.api_state_var.set("监听中　%s　（%d 个接口，%s）" % (
                url, len(API_ENDPOINTS),
                "仅本机" if host == "127.0.0.1" else "允许局域网"))
            logging.getLogger().info("对外接口已启动 %s token=%s cmd=%s",
                                     url, bool(token), allow_command)
            if announce:
                self.status_var.set("对外接口已启动：" + url)
        except OSError as e:
            self.api_state_var.set("启动失败：端口 %d 无法监听（%s）" % (port, e))
            write_error_log("对外接口启动失败：%s\n%s" % (e, traceback.format_exc()))
            if announce:
                messagebox.showerror(
                    "接口启动失败",
                    "端口 %d 无法监听：\n%s\n\n请换一个端口，或检查是否被其他程序占用。"
                    % (port, e))
        except Exception as e:
            self.api_state_var.set("启动失败：%s" % e)
            write_error_log("对外接口启动失败：%s\n%s" % (e, traceback.format_exc()))

    def _open_api_home(self):
        if not self.api_server.running:
            messagebox.showinfo("提示",
                                "接口服务未启动。\n请先勾选“启用对外接口”并点“应用并重启接口”。")
            return
        try:
            webbrowser.open(self.api_server.base_url + "/")
        except Exception as e:
            messagebox.showerror("错误", "无法打开浏览器：%s" % e)

    def _open_api_doc(self):
        path = APP_DIR / "接口文档.md"
        if not path.exists():
            messagebox.showinfo("提示", "接口文档不存在：\n%s" % path)
            return
        try:
            os.startfile(str(path))  # noqa: 用系统默认程序打开
        except Exception as e:
            messagebox.showerror("错误", "无法打开文档：%s" % e)

    def _vision_enabled_changed(self):
        if self.vision_enabled_var.get():
            self.vision_status_var.set("视觉理解已启用（屏幕文字变化时自动描述）")
        else:
            self.vision_status_var.set("视觉理解未启用")

    def _set_vision_text(self, content):
        self.vis_txt.config(state="normal")
        self.vis_txt.delete("1.0", "end")
        self.vis_txt.insert("1.0", content)
        self.vis_txt.config(state="disabled")

    def choose_vision_log_file(self):
        path = filedialog.asksaveasfilename(
            title="选择视觉描述记录文件",
            defaultextension=".txt",
            initialfile="屏幕画面描述.txt",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")])
        if path:
            self.vision_log_var.set(path)
            self._save_config()

    # ---------------- 运行状态下的控件锁定 ----------------
    def _set_running_ui(self, running):
        state_r = "disabled" if running else "normal"
        state_s = "normal" if running else "disabled"
        self.btn_region.config(state=state_r)
        self.interval_spin.config(state=state_r)
        self.btn_test.config(state=state_r)
        self.log_entry.config(state=state_r)
        self.btn_log.config(state=state_r)
        self.btn_start.config(state=state_r)
        self.btn_stop.config(state=state_s)
        for b in (self.btn_rule_add, self.btn_rule_edit, self.btn_rule_del,
                  self.btn_rule_reload, self.btn_rule_test, self.btn_save_cfg,
                  self.cmb_mode, self.ent_serial, self.ent_baud,
                  self.ent_host, self.ent_port, self.ent_prefix, self.ent_suffix,
                  self.btn_vis_test, self.confirm_spin, self.chk_log_daily,
                  self.chk_driver_enabled, self.chk_vision_enabled,
                  self.chk_vision_auto, self.ent_vis_key, self.ent_vis_url,
                  self.ent_vis_model):
            b.config(state=state_r)

    # ================= 区域框选 =================
    def choose_region(self):
        if self.running:
            return
        RegionSelector(self.root, self.on_region_selected)

    def on_region_selected(self, x, y, w, h):
        # 把框选时的窗口坐标换算成屏幕物理像素坐标（自动适应任何显示缩放比例）
        self.region = self._to_physical(x, y, w, h)
        px, py, pw, ph = self.region
        self.region_var.set(f"坐标 ({px}, {py})   宽 {pw} × 高 {ph}")
        self.status_var.set(f"已选择区域 ({px}, {py}) {pw}×{ph}，点击“开始监控”")
        self._save_config()

    def _to_physical(self, x, y, w, h):
        """把 Tk 窗口(逻辑)坐标换算为 mss 截图用的物理像素坐标。

        高分屏/系统缩放时，Tk 的坐标可能是逻辑像素，而截图用的是物理像素，
        直接混用会导致框选区域错位、识别不到。这里用“Tk 屏幕尺寸 / mss 屏幕尺寸”
        的比值换算，无论程序是否成功开启 DPI 感知都能对齐。
        """
        import mss
        with mss.mss() as sct:
            mon = sct.monitors[1]                     # 主屏（框选遮罩所在屏幕）
            pw, ph = mon["width"], mon["height"]
            ox, oy = mon["left"], mon["top"]
        tw = max(self.root.winfo_screenwidth(), 1)
        th = max(self.root.winfo_screenheight(), 1)
        fx, fy = pw / tw, ph / th
        return (int(ox + x * fx), int(oy + y * fy),
                max(int(w * fx), 1), max(int(h * fy), 1))

    # ================= 手动识别一次 =================
    def test_recognize(self):
        if not self.region:
            messagebox.showwarning("提示", "请先框选监控区域")
            return
        self._refresh_active_settings()

        def work():
            try:
                with self._ocr_lock:
                    if self.ocr is None:
                        self.msg_queue.put(("status", "正在加载离线 OCR 模型（首次约需数秒）…"))
                        self.ocr = create_ocr()
                img = capture_region(*self.region)
                text = ocr_image(self.ocr, img)
                self._driver_tick(text)               # 文字驱动器匹配
                self.msg_queue.put(("last", text or "（区域内未识别到文字）"))
                self.msg_queue.put(("status", "手动识别完成"))
                if self._active_vision_enabled and self._active_vision_auto:
                    threading.Thread(target=self._vision_worker,
                                     args=(img, False, self._active_vision_cfg,
                                           self._active_vision_log),
                                     daemon=True).start()
            except Exception as e:
                err = f"识别失败：{e}\n{traceback.format_exc()}"
                write_error_log(err)
                self.msg_queue.put(("error", f"识别失败：{e}（详见 程序错误日志.txt）"))

        threading.Thread(target=work, daemon=True).start()

    # ================= 监控主循环 =================
    def copy_logs_for_dev(self):
        """一键复制最近日志（app.log + 程序错误日志 各 30 行）到剪贴板，发给开发者。"""
        try:
            chunks = ["【日志（供定位）build=20260907-opt2】"]
            for path in (LOG_FILE, ERROR_LOG_FILE):
                chunks.append("===== " + path.name + " 尾部 30 行 =====")
                if path.exists():
                    try:
                        tail = open(path, encoding="utf-8", errors="replace").read().splitlines()[-30:]
                        chunks.extend(tail)
                    except Exception as e:
                        chunks.append("读取失败: " + str(e))
                else:
                    chunks.append("(暂无日志)")
            text = "\n".join(chunks)
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update()
            messagebox.showinfo("已复制", "最近日志已复制到剪贴板。\n\n用法：回到对话里，先写一句现象描述，再直接粘贴日志，即可发给开发者。")
        except Exception as e:
            messagebox.showerror("复制失败", "无法复制日志：" + str(e) + "\n请手动打开 logs\\app.log 发最后 30 行。")
    def start_monitor(self):
        if self.running:
            return
        if not self.region:
            messagebox.showwarning("提示", "请先框选监控区域")
            return
        self._refresh_active_settings()
        self.running = True
        logging.getLogger().info(
            "开始监控 region=%s interval=%s confirm=%s log=%s daily=%s",
            self.region, self._active_interval, self._active_confirm_polls,
            Path(self._active_log).name, self._active_log_daily)
        self._set_running_ui(True)
        self._save_config()
        self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.monitor_thread.start()

    def stop_monitor(self):
        logging.getLogger().info("停止监控")
        self.running = False
        self.status_var.set("正在停止…")

    def _monitor_loop(self):
        """后台线程：循环截图 + OCR。

        针对“评论区逐条弹字”场景做的优化：
        1. 像素级预检：画面没变就不跑 OCR，CPU 占用低，轮询可以很快不漏评论；
        2. 只记新增：对比上一帧，只有“新出现的文字行”才落盘，重复/滚动不重记；
        3. 稳定确认：同一行连续出现 N 轮（默认 2，界面可调）才写记录，滤掉 OCR 半帧/抖动；
        4. mss 实例整个监控期间复用（出错自动重建）；指令发送走独立线程不阻塞截图；
           画面静止时也会按时发送到期的关闭指令（不依赖 OCR 触发）。
        """
        import mss
        sct = None                       # 复用的 mss 实例（失败时置 None 下一轮重建）
        try:
            with self._ocr_lock:
                if self.ocr is None:
                    self.msg_queue.put(("status", "正在加载离线 OCR 模型（首次约需数秒）…"))
                    self.ocr = create_ocr()
            self.msg_queue.put(("status", "OCR 已就绪，开始监控（只记录新出现的文字）…"))
            logging.getLogger().info("监控循环已启动")

            step = 0.1                       # 轮询步长（秒）
            confirm_polls = max(int(self._active_confirm_polls), 1)
            prev_counts = {}                 # 上一帧各文字行的出现次数
            # 待确认的新文字行 -> {"n": 已连续出现轮数, "first_ms": 首次出现时间(epoch 毫秒)}
            # first_ms 是给对外接口用的：调用方据此知道这句话实际什么时候出现在屏幕上
            candidates = {}
            prev_img_bytes = None            # 上一帧图像字节（像素级“变了才 OCR”预检）
            last_full_text = None            # 上一帧完整文字（用于触发视觉描述）

            capture_failures = 0
            while self.running:
                if sct is None:
                    sct = mss.mss()
                self._fire_due_offs(time.time())   # 画面静止时也能按时发到期的关闭指令
                try:
                    img = capture_region(*self.region, sct=sct)
                    capture_failures = 0
                    raw = img.tobytes()
                    changed = (raw != prev_img_bytes)
                    prev_img_bytes = raw

                    # 画面有变化、或有待确认的新文字时，才跑 OCR（省 CPU）
                    text = ocr_image(self.ocr, img) if (changed or candidates) else None
                    if text is not None:
                        self._driver_tick(text)          # 文字驱动器匹配（命中即发指令）

                        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
                        additions, cur_counts = diff_new_lines(prev_counts, lines)
                        prev_counts = cur_counts
                        now_ms = int(time.time() * 1000)

                        # 已不在画面里的候选作废（跳过 OCR 半帧/抖动产生的残影）
                        present = {ln for ln, n in cur_counts.items() if n > 0}
                        candidates = {c: v for c, v in candidates.items()
                                      if c in present}
                        for a in additions:              # 新冒出的行加入候选
                            candidates.setdefault(a, {"n": 0, "first_ms": now_ms})
                        for v in candidates.values():    # 每轮识别算一次“在场”
                            v["n"] += 1

                        confirmed = [c for c, v in candidates.items()
                                     if v["n"] >= confirm_polls]
                        confirmed_info = [(c, candidates[c]["first_ms"])
                                          for c in confirmed]
                        for c in confirmed:
                            candidates.pop(c, None)
                        if confirmed:
                            self._append_log("\n".join(confirmed))
                            # 同步推给对外接口（内存队列，供其他项目按 seq 拉取）
                            for c, first_ms in confirmed_info:
                                self.api_state.push_record(
                                    c, screen=text, first_seen_ms=first_ms)
                            self.msg_queue.put(("status", datetime.now().strftime(
                                "%H:%M:%S") + " 新增文字已写入记录文件"))

                        # 更新对外接口的内存快照（最新帧 + 确认中的候选）
                        self.api_state.set_frame(text, lines, changed)
                        self.api_state.set_pending([
                            {"text": c, "seen_count": v["n"], "need": confirm_polls,
                             "first_seen_ms": v["first_ms"]}
                            for c, v in candidates.items()])
                        self.msg_queue.put(("last", text or "（区域内无文字）"))

                        # 完整文字发生变化时自动调视觉模型描述画面（后台线程，不阻塞监控）
                        if text != last_full_text:
                            last_full_text = text
                            if self._active_vision_enabled and self._active_vision_auto:
                                threading.Thread(target=self._vision_worker,
                                                 args=(img, False,
                                                       self._active_vision_cfg,
                                                       self._active_vision_log),
                                                 daemon=True).start()
                except Exception as e:
                    msg = str(e)
                    if ("ScreenShotError" in msg or "graphics function failed" in msg or "不在屏幕范围" in msg):
                        capture_failures += 1
                        try:
                            sct.close()          # 截图失败常意味着 mss 句柄失效，重建
                        except Exception:
                            pass
                        sct = None
                        if capture_failures == 1:
                            write_error_log("截图失败（将自动重试，不停止监控）：" + msg)
                            self.msg_queue.put(("status", "截图失败，自动重试中（区域可能超出屏幕）…"))
                        time.sleep(0.5)
                        continue
                    err = f"监控出错：{e}\n{traceback.format_exc()}"
                    write_error_log(err)
                    self.api_state.set_error(str(e))
                    # 同一种错误 60 秒内只刷一次状态栏，避免持续报错刷屏
                    last = self._err_throttle.get("monitor")
                    if last is None or last[0] != msg or time.time() - last[1] > 60:
                        self._err_throttle["monitor"] = (msg, time.time())
                        self.msg_queue.put(("error", f"监控出错：{e}（详见 程序错误日志.txt）"))

                # 分小段等待，方便快速停止；总等待 ≈ 识别间隔
                ticks = max(int(round(self._active_interval / step)), 1)
                for _ in range(ticks):
                    if not self.running:
                        break
                    time.sleep(step)
        except Exception:
            err = "监控线程异常：\n" + traceback.format_exc()
            write_error_log(err)
            self.msg_queue.put(("error", "监控线程异常（详见 程序错误日志.txt）"))
        finally:
            try:
                if sct is not None:
                    sct.close()
            except Exception:
                pass
            self.msg_queue.put(("monitor_stopped", "", ""))

    def _append_log(self, text):
        try:
            path = Path(self._active_log).expanduser()
            if self._active_log_daily:
                path = path.with_name(
                    f"{path.stem}_{datetime.now():%Y-%m-%d}{path.suffix}")
            path.parent.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"[{stamp}]\n{text}\n\n")
        except Exception as e:
            write_error_log(f"写入记录文件失败：{e}\n{traceback.format_exc()}")
            self.msg_queue.put(("error", f"写入记录文件失败：{e}（详见 程序错误日志.txt）"))

    # ================= 文字驱动器 =================
    def _load_driver_rules(self, announce=True):
        self.rules = load_rules()
        self._driver_state = [self._new_rule_state() for _ in self.rules]
        self._refresh_rule_tree()
        if announce:
            self.status_var.set(f"文字驱动器：已加载 {len(self.rules)} 条规则")

    @staticmethod
    def _new_rule_state():
        return {"text_hit": False, "kw_hit": False, "active": False,
                "off_due": None, "text_streak": 0}

    def _refresh_rule_tree(self):
        self.rule_tree.delete(*self.rule_tree.get_children())
        for r in self.rules:
            self.rule_tree.insert("", "end",
                                  values=(r["text"], r.get("kw", ""),
                                          r["on"], r["off"], r["delay"]))

    def _get_driver_cfg(self):
        # 指令后缀允许输入 \r\n、\n、\t 转义；不做 strip，避免剥掉结束符
        suffix = self.cmd_suffix_var.get()
        suffix = (suffix.replace("\\r\\n", "\r\n")
                        .replace("\\n", "\n")
                        .replace("\\t", "\t"))
        return {
            "send_mode": self.send_mode_var.get(),
            "serial_port": self.serial_port_var.get().strip(),
            "baudrate": self.baudrate_var.get().strip(),
            "tcp_host": self.tcp_host_var.get().strip(),
            "tcp_port": self.tcp_port_var.get().strip(),
            "cmd_prefix": self.cmd_prefix_var.get().strip(),
            "cmd_suffix": suffix,
        }

    def _apply_driver_cfg(self, cfg):
        self.send_mode_var.set(cfg.get("send_mode", "serial"))
        self.serial_port_var.set(str(cfg.get("serial_port", "COM3")))
        self.baudrate_var.set(str(cfg.get("baudrate", 115200)))
        self.tcp_host_var.set(str(cfg.get("tcp_host", "192.168.1.21")))
        self.tcp_port_var.set(str(cfg.get("tcp_port", 8085)))
        self.cmd_prefix_var.set(str(cfg.get("cmd_prefix", "001")))
        self.cmd_suffix_var.set(str(cfg.get("cmd_suffix", "")))

    def _refresh_active_settings(self):
        """主线程把界面设置快照成普通变量，供后台线程使用（Tk 变量不能跨线程读）。"""
        self._active_interval = max(float(self.interval_var.get()), 0.5)
        self._active_log = self.log_var.get()
        self._active_log_daily = bool(self.log_daily_var.get())
        try:
            self._active_confirm_polls = max(int(self.confirm_polls_var.get()), 1)
        except Exception:
            self._active_confirm_polls = 2
        self._active_driver_enabled = bool(self.driver_enabled_var.get())
        self._active_driver_cfg = self._get_driver_cfg()
        self._active_vision_enabled = bool(self.vision_enabled_var.get())
        self._active_vision_auto = bool(self.vision_auto_var.get())
        self._active_vision_cfg = self._get_vision_cfg()
        self._active_vision_log = self.vision_log_var.get()

    def _save_driver_settings(self):
        self._refresh_active_settings()
        self._save_config()
        self.driver_status_var.set("接口设置已保存")
        self.status_var.set("接口设置已保存")

    def _driver_tick(self, text):
        """按 OCR 文字更新规则状态：触发文字连续 DRIVER_CONFIRM_POLLS 轮命中才发指令。

        不做确认的话，OCR 半帧漏识别一轮就会让规则掉线再上线，导致 on 指令重复发送。
        """
        if not self._active_driver_enabled:
            return
        now = time.time()
        for i, rule in enumerate(self.rules):
            if i >= len(self._driver_state):
                continue
            st = self._driver_state[i]
            raw_hit = bool(rule["text"]) and rule["text"] in text
            st["text_streak"] = st.get("text_streak", 0) + 1 if raw_hit else 0
            st["text_hit"] = raw_hit and st["text_streak"] >= DRIVER_CONFIRM_POLLS
            self._update_rule_state(rule, st, now)
        self._fire_due_offs(now)

    def _driver_keyword_tick(self, description):
        """按视觉描述更新规则状态：描述包含画面关键词 → 触发指令（智能匹配）。"""
        if not self._active_driver_enabled:
            return
        now = time.time()
        for i, rule in enumerate(self.rules):
            if i >= len(self._driver_state):
                continue
            st = self._driver_state[i]
            st["kw_hit"] = bool(rule.get("kw")) and rule["kw"] in description
            self._update_rule_state(rule, st, now)
        self._fire_due_offs(now)

    def _update_rule_state(self, rule, st, now):
        hit = st["text_hit"] or st["kw_hit"]
        if hit and not st["active"]:
            st["active"] = True
            st["off_due"] = (now + rule["delay"]) if (rule["delay"] > 0 and rule["off"]) else None
            self._driver_send(rule, rule["on"])
        elif not hit and st["active"]:
            st["active"] = False

    def _fire_due_offs(self, now):
        for i, rule in enumerate(self.rules):
            if i >= len(self._driver_state):
                continue
            st = self._driver_state[i]
            if st["off_due"] is not None and now >= st["off_due"]:
                st["off_due"] = None
                self._driver_send(rule, rule["off"])

    def _driver_send(self, rule, code):
        """把指令放进发送队列即返回（实际发送在独立线程，串口/TCP 超时不阻塞监控）。"""
        if not code:
            return
        cfg = self._active_driver_cfg or self._get_driver_cfg()
        cmd = cfg.get("cmd_prefix", "") + code + cfg.get("cmd_suffix", "")
        self._send_queue.put((cmd, cfg, rule.get("text") or rule.get("kw", "")))

    def _sender_loop(self):
        """指令发送线程：逐条发送队列里的指令，成功/失败都写日志并更新界面状态。"""
        while True:
            cmd, cfg, label = self._send_queue.get()
            try:
                if cfg.get("send_mode") == "tcp":
                    send_command(cmd, cfg)
                else:
                    self._serial_send(cmd, cfg)
                msg = f"命中“{label}”→ 已发送指令 {cmd}"
                self._append_action_log(msg)
                self.msg_queue.put(("dstatus", datetime.now().strftime(
                    "%H:%M:%S") + " " + msg))
            except Exception as e:
                if self._serial is not None:       # 发送失败则重建串口连接
                    try:
                        self._serial.close()
                    except Exception:
                        pass
                    self._serial = None
                err = f"发送指令失败：{e}\n{traceback.format_exc()}"
                write_error_log(err)
                self.msg_queue.put(("error", f"发送指令失败：{e}（详见 程序错误日志.txt）"))

    def _serial_send(self, cmd, cfg):
        """串口发送：端口整个运行期保持打开，避免每次开关串口把 ESP32 复位。"""
        import serial
        key = (cfg.get("serial_port") or "COM3", str(cfg.get("baudrate") or 115200))
        if self._serial is not None and self._serial_key != key:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None
        if self._serial is None:
            ser = serial.Serial()
            ser.port = key[0]
            ser.baudrate = int(key[1])
            ser.timeout = 2
            ser.write_timeout = 2
            ser.open()
            # 打开后立即拉低 DTR/RTS，减小自动复位电路误触发 EN/GPIO0 的概率
            for attr in ("dtr", "rts"):
                try:
                    setattr(ser, attr, False)
                except Exception:
                    pass
            self._serial = ser
            self._serial_key = key
        self._serial.write(cmd.encode("utf-8"))
        self._serial.flush()

    def _append_action_log(self, msg):
        try:
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            append_text_capped(ACTION_LOG_FILE, f"[{stamp}] {msg}\n")
        except Exception:
            pass

    # ---------------- 规则增删改 ----------------
    def _rule_dialog(self, title, initial=None):
        """弹窗编辑一条规则，返回 dict 或 None。"""
        initial = initial or {}
        dlg = tk.Toplevel(self.root)
        dlg.title(title)
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.resizable(False, False)

        fields = [("触发文字（屏幕 OCR 文字包含它即命中，可留空）", "text"),
                  ("画面关键词（视觉描述包含它即命中，可留空）", "kw"),
                  ("命中指令（如 001on）", "on"),
                  ("关闭指令（如 001off，可留空）", "off"),
                  ("延时（秒）后发送关闭指令（0=不发送）", "delay")]
        vars_ = {}
        for row, (label, key) in enumerate(fields):
            tk.Label(dlg, text=label).grid(row=row, column=0, sticky="w", padx=10, pady=6)
            var = tk.StringVar(value=str(initial.get(key, "")))
            vars_[key] = var
            tk.Entry(dlg, textvariable=var, width=32).grid(
                row=row, column=1, sticky="w", padx=10, pady=6)

        result = {}

        def ok():
            text = vars_["text"].get().strip()
            kw = vars_["kw"].get().strip()
            if not text and not kw:
                messagebox.showwarning("提示", "触发文字和画面关键词至少填一个", parent=dlg)
                return
            result["text"] = text
            result["kw"] = kw
            result["on"] = vars_["on"].get().strip()
            result["off"] = vars_["off"].get().strip()
            try:
                result["delay"] = max(int(vars_["delay"].get().strip() or 0), 0)
            except ValueError:
                result["delay"] = 0
            dlg.destroy()

        def cancel():
            dlg.destroy()

        frm = tk.Frame(dlg)
        frm.grid(row=len(fields), column=0, columnspan=2, pady=10)
        tk.Button(frm, text="确定", width=10, command=ok).pack(side="left", padx=8)
        tk.Button(frm, text="取消", width=10, command=cancel).pack(side="left", padx=8)

        dlg.wait_window()
        return result or None

    def _on_add_rule(self):
        data = self._rule_dialog("添加规则")
        if data:
            self.rules.append(data)
            self._driver_state.append(self._new_rule_state())
            save_rules(self.rules)
            self._refresh_rule_tree()
            self.status_var.set(f"已添加规则：“{data['text'] or data['kw']}”")

    def _on_edit_rule(self):
        sel = self.rule_tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先在列表中选中一条规则")
            return
        idx = self.rule_tree.index(sel[0])
        data = self._rule_dialog("编辑规则", self.rules[idx])
        if data:
            self.rules[idx] = data
            self._driver_state[idx] = self._new_rule_state()
            save_rules(self.rules)
            self._refresh_rule_tree()
            self.status_var.set(f"已修改规则：“{data['text'] or data['kw']}”")

    def _on_del_rule(self):
        sel = self.rule_tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先在列表中选中一条规则")
            return
        idx = self.rule_tree.index(sel[0])
        if messagebox.askyesno("删除确认", f"确定删除规则“{self.rules[idx]['text'] or self.rules[idx].get('kw', '')}”吗？"):
            del self.rules[idx]
            del self._driver_state[idx]
            save_rules(self.rules)
            self._refresh_rule_tree()
            self.status_var.set("已删除规则")

    def _reload_rules(self):
        self._load_driver_rules()
        self.driver_status_var.set(f"已从 指令规则.txt 重新加载 {len(self.rules)} 条规则")

    def _test_send_selected(self):
        sel = self.rule_tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先在列表中选中一条规则")
            return
        idx = self.rule_tree.index(sel[0])
        rule = self.rules[idx]
        self._refresh_active_settings()
        self._driver_send(rule, rule["on"])

    # ================= DeepSeek 视觉 =================
    def _get_vision_cfg(self):
        return {
            "api_key": self.vision_key_var.get().strip(),
            "base_url": self.vision_url_var.get().strip(),
            "model": self.vision_model_var.get().strip(),
        }

    def test_vision(self):
        if not self.region:
            messagebox.showwarning("提示", "请先框选监控区域")
            return
        self._refresh_active_settings()
        threading.Thread(target=self._vision_worker,
                         args=(None, True, self._get_vision_cfg(),
                               self.vision_log_var.get()),
                         daemon=True).start()

    def _vision_worker(self, img=None, manual=True, cfg=None, log_path=None):
        """后台线程：截图（如需）→ 调 DeepSeek 视觉 API → 写记录 + 智能指令匹配。

        manual=False 是监控自动触发：同一时刻只允许一个请求，且有最小间隔冷却，
        避免评论区高频变化时请求堆积（费用/限流/重复触发关键词指令）。
        """
        with self._vision_lock:
            if self._vision_busy:
                if manual:
                    self.msg_queue.put(("vstatus", "上一次视觉请求还在进行，请稍候…"))
                return
            if not manual and time.time() - self._vision_ts < VISION_COOLDOWN_SECONDS:
                return
            self._vision_busy = True
            self._vision_ts = time.time()
        try:
            if cfg is None:
                cfg = self._get_vision_cfg()
            if not cfg.get("api_key"):
                self.msg_queue.put(("error", "请先在“DeepSeek 视觉”页签填写 API Key"))
                return
            if img is None:
                img = capture_region(*self.region)
            self.msg_queue.put(("vstatus", "正在调用 DeepSeek 视觉模型…"))
            desc = vision_describe(img, cfg)
            self._append_vision_log(desc, log_path)
            self._driver_keyword_tick(desc)          # 画面关键词智能匹配
            self.msg_queue.put(("vdesc", desc))
            self.msg_queue.put(("vstatus", datetime.now().strftime(
                "%H:%M:%S") + " 视觉描述完成"))
        except Exception as e:
            err = f"视觉理解失败：{e}\n{traceback.format_exc()}"
            write_error_log(err)
            self.msg_queue.put(("error", f"视觉理解失败：{e}（详见 程序错误日志.txt）"))
        finally:
            self._vision_busy = False

    def _append_vision_log(self, desc, path=None):
        try:
            path = Path(path or self.vision_log_var.get()).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"[{stamp}] 视觉描述：\n{desc}\n\n")
        except Exception:
            pass

    # ================= 记录文件 =================
    def choose_log_file(self):
        path = filedialog.asksaveasfilename(
            title="选择记录文件",
            defaultextension=".txt",
            initialfile="屏幕文字记录.txt",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")])
        if path:
            self.log_var.set(path)
            self._save_config()

    def open_log_file(self):
        path = Path(self.log_var.get()).expanduser()
        daily = self._active_log_daily if self.running else bool(self.log_daily_var.get())
        if daily:
            path = path.with_name(
                f"{path.stem}_{datetime.now():%Y-%m-%d}{path.suffix}")
        if not path.exists():
            messagebox.showinfo("提示", f"记录文件还不存在：\n{path}\n\n开始监控并出现文字变化后会自动创建。")
            return
        try:
            os.startfile(str(path))  # noqa: 用系统默认程序（记事本）打开
        except Exception as e:
            messagebox.showerror("错误", f"无法打开文件：{e}")

    # ================= 配置持久化 =================
    def _load_config(self):
        try:
            if CONFIG_FILE.exists():
                cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                r = cfg.get("region")
                if isinstance(r, (list, tuple)) and len(r) == 4:
                    x, y, w, h = (int(v) for v in r)
                    self.region = (x, y, w, h)
                    self.region_var.set(f"坐标 ({x}, {y})   宽 {w} × 高 {h}")
                try:
                    self.interval_var.set(float(cfg.get("interval", 0.5)))
                except Exception:
                    pass
                self.log_daily_var.set(bool(cfg.get("log_daily", False)))
                try:
                    self.confirm_polls_var.set(max(int(cfg.get("confirm_polls", 2)), 1))
                except Exception:
                    pass
                lp = cfg.get("log_file")
                if lp:
                    self.log_var.set(lp)
                d = cfg.get("driver") or {}
                self.driver_enabled_var.set(bool(d.get("enabled", True)))
                self._apply_driver_cfg(d)
                v = cfg.get("vision") or {}
                self.vision_enabled_var.set(bool(v.get("enabled", False)))
                self.vision_auto_var.set(bool(v.get("auto", True)))
                self.vision_key_var.set(v.get("api_key", ""))
                self.vision_url_var.set(v.get("base_url", "https://api.deepseek.com"))
                self.vision_model_var.set(v.get("model", "deepseek-v4-pro"))
                if v.get("log_file"):
                    self.vision_log_var.set(v["log_file"])
                self._vision_enabled_changed()
                a = cfg.get("api") or {}
                self.api_enabled_var.set(bool(a.get("enabled", True)))
                try:
                    self.api_port_var.set(str(int(a.get("port", API_DEFAULT_PORT))))
                except Exception:
                    self.api_port_var.set(str(API_DEFAULT_PORT))
                self.api_lan_var.set(
                    str(a.get("bind", "127.0.0.1")).strip() == "0.0.0.0")
                self.api_token_var.set(str(a.get("token", "") or ""))
                self.api_allow_cmd_var.set(bool(a.get("allow_command", False)))
                try:
                    mr = int(a.get("max_records", API_DEFAULT_MAX_RECORDS))
                except Exception:
                    mr = API_DEFAULT_MAX_RECORDS
                self.api_maxrec_var.set(str(mr))
                try:
                    ma = int(a.get("max_age_minutes", API_DEFAULT_MAX_AGE_MINUTES))
                except Exception:
                    ma = API_DEFAULT_MAX_AGE_MINUTES
                self.api_state.resize(mr, ma)
        except Exception:
            pass

    def _save_config(self):
        try:
            cfg = {
                "region": list(self.region) if self.region else None,
                "interval": self.interval_var.get(),
                "log_file": self.log_var.get(),
                "log_daily": bool(self.log_daily_var.get()),
                "confirm_polls": int(self.confirm_polls_var.get()),
                "driver": {
                    "enabled": bool(self.driver_enabled_var.get()),
                    "send_mode": self.send_mode_var.get(),
                    "serial_port": self.serial_port_var.get().strip(),
                    "baudrate": self.baudrate_var.get().strip(),
                    "tcp_host": self.tcp_host_var.get().strip(),
                    "tcp_port": self.tcp_port_var.get().strip(),
                    "cmd_prefix": self.cmd_prefix_var.get().strip(),
                    "cmd_suffix": self.cmd_suffix_var.get(),
                },
                "vision": {
                    "enabled": bool(self.vision_enabled_var.get()),
                    "auto": bool(self.vision_auto_var.get()),
                    "api_key": self.vision_key_var.get().strip(),
                    "base_url": self.vision_url_var.get().strip(),
                    "model": self.vision_model_var.get().strip(),
                    "log_file": self.vision_log_var.get(),
                },
                "api": {
                    "enabled": bool(self.api_enabled_var.get()),
                    "port": self._api_port_or_default(),
                    "bind": "0.0.0.0" if self.api_lan_var.get() else "127.0.0.1",
                    "token": self.api_token_var.get().strip(),
                    "allow_command": bool(self.api_allow_cmd_var.get()),
                    "max_records": self._api_maxrec_or_default(),
                    "max_age_minutes": self.api_state.max_age_ms // 60000,
                },
            }
            CONFIG_FILE.write_text(
                json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    # ================= 消息队列 -> 界面 =================
    def _poll_queue(self):
        try:
            while True:
                msg = self.msg_queue.get_nowait()
                if not msg:
                    continue
                kind, text = msg[0], (msg[1] if len(msg) > 1 else "")
                if kind == "status":
                    self.status_var.set(text)
                elif kind == "last":
                    self._set_text(text)
                elif kind == "dstatus":
                    self.driver_status_var.set(text)
                    self.status_var.set(text)
                elif kind == "vdesc":
                    self._set_vision_text(text)
                elif kind == "vstatus":
                    self.vision_status_var.set(text)
                    self.status_var.set(text)
                elif kind == "error":
                    self.status_var.set(text)
                    self._set_text(text)
                elif kind == "monitor_stopped":
                    self.running = False
                    self._set_running_ui(False)
                    self.status_var.set("已停止监控")
        except queue.Empty:
            pass
        self.root.after(200, self._poll_queue)

    def _set_text(self, content):
        self.txt.config(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.insert("1.0", content)
        self.txt.config(state="disabled")

    def _on_close(self):
        self.running = False
        t = self.monitor_thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)          # 等监控线程写完手头的记录再退出
        try:
            self.api_server.stop()       # 先停接口，避免退出期间还有请求进来
        except Exception:
            pass
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
        self._save_config()
        self.root.destroy()


# ============================================================
# 命令行自检：不联网、不开窗口，验证离线 OCR 可用
# ============================================================
def selftest():
    _setup_logging()
    logging.getLogger().info("selftest start")
    from PIL import Image, ImageDraw, ImageFont
    print("正在加载离线 OCR 模型…")
    ocr = create_ocr()

    img = Image.new("RGB", (640, 120), "white")
    draw = ImageDraw.Draw(img)
    font = None
    for name in ("msyh.ttc", "simhei.ttf", "arial.ttf"):
        try:
            font = ImageFont.truetype(name, 40)
            break                    # 用第一个可用的中文字体，别继续往后覆盖成 arial
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()
    draw.text((20, 30), "屏幕文字识别 123456", fill="black", font=font)

    text = ocr_image(ocr, img)
    print("OCR 识别结果：", repr(text))
    if text and ("123456" in text or "屏幕" in text):
        print("自检通过：离线 OCR 引擎可正常工作。")
        return 0
    print("自检完成：引擎已成功运行（识别内容可能受字体影响）。")
    return 0


def main():
    _setup_logging()
    _install_excepthook()
    logging.getLogger().info("程序启动 build=20260907-opt2 args=%s", sys.argv[1:])
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if "--diag" in sys.argv:
        rep = APP_DIR / "diag_report.txt"
        out = []
        for path in (LOG_FILE, ERROR_LOG_FILE):
            out.append("===== " + path.name + " 尾部 30 行 =====")
            if path.exists():
                try:
                    tail = open(path, encoding="utf-8", errors="replace").read().splitlines()[-30:]
                    out.extend(tail)
                except Exception as e:
                    out.append("读取失败: " + str(e))
            else:
                out.append("(文件不存在)")
        rep.write_text("\n".join(out), encoding="utf-8")
        print("诊断报告已生成：" + str(rep))
        print("请把该文件或 logs\\app.log 尾部 30 行发给开发者。")
        sys.exit(0)
    if not _acquire_single_instance():
        # 已有实例在运行：弹窗提示后退出，避免双开同时写记录/抢串口
        try:
            ctypes.windll.user32.MessageBoxW(
                0, "屏幕文字识别监控已经在运行了（请看任务栏）。\n"
                   "不要重复启动；要重启请先关掉已有窗口或运行 stop.bat。",
                "提示", 0x40)
        except Exception:
            pass
        logging.getLogger().info("重复启动被拦截（已有实例在运行）")
        return
    root = tk.Tk()

    def _report(exc_type, exc_value, exc_tb):
        write_error_log("".join(traceback.format_exception(exc_type, exc_value, exc_tb)))
        try:
            root.after(0, lambda: messagebox.showerror("程序错误", "发生未处理错误。\n\n请把日志发给开发者：\n" + str(LOG_FILE) + "\n\n详情：" + str(exc_value)))
        except Exception:
            pass

    root.report_callback_exception = _report
    ScreenTextMonitorApp(root)
    logging.getLogger().info("UI 启动完成")
    root.mainloop()


if __name__ == "__main__":
    main()