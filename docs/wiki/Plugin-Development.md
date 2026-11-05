# 插件开发

## Package 与描述文件

插件随宿主映像部署，最小目录如下。

```text
plugins/sample/
├── __init__.py
├── plugin.json
├── plugin.py
└── static/sample.js
```

```json
{
  "id": "sample",
  "name": "Sample plugin",
  "version": "1.0.0",
  "api_version": 1,
  "entrypoint": "plugins.sample.plugin:setup",
  "requires": [],
  "after": [],
  "assets": [
    {
      "source": "static/sample.js",
      "url": "/pve-plugins/assets/sample/sample.js"
    }
  ]
}
```

ID 与目录一致，使用小写字母开头的英数字及下划线。入口限定为当前 package 的 `plugin:setup`，资源位于本插件目录和独立 URL 前缀内。`requires` 指定必须显式启用的依赖，`after` 调整与已启用模块的相对顺序。

## 后端入口

```python
from fastapi import Request

from app.plugins import PluginHandle


def setup(context):
    @context.application.get("/pve-plugins/api/sample/status")
    async def status(request: Request):
        await context.authenticator.authenticate(request)
        return {"status": "ok"}

    return PluginHandle()
```

`context` 包含插件 application、当前 settings、共享认证器、CSRF protector 和测试 transport。业务 API 自行调用认证器，写操作同时完成来源、CSRF、输入、修订及重复请求处理。可参考 `plugins/sites/site_routes.py`。

`PluginHandle.shutdown` 可返回异步关闭处理。插件拥有的连接池、数据库、后台任务与 PTY 在关闭时释放。宿主在停用或重配置时切换实例，旧实例的已接受 HTTP 请求完成后再执行清理。

## 前端接入

脚本在官方 `pvemanagerlib.js` 之后、工作区创建之前执行。应为重复载入、目标类缺失、登录切换、离页和组件销毁建立明确处理。ExtJS `callParent` 依赖调用元数据，相关 override 使用普通 script 与 function。

前端组件沿用现有 ExtJS 表格、树、表单和窗口。业务数据在进入 HTML renderer 前转义，轮询和终端会话随所依附页面清理。

## 加载器与管理页接入

宿主可扫描新的 `plugin.json` 并调用相应入口。当前统一管理界面为三个可选内置模块提供固定映射，自定义 package 的表格行、导航名称、图标与配置表单需要显式接入。

增加完整可管理模块时，同时检查 `app/static/manager.js`、`app/static/navigation.js` 与 `app/configuration.py` 中的模块字段和校验，以及新增模块的前后端生命周期。已有管理页保存会保留其他启用插件 ID。

开发测试可以通过显式构造 `Settings` 注入插件组合。正式部署的模块选择由 schema 2 的持久化管理配置控制，添加可管理插件需要同步接入该配置流程。

## 测试

应覆盖独立启用、依赖组合、停用后的导入/API/资源/客户端行为，以及认证、CSRF、输入边界、并发修订、结果未知和会话清理。核心组合测试位于 `tests/test_plugins.py`，前端组合测试位于 `tests/test_plugin_composition.mjs`。

加入插件后重新构建映像，再验证真实镜像中的 UID、只读文件系统、数据卷和生成资源。发布前运行正式与 debug 镜像 smoke 脚本，详见 [[兼容性与发布验证|Compatibility]]。
