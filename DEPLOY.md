# 屏幕文字识别监控 · 部署说明（DEPLOY）

## 一、这是什么
框选屏幕区域 → 离线 OCR 识别文字变化并记录；可按"屏幕文字→指令"规则自动给 ESP32 发指令；可选 DeepSeek 视觉看懂画面。详见 README.md。

## 二、源码运行（开发者方式）
```bat
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
start.bat        :: 或直接双击 start.bat / 启动(源码).bat
```
自检（不弹窗验证 OCR）：`python main.py --selftest`

## 三、打包成免安装 exe（换电脑用打包版）
1. 双击 `build.bat`（首次联网下依赖，之后可离线）→ 产物在 `dist\屏幕文字识别监控\`
2. 整个文件夹复制到任意 Windows 电脑，双击 `屏幕文字识别监控.exe` 即用，目标机**无需装 Python、无需联网**

## 四、ESP32 端
- 固件源码：`esp32\esp32_switch.ino`（Arduino IDE 打开烧录；接线与说明见 `esp32\README.md`）
- 电脑与 ESP32 连同一 WiFi，发送方式选 `tcp`，主机填 ESP32 的 IP（默认端口 8080）
- arduino 离线工具链（第三方下载件）体积大，未入库；需要时从原作者机器 `esp32\esp32_offline_pack\` 复制或按 `esp32\README.md` 自行准备

## 五、配置
- 首次运行自动生成 `config.json`；本仓库只提供 `config.json.example` 模板
- 常用配置：`interval` 识别间隔、`confirm_polls` 新文字确认轮数（1=出现即记）、`log_daily` 记录按日期分文件
- DeepSeek API Key 填在程序界面（明文存本地 config.json，注意文件夹保密，勿提交仓库）

## 六、环境要求
- 源码运行：Windows + Python 3.12
- 打包版：任意 Windows（无需 Python）
- ESP32：Arduino IDE（可选，只用"屏幕文字监控"可不用）
