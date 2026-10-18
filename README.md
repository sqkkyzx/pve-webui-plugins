# PVE WebUI Plugins

以獨立 FastAPI 容器承載 Proxmox VE 9 WebUI 插件。沿用 PVE 原生 ExtJS 元件及登入會話，不修改 PVE 官方檔案、VM/CT 設定或 PVE API。

目前提供三個可以各自啟停的插件。切換組合使用同一份程式碼、同一個容器映像；容器啟動時依環境變數與模板生成正式的腳本片段及插件設定。

| 插件 ID | 功能 | 外部依賴 |
| --- | --- | --- |
| `komodo` | Docker／伺服器／應用棧／容器管理、VM/CT 關聯、日誌、終端、告警與操作紀錄 | Komodo Core、Periphery 與服務帳號 API 憑據 |
| `temperatures` | 節點概要中的 CPU、主機板、磁碟溫度與感測器明細 | 節點 node_exporter；可選自訂採集器 |
| `sites` | 共用附加站點清單、iframe、新增、編輯與移除 | SQLite 持久化；無需 Komodo |

詳細功能：[Komodo 插件](docs/KOMODO.md)、[節點溫度](TEMPERATURES.md)。Komodo 的「統計」「計畫任務」仍為占位頁；部署與程式碼倉庫管理仍有未開放功能。

## 選擇插件

在 `.env` 設定：

```dotenv
# 全部啟用（未設定此變數時的預設值）
PVE_PLUGINS=komodo,temperatures,sites
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

停用插件時，不匯入其 Python 實作、不註冊其業務 API、不提供或注入其腳本，也不初始化其連線池與資料庫。未知 ID、重複 ID、缺少依賴或循環依賴會阻止啟動，避免靜默套用錯誤組合。三個內建插件互不依賴，支援全部八種組合。

共用 PVE 元件的可選載入順序由描述檔的 `after` 管理。例如站點與 Komodo 同時啟用時，站點會在 Komodo 之後載入；調換環境變數中的排列也不會破壞資源樹。

修改後執行 `docker compose up -d --force-recreate`，再刷新瀏覽器。這一版在啟動時選擇插件，尚未提供執行中的熱卸載。停用保留原有資料，重新啟用可繼續讀取。

`PVE_EXTENSION_ENABLED=false` 僅停止桌面注入；若要一併移除所有插件 API，使用空的 `PVE_PLUGINS`。單次繞過可訪問 `/?pve-plugins=off`，既有的 `/?pve-komodo=off` 也保留。

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

將 [Nginx 範例](nginx.example.conf) 合入既有配置，替換域名、CA 與上游。只有根路徑 `/`、`/pve-plugins/` 和相容路徑 `/pve-komodo/` 轉交擴充；PVE 原生 API、資源、上傳下載和控制台 WebSocket 仍直連原 PVE。檢查 Nginx 配置後再重載，並保留原始 `:8006` 管理入口。

服務使用 UID/GID `10001:10001`；Compose 設定唯讀根檔案系統、獨立可寫 runtime tmpfs 及 `data` 掛載。擴充不需要 Docker socket、privileged、PVE SSH 憑據或 PVE 主機目錄掛載。

### 主要設定

| 環境變數 | 用途 |
| --- | --- |
| `PVE_UPSTREAM` | 必填，固定 HTTPS origin，例如 `https://pve.example.internal:8006` |
| `PVE_PLUGINS` | 逗號分隔插件 ID；未設定預設全部，明確空值停用全部 |
| `PVE_EXTENSION_ENABLED` | 桌面注入開關，預設 `true` |
| `PVE_ADMIN_USERS` | 完整 PVE 使用者 ID 白名單，預設 `root@pam`；仍須具有 `/` 的 `Sys.Audit` |
| `PVE_TLS_VERIFY`、`PVE_CA_FILE` | 原始 PVE 的憑證驗證；自簽 CA 需唯讀掛載公開 CA 憑證 |
| `PVE_RUNTIME_DIR` | 生成檔目錄；Docker 預設 `/run/pve-webui-plugins`，直接執行 Python 未設定時僅在記憶體生成 |
| `PVE_EXTENSION_IMAGE`、`PVE_EXTENSION_BIND`、`PVE_EXTENSION_PORT` | Compose 映像、綁定位址與主機端口；預設本地映像、`127.0.0.1:18080` |
| `KOMODO_URL`、`KOMODO_API_KEY`、`KOMODO_API_SECRET` | Komodo 插件的固定 Core origin 及憑據；三項齊全才配置完成 |
| `KOMODO_TLS_VERIFY` | Core TLS 驗證，預設 `true`，與 PVE 分開 |
| `PVE_TEMPERATURE_SOURCES_FILE` | 溫度來源覆蓋；預設從 PVE 自動取得節點 IP 並讀取 node_exporter 9100 |
| `PVE_ACTION_STATE_FILE`、`PVE_GUEST_LINKS_FILE`、`PVE_SITES_FILE` | 各插件的 SQLite 路徑；Compose 固定在 `/app/data/` 下 |

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

停止舊擴充並備份 `data` 後，使用新專案的 Compose、映像和所需插件組合。保留相同的 PVE/Core origin 與 `actions.sqlite3`、`guest-links.sqlite3`、`sites.sqlite3`；不需要清空或重建資料庫。舊 `/pve-komodo/` API、資源、WebSocket 路徑及 CSRF header 保留，原有 Nginx 路由可繼續使用；新增 `/pve-plugins/` 路由可使用新的健康檢查和插件清單。

`guest-links` 和站點庫會核對所屬 PVE origin；跨叢集應使用獨立資料庫。這是遷移方法，並不表示已自動遷移任何現有服務。

## 身分與狀態

插件業務 API 先驗證現有 PVE 票據、完整雙因素登入、白名單與 `Sys.Audit`；Komodo 操作仍受服務帳號權限限制。寫入保留來源、CSRF、資源版本、關聯修訂及持久化去重校驗。終端保留一次性授權與獨立 PTY 清理。

- `GET /pve-plugins/health`：服務存活、版本、啟用插件；不證明外部服務可達。
- `GET /pve-plugins/api/session`：已驗證會話。
- `GET /pve-plugins/api/plugins`：已驗證的啟用清單及已安裝描述檔。
- `/pve-komodo/health` 與 `/pve-komodo/api/session`：相容入口。

## 開發與驗證

使用 [compose.debug.yaml](compose.debug.yaml) 可直接掛載後端、插件及模板，修改主機上的檔案後自動重新載入：

```sh
docker compose -f compose.yaml -f compose.debug.yaml up -d --build
```

此模式使用獨立的 `debug` 映像建構階段，增加已鎖定的 `watchfiles` 依賴；一般映像保持原有執行依賴。監看 `app/`、`plugins/`、`templates/` 內的 Python、JavaScript、JSON 描述檔和模板，並使用輪詢以支援 CephFS 等共用檔案系統。三個來源目錄在容器內唯讀，直接編輯主機上的來源即可；資料庫仍使用獨立的可寫掛載。

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
```

[CI](.github/workflows/ci.yml) 執行完整 Python／Node 回歸、八種組合檢查，以及非 root、唯讀容器的八種組合 smoke test。容器驗證也核對生成檔和腳本內容。它不連接真實 PVE，不代替實際瀏覽器驗收。

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
