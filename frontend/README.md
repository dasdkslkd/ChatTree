# ChatTree Frontend

前端由本地 launcher 绑定到一个不可变的 Profile。先从仓库根目录启动 launcher：

```powershell
python -m client_launcher
```

开发前端时，在另一个 PowerShell 窗口运行：

```powershell
$env:VITE_LAUNCHER_PROXY_TARGET='http://127.0.0.1:8000'
npm --prefix frontend run dev
```

launcher 打开的第一个标签页使用 `http://127.0.0.1:5173/s/local`。每个额外标签页都使用其准确的 Profile ID，例如 `http://127.0.0.1:5173/s/<profile-id>`。

Profile 路径支持对话、节点和运行深链接：

```text
/s/<profile-id>
/s/<profile-id>/c/<conversation-id>
/s/<profile-id>/c/<conversation-id>/n/<node-id>
/s/<profile-id>/r/<run-id>
```

根路径 `/` 会被有意拒绝。查询参数和 URL hash 不属于深链接契约，也会被拒绝。Vite 只代理到 launcher 的 `8000` 端口，不会直接访问 Profile 后端的 `8001` 端口。
