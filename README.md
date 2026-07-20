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
