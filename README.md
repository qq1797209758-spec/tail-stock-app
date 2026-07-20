# A股尾盘策略筛选 Web 应用

这是一个基于 Streamlit、AKShare 和 Pandas 的公开行情数据筛选与分析工具。

> 本项目不是自动交易软件，不构成投资建议，也不承诺任何收益。

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

## 每日 Top10 研究名单

- 完整扫描完成后，从通过前序数据验证的真实股票中按综合评分降序展示最多 10 只。
- 综合评分达到 80 分的股票按原评分标签展示。
- 达标股票不足 10 只时，低于 80 分的真实候选会按评分递补，并明确标记“未达80分，仅供研究”。
- 如果通过前序验证的真实股票总数不足 10 只，只展示实际数量，不生成虚假股票。
- 名单仅用于公开数据筛选和策略研究，不构成投资建议。

## 历史扫描存储

- 当前版本使用本地 `data/scan_history.db` SQLite 文件临时保存。
- Streamlit Community Cloud 重启、重新部署或容器迁移后，本地历史可能被清空。
- 运行时数据库已被 `.gitignore` 排除，不会提交到 Git 仓库。
- `ScanHistoryRepository` 已预留统一存储契约，后续可换成 Supabase 实现。

## 历史回测

- 页面“历史回测”标签可回测最近最多 10 个具备完整次日行情的交易日。
- 历史股票池、名称、日线、当时总股本和分钟行情通过同花顺 iFinD HTTP 接口按筛选日获取；AKShare仅用于交易日历。
- 历史量比使用筛选日累计分钟成交量相对前5个交易日同一时刻平均累计量的近似值，并在结果中明确标记。
- 筛选日之后的数据不参与选股与评分；次日开高低收仅用于收益评价。
- 无法验证的数据会记录在失败与缺失表，不会使用今天实时行情、模拟数据或随机数据补齐。
- 历史回测结果不代表未来表现，不构成投资建议。
