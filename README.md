# PVE WebUI Plugins

以獨立 FastAPI 容器承載 Proxmox VE 9 WebUI 插件。沿用 PVE 原生 ExtJS 元件及登入會話，不修改 PVE 官方檔案、VM/CT 設定或 PVE API。

目前提供四個可以各自啟停的插件。切換組合使用同一份程式碼、同一個容器映像；容器啟動時依環境變數與模板生成正式的腳本片段及插件設定。

| 插件 ID | 功能 | 外部依賴 |
| --- | --- | --- |
| `komodo` | Docker／伺服器／應用棧／容器管理、VM/CT 關聯、日誌、終端、告警與操作紀錄 | Komodo Core、Periphery 與服務帳號 API 憑據 |
| `temperatures` | 節點概要中的 CPU、主機板、磁碟溫度與感測器明細 | 節點 node_exporter；可選自訂採集器 |
| `sites` | 共用附加站點清單、iframe、新增、編輯與移除 | SQLite 持久化；無需 Komodo |
| `safeline` | 獨立 SafeLine WAF 視圖、五類資源查閱與編輯、統計、事件／日誌及頁面連線設定 | SafeLine HTTPS API；設定保存在插件資料卷 |

詳細功能：[Komodo 插件](docs/KOMODO.md)、[節點溫度](TEMPERATURES.md)。Komodo 的「統計」「計畫任務」仍為占位頁；部署與程式碼倉庫管理仍有未開放功能。

## 選擇插件

在 `.env` 設定：

```dotenv
# 既有預設組合（未設定此變數時保留此組合）
PVE_PLUGINS=komodo,temperatures,sites

# 加入獨立 SafeLine WAF 視圖
PVE_PLUGINS=komodo,temperatures,sites,safeline
```

其他組合：

```dotenv
# 只啟用溫度與站點；不需要配置 Komodo
PVE_PLUGINS=temperatures,sites

# 只啟用 Docker 管理
PVE_PLUGINS=komodo

# 全部停用，保留原始 PVE 桌面
PVE_PLUGINS=
```

停用插件時，不匯入其 Python 實作、不註冊其業務 API、不提供或注入其腳本，也不初始化其連線池與資料庫。未知 ID、重複 ID、缺少依賴或循環依賴會阻止啟動，避免靜默套用錯誤組合。四個內建插件互不依賴，支援全部 16 種組合。

共用 PVE 元件的可選載入順序由描述檔的 `after` 管理。例如站點與 Komodo 同時啟用時，站點會在 Komodo 之後載入；調換環境變數中的排列也不會破壞資源樹。

修改後執行 `docker compose up -d --force-recreate`，再刷新瀏覽器。這一版在啟動時選擇插件，尚未提供執行中的熱卸載。停用保留原有資料，重新啟用可繼續讀取。

`PVE_PLUGINS_INJECTION_ENABLED=false` 僅停止桌面注入；若要一併移除所有插件 API，使用空的 `PVE_PLUGINS`。單次繞過可訪問 `/?pve-plugins=off`。

## 模板與載入

```text
PVE_PLUGINS
  → 掃描 plugins/*/plugin.json
  → 驗證描述檔、解析依賴和順序
  → templates/desktop-head.html.tmpl → desktop-head.html
  → templates/plugins.json.tmpl      → plugins.json
  → 註冊已選插件的 API、資源與關閉處理
```

容器啟動時，兩份生成檔案寫入 `/run/pve-webui-plugins/`，由非 root 使用者在 tmpfs 中原子替換；不改寫原始碼，也不保存票據或 API 憑據。每個腳本依自身內容產生版本參數。

PVE 桌面本身仍由原始 PVE 即時提供。服務只在已識別的 PVE 9 桌面啟動位置插入生成片段，保留使用者、票據及原生腳本；未知頁面、控制台和行動版不注入。擴充不可達時，Nginx 範例會回退原 PVE。

## 部署

需要能連到原始 PVE 的 Docker 主機和現有 Nginx。僅啟用 `temperatures` 或 `sites` 時不需要 Komodo。溫度來源、Komodo Core 與 Periphery 需按所選插件另行準備。

```sh
git clone https://github.com/sqkkyzx/pve-webui-plugins.git
cd pve-webui-plugins
cp .env.example .env
# 編輯 PVE_UPSTREAM、PVE_PLUGINS、TLS，以及所需插件設定
sudo install -d -m 700 -o 10001 -g 10001 data
docker compose config --quiet
docker compose build --build-arg SOURCE_REVISION="$(git rev-parse HEAD)"
docker compose up -d --pull never
curl --fail http://127.0.0.1:18080/pve-plugins/health
```

範例預設本地建構 `pve-webui-plugins:local`，不假設任何 GHCR 標籤已發布。CI 會測試和建構映像，不會自動發布映像或部署到 PVE。

將 [Nginx 範例](nginx.example.conf) 合入既有配置，替換域名、CA 與上游。只有根路徑 `/` 和 `/pve-plugins/` 轉交插件服務；PVE 原生 API、資源、上傳下載和控制台 WebSocket 仍直連原 PVE。檢查 Nginx 配置後再重載，並保留原始 `:8006` 管理入口。

服務使用 UID/GID `10001:10001`；Compose 設定唯讀根檔案系統、獨立可寫 runtime tmpfs 及 `data` 掛載。擴充不需要 Docker socket、privileged、PVE SSH 憑據或 PVE 主機目錄掛載。

### 主要設定

| 環境變數 | 用途 |
| --- | --- |
| `PVE_UPSTREAM` | 必填，固定 HTTPS origin，例如 `https://pve.example.internal:8006` |
| `PVE_PLUGINS` | 逗號分隔插件 ID；未設定維持 `komodo,temperatures,sites`，明確空值停用全部 |
| `PVE_PLUGINS_INJECTION_ENABLED` | 桌面注入開關，預設 `true` |
| `PVE_ADMIN_USERS` | 完整 PVE 使用者 ID 白名單，預設 `root@pam`；仍須具有 `/` 的 `Sys.Audit` |
| `PVE_TLS_VERIFY`、`PVE_CA_FILE` | 原始 PVE 的憑證驗證；自簽 CA 需唯讀掛載公開 CA 憑證 |
| `PVE_PLUGINS_RUNTIME_DIR` | 生成檔目錄；Docker 預設 `/run/pve-webui-plugins`，直接執行 Python 未設定時僅在記憶體生成 |
| `PVE_PLUGINS_IMAGE`、`PVE_PLUGINS_BIND`、`PVE_PLUGINS_PORT` | Compose 映像、綁定位址與主機端口；預設本地映像、`127.0.0.1:18080` |
| `PVE_KOMODO_URL`、`PVE_KOMODO_API_KEY`、`PVE_KOMODO_API_SECRET` | Komodo 插件的固定 Core origin 及憑據；三項齊全才配置完成 |
| `PVE_KOMODO_TLS_VERIFY` | Core TLS 驗證，預設 `true`，與 PVE 分開 |
| `PVE_TEMPERATURES_SOURCES_FILE` | 溫度來源覆蓋；預設從 PVE 自動取得節點 IP 並讀取 node_exporter 9100 |
| `PVE_KOMODO_ACTION_STATE_FILE`、`PVE_KOMODO_GUEST_LINKS_FILE`、`PVE_SITES_STATE_FILE` | 各插件的 SQLite 路徑；Compose 固定在 `/app/data/` 下 |
| `SAFELINE_STATE_FILE` | SafeLine 連線設定資料庫；Compose 為 `/app/data/safeline/connection.sqlite3` |
| `SAFELINE_URL`、`SAFELINE_API_TOKEN` | 可選初始設定；在頁面保存或清除連線後不再覆蓋頁面設定 |
| `SAFELINE_CA_FILE`、`SAFELINE_TLS_SERVER_NAME` | 初始連線的可選 CA 憑證及憑證主機名；也可直接在頁面填寫 |

### SafeLine WAF

啟用 `safeline` 後，在左上角視圖選擇器選「SafeLine WAF」，於根節點右側「連線資訊 → 編輯連線」填寫管理地址及 API Token，測試後保存即可，不必修改環境變數或重新啟動容器。使用內網 IP 而憑證簽給網域時，可填寫「憑證主機名」並保留 TLS 驗證。

資源樹按防護應用、黑名單、白名單、TLS 憑證及 IP 組分類，二級為具體資源；根節點提供總覽、安全事件、防護規則和連線資訊。資源沿用 [PVE 原生圖標形態](docs/PVE-NATIVE-ICONS.md)。選中具體資源後，右上方「編輯」開啟原生表單：應用可調整說明、域名、監聽、回源、憑證和統計／健康檢查，代理頁可編輯代理選項及全域繼承；另提供運作模式。黑白名單可改名稱、日誌及非敏感匹配條件，啟停使用專用按鈕。IP 組可維護說明與成員；憑證支援更新上傳內容或 ACME 域名／電子郵件。

資源儲存會檢查連線修訂、最新內容指紋、同源 CSRF 和操作編號，保留未編輯欄位並重讀核對。重複請求不再次寫入；逾時或核對失敗顯示結果未知，不自動重送。含敏感條件或未取得完整內容的欄位保留原設定。內建規則只提供啟停；新增、刪除及進階防護設定尚未加入。

Token 保存在後端資料卷的 `0600` SQLite 檔案，目錄預設 `0700`；前端只讀取「已設定」標記，不回填原文，也不保存到瀏覽器儲存。資料庫包含憑據，備份及檔案存取權限須按憑據處理。憑證查閱使用列表資料；僅編輯時由後端讀取詳情，私鑰不回傳、不記錄，更新私鑰欄留空可沿用原私鑰。日誌遮罩認證／Cookie 標頭，本文不傳至前端。

事件數字 ID 與日誌事件 ID 的關係未獲核對，事件詳情按來源、Host、端口及時間範圍查詢相關日誌。應用日誌明確標示 Host／端口篩選；IP 組不把空引用字串當成未被使用。詳見 [SafeLine 設計與驗證範圍](docs/SAFELINE-DESIGN.md)。

總覽把「流量分析」與「安全態勢」放在同一頁，提供共用應用／時間篩選、請求與錯誤指標、QPS、訪問／攔截趨勢、防護指標、攻擊來源 IP 和實時事件。地理位置直接顯示排行，以「世界／中國」及「訪問／僅攔截」兩組按鈕切換四種組合，不載入地球儀。

篩選依目標版本的有效授權能力開放。已實測目前個人版：流量分析只支援全站近 24 小時，安全態勢支援全站近 24 小時／7 天；上游會忽略應用篩選或回退流量時段。因此應用選擇器顯示「全部應用」並停用，選 7 天時流量區塊顯示不可用，保留真正的 7 天安全態勢。支援的版本會自動開放應用與其他時段選項。QPS 和實時事件獨立於時間篩選，分別顯示最新五秒區間與最近 7 筆事件。日誌仍有自己的時間、來源、Host、端口及處置篩選。

自簽 PVE CA 的 Compose 覆蓋範例：

```yaml
services:
  pve-webui-plugins:
    environment:
      PVE_CA_FILE: /run/pve-ca.pem
    volumes:
      - ./pve-ca.pem:/run/pve-ca.pem:ro
```

PVE origin 的主機名須與憑證相符。Nginx 到 PVE 的 TLS 也需要單獨配置信任。`http://core:9120` 僅為 Docker 服務名範例，必須確保容器網路可達。

### 既有安裝遷移

0.4 起只提供 `/pve-plugins/` 路徑，寫入使用 `X-PVE-Plugins-CSRF`。Komodo API 位於 `/pve-plugins/api/komodo/`，站點位於 `/pve-plugins/api/sites`，溫度位於 `/pve-plugins/api/temperatures/nodes/{node}`。腳本依插件分別位於 `/pve-plugins/assets/{plugin_id}/`。舊路由、header、繞過查詢和環境變數不再提供別名或回退。

停止服務後一起更新程式、前端、Compose 與 `.env`，再重建容器；瀏覽器重新整理以載入新腳本及 CSRF token。環境變數直接改名：

| 舊設定 | 現行設定 |
| --- | --- |
| `PVE_EXTENSION_IMAGE`、`PVE_EXTENSION_BIND`、`PVE_EXTENSION_PORT` | `PVE_PLUGINS_IMAGE`、`PVE_PLUGINS_BIND`、`PVE_PLUGINS_PORT` |
| `PVE_EXTENSION_ENABLED`、`PVE_RUNTIME_DIR` | `PVE_PLUGINS_INJECTION_ENABLED`、`PVE_PLUGINS_RUNTIME_DIR` |
| `KOMODO_URL`、`KOMODO_API_KEY`、`KOMODO_API_SECRET`、`KOMODO_TLS_VERIFY` | `PVE_KOMODO_URL`、`PVE_KOMODO_API_KEY`、`PVE_KOMODO_API_SECRET`、`PVE_KOMODO_TLS_VERIFY` |
| `PVE_ACTION_STATE_FILE`、`PVE_GUEST_LINKS_FILE` | `PVE_KOMODO_ACTION_STATE_FILE`、`PVE_KOMODO_GUEST_LINKS_FILE` |
| `PVE_SITES_FILE`、`PVE_TEMPERATURE_SOURCES_FILE` | `PVE_SITES_STATE_FILE`、`PVE_TEMPERATURES_SOURCES_FILE` |

保留相同的 PVE/Core origin 與 `actions.sqlite3`、`guest-links.sqlite3`、`sites.sqlite3`；不需要清空或重建資料庫。Nginx 使用現行範例的 `/pve-plugins/` 代理規則。

`guest-links` 和站點庫會核對所屬 PVE origin；跨叢集應使用獨立資料庫。這是遷移方法，並不表示已自動遷移任何現有服務。

## 身分與狀態

插件業務 API 先驗證現有 PVE 票據、完整雙因素登入、白名單與 `Sys.Audit`；Komodo 操作仍受服務帳號權限限制。寫入保留來源、CSRF、資源版本、關聯修訂及持久化去重校驗。終端保留一次性授權與獨立 PTY 清理。

- `GET /pve-plugins/health`：服務存活、版本、啟用插件；不證明外部服務可達。
- `GET /pve-plugins/api/session`：已驗證會話。
- `GET /pve-plugins/api/plugins`：已驗證的啟用清單及已安裝描述檔。

## 開發與驗證

使用 [compose.debug.yaml](compose.debug.yaml) 可直接掛載後端、插件及模板，修改主機上的檔案後自動重新載入：

```sh
docker compose -f compose.yaml -f compose.debug.yaml up -d --build
```

此模式使用獨立的 `debug` 映像建構階段，增加已鎖定的 `watchfiles` 依賴；一般映像保持原有執行依賴。監看 `app/`、`plugins/`、`templates/` 內的 Python、JavaScript、JSON 描述檔和模板，並使用輪詢以支援 CephFS 等共用檔案系統。三個來源目錄在容器內唯讀，直接編輯主機上的來源即可；資料庫仍使用獨立的可寫掛載。

若部署主機無法拉取基礎映像，可以手動執行 CI 並開啟 `bundle_debug_image`，取得已通過測試的 `linux/amd64` 除錯映像及 SHA-256 校驗檔；下載後使用 `docker load` 載入。此部署產物保留一天。

檔案變更會重新啟動應用程序並重建模板輸出；瀏覽器刷新後載入新前端。修改 `.env` 的插件組合仍須以相同的兩個 `-f` 參數執行 `up -d --force-recreate`。

Python 3.12，Node.js 20 或更新版本。前端使用原生 JavaScript，不需要前端建構。

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes -r requirements-dev.txt
python -m pytest -q
node --test tests/*.mjs
node --check plugins/komodo/static/extension.js
node --check plugins/temperatures/static/temperatures.js
node --check plugins/sites/static/sites.js
node --check plugins/safeline/static/safeline.js
```

[CI](.github/workflows/ci.yml) 執行完整 Python／Node 回歸、16 種組合檢查，以及非 root、唯讀容器的 16 種組合 smoke test。容器驗證也核對生成檔和腳本內容。它不連接真實 PVE，不代替實際瀏覽器驗收。

新增插件的契約與範例見 [插件開發](docs/PLUGINS.md)。

```text
app/                    共用宿主、登入／CSRF、設定與載入器
plugins/komodo/          Docker 管理實作、描述檔與前端
plugins/temperatures/    溫度實作、來源適配器與可選採集器
plugins/sites/           站點 API、儲存與前端
templates/              啟動時使用的模板
scripts/smoke_image.py   容器組合驗證
tests/                  原有回歸及插件組合測試
```

## 來源與授權

PVE 整合從 [sqkkyzx/komodo](https://github.com/sqkkyzx/komodo) 獨立整理；Komodo Core、Periphery 與原本 Web UI 的完整程式碼不隨本專案複製。來源修訂與保留的測試契約見 [NOTICE.md](NOTICE.md)。本專案使用 [GNU GPL v3](LICENSE)。
