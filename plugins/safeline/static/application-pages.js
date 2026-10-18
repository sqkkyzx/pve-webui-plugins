(function () {
    // Loaded only with the SafeLine plugin; core supplies the authenticated API.
    window.__pveSafeLineApplicationPages = function (ctx) {
        const Ext = window.Ext, state = ctx.state, enc = ctx.enc, alive = ctx.alive;
        const names = { basic: '基本資訊', forwarding: '轉發規則', advanced: '高級配置', routes: '應用路由', tamper: '網頁防篡改',
            cc: 'CC 防護', bot: 'BOT 防護', auth: '身份認證', attack: '攻擊防護', statistics: '資料統計', collection: '採集設定', nginx: '自訂 NGINX 配置',
            'log-access': '訪問日誌', 'log-error': '錯誤日誌' };
        const contentTypes = ['text/html', 'text/css', 'text/xml', 'text/plain', 'image/png', 'image/gif', 'image/svg+xml', 'image/jpeg', 'image/x-icon',
            'application/zip', 'application/javascript', 'application/pdf', 'application/json', 'application/msword', 'application/octet-stream'];
        function path(id, section) { return '/applications/' + encodeURIComponent(id) + '/' + section; }
        function copy(value) { return JSON.parse(JSON.stringify(value)); }
        function different(a, b) { return JSON.stringify(a) !== JSON.stringify(b); }
        function lines(value) { return String(value || '').split(/\r?\n/).map(function (v) { return v.trim(); }).filter(Boolean); }
        function display(v) { return Array.isArray(v) ? v.join(', ') : v == null ? '—' : typeof v === 'object' ? JSON.stringify(v) : String(v); }
        function valueCell(v, m, r) { return typeof v === 'boolean' ? ctx.switchMark(v, m, r) : enc(display(v)); }
        function stamp(v) { return ctx.date(v, typeof v === 'number' && v > 100000000000); }
        function hint(value) { return { xtype: 'displayfield', value: enc(value), fieldStyle: 'opacity:.75;font-size:12px' }; }
        function optionGrid(rows, title, edit) {
            return { xtype: 'grid', title: title, border: false, flex: 1, minHeight: 0,
                store: { fields: ['id', 'name', 'value', 'section'], data: rows },
                columns: [{ text: '項目', dataIndex: 'name', width: 230, renderer: enc }, { text: '值', dataIndex: 'value', flex: 1, renderer: valueCell }],
                tbar: edit ? [{ text: '編輯', iconCls: 'fa fa-pencil', handler: function (button) { const table = button.up('grid'), row = table.getSelectionModel().getSelection()[0]; if (row) edit(row.data); } }] : [],
                listeners: { itemdblclick: function (view, record) { if (edit) edit(record.data); } }, viewConfig: { stripeRows: true, enableTextSelection: true } };
        }
        function frame(config, resource) {
            const r = resource || ctx.selected();
            ctx.show({ xtype: 'panel', itemId: 'safeline-app-page', border: false, layout: 'border', items: [
                { xtype: 'panel', region: 'north', height: 72, border: false,
                    tbar: [{ xtype: 'tbtext', text: enc((r.comment || r.name) + ' · #' + r.id) },
                        { text: r.status && r.status.label || '運作模式', iconCls: 'fa fa-play-circle-o', disabled: !!r.stale, handler: function () { ctx.editResource('mode'); } }, '->',
                        { xtype: 'tbtext', text: '今日請求：' + enc(ctx.amount(r.req_value)) + '　今日攔截：' + enc(ctx.amount(r.denied_value)) }],
                    bodyPadding: '5 10', html: enc((r.domains || []).join(', ')) }, Object.assign({ region: 'center', border: false }, config)] });
            return state.page.down('#safeline-app-page');
        }
        function selectedRows(table) { return table.getSelectionModel().getSelection().map(function (r) { return r.data; }); }
        function fieldsOf(table) { const result = []; table.getPlugins().forEach(function (p) { if (typeof p.completeEdit === 'function') p.completeEdit(); }); table.getStore().each(function (r) { result.push(copy(r.data)); }); return result; }
        function readPatterns(table) {
            const grouped = new Map();
            fieldsOf(table).forEach(function (r) {
                const group = Number(r.group); if (!Number.isInteger(group) || group < 1 || group > 64) throw new Error('條件組需為 1–64 的整數');
                if (!grouped.has(group)) grouped.set(group, []);
                const unchanged = r.original && r.k === r.original.k && r.op === r.original.op && r.sub_k === (r.original.sub_k || '') && r.values === (r.original.v || []).join('\n');
                grouped.get(group).push(unchanged ? r.original : { k: r.k, op: r.op, sub_k: r.sub_k || '', v: r.values === '' ? [] : String(r.values || '').split(/\r?\n/) });
            });
            return Array.from(grouped.keys()).sort(function (a, b) { return a-b; }).map(function (key) { return grouped.get(key); });
        }
        function ccRule(table, record, data, owner) {
            if (!record || alive(owner._ruleEditor)) return;
            const original = record.data, locked = data.locked.includes('conditions:' + original.id);
            let child;
            const inputs = [
                Object.assign(ctx.combo('type', '類型', original.type, [['req', '高頻訪問'], ['attack', '高頻攻擊'], ['error', '高頻錯誤']]), { disabled: !!original.built_in }),
                { xtype: 'textfield', name: 'name', fieldLabel: '名稱', value: original.name, allowBlank: !!original.built_in, maxLength: 256,
                    emptyText: ({ req: '基礎訪問限制', attack: '基礎攻擊限制', error: '基礎錯誤限制' })[original.type] },
                { xtype: 'checkbox', name: 'enabled', fieldLabel: '啟用', checked: !!original.enabled },
                { xtype: 'numberfield', name: 'period', fieldLabel: '統計時段（秒）', value: original.period, minValue: 1, maxValue: 86400, allowDecimals: false, allowBlank: false },
                { xtype: 'numberfield', name: 'count', fieldLabel: '次數門檻', value: original.count, minValue: 1, maxValue: 1000000000, allowDecimals: false, allowBlank: false },
                { xtype: 'numberfield', name: 'block_min', fieldLabel: '限制時長（分鐘）', value: original.block_min, minValue: 1, maxValue: 86400, allowDecimals: false, allowBlank: false },
                ctx.combo('action', '處理方式', original.action, [['challenge_v1', '人機驗證'], ['block', '攔截']].concat(data.capabilities.premium ? [['challenge_v2', '滑動驗證']] : original.action === 'challenge_v2' ? [['challenge_v2', '滑動驗證（現有設定）']] : [])),
                { xtype: 'numberfield', name: 'priority', fieldLabel: '優先順序', value: original.priority || 0, minValue: 0, maxValue: 100000, allowDecimals: false },
                { xtype: 'textfield', name: 'custom_status_codes', fieldLabel: '自訂狀態碼', value: (original.custom_status_codes || []).join(', '), emptyText: '例如 403, 404；留空保留預設', maxLength: 1024 }];
            inputs.forEach(function (field) { field.itemId = 'safeline-cc-rule-' + field.name; });
            const conditions = (original.conditions || []).map(function (r) { return { strategy: r.strategy, field: r.condition.field, operator: r.condition.operator, values: r.condition.value.join('\n'), original: r }; });
            inputs.push({ xtype: 'grid', itemId: 'safeline-cc-conditions', height: 175, disabled: locked, store: { fields: ['strategy', 'field', 'operator', 'values', 'original'], data: conditions },
                plugins: [{ ptype: 'cellediting', clicksToEdit: 1 }], columns: [
                    { text: '匹配項', dataIndex: 'strategy', width: 125, renderer: enc, editor: Object.assign(ctx.combo('', '', '', [['url_path', '路徑'], ['status_code', '狀態碼'], ['method', 'HTTP 方法'], ['src_ip', '來源 IP'], ['req_header', '請求標頭']]), { editable: true }) },
                    { text: '欄位', dataIndex: 'field', width: 110, renderer: enc, editor: { xtype: 'textfield' } },
                    { text: '算子', dataIndex: 'operator', width: 100, renderer: enc, editor: Object.assign(ctx.combo('', '', '', [['eq', '等於'], ['ne', '不等於'], ['in', '屬於'], ['not_in', '不屬於'], ['contains', '包含'], ['prefix', '前綴'], ['regex', '正則']]), { editable: true }) },
                    { text: '值（每行一項）', dataIndex: 'values', flex: 1, renderer: enc, editor: { xtype: 'textareafield' } }],
                tbar: [{ text: '新增條件', handler: function (b) { b.up('grid').getStore().add({ strategy: 'url_path', field: '', operator: 'eq', values: '' }); } },
                    { text: '移除所選條件', handler: function (b) { const g = b.up('grid'); g.getStore().remove(g.getSelectionModel().getSelection()); } }] });
            if (locked) inputs.push(hint('現有條件含敏感內容，保持原設定；仍可修改門檻。'));
            child = Ext.create('Ext.window.Window', { title: '編輯頻率限制', width: 760, maxHeight: Math.min(750, (window.innerHeight || 774) - 24), modal: true, constrain: true, layout: 'fit',
                items: [{ xtype: 'form', border: false, bodyPadding: 14, scrollable: true, defaults: { anchor: '100%', labelWidth: 145 }, items: inputs }],
                buttons: [{ text: '套用至草稿', handler: function () {
                    if (!child.down('form').getForm().isValid()) return;
                    const row = Object.assign({}, original);
                    inputs.filter(function (f) { return f.name; }).forEach(function (f) { row[f.name] = child.down('#safeline-cc-rule-' + f.name).getValue(); });
                    row.custom_status_codes = String(row.custom_status_codes || '').split(/[\s,]+/).filter(Boolean);
                    if (!locked) row.conditions = fieldsOf(child.down('#safeline-cc-conditions')).map(function (r) {
                        return r.original && r.strategy === r.original.strategy && r.field === r.original.condition.field && r.operator === r.original.condition.operator && r.values === r.original.condition.value.join('\n') ? r.original :
                            { strategy: r.strategy, condition: { field: r.field || '', operator: r.operator, value: String(r.values || '').split(/\r?\n/) } };
                    });
                    record.set(row); child.destroy();
                } }, { text: '取消', handler: function () { child.close(); } }], listeners: { destroy: function () { if (owner._ruleEditor === child) owner._ruleEditor = null; } } });
            owner._ruleEditor = child; child.show();
        }
        function open(section, operation) {
            const selected = ctx.selected();
            if (!selected || selected.stale || alive(state.editor) || !ctx.user() || !state.connection.csrf_token) return;
            const id = selected.id, generation = state.generation, who = ctx.user(), endpoint = path(id, section);
            let dialog, loaded, reader, pending = false, operationId = ctx.uuid(), canSave = true;
            const booleanChoices = new Set();
            function input(key) { return dialog.down('#safeline-app-input-' + key.replace(/[._]/g, '-')); }
            function value(key) {
                const result = input(key).getValue();
                if (booleanChoices.has(key)) return result === true || result === 'true';
                if (['auth_source_ids', 'resource_ids'].includes(key)) return (result || []).map(Number);
                return loaded && typeof loaded.values[key] === 'number' ? Number(result) : result;
            }
            function add(key, config) { return Object.assign({ itemId: 'safeline-app-input-' + key.replace(/[._]/g, '-'), name: key }, config); }
            function check(key, label, checked, disabled) { return add(key, { xtype: 'checkbox', fieldLabel: label, checked: !!checked, disabled: !!disabled }); }
            function number(key, label, initial, min, max, disabled) { return add(key, { xtype: 'numberfield', fieldLabel: label, value: initial, minValue: min, maxValue: max, allowBlank: false, allowDecimals: false, disabled: !!disabled }); }
            function text(key, label, initial, extra) { return add(key, Object.assign({ xtype: 'textfield', fieldLabel: label, value: initial || '', maxLength: 4096 }, extra)); }
            function choice(key, label, initial, options, extra) {
                if (typeof initial === 'boolean') { booleanChoices.add(key); initial = String(initial); options = options.map(function (option) { return [String(option[0]), option[1]]; }); }
                return add(key, Object.assign(ctx.combo(key, label, initial, options), extra));
            }
            function tags(key, label, initial, options, extra) { return choice(key, label, initial, options, Object.assign({ xtype: 'tagfield', multiSelect: true, forceSelection: true, filterPickList: true }, extra)); }
            function notice(message) { if (alive(dialog)) dialog.down('#safeline-app-edit-status').update(enc(message)); }
            function changed(fields) { const patch = {}; fields.forEach(function (key) { const next = value(key); if (different(next, loaded.values[key])) patch[key] = next; }); return patch; }
            function buildForm(data) {
                const v = data.values, premium = !!data.capabilities.premium, paid = !!data.capabilities.business, locked = data.locked;
                let fields = [];
                if (section === 'nginx') {
                    fields = [text('custom_location', 'NGINX 指令', (v.custom_location || []).join('\n'), { xtype: 'textareafield', height: 330, maxLength: 65536, disabled: locked.includes('custom_location'), fieldStyle: 'font-family:monospace' })];
                    canSave = !locked.includes('custom_location'); reader = function () { const text = String(value('custom_location') || ''), next = text ? text.split(/\r?\n/) : []; return different(next, v.custom_location || []) ? { custom_location: next } : {}; };
                } else if (section === 'collection') {
                    fields = [text('prefixes', '排除路徑前綴', (v.prefixes || []).join('\n'), { xtype: 'textareafield', height: 110, disabled: locked.includes('prefixes') }),
                        tags('content_types', '排除內容類型', v.content_types || [], contentTypes.map(function (t) { return [t, t]; }), { disabled: locked.includes('content_types'), editable: true, forceSelection: false, createNewOnEnter: true, createNewOnBlur: true }),
                        hint('每行一個路徑前綴。此設定只影響路由清單採集。')];
                    reader = function () { const next = { prefixes: lines(value('prefixes')), content_types: value('content_types') || [] }, patch = {}; Object.keys(next).forEach(function (k) { if (!locked.includes(k) && different(next[k], v[k])) patch[k] = next[k]; }); return patch; };
                } else if (section === 'statistics') {
                    fields = [check('stat_enabled', '流量分析', v.stat_enabled, locked.includes('stat_enabled')), check('sp_enabled', '安全態勢', v.sp_enabled, locked.includes('sp_enabled')),
                        hint('應用流量分析需專業版或商業版；應用安全態勢需商業版。')];
                    canSave = !locked.includes('stat_enabled') || !locked.includes('sp_enabled'); reader = function () { return changed(['stat_enabled', 'sp_enabled'].filter(function (k) { return !locked.includes(k); })); };
                } else if (section.startsWith('log-')) {
                    fields = [choice('limit', '日誌大小上限（MB）', v.limit, [[0, '關閉'], [128, '128 MB'], [512, '512 MB'], [1024, '1024 MB'], [2048, '2048 MB']], { editable: true, allowBlank: false }),
                        { xtype: 'grid', itemId: 'safeline-app-log-files', height: 260, store: { fields: ['filename', 'size'], data: v.files || [] },
                            columns: [{ text: '日誌檔案', dataIndex: 'filename', flex: 1, renderer: enc }, { text: '大小（位元組）', dataIndex: 'size', width: 140, renderer: enc }],
                            tbar: [{ text: '預覽', handler: function (button) { const r = button.up('grid').getSelectionModel().getSelection()[0]; if (r) previewLog(dialog, id, section.slice(4), r.get('filename')); } }],
                            listeners: { itemdblclick: function (view, record) { previewLog(dialog, id, section.slice(4), record.get('filename')); } }, viewConfig: { emptyText: '目前沒有日誌檔案', deferEmptyText: false } }, hint('預覽最多 200 行；認證與 Cookie 內容會遮罩。')];
                    reader = function () { const next = Number(value('limit')); return next !== v.limit ? { limit: next } : {}; };
                } else if (section === 'attack') {
                    const rows = Object.keys(data.capabilities.module_names || {}).map(function (key) { return { id: key, name: data.capabilities.module_names[key], mode: v.modules[key] || null }; });
                    fields = [choice('use_global', '採用來源', v.use_global, [[true, '跟隨全域配置'], [false, '使用自訂配置']], { listeners: { select: function () { input('modules').setDisabled(!!value('use_global')); } } }),
                        add('modules', { xtype: 'grid', height: 385, disabled: v.use_global, store: { fields: ['id', 'name', 'mode'], data: rows }, plugins: [{ ptype: 'cellediting', clicksToEdit: 1 }],
                            columns: [{ text: '檢測模組', dataIndex: 'name', flex: 1, renderer: enc }, { text: '防護模式', dataIndex: 'mode', width: 175,
                                renderer: function (mode) { return enc(({ disable: '禁用', dry_run: '僅觀察', default: '平衡防護', strict: '高強度防護' })[mode] || mode || '未提供'); },
                                editor: ctx.combo('', '', '', [['disable', '禁用'], ['dry_run', '僅觀察'], ['default', '平衡防護'], ['strict', '高強度防護']]) }],
                            tbar: [['disable', '全部禁用'], ['dry_run', '全部觀察'], ['default', '全部平衡'], ['strict', '全部高強度']].map(function (item) { return { text: item[1], handler: function (button) { button.up('grid').getStore().each(function (r) { r.set('mode', item[0]); }); } }; }) })];
                    reader = function () { const patch = {}, inherit = !!value('use_global'); if (inherit !== v.use_global) patch.use_global = inherit;
                        if (!inherit) { const modules = {}; fieldsOf(input('modules')).forEach(function (r) { if (r.mode != null && r.mode !== v.modules[r.id]) modules[r.id] = r.mode; }); if (Object.keys(modules).length) patch.modules = modules; } return patch; };
                } else if (section === 'bot' || section === 'auth') {
                    fields = [check('enable', section === 'bot' ? '啟用人機驗證' : '啟用身份認證', v.enable)];
                    if (section === 'bot') {
                        fields.push(choice('level', '驗證方式', v.level, [[1, '無感驗證'], [2, '滑動驗證']], { disabled: !premium }), number('expire', '驗證有效期（秒）', v.expire, 1, 31536000, !premium), check('replay', '請求防重放', v.replay, !premium));
                        [['html_encryption', 'HTML 動態加密'], ['js_encryption', 'JS 動態混淆'], ['img_encryption', '圖片動態水印'], ['html_fast_decryption', '快速解密']].forEach(function (item) { fields.push(check('chaos.' + item[0], item[1], v.chaos[item[0]], item[0] === 'html_fast_decryption' && !premium)); });
                        fields.push(text('chaos.js_path', 'JS 路徑（每行一項）', (v.chaos.js_path || []).join('\n'), { xtype: 'textareafield', height: 70, disabled: locked.includes('chaos.js_path') }), text('chaos.img_text', '水印文字', v.chaos.img_text, { disabled: !premium || locked.includes('chaos.img_text') }));
                        if (!premium) fields.push(hint('滑動驗證、驗證有效期、防重放及水印文字需專業版或商業版。'));
                    } else {
                        function authMode() { const unified = value('auth_mode') === 'portal'; input('auth_source_ids').setDisabled(unified); input('tfa_enabled').setDisabled(unified); input('portal_redirect').setVisible(unified);
                            if (unified) { input('auth_source_ids').setValue((data.portal.auth_sources || []).map(function (s) { return s.id; })); input('tfa_enabled').setValue(!!data.portal.tfa_enabled); } }
                        fields.push(choice('auth_mode', '認證方式', v.portal_redirect ? 'portal' : 'own', [['own', '簡易認證'], ['portal', '統一認證']], { listeners: { select: authMode } }),
                            tags('auth_source_ids', '登入方式', v.portal_redirect ? (data.portal.auth_sources || []).map(function (s) { return s.id; }) : v.auth_source_ids, (data.sources || []).map(function (s) { return [s.id, s.name]; }), { disabled: !!v.portal_redirect }),
                            check('tfa_enabled', '強制 TOTP 二步驗證', v.portal_redirect ? data.portal.tfa_enabled : v.tfa_enabled, !!v.portal_redirect), text('auth_callback', '認證回調地址', v.auth_callback),
                            text('portal_redirect', '應用跳轉地址', v.portal_redirect, { hidden: !v.portal_redirect, emptyText: 'https://app.example' }),
                            number('auth_challenge_timeout', '登入有效期', v.auth_challenge_timeout, 1, 36500), choice('auth_challenge_timeout_unit', '有效期單位', v.auth_challenge_timeout_unit, [['day', '天'], ['hour', '小時'], ['minute', '分鐘']]),
                            choice('review', '訪問授權', v.review, [[0, '需審批授權訪問'], [1, '認證後直接訪問']]));
                        if (!data.portal.enable) fields.push(hint('統一認證入口尚未啟用；目前可使用簡易認證。'));
                    }
                    fields.push(choice('negate', '條件方式', v.negate, [[false, '符合條件時驗證'], [true, '符合條件時跳過驗證']]), hint('沒有匹配條件時套用於所有請求。'));
                    if (!locked.includes('pattern')) {
                        const conditions = ctx.conditionGrid(v.pattern || []); conditions.bbar = [{ xtype: 'tbtext', text: '同組條件全部符合；不同條件組任一組符合。只套用於目前應用。' }]; fields.push(conditions);
                    }
                    else fields.push(hint('現有匹配條件含敏感內容，保持原設定。'));
                    reader = function () {
                        const keys = section === 'bot' ? ['enable', 'negate'].concat(premium ? ['level', 'expire', 'replay'] : []) : ['enable', 'negate', 'auth_source_ids', 'review', 'auth_callback', 'tfa_enabled', 'auth_challenge_timeout', 'auth_challenge_timeout_unit'];
                        const patch = changed(keys);
                        if (!locked.includes('pattern')) { const next = readPatterns(dialog.down('#safeline-patterns')); if (different(next, v.pattern)) patch.pattern = next; }
                        if (section === 'auth') { const redirect = value('auth_mode') === 'portal' ? String(value('portal_redirect') || '').trim() : ''; if (redirect !== v.portal_redirect) patch.portal_redirect = redirect; }
                        else { const chaos = {}; ['html_encryption', 'js_encryption', 'img_encryption'].concat(premium ? ['html_fast_decryption'] : []).forEach(function (k) { const next = !!value('chaos.' + k); if (next !== v.chaos[k]) chaos[k] = next; });
                            const paths = lines(value('chaos.js_path')); if (!locked.includes('chaos.js_path') && different(paths, v.chaos.js_path)) chaos.js_path = paths;
                            if (premium && !locked.includes('chaos.img_text') && value('chaos.img_text') !== v.chaos.img_text) chaos.img_text = value('chaos.img_text'); if (Object.keys(chaos).length) patch.chaos = chaos; }
                        return patch;
                    };
                } else if (section === 'cc') {
                    let customRules = copy(v.rules), inheritedRules = !!v.use_global;
                    function ccSource() {
                        const inherit = !!value('use_global'), table = input('rules');
                        if (inherit && !inheritedRules) customRules = fieldsOf(table);
                        table.getStore().loadData(copy(inherit ? data.global_rules || v.rules : customRules)); table.setDisabled(inherit); inheritedRules = inherit;
                    }
                    fields = [check('waiting.is_enabled', '啟用等候室', v.waiting.is_enabled), number('waiting.max_concurrent', '最大同時在線人數', v.waiting.max_concurrent, 1, 5000, !premium),
                        number('waiting.session_timeout', '會話有效期（分鐘）', v.waiting.session_timeout, 1, 1440, !premium),
                        choice('use_global', '頻率限制來源', v.use_global, [[true, '跟隨全域配置'], [false, '使用自訂配置']], { listeners: { select: ccSource } }),
                        add('rules', { xtype: 'grid', height: 240, disabled: v.use_global, store: { fields: ['id', 'name', 'type', 'enabled', 'period', 'count', 'block_min', 'action', 'priority', 'conditions', 'custom_status_codes', 'built_in', 'use_global'], data: copy(v.use_global ? data.global_rules || v.rules : v.rules) },
                            columns: [{ text: '規則', dataIndex: 'name', flex: 1, renderer: function (name, meta, record) { return enc(name || ({ req: '基礎訪問限制', attack: '基礎攻擊限制', error: '基礎錯誤限制' })[record.get('type')]); } }, { text: '啟用', dataIndex: 'enabled', width: 60, renderer: ctx.switchMark },
                                { text: '時段（秒）', dataIndex: 'period', width: 95, renderer: enc }, { text: '門檻', dataIndex: 'count', width: 90, renderer: enc }, { text: '時長（分鐘）', dataIndex: 'block_min', width: 110, renderer: enc }],
                            tbar: [{ text: '編輯', handler: function (button) { const table = button.up('grid'); ccRule(table, table.getSelectionModel().getSelection()[0], data, dialog); } },
                                { text: '新增規則', disabled: !premium, handler: function (button) { button.up('grid').getStore().add({ id: 'new:' + ctx.uuid(), name: '自訂規則', type: 'req', priority: 1, enabled: false, period: 10, count: 500, block_min: 30, action: 'challenge_v1', conditions: [], custom_status_codes: [], built_in: false, use_global: false }); } },
                                { text: '移除所選規則', disabled: !premium, handler: function (button) { const table = button.up('grid'); table.getStore().remove(table.getSelectionModel().getSelection().filter(function (r) { return !r.get('built_in'); })); } }],
                            listeners: { itemdblclick: function (view, record) { ccRule(input('rules'), record, data, dialog); } } }), hint('雙擊規則修改門檻、處理方式及匹配條件。等候室進階參數與新增自訂規則需專業版或商業版。')];
                    reader = function () { const patch = {}, waiting = {}, inherit = !!value('use_global'); ['is_enabled'].concat(premium ? ['max_concurrent', 'session_timeout'] : []).forEach(function (key) { const next = value('waiting.' + key); if (different(next, v.waiting[key])) waiting[key] = next; });
                        if (Object.keys(waiting).length) patch.waiting = waiting;
                        if (inherit !== v.use_global) patch.use_global = inherit;
                        if (!inherit) { const rules = fieldsOf(input('rules')).map(function (r) { return Object.assign({}, r, { id: String(r.id).startsWith('new:') ? 0 : r.id, use_global: false }); }); if (different(rules, v.rules)) patch.rules = rules; } return patch; };
                } else if (section === 'forwarding') {
                    const action = operation.action, row = (v.rules || []).find(function (r) { return r.id === operation.id; });
                    if (action !== 'create' && !row) { canSave = false; fields = [hint('規則已變更，請關閉並重新載入。')]; reader = function () { return {}; }; }
                    else if (action === 'delete') { fields = [hint('刪除轉發規則：' + row.path_prefix)]; reader = function () { return { action: action, id: row.id }; }; }
                    else if (action === 'switch') { fields = [check('is_enabled', '啟用轉發規則', row.is_enabled)]; reader = function () { return value('is_enabled') === row.is_enabled ? {} : { action: action, id: row.id, values: { is_enabled: !!value('is_enabled') } }; }; }
                    else {
                        const original = row || { path_prefix: '', upstreams: [], comment: '' };
                        fields = [text('path_prefix', '路徑前綴', original.path_prefix, { allowBlank: false, emptyText: '/api' }), text('upstreams', '上游伺服器', original.upstreams.join('\n'), { xtype: 'textareafield', height: 100, allowBlank: false }), text('comment', '備註', original.comment)];
                        canSave = action !== 'create' || v.rules.length < data.capabilities.forwarding_limit;
                        reader = function () { const next = { path_prefix: String(value('path_prefix') || '').trim(), upstreams: lines(value('upstreams')), comment: value('comment') || '' }, patch = {};
                            Object.keys(next).forEach(function (k) { if (action === 'create' || different(next[k], original[k])) patch[k] = next[k]; }); return Object.keys(patch).length ? { action: action, ...(row ? { id: row.id } : {}), values: patch } : {}; };
                    }
                } else if (section === 'routes') {
                    const ids = operation.ids.filter(function (key) { return (v.resources || []).some(function (r) { return r.id === key; }); });
                    canSave = ids.length > 0 && ids.length === operation.ids.length;
                    fields = [hint('刪除 ' + ids.length + ' 筆已採集路由記錄；應用上的實際檔案不受影響。')]; reader = function () { return { action: 'delete', ids: ids }; };
                } else if (section === 'tamper') {
                    canSave = paid;
                    if (operation.action === 'create') {
                        fields = [tags('resource_ids', '選擇應用資源', [], (v.resources || []).map(function (r) { return [r.id, r.method + ' ' + r.path]; }), { allowBlank: false, disabled: !paid })];
                        reader = function () { return { action: 'create', resource_ids: value('resource_ids') || [] }; };
                    } else {
                        const row = (v.data || []).find(function (r) { return r.id === operation.id; });
                        if (operation.action !== 'refresh' && !row) canSave = false;
                        fields = [hint(operation.action === 'refresh' ? '重新檢查所有防篡改資源。' : (operation.action === 'accept' ? '將目前內容設為新的防篡改基準：' : '移除防篡改資源：') + (row && row.path || '項目已不存在'))];
                        reader = function () { return { action: operation.action, ...(operation.id ? { id: operation.id } : {}) }; };
                    }
                    if (!paid) fields.push(hint('此功能需要 SafeLine 商業版。'));
                }
                const container = dialog.down('#safeline-app-edit-fields'); container.removeAll(true); fields.forEach(function (f) { container.add(f); });
                dialog.down('#safeline-app-save').setDisabled(!canSave || !operationId);
            }
            function save() {
                if (!loaded || !reader || pending || !canSave || !operationId || !alive(dialog) || !ctx.current(generation, who) || dialog.down('#safeline-app-save').disabled) return;
                if (!dialog.down('form').getForm().isValid()) return;
                let changes; try { changes = reader(); } catch (e) { notice(e.message || '請檢查尚未完成的編輯。'); return; }
                if (!Object.keys(changes).length) { notice('設定未變更'); return; }
                pending = true; dialog.down('#safeline-app-save').setDisabled(true); dialog.down('#safeline-app-close').setDisabled(true); notice('正在儲存並回讀核對…');
                const form = dialog.down('form'); if (typeof form.setLoading === 'function') form.setLoading('正在儲存…');
                function finish() { pending = false; if (alive(dialog)) { dialog.down('#safeline-app-close').setDisabled(false); if (typeof form.setLoading === 'function') form.setLoading(false); } }
                ctx.request(endpoint, { method: 'PUT', headers: { 'X-PVE-Plugins-CSRF': state.connection.csrf_token }, jsonData: { expected_revision: loaded.connection_revision, fingerprint: loaded.fingerprint, operation_id: operationId, changes: changes } }, function (result) {
                    if (!alive(dialog)) return; finish();
                    if (result.outcome === 'confirmed') { dialog.destroy(); ctx.status('已儲存並確認設定'); ctx.refresh(); return; }
                    notice(ctx.message(result.outcome === 'failed' ? result.code : 'safeline_write_unknown'));
                    if (result.outcome === 'failed') { operationId = ctx.uuid(); dialog.down('#safeline-app-save').setDisabled(!operationId); }
                }, function (code) { if (!alive(dialog)) return; finish(); notice(ctx.message(code.startsWith('invalid_') || ['safeline_resource_changed', 'safeline_feature_unavailable', 'safeline_business_required', 'safeline_portal_disabled', 'safeline_forwarding_limit', 'safeline_edit_unsupported'].includes(code) ? code : 'safeline_write_unknown')); if (code.startsWith('invalid_')) dialog.down('#safeline-app-save').setDisabled(false); }, false);
            }
            dialog = Ext.create('Ext.window.Window', { title: (operation && operation.action === 'delete' ? '刪除：' : '配置：') + names[section], width: ['cc', 'bot', 'auth', 'attack'].includes(section) ? 880 : 720,
                maxHeight: Math.min(820, (window.innerHeight || 844) - 24), modal: true, constrain: true, closeAction: 'destroy', layout: 'fit',
                items: [{ xtype: 'form', border: false, bodyPadding: 14, scrollable: true, items: [{ xtype: 'container', itemId: 'safeline-app-edit-fields', layout: 'anchor', defaults: { anchor: '100%', labelWidth: 185 } },
                    { xtype: 'component', itemId: 'safeline-app-edit-status', margin: '8 0 0', html: '正在讀取最新設定…' }] }],
                buttons: [{ text: operation && operation.action === 'delete' ? '刪除' : '儲存', itemId: 'safeline-app-save', disabled: true, handler: save }, { text: '關閉', itemId: 'safeline-app-close', handler: function () { dialog.close(); } }],
                listeners: { beforeclose: function () { return !pending; }, destroy: function () { if (state.editor === dialog) state.editor = null; if (alive(dialog._ruleEditor)) dialog._ruleEditor.destroy(); if (alive(dialog._logPreview)) dialog._logPreview.destroy(); } } });
            state.editor = dialog; dialog.show();
            ctx.request(endpoint, {}, function (data) { if (!alive(dialog)) return; loaded = data; buildForm(data); notice(operationId ? '儲存後會回讀核對。' : '目前瀏覽器無法產生操作編號。'); }, function (code) { notice(ctx.message(code)); }, false);
        }
        function previewLog(owner, id, kind, filename) {
            if (alive(owner._logPreview)) owner._logPreview.destroy();
            const child = Ext.create('Ext.window.Window', { title: filename + ' · 最多 200 行', width: 880, height: 500, modal: true, constrain: true, layout: 'fit',
                items: [{ xtype: 'panel', itemId: 'safeline-log-preview', scrollable: true, bodyPadding: 12, html: '正在讀取…' }], buttons: [{ text: '關閉', handler: function () { child.close(); } }] });
            owner._logPreview = child; child.show();
            ctx.request('/applications/' + id + '/logs/' + kind + '/preview?filename=' + encodeURIComponent(filename), {}, function (data) { if (alive(child)) child.down('#safeline-log-preview').update('<pre style="white-space:pre-wrap;overflow-wrap:anywhere">' + enc((data.lines || []).join('\n') || '目前沒有可讀的日誌行') + '</pre>' + (data.truncated ? '<p>已達預覽上限。</p>' : '')); }, function (code) { if (alive(child)) child.down('#safeline-log-preview').update(enc(ctx.message(code))); }, false);
        }
        function basic(data, resource) {
            const r = data.values.resource;
            const rows = [['name', '應用名稱', r.comment || r.name], ['domains', '應用域名', r.domains], ['ports', '監聽端口', (r.ports || []).map(ctx.portLabel)],
                ['type', '接入方式', r.access_type === 0 ? '代理到已有應用' : r.access_type === 1 ? '本地靜態站點' : '上游值：' + r.access_type], ['upstreams', '上游伺服器', r.upstreams],
                ['health', '回源健康檢查', r.health_check], ['certificate', 'TLS 憑證 ID', r.cert_id], ['updated', '更新時間', ctx.date(r.updated_at)]];
            const controls = [['cc', 'CC 防護', r.cc_enabled], ['bot', 'BOT 防護', r.bot_enabled], ['auth', '身份認證', r.auth_enabled], ['attack', '攻擊防護', r.attack_enabled],
                ['traffic', '流量分析', r.stat_enabled, 'statistics'], ['security', '安全態勢', r.sp_enabled, 'statistics'],
                ['access', '訪問日誌', typeof r.access_log_limit === 'number' ? r.access_log_limit > 0 : null, 'log-access'], ['error', '錯誤日誌', typeof r.error_log_limit === 'number' ? r.error_log_limit > 0 : null, 'log-error']];
            const table = optionGrid(rows.map(function (row) { return { id: row[0], name: row[1], value: row[2] }; }), '基礎配置', function (row) {
                if (row.id === 'certificate' && r.cert_id) ctx.choose('certificates', String(r.cert_id)); else ctx.editResource('settings');
            });
            table.tbar = [{ text: '編輯基礎配置', iconCls: 'fa fa-pencil', handler: function () { ctx.editResource('settings'); } }];
            frame({ xtype: 'panel', itemId: 'safeline-app-basic', layout: { type: 'vbox', align: 'stretch' }, items: [table,
                optionGrid(controls.map(function (row) { return { id: row[0], name: row[1], value: row[2], section: row[3] || row[0] }; }), '防護、統計與日誌', function (row) { open(row.section); })] }, r);
        }
        function routingNodes(resources) {
            const root = { id: 'routes-root', expanded: true, children: [] }, folders = new Map([['', root]]);
            resources.forEach(function (r) {
                const parts = String(r.path || '/').split('?')[0].split('/').filter(Boolean); let parent = root, prefix = '';
                parts.slice(0, -1).forEach(function (part) { prefix += '/' + part; if (!folders.has(prefix)) { const folder = { id: 'folder:' + prefix, text: prefix, expanded: true, checked: false, children: [] }; parent.children.push(folder); folders.set(prefix, folder); } parent = folders.get(prefix); });
                parent.children.push(Object.assign({}, r, { id: 'resource:' + r.id, resource_id: r.id, text: r.path, leaf: true, checked: false, iconCls: 'fa fa-file-text-o' }));
            }); return root;
        }
        function list(section, data, resource) {
            const v = data.values; let config;
            if (section === 'forwarding') {
                config = ctx.grid(v.rules || [], [{ text: '啟用', dataIndex: 'is_enabled', width: 65, renderer: ctx.switchMark }, { text: '備註', dataIndex: 'comment', flex: 1 },
                    { text: '路徑前綴', dataIndex: 'path_prefix', flex: 1 }, { text: '上游伺服器', dataIndex: 'upstreams', flex: 2, renderer: function (value) { return enc((value || []).join(', ')); } }, { text: '更新時間', dataIndex: 'updated_at', width: 175, renderer: function (value) { return ctx.date(value); } }],
                    function (row) { open(section, { action: 'edit', id: row.id }); }, '僅轉發特定路徑前綴的請求，防護仍由此應用套用。');
                config.tbar = [{ text: '新增', iconCls: 'fa fa-plus', disabled: (v.rules || []).length >= data.capabilities.forwarding_limit, handler: function () { open(section, { action: 'create' }); } }].concat(['edit', 'switch', 'delete'].map(function (action) { return { text: ({ edit: '編輯', switch: '啟停', delete: '刪除' })[action], handler: function (button) { const row = selectedRows(button.up('grid'))[0]; if (row) open(section, { action: action, id: row.id }); } }; }),
                    [{ xtype: 'tbtext', text: '規則總數：' + (v.rules || []).length + ' / ' + data.capabilities.forwarding_limit }]);
            } else if (section === 'routes') {
                config = { xtype: 'treepanel', itemId: 'safeline-app-routes', rootVisible: false, useArrows: true, border: false,
                    store: { type: 'tree', fields: ['id', 'text', 'resource_id', 'path', 'method', 'content_type', 'req_today', 'updated_at', 'checked'], root: routingNodes(v.resources || []) },
                    columns: [{ xtype: 'treecolumn', text: '路徑', dataIndex: 'text', flex: 2, minWidth: 250, renderer: enc }, { text: '方法', dataIndex: 'method', width: 85, renderer: enc },
                        { text: '資源屬性', dataIndex: 'content_type', flex: 1, renderer: enc }, { text: '24H 訪問量', dataIndex: 'req_today', width: 120, renderer: function (n) { return n == null ? '' : enc(ctx.amount(n)); } },
                        { text: '最近訪問時間', dataIndex: 'updated_at', width: 180, renderer: function (n) { return n ? ctx.date(n) : ''; } }],
                    tbar: [{ text: '採集設定', iconCls: 'fa fa-cog', handler: function () { open('collection'); } }, { text: '刪除所選記錄', iconCls: 'fa fa-trash', handler: function (button) {
                        const table = button.up('treepanel'), rows = typeof table.getChecked === 'function' ? table.getChecked().map(function (r) { return r.data; }) : selectedRows(table);
                        const ids = rows.filter(function (r) { return r.resource_id; }).map(function (r) { return r.resource_id; }); if (ids.length) open('routes', { action: 'delete', ids: ids });
                    } }, { xtype: 'tbtext', text: '資源總數：' + v.total }], bbar: [{ xtype: 'tbtext', text: '此清單用於資源盤點；刪除記錄不會移除應用檔案。' }],
                    listeners: { checkchange: function (record, checked) { function walk(node) { (node.childNodes || []).forEach(function (child) { child.set({ checked: checked }); walk(child); }); } walk(record); } } };
            } else {
                config = ctx.grid(v.data || [], [{ text: '路徑', dataIndex: 'path', flex: 2 }, { text: '已變更', dataIndex: 'changed', width: 100, renderer: ctx.switchMark }, { text: '資源快取時間', dataIndex: 'cache_time', width: 190, renderer: stamp }], null,
                    data.capabilities.business ? '上次檢查：' + stamp(v.refresh_time) + (v.refresh_state === 'running' ? '（正在檢查）' : '') : '此功能需要 SafeLine 商業版。');
                config.tbar = [{ text: '新增資源', disabled: !data.capabilities.business, handler: function () { open('tamper', { action: 'create' }); } },
                    { text: '重新檢查', disabled: !data.capabilities.business, handler: function () { open('tamper', { action: 'refresh' }); } }].concat(['accept', 'delete'].map(function (action) {
                    return { text: action === 'accept' ? '接受目前內容' : '移除資源', disabled: !data.capabilities.business, handler: function (button) { const row = selectedRows(button.up('grid'))[0]; if (row) open('tamper', { action: action, id: row.id }); } };
                }), [{ xtype: 'checkbox', boxLabel: '只看已變更', listeners: { change: function (field, checked) { const store = field.up('grid').getStore(); store.clearFilter(); if (checked) store.filterBy(function (r) { return r.get('changed') === true; }); } } }]);
            }
            frame(config, resource);
        }
        function render(resource) {
            const section = state.tab;
            if (section === 'advanced') { ctx.renderAdvanced(resource); return; }
            ctx.note('正在讀取應用設定…');
            const owner = state.page.down('#safeline-body').children ? state.page.down('#safeline-body').children[0] : state.page.down('#safeline-body').items.first();
            ctx.request(path(resource.id, section), {}, function (data) {
                if (!alive(owner) || state.selection.id !== resource.id || state.tab !== section) return;
                if (section === 'basic') basic(data, resource); else list(section, data, resource);
            }, function (code) { if (alive(owner)) ctx.note(ctx.message(code)); }, true);
        }
        return { render: render, edit: open, frame: frame };
    };
}());
