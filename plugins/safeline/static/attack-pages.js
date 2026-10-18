(function () {
    // SafeLine-only asset. All data and edits use the existing PVE session boundary.
    window.__pveSafeLineAttackPages = function (ctx) {
        const Ext = window.Ext, state = ctx.state, enc = ctx.enc, alive = ctx.alive;
        if (Ext.util && Ext.util.CSS) Ext.util.CSS.createStyleSheet('.sl-ip-choice{font-family:FontAwesome;display:inline-block;width:23px}.sl-ip-choice:before{content:"\\f096"}.x-boundlist-selected .sl-ip-choice:before{content:"\\f046"}', 'safeline-ip-choices');
        const attackNames = ['SQL 注入', 'XSS', 'CSRF', 'SSRF', '拒絕服務', '後門', '反序列化', '程式碼執行', '程式碼注入', '命令注入',
            '檔案上傳', '檔案包含', '重新導向', '權限不當', '資訊洩漏', '未授權存取', '不安全的設定', 'XXE', 'XPath 注入', 'LDAP 注入',
            '目錄穿越', '掃描器', '水平權限繞過', '垂直權限繞過', '檔案修改', '檔案讀取', '檔案刪除', '邏輯錯誤', 'CRLF 注入', '範本注入',
            '點擊劫持', '緩衝區溢位', '整數溢位', '格式化字串', '競爭條件', 'HTTP 協定違規', 'HTTP 請求走私'];
        const extraAttacks = { 61: '逾時', 62: '未知', 63: '威脅情報', 64: 'Cookie 篡改' };
        const modules = { blacklist: '黑名單', whitelist: '白名單', m_asp_code_injection: 'ASP 程式碼注入檢測', m_cmd_injection: '命令注入檢測',
            m_csrf: 'CSRF 檢測', m_file_include: '檔案包含檢測', m_file_upload: '檔案上傳檢測', m_http: '畸形 HTTP 協定檢測',
            m_java_unserialize: 'JAVA 反序列化檢測', m_java: 'JAVA 程式碼注入檢測', m_php_code_injection: 'PHP 程式碼注入檢測',
            m_php_unserialize: 'PHP 反序列化檢測', m_response: '伺服器回應檢測', m_rule: '加強規則', m_scanner: '機器人檢測',
            m_sqli: 'SQL 注入檢測', m_ssrf: 'SSRF 檢測', m_ssti: '範本注入檢測', m_timeout: '檢測逾時', m_xss: 'XSS 檢測' };
        function attack(v) { return ({ '-4': '超長資料', '-3': '黑名單', '-2': '白名單', '-1': '非攻擊' })[v] || attackNames[v] || extraAttacks[v] || (v == null ? '—' : '類型 ' + v); }
        function moduleName(v) { return modules[v] || v || '—'; }
        function action(v) { return v === 0 ? '放行' : v === 1 ? '攔截' : v == null ? '—' : '動作 ' + v; }
        function geo(row) {
            if (row.country === 'B1') return '區域網路';
            let country = row.country || '';
            try { if (/^[A-Z]{2}$/.test(country) && typeof Intl.DisplayNames === 'function') country = new Intl.DisplayNames(['zh-Hant'], { type: 'region' }).of(country); } catch (e) { /* keep upstream label */ }
            return Array.from(new Set([country, row.province, row.city].filter(Boolean))).join(' · ') || '—';
        }
        function duration(row) {
            const start = row.start_at, end = row.end_at;
            if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return '—';
            const seconds = Math.floor((end - start) / 1000), minutes = Math.floor(seconds / 60), hours = Math.floor(minutes / 60);
            return hours ? hours + ' 小時 ' + minutes % 60 + ' 分' : minutes ? minutes + ' 分 ' + seconds % 60 + ' 秒' : seconds + ' 秒';
        }
        function portrait(value, fingerprint) {
            if (typeof value !== 'string' || !value) return;
            window.open((fingerprint ? 'https://intelligence.app.cyberserval.tech/?search=' : 'https://rivers.chaitin.cn/ip-intelligence?search=') + encodeURIComponent(value), '_blank', 'noopener,noreferrer');
        }
        function combo(name, label, value, options) { return Object.assign(ctx.combo(name, label, value, options), { forceSelection: true }); }
        function cellText(value, meta) { if (meta) meta.tdAttr = 'title="' + enc(value) + '"'; return enc(value); }
        function stop() {
            if (state.logTimer) window.clearTimeout(state.logTimer);
            if (state.logFitTimer) window.clearTimeout(state.logFitTimer);
            state.logTimer = null; state.logFitTimer = null; state.logSerial = (state.logSerial || 0) + 1;
        }
        function closeEditor() { if (alive(state.editor)) state.editor.destroy(); state.editor = null; }

        function addIp(ip) {
            if (!ctx.user()) return;
            closeEditor();
            const generation = state.generation, who = ctx.user();
            let loaded, pending = false, create = false, operation = ctx.uuid(), dialog;
            function notice(text) { if (alive(dialog)) dialog.down('#safeline-ip-status').update(enc(text)); }
            function save() {
                if (pending || !loaded || !operation || !ctx.current(generation, who) || !dialog.down('form').getForm().isValid()) return;
                const value = String(dialog.down('#safeline-ip-address').getValue() || '').trim();
                const ids = dialog.down('#safeline-ip-groups').getValue() || [], comment = String(dialog.down('#safeline-ip-name').getValue() || '').trim();
                if (!value || (create ? !comment : !ids.length)) { notice(create ? '請填寫 IP 與新組名稱。' : '請選擇至少一個 IP 組。'); return; }
                pending = true; dialog.down('#safeline-ip-save').setDisabled(true); dialog.down('#safeline-ip-cancel').setDisabled(true); dialog.down('#safeline-ip-create').setDisabled(true);
                notice('正在儲存並回讀確認…');
                ctx.request('/ip-groups' + (create ? '' : '/append'), { method: 'POST', headers: { 'X-PVE-Plugins-CSRF': state.connection.csrf_token }, jsonData: {
                    expected_revision: loaded.connection_revision, fingerprint: loaded.fingerprint, operation_id: operation,
                    changes: create ? { ip: value, comment: comment } : { ip: value, ip_group_ids: ids.map(Number) } } }, function (result) {
                    pending = false;
                    if (!alive(dialog)) return;
                    if (result.outcome === 'confirmed') { dialog.destroy(); ctx.loadInventory(); return; }
                    notice(result.outcome === 'unknown' ? ctx.message('safeline_write_unknown') : ctx.message(result.code));
                    dialog.down('#safeline-ip-cancel').setDisabled(false);
                }, function (code) { pending = false; notice(ctx.message(code) + '；請關閉視窗後核對實際 IP 組內容。'); if (alive(dialog)) dialog.down('#safeline-ip-cancel').setDisabled(false); }, false);
            }
            dialog = Ext.create('Ext.window.Window', { title: '加入 IP 組', width: 520, modal: true, constrain: true, layout: 'fit', closeAction: 'destroy',
                items: [{ xtype: 'form', border: false, bodyPadding: 16, defaults: { anchor: '100%', labelWidth: 105 }, items: [
                    { xtype: 'textfield', itemId: 'safeline-ip-address', fieldLabel: 'IP 或網段', value: ip || '', allowBlank: false, maxLength: 128 },
                    { xtype: 'combo', itemId: 'safeline-ip-groups', fieldLabel: 'IP 組', multiSelect: true, editable: false, queryMode: 'local', forceSelection: true,
                        displayField: 'name', valueField: 'id', store: { fields: ['id', 'name'], data: [] }, value: [],
                        listConfig: { getInnerTpl: function () { return '<span class="sl-ip-choice" aria-hidden="true"></span>{name:htmlEncode}'; } } },
                    { xtype: 'textfield', itemId: 'safeline-ip-name', fieldLabel: '新 IP 組名稱', hidden: true, maxLength: 1024 },
                    { xtype: 'component', itemId: 'safeline-ip-status', html: '正在讀取可編輯的 IP 組…', style: 'white-space:normal;overflow-wrap:anywhere' },
                ] }], buttons: [{ text: '新增 IP 組', itemId: 'safeline-ip-create', disabled: true, handler: function () {
                    create = !create; dialog.down('#safeline-ip-groups').setVisible(!create); dialog.down('#safeline-ip-name').setVisible(create);
                    dialog.down('#safeline-ip-create').setText(create ? '選擇現有 IP 組' : '新增 IP 組'); dialog.down('#safeline-ip-save').setText(create ? '建立並加入' : '加入');
                } }, '->', { text: '加入', itemId: 'safeline-ip-save', disabled: true, handler: save },
                    { text: '取消', itemId: 'safeline-ip-cancel', handler: function () { dialog.close(); } }],
                listeners: { beforeclose: function () { return !pending; }, destroy: function () { if (state.editor === dialog) state.editor = null; } } });
            state.editor = dialog; dialog.show();
            ctx.request('/ip-groups/append', {}, function (data) {
                if (!alive(dialog)) return;
                if (state.connection.revision !== data.connection_revision) { notice(ctx.message('safeline_connection_changed')); return; }
                loaded = data; dialog.down('#safeline-ip-groups').getStore().loadData(data.groups);
                dialog.down('#safeline-ip-save').setDisabled(!operation); dialog.down('#safeline-ip-create').setDisabled(!operation);
                notice(data.groups.length ? '可多選；僅列出可手動維護的 IP 組。' : '目前沒有可手動維護的 IP 組，可以新增一組。');
            }, function (code) { notice(ctx.message(code)); }, false);
        }
        function packet(row, response) {
            return [row[response ? 'rsp_header' : 'req_header'] || '', row[response ? 'rsp_body' : 'req_body'] || ''].filter(Boolean).join('\r\n\r\n') || '未記錄報文';
        }
        function detail(row, source) {
            if (!ctx.user()) return;
            closeEditor(); let dialog;
            dialog = Ext.create('Ext.window.Window', { title: '攻擊日誌詳情', width: 1000, height: Math.min(760, (window.innerHeight || 840) - 60), modal: true, constrain: true, closeAction: 'destroy', layout: 'fit',
                items: [{ xtype: 'panel', itemId: 'safeline-attack-detail', border: false, layout: { type: 'vbox', align: 'stretch' }, bodyPadding: 12 }],
                buttons: [{ text: '關閉', handler: function () { dialog.close(); } }], listeners: { destroy: function () { if (state.editor === dialog) state.editor = null; } } });
            state.editor = dialog; dialog.show();
            const body = dialog.down('#safeline-attack-detail');
            body.add({ xtype: 'component', html: '正在讀取詳情…' });
            if (!row.event_id) { body.removeAll(); body.add({ xtype: 'component', html: '此記錄未提供日誌 ID，無法讀取詳情。' }); return; }
            ctx.request('/records/' + encodeURIComponent(row.event_id) + '?source=' + source, {}, function (data) {
                if (!alive(dialog)) return;
                if (data.connection_revision !== state.connection.revision) { body.removeAll(); body.add({ xtype: 'component', html: enc(ctx.message('safeline_connection_changed')) }); return; }
                const value = data.record; body.removeAll();
                const summary = ctx.properties([['攻擊類型', attack(value.attack_type)], ['攻擊地址', value.website || (value.host || '') + (value.url_path || '') || '—'],
                    ['處置動作', action(value.action)], ['攻擊者', (value.src_ip || '—') + '（' + geo(value) + '）'], ['JA4 指紋', value.ja4_fingerprint || '—'],
                    ['攻擊載荷', [value.location, value.decode_path, value.payload].filter(Boolean).join(' → ') || '—'], ['命中防護模組', moduleName(value.module)],
                    ['攻擊時間', ctx.date(value.timestamp)], ['ID', value.event_id]], '攻擊資訊');
                summary.height = 330;
                summary.columns[1].renderer = enc;
                summary.tbar = [{ text: '加入 IP 組', disabled: !value.src_ip, handler: function () { addIp(value.src_ip); } },
                    { text: '查看 IP 畫像', iconCls: 'fa fa-external-link', disabled: !value.src_ip, handler: function () { portrait(value.src_ip, false); } },
                    { text: '查看指紋畫像', iconCls: 'fa fa-external-link', disabled: !value.ja4_fingerprint, handler: function () { portrait(value.ja4_fingerprint, true); } }];
                body.add(summary);
                let response = false, encoding = 'text';
                function updatePacket() {
                    let content = packet(value, response);
                    if (encoding === 'hex') content = Array.from(new window.TextEncoder().encode(content)).map(function (b) { return b.toString(16).padStart(2, '0'); }).join(' ');
                    dialog.down('#safeline-attack-packet').update('<pre style="white-space:pre-wrap;overflow-wrap:anywhere;margin:0;font-family:monospace">' + enc(content) + '</pre>');
                }
                body.add({ xtype: 'panel', title: 'HTTP 報文', flex: 1, minHeight: 0, border: false, layout: 'fit',
                    tbar: [{ text: '請求報文', enableToggle: true, pressed: true, toggleGroup: dialog.id + '-packet', allowDepress: false, handler: function () { response = false; updatePacket(); } },
                        { text: '回應報文', enableToggle: true, toggleGroup: dialog.id + '-packet', allowDepress: false, handler: function () { response = true; updatePacket(); } }, '->',
                        Object.assign(combo('encoding', '', 'text', [['text', 'UTF-8'], ['hex', '十六進位']]), { width: 120, listeners: { select: function (field) { encoding = field.getValue(); updatePacket(); } } })],
                    items: [{ xtype: 'panel', itemId: 'safeline-attack-packet', scrollable: true, bodyPadding: 12, border: true }],
                    bbar: [{ xtype: 'tbtext', text: '認證與 Cookie 內容已遮罩' + (value.content_truncated ? '；報文過長，僅顯示部分內容' : '') }] });
                updatePacket();
            }, function (code) { if (alive(dialog)) { body.removeAll(); body.add({ xtype: 'component', html: enc(ctx.message(code)) }); } }, false);
        }
        function drill(row, denied, context) {
            if (!row.ip || !row.host || !row.dst_port || !Number.isFinite(row.start_at) || !Number.isFinite(row.end_at) || row.end_at < row.start_at) { ctx.note('事件缺少完整的來源與時間，無法精確查詢相關日誌。'); return; }
            const filters = { ip: row.ip, host: row.host, port: row.dst_port == null ? '' : String(row.dst_port), start: row.start_at, end: row.end_at, action: denied ? '1' : '0' };
            state.logMode = 'records'; state.logPage = 1; state.logFilters = filters; state.logHours = 0; state.logRelated = true;
            render(context);
        }
        function cell(table, index, record, events, context, source) {
            // Resolve the visible column, including after the user reorders or hides columns.
            const column = table.getVisibleColumnManager().getHeaderAtIndex(index), key = column && column.dataIndex, row = record.data;
            if (key === 'ip' || key === 'src_ip') addIp(events ? row.ip : row.src_ip);
            else if (key === 'ip_portrait') portrait(events ? row.ip : row.src_ip, false);
            else if (events && (key === 'pass_count' || key === 'deny_count')) drill(row, key === 'deny_count', context);
            else if (!events) detail(row, source);
        }
        function filterForm(events, filters, apply) {
            const fields = [
                { xtype: 'textfield', name: 'ip', fieldLabel: '攻擊 IP', value: filters.ip || '' },
                { xtype: 'textfield', name: 'host', fieldLabel: '域名', value: filters.host || '' },
                { xtype: 'numberfield', name: 'port', fieldLabel: '端口', value: filters.port || '', minValue: 1, maxValue: 65535, allowDecimals: false },
                Object.assign(combo('hours', '時間範圍', state.logHours || 0, [[0, '全部時間'], [24, '近 24 小時'], [168, '近 7 天'], [720, '近 30 天']]), { name: 'hours' }),
                { xtype: 'datefield', name: 'start', fieldLabel: '開始時間', value: filters.start != null ? new Date(filters.start) : null, format: 'Y-m-d H:i:s', altFormats: 'Y-m-d H:i|Y-m-d|c', emptyText: '年-月-日 時:分:秒' },
                { xtype: 'datefield', name: 'end', fieldLabel: '結束時間', value: filters.end != null ? new Date(filters.end) : null, format: 'Y-m-d H:i:s', altFormats: 'Y-m-d H:i|Y-m-d|c', emptyText: '年-月-日 時:分:秒' },
            ];
            if (!events) fields.push(combo('action', '動作', filters.action || '', [['', '全部'], ['0', '放行'], ['1', '攔截']]),
                Object.assign(combo('attack_type', '攻擊類型', filters.attack_type ? String(filters.attack_type).split(',') : [],
                    [['-4', '超長資料'], ['-3', '黑名單'], ['-2', '白名單'], ['-1', '非攻擊']].concat(attackNames.map(function (name, id) { return [String(id), name]; }), Object.keys(extraAttacks).map(function (id) { return [id, extraAttacks[id]]; }))), { multiSelect: true }),
                { xtype: 'textfield', name: 'url', fieldLabel: '路徑', value: filters.url || '' },
                { xtype: 'textfield', name: 'event_id', fieldLabel: 'ID', value: filters.event_id || '' },
                { xtype: 'textfield', name: 'ja4_fingerprint', fieldLabel: 'JA4 指紋', value: filters.ja4_fingerprint || '' });
            fields.push(Object.assign(combo('auto_refresh', '自動刷新', state.logAutoRefresh || 0, [[0, '關閉'], [5, '每 5 秒'], [10, '每 10 秒'], [30, '每 30 秒'], [60, '每 60 秒']]), {
                listeners: { select: function (field) { state.logAutoRefresh = Number(field.getValue()) || 0; apply(); } } }));
            fields.forEach(function (field) { Object.assign(field, { columnWidth: 0.25, padding: '0 10 6 0', labelAlign: 'top', maxLength: 1024 });
                field.listeners = Object.assign({ specialkey: function (f, event) { if (event.getKey() === event.ENTER) apply(); } }, field.listeners || {}); });
            return { xtype: 'form', itemId: 'safeline-attack-filters', border: false, bodyPadding: '5 10 0', layout: 'column', items: fields };
        }
        function render(context) {
            stop();
            const serial = state.logSerial, policy = context && ['deny', 'allow', 'policies'].includes(context.kind), application = context && context.kind === 'applications';
            const events = !context && state.logMode === 'events', source = policy ? 'rule' : state.logSource;
            if (application && (!context.domains.length || context.domains.some(function (d) { return d.includes('*'); }))) { ctx.note('此應用的域名無法精確限定日誌，請在攻擊防護頁按實際 Host、端口及來源 IP 查詢。'); return; }
            if (application && !state.logFilters.host) {
                const listener = (context.listeners || [])[0], match = String((context.ports || [])[0] || '').match(/^([1-9][0-9]{0,4})(?:_ssl)?$/), port = listener ? listener.port : match && Number(match[1]);
                if (!port || port > 65535) { ctx.note('無法核對此應用的監聽端口，請在攻擊防護頁依實際 Host／端口查詢。'); return; }
                state.logFilters.host = context.domains[0]; state.logFilters.port = String(port);
            }
            const mode = events ? 'events' : 'records', fitted = state.logFittedSizes || (state.logFittedSizes = {});
            const filters = state.logFilters || {}, size = state.logPageSize || fitted[mode] || 20, params = { page: state.logPage, page_size: size, source: source };
            Object.keys(filters).forEach(function (key) { if (filters[key] != null && filters[key] !== '') params[key] = filters[key]; });
            if (policy) params.policy_rule_id = context.id;
            if (state.logHours && params.start == null && params.end == null) { params.end = Date.now(); params.start = params.end - state.logHours * 3600000; }
            ctx.note('正在讀取' + (events ? '攻擊事件' : '攻擊日誌') + '…');
            ctx.request('/' + (events ? 'events' : 'records') + '?' + new URLSearchParams(params).toString(), {}, function (data) {
                if (serial !== state.logSerial) return;
                if (data.connection_revision !== state.connection.revision || !Array.isArray(data.items) || !Number.isSafeInteger(data.total) || data.total < 0) { ctx.note('日誌資料已變更或格式不符，請刷新。'); return; }
                const ip = { text: '來源 IP', dataIndex: events ? 'ip' : 'src_ip', width: 300, renderer: cellText }, icon = { text: 'IP 畫像', dataIndex: 'ip_portrait', width: 78, minWidth: 65, align: 'center', renderer: function (v, meta, r) { return (events ? r.data.ip : r.data.src_ip) ? '<span class="fa fa-external-link" role="img" aria-label="雙擊查看 IP 畫像"></span>' : '—'; } },
                    location = { text: '來源 IP 屬地', dataIndex: 'geography', width: 250, renderer: function (v, m, r) { return cellText(geo(r.data), m); } },
                    host = { text: events ? '應用' : 'HOST', dataIndex: 'host', width: 240, renderer: cellText };
                const columns = events ? [ip, icon, location, host,
                    { text: '放行攻擊', dataIndex: 'pass_count', width: 100, align: 'right', renderer: function (v) { return enc(ctx.amount(v)); } },
                    { text: '攔截攻擊', dataIndex: 'deny_count', width: 100, align: 'right', renderer: function (v) { return enc(ctx.amount(v)); } },
                    { text: '持續時間', dataIndex: 'duration', width: 120, renderer: function (v, m, r) { return enc(duration(r.data)); } },
                    { text: '開始時間', dataIndex: 'start_at', flex: 1, minWidth: 180, maxWidth: 240, renderer: function (v) { return enc(ctx.date(v, true)); } },
                    { text: '結束時間', dataIndex: 'end_at', flex: 1, minWidth: 180, maxWidth: 240, renderer: function (v) { return enc(ctx.date(v, true)); } }]
                    : [{ text: '時間', dataIndex: 'timestamp', width: 180, renderer: function (v) { return enc(ctx.date(v)); } },
                        { text: '動作', dataIndex: 'action', width: 80, renderer: function (v) { return enc(action(v)); } }, ip, icon, location, host,
                        { text: '路徑', dataIndex: 'url_path', flex: 1, minWidth: 200, renderer: cellText }, { text: '攻擊類型', dataIndex: 'attack_type', width: 130, renderer: function (v) { return enc(attack(v)); } }];
                if (!events && data.module_in_list !== false) columns.push({ text: '命中防護模組', dataIndex: 'module', width: 190, renderer: function (v) { return enc(moduleName(v)); } });
                let panel, table;
                function scheduleFit() {
                    if (serial !== state.logSerial || !alive(table)) return;
                    if (state.logFitTimer) window.clearTimeout(state.logFitTimer);
                    state.logFitTimer = null;
                    if (state.logPageSize) return;
                    state.logFitTimer = window.setTimeout(function () {
                        state.logFitTimer = null;
                        if (!alive(table) || serial !== state.logSerial || !ctx.user() || state.logPageSize || typeof table.getView !== 'function') return;
                        if (panel.down('#safeline-attack-filters').getForm().isDirty()) return;
                        const view = table.getView(), element = view.getEl(), node = view.getNode(0);
                        const height = element && element.dom.clientHeight;
                        const rowHeight = node ? node.getBoundingClientRect().height : state.logRowHeight || 25;
                        if (!(height > 0) || !(rowHeight > 0)) return;
                        state.logRowHeight = rowHeight;
                        // Keep complete rows inside the measured body, including its table border.
                        const next = Math.max(1, Math.min(100, Math.floor((height - 1) / rowHeight)));
                        fitted[mode] = next;
                        if (next === size) return;
                        const first = (state.logPage - 1) * size;
                        state.logPage = Math.min(Math.max(1, Math.floor(first / next) + 1), Math.max(1, Math.ceil(data.total / next)));
                        render(context);
                    }, 120);
                }
                function apply() {
                    const form = panel.down('#safeline-attack-filters'); if (!form.getForm().isValid()) return;
                    const next = {}, value = function (name) { return form.down('[name=' + name + ']').getValue(); };
                    ['ip', 'host', 'port', 'action', 'attack_type', 'event_id', 'url', 'ja4_fingerprint'].forEach(function (key) {
                        const field = form.down('[name=' + key + ']'); if (!field) return;
                        const raw = field.getValue(), text = (Array.isArray(raw) ? raw.join(',') : String(raw == null ? '' : raw)).trim(); if (text) next[key] = text;
                    });
                    for (const key of ['start', 'end']) { const date = value(key); if (date) { if (!Number.isFinite(date.getTime()) || date.getTime() < 0) return; next[key] = date.getTime(); } }
                    if (next.start != null && next.end != null && next.start > next.end) { panel.down('#safeline-attack-status').setText('開始時間不能晚於結束時間'); return; }
                    state.logHours = Number(value('hours')) || 0; state.logAutoRefresh = Number(value('auto_refresh')) || 0;
                    state.logFilters = next; state.logPage = 1; state.logRelated = false; render(context);
                }
                const config = ctx.grid(data.items, columns); config.itemId = 'safeline-attack-grid'; config.region = 'center';
                config.tbar = null;
                config.listeners = { celldblclick: function (view, td, index, record) { cell(table, index, record, events, context, source); },
                    afterlayout: scheduleFit, resize: scheduleFit, columnresize: scheduleFit, columnhide: scheduleFit, columnshow: scheduleFit };
                config.bbar = [{ text: '上一頁', disabled: state.logPage <= 1, handler: function () { state.logPage--; render(context); } },
                    { xtype: 'tbtext', text: enc('第 ' + state.logPage + ' 頁 · 共 ' + data.total + ' 筆') },
                    { text: '下一頁', disabled: state.logPage * size >= data.total, handler: function () { state.logPage++; render(context); } }, '->',
                    Object.assign(combo('page_size', '', state.logPageSize || 0, [[0, '自動（' + size + ' 筆／頁）'], [20, '20 筆／頁'], [50, '50 筆／頁'], [100, '100 筆／頁']]), { width: 160,
                        listeners: { select: function (f) { state.logPageSize = Number(f.getValue()) || 0; state.logPage = 1; render(context); } } })];
                const tools = [];
                if (!context) tools.push({ text: '攻擊事件', enableToggle: true, pressed: events, handler: function () { state.logMode = 'events'; state.logPage = 1; state.logFilters = {}; state.logRelated = false; render(context); } },
                    { text: '攻擊日誌', enableToggle: true, pressed: !events, handler: function () { state.logMode = 'records'; state.logPage = 1; state.logFilters = {}; state.logRelated = false; render(context); } });
                if (!policy) tools.push(Object.assign(combo('source', '', source, [['general', '一般檢測'], ['rule', '自訂規則']]), { width: 125, listeners: { select: function (f) { state.logSource = f.getValue(); state.logPage = 1; render(context); } } }));
                tools.push('->', { text: '篩選／刷新', iconCls: 'fa fa-refresh', handler: apply }, { text: '清除篩選', handler: function () { state.logFilters = {}; state.logHours = 0; state.logPage = 1; state.logRelated = false; render(context); } });
                ctx.show({ xtype: 'panel', itemId: 'safeline-attacks', border: false, layout: 'border',
                    items: [{ xtype: 'panel', region: 'north', border: false, layout: 'anchor', tbar: { enableOverflow: true, items: tools },
                        items: [filterForm(events, filters, apply)] }, config],
                    bbar: [{ xtype: 'tbtext', itemId: 'safeline-attack-status', text: enc(state.logRelated ? '已按事件來源、Host、端口、起訖時間及動作篩選' : events ? '雙擊來源 IP 加入 IP 組；雙擊攻擊次數查看日誌' : '雙擊來源 IP 加入 IP 組；雙擊其他欄位查看詳情') }],
                    listeners: { destroy: function () {
                        if (state.logTimer) window.clearTimeout(state.logTimer);
                        if (state.logFitTimer) window.clearTimeout(state.logFitTimer);
                        state.logTimer = null; state.logFitTimer = null;
                    } } });
                panel = state.page.down('#safeline-attacks'); table = panel.down('#safeline-attack-grid');
                function arm() {
                    if (!state.logAutoRefresh || !alive(panel)) return;
                    state.logTimer = window.setTimeout(function () {
                        state.logTimer = null;
                        if (!alive(table) || !ctx.user() || serial !== state.logSerial) return;
                        if (state.editor || window.document.hidden || panel.down('#safeline-attack-filters').getForm().isDirty()) { arm(); return; }
                        render(context);
                    }, state.logAutoRefresh * 1000);
                }
                // Field changes are intentional user edits; establish the auto-refresh baseline now.
                panel.down('#safeline-attack-filters').getForm().trackResetOnLoad = true;
                scheduleFit();
                arm();
            }, function (code) { if (serial === state.logSerial) ctx.note(ctx.message(code)); }, true);
        }
        return { render: render, stop: stop };
    };
}());
