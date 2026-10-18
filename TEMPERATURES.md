# 实体 PVE 节点温度

在节点“概要”的原生状态框中显示 CPU、主板、磁盘温度及感测器明细。默认直接接入各实体节点的 node_exporter，自动从 PVE 读取节点 IP，不需要逐台设置温度环境变量或来源文件，也不需要额外部署自定义采集器或 Prometheus。

## 自动发现与读取

```text
浏览器选择 PVE 节点，JS 只提交节点名称
  → FastAPI 验证现有 PVE 登录会话、白名单及 Sys.Audit
  → 使用同一票据读取固定 PVE 上游 /api2/json/cluster/status
  → 精确匹配在线节点的 name / ip
  → 读取 http://节点IP:9100/metrics?collect[]=hwmon
  → 整理温度与标签，返回浏览器同源接口
```

PVE 的 `/cluster/status` 同时支持叢集与独立节点。节点 IP 由 PVE 提供，不使用浏览器地址栏域名，也不接受浏览器指定目标 IP、URL、端口或命令。IPv4 和 IPv6 均支持。离线、缺 IP、重复节点或冲突地址不会退回旧 IP，也不扫描其他网卡。

浏览器始终访问 `/pve-plugins/api/temperatures/nodes/{node}/temperatures`。HTTPS 管理页面无需直接跨域连接 HTTP exporter，因此不需要为 node_exporter 开放 CORS。PVE Cookie 只发给 PVE；自动发现的 exporter 请求不携带 Cookie、PVE token 或 Komodo 凭据。

## 安装与连通性

在每个实体 PVE 节点，以 root 执行：

```sh
apt update
apt install --no-install-recommends prometheus-node-exporter
systemctl enable --now prometheus-node-exporter
systemctl is-active prometheus-node-exporter
curl -fsS 'http://127.0.0.1:9100/metrics?collect[]=hwmon' \
  | grep '^node_hwmon_temp_celsius'
```

node_exporter 的 Linux `hwmon` collector 默认启用，读取 `/sys/class/hwmon`。它不执行 `sensors -j`，也不会自动应用 lm-sensors 用户配置中的标签或校准；界面使用 exporter 提供的芯片名称、原始 sysfs 通道标签与摄氏数值。

扩展服务所在容器必须能连到 PVE 返回的节点 IP 的 TCP 9100。服务若只监听 `127.0.0.1`、路由不通或防火墙阻断，将显示“不可用”。只允许扩展／监控服务来源访问指标接口。特殊拓扑可以使用下文的可选覆盖配置。

安装 exporter 只准备了数据源；本地扩展代码更新仍需按主部署文档部署，刷新浏览器后才会使用新逻辑。安装与本地模拟测试不等于已经完成正式环境接入。

## 显示规则

- CPU 优先 Intel `Package id` 或 AMD `Tdie`；只有 `Tctl` 时标明控制温度。多颗 CPU 的摘要显示最高值；取 `Tccd`、核心或其他通道时，在摘要附上所选原始标签。
- 主板优先 `Motherboard`、`Mainboard`、`System`、`SYSTIN` 等明确系统通道，避免把同一芯片上的 CPU、TSI 或芯片组温度当作主板本体。没有明确系统通道时显示已有通道的最高值，并在摘要附上来源标签；全部原始读数仍保留在明细中。
- 磁盘显示已有 NVMe / drivetemp 温度的最高值与设备数。不同设备即使同名，也按 exporter 的唯一设备标识分别统计。
- 明细包含未能分类的传感器、采样时间和设备报告的警戒／严重阈值。没有标签不会按 temp1 猜成 CPU；没有阈值便显示未知。
- exporter 的温度已经是摄氏度，不再次除以 1000。真实 `0°C` 保留，缺测或故障通道不补零。
- 概要激活时每 10 秒读取，指标快取 5 秒。每次读取仍验证 PVE 会话和节点映射；节点 IP 改变立即放弃旧目标的快取。
- 超过 30 秒的样本显示“已过期”。离页、注销或销毁面板时停止独立轮询，保留 PVE 原有状态更新。

数据来自本次 scrape，采样时间由后端记录。`hwmon` 采集失败、格式错误或超时不保留旧值冒充当前温度。硬件驱动未暴露的数据不能由界面补出，部分主板或 SATA 磁盘可能没有温度。

## 可选覆盖配置

默认不需要配置。只有 exporter 使用其他地址、端口、TLS 或认证代理时，才使用 [`temperature-sources.example.json`](temperature-sources.example.json)：

```json
{
  "nodes": {
    "pve-node": {
      "type": "node_exporter",
      "url": "https://metrics.example.internal:19100",
      "ca_file": "/run/temperatures/collector-ca.pem"
    }
  }
}
```

`url` 只允许固定 HTTP(S) origin，不能包含路径、账号密码或查询参数。接口路径固定为 `/metrics`，仅筛选 `hwmon`。`ca_file` 可省略使用默认信任 CA；自签证书挂载公开 CA，保持 TLS 验证。明文 HTTP 非 loopback 地址需要显式 `"allow_http": true`，仅用于已有保护的传输网络。认证代理可额外配置 `token`（32–256 个 ASCII 字符），用于 Bearer 认证；原生 exporter 默认无需此 token。

覆盖配置通过 `PVE_TEMPERATURES_SOURCES_FILE` 指定，并只读挂载到容器，例如：

```dotenv
PVE_TEMPERATURES_SOURCES_FILE=/run/temperatures/sources.json
```

```yaml
services:
  pve-webui-plugins:
    volumes:
      - ./temperature-sources.json:/run/temperatures/sources.json:ro
      - ./collector-ca.pem:/run/temperatures/collector-ca.pem:ro
```

文件应允许容器 UID/GID `10001:10001` 读取。含真实地址或 token 的配置不提交 Git。修改覆盖配置后重建服务。历史自定义采集器格式仍可显式使用 `"type": "collector"`，要求 token，读取固定 `/v1/temperatures`；它不参与默认自动发现。

## 排错

| 显示 | 优先核对 |
| --- | --- |
| 不可用 | PVE 节点在线、名称/IP 唯一、节点 IP 可达、9100 监听、防火墙、hwmon scrape 与格式 |
| 已过期 | 样本时间与服务运行状态 |
| 未知 | 没有对应类别的有效温度；打开明细查看芯片和原始标签 |
| 无访问权限／会话已过期 | PVE 登录、管理员白名单与 Sys.Audit |

参考：[PVE Cluster API 源码](https://github.com/proxmox/pve-manager/blob/master/PVE/API2/Cluster.pm)、[node_exporter collectors](https://github.com/prometheus/node_exporter#collectors)、[hwmon 实作](https://github.com/prometheus/node_exporter/blob/v1.9.0/collector/hwmon_linux.go)、[Linux hwmon ABI](https://docs.kernel.org/hwmon/sysfs-interface.html)。
