# PVE WebUI Plugins

PVE WebUI Plugins 是面向 Proxmox VE 9 桌面的外挂式扩展宿主。独立容器默认提供 Nginx HTTPS 8006 网关。也可关闭内置代理，将 HTTP 应用 8008 接入已有业务网关，默认发布地址为 `127.0.0.1:8008`。容器与宿主机默认端口一致。它从同集群节点取得桌面，在原生工作区建立前载入插件，沿用 PVE 的 ExtJS 组件、登录会话、视图选择器和资源树。

> 本组文档对应 0.14.0 的全新部署契约，宿主配置使用 schema 2。首次启动提供内置附加站点，可选模块从管理页启用。发布验证范围见兼容性页。

## 能做什么

| 模块 | 主要功能 | 接入方式 |
| --- | --- | --- |
| Komodo | 服务器、应用栈、容器、日志、终端、操作记录，以及 VM/CT 关联 | Komodo Core API 与 Periphery |
| 节点温度 | 节点概要中的 CPU、主板、磁盘温度和传感器明细 | node_exporter，支持来源覆盖 |
| SafeLine WAF | 防护应用、规则、证书、IP 组、事件、日志和统计 | SafeLine 管理 API |
| 附加站点 | 嵌入页面、站点信息、独立启停、别名、图标和排序 | 站点 URL 与宿主 SQLite |

「--插件--」提供统一管理表格。Komodo、节点温度和 SafeLine WAF 可以独立启停，附加站点能力内置提供。每个站点单独管理，模块与站点共享显示顺序。

## 阅读顺序

1. [[架构与设计边界|Architecture]] 说明请求路径、插件组合和故障回退。
2. [[安装与双模式接入|Installation]] 准备节点 IP 列表、数据卷、HTTPS 网关或业务网关分流。
3. [[模块与站点管理|Configuration]] 设置连接、别名、图标和统一排序。
4. [[内置模块|Modules]] 了解外部依赖与各模块功能范围。
5. [[插件开发|Plugin-Development]] 实现描述文件、API、前端和资源清理。
6. [[升级与排错|Operations]] 检查服务、备份数据和定位接入故障。
7. [[兼容性与发布验证|Compatibility]] 查看验证范围及本次评审待办。

## 项目定位

项目将反向代理入口注入、原生 PVE 界面集成、声明式插件装配、服务端凭据管理和运行时启停组合为一个宿主。PVE 的官方页面与原生管理链路继续由 PVE 提供，扩展数据和连接配置由宿主持有。

[PVE electrified](https://github.com/bogeeee/pve-manager-electrified) 同样提供 PVE UI 插件能力。本项目的设计重点是独立容器接入、原生界面、服务端连接配置、运行时插件生命周期与统一管理，定位为「非侵入式、可插拔的 PVE WebUI 扩展宿主」。

源码与授权说明见项目仓库。[源代码](https://github.com/sqkkyzx/pve-webui-plugins)、[LICENSE](https://github.com/sqkkyzx/pve-webui-plugins/blob/main/LICENSE)、[NOTICE](https://github.com/sqkkyzx/pve-webui-plugins/blob/main/NOTICE.md)
