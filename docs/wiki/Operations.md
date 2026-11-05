# 升级与排错

## 数据备份与升级

单实例使用相同的数据卷、基础配置和 Compose 覆盖文件。升级前记录源码修订、映像 ID、节点 IP 列表及部署参数。

默认使用 Compose 命名卷，可以在停止服务后导出一致的文件备份。

```sh
umask 077
docker compose stop pve-webui-plugins
docker compose run --rm --no-deps -T --entrypoint tar pve-webui-plugins -C /app/data -czf - . > pve-plugins-data.tar.gz
docker compose up -d --pull never
```

使用覆盖文件的部署，在这些命令中带上同一组 `-f` 参数。自定义绑定目录也可以停机后直接备份对应主机目录，并保留权限。

数据卷包含模块配置、各模块 SQLite，以及默认网关的 `proxy/cert.pem` 和 `proxy/key.pem`。`plugins.json`、SafeLine 数据库及网关私钥按凭据保护。恢复时使用同一停机备份集合，并核对已完成的外部操作。

更新源码后构建或加载经核验的映像，使用原数据卷启动单个服务实例。先检查健康、配置和外部连接，再刷新浏览器。源码回退与数据格式回退分别核对。本版初始化采用独立数据目录，旧 schema 和旧站点表结构按配置契约拒绝。

## 基础检查

```sh
docker compose ps
docker compose logs --tail=100 pve-webui-plugins
curl -fsS --insecure https://127.0.0.1:8006/pve-plugins/health
curl -sS --insecure -D - -o /dev/null https://127.0.0.1:8006/
```

上例访问 HTTPS 8006 网关，使用默认自签证书。使用可信证书时按实际访问名称校验证书。应用也可通过 `http://127.0.0.1:8008/pve-plugins/health` 单独检查。容器与宿主机默认端口一致，关闭内置网关时使用该 HTTP 地址。

镜像健康检查同时检查应用及已启用网关的健康路径。桌面响应的 `X-PVE-Plugins-Extension` 标明注入结果。

| 结果或现象 | 核对方向 |
| --- | --- |
| `enabled` | 已插入启动片段，继续检查脚本及登录后的功能 |
| `disabled` | 注入开关或 `pve-plugins=off` 查询参数 |
| `unsupported-page` | PVE 版本、HTML 结构及启动脚本 |
| `console-or-mobile` | 当前请求是否为控制台或移动入口 |
| HTTPS 8006 无法访问 | 内置网关开关、容器状态、8006 端口发布与证书文件 |
| TLS 验证失败 | 当前失败的是浏览器到网关还是网关/应用到 PVE。核对各自 CA、证书名称和挂载权限 |
| API 返回 401 | PVE 票据、完整登录和会话有效期 |
| API 返回 403 | 用户白名单、`Sys.Audit`、同源协议/Host/端口、可信代理来源及外部权限 |
| 保存返回 409 | 重新读取最新配置或资源后编辑 |
| 保存返回 422 | 字段格式、节点 IP 列表、证书名称列表或提交结构 |
| 外部 API 不可用 | Core、SafeLine、exporter 的网络、TLS、认证与状态 |
| 站点嵌入空白 | CSP、X-Frame-Options、混合内容和目标站点登录 |

## 节点故障与结果未知

IP 列表首项是稳定 PVE 标识，保持配置顺序稳定。主节点失败后可读取备用池，备用池不承诺严格逐项顺序。节点恢复后可被后续请求重新尝试。

WebSocket、控制台和正在传输的响应无法迁移，断开后重新连接。应用和网关不会盲目重放已经发送的写入，超时或结果未知时先检查 PVE/外部服务状态，再决定是否再次提交。

关闭内置网关后，原生 API、控制台及其备用节点策略由外部业务网关负责。应用内的桌面和会话验证仍使用配置的节点列表。

## 分层验证与问题报告

分别检查进程健康、网关入口、PVE 会话、外部模块和真实浏览器交互。记录最先失败的资源 URL、HTTP 状态及控制台错误，比较实际脚本与部署修订。`/?pve-plugins=off` 用于单次绕过，原始 PVE `:8006` 入口可独立验证节点。

问题报告包含 PVE/插件版本、两种接入模式中的实际选择、节点数量、部署方式、重现步骤、预期和实际结果。日志及截图先移除票据、Cookie、Key、Secret、Token、私钥和业务敏感信息，使用示例地址描述网络关系。
