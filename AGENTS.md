# AGENTS.md — 屏幕文字识别监控（dupingmu）项目档案

> ⚠️ 修改本仓库前先读本文件（AI 助手/开发者项目记忆）。用户向文档见 README.md / DEPLOY.md。

## 1. 定位
Windows 便携工具：框选屏幕区域 → 离线 OCR（RapidOCR+ONNX）监控文字变化 → 写记录 txt（新文字需连续 N 轮确认，默认 2，界面可调；可选按日期分文件）；按「屏幕文字→指令」规则经串口/TCP 给 ESP32 发开关指令（触发防抖、串口常驻连接、发送走独立线程）；可选 DeepSeek 视觉"看图说话"（自动调用有 30 秒冷却）。全程 OCR 离线、数据不出本机。

## 2. 结构
| 文件/目录 | 说明 |
| ---- | ---- |
| main.py | 全部主程序（约 70KB，含界面/OCR/驱动/视觉/打包入口） |
| build.bat | 打包 exe（产物 dist\屏幕文字识别监控\） |
| requirements.txt | 运行依赖（rapidocr/onnxruntime/mss/Pillow/numpy/pyserial/requests） |
| config.json.example | 配置模板（真实 config.json 运行时生成，不入库） |
| 指令规则.txt | 规则文件（触发文字\|命中指令\|关闭指令\|延时秒\|画面关键词） |
| esp32\esp32_switch.ino | ESP32 固件（8 路开关，GPIO 见规则注释） |
| esp32\README.md | 固件说明 |

## 3. 公开版边界（不入库）
.venv/、dist/（打包产物）、config.json（本机路径/区域/接口设置）、程序错误日志.txt、指令发送记录.txt、屏幕文字记录.txt、屏幕画面描述.txt、开发日志.md（个人开发笔记）、esp32_offline_pack/（arduino 第三方离线工具链，几百 MB）等。
> DeepSeek API Key 只在运行时填进本地 config.json，绝不入库。

## 4. 维护约定
- 改代码后同步更新 README.md/DEPLOY.md/本文件；打包后产物不入库
- 提交：`git add <文件>` → `git commit` → `git push`（默认推到备份远端，勿 `git add -A`）
- 中文 UTF-8；bat 纯 ASCII + CRLF + 无 BOM
