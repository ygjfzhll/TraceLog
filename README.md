# TraceLog

按项目查看 VictoriaLogs 里的请求链路。

## 目录

- `app.py`：启动程序
- `config.yaml`：本机配置，不提交到仓库
- `config.example.yaml`：配置示例
- `web/index.html`：页面
- `db/mysql/init.sql`：表结构

## 启动

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml
.venv/bin/python app.py
```

浏览器打开 `http://127.0.0.1:8789`。第一次启动会创建管理员 `admin` / `admin`，并按配置加入一条日志接入。

先在「项目管理」里新建项目并选择日志接入，再到「请求链路」按业务用户查询。
