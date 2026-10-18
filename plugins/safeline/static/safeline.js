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
    };
    function alive(c) { return c && !c.destroyed && !c.destroying; }
    function enc(v) { return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
    function readable(v) { return v == null ? '—' : typeof v === 'boolean' ? v ? '是' : '否' : typeof v === 'object' ? JSON.stringify(v, null, 2) : String(v); }
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
        if (all) { state.generation++; if (state.timer) window.clearTimeout(state.timer); state.timer = null; }
        Array.from(state.requests).forEach(function (entry) {
            if (all || entry.page) { state.requests.delete(entry); if (entry.request) Ext.Ajax.abort(entry.request); }
        });
    }
    function leave(state) {
        state.active = false; cancel(state, true);
        if (alive(state.editor)) state.editor.destroy(); state.editor = null;
        state.connection = null; state.inventory = null; state.overview = null; state.health = null; state.origin = null; state.user = null;
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
            return { id: 'type:' + t.id, kind: t.id, text: t.name + (error ? ' (?)' : ' (' + values.length + (inventory && inventory.truncated[t.id] ? '+' : '') + ')'),
                iconCls: 'fa ' + t.icon, leaf: false, children: values.map(function (r) {
                    return { id: resourceKey(r.kind, r.id), kind: r.kind, resourceId: r.id, text: r.name, iconCls: icon(r),
                        qtip: r.stale ? '資料已過期；最後成功更新：' + date(state.lastInventory) : r.status.label, leaf: true };
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
        state.tab = tab || null; state.logPage = 1; state.logFilters = {}; renderTree(state); renderSelection(state);
    }
    function resourceColumns(kind) {
        const name = { text: '名稱', dataIndex: 'name', flex: 2, minWidth: 180 };
        const statusColumn = { text: '狀態', width: 125, renderer: function (v, m, r) { return enc(r.data.stale ? '資料已過期' : r.data.status.label); } };
        if (kind === 'applications') return [name, statusColumn, { text: '域名', dataIndex: 'domains', flex: 2, renderer: function (v) { return enc((v || []).join(', ')); } },
            { text: '監聽', dataIndex: 'ports', width: 130, renderer: function (v) { return enc((v || []).join(', ')); } }];
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
    function chart(title, points) {
        const valid = (points || []).filter(function (p) { return typeof p.count === 'number' && Number.isFinite(p.count) && p.count >= 0; });
        if (!valid.length) return '<div style="padding:18px"><b>' + enc(title) + '</b><p>暫無可用資料</p></div>';
        const max = Math.max(1, ...valid.map(function (p) { return p.count; }));
        const line = valid.map(function (p, i) { return (32 + i * 636 / Math.max(1, valid.length - 1)).toFixed(1) + ',' + (144 - p.count * 120 / max).toFixed(1); }).join(' ');
        const label = function (p) { return typeof p.time === 'number' ? date(p.time) : p.time; };
        return '<div style="padding:12px 16px"><b>' + enc(title) + '</b><svg role="img" aria-label="' + enc(title) + '" viewBox="0 0 700 176" style="display:block;width:100%;max-height:210px;color:inherit">' +
            '<path d="M32 18V144H670" fill="none" stroke="currentColor" opacity="0.25"/><polyline points="' + line + '" fill="none" stroke="#3892d4" stroke-width="2"/>' +
            '<text x="34" y="16" fill="currentColor" font-size="11">' + enc(max) + '</text><text x="32" y="168" fill="currentColor" font-size="10">' + enc(label(valid[0])) +
            '</text><text x="670" y="168" fill="currentColor" font-size="10" text-anchor="end">' + enc(label(valid[valid.length-1])) + '</text></svg></div>';
    }
    function renderOverview(state) {
        if (!state.overview) { note(state, '正在讀取統計…'); return; }
        const d = state.overview, a = d.sections.access || {}, attack = d.sections.attack || {}, inv = state.inventory;
        const metric = function (label, value) { return '<div style="min-width:130px;flex:1;padding:14px;border:1px solid #8884"><div>' + enc(label) + '</div><div style="font-size:25px;margin-top:7px">' + enc(value == null ? '不可用' : value) + '</div></div>'; };
        let html = '<div style="display:flex;gap:10px;flex-wrap:wrap;padding:14px">' + metric('防護應用', inv && !inv.errors.applications ? inv.resources.applications.length : null) + metric('請求數', a.access) + metric('來源 IP', a.ip) + metric('攻擊來源 IP', attack.attack_ip) + '</div>';
        const labels = { block: '檢測攔截', blacklist: '黑名單', challenge: '驗證', rate_limit: '頻率限制', auth_defense: '認證防護', offline: '離線模式' };
        html += '<div style="display:flex;gap:10px;flex-wrap:wrap;padding:0 14px 14px">' + Object.keys(labels).map(function (k) { return metric(labels[k], attack.intercept && attack.intercept[k]); }).join('') + '</div>';
        html += chart('請求趨勢', d.sections.access_trend) + chart('攔截趨勢', d.sections.intercept_trend) + chart('QPS（上游即時資料）', d.sections.qps);
        Object.keys(d.errors).forEach(function (k) { html += '<p style="padding:0 18px">' + enc(k + '：' + message(d.errors[k])) + '</p>'; });
        const attention = inv ? inv.resources.applications.filter(function (r) { return ['error', 'pending'].includes(r.status.code); }).concat(inv.resources.certificates.filter(function (r) { return ['error', 'pending'].includes(r.status.code); })) : [];
        show(state, { xtype: 'panel', layout: { type: 'vbox', align: 'stretch' }, border: false, scrollable: 'y',
            tbar: [{ xtype: 'combo', fieldLabel: '時間範圍', labelWidth: 70, width: 230, editable: false, queryMode: 'local', displayField: 'label', valueField: 'hours', value: state.hours,
                store: { fields: ['label', 'hours'], data: [{ label: '最近 1 小時', hours: 1 }, { label: '最近 24 小時', hours: 24 }, { label: '最近 7 天', hours: 168 }] },
                listeners: { select: function (combo) { state.hours = combo.getValue(); loadOverview(state); } } }, '->',
                { xtype: 'tbtext', text: enc('更新：' + date(d.updated_at)) }],
            items: [{ xtype: 'component', html: html }, Object.assign(grid(attention, resourceColumns('applications'), function (r) { choose(state, r.kind, r.id); }, '異常應用及即將到期／失效憑證'),
                { title: '需關注', height: 220, minHeight: 180 })] });
    }
    function loadOverview(state) {
        request(state, '/overview?hours=' + state.hours, {}, function (data) { state.overview = data; renderOverview(state); }, function (code) { note(state, message(code)); }, true);
    }
    function renderConnection(state) {
        const d = state.connection, c = d.connection, h = state.health || {};
        show(state, Object.assign(properties([
            ['管理地址', c.url || '尚未設定'], ['API Token', c.token_configured ? '已設定（不顯示原文）' : '尚未設定'],
            ['TLS 憑證驗證', c.tls_verify], ['憑證主機名', c.tls_server_name || '使用管理地址的主機名'],
            ['自訂 CA', c.ca_pem ? '已設定' : '使用系統信任庫'], ['設定來源', d.source === 'saved' ? '頁面保存' : d.source === 'environment' ? '部署設定' : '尚未設定'],
            ['連線狀態', h.connected ? '已連線' : h.error ? message(h.error) : d.configured ? '尚未測試' : '待設定'],
            ['SafeLine 版本', h.version], ['版本類型（上游值）', h.edition], ['最近測試', h.updated_at ? date(h.updated_at) : '—'],
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
        if (r.kind === 'applications') values.push(['域名', r.domains], ['監聽端口', r.ports], ['回源', r.upstreams], ['說明', r.comment],
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
            if (tab === 'proxy') { show(state, properties(Object.keys(r.proxy || {}).map(function (k) { return [k, r.proxy[k]]; }), '代理設定')); return; }
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
    function logDetail(state, row, events) {
        const generation = state.generation, who = user(state), values = [];
        Object.keys(row).forEach(function (key) { values.push([key, ['start_at', 'end_at', 'updated_at'].includes(key) && events ? date(row[key], true) :
            ['created_at', 'timestamp'].includes(key) ? date(row[key]) : row[key]]); });
        if (!events) values.push(['內容保護', '已遮罩認證與 Cookie 標頭；請求及回應本文不在此頁展示。']);
        let dialog;
        const buttons = events ? [{ text: '查詢相同來源與時間的日誌', handler: function () {
            if (!current(state, generation, who)) return;
            state.logMode = 'records'; state.logPage = 1;
            state.logFilters = { ip: row.ip, host: row.host, port: String(row.dst_port), start: row.start_at, end: row.end_at };
            dialog.destroy(); renderLogs(state, null);
        } }] : [];
        buttons.push({ text: '關閉', handler: function () { dialog.close(); } });
        dialog = Ext.create('Ext.window.Window', { title: events ? '安全事件詳情' : '攻擊日誌詳情', width: 820, height: 580, modal: true, constrain: true,
            closeAction: 'destroy', layout: 'fit', items: [properties(values)], buttons: buttons,
            listeners: { destroy: function () { if (state.editor === dialog) state.editor = null; } } });
        state.editor = dialog; dialog.show();
    }
    function renderLogs(state, context) {
        const policy = context && ['deny', 'allow', 'policies'].includes(context.kind);
        const application = context && context.kind === 'applications';
        const mode = context ? 'records' : state.logMode;
        if (application && (!context.domains.length || context.domains.some(function (d) { return d.includes('*'); }))) {
            note(state, '此應用的域名無法精確限定日誌。請在總覽的安全事件頁，按實際 Host、端口及來源 IP 查詢。'); return;
        }
        if (application && !state.logFilters.host) { state.logFilters.host = context.domains[0]; state.logFilters.port = context.ports[0]; }
        const events = mode === 'events', filters = state.logFilters || {}, source = policy ? 'rule' : state.logSource;
        const params = { page: state.logPage, page_size: 20, source: source };
        Object.keys(filters).forEach(function (key) { if (filters[key] != null && filters[key] !== '') params[key] = filters[key]; });
        if (policy) params.policy_rule_id = context.id;
        if (state.logHours && !params.start) { params.end = Date.now(); params.start = params.end - state.logHours * 3600000; }
        note(state, '正在讀取' + (events ? '安全事件' : '攻擊日誌') + '…');
        request(state, '/' + (events ? 'events' : 'records') + '?' + new URLSearchParams(params).toString(), {}, function (data) {
            if (!Array.isArray(data.items) || !Number.isSafeInteger(data.total) || data.total < 0) { note(state, '日誌資料格式不符'); return; }
            const columns = events ? [
                { text: '來源 IP', dataIndex: 'ip', width: 155 }, { text: 'Host', dataIndex: 'host', flex: 1, minWidth: 170 },
                { text: '端口', dataIndex: 'dst_port', width: 80 }, { text: '開始', dataIndex: 'start_at', width: 175, renderer: function (v) { return enc(date(v, true)); } },
                { text: '攔截', dataIndex: 'deny_count', width: 85 }, { text: '放行', dataIndex: 'pass_count', width: 85 },
            ] : [
                { text: '時間', dataIndex: 'timestamp', width: 175, renderer: function (v) { return enc(date(v)); } }, { text: '來源 IP', dataIndex: 'src_ip', width: 155 },
                { text: 'Host', dataIndex: 'host', flex: 1, minWidth: 140 }, { text: '路徑', dataIndex: 'url_path', flex: 1, minWidth: 130 },
                { text: '動作', dataIndex: 'action', width: 90, renderer: function (v) { return enc(({ 0: '放行', 1: '攔截' })[v] || '動作 ' + v); } },
                { text: '原因', dataIndex: 'reason', flex: 1, minWidth: 110 },
            ];
            const config = grid(data.items, columns, function (row) { logDetail(state, row, events); });
            const toolbar = [];
            if (!context) {
                toolbar.push({ text: '聚合事件', pressed: events, enableToggle: true, handler: function () { state.logMode = 'events'; state.logPage = 1; state.logFilters = {}; renderLogs(state, context); } },
                    { text: '逐筆日誌', pressed: !events, enableToggle: true, handler: function () { state.logMode = 'records'; state.logPage = 1; state.logFilters = {}; renderLogs(state, context); } });
            }
            if (!policy) toolbar.push({ xtype: 'combo', width: 125, queryMode: 'local', editable: false, displayField: 'name', valueField: 'id', value: source,
                store: { fields: ['id', 'name'], data: [{ id: 'general', name: '一般檢測' }, { id: 'rule', name: '自訂規則' }] },
                listeners: { select: function (combo) { state.logSource = combo.getValue(); state.logPage = 1; renderLogs(state, context); } } });
            toolbar.push({ xtype: 'textfield', itemId: 'safeline-filter-ip', emptyText: '來源 IP', width: 145, value: filters.ip || '' },
                { xtype: 'textfield', itemId: 'safeline-filter-host', emptyText: 'Host', width: 165, value: filters.host || '' },
                { xtype: 'textfield', itemId: 'safeline-filter-port', emptyText: '端口', width: 70, value: filters.port || '' },
                { xtype: 'combo', itemId: 'safeline-filter-hours', width: 120, queryMode: 'local', editable: false, displayField: 'name', valueField: 'hours', value: state.logHours,
                    store: { fields: ['name', 'hours'], data: [{ name: '全部時間', hours: 0 }, { name: '最近 24 小時', hours: 24 }, { name: '最近 7 天', hours: 168 }, { name: '最近 30 天', hours: 720 }] } },
                { text: '查詢', handler: function (button) {
                    const panel = button.up('grid'); state.logPage = 1;
                    state.logHours = panel.down('#safeline-filter-hours').getValue();
                    state.logFilters = { ip: String(panel.down('#safeline-filter-ip').getValue() || '').trim(), host: String(panel.down('#safeline-filter-host').getValue() || '').trim(),
                        port: String(panel.down('#safeline-filter-port').getValue() || '').trim() };
                    renderLogs(state, context);
                } });
            config.tbar = { enableOverflow: true, items: toolbar };
            config.bbar = [{ text: '上一頁', disabled: state.logPage <= 1, handler: function () { state.logPage--; renderLogs(state, context); } },
                { xtype: 'tbtext', text: enc('第 ' + state.logPage + ' 頁 · 共 ' + data.total + ' 筆') },
                { text: '下一頁', disabled: state.logPage * 20 >= data.total, handler: function () { state.logPage++; renderLogs(state, context); } }, '->',
                { xtype: 'tbtext', text: enc(application ? '按 Host／端口查詢全域日誌；雙擊查看詳情' : filters.start ? '依事件來源及時間範圍篩選；雙擊查看詳情' : '雙擊查看詳情') }];
            show(state, config);
        }, function (code) { note(state, message(code)); }, true);
    }
    function navItems(selection) {
        if (selection.kind === 'root') return [['overview', '總覽', 'fa-dashboard'], ['events', '安全事件', 'fa-shield'], ['policies', '防護規則', 'fa-filter'], ['connection', '連線資訊', 'fa-plug']];
        if (!selection.id) return [['list', type(selection.kind).name, type(selection.kind).icon]];
        if (selection.kind === 'applications') return [['summary', '概要', 'fa-list-alt'], ['proxy', '代理設定', 'fa-exchange'], ['protection', '防護設定', 'fa-shield'], ['logs', '安全事件與日誌', 'fa-search'], ['certificates', 'TLS 憑證', 'fa-certificate']];
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
        nav.getStore().loadData(items.map(function (item) { return { id: item[0], name: item[1], icon: item[2] }; }));
        nav.getSelectionModel().select(nav.getStore().getById(state.tab), false, true);
        const resource = selected.id && findResource(state);
        title.setText(enc('SafeLine WAF' + (selected.kind !== 'root' ? ' / ' + type(selected.kind).name : '') + (resource ? ' / ' + resource.name : '')));
        if (!state.connection) { note(state, '正在讀取連線設定…'); return; }
        if (selected.kind === 'root' && state.tab === 'connection') { renderConnection(state); return; }
        if (!state.connection.configured) { note(state, '尚未設定 SafeLine。請在「連線資訊」中填寫管理地址及 API Token。'); return; }
        if (selected.kind === 'root') {
            if (state.tab === 'overview') { loadOverview(state); return; }
            if (state.tab === 'events') { renderLogs(state, null); return; }
            if (state.tab === 'policies') {
                if (state.inventory && (state.inventory.errors.deny || state.inventory.errors.allow)) note(state, message(state.inventory.errors.deny || state.inventory.errors.allow));
                else resourceList(state, 'policies'); return;
            }
        }
        if (!state.inventory) { note(state, '正在讀取資源…'); return; }
        if (!selected.id) { resourceList(state, selected.kind); return; }
        if (!resource) { note(state, state.inventory.errors[selected.kind] ? message(state.inventory.errors[selected.kind]) : '此資源已不存在，請選擇左側類型目錄返回列表。'); return; }
        renderResource(state, resource);
    }
    function ensurePage(state) {
        if (alive(state.page)) return;
        state.workspace.setContent({ xtype: 'panel', itemId: 'pve-safeline-content', border: false, header: false, layout: 'border', pveSafeLinePage: true,
            tbar: [{ xtype: 'tbtext', itemId: 'safeline-heading', text: 'SafeLine WAF' }, '->', { text: '刷新', iconCls: 'fa fa-refresh', handler: function () { refresh(state); } },
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
            if (state.inventory) types.forEach(function (t) {
                if (data.errors[t.id]) data.resources[t.id] = state.inventory.resources[t.id].map(function (row) { return Object.assign({}, row, { stale: true }); });
            });
            state.origin = data.origin_id; state.inventory = data; state.lastInventory = data.updated_at;
            renderTree(state);
            status(state, (Object.keys(data.errors).length ? '部分資源無法更新 · ' : '已更新 · ') + date(data.updated_at));
            if (state.selection.kind === 'root' && state.tab === 'overview' && state.overview) renderOverview(state);
            else if (!(state.selection.kind === 'root' && ['connection', 'events'].includes(state.tab))) renderSelection(state);
        }, function (code) {
            status(state, message(code));
            if (state.inventory) types.forEach(function (t) { state.inventory.resources[t.id].forEach(function (row) { row.stale = true; }); });
            renderTree(state);
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
        if (state.connection.configured) { loadInventory(state); loadHealth(state, true); }
        renderSelection(state);
    }
    function ensureTree(native) {
        if (native._pveSafeLine) return native._pveSafeLine;
        const owner = native.ownerCt, workspace = native.up('pveStdWorkspace');
        if (!owner || typeof owner.add !== 'function' || !workspace || typeof workspace.setContent !== 'function') return null;
        const state = { native: native, workspace: workspace, active: false, generation: 0, navigation: 0, requests: new Set(),
            selection: { kind: 'root' }, tab: null, inventory: null, connection: null, page: null, editor: null, hours: 24, logHours: 0,
            logPage: 1, logMode: 'events', logSource: 'general', logFilters: {} };
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
