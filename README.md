# XMU Check-in · 厦大课程考勤簿（服务器版）

这是可以部署到 **Ubuntu / Linux 服务器、全天运行**的版本。使用独立 Python 服务监控数字和雷达签到，并按课程查看本人签到、缺勤、迟到、请假及未知记录；关闭浏览器或个人电脑不影响服务器后台。

**项目前身来自「虾大助手」Android APK（1.7.4，包名 `com.xmu.assistant`）。** 当前版本参考该 APK 静态分析中确认的登录流程、平台接口及签到行为，重新实现为 Python 后台、SQLite 数据库和中文网页。原 APK 与反编译产物不随本仓库发布，来源和适配范围见 [来源说明](docs/provenance.md)。

推荐单人使用，通过 `systemd` 常驻、自启及异常重启，不依赖手机或安卓模拟器。Docker 配置作为可选部署方式提供。

本学期课程默认显示，其他学期折叠，搜索会展开匹配的历史学期；顶部次数仅统计本学期。优先依据平台的学期起始日期判断当前学期，缺少日期时使用学期日历回退。

自动签到默认等其他 10 名已到同学签到后再提交，数字和雷达共用此条件。人数按身份去重并排除本人，请假与缺勤不算已到。平台无法提供可靠名单时继续等待，不会因即将截止而提前签。可在“调整策略”修改等待人数，0 表示不等待；设置会跨服务重启保留。

## 已实现与验证范围

- 协议来自虾大助手 APK 1.7.4 的静态分析，提供学校账号密码登录及已有 Cookie 登录。
- 提交前将本次操作写入数据库，避免超时或重启后盲目重发；回查本人状态后才显示签到成功。
- 历史逐页读取，遇到权限不足、分页异常或状态无法核实时标记不完整；未知不计入缺勤。
- 二维码签到只在监控页展示提醒。此版本不发送邮件、微信或其他外部通知。
- 本地接口测试、HTTP 模拟响应测试与合成数据浏览器检查通过。2026-10-26 已完成真实学校账号登录、本人资料校验和课程历史读取；实际签到尚未实测。平台接口变化、验证码或访问限制可能需要调整适配。
- 已适配当前学校登录页的密码表单隔离与 CAS `service` 参数。历史接口忽略分页时会用无参数请求复核较小记录集；记录较大或有分页总数时继续严格检查分页。已结束的 `finished` 记录按平台的本人状态统计，不用 `is_expired=false` 推断为待签到。

## 本机运行

Python 3.10 及以上，建议 3.12。

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -m uvicorn xmu.web:create_app --factory --host 127.0.0.1 --port 7321
```

打开 `http://127.0.0.1:7321/`。首次启动自动生成管理密码，位于 `data/admin-password.txt`。管理密码与学校密码分开，修改管理密码需停止服务并通过维护脚本重置 `admin_hash`，不要删除整个数据库。

1. 输入管理密码进入页面。
2. 点击“账号设置”，登录学校账号。遇到验证码时在自己的浏览器完成学校登录，再导入 `lnt.xmu.edu.cn` API 请求的 Cookie 请求头内容。不要复制给其他人。
3. 等待课程同步，逐门查看记录及同步状态。
4. 选择数字/雷达处理方式，点击“启动监控”。默认每 30 秒检查，设置可调整。
5. 提交结果待核实时不会自动重发；以平台本人记录为准。

学校密码、刷新令牌及 Cookie 使用 Fernet 加密存储；`secret.key` 与数据库共同备份才能恢复。加密不代替服务器账号和文件访问保护。凭据不会返回给网页，也不会写进运行日志。

## Ubuntu / systemd

以下命令适用于新的 Ubuntu 服务器。程序放在 `/opt/xmu-attendance`，运行数据放在 `/var/lib/xmu-attendance`，两者分开维护：

```bash
sudo apt-get update
sudo apt-get install -y git python3-venv
sudo git clone https://github.com/legend91019/XMU_check_in.git /opt/xmu-attendance
cd /opt/xmu-attendance
sudo python3 -m venv .venv
sudo .venv/bin/python -m pip install -r requirements.txt
id -u xmu-attendance >/dev/null 2>&1 || sudo useradd --system --home-dir /var/lib/xmu-attendance --shell /usr/sbin/nologin xmu-attendance
sudo install -m 644 deploy/xmu-attendance.service /etc/systemd/system/xmu-attendance.service
sudo systemctl daemon-reload
sudo systemctl enable --now xmu-attendance
sudo systemctl status xmu-attendance
sudo journalctl -u xmu-attendance -n 50 --no-pager
```

服务自动创建 `/var/lib/xmu-attendance`，管理密码在其中的 `admin-password.txt`，仅服务器管理员和服务用户可读。使用单个 Uvicorn 进程，不要加 `--workers`，也不要同时启动 systemd 和 Docker。

默认服务仅监听 `127.0.0.1:7321`。可从自己的电脑建立 SSH 隧道：

```bash
ssh -N -L 7321:127.0.0.1:7321 <user>@<server>
```

如果要在手机浏览器直接访问，使用已有 HTTPS 域名，在其 Nginx HTTPS `server` 中 include `deploy/nginx-location.conf`，并设置 `/etc/xmu-attendance.env`：

```text
XMU_ROOT_PATH=/attendance
```

测试 `nginx -t` 后 reload，重启本服务。打开 `https://<your-domain>/attendance/`。不要将学校登录表单通过公网 HTTP 暴露。使用 HTTPS 路径部署时，SSH 隧道需同时按 `/attendance/` 路径和反向代理访问，或临时清空 `XMU_ROOT_PATH` 后重启。

`systemctl restart xmu-attendance` 后监控设置和数据保留，网页需重新输入管理密码。`systemctl disable --now xmu-attendance` 可停用服务，数据保留。

首次启动后，读取管理密码并通过 SSH 隧道或 HTTPS 打开网页，再配置自己的学校账号和监控策略：

```bash
sudo cat /var/lib/xmu-attendance/admin-password.txt
```

默认监控暂停；登录账号后手动点击“启动监控”。开启后每 30 秒检查一次，等待人数默认 10，可在网页调整。服务器正常运行并联网时会持续工作；平台登录失效时尝试续登，无法续登时在网页提示重新登录。

## Docker 可选部署

```bash
docker compose up -d --build
docker compose logs --tail 30
docker compose exec attendance cat /data/admin-password.txt
```

named volume 保存数据。Docker 为备用部署配置，本次服务器采用 systemd；Docker 镜像构建需另行验证。

## 测试

```powershell
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
New-Item -ItemType Directory -Force test-results
.\.venv\Scripts\python -m pytest tests -q --basetemp test-results/pytest-run
node --check xmu/static/app.js
```

`tests/ui_smoke.py` 使用 Playwright 和本机 Chrome 进行无头浏览器检查，要求 localhost:7322 上启动带合成测试数据的独立服务，截图位于 `test-results/screenshots`。合成数据与生产数据严格分开。
