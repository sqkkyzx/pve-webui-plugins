# 插件契約

每個插件是一個隨映像打包的 Python package，具有 `plugin.json`、`plugin.py`，以及可選的前端資源。宿主掃描描述檔；新增插件不需要修改 `app/main.py` 或手動增加 import。

## 最小結構

```text
plugins/sample/
├── __init__.py
├── plugin.json
├── plugin.py
└── static/sample.js
```

`plugin.json`：

```json
{
  "id": "sample",
  "name": "Sample plugin",
  "version": "1.0.0",
  "api_version": 1,
  "entrypoint": "plugins.sample.plugin:setup",
  "requires": [],
  "after": [],
  "assets": [{"source": "static/sample.js", "url": "/pve-plugins/assets/sample.js"}]
}
```

ID 必須與目錄相同，只接受小寫英文字母開頭、英數字與底線；描述檔使用固定欄位。入口限定為本 package 的 `plugin:setup`。資源只能指向本插件內的檔案，公開 URL 不能重複。當前資源契約提供一般 JavaScript 腳本。

`requires` 是必須明確啟用的其他插件 ID；宿主按依賴先後載入，不自動擴大啟用範圍。可選的 `after` 指定「對方也啟用時才套用」的先後關係，不會啟用對方。例如站點插件宣告 `after: ["komodo"]`，確保它包裝容器視圖的共用資源樹；仍可單獨啟用站點。沒有順序約束時沿用 `PVE_PLUGINS` 的順序。未知 ID、重複 ID、循環及缺少必要依賴都會在啟動時報錯。

## 後端入口

```python
from fastapi import Request
from app.plugins import PluginHandle


def setup(context):
    @context.application.get("/pve-plugins/api/sample/status")
    async def status(request: Request):
        await context.authenticator.authenticate(request)
        return {"status": "ok"}

    async def shutdown():
        # 關閉本插件擁有的資源；不關閉呼叫端注入的 transport。
        pass

    return PluginHandle(shutdown=shutdown)
```

`context` 提供 FastAPI application、啟動設定、PVE authenticator、共用 CSRF protector，以及測試用的具名 transports。需要自訂環境變數的插件可以在自身 `setup` 中讀取和驗證；停用插件時不執行其驗證。內建插件原有環境變數保留在 `app/settings.py` 供相容遷移。

每個業務端點都要呼叫 authenticator；宿主不會把所有未知路徑自動當作已授權。寫入還要使用 `context.csrf` 驗證來源與票據綁定的 token，並自行處理輸入校驗、併發版本和重複提交。可參考 `plugins/sites/site_routes.py`。

插件只在啟用時 import 和呼叫 `setup`。關閉時按照反向順序等待各插件的 `shutdown`；某個關閉失敗仍會嘗試其餘插件的清理。Komodo 插件會先完成已接受的寫入與回執，再關閉儲存與連線池。

插件是受信任的程式碼，與宿主處於同一程序。這個契約管理組合與生命週期，沒有提供第三方程式碼 sandbox 或執行中熱卸載。

## 前端與共用檔案

資源依解析後的插件順序載入，在 `pvemanagerlib.js` 之後、`PVE.StdWorkspace` 建立之前執行。使用 ExtJS override 時保留一般 script／function；不要加上會破壞 ExtJS `callParent` 的 strict mode。插件應有重複載入標記、能力檢查，以及切頁、銷毀和登出時的輪詢／視窗／PTY 清理。

同一份桌面入口需要多插件內容時，由 `templates/desktop-head.html.tmpl` 統一組合腳本，沒有插件各自修改核心檔案的步驟。`templates/plugins.json.tmpl` 生成同一次啟動的公開清單。正式容器使用 `/run/pve-webui-plugins/desktop-head.html` 和 `plugins.json`；生成檔不放使用者身分或憑據。

PVE 官方 HTML 仍來自固定上游；核心只辨識受支援的啟動位置並插入生成片段。這裡的模板不會替代或覆寫 PVE 官方 HTML／JavaScript 檔案。

## 加入與測試

新增 package 後，將 `sample` 加入 `PVE_PLUGINS`，重新建構並啟動映像。已有插件之間切換組合僅需重建容器。

至少驗證：單獨啟用、與既有插件組合、停用後 API／資源不存在、匿名拒絕、依賴順序，以及離頁／登出的清理。共用宿主的組合回歸位於 `tests/test_plugins.py`，真實映像 smoke test 位於 `scripts/smoke_image.py`。
