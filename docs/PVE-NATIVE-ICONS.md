# PVE 原生資源圖標參考

核對日期：2026-10-18。此文件保存已核對的原生圖標形態與樣式名稱，供後續插件直接查用；常規設計與實作可先使用本表，遇到部署版本差異、主題差異或未收錄形態時再核對上游。

## 來源

- [PVE 原生樣式 ext6-pve.css](https://github.com/proxmox/pve-manager/blob/master/www/css/ext6-pve.css)：圖標顏色、角標字形與組合樣式。
- [PVE Utils.js](https://github.com/proxmox/pve-manager/blob/master/www/manager6/Utils.js)：`get_object_icon_class(type, record)` 組合類型、狀態、HA 及鎖定 class。
- [PVE ResourceTree.js](https://github.com/proxmox/pve-manager/blob/master/www/manager6/tree/ResourceTree.js)：`typeDefaults`、`setIconCls` 及狀態 tooltip。

以下記錄的是當日官方上游原始碼，並非所有已安裝 PVE 版本的瀏覽器驗收結果。色碼是該樣式檔的基準值；插件應沿用宿主樣式，以適應亮／暗主題。

## 狀態形態速查

角標使用 Font Awesome 字形；字形編碼方便日後辨識，不需要插件自行重畫。

| class | 主圖標／角標形態 | 原生含義 | 角標字形 | 基準顏色 |
| --- | --- | --- | --- | --- |
| 無狀態 class | 一般深色主圖標，無角標 | 資源類型 | — | 主圖標 `#555` |
| `running` | 深色主圖標＋綠色小三角 | 運行中 | `\f04b` | 主圖標 `#000`；角標 `#21bf4b` |
| `paused` | 黃色雙豎線 | 暫停 | `\f04c` | `#cc8e00` |
| `suspended` | 黃色雙豎線，與 paused 同形 | 掛起 | `\f04c` | `#cc8e00` |
| `stopped` | 主圖標變淡灰，無狀態角標 | 已停止 | — | 主圖標 `#cfcfcf` |
| `online` | 綠色圓形勾 | 在線 | `\f058` | `#21bf4b` |
| `unknown` | 灰色圓形問號 | 狀態未知 | `\f059` | `#888` |
| `pending` | 黃色驚嘆號三角 | 等待／警告 | `\f071` | `#ffcc00` |
| `io-error`、`internal-error`、`guest-panicked` | 黃色驚嘆號三角 | 對應運行異常 | `\f071` | `#ffcc00` |
| `error`、`ha-error` | 紅色圓形叉號 | 錯誤／HA 錯誤 | `\f057` | `#cc1800` |
| `offline` | 淡灰主圖標＋紅色圓形叉號 | 離線 | `\f057` | 主圖標 `#cfcfcf`；角標 `#cc1800` |
| `locked` | 小鎖 | 設定鎖定 | `\f023` | `#000000` |
| `lock-backup` | 磁碟片形備份角標 | 備份鎖定 | `\f0c7` | `#000000` |
| `lock-migrate` | 紙飛機形遷移角標 | 遷移鎖定 | `\f1d9` | `#000000` |
| `lock-suspending` | 黃色向下箭頭／托盤 | 正在掛起至磁碟 | `\f019` | `#cc8e00` |
| `lock-suspended` | 深色向下箭頭／托盤 | 已掛起至磁碟 | `\f019` | `#000000` |
| `ha-maintenance` | 藍色扳手 | HA 維護 | `\f0ad` | `#3892d4` |

需要保留的區別：

- `stopped` 是淡灰圖標；`offline` 同時有淡灰圖標及紅色叉號。
- `paused` 與 `suspended` 使用同一種雙豎線；掛起至磁碟的鎖定角標則是向下箭頭／托盤。
- `pending` 是黃色警告三角，並非旋轉載入動畫。
- 一般圖標本身也是深灰色；「已停止」指的是明顯變淡，不能只用「灰色」一詞區分。

## 類型與狀態的組合

資源類型使用基礎 Font Awesome 圖標；狀態在其上增加 class。例如插件自己的應用資源可使用：

```javascript
iconCls: 'fa fa-globe running'
```

樹圖標的相關樣式為 `.x-tree-icon-custom`；資源表格為 `.x-grid-icon-custom`。原生以 `:before` 呈現基礎圖標，以 `:after` 呈現小角標。預設角標相對位置為 `left: -5px`、`top: 1px`，字體大小 `0.75em`；雙豎線為 `0.6em`，維護扳手有獨立的大小設定。

`get_object_icon_class` 的查找依賴 PVE 已知的 `typeDefaults`。外部插件應直接組合自己的基礎圖標與已核對狀態 class，不需捏造 VM/CT 類型，也不需修改全域類型定義。

QEMU/LXC 模板另有類型角標：主圖標使用 `fa-file-o`，搭配 `qemu`／`lxc` 的桌面／方塊小角標。這是資源類型資訊，不是運行狀態。

## 插件使用原則

- 每個節點只選一種主要狀態角標。原生角標共用同一個 `:after`，疊加多個 class 的結果由 CSS 規則順序決定，不能用來同時展示所有狀態。
- 刷新時替換原狀態 class，保留資源類型圖標、資源 ID、選取及展開狀態。
- 狀態文字、tooltip 及詳情使用同一含義，讓顏色與圖形都有文字對應。
- 載入失敗／未知與資源停用／故障分開處理；缺少真實狀態時，不補成綠色運行圖標。
- 備份、遷移、掛起及 HA 維護角標有特定含義，只在業務確有對應狀態時使用。
- 共用宿主原生 CSS，不修改 PVE 官方檔案。

SafeLine 各類資源的具體映射是另一層設計，見 [SafeLine 設計草案](SAFELINE-DESIGN.md#safeline-狀態映射)。本參考文件記錄原生能力，不表示 SafeLine 插件已實作或部署。

2026-10-18 實作驗收補充：以實際 PVE `9.2.21` 的 ExtJS `7.0.0` 與原生 CSS 載入 SafeLine 頁面，已目視確認綠色三角、黃色雙豎線、憑證綠色勾及 IP 組中性圖標的組合。此驗收使用本地頁面及真實 SafeLine API 資料，不表示已登入正式 PVE 做瀏覽器驗收。
