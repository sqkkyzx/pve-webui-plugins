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
  "assets": [{"source": "static/sample.js", "url": "/pve-plugins/assets/sample/sample.js"}]
}
```

ID 必須與目錄相同，只接受小寫英文字母開頭、英數字與底線；描述檔使用固定欄位。入口限定為本 package 的 `plugin:setup`。資源只能指向本插件內的檔案，公開 URL 必須位於 `/pve-plugins/assets/{plugin_id}/`，且不能重複。當前資源契約提供一般 JavaScript 腳本。

`requires` 是必須明確啟用的其他插件 ID；宿主按依賴先後載入，不自動擴大啟用範圍。可選的 `after` 指定「對方也啟用時才套用」的先後關係，不會啟用對方。例如站點插件宣告 `after: ["komodo"]`，確保它包裝容器視圖的共用資源樹；站點功能可獨立使用。沒有順序約束時沿用保存的啟用清單順序。未知 ID、重複 ID、循環及缺少必要依賴會拒絕套用。

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

`context` 提供插件 application、當前設定、PVE authenticator、共用 CSRF protector，以及測試用的具名 transports。內建插件的啟停和使用者設定由核心管理頁持久化；插件應在 `setup` 中驗證自身設定。停用插件時不執行其驗證。路由及資源只使用 `/pve-plugins/`；寫入使用 `X-PVE-Plugins-CSRF`。

每個業務端點都要呼叫 authenticator；宿主不會把所有未知路徑自動當作已授權。寫入還要使用 `context.csrf` 驗證來源與票據綁定的 token，並自行處理輸入校驗、併發版本和重複提交。可參考 `plugins/sites/site_routes.py`。

插件只在啟用時 import 和呼叫 `setup`。設定切換或關閉時按照反向順序等待各插件的 `shutdown`；某個關閉失敗仍會嘗試其餘插件的清理。Komodo 插件會先完成已接受的寫入與回執，再關閉儲存與連線池。

插件是受信任的程式碼，與宿主處於同一程序。這個契約管理組合與生命週期，未提供第三方程式碼 sandbox。保存模組設定時，宿主切換插件執行個體；插件必須在 `shutdown` 中釋放所有自有資源。

## 前端與共用檔案

資源依解析後的插件順序載入，在 `pvemanagerlib.js` 之後、`PVE.StdWorkspace` 建立之前執行。使用 ExtJS override 時保留一般 script／function；不要加上會破壞 ExtJS `callParent` 的 strict mode。插件應有重複載入標記、能力檢查，以及切頁、銷毀和登出時的輪詢／視窗／PTY 清理。

同一份桌面入口需要多插件內容時，由 `templates/desktop-head.html.tmpl` 統一組合腳本，沒有插件各自修改核心檔案的步驟。`templates/plugins.json.tmpl` 生成當前組合的公開清單。核心管理腳本獨立載入，全部可選模組停用時仍保留管理入口與內建附加站點。正式容器使用 `/run/pve-webui-plugins/desktop-head.html` 和 `plugins.json`；生成檔不放使用者身分或憑據。

PVE 官方 HTML 來自同叢集的節點 IP 列表；首項維持持久化 PVE 標識，其餘為備用池。核心辨識受支援的啟動位置並插入生成片段，官方 HTML／JavaScript 來源仍由 PVE 提供。

容器入口預設同時監督 HTTP 應用 8008 與 Nginx HTTPS 網關 8006，預設宿主地址分別為 `127.0.0.1:8008` 與 `0.0.0.0:8006`。`PVE_INTERNAL_PROXY_ENABLED=false` 時只執行應用。此開關屬於部署設定，與可選插件啟停分開配置。開發映像也沿用相同入口，將 reload 命令作為應用子程序執行。

## 管理介面與顯示設定

「--插件--」根節點提供統一的「模块与站点」表格，以藍色「功能模块」、橙色「附加站点」區分類型，左側樹平鋪顯示所有條目。「开发进度」並列呈現功能覆蓋與成熟度，懸停可查看邊界；狀態隨專案版本維護，站點顯示「外部站点」，不評定外部應用。選中一行後按「编辑」或雙擊，於視窗中設定啟用狀態、顯示別名與圖示，站點另有名稱和目標地址。按視窗中的「保存」即寫入該條目。表格上移、下移調整混合順序，再按頂部「保存并应用」套用。

宿主設定採用 schema 2，導航必須包含 `entry_order`、`module_aliases`、`module_icons` 三個欄位。混合順序保存在核心設定的 `navigation.entry_order`，模組別名與圖示保存在 `navigation.module_aliases`、`navigation.module_icons`。站點功能內建提供，各站點的 `enabled`、`alias`、`icon` 與名稱、URL 共同保存在原有 SQLite 列中，使用同一個修訂進行 CAS 更新。省略的 PATCH 欄位沿用既有值，`icon: null` 使用自動圖示。停用站點仍列於管理 API，可繼續編輯，但不加入視圖選單，也不發起圖示探索。

圖示來源支援預設／自動、PVE 原生類名與 HTTP(S) URL，可輸入 `fa fa-server`、`fl fl-docker` 並即時預覽。自動站點圖示優先採用網頁宣告，回退至 `/favicon.ico`，讀取失敗時顯示原生通用圖示。`GET /pve-plugins/api/sites/{site_id}/icon` 經會話驗證後只探索已保存且啟用、使用自動圖示的站點，回傳 ID、修訂與圖示地址。

顯示設定與排序保存在資料卷，僅更新顯示資訊時立即同步樹、視圖選單與表格並保留當前頁面。點擊左側模組行後，中間以 PVE 原生圖示與文字的垂直選單切換「介绍」「基本配置」「帮助文档」，右側顯示對應內容；表格「模块配置」直接進入基本配置。介紹與說明包含功能範圍、驗證階段、設定步驟及排錯，停用模組仍可讀取，無須載入其腳本或初始化外部連線。SafeLine 基本配置須啟用並保存後開放。站點行則定位表格，使用「打开站点」開啟嵌入頁面。

## 加入與測試

新增 package 後，載入器可發現描述檔並裝配 API 與資源。統一管理頁目前為三個可選內建模組提供固定映射；自訂插件還需接入管理表格、導航、設定表單與 schema 2 驗證。開發測試可顯式構造 Settings 注入組合。正式部署透過管理頁保存模組選擇。

至少驗證：單獨啟用、與既有插件組合、停用後 API／資源不存在、匿名拒絕、依賴順序，以及離頁／登出的清理。共用宿主的組合回歸位於 `tests/test_plugins.py`，真實映像 smoke test 位於 `scripts/smoke_image.py`。
