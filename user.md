# ePaper Service 使用说明

当前云端服务地址：

```text
http://47.113.120.232
```

管理员 Token：

```text
uQ16ZW5i_NAWZr51NHUR6upsf579uSE8jqqCLlKqGx0
```

管理员 Token 用于网页上传图片、创建设备、分配图片等管理操作。ESP32 设备不使用管理员 Token，ESP32 使用每个设备自己的 Device Token。

## 1. 普通用户网页测试

适合给同事在电脑浏览器里手动验证图片处理效果。

### 1.1 打开网页

在浏览器打开：

```text
http://47.113.120.232
```

如果页面能正常打开，说明公网访问已经可用。

### 1.2 上传图片

在页面里填写：

```text
管理员 Token：uQ16ZW5i_NAWZr51NHUR6upsf579uSE8jqqCLlKqGx0
图片：选择本地图片
方向：自动 / 横屏 800x480 / 竖屏 480x800
适配方式：铺满并居中裁切 / 完整显示并补白
启用抖动：通常保持勾选
```

点击：

```text
上传并处理
```

上传成功后，页面会显示：

```text
Image ID
尺寸
数据大小
格式
处理后的预览图
下载 BMP 预览图
下载 EPD 数据文件
```

### 1.3 下载结果

用户可以下载两种文件：

```text
BMP 预览图：用于人工查看 6 色处理效果
EPD 数据文件：用于 ESP32 或固件端测试显示
```

正常情况下，横屏或竖屏图片都会生成：

```text
数据大小：192000 bytes
格式：epd4bit-indexed-v1
```

## 2. 常见网页错误

### 2.1 invalid admin token

返回：

```json
{"detail":"invalid admin token"}
```

原因：管理员 Token 没填或填错。

解决：确认页面里的管理员 Token 是：

```text
uQ16ZW5i_NAWZr51NHUR6upsf579uSE8jqqCLlKqGx0
```

### 2.2 image conversion failed

原因可能是：

```text
上传的文件不是图片
图片文件损坏
图片格式 Pillow 无法识别
```

解决：换一张常见格式图片测试，例如 `.jpg`、`.png`。

### 2.3 页面打不开

先测试健康检查接口：

```text
http://47.113.120.232/health
```

正常应返回：

```json
{"status":"ok"}
```

如果浏览器打不开，检查：

```text
阿里云安全组是否放行 TCP 80
服务器上的 nginx 是否运行
epaper 服务是否运行
```

## 3. API 测试

以下命令适合在电脑终端里验证云服务。

### 3.1 健康检查

```bash
curl http://47.113.120.232/health
```

正常返回：

```json
{"status":"ok"}
```

### 3.2 上传图片

把 `/path/to/image.jpg` 替换成真实图片路径：

```bash
curl -X POST http://47.113.120.232/api/images \
  -H 'X-Admin-Token: uQ16ZW5i_NAWZr51NHUR6upsf579uSE8jqqCLlKqGx0' \
  -F 'file=@/path/to/image.jpg' \
  -F 'direction=auto' \
  -F 'mode=scale' \
  -F 'dither=true'
```

返回示例：

```json
{
  "image_id": "IMAGE_ID",
  "width": 800,
  "height": 480,
  "format": "epd4bit-indexed-v1",
  "sha256": "SHA256",
  "data_size": 192000,
  "data_url": "/api/images/IMAGE_ID/data",
  "preview_url": "/api/images/IMAGE_ID/preview"
}
```

### 3.3 下载预览图

把 `IMAGE_ID` 替换成上传接口返回的值：

```bash
curl -o preview.bmp \
  http://47.113.120.232/api/images/IMAGE_ID/preview
```

### 3.4 下载 EPD 数据文件

```bash
curl -o image.epd \
  http://47.113.120.232/api/images/IMAGE_ID/data
```

正常文件大小：

```text
192000 bytes
```

## 4. ESP32 测试流程

ESP32 端推荐流程：

```text
1. ESP32 启动或从 deep sleep 唤醒
2. 连接 Wi-Fi
3. 请求当前设备的 manifest
4. 如果 version 没变，不下载图片，直接上报 unchanged
5. 如果 version 变化，下载 download_url
6. 校验数据大小
7. 校验 sha256
8. 解码 4-bit palette index 数据
9. 刷新电子纸屏幕
10. 上报 displayed 或 error
11. 进入 deep sleep
```

## 5. 创建设备

每个 ESP32 应该有自己的 `device_id` 和 `device_token`。

例如创建设备 `device001`：

```bash
curl -X POST http://47.113.120.232/api/devices/device001 \
  -H 'Content-Type: application/json' \
  -H 'X-Admin-Token: uQ16ZW5i_NAWZr51NHUR6upsf579uSE8jqqCLlKqGx0' \
  -d '{}'
```

返回示例：

```json
{
  "device_id": "device001",
  "token": "DEVICE_TOKEN"
}
```

这里的 `DEVICE_TOKEN` 要交给 ESP32 固件使用。

## 6. 上传并分配图片给设备

### 6.1 上传图片

```bash
curl -X POST http://47.113.120.232/api/images \
  -H 'X-Admin-Token: uQ16ZW5i_NAWZr51NHUR6upsf579uSE8jqqCLlKqGx0' \
  -F 'file=@/path/to/image.jpg' \
  -F 'direction=auto' \
  -F 'mode=scale' \
  -F 'dither=true'
```

记下返回的：

```text
image_id
```

### 6.2 分配图片

把 `IMAGE_ID` 替换成上传得到的图片 ID：

```bash
curl -X POST http://47.113.120.232/api/devices/device001/assign \
  -H 'Content-Type: application/json' \
  -H 'X-Admin-Token: uQ16ZW5i_NAWZr51NHUR6upsf579uSE8jqqCLlKqGx0' \
  -d '{"image_id":"IMAGE_ID"}'
```

返回示例：

```json
{
  "device_id": "device001",
  "version": 1,
  "has_image": true,
  "image_id": "IMAGE_ID",
  "width": 800,
  "height": 480,
  "format": "epd4bit-indexed-v1",
  "sha256": "SHA256",
  "download_url": "/api/images/IMAGE_ID/data"
}
```

每次重新分配图片，`version` 会递增。ESP32 可以用 version 判断是否需要重新下载。

## 7. ESP32 请求当前图片

ESP32 请求：

```http
GET /api/devices/{device_id}/current
X-Device-Token: DEVICE_TOKEN
```

curl 示例：

```bash
curl http://47.113.120.232/api/devices/device001/current \
  -H 'X-Device-Token: DEVICE_TOKEN'
```

如果设备有图片，返回：

```json
{
  "device_id": "device001",
  "version": 1,
  "has_image": true,
  "image_id": "IMAGE_ID",
  "width": 800,
  "height": 480,
  "format": "epd4bit-indexed-v1",
  "palette": [[0,0,0],[255,255,255],[255,255,0],[255,0,0],[0,0,255],[0,255,0]],
  "sha256": "SHA256",
  "download_url": "/api/images/IMAGE_ID/data"
}
```

如果设备还没有分配图片，返回：

```json
{
  "device_id": "device001",
  "version": 0,
  "has_image": false
}
```

## 8. ESP32 下载图片数据

如果 manifest 里的 `download_url` 是相对路径：

```text
/api/images/IMAGE_ID/data
```

ESP32 需要拼成完整 URL：

```text
http://47.113.120.232/api/images/IMAGE_ID/data
```

下载请求：

```http
GET /api/images/IMAGE_ID/data
```

当前实现中，图片数据接口不需要 Device Token。设备可以直接下载 `download_url`。

下载后必须校验：

```text
文件大小 = width * height / 2
sha256 = manifest.sha256
```

对于 `800x480` 或 `480x800`：

```text
width * height / 2 = 192000 bytes
```

## 9. EPD 数据格式

格式名称：

```text
epd4bit-indexed-v1
```

每个字节存两个像素：

```text
高 4 bit：第一个像素
低 4 bit：第二个像素
```

解码方式：

```c
uint8_t first = (byte >> 4) & 0x0F;
uint8_t second = byte & 0x0F;
```

调色板索引：

```text
0 black  RGB(0, 0, 0)
1 white  RGB(255, 255, 255)
2 yellow RGB(255, 255, 0)
3 red    RGB(255, 0, 0)
4 blue   RGB(0, 0, 255)
5 green  RGB(0, 255, 0)
```

ESP32 固件需要把这些 palette index 转换成屏幕驱动需要的颜色编码。

## 10. ESP32 上报状态

ESP32 显示完成后建议上报：

```http
POST /api/devices/{device_id}/status
X-Device-Token: DEVICE_TOKEN
Content-Type: application/json
```

成功显示：

```bash
curl -X POST http://47.113.120.232/api/devices/device001/status \
  -H 'Content-Type: application/json' \
  -H 'X-Device-Token: DEVICE_TOKEN' \
  -d '{"version":1,"status":"displayed"}'
```

图片未变化：

```bash
curl -X POST http://47.113.120.232/api/devices/device001/status \
  -H 'Content-Type: application/json' \
  -H 'X-Device-Token: DEVICE_TOKEN' \
  -d '{"version":1,"status":"unchanged"}'
```

显示失败：

```bash
curl -X POST http://47.113.120.232/api/devices/device001/status \
  -H 'Content-Type: application/json' \
  -H 'X-Device-Token: DEVICE_TOKEN' \
  -d '{"version":1,"status":"error","error":"sha256 mismatch"}'
```

也可以附带电池和信号：

```json
{
  "version": 1,
  "status": "displayed",
  "battery_mv": 3800,
  "rssi": -62
}
```

## 11. 使用 simulate_device.py 测试

在项目目录本地运行：

```bash
python3 simulate_device.py \
  --server http://47.113.120.232 \
  --device-id device001 \
  --token DEVICE_TOKEN
```

如果正常，会输出 manifest，并显示：

```text
Downloaded version 1: 192000 bytes, sha256 ok.
```

如果传入当前已知版本：

```bash
python3 simulate_device.py \
  --server http://47.113.120.232 \
  --device-id device001 \
  --token DEVICE_TOKEN \
  --known-version 1
```

如果云端版本也是 `1`，会输出：

```text
Version 1 is unchanged.
```

## 12. 查看设备状态

管理员可以查看设备信息：

```bash
curl http://47.113.120.232/api/devices/device001 \
  -H 'X-Admin-Token: uQ16ZW5i_NAWZr51NHUR6upsf579uSE8jqqCLlKqGx0'
```

返回中包含：

```text
current_image_id
current_version
last_seen_at
last_status
last_error
battery_mv
rssi
```

## 13. 服务器运维命令

SSH 登录：

```bash
ssh root@47.113.120.232
```

查看服务状态：

```bash
systemctl status epaper
systemctl status nginx
```

查看实时日志：

```bash
journalctl -u epaper -f
```

重启服务：

```bash
systemctl restart epaper
systemctl reload nginx
```

检查 nginx 配置：

```bash
nginx -t
```

检查健康状态：

```bash
curl http://127.0.0.1/health
curl http://47.113.120.232/health
```

## 14. 安全建议

当前服务是 HTTP 明文访问，管理员 Token 和设备 Token 会以明文在网络上传输。

正式长期使用前建议：

```text
1. 绑定域名
2. 配置 HTTPS
3. 改用 SSH key 登录服务器
4. 关闭 root 密码登录
5. 不要把管理员 Token 写进 ESP32 固件
6. 每个 ESP32 使用独立 device_id 和 device_token
```

