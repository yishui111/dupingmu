# -*- coding: utf-8 -*-
"""ESP32 日志查看器：打开串口实时显示 ESP32 打印的日志。"""
import sys
import time

try:
    import serial
except ImportError:
    print("缺少 pyserial，请先运行: pip install pyserial")
    input("按回车退出...")
    sys.exit(1)

port = sys.argv[1] if len(sys.argv) > 1 else "COM5"
try:
    ser = serial.Serial(port, 115200, timeout=0.2)
except Exception as e:
    print(f"打不开串口 {port}: {e}")
    print("请检查：串口号是否正确、USB 是否插好、是否被其他程序占用（如串口监视器）")
    input("按回车退出...")
    sys.exit(1)

print(f"== ESP32 日志查看器 ==  端口: {port}   按 Ctrl+C 退出")
print("=" * 50)
try:
    while True:
        data = ser.read(4096)
        if data:
            sys.stdout.write(data.decode("utf-8", errors="replace"))
            sys.stdout.flush()
except KeyboardInterrupt:
    print("\n已退出")
finally:
    ser.close()
