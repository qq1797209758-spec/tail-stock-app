# A股尾盘策略筛选 Web 应用

这是一个基于 Streamlit、AKShare 和 Pandas 的公开行情数据筛选与分析工具。

> 本项目不是自动交易软件，不构成投资建议，也不承诺任何收益。

## 每日复盘与持久化

正式 Top5 按“推荐日期 + 策略版本”幂等保存到 `data/scan_history.db`，下一交易日收盘后使用真实不复权日线补录。每日复盘页打开时立即检查，页面保持打开时每 10 分钟检查一次，也可手动补录。收益基准为推荐日不复权收盘价；缺失、停牌或接口失败保持等待/待补录，不按亏损处理。

当前仓储已通过接口层隔离，但默认实现仍是 SQLite。Streamlit Community Cloud 的本地文件可能在重启、重新部署或容器迁移时丢失，不属于永久云存储；页面提供 SQLite 备份导出和导入。真正无人值守的收盘定时任务需要接入外部持久数据库与调度器。

历史复盘与模型评分仅用于量化研究，不代表未来收益，不构成投资建议。

## 第一阶段功能

- 响应式 Streamlit 首页，支持电脑和手机浏览器
- AKShare 全 A 股实时行情连接测试
- 数据加载状态与成功提示
- 请求失败时展示错误原因，不让页面白屏
- 实时行情前 20 条预览
- 为后续筛选、图表和 Excel 导出预留模块结构

## 环境要求

- Windows
- Python 3.11

## 本地运行

在 PowerShell 中进入项目目录：

```powershell
cd C:\Users\17972\Documents\gu\tail-stock-app
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
streamlit run app.py
```

启动后，电脑浏览器访问：

```text
http://localhost:8501
```

## 手机访问

确保手机与电脑连接同一个局域网，并允许 Windows 防火墙访问端口 8501。
启动应用后，在电脑 PowerShell 中运行 `ipconfig` 查询电脑的 IPv4 地址，
然后在手机浏览器访问：

```text
http://电脑IPv4地址:8501
```

例如：`http://192.168.1.10:8501`。

## Streamlit Community Cloud

1. 将项目上传到 GitHub 仓库。
2. 在 Streamlit Community Cloud 新建应用。
3. 将入口文件设置为 `app.py`。
4. 平台会根据 `requirements.txt` 自动安装依赖。

免费公开行情接口可能出现网络波动、限流或临时维护，应用会显示错误提示，稍后重试即可。

## 每日 Top5 研究名单

- 每个A股交易日从真实有效的沪深主板行情中输出5只相对高概率候选。
- 严格候选不足时依次使用一级、二级、三级和综合评分递补，并逐只标注入选类型。
- 评分按尾盘20、量能15、资金15、板块15、趋势15、换手10、活跃度10计算。
- 缺失分项不直接淘汰或计零分，按其余可用权重重新归一化并展示完整度和缺失字段。
- 如果真实行情接口无法提供至少5只有效股票，显示实际数量和缺失数量，不生成虚假股票。
- 名单仅用于公开数据筛选和策略研究，不构成投资建议。

## 历史扫描存储

- 当前版本使用本地 `data/scan_history.db` SQLite 文件临时保存。
- Streamlit Community Cloud 重启、重新部署或容器迁移后，本地历史可能被清空。
- 运行时数据库已被 `.gitignore` 排除，不会提交到 Git 仓库。
- `ScanHistoryRepository` 已预留统一存储契约，后续可换成 Supabase 实现。

## 历史回测

- 页面“历史回测”标签可回测最近最多 10 个具备完整次日行情的交易日。
- 历史股票池、名称、日线、当时总股本和分钟行情通过已配置的历史行情接口按筛选日获取；AKShare用于交易日历。
- 历史量比使用筛选日累计分钟成交量相对前5个交易日同一时刻平均累计量的近似值，并在结果中明确标记。
- 筛选日之后的数据不参与选股与评分；次日开高低收仅用于收益评价。
- 无法验证的数据会记录在失败与缺失表，不会使用今天实时行情、模拟数据或随机数据补齐。
- 历史回测结果不代表未来表现，不构成投资建议。

## 可安装 PWA

项目保留原有 Streamlit 入口 `app.py`，同时新增独立的响应式 PWA 前端和 FastAPI 只读接口。原因是 Streamlit Community Cloud 不能可靠地在站点根作用域提供自定义 Service Worker；仅靠页面 HTML 注入无法形成稳定、可验证的 PWA。

本地启动 PWA：

```powershell
.\.venv\Scripts\python.exe -m uvicorn pwa_server:app --host 0.0.0.0 --port 8000
```

浏览器访问 `http://localhost:8000`。生产环境必须使用 HTTPS。PWA 与 API 应部署在同一来源；接口只读取服务端数据库，不向前端发送数据库密码或行情密钥。股票行情、Top5 和复盘接口设置为 `no-store`，Service Worker 只缓存静态资源和页面框架，不缓存 POST、敏感数据或行情接口响应。

安装方式：

- Windows/macOS：使用 Chrome 或 Edge 打开 PWA HTTPS 地址，点击页面“安装应用”或地址栏安装图标。
- Android：使用 Chrome 打开后选择“安装应用”或“添加到主屏幕”。
- iPhone/iPad：使用 Safari 打开，点击分享按钮，再选择“添加到主屏幕”。

当前复盘仓储默认仍为 SQLite。PWA API 与 Streamlit 在同一主机并指向同一个数据库文件时会共享记录；若分开部署到不同云服务，必须先接入双方可访问的持久化数据库，不能依赖各自容器内的 SQLite 文件。Streamlit 现有部署和访问地址不受 PWA 入口影响。

## 邀请码访问控制

生产入口必须使用 FastAPI/PWA HTTPS 地址。Streamlit 无法可靠设置长期 HttpOnly Cookie，因此启用 `REQUIRE_INVITE_AUTH=true`（默认值）后，旧 Streamlit 入口只显示安全迁移提示，不再渲染股票数据。

部署平台必须配置：

```text
INVITE_HMAC_SECRET       至少32字节的安全随机值
SESSION_SECRET           与上项不同的至少32字节安全随机值
ADMIN_USERNAME           独立管理员账号
ADMIN_PASSWORD_HASH      pbkdf2_sha256格式密码哈希
ACCESS_SESSION_DAYS      默认30
AUTH_COOKIE_SECURE       生产必须为true
REQUIRE_INVITE_AUTH      true
REQUIRE_PERSISTENT_DATABASE true
TAIL_STOCK_DATABASE      持久卷中的绝对数据库路径
PWA_PUBLIC_URL           PWA的公网HTTPS地址
```

生成管理员密码哈希：

```powershell
python manage_invites.py hash-password
```

生成邀请码（完整值只显示一次）：

```powershell
python manage_invites.py generate --count 1 --max-uses 1 --note "测试用户"
```

查看脱敏记录、停用邀请码或撤销会话：

```powershell
python manage_invites.py list
python manage_invites.py status 1 --active no
python manage_invites.py revoke-sessions 1
```

默认 SQLite 仅适合本地或带持久卷的单实例部署。Streamlit Community Cloud 容器本地文件不是持久存储；不得在该临时文件系统中保存生产邀请码。多实例部署需要把 `AccessRepository` 替换为共享的 PostgreSQL 等持久化适配器。
