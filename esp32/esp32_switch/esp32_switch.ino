/*
  屏幕文字监控 · 文字驱动器 —— ESP32-C6 端固件（带完整运行日志）
  ==============================================================
  三大功能：
  1. WiFi 配网（网页弹窗填 WiFi）：未保存 WiFi 时开热点 ESP32-Switch-Config，
     手机连上后配网页自动弹出，填 WiFi 名密码点保存，自动连接并记住。
  2. 网页开关控制：连上 WiFi 后，手机/电脑浏览器打开 http://<IP>/，
     页面有 8 个开关按钮，点击开/关对应继电器（测试接线用）。
  3. 屏幕文字联动（TCP 指令）：电脑端"屏幕文字监控"程序识别屏幕文字后
     发指令（如 001001on / 001001off），本固件解析并控制继电器。

  运行日志：所有关键动作都会通过串口打印（见 LOG），
  在 Arduino IDE 里：工具 → 串口监视器（波特率 115200）即可查看。

  指令格式（与电脑端程序一致）：
    设备号(3位) + 开关号(3位) + 动作(on/off)
    例：001001on → 设备001 的 1号开关 打开；001001off → 1号开关 关闭
    电脑端实际发送 = 指令前缀(001) + 命中指令(001on) = 001001on
    也兼容简写：001on（不带设备号）

  烧录与看日志（Arduino IDE）：
    1. 工具 → 开发板 → ESP32C6 Dev Module
    2. 工具 → USB CDC On Boot → Enabled   （让日志走 USB，串口监视器能看到）
    3. 工具 → 端口 → 选 ESP32 的 COM 口
    4. 点"上传"
    5. 工具 → 串口监视器（115200）看日志

  重新配网：按住板子 BOOT 键再上电。

  接线（已按你的实际接线设置）：
    继电器1~8 -> GPIO 12, 11, 10, 8, 1, 0, 7, 6
    注意：GPIO12 是 USB 串口引脚之一，继电器1用它与 USB 共用，若有异常可换引脚
*/

#include <WiFi.h>
#include <WiFiServer.h>
#include <WebServer.h>
#include <DNSServer.h>
#include <ESPmDNS.h>
#include <Preferences.h>

// ================= 可修改配置 =================
const char* DEVICE_ID     = "001";        // 本机设备号（指令前3位与之相同才执行）
const uint16_t TCP_PORT   = 8085;         // 电脑端程序连的指令端口
const char* AP_SSID       = "ESP32-Switch-Config";   // 配网热点名称
const char* AP_PASS       = "";           // 热点密码（留空 = 无需密码）
const uint32_t WIFI_TIMEOUT = 30000;      // 连接 WiFi 超时(毫秒)

// ====== WiFi 写死配置（开机自动连，不用配网） ======
// 填好 SSID/密码后，开机直接连这个 WiFi；留空则用配网页保存的
const char* WIFI_SSID     = "wdwf";
const char* WIFI_PASSWORD = "wdwf1234";
// =============================================
// =============================================

// BOOT 键引脚：ESP32-C6 在 GPIO9，经典 ESP32 / S3 在 GPIO0（按你板子改）
#define BOOT_PIN 9

// 开关号 -> GPIO 引脚（数组第0个=开关1，第1个=开关2 ...）
// 重要：ESP32-C6 的 GPIO12/13 是 USB 数据线，【不能】接继电器，否则 USB 会失效！
// 继电器1 已从 GPIO12 改到 GPIO14（请把继电器1的信号线从 GPIO12 挪到 GPIO14）
const int switch_pins[] = {14, 11, 10, 8, 1, 0, 7, 6};
const int SWITCH_COUNT  = sizeof(switch_pins) / sizeof(switch_pins[0]);

// 继电器触发方式：你的继电器实测是【高电平触发】（IN 给高电平才吸合），
// 所以这里必须是 false；如果是低电平触发模块改成 true
const bool RELAY_ACTIVE_LOW = false;

// ============ 日志宏：带毫秒时间戳输出到串口 ============
#define LOG(fmt, ...) do { \
    Serial.printf("[%lu] " fmt "\r\n", (unsigned long)millis(), ##__VA_ARGS__); \
  } while (0)

WiFiServer server(TCP_PORT);     // 电脑端程序指令 TCP
WebServer web(80);               // 配网页 / 开关控制页
DNSServer dns;
Preferences prefs;

enum Mode { MODE_SWITCH, MODE_CONFIG };
Mode mode = MODE_SWITCH;

String scanOptions = "";         // 配网页面里的 WiFi 列表

// ---------------------------------------------------------------
void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println();
  LOG("======================================");
  LOG("== ESP32-C6 开关控制器 v3.1 启动 ==");
  LOG("======================================");
  LOG("设备号: %s  继电器数量: %d", DEVICE_ID, SWITCH_COUNT);
  for (int i = 0; i < SWITCH_COUNT; i++) {
    pinMode(switch_pins[i], OUTPUT);
    setSwitch(i + 1, false);              // 上电全部关闭
    LOG("开关%d -> GPIO%d (已初始化)", i + 1, switch_pins[i]);
  }

  // BOOT 键：上电时按住 → 强制进入配网模式
  pinMode(BOOT_PIN, INPUT_PULLUP);
  delay(50);
  bool forceConfig = (digitalRead(BOOT_PIN) == LOW);
  LOG("BOOT 键状态: %s", forceConfig ? "按下（强制配网）" : "未按下");

  prefs.begin("wifi", false);
  String ssid = prefs.getString("ssid", "");
  String pass = prefs.getString("pass", "");

  // 页面保存的 WiFi 优先；没有则用写死的默认 WiFi（wdwf）
  if (ssid.length() == 0 && WIFI_SSID[0] != '\0') {
    ssid = WIFI_SSID;
    pass = WIFI_PASSWORD;
    LOG("使用写死的默认 WiFi: %s", WIFI_SSID);
  } else {
    LOG("使用已保存的 WiFi: %s", ssid.length() > 0 ? ssid.c_str() : "(无)");
  }

  if (!forceConfig && ssid.length() > 0) {
    if (tryConnect(ssid, pass)) {
      startSwitchMode();
      return;
    }
    LOG("使用 WiFi 连接失败，进入配网模式");
  }
  startConfigMode();
}

void loop() {
  if (mode == MODE_CONFIG) {
    dns.processNextRequest();
    web.handleClient();
    delay(5);
  } else {
    static unsigned long lastWifiLog = 0;
    if (WiFi.status() != WL_CONNECTED) {
      if (millis() - lastWifiLog > 10000) {
        LOG("WiFi 断开，尝试重连... (状态=%d)", WiFi.status());
        lastWifiLog = millis();
      }
      WiFi.reconnect();
      delay(3000);
      if (WiFi.status() == WL_CONNECTED) {
        LOG("WiFi 重连成功！IP: %s", WiFi.localIP().toString().c_str());
      }
      return;
    }
    handleSwitchClient();       // 电脑端 TCP 指令
    web.handleClient();         // 网页开关控制
    delay(5);
  }
}

// ---------------------------------------------------------------
// WiFi 连接
// ---------------------------------------------------------------
bool tryConnect(String ssid, String pass) {
  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid.c_str(), pass.c_str());
  LOG("正在连接 WiFi: %s ...", ssid.c_str());
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < WIFI_TIMEOUT) {
    delay(300);
  }
  if (WiFi.status() == WL_CONNECTED) {
    LOG("WiFi 已连接！IP: %s  信号: %d dBm",
        WiFi.localIP().toString().c_str(), WiFi.RSSI());
    return true;
  }
  LOG("WiFi 连接超时失败（%lu ms）", (unsigned long)WIFI_TIMEOUT);
  return false;
}

// ---------------------------------------------------------------
// 配网模式：开热点 + 配置网页（手机连上后自动弹出）
// ---------------------------------------------------------------
void startConfigMode() {
  mode = MODE_CONFIG;
  WiFi.mode(WIFI_AP);
  WiFi.softAP(AP_SSID, AP_PASS);
  LOG("配网模式已开启：热点 [%s]，配置页 http://192.168.4.1", AP_SSID);

  int n = WiFi.scanNetworks();
  LOG("扫描到 %d 个 WiFi", n);
  scanOptions = "";
  for (int i = 0; i < n && i < 15; i++) {
    String s = WiFi.SSID(i);
    if (s.length() > 0) {
      scanOptions += "<option value=\"" + s + "\">" + s + " (" + String(WiFi.RSSI(i)) + " dBm)</option>\n";
    }
  }

  web.on("/", HTTP_GET, handleRoot);
  web.on("/save", HTTP_POST, handleSave);
  web.onNotFound([]() {
    LOG("配网模式收到未知请求: %s → 重定向 /", web.uri().c_str());
    web.sendHeader("Location", "/", true);
    web.send(302, "text/html", "");
  });
  web.begin();
  LOG("配网 HTTP 服务已启动（端口 80）");

  dns.start(53, "*", WiFi.softAPIP());
  LOG("DNS 劫持已启动（手机可自动弹出配网页）");
}

void handleRoot() {
  LOG("配网页被访问");
  String html = "<!DOCTYPE html><html><head><meta charset='utf-8'>"
    "<meta name='viewport' content='width=device-width, initial-scale=1'>"
    "<title>ESP32 配网</title>"
    "<style>"
    "body{font-family:'Microsoft YaHei',Arial,sans-serif;max-width:420px;margin:30px auto;padding:0 16px;background:#f5f6fa}"
    "h2{color:#1a66cc}.box{background:#fff;border-radius:10px;padding:20px;box-shadow:0 2px 8px rgba(0,0,0,.1)}"
    "label{display:block;margin:12px 0 4px;color:#333}input,select{width:100%;box-sizing:border-box;padding:9px;"
    "border:1px solid #ccc;border-radius:6px;font-size:15px}"
    "button{width:100%;margin-top:18px;padding:11px;background:#1a66cc;color:#fff;border:none;border-radius:6px;font-size:16px;cursor:pointer}"
    ".tip{color:#888;font-size:13px;margin-top:10px}</style></head><body>"
    "<h2>ESP32 开关控制器 · WiFi 配置</h2>"
    "<div class='box'>"
    "<form method='POST' action='/save'>"
    "<label>选择家里的 WiFi（没看到就手动输入）：</label>"
    "<select name='ssid'><option value=''>-- 选择 WiFi --</option>" + scanOptions + "</select>"
    "<label>WiFi 名称（手动输入）：</label>"
    "<input name='ssid_manual' placeholder='如 myhome_wifi'>"
    "<label>WiFi 密码：</label>"
    "<input type='password' name='password' placeholder='请输入 WiFi 密码'>"
    "<button type='submit'>保存并连接</button>"
    "</form><div class='tip'>保存后设备会自动重启并连接你家 WiFi，连上后本热点会关闭。</div>"
    "</div></body></html>";
  web.send(200, "text/html; charset=utf-8", html);
}

void handleSave() {
  String ssid = web.arg("ssid");
  if (ssid.length() == 0) ssid = web.arg("ssid_manual");
  String pass = web.arg("password");
  ssid.trim();
  pass.trim();
  LOG("收到配网提交: ssid=%s 密码长度=%d", ssid.c_str(), pass.length());
  if (ssid.length() == 0) {
    LOG("配网失败：WiFi 名称为空");
    web.send(200, "text/html; charset=utf-8",
             "<h3 style='color:red'>WiFi 名称不能为空</h3><a href='/'>返回重新填写</a>");
    return;
  }
  prefs.putString("ssid", ssid);
  prefs.putString("pass", pass);
  LOG("WiFi 已保存到闪存: %s，即将重启连接...", ssid.c_str());
  web.send(200, "text/html; charset=utf-8",
           "<!DOCTYPE html><html><head><meta charset='utf-8'></head><body>"
           "<h3>已保存！正在连接 " + ssid + " ...</h3>"
           "<p>设备将自动重启并连上你家 WiFi（热点会关闭）。<br>"
           "连接成功后，打开 http://esp32.local/ 或到串口监视器看 IP 地址。</p></body></html>");
  delay(800);
  LOG("重启中...");
  ESP.restart();
}

// ---------------------------------------------------------------
// 开关控制模式：网页控制页 + mDNS + TCP 指令服务器
// ---------------------------------------------------------------
void startSwitchMode() {
  mode = MODE_SWITCH;
  LOG("进入工作模式（WiFi 已连接）");

  if (MDNS.begin("esp32")) {
    LOG("mDNS 已开启：esp32.local");
  }

  web.on("/", HTTP_GET, handleControlPage);
  web.on("/toggle", HTTP_GET, handleToggle);
  web.on("/status", HTTP_GET, handleStatus);
  web.on("/wifisave", HTTP_POST, handleWifiSave);
  web.onNotFound([]() {
    LOG("工作模式收到未知请求: %s → 重定向 /", web.uri().c_str());
    web.sendHeader("Location", "/", true);
    web.send(302, "text/html", "");
  });
  web.begin();
  LOG("网页控制服务已启动: http://%s/", WiFi.localIP().toString().c_str());

  server.begin();
  LOG("TCP 指令服务已启动: 端口 %d（电脑端程序用）", TCP_PORT);
  LOG("======================================");
  LOG("就绪！手机控制页 http://%s/  指令示例 001001on", WiFi.localIP().toString().c_str());
  LOG("======================================");
}

void handleControlPage() {
  LOG("控制页被访问");
  String html = "<!DOCTYPE html><html><head><meta charset='utf-8'>"
    "<meta name='viewport' content='width=device-width, initial-scale=1'>"
    "<title>ESP32 开关控制</title>"
    "<style>"
    "body{font-family:'Microsoft YaHei',sans-serif;max-width:520px;margin:20px auto;padding:0 14px;background:#f5f6fa}"
    "h2{color:#1a66cc;text-align:center;margin-bottom:6px}"
    ".sub{color:#888;text-align:center;font-size:13px;margin-bottom:16px}"
    ".grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}"
    ".btn{display:block;text-align:center;padding:24px 0;border-radius:12px;font-size:19px;text-decoration:none;"
    "box-shadow:0 2px 6px rgba(0,0,0,.15);color:#fff;font-weight:bold}"
    ".on{background:#27ae60}.off{background:#bdc3c7;color:#555}"
    ".btn small{display:block;font-size:12px;font-weight:normal;margin-top:4px;opacity:.85}"
    "</style></head><body>"
    "<h2>ESP32 开关控制器</h2>"
    "<div class='sub'>点击按钮测试对应继电器 · IP: " + WiFi.localIP().toString() + "</div>"
    "<div class='grid'>";

  for (int i = 0; i < SWITCH_COUNT; i++) {
    bool on = isSwitchOn(i + 1);
    String stateCls = on ? "on" : "off";
    String stateTxt = on ? "开" : "关";
    html += "<a class='btn " + stateCls + "' href='/toggle?sw=" + String(i + 1) + "'>"
            + "开关" + String(i + 1)
            + "<small>GPIO" + String(switch_pins[i]) + " · " + stateTxt + "</small></a>";
  }

  html += "</div><div class='sub' style='margin-top:14px'>屏幕文字联动：电脑端程序通过 TCP 端口 "
          + String(TCP_PORT) + " 发送指令（如 001001on）</div>";

  // ---- WiFi 设置区（开关页上直接改 WiFi）----
  String curSsid = WiFi.SSID();
  html += "<div style='margin-top:20px;background:#fff;border-radius:10px;padding:16px;"
          "box-shadow:0 2px 8px rgba(0,0,0,.1)'>"
          "<div style='font-weight:bold;color:#1a66cc;margin-bottom:8px'>WiFi 设置</div>"
          "<form method='POST' action='/wifisave'>"
          "<label style='display:block;margin:6px 0 3px;color:#333'>WiFi 名称(SSID)：</label>"
          "<input name='ssid' value='" + curSsid + "' "
          "style='width:100%;box-sizing:border-box;padding:8px;border:1px solid #ccc;border-radius:6px;font-size:15px'>"
          "<label style='display:block;margin:6px 0 3px;color:#333'>WiFi 密码：</label>"
          "<input type='password' name='password' placeholder='新密码（不修改可留空）' "
          "style='width:100%;box-sizing:border-box;padding:8px;border:1px solid #ccc;border-radius:6px;font-size:15px'>"
          "<button type='submit' style='width:100%;margin-top:12px;padding:10px;background:#1a66cc;color:#fff;"
          "border:none;border-radius:6px;font-size:15px;cursor:pointer'>保存 WiFi 并重启</button>"
          "</form>"
          "<div style='color:#888;font-size:12px;margin-top:8px'>当前连接: " + curSsid
          + " · 修改后设备自动重启并连接新 WiFi</div>"
          "</div>";
  html += "</body></html>";
  web.send(200, "text/html; charset=utf-8", html);
}

void handleToggle() {
  int sw = web.arg("sw").toInt();
  if (sw >= 1 && sw <= SWITCH_COUNT) {
    bool on = isSwitchOn(sw);
    setSwitch(sw, !on);
    LOG("网页操作: 开关%d (GPIO%d) → %s", sw, switch_pins[sw - 1], on ? "关" : "开");
  } else {
    LOG("网页操作: 无效开关号 %d", sw);
  }
  web.sendHeader("Location", "/", true);
  web.send(302, "text/html", "");
}

void handleStatus() {
  String json = "{";
  for (int i = 0; i < SWITCH_COUNT; i++) {
    if (i > 0) json += ",";
    json += "\"sw" + String(i + 1) + "\":" + String(isSwitchOn(i + 1) ? 1 : 0);
  }
  json += "}";
  LOG("状态查询被访问: %s", json.c_str());
  web.send(200, "application/json", json);
}

// 开关页上的 WiFi 修改：保存后重启连接新 WiFi
void handleWifiSave() {
  String ssid = web.arg("ssid");
  String pass = web.arg("password");
  ssid.trim();
  pass.trim();
  LOG("控制页收到 WiFi 修改: ssid=%s 密码长度=%d", ssid.c_str(), pass.length());
  if (ssid.length() == 0) {
    LOG("WiFi 修改失败：名称为空");
    web.send(200, "text/html; charset=utf-8",
             "<h3 style='color:red'>WiFi 名称不能为空</h3><a href='/'>返回</a>");
    return;
  }
  prefs.putString("ssid", ssid);
  prefs.putString("pass", pass);
  LOG("新 WiFi 已保存: %s，即将重启...", ssid.c_str());
  web.send(200, "text/html; charset=utf-8",
           "<!DOCTYPE html><html><head><meta charset='utf-8'></head><body>"
           "<h3>已保存！正在重启并连接新 WiFi：" + ssid + " ...</h3>"
           "<p>请稍后刷新页面，新地址由新 WiFi 网络分配。</p></body></html>");
  delay(800);
  ESP.restart();
}

// ---------------------------------------------------------------
// TCP 指令处理（电脑端程序）
// ---------------------------------------------------------------
void handleSwitchClient() {
  WiFiClient client = server.available();
  if (client) {
    LOG("TCP 客户端接入: %s", client.remoteIP().toString().c_str());
    String cmd = "";
    unsigned long start = millis();
    // 注意：客户端发送后立即断开时，数据还在缓冲里但连接已断，
    // 必须用 (connected || available) 继续读，否则指令会丢失
    while ((client.connected() || client.available()) && millis() - start < 2000) {
      if (client.available()) {
        char c = client.read();
        if (c == '\n' || c == '\r') {
          if (cmd.length() > 0) break;
        } else {
          cmd += c;
          if (cmd.length() > 32) break;
        }
      }
      delay(1);
    }
    cmd.trim();
    if (cmd.length() > 0) {
      LOG("收到指令: %s", cmd.c_str());
      parseCommand(cmd);
    } else {
      LOG("TCP 连接无数据，关闭");
    }
    client.stop();
    LOG("TCP 连接关闭");
  }
}

void parseCommand(String cmd) {
  String body = cmd;
  if (cmd.length() >= 8) {
    if (cmd.substring(0, 3) != DEVICE_ID) {
      LOG("指令被忽略：设备号不匹配（期望 %s）", DEVICE_ID);
      return;
    }
    body = cmd.substring(3);            // "001001on" -> "001on"
  }
  if (body.length() < 5) {
    LOG("指令格式错误: %s", cmd.c_str());
    return;
  }
  int sw = body.substring(0, 3).toInt();
  String act = body.substring(3);
  act.toLowerCase();

  if (sw < 1 || sw > SWITCH_COUNT) {
    LOG("开关号无效: %d", sw);
    return;
  }
  if (act == "on") {
    setSwitch(sw, true);
    LOG(">> 开关%d (GPIO%d) 打开", sw, switch_pins[sw - 1]);
  } else if (act == "off") {
    setSwitch(sw, false);
    LOG(">> 开关%d (GPIO%d) 关闭", sw, switch_pins[sw - 1]);
  } else {
    LOG("动作无效（应为 on 或 off）: %s", act.c_str());
  }
}

// ---------------------------------------------------------------
// 开关控制底层
// ---------------------------------------------------------------
bool isSwitchOn(int sw) {
  int pin = switch_pins[sw - 1];
  int level = digitalRead(pin);
  return RELAY_ACTIVE_LOW ? (level == LOW) : (level == HIGH);
}

void setSwitch(int sw, bool on) {
  int pin = switch_pins[sw - 1];
  if (RELAY_ACTIVE_LOW) {
    digitalWrite(pin, on ? LOW : HIGH);
  } else {
    digitalWrite(pin, on ? HIGH : LOW);
  }
}
