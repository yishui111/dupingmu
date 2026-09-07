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

import ctypes
import json
import os
import queue
import socket
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
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
        self.nb.add(self.tab_monitor, text="  屏幕监控  ")
        self.nb.add(self.tab_driver, text="  文字驱动器  ")
        self.nb.add(self.tab_vision, text="  DeepSeek 视觉  ")
        self._build_monitor_tab()
        self._build_driver_tab()
        self._build_vision_tab()

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
            candidates = {}                  # 待确认的新文字行 -> 已连续出现次数
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

                        # 已不在画面里的候选作废（跳过 OCR 半帧/抖动产生的残影）
                        present = {ln for ln, n in cur_counts.items() if n > 0}
                        candidates = {c: n for c, n in candidates.items()
                                      if c in present}
                        for a in additions:              # 新冒出的行加入候选
                            candidates.setdefault(a, 0)
                        for c in candidates:             # 每轮识别算一次“在场”
                            candidates[c] += 1

                        confirmed = [c for c, n in candidates.items()
                                     if n >= confirm_polls]
                        for c in confirmed:
                            candidates.pop(c, None)
                        if confirmed:
                            self._append_log("\n".join(confirmed))
                            self.msg_queue.put(("status", datetime.now().strftime(
                                "%H:%M:%S") + " 新增文字已写入记录文件"))
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