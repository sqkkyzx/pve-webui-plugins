(function () {
    // ExtJS callParent relies on caller metadata; keep ordinary script functions.
    const Ext = window.Ext, marker = '__pveSafeLineLoaded', viewId = 'pve-safeline-waf', api = '/pve-plugins/api/safeline';
    if (window[marker] || !Ext || !Ext.ClassManager || typeof Ext.define !== 'function') return;
    const Selector = Ext.ClassManager.get('PVE.form.ViewSelector'), Tree = Ext.ClassManager.get('PVE.tree.ResourceTree');
    const Workspace = Ext.ClassManager.get('PVE.StdWorkspace');
    if (![Selector, Tree].every(function (type) { return type && type.prototype && typeof type.prototype.initComponent === 'function'; }) ||
        !Workspace || !Workspace.prototype || typeof Workspace.prototype.onLogin !== 'function') return;

    const states = new Set();
    const types = [
        { id: 'applications', name: '防護應用', icon: 'fa-globe' }, { id: 'deny', name: '黑名單', icon: 'fa-ban' },
        { id: 'allow', name: '白名單', icon: 'fa-check-circle-o' }, { id: 'certificates', name: 'TLS 憑證', icon: 'fa-certificate' },
        { id: 'ip-groups', name: 'IP 組', icon: 'fa-list-alt' },
    ];
    const errors = {
        safeline_not_configured: '尚未設定 SafeLine 連線', safeline_invalid_credentials: 'Token 無效或已過期',
        safeline_permission_denied: 'SafeLine 拒絕存取此功能', safeline_endpoint_unavailable: '此接口目前不可用',
        safeline_sso_required: '管理地址要求互動登入，請改用可直接驗證 API Token 的管理地址',
        safeline_timeout: '連線逾時', safeline_tls_error: 'TLS 憑證驗證失敗，請檢查憑證主機名及 CA',
        safeline_unreachable: '無法連到 SafeLine', safeline_business_error: 'SafeLine 回報操作失敗',
        safeline_unexpected_response: 'SafeLine 回應格式不符', safeline_connection_changed: '連線設定已變更，請重新讀取後再操作',
        safeline_token_required: '請填寫 Token；變更地址或憑證主機名後需要重新填寫', invalid_safeline_url: '請使用不含路徑或帳密的 HTTPS 管理地址',
        invalid_safeline_token: 'Token 格式無效', invalid_ca_certificate: 'CA 憑證格式無效', invalid_tls_server_name: '憑證主機名格式無效',
        invalid_tls_settings: 'TLS 設定無效', safeline_resource_not_found: '此資源已不存在', safeline_resource_moved: '資源分類已變更，請刷新資源樹',
        safeline_store_unavailable: '無法讀寫本地連線設定', safeline_instance_mismatch: '連線資料屬於其他 PVE 實例',
        invalid_csrf_token: '會話校驗已更新，請重新進入此視圖', invalid_safeline_request: '設定格式無效',
        safeline_resource_changed: '資源已被修改，請關閉視窗後重新編輯', safeline_edit_unsupported: '此資源的這些欄位目前無法安全編輯',
        safeline_edit_incomplete: '未取得完整資源內容，無法編輯', safeline_operation_conflict: '此操作編號已用於另一項變更，請重新開啟編輯視窗',
        safeline_operation_pending: '先前操作尚未確認，請先核對 SafeLine 中的實際設定', safeline_write_unknown: '寫入結果尚未確認，請刷新核對實際設定；不會自動重送',
        safeline_operation_store_full: '操作紀錄已滿，暫時無法寫入', invalid_safeline_domains: '域名格式無效', invalid_safeline_ports: '端口需為 1–65535，TLS 端口請加 _ssl',
        invalid_safeline_upstreams: '回源請填寫完整的 HTTP 或 HTTPS 地址及端口', invalid_safeline_ips: 'IP、CIDR 或 IP 範圍格式無效',
        invalid_safeline_certificate: '請檢查 PEM 憑證及私鑰格式',
        invalid_safeline_advanced: '高級配置格式無效，請檢查協定、Host、標頭及數值選項',
        invalid_safeline_source_ip: '請檢查源 IP 方式、XFF 位置（1–100）及代理 IP／CIDR',
        invalid_safeline_headers: '請檢查 Header 目標、動作、名稱和值；敏感標頭改名或改目標時需填入新值',
        safeline_feature_unavailable: '此功能需 SafeLine 專業版或商業版授權',
        safeline_business_required: '此功能需 SafeLine 商業版授權', safeline_portal_disabled: '統一認證入口尚未啟用',
        safeline_forwarding_limit: '已達目前版本的轉發規則上限', invalid_safeline_application: '設定格式無效，請檢查必填欄位、數值及選項',
        safeline_statistics_scope_unavailable: '目前版本不支援此區塊的應用篩選',
        safeline_statistics_range_unavailable: '目前版本不支援此區塊的統計時段',
    };
    function alive(c) { return c && !c.destroyed && !c.destroying; }
    function enc(v) { return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
    function readable(v) {
        if (Array.isArray(v) && v.every(function (item) { return typeof item !== 'object'; })) return v.length ? v.join(', ') : '—';
        return v == null ? '—' : typeof v === 'boolean' ? v ? '是' : '否' : typeof v === 'object' ? JSON.stringify(v, null, 2) : String(v);
    }
    function portLabel(v) { return String(v).replace(/_ssl$/, ' (TLS)'); }
    function date(v, milliseconds) {
        if (v == null || v === '') return '—';
        const d = new Date(typeof v === 'number' ? v * (milliseconds ? 1 : 1000) : v);
        return Number.isNaN(d.getTime()) ? String(v) : d.toLocaleString();
    }
    function message(code) { return errors[code] || '讀取失敗，請稍後刷新'; }
    function user(state) {
        const p = window.Proxmox;
        if (!state.active || !alive(state.workspace) || state.workspace._pveSafeLineLoggedOut || !p || !p.Utils || typeof p.Utils.authOK !== 'function') return null;
        try {
            const who = p.Utils.authOK() && typeof p.UserName === 'string' && p.UserName ? p.UserName : null;
            if (!who || state.user && state.user !== who) { if (state.user) leave(state); return null; }
            state.user = who; return who;
        } catch (e) { if (state.user) leave(state); return null; }
    }
    function current(state, generation, who) { return state.active && alive(state.tree) && generation === state.generation && user(state) === who; }
    function status(state, value) { const c = alive(state.tree) && state.tree.down('#safeline-tree-status'); if (c) c.setText(enc(value)); }
    function cancel(state, all) {
        state.navigation++;
        if (state.attackPages) state.attackPages.stop();
        state.overviewSerial = (state.overviewSerial || 0) + 1; state.overviewPending = false;
        if (all) {
            state.generation++; if (state.timer) window.clearTimeout(state.timer); state.timer = null;
            state.overview = null; state.dashboardCaps = null; state.overviewFilters = { preset: 'last1Day', siteId: null };
            state.geoRegion = 'world'; state.geoMode = 'access';
        }
        Array.from(state.requests).forEach(function (entry) {
            if (all || entry.page) { state.requests.delete(entry); if (entry.request) Ext.Ajax.abort(entry.request); }
        });
    }
    function leave(state) {
        state.active = false; cancel(state, true);
        if (alive(state.editor)) state.editor.destroy(); state.editor = null;
        state.connection = null; state.inventory = null; state.overview = null; state.health = null; state.origin = null; state.user = null;
        state.selection = { kind: 'root' }; state.tab = null; state.logFilters = {}; state.logPage = 1; state.logMode = 'events'; state.logSource = 'general'; state.logHours = 0; state.logAutoRefresh = 0; state.logRelated = false;
        if (alive(state.page)) state.page.destroy(); state.page = null;
        const root = state.tree.getRootNode(); if (root) root.removeAll();
    }
    function request(state, path, options, done, failed, page) {
        const generation = state.generation, who = user(state), navigation = state.navigation;
        if (!who) return;
        const entry = { page: !!page, request: null }; state.requests.add(entry);
        let completed = false;
        function complete(opts, success, response) {
            if (completed) return; completed = true; state.requests.delete(entry);
            if (!current(state, generation, who) || page && navigation !== state.navigation) return;
            if (response && (response.status === 401 || response.status === 403)) {
                leave(state); status(state, response.status === 401 ? 'PVE 會話已過期，請重新登入' : '無存取權限或會話校驗已失效'); return;
            }
            let data; try { data = JSON.parse(response.responseText); } catch (e) { data = null; }
            if (!success || !data || data.user !== who) { if (failed) failed(data && data.detail || 'safeline_unexpected_response'); return; }
            if (state.connection && data.connection_revision != null && data.connection_revision !== state.connection.revision) {
                cancel(state, true); state.inventory = null; state.selection = { kind: 'root' }; state.tab = 'connection';
                loadConnection(state); return;
            }
            done(data);
        }
        try { entry.request = Ext.Ajax.request(Object.assign({ url: api + path, method: 'GET', disableCaching: false, timeout: 45000, callback: complete }, options)); }
        catch (e) { complete(null, false, null); }
        return entry;
    }
    function type(kind) { return types.find(function (t) { return t.id === kind; }) || { name: '防護規則', icon: 'fa-shield' }; }
    function icon(resource) {
        const code = resource.stale ? 'unknown' : resource.status && resource.status.code;
        return 'fa ' + type(resource.kind).icon + (['running', 'paused', 'stopped', 'online', 'pending', 'error', 'unknown'].includes(code) ? ' ' + code : '');
    }
    function resourceKey(kind, id) { return kind + ':' + id; }
    function findResource(state) {
        const s = state.selection;
        const rows = state.inventory && (s.kind === 'policies' ? state.inventory.policies : state.inventory.resources[s.kind]);
        return Array.isArray(rows) && rows.find(function (row) { return row.id === s.id; });
    }
    function syncNodes(parent, desired) {
        const wanted = new Set(desired.map(function (v) { return v.id; }));
        parent.childNodes.slice().forEach(function (node) { if (!wanted.has(node.getId())) node.remove(true); });
        desired.forEach(function (data) {
            let node = parent.findChild('id', data.id);
            const children = data.children; const props = Object.assign({}, data); delete props.children;
            if (node) node.set(props); else node = parent.appendChild(Object.assign({}, props, { expanded: !props.leaf, loaded: true, children: props.leaf ? undefined : [] }));
            if (children) syncNodes(node, children);
        });
    }
    function renderTree(state) {
        if (!state.active || !alive(state.tree)) return;
        const inventory = state.inventory;
        const root = state.tree.getRootNode();
        const nodes = types.map(function (t) {
            const values = inventory && inventory.resources[t.id] || [];
            const error = inventory && inventory.errors[t.id];
            const names = new Map(); values.forEach(function (r) { names.set(r.name, (names.get(r.name) || 0) + 1); });
            return { id: 'type:' + t.id, kind: t.id, text: t.name + (error ? ' (?)' : inventory ? ' (' + values.length + (inventory.truncated[t.id] ? '+' : '') + ')' : ''),
                iconCls: 'fa ' + t.icon, leaf: false, children: values.map(function (r) {
                    return { id: resourceKey(r.kind, r.id), kind: r.kind, resourceId: r.id, text: r.name + (names.get(r.name) > 1 ? ' · #' + r.id : ''), iconCls: icon(r),
                        qtip: r.stale ? '資料已過期；最後成功更新：' + date(r.observed_at || state.lastInventory) : r.status.label, leaf: true };
                }) };
        });
        syncNodes(root, nodes);
        const selected = state.selection.kind === 'root' ? root : state.tree.getStore().getNodeById(
            state.selection.id ? resourceKey(state.selection.kind, state.selection.id) : 'type:' + state.selection.kind);
        if (selected) state.tree.getSelectionModel().select(selected, false, true);
    }
    function show(state, config) {
        if (!alive(state.page)) return;
        const body = state.page.down('#safeline-body'); if (!body) return;
        body.removeAll(true); body.add(config);
    }
    function note(state, value) { show(state, { xtype: 'panel', border: false, bodyPadding: 18, html: '<p>' + enc(value) + '</p>' }); }
    function properties(values, title) {
        return { xtype: 'grid', border: false, title: title, store: { fields: ['name', 'value'], data: values.map(function (r) { return { name: r[0], value: readable(r[1]) }; }) },
            columns: [{ text: '項目', dataIndex: 'name', width: 180, renderer: enc },
                { text: '內容', dataIndex: 'value', flex: 1, renderer: function (v) { return '<div style="white-space:pre-wrap;overflow-wrap:anywhere;padding:3px 0">' + enc(v) + '</div>'; } }],
            viewConfig: { enableTextSelection: true, stripeRows: true } };
    }
    function grid(data, columns, action, hint) {
        const fields = Array.from(new Set(['id', 'name'].concat(...data.map(function (row) { return Object.keys(row); }))));
        return { xtype: 'grid', border: false, store: { fields: fields, data: data, autoDestroy: true },
            columns: columns.map(function (c) { return Object.assign({ renderer: enc, minWidth: 85 }, c); }),
            viewConfig: { enableTextSelection: true, emptyText: '沒有符合條件的資料', deferEmptyText: false },
            tbar: [{ xtype: 'textfield', emptyText: '搜尋', width: 240, listeners: { change: function (field, value) {
                const store = field.up('grid').getStore(); store.clearFilter();
                if (value) store.filterBy(function (record) { return JSON.stringify(record.data).toLowerCase().includes(String(value).toLowerCase()); });
            } } }], bbar: [{ xtype: 'tbtext', text: enc(hint || (action ? '雙擊項目開啟詳情' : '')) }],
            listeners: { itemdblclick: function (view, record) { if (action) action(record.data); } } };
    }
    function choose(state, kind, id, tab) {
        state.selection = id ? { kind: kind, id: String(id) } : { kind: kind };
        state.tab = tab || null; state.logPage = 1; state.logFilters = {}; renderTree(state);
        const node = id && state.tree.getStore().getNodeById(resourceKey(kind, String(id)));
        if (node && node.parentNode && typeof node.parentNode.expand === 'function') node.parentNode.expand();
        renderSelection(state);
    }
    function switchMark(value, metadata, record) {
        if (record && record.data.stale || typeof value !== 'boolean') return '<span aria-label="狀態未知">—</span>';
        return value ? '<span class="fa fa-check" role="img" aria-label="啟用"></span>' : '<span role="img" aria-label="停用"></span>';
    }
    function resourceColumns(kind, compact) {
        const name = { text: '名稱', dataIndex: 'name', flex: 2, minWidth: 180 };
        const statusColumn = { text: '狀態', width: 125, renderer: function (v, m, r) { return enc(r.data.stale ? '資料已過期' : r.data.status.label); } };
        if (kind === 'applications') {
            const columns = [name, statusColumn, { text: '域名', dataIndex: 'domains', flex: 2, minWidth: 180, renderer: function (v) { return enc((v || []).join(', ')); } },
                { text: '監聽', dataIndex: 'ports', width: 115, renderer: function (v) { return enc((v || []).map(portLabel).join(', ')); } }];
            if (compact) return columns;
            columns.splice(1, 0, { text: '說明', dataIndex: 'comment', flex: 2, minWidth: 180 });
            function count(v, m, r) { return r.data.stale || typeof v !== 'number' || !Number.isFinite(v) || v < 0 ? '—' : enc(amount(v)); }
            return columns.concat([{ text: '今日請求', dataIndex: 'req_value', width: 110, align: 'right', renderer: count },
                { text: '今日攔截', dataIndex: 'denied_value', width: 110, align: 'right', renderer: count }],
                [['cc_enabled', 'CC 防護'], ['bot_enabled', 'BOT 防護'], ['auth_enabled', '身份認證'], ['attack_enabled', '攻擊防護']].map(function (item) {
                    return { text: item[1], dataIndex: item[0], width: 85, align: 'center', renderer: switchMark };
                }));
        }
        if (kind === 'certificates') return [name, statusColumn, { text: '簽發者', dataIndex: 'issuer', flex: 1 }, { text: '到期', dataIndex: 'valid_before', width: 175, renderer: date }];
        if (kind === 'ip-groups') return [name, { text: '成員數', dataIndex: 'total', width: 95 }, { text: '內建', dataIndex: 'builtin', width: 85, renderer: function (v) { return enc(readable(v)); } },
            { text: '更新時間', dataIndex: 'updated_at', width: 180, renderer: date }];
        return [name, statusColumn, { text: '動作', dataIndex: 'action', width: 100, renderer: function (v) { return enc(({ 0: '放行', 1: '封禁', 2: '驗證', 3: '認證', 4: '動態防護', 5: '等候室' })[v] || '動作 ' + v); } },
            { text: '順序', dataIndex: 'order', width: 85 }, { text: '內建', dataIndex: 'builtin', width: 85, renderer: function (v) { return enc(readable(v)); } }];
    }
    function resourceList(state, kind, values) {
        const inventory = state.inventory;
        if (!inventory) { note(state, '正在讀取資源…'); return; }
        const rows = values || (kind === 'policies' ? inventory.policies : inventory.resources[kind]) || [];
        show(state, grid(rows, resourceColumns(kind), function (row) { choose(state, row.kind, row.id); },
            (inventory.errors[kind] ? message(inventory.errors[kind]) + '；' : '') + (inventory.truncated[kind] ? '列表未完整載入；' : '') + '共 ' + rows.length + ' 項，雙擊開啟詳情'));
    }
    function amount(value, compact) {
        if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return '不可用';
        if (compact && value >= 1000000) return (value / 1000000).toFixed(1) + 'm';
        if (compact && value >= 1000) return (value / 1000).toFixed(1) + 'k';
        return value.toLocaleString(undefined, { maximumFractionDigits: 1 });
    }
    function total(values) { return values.length && values.every(function (v) { return typeof v === 'number' && Number.isFinite(v) && v >= 0; }) ? values.reduce(function (a, b) { return a + b; }, 0) : null; }
    function rate(value, requests) { return typeof value === 'number' && typeof requests === 'number' && requests > 0 ? (value * 100 / requests).toFixed(2) + '%' : '—'; }
    function dashboardMetric(label, value, options) {
        const o = options || {}, display = o.display == null ? amount(value, true) : o.display;
        return '<div class="sl-metric"><div class="sl-label" title="' + enc(o.tip || label) + '">' + enc(label) +
            '<i aria-hidden="true" class="fa ' + enc(o.icon || 'fa-circle-o') + (o.error ? ' sl-red' : '') + '"></i></div>' +
            '<strong title="' + enc(o.tip || amount(value)) + '">' + enc(display) + '</strong></div>';
    }
    function dashboardChart(title, series, options) {
        const o = options || {}, datasets = series || [];
        const valid = datasets.flatMap(function (s) { return s.points || []; }).filter(function (p) { return typeof p.count === 'number' && Number.isFinite(p.count) && p.count >= 0; });
        const peak = valid.length ? Math.max(...valid.map(function (p) { return p.count; })) : null;
        const latest = datasets[0] && (datasets[0].points || []).slice(-1)[0];
        const heading = '<div class="sl-card-heading"><h3>' + enc(title) + '</h3><span>' + enc(o.bars ? '即時 ' + amount(latest && latest.count) : peak == null ? '' : '峰值 ' + amount(peak, true)) + '</span></div>';
        if (!valid.length) return heading + '<div class="sl-empty">' + enc(o.error ? message(o.error) : '暫無可用資料') + '</div>';
        const width = 560, height = o.height || 130, left = 8, right = 552, bottom = height - 8, top = 8, maximum = Math.max(1, peak);
        const numeric = valid.every(function (p) { return typeof p.time === 'number'; });
        const times = numeric ? valid.map(function (p) { return p.time; }) : [];
        const first = numeric ? Math.min(...times) : 0, last = numeric ? Math.max(...times) : 0;
        const x = function (p, i, length) { return left + (right-left) * (numeric && last > first ? (p.time-first)/(last-first) : i / Math.max(1, length-1)); };
        const y = function (v) { return bottom - v * (bottom-top) / maximum; };
        let svg = '<svg role="img" aria-label="' + enc(title) + '" viewBox="0 0 ' + width + ' ' + height + '" preserveAspectRatio="none" style="height:' + height + 'px">';
        [0, 0.5, 1].forEach(function (part) {
            const level = top + part * (bottom-top);
            svg += '<line x1="' + left + '" x2="' + right + '" y1="' + level + '" y2="' + level + '" stroke="currentColor" opacity="0.08" vector-effect="non-scaling-stroke"/>';
        });
        datasets.forEach(function (s, seriesIndex) {
            const points = s.points || [], color = s.color || (seriesIndex ? '#ff6578' : o.color || '#14bfc3');
            if (o.bars) {
                const barWidth = Math.max(1, (right-left) / Math.max(1, points.length) * 0.64);
                points.forEach(function (p, i) {
                    if (typeof p.count !== 'number') return;
                    svg += '<rect x="' + Math.min(right-barWidth, x(p,i,points.length)).toFixed(2) + '" y="' + y(p.count).toFixed(2) + '" width="' + barWidth.toFixed(2) + '" height="' + Math.max(0, bottom-y(p.count)).toFixed(2) + '" rx="2" fill="' + color + '"><title>' + enc(p.time + ' · ' + amount(p.count) + ' QPS') + '</title></rect>';
                });
            } else {
                let segment = [];
                function flush() { if (segment.length) svg += '<polyline points="' + segment.join(' ') + '" fill="none" stroke="' + color + '" stroke-width="2.5" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>'; segment = []; }
                points.forEach(function (p, i) { if (typeof p.count !== 'number') flush(); else segment.push(x(p,i,points.length).toFixed(2) + ',' + y(p.count).toFixed(2)); }); flush();
            }
        });
        const label = function (p) { return numeric ? new Date(p.time * 1000).toLocaleString(undefined, { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : String(p.time); };
        const ordered = valid.slice().sort(function (a,b) { return numeric ? a.time-b.time : 0; });
        svg += '</svg>' + (o.bars ? '' : '<div class="sl-axis-labels"><span>' + enc(label(ordered[0])) + '</span><span>' + enc(label(ordered[ordered.length-1])) + '</span></div>');
        const legend = datasets.length > 1 ? '<div class="sl-legend">' + datasets.map(function (s,i) { return '<span><i style="background:' + (s.color || (i ? '#ff6578' : '#14bfc3')) + '"></i>' + enc(s.name) + '</span>'; }).join('') + '</div>' : '';
        return heading + svg + legend;
    }
    function dashboardBars(rows, error, empty) {
        if (error || !rows || !rows.length) return '<div class="sl-empty">' + enc(error ? message(error) : empty || '沒有符合條件的資料') + '</div>';
        const maximum = Math.max(1, ...rows.map(function (r) { return r.count || 0; }));
        return '<ol class="sl-ranking">' + rows.map(function (r) {
            return '<li><div><span title="' + enc(r.name) + '">' + enc(r.name || '未知') + '</span><b title="' + enc(amount(r.count)) + '">' + enc(amount(r.count,true)) +
                '</b></div><div class="sl-track"><span style="width:' + Math.min(100, Math.max(0,(r.count || 0) / maximum*100)).toFixed(2) + '%"></span></div></li>';
        }).join('') + '</ol>';
    }
    function dashboardGeo(state, d) {
        const key = 'geo_' + state.geoRegion + '_' + state.geoMode, values = d.sections[key], error = d.errors[key];
        let regionNames;
        try { regionNames = new Intl.DisplayNames(['zh-Hant'], { type: 'region' }); } catch (e) { regionNames = null; }
        const rows = (values || []).map(function (r) {
            let name = state.geoRegion === 'china' ? r.province || '中國（省份未知）' : r.country || '未知地區';
            if (state.geoRegion === 'world' && regionNames && /^[A-Z]{2}$/.test(name)) { try { name = regionNames.of(name) || name; } catch (e) { /* Preserve unknown codes. */ } }
            return { name: name, count: r.count };
        });
        function button(group, value, label) { return '<button type="button" data-safeline-geo-' + group + '="' + value + '" aria-pressed="' + ((group === 'region' ? state.geoRegion : state.geoMode) === value) + '">' + enc(label) + '</button>'; }
        return '<div class="sl-card-heading"><h3>地理位置</h3><div class="sl-switches"><div role="group" aria-label="地理範圍">' + button('region','world','世界') + button('region','china','中國') +
            '</div><div role="group" aria-label="流量類型">' + button('mode','access','訪問') + button('mode','intercept','僅攔截') + '</div></div></div>' +
            '<div class="sl-geo-summary"><span>' + enc((state.geoRegion === 'world' ? '世界' : '中國') + ' · ' + (state.geoMode === 'access' ? '訪問' : '僅攔截')) +
            '</span><b>' + enc(error || !values ? '不可用' : amount(rows.length ? total(rows.map(function (r) { return r.count; })) : 0, true)) + '</b></div>' +
            '<div class="sl-geo-ranking">' + dashboardBars(rows,error,'此範圍暫無地理位置資料') + '</div><div class="sl-footnote">' + enc(error || !values ? '地理位置依 SafeLine 統計資料顯示。' : rows.length + ' 個地區；數量僅含上游已定位資料，長條按最高值比較。') + '</div>';
    }
    function dashboardEvents(d) {
        const events = d.sections.realtime, labels = { 1: '攻擊防護', 2: '黑白名單', 3: '頻率限制', 4: '人機驗證', 5: '等候室', 6: '身份認證', 7: '網頁防篡改' };
        let html = '<div class="sl-card-heading"><h3>實時事件</h3><span>最近 7 筆</span></div><div class="sl-events">';
        if (d.errors.realtime || !events || !events.length) html += '<div class="sl-empty">' + enc(d.errors.realtime ? message(d.errors.realtime) : '暫無事件') + '</div>';
        else {
            let previous = '';
            events.forEach(function (r) {
                const timestamp = typeof r.time === 'number' ? new Date(r.time * 1000) : null;
                const day = timestamp && !Number.isNaN(timestamp.getTime()) ? timestamp.toLocaleDateString() : '時間未知';
                if (day !== previous) { html += '<div class="sl-event-date">' + enc(day) + '</div>'; previous = day; }
                html += '<div class="sl-event"><span class="sl-badge" title="' + enc(r.attack_type || labels[r.type] || '事件') + '">' + enc(labels[r.type] || '事件') + '</span>' +
                    '<span class="sl-event-site" title="' + enc(r.site_name || '應用 #' + r.site_id) + '">' + enc(r.site_name || '應用 #' + r.site_id) + '</span><time>' + enc(timestamp && !Number.isNaN(timestamp.getTime()) ? timestamp.toLocaleTimeString() : '—') + '</time></div>';
            });
        }
        return html + '</div><div class="sl-footnote">最新事件，獨立於時間篩選。</div>';
    }
    const dashboardStyle = '<style>' +
        '.sl-dashboard{padding:0 12px 12px;font-size:13px;container:safeline-dashboard / inline-size}.sl-dashboard h2{font-size:15px;margin:12px 0;font-weight:600}.sl-dashboard h3{font-size:13px;font-weight:600;margin:0}' +
        '.sl-dashboard .sl-card{border:1px solid #8882;border-radius:4px;box-shadow:0 2px 8px #00000009;padding:16px;min-width:0;background:inherit}' +
        '.sl-dashboard .sl-card-heading{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:16px}.sl-dashboard .sl-card-heading>span{font-size:12px;opacity:.7;white-space:nowrap}' +
        '.sl-dashboard .sl-traffic-layout,.sl-dashboard .sl-security-layout{display:grid;grid-template-columns:minmax(0,3fr) minmax(240px,1fr);gap:14px}.sl-dashboard .sl-traffic-content,.sl-dashboard .sl-security-content{display:grid;gap:14px;min-width:0}' +
        '.sl-dashboard .sl-traffic-stats{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:12px}.sl-dashboard .sl-request-card{grid-column:span 4}.sl-dashboard .sl-intercept-card,.sl-dashboard .sl-error-card{grid-column:span 2}' +
        '.sl-dashboard .sl-metrics{display:grid;grid-template-columns:repeat(var(--sl-count),minmax(0,1fr));gap:0;padding:4px 0}' +
        '.sl-dashboard .sl-metric{min-width:0;padding:0 14px;border-left:1px solid #8882}.sl-dashboard .sl-metric:first-child{padding-left:0;border-left:0}.sl-dashboard .sl-metric:last-child{padding-right:0}' +
        '.sl-dashboard .sl-label{opacity:.65;white-space:nowrap;text-overflow:ellipsis;overflow:hidden;font-size:12px;margin-bottom:12px}.sl-dashboard .sl-label .fa{color:#00b9bc;margin-left:8px}.sl-dashboard .sl-label .sl-red{color:#f46372}' +
        '.sl-dashboard .sl-metric strong{font-size:22px;font-weight:600;white-space:nowrap}.sl-dashboard svg{display:block;width:100%;max-height:220px;overflow:visible}.sl-dashboard svg text{font-size:10px;fill:currentColor;opacity:.55}' +
        '.sl-dashboard .sl-error-card .sl-label{font-size:11px;letter-spacing:-.1px}.sl-dashboard .sl-error-card .sl-label .fa{margin-left:4px;font-size:9px}.sl-dashboard .sl-traffic-charts{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.sl-dashboard .sl-traffic-charts>.sl-card{display:flex;flex-direction:column}.sl-dashboard .sl-geo-card,.sl-dashboard .sl-realtime-card{display:flex;flex-direction:column;min-height:0;contain:size}.sl-dashboard .sl-geo-card .sl-card-heading{flex-wrap:wrap;align-items:flex-start}' +
        '.sl-dashboard .sl-switches{display:flex;gap:12px;flex-wrap:wrap}.sl-dashboard .sl-switches>div{display:flex;border:1px solid #8883;border-radius:4px;padding:3px;gap:3px}.sl-dashboard .sl-switches button{border:0;background:transparent;color:inherit;border-radius:3px;font:inherit;font-size:12px;padding:5px 12px;cursor:pointer}' +
        '.sl-dashboard .sl-switches button[aria-pressed=true]{background:#08bdc0;color:white}.sl-dashboard .sl-switches button:focus-visible{outline:2px solid #3892d4;outline-offset:2px}.sl-dashboard .sl-geo-summary{display:flex;justify-content:space-between;padding:10px 0;border-bottom:1px solid #8882}.sl-dashboard .sl-geo-summary>b{color:#0ab9bc;font-size:20px}' +
        '.sl-dashboard .sl-geo-ranking{height:0;flex:1;min-height:0;overflow:auto;padding-right:4px}.sl-dashboard .sl-ranking{list-style:none;margin:0;padding:0}.sl-dashboard .sl-ranking li{padding:13px 0}.sl-dashboard .sl-ranking li>div:first-child{display:flex;justify-content:space-between;gap:16px}.sl-dashboard .sl-ranking li span[title]{max-width:80%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}' +
        '.sl-dashboard .sl-track{height:4px;border-radius:3px;background:#8882;margin-top:8px;overflow:hidden}.sl-dashboard .sl-track>span{display:block;height:100%;background:#0bbfc1;border-radius:3px}.sl-dashboard .sl-footnote{opacity:.55;font-size:11px;line-height:1.6;margin-top:10px}' +
        '.sl-dashboard .sl-security-metrics{--sl-count:7}.sl-dashboard .sl-security-chart{display:grid;grid-template-columns:minmax(0,3fr) minmax(160px,2fr);gap:16px;align-items:start}.sl-dashboard .sl-security-chart>.sl-source-ips{padding:14px;background:#88888807;border-radius:3px;min-height:230px;max-height:280px;overflow:auto}' +
        '.sl-dashboard .sl-empty{min-height:140px;display:flex;align-items:center;justify-content:center;text-align:center;opacity:.55;padding:16px;line-height:1.7}.sl-dashboard .sl-traffic-charts .sl-empty{flex:1;min-height:160px}.sl-dashboard .sl-legend{display:flex;justify-content:center;gap:20px;font-size:11px;opacity:.7}.sl-dashboard .sl-legend i{display:inline-block;width:12px;height:3px;margin-right:6px;vertical-align:middle}' +
        '.sl-dashboard .sl-axis-labels{display:flex;justify-content:space-between;font-size:10px;opacity:.5;margin-top:4px;gap:10px}.sl-dashboard .sl-events{height:0;flex:1;min-height:0;overflow:auto}.sl-dashboard .sl-event-date{font-size:11px;display:table;padding:3px 9px;background:#8881;border-radius:3px;margin:8px 0}.sl-dashboard .sl-event{display:flex;gap:9px;align-items:center;margin:12px 0;font-size:11px;min-width:0}.sl-dashboard .sl-badge{border:1px solid #12bfc2;color:#08aeb1;background:#0db9bb0c;border-radius:3px;padding:2px 5px;white-space:nowrap}.sl-dashboard .sl-event-site{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.sl-dashboard .sl-event time{opacity:.6;white-space:nowrap;font-size:10px}' +
        '@container safeline-dashboard (max-width:1450px){.sl-dashboard .sl-card{padding:12px}.sl-dashboard .sl-metric{padding:0 9px}.sl-dashboard .sl-metric strong{font-size:20px}.sl-dashboard .sl-switches{gap:5px}.sl-dashboard .sl-switches button{padding:5px 8px}}' +
        '@container safeline-dashboard (max-width:1120px){.sl-dashboard .sl-traffic-layout,.sl-dashboard .sl-security-layout{grid-template-columns:1fr}.sl-dashboard .sl-geo-card,.sl-dashboard .sl-realtime-card{height:300px}.sl-dashboard .sl-security-metrics{--sl-count:4;row-gap:18px}.sl-dashboard .sl-security-metrics .sl-metric:nth-child(5){border-left:0;padding-left:0}}' +
        '@container safeline-dashboard (max-width:760px){.sl-dashboard .sl-traffic-charts,.sl-dashboard .sl-security-chart{grid-template-columns:1fr}.sl-dashboard .sl-card-heading{flex-wrap:wrap}}' +
        '@container safeline-dashboard (max-width:600px){.sl-dashboard .sl-request-card,.sl-dashboard .sl-intercept-card,.sl-dashboard .sl-error-card{grid-column:span 6}.sl-dashboard .sl-security-metrics{--sl-count:2}.sl-dashboard .sl-security-metrics .sl-metric:nth-child(odd){border-left:0;padding-left:0}.sl-dashboard .sl-geo-card{height:340px}}' + '</style>';
    function dashboardBody(state, d) {
        const a = d.sections.access || {}, attack = d.sections.attack || {}, up = d.sections.error_upstream || {}, waf = d.sections.error_waf || {}, posture = d.sections.posture || {};
        const intercepted = total(['block','blacklist','challenge','rate_limit','auth_defense','offline'].map(function (k) { return attack.intercept && attack.intercept[k]; }));
        const pair = function (label, value, requests, tip) { return '<div class="sl-card sl-error-card"><div class="sl-metrics" style="--sl-count:2">' + dashboardMetric(label + '數',value,{error:true,tip:tip}) + dashboardMetric(label + '率',null,{display:rate(value,requests),error:true,tip:tip}) + '</div></div>'; };
        let html = dashboardStyle + '<div class="sl-dashboard"><h2>流量分析</h2><div class="sl-traffic-layout"><div class="sl-traffic-content"><div class="sl-traffic-stats">' +
            '<div class="sl-card sl-request-card"><div class="sl-metrics" style="--sl-count:4">' + dashboardMetric('請求次數',a.access,{icon:'fa-user'}) + dashboardMetric('訪問次數（PV）',a.pv,{icon:'fa-paper-plane'}) + dashboardMetric('獨立訪客（UV）',a.session,{icon:'fa-users'}) + dashboardMetric('獨立 IP',a.ip,{icon:'fa-map-marker'}) + '</div></div>' +
            '<div class="sl-card sl-intercept-card"><div class="sl-metrics" style="--sl-count:2">' + dashboardMetric('攔截次數',intercepted,{icon:'fa-shield',tip:'SafeLine 各攔截類別計數之和'}) + dashboardMetric('攻擊 IP',attack.attack_ip,{icon:'fa-map-marker'}) + '</div></div>' +
            pair('4xx 錯誤',up.error_4xx,a.access,'上游回應 4xx；比率以所選範圍請求次數為分母') + pair('4xx 攔截',waf.error_4xx,a.access,'SafeLine 回應 4xx，包含其 4xx 回應；比率以請求次數為分母') +
            pair('5xx 錯誤',total([up.error_5xx,waf.error_5xx]),a.access,'上游與 SafeLine 的 5xx 回應之和；比率以請求次數為分母') + '</div>';
        const trafficError = d.errors.access;
        if (trafficError) html += '<p class="sl-footnote">' + enc(message(trafficError)) + '</p>';
        html += '<div class="sl-traffic-charts"><section class="sl-card">' + dashboardChart('即時 QPS',[{points:d.sections.qps}],{bars:true,height:160,error:d.errors.qps}) + '<div class="sl-footnote">最新五秒區間，每 30 秒更新。</div></section>' +
            '<section class="sl-card">' + dashboardChart('訪問情況',[{points:d.sections.access_trend}],{height:160,color:'#279cec',error:d.errors.access_trend}) +
            '</section><section class="sl-card">' + dashboardChart('攔截情況',[{points:d.sections.intercept_trend}],{height:160,color:'#ff6578',error:d.errors.intercept_trend}) + '</section></div></div><section class="sl-card sl-geo-card">' + dashboardGeo(state,d) + '</section></div>';
        const defenses = [['攻擊防護',total([posture.attack_deny,posture.attack_allow]),'fa-shield'],['黑白名單',total([posture.black_hit,posture.white_hit]),'fa-list-alt'],['頻率限制',posture.acl_hit,'fa-line-chart'],
            ['等候室',posture.waiting_hit,'fa-hourglass-half'],['人機驗證',total([posture.challenge_deny,posture.challenge_allow]),'fa-user-circle-o'],['身份認證',total([posture.auth_deny,posture.auth_allow]),'fa-key'],['網頁防篡改',Array.isArray(posture.anti_tamper) ? posture.anti_tamper.length ? total(posture.anti_tamper.map(function (v) { return v.count; })) : 0 : null,'fa-file-code-o']];
        html += '<h2>安全態勢</h2><div class="sl-security-layout"><div class="sl-security-content"><section class="sl-card"><div class="sl-metrics sl-security-metrics">' + defenses.map(function (v) { return dashboardMetric(v[0],v[1],{icon:v[2]}); }).join('') + '</div>' +
            (d.errors.posture ? '<p class="sl-footnote">' + enc(message(d.errors.posture)) + '</p>' : '') + '</section>';
        const trend = d.sections.posture_trend || {};
        html += '<section class="sl-card"><div class="sl-security-chart"><div>' + dashboardChart('攻擊防護趨勢',[{name:'已攔截',points:trend.denied,color:'#ff6578'},{name:'已放行',points:trend.allowed,color:'#279cec'}],{height:230,error:d.errors.posture_trend}) +
            '</div><div class="sl-source-ips"><h3>攻擊來源 IP</h3>' + dashboardBars(trend.sources,d.errors.posture_trend,'暫無攻擊來源 IP') + '</div></div></section></div><section class="sl-card sl-realtime-card">' + dashboardEvents(d) + '</section></div></div>';
        return html;
    }
    function overviewFilters(state) { return state.overviewFilters || (state.overviewFilters = { preset: 'last1Day', siteId: null }); }
    function ensureDashboard(state) {
        let panel = state.page.down('#safeline-dashboard'); if (alive(panel)) return panel;
        const filters = overviewFilters(state);
        show(state, { xtype:'panel',itemId:'safeline-dashboard',border:false,scrollable:'y',
            tbar:{enableOverflow:true,items:[{xtype:'tbtext',text:'流量分析與安全態勢'},{itemId:'safeline-overview-refresh',text:'刷新',iconCls:'fa fa-refresh',handler:function(){loadOverview(state,true);}},'->',
                {xtype:'combo',itemId:'safeline-overview-application',fieldLabel:'應用',labelWidth:35,width:235,queryMode:'local',editable:false,displayField:'name',valueField:'id',value:filters.siteId || 'all',
                    store:{fields:['id','name'],data:[{id:'all',name:'全部應用'}]},disabled:true,listeners:{select:function(combo){if(!state.dashboardSync){overviewFilters(state).siteId=combo.getValue()==='all' ? null : combo.getValue();loadOverview(state,true);}}}},
                {xtype:'combo',itemId:'safeline-overview-time',fieldLabel:'時間',labelWidth:35,width:170,queryMode:'local',editable:false,displayField:'name',valueField:'id',value:filters.preset,
                    store:{fields:['id','name'],data:[{id:'last1Day',name:'近 24 小時'}]},disabled:true,listeners:{select:function(combo){if(!state.dashboardSync){overviewFilters(state).preset=combo.getValue();loadOverview(state,true);}}}}]},
            items:[{xtype:'component',itemId:'safeline-overview-notice',padding:'10 12',style:'font-size:12px;line-height:1.7;opacity:.7',html:'正在讀取統計…'},
                {xtype:'component',itemId:'safeline-overview-body',html:'',listeners:{afterrender:function(component){component.getEl().on('click',function(event){
                    const target = event.getTarget('button[data-safeline-geo-region],button[data-safeline-geo-mode]');if(!target || !user(state))return;
                    const region=target.getAttribute('data-safeline-geo-region'), mode=target.getAttribute('data-safeline-geo-mode');
                    if(['world','china'].includes(region))state.geoRegion=region;else if(['access','intercept'].includes(mode))state.geoMode=mode;else return;
                    renderOverview(state);
                    const next=component.getEl().down('button[data-safeline-geo-'+(region?'region':'mode')+'="'+(region||mode)+'"]');if(next)next.focus();
                });}}},
                Object.assign(grid([],resourceColumns('applications',true),function(r){choose(state,r.kind,r.id);}),{itemId:'safeline-overview-attention',title:'資源提醒',hidden:true,height:180,margin:12,
                    store:{fields:['id','kind','name','status','domains','ports'],data:[]}})] });
        return state.page.down('#safeline-dashboard');
    }
    function renderOverview(state) {
        if (!state.active || state.selection.kind !== 'root' || state.tab !== 'overview' || !alive(state.page)) return;
        const panel = ensureDashboard(state), filters = overviewFilters(state), d = state.overview;
        const caps = d && d.capabilities || state.dashboardCaps;
        const app = panel.down('#safeline-overview-application'), timeFilter = panel.down('#safeline-overview-time');
        const appRows = [{id:'all',name:'全部應用'}].concat((state.inventory && state.inventory.resources.applications || []).map(function(r){return {id:r.id,name:r.name};}));
        const labels = {last1Day:'近 24 小時',today:'今天',last7Day:'近 7 天',last30Day:'近 30 天'};
        state.dashboardSync = true;
        const appKey=JSON.stringify(appRows), timeRows=(caps && caps.presets || ['last1Day']).map(function(p){return {id:p,name:labels[p]};}),timeKey=JSON.stringify(timeRows);
        if(panel._appRows!==appKey){app.getStore().loadData(appRows);panel._appRows=appKey;}if(panel._timeRows!==timeKey){timeFilter.getStore().loadData(timeRows);panel._timeRows=timeKey;}
        app.setValue(filters.siteId || 'all');timeFilter.setValue(filters.preset);app.setDisabled(!caps || !(caps.traffic.applications || caps.security.applications));timeFilter.setDisabled(!caps);
        state.dashboardSync=false;
        let notice = state.overviewPending ? '正在更新所選範圍…' : d ? '更新：'+date(d.updated_at)+' · '+date(d.begin_time)+' – '+date(d.end_time) : '正在讀取統計…';
        if(caps && !caps.traffic.applications && !caps.security.applications)notice+='；此版本的應用統計僅提供全站資料。';
        if(caps && caps.traffic.presets.length===1)notice+=caps.security.presets.includes('last7Day') ? ' 流量分析限近 24 小時；安全態勢可選近 7 天。' : ' 暫僅使用已確認的近 24 小時範圍。';
        panel.down('#safeline-overview-notice').update(enc(notice));
        const body = panel.down('#safeline-overview-body');
        body.update(d ? dashboardBody(state,d) : '<div style="padding:24px;opacity:.6">正在讀取流量分析與安全態勢…</div>');
        const inv=state.inventory;
        let attention = inv ? inv.resources.applications.filter(function(r){return ['error','pending'].includes(r.status.code) && (!filters.siteId || r.id===filters.siteId);}) : [];
        if(inv && !filters.siteId)attention=attention.concat(inv.resources.certificates.filter(function(r){return ['error','pending'].includes(r.status.code);}));
        const table=panel.down('#safeline-overview-attention');table.getStore().loadData(attention);table.setVisible(!!attention.length);
    }
    function loadOverview(state, changed) {
        if(!state.active || state.selection.kind!=='root' || state.tab!=='overview')return;
        if(changed){cancel(state,false);state.overview=null;}
        if(state.overviewPending)return;
        const filters=Object.assign({},overviewFilters(state)), serial=(state.overviewSerial || 0)+1;state.overviewSerial=serial;state.overviewPending=true;renderOverview(state);
        const params={preset:filters.preset,utc_offset:new Date().getTimezoneOffset()};if(filters.siteId)params.site_id=filters.siteId;
        request(state,'/overview?'+new URLSearchParams(params).toString(),{timeout:90000},function(data){
            if(serial!==state.overviewSerial)return;state.overviewPending=false;
            if(!data.sections || !data.errors || !data.capabilities || !data.filters || data.filters.preset!==filters.preset || String(data.filters.site_id || '')!==String(filters.siteId || '')){panelOverviewError(state,'safeline_unexpected_response');return;}
            state.overview=data;state.dashboardCaps=data.capabilities;renderOverview(state);
        },function(code){if(serial!==state.overviewSerial)return;state.overviewPending=false;panelOverviewError(state,code);},true);
    }
    function panelOverviewError(state, code) {
        const panel = ensureDashboard(state);panel.down('#safeline-overview-notice').update(enc(message(code)+(state.overview ? ' · 資料最後更新：'+date(state.overview.updated_at) : '')));
        if(!state.overview)panel.down('#safeline-overview-body').update('<div style="padding:24px">'+enc(message(code))+'</div>');
    }
    function renderConnection(state) {
        const d = state.connection, c = d.connection, h = state.health || {};
        show(state, Object.assign(properties([
            ['管理地址', c.url || '尚未設定'], ['API Token', c.token_configured ? '已設定（不顯示原文）' : '尚未設定'],
            ['TLS 憑證驗證', c.tls_verify], ['憑證主機名', c.tls_server_name || '使用管理地址的主機名'],
            ['自訂 CA', c.ca_pem ? '已設定' : '使用系統信任庫'], ['設定來源', d.source === 'saved' ? '頁面保存' : d.source === 'environment' ? '部署設定' : '尚未設定'],
            ['連線狀態', h.connected ? '已連線' : h.error ? message(h.error) : d.configured ? '尚未測試' : '待設定'],
            ['SafeLine 版本', h.version], ['版本類型', h.edition && h.edition.version === 0 ? '個人版' : h.edition], ['最近測試', h.updated_at ? date(h.updated_at) : '—'],
        ]), { tbar: [{ text: '編輯連線', iconCls: 'fa fa-pencil', handler: function () { editConnection(state); } },
            { text: '測試已保存連線', iconCls: 'fa fa-refresh', disabled: !d.configured, handler: function () { loadHealth(state, true); } },
            { text: '清除連線', iconCls: 'fa fa-unlink', disabled: !d.configured, handler: function () { clearConnection(state); } }] }));
    }
    function clearConnection(state) {
        if (!state.connection || !user(state) || alive(state.editor)) return;
        const d = state.connection, generation = state.generation, who = user(state);
        const dialog = Ext.create('Ext.window.MessageBox', { closeAction: 'destroy' }); state.editor = dialog;
        dialog.show({ title: '清除 SafeLine 連線', message: '確定清除插件的連線設定與 API Token？', buttons: Ext.Msg.YESNO, icon: Ext.Msg.WARNING,
            fn: function (answer) {
                if (state.editor === dialog) state.editor = null; if (alive(dialog)) dialog.destroy();
                if (answer !== 'yes' || !current(state, generation, who)) return;
                request(state, '/connection', { method: 'DELETE', headers: { 'X-PVE-Plugins-CSRF': d.csrf_token }, jsonData: { expected_revision: d.revision } }, function () {
                    cancel(state, true); state.inventory = null; state.overview = null; state.health = null;
                    state.selection = { kind: 'root' }; state.tab = 'connection'; loadConnection(state);
                }, function (code) { status(state, message(code)); }, false);
            } });
    }
    function editConnection(state) {
        if (!state.connection || !state.connection.csrf_token || !user(state)) return;
        if (alive(state.editor)) state.editor.destroy();
        const d = state.connection, c = d.connection, generation = state.generation, who = user(state);
        let editor, pending = false;
        function notice(value) { if (alive(editor)) editor.down('#safeline-edit-status').update(enc(value)); }
        function command(method, path) {
            if (pending || !alive(editor) || !current(state, generation, who)) return;
            const form = editor.down('form').getForm(); if (!form.isValid()) return;
            const values = form.getValues();
            const body = { url: String(values.url || '').trim(), token: String(values.token || ''), tls_verify: editor.down('#safeline-tls-verify').getValue(),
                tls_server_name: String(values.tls_server_name || '').trim(), ca_pem: String(values.ca_pem || '').trim(), expected_revision: d.revision };
            pending = true; editor.down('#safeline-save').setDisabled(true); editor.down('#safeline-test').setDisabled(true);
            notice(method === 'PUT' ? '正在保存…' : '正在測試…');
            request(state, path, { method: method, headers: { 'X-PVE-Plugins-CSRF': d.csrf_token }, jsonData: body }, function (result) {
                pending = false;
                if (!alive(editor)) return;
                if (method === 'PUT') {
                    editor.destroy(); state.editor = null; cancel(state, true); state.inventory = null; state.overview = null; state.health = null;
                    state.selection = { kind: 'root' }; state.tab = 'connection'; loadConnection(state);
                } else {
                    notice('連線成功 · SafeLine ' + result.version + '；此測試尚未保存設定');
                    editor.down('#safeline-save').setDisabled(false); editor.down('#safeline-test').setDisabled(false);
                }
            }, function (code) {
                pending = false; if (!alive(editor)) return;
                notice(message(code)); editor.down('#safeline-save').setDisabled(false); editor.down('#safeline-test').setDisabled(false);
            }, false);
        }
        editor = Ext.create('Ext.window.Window', { title: 'SafeLine 連線設定', width: 620, maxHeight: 720, modal: true, constrain: true, closeAction: 'destroy', layout: 'fit', scrollable: true,
            items: [{ xtype: 'form', border: false, bodyPadding: 16, defaults: { anchor: '100%', labelWidth: 115 }, items: [
                { xtype: 'textfield', name: 'url', fieldLabel: '管理地址', value: c.url, emptyText: 'https://10.101.100.101:9443', allowBlank: false, maxLength: 512 },
                { xtype: 'textfield', name: 'token', fieldLabel: 'API Token', inputType: 'password', value: '', maxLength: 4096,
                    emptyText: c.token_configured ? '留空沿用；變更地址後請重新填寫' : '請輸入 API Token', inputAttrTpl: 'autocomplete="new-password"' },
                { xtype: 'checkbox', name: 'tls_verify', itemId: 'safeline-tls-verify', fieldLabel: 'TLS 憑證驗證', checked: c.tls_verify },
                { xtype: 'textfield', name: 'tls_server_name', fieldLabel: '憑證主機名', value: c.tls_server_name, emptyText: '可選，例如 waf.blsy.team', maxLength: 253 },
                { xtype: 'displayfield', value: '使用 IP 連線而憑證簽給網域時，填寫憑證對應的主機名。' },
                { xtype: 'textareafield', name: 'ca_pem', fieldLabel: '自訂 CA（PEM）', value: c.ca_pem, height: 120, maxLength: 65536 },
                { xtype: 'component', itemId: 'safeline-edit-status', html: '', style: 'white-space:normal;overflow-wrap:anywhere' },
            ] }], buttons: [{ text: '測試連線', itemId: 'safeline-test', handler: function () { command('POST', '/connection/test'); } },
                { text: '保存', itemId: 'safeline-save', handler: function () { command('PUT', '/connection'); } }, { text: '取消', handler: function () { editor.close(); } }],
            listeners: { beforeclose: function () { return !pending; }, destroy: function () { if (state.editor === editor) state.editor = null; } } });
        state.editor = editor; editor.show();
    }
    function resourceSummary(resource) {
        const r = resource, values = [['名稱', r.name], ['ID', r.id], ['狀態', r.stale ? '資料已過期' : r.status.label]];
        if (r.kind === 'applications') values.push(['域名', r.domains], ['監聽端口', (r.ports || []).map(portLabel)], ['回源', r.upstreams], ['說明', r.comment],
            ['回源健康檢查', r.health_check], ['回源狀態（上游值）', r.health], ['TLS 憑證 ID', r.cert_id], ['統計', r.stat_enabled], ['更新時間', date(r.updated_at)]);
        else if (r.kind === 'certificates') values.push(['域名', r.domains], ['簽發者', r.issuer], ['到期時間', date(r.valid_before)],
            ['已過期', r.expired], ['已撤銷', r.revoked], ['自簽', r.self_signature], ['受信任', r.trusted], ['來源類型（上游值）', r.type]);
        else if (r.kind === 'ip-groups') values.push(['說明', r.comment], ['成員數', r.total], ['內建', r.builtin], ['更新時間', date(r.updated_at)]);
        else values.push(['動作', ({ 0: '放行', 1: '封禁', 2: '驗證', 3: '認證', 4: '動態防護', 5: '等候室' })[r.action] || r.action],
            ['啟用', r.is_enabled], ['有效期', r.expire === 0 ? '永久' : '上游值：' + r.expire + '（單位待核對）'], ['內建', r.builtin], ['記錄日誌', r.log],
            ['順序', r.order], ['層級（上游值）', r.level], ['請求數', r.req_count], ['放行數', r.pass_count], ['更新時間', date(r.updated_at)]);
        return values;
    }
    function renderResource(state, selected) {
        const tab = state.tab, kind = selected.kind;
        if (kind === 'applications') { applicationPages(state).render(selected); return; }
        if (tab === 'logs') { renderLogs(state, selected); return; }
        if (tab === 'certificates' && kind === 'applications') {
            resourceList(state, 'certificates', state.inventory.resources.certificates.filter(function (r) { return r.id === String(selected.cert_id); })); return;
        }
        if (tab === 'applications' && kind === 'certificates') {
            resourceList(state, 'applications', state.inventory.resources.applications.filter(function (r) { return String(r.cert_id) === selected.id; })); return;
        }
        note(state, '正在讀取資源…');
        request(state, '/resources/' + kind + '/' + encodeURIComponent(selected.id) + '?section=' + (tab === 'proxy' || tab === 'protection' ? tab : 'summary'), {}, function (data) {
            const r = data.resource;
            if (!r || r.id !== selected.id || r.kind !== kind) { note(state, '資源資料不符，請刷新後重試'); return; }
            if (tab === 'proxy') {
                const labels = { br: 'Brotli 壓縮', force_https: '強制 HTTPS', gzip: 'Gzip 壓縮', host: '回源 Host', hsts: 'HSTS', hsts_max_age: 'HSTS 有效秒數',
                    hsts_preload: 'HSTS 預載', hsts_sub: 'HSTS 子域名', http2: 'HTTP/2', http3: 'HTTP/3', 'http_1.0': 'HTTP/1.0', ipv6: 'IPv6', ssl_ciphers: 'TLS 密碼套件',
                    ssl_protocols: 'TLS 協定', sse: 'Server-Sent Events', reset_xff: '重設 X-Forwarded-For' };
                show(state, grid(Object.keys(r.proxy || {}).map(function (k) {
                    const item = r.proxy[k], globalValue = r.proxy_global && r.proxy_global[k];
                    return { name: labels[k] || k, local: readable(item && item.value), source: item && item.global === true ? '全域設定' : item && item.global === false ? '此應用' : '未提供',
                        global: readable(globalValue && typeof globalValue === 'object' && 'value' in globalValue ? globalValue.value : globalValue) };
                }), [{ text: '項目', dataIndex: 'name', flex: 1 }, { text: '應用儲存值', dataIndex: 'local', flex: 1 }, { text: '採用來源', dataIndex: 'source', width: 110 },
                    { text: '全域值', dataIndex: 'global', flex: 1 }], null, '採用全域設定時，以全域值為準。')); return;
            }
            if (tab === 'protection') {
                show(state, properties([['語意防護', r.semantics], ['驗證', r.challenge_is_enabled], ['動態防護', r.chaos_is_enabled], ['頻率限制', r.acl_enabled],
                    ['語意防護設定', r.protection && r.protection.semantics], ['頻率限制設定', r.protection && r.protection.acl]], '防護設定')); return;
            }
            if (tab === 'conditions') {
                const values = []; (r.pattern || []).forEach(function (group, index) { group.forEach(function (item) {
                    values.push({ group: index + 1, field: item.k, operator: item.op, subkey: item.sub_k, value: readable(item.v) });
                }); });
                show(state, grid(values, [{ text: '條件組', dataIndex: 'group', width: 85 }, { text: '匹配項', dataIndex: 'field', flex: 1 },
                    { text: '算子', dataIndex: 'operator', width: 100 }, { text: '子鍵', dataIndex: 'subkey', flex: 1 }, { text: '值', dataIndex: 'value', flex: 2 }], null,
                '保留上游條件組及順序；內建規則可能沒有自訂匹配條件。')); return;
            }
            if (tab === 'scope') {
                show(state, properties([['規則層級（上游值）', r.level], ['順序', r.order], ['條件取反', r.negate], ['匹配條件', r.pattern],
                    ['範圍說明', '生效範圍取決於匹配條件及規則順序。此接口未提供可獨立核對的應用引用清單。']])); return;
            }
            if (tab === 'members') {
                show(state, grid((r.ips || []).map(function (ip, index) { return { id: index, ip: ip }; }), [{ text: 'IP / CIDR', dataIndex: 'ip', flex: 1 }], null,
                    (r.members_truncated ? '僅顯示前 10000 筆；' : '') + '上游成員總數：' + readable(r.total))); return;
            }
            if (tab === 'references') {
                show(state, properties([['上游引用欄位', r.reference || '空'], ['引用資訊', '此欄位不是完整的引用清單；空值不代表未被使用。']])); return;
            }
            show(state, properties(resourceSummary(r), type(kind).name));
        }, function (code) { note(state, message(code)); }, true);
    }
    function uuid() {
        const crypto = window.crypto;
        if (!crypto || typeof crypto.getRandomValues !== 'function') return null;
        if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
        const bytes = crypto.getRandomValues(new Uint8Array(16)); bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
        const hex = Array.from(bytes, function (v) { return v.toString(16).padStart(2, '0'); }).join('');
        return hex.slice(0, 8) + '-' + hex.slice(8, 12) + '-' + hex.slice(12, 16) + '-' + hex.slice(16, 20) + '-' + hex.slice(20);
    }
    function combo(name, label, value, options) {
        return { xtype: 'combo', name: name, fieldLabel: label, value: value, queryMode: 'local', editable: false, displayField: 'name', valueField: 'id',
            store: { fields: ['id', 'name'], data: options.map(function (v) { return { id: v[0], name: v[1] }; }) } };
    }
    function applicationPages(state) {
        if (!state.applicationPages) state.applicationPages = window.__pveSafeLineApplicationPages({ state: state, enc: enc, alive: alive, switchMark: switchMark,
            date: date, amount: amount, portLabel: portLabel, combo: combo, conditionGrid: conditionGrid, grid: grid, message: message, uuid: uuid,
            selected: function () { return findResource(state); }, user: function () { return user(state); }, current: function (generation, who) { return current(state, generation, who); },
            request: function (path, options, done, fail, page) { return request(state, path, options, done, fail, page); },
            show: function (config) { show(state, config); }, note: function (text) { note(state, text); }, status: function (text) { status(state, text); },
            refresh: function () { refresh(state); }, editResource: function (section) { editResource(state, section); },
            renderAdvanced: function (selected) { renderAdvanced(state, selected); }, choose: function (kind, id) { choose(state, kind, id); } });
        return state.applicationPages;
    }
    function advancedHeaders(values, disabled) {
        function choice(options) { return Object.assign(combo('', '', '', options), { allowBlank: false, forceSelection: true }); }
        return { xtype:'grid',itemId:'safeline-advanced-headers',height:240,border:true,disabled:disabled,
            store:{fields:['target','op','key','val','keep'],data:values.map(function(row){return Object.assign({},row);})},
            plugins:[{ptype:'cellediting',clicksToEdit:1}],columns:[
                {text:'目標',dataIndex:'target',width:130,renderer:function(v){return enc(({req:'請求標頭',res:'回應標頭'})[v]||v);},editor:choice([['req','請求標頭'],['res','回應標頭']])},
                {text:'動作',dataIndex:'op',width:90,renderer:function(v){return enc(({set:'設定',del:'刪除'})[v]||v);},editor:choice([['set','設定'],['del','刪除']])},
                {text:'名稱',dataIndex:'key',flex:1,minWidth:140,renderer:enc,editor:Object.assign(combo('', '', '', [['Cache-Control','Cache-Control'],['X-Forwarded-Host','X-Forwarded-Host'],['X-Forwarded-Proto','X-Forwarded-Proto'],['X-Real-IP','X-Real-IP'],['Authorization','Authorization']]),{editable:true,allowBlank:false,maxLength:256})},
                {text:'值',dataIndex:'val',flex:2,minWidth:180,renderer:function(v,m,r){return r.data.op==='del'?'—':r.data.keep!=null&&!v?'已隱藏；留空保留':enc(v);},editor:{xtype:'textfield',maxLength:4096,inputAttrTpl:'autocomplete="off" spellcheck="false"'}}],
            tbar:[{text:'新增 Header 操作',iconCls:'fa fa-plus',disabled:disabled,handler:function(button){button.up('grid').getStore().add({target:'req',op:'set',key:'',val:''});}},
                {text:'移除所選項',iconCls:'fa fa-minus',disabled:disabled,handler:function(button){const table=button.up('grid');table.getStore().remove(table.getSelectionModel().getSelection());}}],
            viewConfig:{emptyText:disabled?'此版本無法編輯 Header 操作':'尚未設定 Header 操作',deferEmptyText:false}};
    }
    function renderAdvanced(state, resource) {
        const identity = resource ? resource.id : 'global', existing = state.page.down('#safeline-advanced');
        if (alive(existing) && existing.applicationId === identity) return;
        const generation = state.generation, who = user(state), paths = resource ? { proxy: '/applications/' + encodeURIComponent(resource.id) + '/advanced', nginx: '/applications/' + encodeURIComponent(resource.id) + '/nginx' } : { proxy: '/configuration/proxy', groups: '/configuration/groups' };
        const loaded = {}, loading = {}, messages = {};
        const sources = [['socket_ip', '直接取連線來源 IP'], ['xff_dynamic', '從 X-Forwarded-For 中取得'], ['http_header', '從指定 HTTP 標頭取得'],
            ['proxy_protocol', '從 PROXY protocol 取得'], ['xff_first', 'XFF 最右側第 1 個 IP'], ['xff_second', 'XFF 最右側第 2 個 IP'], ['xff_third', 'XFF 最右側第 3 個 IP']];
        const rows = [
            ['source', '源 IP 取得方式', ['ip_source', 'ip_value']], ['ipv6', '監聽 IPv6 地址'], ['http_1.0', 'HTTP/1.0'], ['http2', 'HTTP/2'],
            ['force_https', 'HTTP 自動跳轉到 HTTPS'], ['hsts', 'HSTS', ['hsts', 'hsts_max_age', 'hsts_sub', 'hsts_preload']], ['host', '代理請求 Host'],
            ['forwarded', '傳送 X-Forwarded-Host／Proto', ['xfh', 'xfp']], ['reset_xff', '清空並重寫 X-Forwarded-For'], ['gzip', 'Gzip 壓縮'],
            ['br', 'Brotli 壓縮'], ['sse', 'SSE 串流回應'], ['ntlm', 'NTLM 認證'], ['default_server', '應用不存在時返回內建憑證'],
            ['ssl_protocols', 'SSL／TLS 協定'], ['ssl_ciphers', 'SSL Ciphers'], ['http_headers', 'HTTP Header 操作'], ['groups', '分組管理', ['enable']],
        ].map(function (r) { return { id: r[0], name: r[1], keys: r[2] || [r[0]], section: r[0] === 'groups' ? 'groups' : 'proxy' }; });
        if (resource) { rows.pop(); rows.push({ id: 'nginx', name: '自訂 NGINX 配置', keys: ['custom_location'], section: 'nginx' }); }
        let panel, editor;
        function valid(data) { return data && data.values && Array.isArray(data.locked) && data.capabilities && typeof data.fingerprint === 'string'; }
        function available(row, data) { return !!data && row.keys.every(function (key) { return key in data.values; }); }
        function editable(row, data) { return available(row, data) && (resource && row.section === 'proxy' || !row.keys.some(function (key) { return data.locked.includes(key); })); }
        function stored(data, key) { const item = data.values[key]; return resource && item.global && key !== 'http_headers' && data.global_values && data.global_values[key] ? data.global_values[key].value : item.value; }
        function selected() { const record = panel.getSelectionModel().getSelection()[0]; return record && rows.find(function (r) { return r.id === record.get('id'); }); }
        function updateEdit() {
            if (!alive(panel)) return;
            const row = selected();
            panel.down('#safeline-advanced-edit').setDisabled(alive(editor) || !row || !editable(row, loaded[row.section]));
            panel.down('#safeline-advanced-reload').setDisabled(!!alive(editor));
        }
        function summary(row, data) {
            if (!available(row, data)) return data ? '未提供' : messages[row.section] || '正在讀取…';
            function v(key) { return stored(data, key); }
            if (row.section === 'nginx') return (data.values.custom_location || []).join('\n') || '未設定';
            if (row.section === 'groups') return data.values.enable ? '啟用' : '停用';
            if (row.id === 'source') {
                const source = sources.find(function (r) { return r[0] === v('ip_source'); });
                let detail = v('ip_value');
                if (v('ip_source') === 'xff_dynamic') {
                    try { const xff = JSON.parse(detail); detail = (xff.position === 'from_start' ? '從左往右' : '從右往左') + '，第 ' + xff.index + ' 個 IP'; } catch (e) { /* Display the stored value. */ }
                }
                return (source ? source[1] : v('ip_source')) + (detail ? ' · ' + detail : '');
            }
            if (row.id === 'hsts') return v('hsts') ? (resource ? '✓ · ' : '啟用 · ') + v('hsts_max_age') + ' 秒' + (v('hsts_sub') ? ' · 包含子域名' : '') + (v('hsts_preload') ? ' · 預載' : '') : resource ? false : '停用';
            if (row.id === 'forwarded') return !v('xfh') && !v('xfp') ? resource ? false : '停用' : v('xfh') === '$http_host' && v('xfp') === '$scheme' ? resource ? true : '啟用' : '自訂 · Host: ' + v('xfh') + '，Proto: ' + v('xfp');
            if (row.id === 'host') return v('host') || '停用';
            if (row.id === 'http_headers') return v('http_headers').length + ' 項操作';
            if (row.id === 'ssl_ciphers') return v('ssl_ciphers') || 'SafeLine 預設';
            return typeof v(row.id) === 'boolean' ? resource ? v(row.id) : v(row.id) ? '啟用' : '停用' : v(row.id);
        }
        function rebuild() {
            if (!alive(panel)) return;
            const selection = selected();
            panel.getStore().loadData(rows.map(function (row) {
                const data = loaded[row.section], locked = available(row, data) && !editable(row, data);
                const reason = locked ? !data.capabilities.premium && (resource || row.section === 'groups' || ['ssl_protocols', 'ssl_ciphers', 'http_headers'].includes(row.id)) ? '需專業版或商業版' : '無法編輯' : '';
                const flags = available(row, data) && row.section === 'proxy' ? row.keys.map(function (key) { return data.values[key].global; }) : [];
                return { id: row.id, name: row.name, value: reason ? summary(row, data) + '（' + reason + '）' : summary(row, data), source: !flags.length ? '—' : flags.every(Boolean) ? '全域' : flags.some(Boolean) ? '混合' : '自訂' };
            }));
            if (selection) panel.getSelectionModel().select(panel.getStore().getById(selection.id));
            const issues = Object.keys(messages).filter(function (section) { return messages[section]; }).map(function (section) { return (section === 'proxy' ? '代理配置：' : section === 'nginx' ? 'NGINX：' : '分組管理：') + messages[section]; });
            panel.down('#safeline-advanced-status').setText(enc(issues.length ? issues.join('；') : '已讀取 · ' + date((loaded.proxy || loaded.groups).updated_at)));
            updateEdit();
        }
        function load(section) {
            if (loading[section] || !alive(panel)) return;
            loading[section] = true; loaded[section] = null; messages[section] = '正在讀取…'; rebuild();
            request(state, paths[section], {}, function (data) {
                if (!alive(panel)) return;
                loading[section] = false; loaded[section] = valid(data) ? data : null;
                messages[section] = loaded[section] ? '' : message('safeline_unexpected_response'); rebuild();
            }, function (code) { if (alive(panel)) { loading[section] = false; messages[section] = message(code); rebuild(); } }, false);
        }
        function edit(row) {
            if (!row || !editable(row, loaded[row.section]) || alive(state.editor) || !state.connection.csrf_token || !current(state, generation, who)) return;
            if (resource && row.section === 'nginx') { applicationPages(state).edit('nginx'); return; }
            let dialog, data, pending = false, operation = uuid(), initialSource;
            function field(key) { return dialog.down('#safeline-advanced-' + key.replace(/[._]/g, '-')); }
            function value(key) { return field(key).getValue(); }
            function input(key, config) { return Object.assign({ itemId: 'safeline-advanced-' + key.replace(/[._]/g, '-'), name: key }, config); }
            function old(key) { return stored(data, key); }
            function hint(text) { return { xtype: 'displayfield', value: enc(text), fieldStyle: 'opacity:.7;font-size:12px' }; }
            function notice(text) { if (alive(dialog)) dialog.down('#safeline-advanced-edit-status').update(enc(text)); }
            function check(key, label, checked) { return input(key, { xtype: 'checkbox', fieldLabel: label, checked: checked == null ? !!old(key) : checked, listeners: { change: sync } }); }
            function sync() {
                const customAllowed = data && data.capabilities.premium && !row.keys.some(function (key) { return data.locked.includes(key); });
                const inherited = resource && (value('inherit-mode') !== 'custom' || !customAllowed);
                if (resource) {
                    row._fields.forEach(function (config) { const f = config.itemId ? dialog.down('#' + config.itemId) : null; if (f) f.setDisabled(!!inherited); });
                    dialog.down('#safeline-advanced-save').setDisabled(!operation || value('inherit-mode') === 'custom' && !customAllowed);
                }
                function visible(key, show) { const f = field(key); if (f) { f.setVisible(show); f.setDisabled(!show || !!inherited); } }
                if (row.id === 'source') {
                    visible('header', value('source') === 'http_header'); visible('trusted-proxies', value('source') === 'proxy_protocol');
                    visible('xff-position', value('source') === 'xff_dynamic'); visible('xff-index', value('source') === 'xff_dynamic');
                }
                if (row.id === 'host') visible('host-value', !!value('host-enable'));
                if (row.id === 'hsts') ['hsts_max_age', 'hsts_sub', 'hsts_preload'].forEach(function (key) { visible(key, !!value('hsts')); });
            }
            function populate() {
                let fields;
                if (row.section === 'groups') fields = [input('enable', { xtype: 'checkbox', fieldLabel: '啟用分組管理', checked: data.values.enable })];
                else if (row.id === 'source') {
                    let xff = { position: 'from_end', index: 1 }; try { if (old('ip_source') === 'xff_dynamic') xff = Object.assign(xff, JSON.parse(old('ip_value'))); } catch (e) { /* Preserve until edited. */ }
                    initialSource = { source: old('ip_source'), value: old('ip_value'), position: xff.position, index: xff.index };
                    const choices = sources.slice(); if (!choices.some(function (r) { return r[0] === old('ip_source'); })) choices.push([old('ip_source'), '目前值：' + old('ip_source')]);
                    fields = [input('source', Object.assign(combo('source', '源 IP 取得方式', old('ip_source'), choices), { allowBlank: false, forceSelection: true, listeners: { select: sync }, listConfig: { itemTpl: '{name:htmlEncode}' } })),
                        input('xff-position', combo('xff-position', 'XFF 取得方向', xff.position, [['from_start', '從左往右'], ['from_end', '從右往左']])),
                        input('xff-index', { xtype: 'numberfield', fieldLabel: 'IP 位置序號', value: xff.index, minValue: 1, maxValue: 100, allowDecimals: false, allowBlank: false }),
                        input('header', Object.assign(combo('header', 'HTTP 標頭', old('ip_source') === 'http_header' ? old('ip_value') : 'X-Real-IP', [['X-Real-IP', 'X-Real-IP'], ['CF-Connecting-IP', 'CF-Connecting-IP'], ['True-Client-IP', 'True-Client-IP']]), { editable: true, allowBlank: false, maxLength: 256 })),
                        input('trusted-proxies', { xtype: 'textareafield', fieldLabel: '信任的代理 IP／CIDR（每行一項）', value: old('ip_source') === 'proxy_protocol' ? old('ip_value').split(',').join('\n') : '', height: 90, maxLength: 4096 }),
                        hint('XFF 位置從 1 開始；PROXY protocol 需與前置代理的設定一致。')];
                } else if (row.id === 'hsts') fields = [check('hsts', '啟用 HSTS'),
                    input('hsts_max_age', Object.assign(combo('hsts_max_age', '有效秒數', old('hsts_max_age'), [['15768000', '半年（15768000）'], ['31536000', '一年（31536000）'], ['63072000', '兩年（63072000）'], ['0', '0']]), { editable: true, allowBlank: false })), check('hsts_sub', '包含子域名'), check('hsts_preload', '預載')];
                else if (row.id === 'host') fields = [check('host-enable', '修改請求中的 Host', !!old('host')), input('host-value', Object.assign(combo('host-value', 'Host', old('host') || '$http_host', [['$http_host', '原請求 Host（$http_host）'], ['$host', '解析後的主機名（$host）'], ['$proxy_host', '上游主機名（$proxy_host）']]), { editable: true, allowBlank: false, maxLength: 1024 }))];
                else if (row.id === 'forwarded') fields = [check('forwarded', '啟用', !!(old('xfh') || old('xfp')))];
                else if (row.id === 'ssl_protocols') fields = [input('ssl_protocols', Object.assign(combo('ssl_protocols', 'SSL／TLS 協定', old('ssl_protocols').split(/\s+/).filter(Boolean), [['TLSv1.2', 'TLSv1.2'], ['TLSv1.3', 'TLSv1.3'], ['TLSv1', 'TLSv1.0（舊版）'], ['TLSv1.1', 'TLSv1.1（舊版）'], ['SSLv2', 'SSLv2（舊版）'], ['SSLv3', 'SSLv3（舊版）']]), { xtype: 'tagfield', multiSelect: true, forceSelection: true, filterPickList: true, allowBlank: false }))];
                else if (row.id === 'ssl_ciphers') fields = [input('ssl_ciphers', Object.assign(combo('ssl_ciphers', 'SSL Ciphers', old('ssl_ciphers'), [['', 'SafeLine 預設'], ['HIGH:!aNULL:!MD5', 'HIGH:!aNULL:!MD5']]), { editable: true, maxLength: 4096 })), hint('留空使用 SafeLine 預設密碼套件。')];
                else if (row.id === 'http_headers') fields = [advancedHeaders(old('http_headers'), false), hint('敏感標頭的現有值不回填：留空保留，填入新值替換。刪除動作無需填寫值。')];
                else fields = [check(row.id, '啟用')].concat(row.id === 'force_https' ? [hint('需要應用同時監聽 HTTP 與 HTTPS 端口。')] : []);
                row._fields = fields;
                if (resource) {
                    const flags = row.keys.map(function (key) { return data.values[key].global; }), initial = flags.every(Boolean) ? 'inherit' : flags.some(Boolean) ? 'mixed' : 'custom';
                    fields = [input('inherit-mode', Object.assign(combo('inherit-mode', '採用來源', initial, [['inherit', '跟隨全域配置'], ['custom', '使用自訂配置']].concat(initial === 'mixed' ? [['mixed', '保留各項來源']] : [])), { listeners: { select: sync } }))].concat(fields, data.capabilities.premium ? [] : [hint('此版本可恢復跟隨全域；自訂應用配置需專業版或商業版。')]);
                }
                const container = dialog.down('#safeline-advanced-edit-fields'); container.removeAll(true); fields.forEach(function (config) { container.add(config); }); sync();
            }
            function changes() {
                const patch = {};
                if (resource && value('inherit-mode') !== 'custom') {
                    if (value('inherit-mode') === 'inherit') row.keys.forEach(function (key) { if (!data.values[key].global) patch[key] = { global: true, value: data.values[key].value }; });
                    return patch;
                }
                function put(key, next, global) { if (row.keys.includes(key) && (JSON.stringify(data.values[key].value) !== JSON.stringify(next) || resource && data.values[key].global)) patch[key] = { global: resource ? false : global == null ? data.values[key].global : global, value: next }; }
                if (row.section === 'groups') return value('enable') === data.values.enable ? {} : { enable: !!value('enable') };
                if (row.id === 'source') {
                    const source = value('source'); let next = '';
                    if (source === 'http_header') next = String(value('header') || '').trim();
                    if (source === 'proxy_protocol') next = String(value('trusted-proxies') || '').split(/[\r\n,]+/).map(function (v) { return v.trim(); }).filter(Boolean).join(',');
                    if (source === 'xff_dynamic') next = JSON.stringify({ position: value('xff-position'), index: value('xff-index') });
                    const changed = source !== initialSource.source || source === 'http_header' && next !== initialSource.value || source === 'proxy_protocol' && next !== initialSource.value || source === 'xff_dynamic' && (value('xff-position') !== initialSource.position || Number(value('xff-index')) !== Number(initialSource.index));
                    if (changed || resource && row.keys.some(function (key) { return data.values[key].global; })) { patch.ip_source = { global: false, value: source }; patch.ip_value = { global: false, value: next }; }
                } else if (row.id === 'hsts') {
                    ['hsts', 'hsts_sub', 'hsts_preload'].forEach(function (key) { put(key, !!value(key), true); }); put('hsts_max_age', String(value('hsts_max_age')).trim());
                } else if (row.id === 'host') put('host', value('host-enable') ? String(value('host-value') || '').trim() : '', true);
                else if (row.id === 'forwarded') { if (!!value('forwarded') !== !!(old('xfp') || old('xfh')) || resource && row.keys.some(function (key) { return data.values[key].global; })) { put('xfp', value('forwarded') ? '$scheme' : '', true); put('xfh', value('forwarded') ? '$http_host' : '', true); } }
                else if (row.id === 'ssl_protocols') { const next = (value('ssl_protocols') || []).slice().sort().join(' '); if (old('ssl_protocols').split(/\s+/).filter(Boolean).sort().join(' ') !== next || resource && data.values.ssl_protocols.global) put('ssl_protocols', next); }
                else if (row.id === 'ssl_ciphers') put('ssl_ciphers', String(value('ssl_ciphers') || '').trim());
                else if (row.id === 'http_headers') {
                    const table = dialog.down('#safeline-advanced-headers'), headers = [];
                    table.getPlugins().forEach(function (plugin) { if (typeof plugin.completeEdit === 'function') plugin.completeEdit(); });
                    table.getStore().each(function (record) { const r = record.data, item = { target: r.target, op: r.op, key: r.key, val: r.op === 'del' ? '' : r.val }; if (r.keep != null && !r.val && r.op !== 'del') item.keep = r.keep; headers.push(item); }); put('http_headers', headers);
                } else put(row.id, !!value(row.id), true);
                return patch;
            }
            function saving(busy) { pending = busy; dialog.down('#safeline-advanced-close').setDisabled(busy); const form = dialog.down('form'); if (typeof form.setLoading === 'function') form.setLoading(busy ? '正在儲存…' : false); }
            function save() {
                if (pending || !data || !operation || !alive(dialog) || !current(state, generation, who) || dialog.down('#safeline-advanced-save').disabled) return;
                if (!dialog.down('form').getForm().isValid()) { notice('請檢查標記的欄位。'); return; }
                let patch; try { patch = changes(); } catch (e) { notice('請檢查尚未完成的 Header 編輯。'); return; }
                if (!Object.keys(patch).length) { notice('設定未變更'); return; }
                saving(true); dialog.down('#safeline-advanced-save').setDisabled(true); notice('正在儲存並核對設定…');
                request(state, paths[row.section], { method: 'PUT', headers: { 'X-PVE-Plugins-CSRF': state.connection.csrf_token }, jsonData: { expected_revision: data.connection_revision, fingerprint: data.fingerprint, operation_id: operation, changes: patch } }, function (result) {
                    if (!alive(dialog)) return; saving(false);
                    if (result.outcome === 'confirmed') { dialog.destroy(); status(state, '高級配置已儲存並確認'); load(row.section); return; }
                    notice(message(result.outcome === 'failed' ? result.code : 'safeline_write_unknown'));
                    if (result.outcome === 'failed') { operation = uuid(); dialog.down('#safeline-advanced-save').setDisabled(!operation); }
                }, function (code) {
                    if (!alive(dialog)) return; saving(false);
                    notice(message(code.startsWith('invalid_') || ['safeline_resource_changed', 'safeline_feature_unavailable', 'safeline_edit_unsupported', 'safeline_operation_conflict', 'safeline_operation_pending'].includes(code) ? code : 'safeline_write_unknown'));
                    if (code.startsWith('invalid_')) dialog.down('#safeline-advanced-save').setDisabled(false);
                }, false);
            }
            dialog = Ext.create('Ext.window.Window', { title: '編輯：' + row.name, width: row.id === 'http_headers' ? 820 : 620, maxHeight: Math.min(650, (window.innerHeight || 674) - 24), modal: true, constrain: true, closeAction: 'destroy', layout: 'fit',
                items: [{ xtype: 'form', border: false, bodyPadding: 16, scrollable: true, items: [{ xtype: 'container', itemId: 'safeline-advanced-edit-fields', layout: 'anchor', defaults: { anchor: '100%', labelWidth: 160 } }, { xtype: 'component', itemId: 'safeline-advanced-edit-status', margin: '8 0 0', html: '正在讀取最新設定…' }] }],
                buttons: [{ text: '儲存', itemId: 'safeline-advanced-save', disabled: true, handler: save }, { text: '關閉', itemId: 'safeline-advanced-close', handler: function () { dialog.close(); } }],
                listeners: { beforeclose: function () { return !pending; }, destroy: function () { if (state.editor === dialog) state.editor = null; if (editor === dialog) editor = null; updateEdit(); } } });
            editor = dialog; state.editor = dialog; updateEdit(); dialog.show();
            request(state, paths[row.section], {}, function (result) {
                if (!alive(dialog) || !alive(panel)) return;
                if (!valid(result)) { notice(message('safeline_unexpected_response')); return; }
                loaded[row.section] = result; messages[row.section] = ''; rebuild();
                if (!editable(row, result)) { notice(message(!result.capabilities.premium ? 'safeline_feature_unavailable' : 'safeline_edit_unsupported')); return; }
                data = result; populate(); notice(operation ? resource ? '此應用的自訂值優先；可逐項恢復跟隨全域。' : '全域應用預設；個別應用的自訂值優先。' : '此瀏覽器無法產生操作編號，暫時無法儲存。'); if (!resource) dialog.down('#safeline-advanced-save').setDisabled(!operation);
            }, function (code) { notice(message(code)); }, false);
        }
        const config = { xtype: 'grid', itemId: 'safeline-advanced', border: false, store: { fields: ['id', 'name', 'value', 'source'], data: [] },
            columns: [{ text: '項目', dataIndex: 'name', width: 290, renderer: enc }, { text: '值', dataIndex: 'value', flex: 1, renderer: function (v, m, r) { return typeof v === 'boolean' ? switchMark(v, m, r) : enc(v); } }].concat(resource ? [{ text: '採用來源', dataIndex: 'source', width: 110, renderer: enc }] : []),
            tbar: [{ text: '編輯', iconCls: 'fa fa-pencil', itemId: 'safeline-advanced-edit', disabled: true, handler: function () { edit(selected()); } }, { text: '重新載入', iconCls: 'fa fa-refresh', itemId: 'safeline-advanced-reload', handler: function () { panel.reloadAdvanced(); } }, { xtype: 'tbtext', itemId: 'safeline-advanced-status', text: '正在讀取…' }],
            bbar: [{ xtype: 'tbtext', text: resource ? '雙擊設定行可編輯此應用的配置及採用來源。' : '雙擊設定行可編輯。全域應用預設；個別應用的自訂值優先。' }],
            viewConfig: { stripeRows: true }, listeners: { selectionchange: updateEdit, itemdblclick: function (view, record) { edit(rows.find(function (r) { return r.id === record.get('id'); })); }, beforedestroy: function () { if (alive(editor)) editor.destroy(); } } };
        if (resource) applicationPages(state).frame(config, resource); else show(state, config);
        panel = state.page.down('#safeline-advanced'); panel.applicationId = identity; panel.reloadAdvanced = function () { if (!alive(editor) && !alive(state.editor)) Object.keys(paths).forEach(load); }; panel.reloadAdvanced();
    }
    function conditionGrid(pattern) {
        const values = [], fields = [['src_ip', '來源 IP'], ['website', '防護應用 ID'], ['host', 'Host'], ['uri', 'URI'], ['uri_no_query', '不含參數的 URI'],
            ['method', 'HTTP 方法'], ['req_header', '請求標頭'], ['ja4', 'JA4']];
        const operators = [['eq', '等於'], ['not_eq', '不等於'], ['in', '屬於'], ['not_in', '不屬於'], ['has', '包含'], ['not_has', '不包含'],
            ['prefix', '前綴'], ['re', '正則表達式'], ['exist', '存在'], ['not_exist', '不存在'], ['ja4_in', 'JA4 屬於']];
        pattern.forEach(function (group, index) { group.forEach(function (r) { values.push({ group: index + 1, k: r.k, op: r.op, sub_k: r.sub_k || '', values: (r.v || []).join('\n'), original: r }); }); });
        function editor(options) { return { xtype: 'combo', queryMode: 'local', editable: true, forceSelection: false, displayField: 'name', valueField: 'id',
            store: { fields: ['id', 'name'], data: options.map(function (v) { return { id: v[0], name: v[1] }; }) } }; }
        function label(options) { return function (v) { const item = options.find(function (a) { return a[0] === v; }); return enc(item ? item[1] : v); }; }
        return { xtype: 'grid', itemId: 'safeline-patterns', title: '匹配條件（點擊儲存格編輯）', height: 265, border: true,
            store: { fields: ['group', 'k', 'op', 'sub_k', 'values', 'original'], data: values }, plugins: [{ ptype: 'cellediting', clicksToEdit: 1 }],
            columns: [{ text: '條件組', dataIndex: 'group', width: 80, editor: { xtype: 'numberfield', allowDecimals: false, minValue: 1, maxValue: 64 } },
                { text: '匹配項', dataIndex: 'k', width: 150, editor: editor(fields), renderer: label(fields) },
                { text: '運算子', dataIndex: 'op', width: 125, editor: editor(operators), renderer: label(operators) },
                { text: '子鍵', dataIndex: 'sub_k', width: 145, editor: { xtype: 'textfield' }, renderer: enc },
                { text: '匹配值（每行一項）', dataIndex: 'values', flex: 1, minWidth: 150, editor: { xtype: 'textareafield' }, renderer: function (v) { return '<div style="white-space:pre-wrap">' + enc(v) + '</div>'; } }],
            tbar: [{ text: '新增條件', handler: function (button) { button.up('grid').getStore().add({ group: 1, k: 'src_ip', op: 'eq', sub_k: '', values: '' }); } },
                { text: '移除所選條件', handler: function (button) { const table = button.up('grid'); table.getStore().remove(table.getSelectionModel().getSelection()); } }],
            bbar: [{ xtype: 'tbtext', text: '同組條件全部符合；不同條件組任一組符合。應用範圍由 website 條件指定。' }] };
    }
    function editResource(state, section) {
        if (!state.connection || !state.connection.csrf_token || !state.selection.id || !user(state) || alive(state.editor)) return;
        const selected = Object.assign({}, state.selection), resource = findResource(state), generation = state.generation, who = user(state);
        const path = '/resources/' + selected.kind + '/' + encodeURIComponent(selected.id) + '/edit?section=' + section;
        let dialog, loaded, pending = false, operation = uuid();
        function notice(value) { if (alive(dialog)) dialog.down('#safeline-resource-edit-status').update(enc(value)); }
        function lines(value) { return String(value || '').split(/\r?\n/).map(function (v) { return v.trim(); }).filter(Boolean); }
        function readValues() {
            const result = {}, form = dialog.down('form'), original = loaded.values;
            Object.keys(original).forEach(function (key) {
                if (key === 'pattern') {
                    const table = dialog.down('#safeline-patterns');
                    if (table) {
                        table.getPlugins().forEach(function (plugin) { if (typeof plugin.completeEdit === 'function') plugin.completeEdit(); });
                        const groups = new Map(); table.getStore().each(function (record) {
                            const row = record.data, index = Number(row.group);
                            if (!Number.isInteger(index) || index < 1 || index > 64) throw new Error('條件組需為 1–64 的整數');
                            if (!groups.has(index)) groups.set(index, []);
                            const unchanged = row.original && row.k === row.original.k && row.op === row.original.op && row.sub_k === (row.original.sub_k || '') && row.values === (row.original.v || []).join('\n');
                            groups.get(index).push(unchanged ? row.original : { k: String(row.k || ''), op: String(row.op || ''), sub_k: String(row.sub_k || ''), v: row.values === '' ? [] : String(row.values || '').split(/\r?\n/) });
                        });
                        result.pattern = Array.from(groups.keys()).sort(function (a, b) { return a-b; }).map(function (k) { return groups.get(k); });
                    }
                } else if (section === 'proxy') {
                    const mode = form.down('#safeline-input-' + key).getValue(), previous = original[key];
                    result[key] = { global: mode === 'inherit', value: typeof previous.value === 'boolean' ? mode === 'inherit' ? previous.value : mode === 'on' : String(form.down('#safeline-value-' + key).getValue() || '') };
                } else {
                    const field = form.down('#safeline-input-' + key), value = field.getValue();
                    result[key] = typeof original[key] === 'boolean' ? !!value : Array.isArray(original[key]) ? lines(value) : typeof original[key] === 'number' ? Number(value) : String(value || '');
                }
            });
            const changed = {};
            Object.keys(result).forEach(function (key) { if (JSON.stringify(result[key]) !== JSON.stringify(original[key])) changed[key] = result[key]; });
            if (selected.kind === 'certificates' && loaded.certificate_type === 2) {
                if (!String(result.crt || '').trim()) throw new Error('請填寫新的 PEM 憑證；私鑰可留空沿用現有設定');
                changed.crt = result.crt; changed.key = result.key;
            }
            return changed;
        }
        dialog = Ext.create('Ext.window.Window', { title: '編輯 ' + type(selected.kind).name + ' · #' + selected.id, width: selected.kind === 'deny' || selected.kind === 'allow' ? 940 : 680,
            maxHeight: Math.min(820, (window.innerHeight || 844) - 24), modal: true, constrain: true, closeAction: 'destroy', layout: 'fit',
            items: [{ xtype: 'form', border: false, bodyPadding: 16, scrollable: true, defaults: { anchor: '100%', labelWidth: 130 }, items: [
                { xtype: 'displayfield', value: enc(resource && resource.name || '#' + selected.id) },
                { xtype: 'component', itemId: 'safeline-resource-edit-status', html: '正在讀取最新設定…' }] }],
            buttons: [{ text: '儲存', itemId: 'safeline-resource-save', disabled: true, handler: function () {
                if (pending || !loaded || !operation || !alive(dialog) || !current(state, generation, who)) return;
                if (!dialog.down('form').getForm().isValid()) return;
                let changes; try { changes = readValues(); } catch (e) { notice(e.message); return; }
                if (!Object.keys(changes).length) { notice('尚未修改設定'); return; }
                pending = true; dialog.down('#safeline-resource-save').setDisabled(true); notice('正在儲存並核對結果…');
                request(state, path, { method: 'PUT', headers: { 'X-PVE-Plugins-CSRF': state.connection.csrf_token },
                    jsonData: { expected_revision: loaded.connection_revision, fingerprint: loaded.fingerprint, operation_id: operation, changes: changes } }, function (data) {
                    pending = false; if (!alive(dialog)) return;
                    if (data.outcome === 'confirmed') { dialog.destroy(); state.editor = null; status(state, '已儲存並確認設定'); loadInventory(state); return; }
                    notice(data.outcome === 'failed' ? message(data.code) : message('safeline_write_unknown'));
                    if (data.outcome === 'failed') { operation = uuid(); dialog.down('#safeline-resource-save').setDisabled(!operation); }
                }, function (code) {
                    pending = false; if (!alive(dialog)) return;
                    if (code === 'safeline_connection_changed') { dialog.destroy(); cancel(state, true); state.inventory = null; loadConnection(state); return; }
                    notice(['invalid_safeline_request', 'invalid_safeline_domains', 'invalid_safeline_ports', 'invalid_safeline_upstreams', 'invalid_safeline_ips', 'invalid_safeline_certificate',
                        'safeline_resource_changed', 'safeline_operation_conflict', 'safeline_edit_unsupported', 'safeline_operation_pending'].includes(code) ? message(code) : message('safeline_write_unknown'));
                    if (code.startsWith('invalid_safeline_')) dialog.down('#safeline-resource-save').setDisabled(false);
                }, false);
            } }, { text: '關閉', handler: function () { dialog.close(); } }],
            listeners: { destroy: function () { if (state.editor === dialog) state.editor = null; } } });
        state.editor = dialog; dialog.show();
        request(state, path, {}, function (data) {
            if (!alive(dialog)) return;
            loaded = data;
            const form = dialog.down('form'), v = data.values, fields = [];
            const labels = { comment: '說明', server_names: '域名（每行一項）', ports: '監聽端口', upstreams: '回源地址（每行一項）', cert_id: 'TLS 憑證', health_check: '回源健康檢查',
                stat_enabled: '流量統計', name: '規則名稱', log: '記錄日誌', is_enabled: '啟用規則', ips: 'IP / CIDR / 範圍', crt: '新的 PEM 憑證', key: '新的 PEM 私鑰', domains: 'ACME 域名', email: 'ACME 電子郵件' };
            const proxyLabels = { br: 'Brotli', force_https: '強制 HTTPS', gzip: 'Gzip', host: '回源 Host', hsts: 'HSTS', hsts_max_age: 'HSTS 有效秒數', hsts_preload: 'HSTS 預載',
                hsts_sub: 'HSTS 子域名', http2: 'HTTP/2', http3: 'HTTP/3', 'http_1.0': 'HTTP/1.0', ipv6: 'IPv6', ssl_ciphers: 'TLS 密碼套件', ssl_protocols: 'TLS 協定', sse: 'SSE', reset_xff: '重設 X-Forwarded-For' };
            Object.keys(v).forEach(function (key) {
                let field;
                if (key === 'pattern') { fields.push(conditionGrid(v.pattern)); return; }
                if (section === 'proxy') {
                    const item = v[key], bool = typeof item.value === 'boolean';
                    field = combo(key, proxyLabels[key] || key, item.global ? 'inherit' : bool ? item.value ? 'on' : 'off' : 'local', bool ? [['inherit', '沿用全域'], ['on', '啟用'], ['off', '停用']] : [['inherit', '沿用全域'], ['local', '此應用設定']]);
                    if (!bool) fields.push({ xtype: 'textfield', itemId: 'safeline-value-' + key, fieldLabel: (proxyLabels[key] || key) + ' · 值', value: item.value, maxLength: 4096 });
                } else if (key === 'mode') field = combo(key, '運作模式', v.mode, [[0, '防護模式'], [2, '觀察模式'], [1, '離線模式']]);
                else if (key === 'cert_id') field = combo(key, labels[key], v[key], [[0, '不指定憑證']].concat((state.inventory.resources.certificates || []).map(function (r) { return [Number(r.id), r.name + ' · #' + r.id]; })));
                else if (typeof v[key] === 'boolean') field = { xtype: 'checkbox', name: key, fieldLabel: labels[key] || key, checked: v[key] };
                else field = { xtype: Array.isArray(v[key]) || key === 'crt' || key === 'key' ? 'textareafield' : 'textfield', name: key, fieldLabel: labels[key] || key,
                    value: Array.isArray(v[key]) ? v[key].join('\n') : v[key], height: key === 'crt' || key === 'key' ? 140 : Array.isArray(v[key]) ? 90 : undefined,
                    maxLength: key === 'crt' ? 65536 : key === 'key' ? 16384 : key === 'ips' ? 95000 : 65536, inputAttrTpl: key === 'key' ? 'autocomplete="off" spellcheck="false"' : undefined };
                field.itemId = 'safeline-input-' + key; fields.push(field);
            });
            if (selected.kind === 'applications' && section === 'settings') fields.push({ xtype: 'displayfield', value: 'TLS 端口格式為 443_ssl，普通端口格式為 80。回源需包含 http:// 或 https://。' });
            if (section === 'mode') fields.push({ xtype: 'displayfield', value: '觀察模式用於觀察攻擊；離線模式會拒絕此應用的訪問。其他規則仍依 SafeLine 設定生效。' });
            if (selected.kind === 'certificates') fields.push({ xtype: 'displayfield', value: data.certificate_type === 2 ? '更新會作用於引用此憑證的應用。私鑰留空沿用現有私鑰；現有私鑰不會回填。' : '更新 ACME 域名與電子郵件，憑證簽發結果以 SafeLine 為準。' });
            if (data.locked.length) fields.push({ xtype: 'displayfield', value: '部分欄位含敏感內容或不支援的條件，維持原設定；仍可編輯其餘欄位。' });
            fields.forEach(function (field) { form.add(field); });
            notice(operation ? '儲存前會再次檢查資源是否已變更。' : '此瀏覽器無法產生操作編號，暫時無法儲存。');
            dialog.down('#safeline-resource-save').setDisabled(!operation || !Object.keys(v).length);
        }, function (code) { notice(message(code)); }, false);
    }
    function renderLogs(state, context) {
        if (!state.attackPages) state.attackPages = window.__pveSafeLineAttackPages({ state: state, enc: enc, alive: alive,
            date: date, amount: amount, combo: combo, grid: grid, properties: properties, message: message, uuid: uuid,
            user: function () { return user(state); }, current: function (generation, who) { return current(state, generation, who); },
            request: function (path, options, done, fail, page) { return request(state, path, options, done, fail, page); },
            show: function (config) { show(state, config); }, note: function (value) { note(state, value); },
            loadInventory: function () { loadInventory(state); } });
        state.attackPages.render(context);
    }
    function navItems(selection) {
        if (selection.kind === 'root') return [['overview', '總覽', 'fa-dashboard'], ['events', '攻擊防護', 'fa-shield'], ['policies', '防護規則', 'fa-filter'], ['challenge', '人機驗證', 'fa-user-circle-o'], ['connection', '連線資訊', 'fa-plug']];
        if (!selection.id) return [['list', type(selection.kind).name, type(selection.kind).icon]].concat(selection.kind==='applications'?[['advanced','高級配置','fa-cogs']]:[]);
        if (selection.kind === 'applications') return [['basic', '基本資訊', 'fa-list-alt'], ['forwarding', '轉發規則', 'fa-exchange'], ['advanced', '高級配置', 'fa-cogs'], ['routes', '應用路由', 'fa-sitemap'], ['tamper', '網頁防篡改', 'fa-shield']];
        if (selection.kind === 'certificates') return [['summary', '概要', 'fa-list-alt'], ['info', '憑證資訊', 'fa-certificate'], ['applications', '引用應用', 'fa-globe']];
        if (selection.kind === 'ip-groups') return [['summary', '概要', 'fa-list-alt'], ['members', 'IP 成員', 'fa-list'], ['references', '引用關係', 'fa-link']];
        return [['summary', '概要', 'fa-list-alt'], ['conditions', '匹配條件', 'fa-filter'], ['scope', '作用範圍', 'fa-bullseye'], ['logs', '命中日誌', 'fa-search']];
    }
    function renderSelection(state) {
        if (!state.active || !alive(state.page)) return;
        cancel(state, false);
        const selected = state.selection, items = navItems(selected);
        if (!items.some(function (item) { return item[0] === state.tab; })) state.tab = items[0][0];
        const nav = state.page.down('#safeline-nav'), title = state.page.down('#safeline-heading');
        const previousItems = []; nav.getStore().each(function (row) { previousItems.push([row.get('id'), row.get('name'), row.get('icon')]); });
        if (JSON.stringify(previousItems) !== JSON.stringify(items)) nav.getStore().loadData(items.map(function (item) { return { id: item[0], name: item[1], icon: item[2] }; }));
        nav.getSelectionModel().select(nav.getStore().getById(state.tab), false, true);
        const resource = selected.id && findResource(state);
        title.setText(enc('SafeLine WAF' + (selected.kind !== 'root' ? ' / ' + type(selected.kind).name : '') + (resource ? ' / ' + resource.name : '')));
        const edit = state.page.down('#safeline-resource-edit'), mode = state.page.down('#safeline-resource-mode'), toggle = state.page.down('#safeline-resource-switch');
        if (edit) { edit.setVisible(!!resource && (selected.kind !== 'applications' || state.tab === 'basic')); edit.setDisabled(!resource || !!resource.stale || resource.builtin && ['deny', 'allow', 'policies'].includes(selected.kind) || selected.kind === 'policies'); }
        if (mode) { mode.setVisible(!!resource && selected.kind === 'applications'); mode.setDisabled(!resource || !!resource.stale); }
        if (toggle) { toggle.setVisible(!!resource && ['deny', 'allow', 'policies'].includes(selected.kind)); toggle.setDisabled(!resource || !!resource.stale); }
        if (!state.connection) { note(state, '正在讀取連線設定…'); return; }
        if (selected.kind === 'root' && state.tab === 'connection') { renderConnection(state); return; }
        if (!state.connection.configured) { note(state, '尚未設定 SafeLine。請在「連線資訊」中填寫管理地址及 API Token。'); return; }
        if (selected.kind === 'root') {
            if (state.tab === 'overview') { loadOverview(state); return; }
            if (state.tab === 'events') { renderLogs(state, null); return; }
            if (state.tab === 'challenge') { renderLogs(state, { kind: 'challenge' }); return; }
            if (state.tab === 'policies') {
                if (state.inventory && (state.inventory.errors.deny || state.inventory.errors.allow)) note(state, message(state.inventory.errors.deny || state.inventory.errors.allow));
                else resourceList(state, 'policies'); return;
            }
        }
        if (!state.inventory) { note(state, '正在讀取資源…'); return; }
        if (!selected.id) { if(selected.kind==='applications'&&state.tab==='advanced')renderAdvanced(state);else resourceList(state, selected.kind); return; }
        if (!resource) { note(state, state.inventory.errors[selected.kind] ? message(state.inventory.errors[selected.kind]) : '此資源已不存在，請選擇左側類型目錄返回列表。'); return; }
        renderResource(state, resource);
    }
    function ensurePage(state) {
        if (alive(state.page)) return;
        state.workspace.setContent({ xtype: 'panel', itemId: 'pve-safeline-content', border: false, header: false, layout: 'border', pveSafeLinePage: true,
            tbar: [{ xtype: 'tbtext', itemId: 'safeline-heading', text: 'SafeLine WAF' }, '->', { text: '刷新', iconCls: 'fa fa-refresh', handler: function () { refresh(state); } },
                { text: '編輯', itemId: 'safeline-resource-edit', iconCls: 'fa fa-pencil', hidden: true, handler: function () { editResource(state, state.selection.kind === 'applications' && state.tab === 'proxy' ? 'proxy' : 'settings'); } },
                { text: '運作模式', itemId: 'safeline-resource-mode', iconCls: 'fa fa-play-circle-o', hidden: true, handler: function () { editResource(state, 'mode'); } },
                { text: '規則啟停', itemId: 'safeline-resource-switch', iconCls: 'fa fa-power-off', hidden: true, handler: function () { editResource(state, 'switch'); } },
                { text: '開啟 SafeLine', iconCls: 'fa fa-external-link', handler: function () {
                    const url = state.connection && state.connection.connection.url;
                    if (url && /^https:\/\//.test(url)) window.open(url, '_blank', 'noopener,noreferrer');
                } }],
            items: [{ xtype: 'grid', itemId: 'safeline-nav', region: 'west', width: 165, minWidth: 140, maxWidth: 260, split: true, border: false, hideHeaders: true,
                store: { fields: ['id', 'name', 'icon'], data: [] },
                columns: [{ flex: 1, dataIndex: 'name', renderer: function (value, meta, record) { return '<i class="fa ' + enc(record.data.icon) + '" aria-hidden="true" style="width:23px"></i>' + enc(value); } }],
                listeners: { selectionchange: function (model, records) {
                    const record = records && records[0];
                    if (record && state.tab !== record.get('id')) { state.tab = record.get('id'); state.logPage = 1; state.logFilters = {}; renderSelection(state); }
                }, itemclick: function (view, record) { if (state.tab !== record.get('id')) { state.tab = record.get('id'); state.logPage = 1; state.logFilters = {}; renderSelection(state); } } } },
                { xtype: 'panel', itemId: 'safeline-body', region: 'center', border: false, layout: 'fit' }],
            listeners: { afterrender: function (panel) { if (state.active && user(state)) { state.page = panel; renderSelection(state); } else panel.destroy(); },
                deactivate: function () { if (state.active) leave(state); }, hide: function () { if (state.active) leave(state); },
                beforedestroy: function (panel) { if (state.active && state.page === panel) leave(state); },
                destroy: function (panel) { if (state.page === panel) state.page = null; } } });
    }
    function loadHealth(state, display) {
        request(state, '/status', {}, function (data) {
            state.health = data;
            if (display && state.selection.kind === 'root' && state.tab === 'connection') renderConnection(state);
        }, function (code) {
            state.health = { connected: false, error: code };
            if (display && state.selection.kind === 'root' && state.tab === 'connection') renderConnection(state);
        }, false);
    }
    function loadInventory(state) {
        request(state, '/inventory', {}, function (data) {
            if (!data.resources || !types.every(function (t) { return Array.isArray(data.resources[t.id]); }) || !data.errors || !data.truncated || !Array.isArray(data.policies)) {
                status(state, '資源資料格式不符'); return;
            }
            if (state.origin && state.origin !== data.origin_id) { state.selection = { kind: 'root' }; state.tab = null; state.inventory = null; }
            types.forEach(function (t) {
                if (data.errors[t.id] && state.inventory) data.resources[t.id] = state.inventory.resources[t.id].map(function (row) {
                    return Object.assign({}, row, { stale: true, observed_at: row.observed_at || state.lastInventory });
                });
                else data.resources[t.id].forEach(function (row) { row.observed_at = data.updated_at; });
            });
            state.origin = data.origin_id; state.inventory = data; state.lastInventory = data.updated_at;
            renderTree(state);
            status(state, (Object.keys(data.errors).length ? '部分資源無法更新 · ' : '已更新 · ') + date(data.updated_at));
            if (state.selection.kind === 'root' && state.tab === 'overview') renderOverview(state);
            else if (!alive(state.editor) && !(state.selection.kind === 'root' && ['connection', 'events', 'challenge'].includes(state.tab)) && !(state.selection.kind==='applications'&&state.tab==='advanced')) renderSelection(state);
        }, function (code) {
            status(state, message(code));
            if (state.inventory) types.forEach(function (t) { state.inventory.errors[t.id] = code; state.inventory.resources[t.id].forEach(function (row) { row.stale = true; }); });
            renderTree(state);
            if (state.selection.kind === 'root' && state.tab === 'overview') renderOverview(state);
            if (!state.inventory && state.tab !== 'connection') note(state, message(code));
        }, false);
    }
    function arm(state) {
        if (state.timer) window.clearTimeout(state.timer);
        state.timer = window.setTimeout(function () {
            state.timer = null;
            if (!state.active || !user(state)) return;
            if (!(window.document && window.document.hidden) && !state.editor && state.connection && state.connection.configured) {
                loadInventory(state);
                if (state.selection.kind === 'root' && state.tab === 'overview') loadOverview(state);
            }
            arm(state);
        }, 30000);
    }
    function loadConnection(state) {
        request(state, '/connection', {}, function (data) {
            if (!data.connection || typeof data.configured !== 'boolean' || typeof data.csrf_token !== 'string' || !Number.isSafeInteger(data.revision)) {
                status(state, '連線設定資料格式不符'); return;
            }
            state.connection = data;
            if (!data.configured) { state.selection = { kind: 'root' }; state.tab = 'connection'; }
            renderTree(state); ensurePage(state); renderSelection(state);
            if (data.configured) { loadInventory(state); loadHealth(state, true); } else status(state, '待設定連線');
            arm(state);
        }, function (code) { ensurePage(state); note(state, message(code)); status(state, message(code)); }, false);
    }
    function refresh(state) {
        if (!state.active || !user(state)) return;
        if (!state.connection) { loadConnection(state); return; }
        const advanced=state.page&&state.page.down('#safeline-advanced');if(alive(advanced)&&advanced.reloadAdvanced)advanced.reloadAdvanced();
        if (state.connection.configured) { loadInventory(state); loadHealth(state, true); }
        renderSelection(state);
    }
    function ensureTree(native) {
        if (native._pveSafeLine) return native._pveSafeLine;
        const owner = native.ownerCt, workspace = native.up('pveStdWorkspace');
        if (!owner || typeof owner.add !== 'function' || !workspace || typeof workspace.setContent !== 'function') return null;
        const state = { native: native, workspace: workspace, active: false, generation: 0, navigation: 0, requests: new Set(),
            selection: { kind: 'root' }, tab: null, inventory: null, connection: null, page: null, editor: null, hours: 24, logHours: 0,
            logPage: 1, logMode: 'events', logSource: 'general', logFilters: {}, overviewPending:false, overviewSerial:0, geoRegion:'world', geoMode:'access' };
        const tree = Ext.create('Ext.tree.Panel', { itemId: 'pve-safeline-tree', hidden: true, flex: 1, border: false, rootVisible: true, useArrows: true, hideHeaders: true,
            store: { model: 'Ext.data.TreeModel', autoDestroy: true, root: { id: 'safeline-root', kind: 'root', text: 'SafeLine WAF', iconCls: 'fa fa-shield', expanded: true, children: [] } },
            columns: [{ xtype: 'treecolumn', dataIndex: 'text', flex: 1, renderer: function (value, meta, record) { if (meta) meta.tdAttr = 'data-qtip="' + enc(record.get('qtip') || value) + '"'; return enc(value); } }],
            tbar: [{ text: '刷新', iconCls: 'fa fa-refresh', handler: function () { refresh(state); } }],
            bbar: [{ xtype: 'tbtext', itemId: 'safeline-tree-status', text: '', style: 'white-space:normal' }],
            listeners: { selectionchange: function (model, records) {
                const record = records && records[0];
                if (state.active && record) choose(state, record.get('kind') || 'root', record.get('resourceId'));
            }, itemclick: function (view, record, item, index, event) {
                if (!state.active || event && typeof event.getTarget === 'function' && event.getTarget(view.expanderSelector || '.x-tree-expander')) return;
                const kind = record.get('kind') || 'root', id = record.get('resourceId');
                if (state.selection.kind !== kind || state.selection.id !== id) choose(state, kind, id);
            } } });
        state.tree = tree; native._pveSafeLine = state; states.add(state); owner.add(tree);
        native.on('beforedestroy', function () { leave(state); states.delete(state); if (alive(tree)) tree.destroy(); });
        return state;
    }
    function switchView(native, view) {
        const state = ensureTree(native); if (!state) return;
        const active = !!view && view.id === viewId;
        state.tree.setVisible(active);
        if (active) {
            native.setVisible(false);
            const owner = native.ownerCt;
            ['#pve-komodo-container-tree', '#pve-extra-sites-panel'].forEach(function (selector) { const panel = owner && owner.down && owner.down(selector); if (alive(panel)) panel.setVisible(false); });
            if (!state.active) { state.active = true; ensurePage(state); loadConnection(state); }
        } else if (state.active) leave(state);
    }
    try {
        Ext.define('PVESafeLine.override.Selector', { override: 'PVE.form.ViewSelector', initComponent: function () {
            const result = this.callParent(arguments), store = this.getStore();
            if (!store.findRecord('key', viewId, 0, false, true, true)) store.add({ key: viewId, value: 'SafeLine WAF' });
            return result;
        } });
        Ext.define('PVESafeLine.override.Tree', { override: 'PVE.tree.ResourceTree', initComponent: function () {
            const result = this.callParent(arguments), native = this, previous = this.setViewFilter;
            if (typeof previous !== 'function') return result;
            this.setViewFilter = function (view) {
                const state = native._pveSafeLine;
                if (state && state.active && (!view || view.id !== viewId)) leave(state);
                let returned;
                if (view && view.id === viewId) {
                    const selection = native.getSelectionModel(); selection.suspendEvents(false);
                    try { returned = previous.call(this, { id: viewId, groups: ['node'], getFilterFn: function () { return function () { return false; }; } }); }
                    finally { selection.resumeEvents(); }
                } else returned = previous.apply(this, arguments);
                switchView(native, view); return returned;
            };
            this.on('afterrender', function () { if (native.viewFilter && native.viewFilter.id === viewId) native.setViewFilter(native.viewFilter); });
            return result;
        } });
        Ext.define('PVESafeLine.override.Workspace', { override: 'PVE.StdWorkspace', onLogin: function (data) {
            const workspace = this; workspace._pveSafeLineLoggedOut = !data;
            if (!data) states.forEach(function (state) { if (state.workspace === workspace) { leave(state); state.selection = { kind: 'root' }; status(state, '請先登入 PVE'); } });
            const result = this.callParent(arguments);
            if (data) states.forEach(function (state) { if (state.workspace === workspace && state.native.viewFilter && state.native.viewFilter.id === viewId) { state.active = true; state.tree.setVisible(true); ensurePage(state); loadConnection(state); } });
            return result;
        } });
        window[marker] = true;
    } catch (error) { if (window.console && typeof window.console.warn === 'function') window.console.warn('SafeLine view could not be registered.', error); }
}());
