# 安装与双模式接入

## 准备条件

- Docker Engine 与 Docker Compose，以及本版源码。
- 容器能够连接同一 PVE 集群的各节点 HTTPS 端口。
- 位于 `PVE_ADMIN_USERS` 白名单且在 `/` 具有 `Sys.Audit` 的完整 PVE 用户 ID。

默认使用内置 Nginx 网关提供 HTTPS 8006，容器与宿主机默认端口一致。已有 HAProxy、Nginx 等业务网关也可以接入，选择下文的全量转发或路径分流模式。

## 默认 HTTPS 网关

在源码目录执行以下步骤，并编辑 `.env`。

```sh
cp .env.example .env
```

```dotenv
PVE_UPSTREAM=192.0.2.2,192.0.2.3,192.0.2.4
PVE_ADMIN_USERS=root@pam
PVE_INTERNAL_PROXY_ENABLED=true
PVE_TLS_VERIFY=false
```

`PVE_UPSTREAM` 只填写 IP 与可选端口，最多 16 项，以逗号分隔。默认使用 HTTPS 8006。例如 `192.0.2.2:8006,[2001:db8::2]:8006`，带端口的 IPv6 使用方括号。各项必须属于同一集群，首项为主节点，其余为备用池。

这里的端口是目标 PVE 服务端口，与容器及宿主机监听端口独立。例如 `PVE_UPSTREAM=192.0.2.2:443,[2001:db8::2]:8443` 会分别连接上游的 443 和 8443，容器网关仍监听 8006，应用仍监听 8008；省略上游端口时才使用 8006。

默认到 PVE 的 HTTPS 连接不验证证书与服务身份，适用于直接填写节点 IP 和使用 PVE 自签证书的接入方式。下文提供启用证书验证的方法。

```sh
docker compose config --quiet
docker compose build --build-arg SOURCE_REVISION="$(git rev-parse HEAD)"
docker compose up -d --pull never
curl --fail --insecure https://127.0.0.1:8006/pve-plugins/health
```

浏览器访问 `https://Docker主机地址:8006`。首次启动生成自签证书并保存到数据卷的 `proxy/cert.pem`、`proxy/key.pem`，重建容器后继续使用。浏览器首次访问会提示证书信任或名称问题，示例健康检查中的 `--insecure` 用于该自签入口。

Compose 默认端口映射如下。

| 服务 | 容器内部端口 | 默认宿主机地址 |
| --- | --- | --- |
| HTTPS 网关 | 8006 | `0.0.0.0:8006` |
| HTTP 应用 | 8008 | `127.0.0.1:8008` |

`PVE_PROXY_BIND`、`PVE_PROXY_PORT` 控制网关的宿主机发布地址，`PVE_PLUGINS_BIND`、`PVE_PLUGINS_PORT` 控制应用的宿主机发布地址。

## 数据卷

默认命名卷 `pve_webui_plugins_data` 挂载到 `/app/data`，映像预先设置 UID/GID `10001:10001` 与目录权限。首次启动无需手工创建主机目录。Compose 会按项目名为卷命名，备份步骤见 [[升级与排错|Operations]]。

需要自定义绑定目录时，先准备权限，再用覆盖文件替换 `/app/data` 的挂载。

```sh
sudo install -d -m 700 -o 10001 -g 10001 data
```

```yaml
services:
  pve-webui-plugins:
    volumes:
      - ./data:/app/data
```

构建、启动和运维命令始终使用同一组 Compose 文件。

## 自定义网关证书

证书和私钥必须成对配置，并允许容器 UID `10001` 读取。例如保存以下内容为 `compose.gateway-cert.yaml`。

```yaml
services:
  pve-webui-plugins:
    environment:
      PVE_PROXY_CERT_FILE: /run/certs/gateway.crt
      PVE_PROXY_KEY_FILE: /run/certs/gateway.key
    volumes:
      - ./gateway.crt:/run/certs/gateway.crt:ro
      - ./gateway.key:/run/certs/gateway.key:ro
```

```sh
docker compose -f compose.yaml -f compose.gateway-cert.yaml up -d --pull never
```

证书名称应覆盖浏览器或前置业务网关使用的访问名称。

## 验证 PVE 上游证书

这是容器到 PVE 的 TLS 设置，与浏览器访问插件网关 HTTPS 8006 的证书分别配置。

```dotenv
PVE_UPSTREAM=192.0.2.2,192.0.2.3,192.0.2.4
PVE_TLS_VERIFY=true
PVE_TLS_SERVER_NAMES=pve-a.example.internal,pve-b.example.internal,pve-c.example.internal
PVE_CA_FILE=/run/pve-ca.pem
```

DNS 名称与节点 IP 逐项对齐，用于 SNI 和证书名称校验。内置网关启用验证时每个节点都需要名称。连接目标仍是配置的 IP，名称需要与相应节点证书匹配。自签 CA 可以使用包含集群可信 CA 的公开 PEM 文件，系统已信任的 CA 可省略 `PVE_CA_FILE`。

将下面内容保存为 `compose.ca.yaml`，并确保 CA 文件可由 UID `10001` 读取。

```yaml
services:
  pve-webui-plugins:
    volumes:
      - ./pve-ca.pem:/run/pve-ca.pem:ro
```

```sh
docker compose -f compose.yaml -f compose.ca.yaml build --build-arg SOURCE_REVISION="$(git rev-parse HEAD)"
docker compose -f compose.yaml -f compose.ca.yaml up -d --pull never
```

## 接入现有业务网关

### 全量转发

业务网关可以将增强入口的全部请求转发到 HTTPS 8006。内置 Nginx 负责应用与原生 PVE 路由、多节点备用池和 WebSocket。前置网关到容器的证书信任按所选网关配置，可使用上文的自定义网关证书。

如果前置网关终止浏览器 TLS，内置 Nginx 可通过 `PVE_TRUSTED_PROXIES` 接受其 `X-Forwarded-Proto`。填写内置 Nginx 实际看到的可信直接对端 IP/CIDR，例如 `192.0.2.10/32`。Docker NAT 后的来源可能与网关原地址不同。该变量只作用于启用的内置 Nginx。

### 关闭内置代理并按路径分流

设置 `PVE_INTERNAL_PROXY_ENABLED=false` 后，容器只运行 HTTP 应用 8008，容器内 8006 没有 HTTPS 服务。业务网关终止浏览器 TLS，并将精确根路径 `/` 和 `/pve-plugins/` 转发到应用，其余路径转发 PVE。默认宿主机应用地址为 `http://127.0.0.1:8008`，跨主机接入时调整应用发布地址或使用可达的容器网络直接访问 HTTP 8008。

HAProxy 的路径选择示例适用于已经能读取 HTTP 的前端。

```haproxy
acl pve_desktop path -m str /
acl pve_plugins path_beg /pve-plugins/
use_backend pve_plugin_app if pve_desktop
use_backend pve_plugin_app if pve_plugins
default_backend pve_original
```

`pve_plugin_app` 指向应用 HTTP 地址，`pve_original` 指向节点 HTTPS 8006。保持 Host、路径、查询参数及 WebSocket 升级标头，上传下载限制和原生节点故障切换由业务网关配置。Nginx 分流可参考源码的 `nginx.example.conf`。

仅应用模式依赖外部网关覆写 `X-Forwarded-Proto` 为浏览器使用的 HTTP(S) 协议、保留包含端口的 Host，并将应用端口的网络访问限定到该网关。

纯 TCP TLS 透传无法读取上述 HTTP 路径。分流模式的原生 PVE 主备与入口回退规则需要在业务网关中配置。

## 首次使用与故障切换

从增强入口登录 PVE，打开左上角「--插件--」。在统一表格中启用模块、填写连接参数或新增站点。详见 [[模块与站点管理|Configuration]]。

保持 IP 列表首项稳定，它也是宿主持久化数据的 PVE 标识。备用池不保证严格逐项顺序。节点故障后已有 WebSocket 和控制台连接需要重新连接，写入超时或结果未知时先核对实际状态。
