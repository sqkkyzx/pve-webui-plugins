(function () {
    window.__pveSafeLineSecurityPages = function (ctx) {
        const Ext = window.Ext, state = ctx.state, enc = ctx.enc, alive = ctx.alive;
        function copy(v) { return JSON.parse(JSON.stringify(v)); }
        function same(a, b) { return JSON.stringify(a) === JSON.stringify(b); }
        function choice(name, label, value, options) {
            return Object.assign(ctx.combo(name, label, value, options), { forceSelection: true,
                listConfig: { getInnerTpl: function () { return '{name:htmlEncode}'; } } });
        }
        function textCell(v, meta) { if (meta) meta.tdAttr = 'title="' + enc(v) + '"'; return enc(v); }
        function modal(title, width) {
            if (!ctx.user() || alive(state.editor)) return null;
            const who = ctx.user(), generation = state.generation;
            const f = { data: null, pending: false, submitted: false, change: null };
            f.dialog = Ext.create('Ext.window.Window', { title: title, width: Math.min(width || 900, (window.innerWidth || 1200) - 30),
                maxHeight: Math.min(850, (window.innerHeight || 900) - 30), modal: true, constrain: true, layout: 'fit',
                items: [{ xtype: 'panel', itemId: 'safeline-security-dialog-body', border: false, bodyPadding: 14, scrollable: true, layout: 'anchor',
                    items: [{ xtype: 'component', html: '正在讀取…' }] }],
                bbar: [{ xtype: 'tbtext', itemId: 'safeline-security-dialog-status', text: '', style: 'white-space:normal' }],
                buttons: [{ text: '儲存', itemId: 'safeline-security-dialog-save', disabled: true, handler: function () {
                    if (!f.data || f.pending || f.submitted || !ctx.current(generation, who) || !f.change) return;
                    const form = f.dialog.down('form'); if (form && !form.getForm().isValid()) return;
                    let changes; try { changes = f.change(); } catch (e) { f.notice(e.message || '請檢查匹配條件'); return; }
                    if (!changes || !Object.keys(changes).length) { f.dialog.close(); return; }
                    const operation = ctx.uuid(); if (!operation) { f.notice('此瀏覽器無法產生操作編號'); return; }
                    f.pending = true; f.submitted = true; f.save.setDisabled(true); f.cancel.setDisabled(true); f.notice('正在儲存並回讀確認…');
                    ctx.request(f.path, { method: 'POST', headers: { 'X-PVE-Plugins-CSRF': state.connection.csrf_token }, jsonData: {
                        expected_revision: f.data.connection_revision, fingerprint: f.data.fingerprint, operation_id: operation, changes: changes } }, function (result) {
                        f.pending = false;
                        if (!alive(f.dialog)) return;
                        f.cancel.setDisabled(false);
                        if (result.outcome === 'confirmed') { f.dialog.destroy(); ctx.loadInventory(); if (f.confirmed) f.confirmed(); }
                        else f.notice(ctx.message(result.code));
                    }, function (code) { f.pending = false; if (alive(f.dialog)) {
                        f.cancel.setDisabled(false);
                        const rejected = code.startsWith('invalid_') || ['safeline_feature_unavailable', 'safeline_edit_unsupported'].includes(code);
                        if (rejected) { f.submitted = false; f.save.setDisabled(false); }
                        f.notice(ctx.message(code) + (rejected ? '；請修正後儲存。' : '；請關閉視窗後核對實際結果。'));
                    } }, false);
                } }, { text: '取消', itemId: 'safeline-security-dialog-cancel', handler: function () { f.dialog.close(); } }],
                listeners: { beforeclose: function () { return !f.pending; }, destroy: function () { if (alive(f.child)) f.child.destroy(); if (state.editor === f.dialog) state.editor = null; } } });
            state.editor = f.dialog; f.dialog.show(); f.body = f.dialog.down('#safeline-security-dialog-body');
            f.save = f.dialog.down('#safeline-security-dialog-save'); f.cancel = f.dialog.down('#safeline-security-dialog-cancel');
            f.notice = function (v) { if (alive(f.dialog)) f.dialog.down('#safeline-security-dialog-status').setText(enc(v)); };
            f.read = function (path, build) {
                f.path = path;
                ctx.request(path, {}, function (data) {
                    if (!alive(f.dialog)) return;
                    if (!data.values || !Array.isArray(data.locked) || !data.fingerprint) { f.notice(ctx.message('safeline_unexpected_response')); return; }
                    f.data = data; f.body.removeAll(); build(data); f.notice('已讀取 · ' + ctx.date(data.updated_at));
                    if (typeof f.dialog.center === 'function') f.dialog.center();
                }, function (code) { f.notice(ctx.message(code)); }, false);
            };
            return f;
        }
        function form(fields) { return { xtype: 'form', border: false, defaults: { anchor: '100%', labelWidth: 125 }, items: fields }; }
        function check(name, label, value) { return { xtype: 'checkbox', name: name, fieldLabel: label, checked: !!value }; }
        function number(name, label, value, min, max) { return { xtype: 'numberfield', name: name, fieldLabel: label, value: value, minValue: min, maxValue: max, allowDecimals: false, allowBlank: false }; }
        function fieldValue(f, name) { return f.dialog.down('[name=' + name + ']').getValue(); }
        function belongs(r, action) { return r.action === action || r.action === 999 && r[action === 0 ? 'white_rule' : 'black_rule'] > 0; }
        function sortable(r) { return !r.builtin && !(r.cloud_id > 0) && [0, 1].includes(r.action); }
        function patternLabel(v) {
            if (!Array.isArray(v) || !v.length) return '—';
            if (!Array.isArray(v[0])) return '—';
            if (v.length > 1 || v[0].length > 1) return '複合條件';
            const c = v[0][0]; if (!c) return '—';
            const targets = { src_ip: '來源 IP', website: '防護應用', uri: 'URL', uri_no_query: 'URL 路徑', host: '域名', get_param: 'GET 參數', post_param: 'POST 參數',
                req_header: '請求 Header', req_body: '請求 Body', res_body: '回應 Body', req_raw_header: '完整請求 Header', res_raw_header: '完整回應 Header', method: '請求方法', ja4: 'JA4 指紋' };
            const ops = { eq: '等於', not_eq: '不等於', match: '萬用字元匹配', cidr: '屬於網段', not_cidr: '不屬於網段', in: '屬於 IP 組', not_in: '不屬於 IP 組', geo_eq: '屬於地理位置', geo_not_eq: '不屬於地理位置',
                has: '包含', not_has: '不包含', prefix: '前綴匹配', re: '正則匹配', exist: '存在', not_exist: '不存在', ja4_in: '屬於指紋庫' };
            return (targets[c.k] || c.k) + (c.sub_k ? ' [' + c.sub_k + ']' : '') + ' ' + (ops[c.op] || c.op) + ' ' + (Array.isArray(c.v) ? c.v.join(', ') : '—');
        }

        function edit(id, action) {
            const f = modal(id ? '編輯規則' : '添加規則', 1050); if (!f) return;
            f.read('/policy-management/' + (id ? 'edit?id=' + encodeURIComponent(id) : 'create'), function (data) {
                const v = id ? data.values : { action: action, name: '', is_enabled: true, log: false, pattern: [[{ k: 'src_ip', op: 'eq', sub_k: '', v: [] }]] };
                const options = data.options || {}, targets = options.targets || [], premium = data.capabilities && data.capabilities.premium;
                let groups = copy(v.pattern || []), groupsPanel, conditions = [], chosenAction = v.action;
                const actionField = { xtype: 'radiogroup', name: 'action', itemId: 'safeline-policy-action', columns: 2, value: { policy_action: v.action },
                    items: [{ boxLabel: '白名單（放行）', name: 'policy_action', inputValue: 0, checked: v.action === 0 }, { boxLabel: '黑名單（攔截）', name: 'policy_action', inputValue: 1, checked: v.action === 1 }],
                    listeners: { change: function (field, value) { const log = f.dialog.down('[name=log]'); if (log) log.setVisible(Number(value.policy_action) === 0); } } };
                const fields = [actionField, { xtype: 'fieldcontainer', layout: 'hbox', items: [
                    { xtype: 'textfield', name: 'name', fieldLabel: '名稱', labelAlign: 'top', value: v.name, allowBlank: false, maxLength: 1024, flex: 2, margin: '0 12 12 0' },
                    Object.assign(choice('insert_position', '插入位置', id ? '' : 'first', (id ? [['', '不改變']] : []).concat([['first', '首位'], ['last', '末位']])), { flex: 1, labelAlign: 'top' })] },
                    { xtype: 'panel', itemId: 'safeline-policy-groups', border: false, layout: 'anchor', items: [] },
                    { xtype: 'button', text: '添加一個 OR 條件', itemId: 'safeline-policy-add-or', margin: '0 0 12', hidden: data.locked.includes('pattern'), handler: function () { capture(); if (groups.length < 64) { groups.push([{ k: 'src_ip', op: 'eq', sub_k: '', v: [] }]); draw(); } } },
                    Object.assign(check('log', '加白時仍檢測，記錄其中的攻擊請求', v.log), { labelWidth: 310, hidden: v.action !== 0 }), check('is_enabled', '啟用此規則', v.is_enabled)];
                f.body.add(form(fields)); groupsPanel = f.dialog.down('#safeline-policy-groups');
                function capture() {
                    conditions.forEach(function (c) {
                        c.rule.k = c.row.down('[name=k]').getValue(); c.rule.op = c.row.down('[name=op]').getValue();
                        const sub = c.row.down('[name=sub_k]'), values = c.row.down('[name=v]'); c.rule.sub_k = sub ? sub.getValue() || '' : '';
                        const value = values ? values.getValue() : [];
                        c.rule.v = Array.isArray(value) ? value.map(String) : value === '' || value == null ? [] : String(value).split(/\r?\n/);
                    });
                }
                function valueOptions(rule) {
                    if (rule.k === 'src_ip' && ['in', 'not_in'].includes(rule.op)) return (options.groups || []).map(function (g) { return [String(g.id), g.comment || 'IP 組 #' + g.id]; });
                    if (rule.k === 'website') return (options.sites || []).map(function (s) { return [String(s.id), (s.comment || s.title || '應用 #' + s.id) + ' · ' + (s.server_names || []).join(', ')]; });
                    if (rule.op === 'ja4_in') return (options.ja4 || []).map(function (g) { return [String(g.id), g.name || '指紋庫 #' + g.id]; });
                    if (rule.k === 'method') return ['GET', 'HEAD', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS', 'TRACE', 'CONNECT'].map(function (s) { return [s, s]; });
                    if (['geo_eq', 'geo_not_eq'].includes(rule.op)) return (options.geo || []).map(function (g) {
                        let name = g.name; try { if (/^[A-Z]{2}$/.test(g.id) && typeof Intl.DisplayNames === 'function') name = new Intl.DisplayNames(['zh-Hant'], { type: 'region' }).of(g.id); } catch (e) { /* keep upstream name */ }
                        return [g.id, name + ' · ' + g.id];
                    });
                    return null;
                }
                function draw() {
                    conditions = []; groupsPanel.removeAll();
                    if (data.locked.includes('pattern')) { groupsPanel.add({ xtype: 'component', html: '匹配條件含敏感或不支援的內容，保留現有設定。', margin: '10 0' }); return; }
                    groups.forEach(function (group, gi) {
                        const box = groupsPanel.add({ xtype: 'fieldset', title: '條件組 ' + (gi + 1) + '（AND）', padding: 12, layout: 'anchor', items: [] });
                        group.forEach(function (rule, ri) {
                            const target = targets.find(function (t) { return t.id === rule.k; }), ops = target ? target.operators : [{ id: rule.op, name: '現有方式：' + rule.op }];
                            const known = target && ops.some(function (o) { return o.id === rule.op; });
                            const change = function (which, field) {
                                capture(); rule[which] = field.getValue();
                                if (which === 'k') { const next = targets.find(function (t) { return t.id === rule.k; }); rule.op = next ? next.operators[0].id : rule.op; rule.sub_k = ''; }
                                rule.v = []; draw();
                            };
                            const items = [Object.assign(choice('k', '匹配目標', rule.k, targets.map(function (t) { return [t.id, t.name + (!premium && ['res_body', 'res_raw_header'].includes(t.id) ? '（需授權）' : '')]; }).concat(target ? [] : [[rule.k, '現有目標：' + rule.k]])),
                                { width: 195, disabled: !known, margin: '0 10 0 0', listeners: { select: function (field) { change('k', field); } } })];
                            if (['req_header', 'get_param', 'post_param'].includes(rule.k)) items.push(Object.assign(choice('sub_k', rule.k === 'req_header' ? 'Header 名稱' : '參數名稱', rule.sub_k || '',
                                ['User-Agent', 'Referer', 'Content-Type', 'Cookie', 'Authorization', 'X-Forwarded-For'].map(function (x) { return [x, x]; })), { width: 160, editable: true, forceSelection: false, allowBlank: false, margin: '0 10 0 0' }));
                            items.push(Object.assign(choice('op', '匹配方式', rule.op, ops.map(function (o) { return [o.id, o.name + (!premium && ['geo_eq', 'geo_not_eq'].includes(o.id) ? '（需授權）' : '')]; }).concat(known || !target ? [] : [[rule.op, '現有方式：' + rule.op]])),
                                { width: 175, disabled: !known, margin: '0 10 0 0', listeners: { select: function (field) { change('op', field); } } }));
                            const choices = valueOptions(rule);
                            if (['exist', 'not_exist'].includes(rule.op)) items.push({ xtype: 'displayfield', value: '不需填寫匹配內容', flex: 1 });
                            else if (choices) items.push(Object.assign(choice('v', '匹配內容', rule.v, choices), { multiSelect: true, editable: true, allowBlank: false, disabled: !known, flex: 1, margin: '0 10 0 0' }));
                            else items.push({ xtype: 'textareafield', name: 'v', fieldLabel: '匹配內容（每行一項）', value: (rule.v || []).join('\n'), allowBlank: false, maxLength: 65536, height: 64, disabled: !known, flex: 1, margin: '0 10 0 0' });
                            items.push({ xtype: 'button', text: '刪除', disabled: group.length === 1, margin: '21 0 0', handler: function () { capture(); group.splice(ri, 1); draw(); } });
                            const row = box.add({ xtype: 'fieldcontainer', layout: 'hbox', defaults: { labelAlign: 'top' }, items: items }); conditions.push({ rule: rule, row: row });
                            if (!premium && (['geo_eq', 'geo_not_eq'].includes(rule.op) || ['res_body', 'res_raw_header'].includes(rule.k))) box.add({ xtype: 'component', html: enc(ctx.message('safeline_feature_unavailable')), margin: '0 0 8' });
                        });
                        box.add({ xtype: 'fieldcontainer', layout: 'hbox', items: [{ xtype: 'button', text: '添加一個 AND 條件', handler: function () { capture(); if (group.length < 64) { group.push({ k: 'src_ip', op: 'eq', sub_k: '', v: [] }); draw(); } } },
                            { xtype: 'component', flex: 1 }, { xtype: 'button', text: '刪除該條件組', disabled: groups.length === 1, handler: function () { capture(); groups.splice(gi, 1); draw(); } }] });
                    });
                }
                draw();
                f.change = function () {
                    capture(); const changes = {}, values = { action: Number(fieldValue(f, 'action').policy_action), name: fieldValue(f, 'name'),
                        log: fieldValue(f, 'log'), is_enabled: fieldValue(f, 'is_enabled') };
                    if (!data.locked.includes('pattern')) values.pattern = groups;
                    chosenAction = values.action;
                    Object.keys(values).forEach(function (key) { if (!id || !same(values[key], v[key])) changes[key] = values[key]; });
                    const position = fieldValue(f, 'insert_position'); if (position) changes.insert_position = position;
                    return changes;
                };
                f.confirmed = function () { if (id) ctx.edited(id, chosenAction); if (!state.selection.id) ctx.showList(chosenAction); };
                f.save.setDisabled(data.locked.includes('name'));
            });
        }

        function subscribe() {
            const f = modal('訂閱在線規則', 1050); if (!f) return;
            f.read('/policy-management/subscribe', function (data) {
                let selected;
                const table = ctx.grid(data.catalog || [], [{ text: '規則庫', dataIndex: 'policy_name', flex: 2, minWidth: 220 }, { text: '提供者', dataIndex: 'org_name', flex: 1, minWidth: 140 },
                    { text: '黑名單', dataIndex: 'black_rule', width: 90 }, { text: '白名單', dataIndex: 'white_rule', width: 90 }, { text: '可用規則', dataIndex: 'compatible', width: 90 },
                    { text: '已添加', dataIndex: 'added', width: 80, renderer: ctx.switchMark }, { text: '更新時間', dataIndex: 'updated_at', width: 180, renderer: function (v) { return enc(ctx.date(v)); } }]);
                table.itemId = 'safeline-subscription-grid'; table.height = Math.min(510, (window.innerHeight || 800) - 220);
                table.bbar = [{ xtype: 'tbtext', text: '選取規則庫後，訂閱會套用該庫包含的黑、白名單。' }];
                table.listeners = { selectionchange: function (model, records) {
                    selected = records[0] && records[0].data; f.save.setDisabled(!selected || selected.added !== false || f.submitted);
                    if (selected) f.notice(selected.policy_name + '：' + Math.max(0, selected.black_rule || 0) + ' 條黑名單、' + Math.max(0, selected.white_rule || 0) + ' 條白名單；訂閱整個規則庫。');
                } };
                f.body.add(table); f.save.setText('訂閱所選規則庫'); f.change = function () { if (!selected || selected.added) throw new Error('請選擇尚未添加的規則庫'); return { id: selected.id }; };
                f.confirmed = function () { if (state.selection.kind === 'lists') ctx.showList(state.tab === 'allow' ? 0 : 1); };
            });
        }

        function list(action) {
            const serial = state.policySerial = (state.policySerial || 0) + 1, path = '/policy-management/order?action=' + action;
            ctx.note('正在讀取' + (action === 0 ? '白名單' : '黑名單') + '…');
            ctx.request(path, {}, function (data) {
                if (serial !== state.policySerial || !Array.isArray(data.rules)) return;
                const source = data.rules.filter(function (r) { return belongs(r, action); });
                const locked = source.filter(function (r) { return !sortable(r); }), initial = source.filter(sortable).sort(function (a, b) { return (a.order || 0) - (b.order || 0); });
                let draft = initial.slice(), table, dirty = false, pending = false, submitted = false;
                function selected() { const record = table.getSelectionModel().getSelection()[0]; return record && record.data; }
                function rebuild(id) {
                    table.getStore().loadData(locked.concat(draft.map(function (r, i) { return Object.assign({}, r, { order: i + 1 }); })));
                    dirty = !same(draft.map(function (r) { return r.id; }), initial.map(function (r) { return r.id; }));
                    table.down('#safeline-policy-order-save').setDisabled(!dirty || submitted); table.down('#safeline-policy-order-cancel').setDisabled(!dirty || pending);
                    table.down('#safeline-policy-order-status').setText(dirty ? '排序尚未儲存；按「套用排序」寫入全部自訂規則的順序。' : '內建規則與在線規則庫保持原有位置。');
                    ['add', 'subscribe', 'edit'].forEach(function (key) { table.down('#safeline-policy-' + key).setDisabled(dirty || pending); });
                    if (id) table.getSelectionModel().select(table.getStore().getById(id));
                }
                function move(position) {
                    const row = selected(), from = row && draft.findIndex(function (r) { return r.id === row.id; });
                    if (!row || from < 0 || pending || submitted) return;
                    const index = Math.max(0, Math.min(draft.length - 1, position)); if (index === from) return;
                    const item = draft.splice(from, 1)[0]; draft.splice(index, 0, item); rebuild(row.id);
                }
                function saveOrder() {
                    if (!dirty || pending || submitted || !ctx.user()) return;
                    const operation = ctx.uuid(); if (!operation) return;
                    pending = true; submitted = true; table.down('#safeline-policy-order-save').setDisabled(true); table.down('#safeline-policy-order-cancel').setDisabled(true);
                    table.down('#safeline-policy-order-status').setText('正在套用並回讀順序…');
                    ctx.request(path, { method: 'POST', headers: { 'X-PVE-Plugins-CSRF': state.connection.csrf_token }, jsonData: {
                        expected_revision: data.connection_revision, fingerprint: data.fingerprint, operation_id: operation, changes: { ids: draft.map(function (r) { return Number(r.id); }) } } }, function (result) {
                        pending = false; ctx.loadInventory(); if (!alive(table)) return;
                        if (result.outcome === 'confirmed') list(action);
                        else { table.down('#safeline-policy-order-status').setText(ctx.message(result.code)); table.down('#safeline-policy-order-cancel').setDisabled(false); }
                    }, function (code) { pending = false; if (alive(table)) { table.down('#safeline-policy-order-status').setText(ctx.message(code) + '；請重新載入後核對順序。'); table.down('#safeline-policy-order-cancel').setDisabled(false); } }, false);
                }
                const config = ctx.grid(locked.concat(initial), [{ text: '順序', dataIndex: 'order', width: 85, renderer: function (v, m, r) { return sortable(r.data) ? enc(v || 1) : '—'; } },
                    { text: '規則 ID', dataIndex: 'id', width: 90 }, { text: '啟用', dataIndex: 'is_enabled', width: 70, align: 'center', renderer: ctx.switchMark },
                    { text: '類型', dataIndex: 'action', width: 90, renderer: function () { return action === 0 ? '白名單' : '黑名單'; } }, { text: '名稱', dataIndex: 'name', flex: 1, minWidth: 210 },
                    { text: '匹配條件', dataIndex: 'pattern', flex: 2, minWidth: 250, renderer: function (v, m, r) { return textCell(r.data.cloud_id > 0 ? '在線規則庫：' + (r.data.black_rule || 0) + ' 黑／' + (r.data.white_rule || 0) + ' 白' : patternLabel(v), m); } },
                    { text: '今日命中', dataIndex: 'req_count', width: 110, align: 'right', renderer: function (v) { return enc(ctx.amount(v)); } }, { text: '建立人', dataIndex: 'creator', width: 150 },
                    { text: '更新時間', dataIndex: 'updated_at', width: 185, renderer: function (v) { return enc(ctx.date(v)); } }], function (row) { if (!dirty && !pending) ctx.choose(row); });
                config.itemId = 'safeline-policy-list';
                config.tbar = { enableOverflow: true, items: config.tbar.concat([
                    { text: '添加規則', itemId: 'safeline-policy-add', iconCls: 'fa fa-plus', handler: function () { if (!dirty && !pending) edit(null, action); } }, { text: '訂閱在線規則', itemId: 'safeline-policy-subscribe', iconCls: 'fa fa-cloud-download', handler: function () { if (!dirty && !pending) subscribe(); } },
                    { text: '編輯', itemId: 'safeline-policy-edit', iconCls: 'fa fa-pencil', handler: function () { const r = selected(); if (r && sortable(r) && !dirty && !pending) edit(r.id, action); } },
                    { text: '上移', handler: function () { const r = selected(); if (r) move(draft.findIndex(function (v) { return v.id === r.id; }) - 1); } },
                    { text: '下移', handler: function () { const r = selected(); if (r) move(draft.findIndex(function (v) { return v.id === r.id; }) + 1); } },
                    Object.assign(number('order_position', '位置', 1, 1, Math.max(1, draft.length)), { width: 110, labelWidth: 35 }), { text: '移至', handler: function () { move(Number(table.down('[name=order_position]').getValue()) - 1); } },
                    { text: '套用排序', itemId: 'safeline-policy-order-save', disabled: true, handler: saveOrder }, { text: '取消排序', itemId: 'safeline-policy-order-cancel', disabled: true, handler: function () { if (!pending) list(action); } }
                ]) };
                config.bbar = [{ xtype: 'tbtext', itemId: 'safeline-policy-order-status', text: '共 ' + source.length + ' 項；內建規則與在線規則庫保持原有位置。' }];
                ctx.show(config); table = state.page.down('#safeline-policy-list');
            }, function (code) { if (serial === state.policySerial) ctx.note(ctx.message(code)); }, true);
        }

        function configureCC() {
            const f = modal('CC 防護 · 全域頻率限制配置', 1020); if (!f) return;
            f.read('/cc/configuration', function (data) {
                const originals = copy(data.values.rules || []), draft = copy(originals);
                function editRow(row) {
                    if (f.pending || alive(f.child)) return;
                    row = draft.find(function (r) { return r.id === row.id; }); if (!row) return;
                    const childFields = [check('enabled', '啟用', row.enabled), number('period', '統計時段（秒）', row.period, 1, 86400), number('count', '觸發門檻', row.count, 1, 1000000000),
                        number('block_min', '限制時間（分鐘）', row.block_min, 1, 86400), choice('action', '處理方式', row.action, [['block', '攔截'], ['challenge_v1', '人機驗證']].concat(data.capabilities.premium || row.action === 'challenge_v2' ? [['challenge_v2', '滑動驗證']] : [])),
                        { xtype: 'textfield', name: 'custom_status_codes', fieldLabel: '自訂錯誤狀態碼', value: (row.custom_status_codes || []).join(', '), emptyText: '留空沿用預設，例如 403, 404' }];
                    f.child = Ext.create('Ext.window.Window', { title: row.name || '編輯頻率限制', width: 570, modal: true, constrain: true, layout: 'fit', bodyPadding: 14, items: [form(childFields)],
                        buttons: [{ text: '套用至草稿', handler: function () {
                            if (!f.child.down('form').getForm().isValid()) return;
                            childFields.forEach(function (field) { const value = f.child.down('[name=' + field.name + ']').getValue(); row[field.name] = field.name === 'custom_status_codes' ? String(value || '').split(/[,\s]+/).filter(Boolean) : value; });
                            table.getStore().loadData(draft); f.child.destroy();
                        } }, { text: '取消', handler: function () { f.child.close(); } }] }); f.child.show();
                }
                const config = ctx.grid(draft, [{ text: '名稱', dataIndex: 'name', flex: 1, minWidth: 190 }, { text: '啟用', dataIndex: 'enabled', width: 70, renderer: ctx.switchMark },
                    { text: '時段（秒）', dataIndex: 'period', width: 100 }, { text: '次數門檻', dataIndex: 'count', width: 110 }, { text: '限制（分鐘）', dataIndex: 'block_min', width: 115 },
                    { text: '處理方式', dataIndex: 'action', width: 140, renderer: function (v) { return enc(({ block: '攔截', challenge_v1: '人機驗證', challenge_v2: '滑動驗證' })[v] || v); } }], editRow);
                config.itemId = 'safeline-global-cc-rules'; config.height = 330;
                config.tbar.push({ text: '編輯', iconCls: 'fa fa-pencil', handler: function () { const r = table.getSelectionModel().getSelection()[0]; if (r) editRow(draft.find(function (v) { return v.id === r.data.id; })); } });
                f.body.add(config); const table = f.dialog.down('#safeline-global-cc-rules');
                f.change = function () {
                    const changes = draft.map(function (row) { const old = originals.find(function (r) { return r.id === row.id; }), patch = { id: row.id };
                        ['enabled', 'period', 'count', 'block_min', 'action', 'custom_status_codes'].forEach(function (k) { if (!same(row[k], old[k])) patch[k] = row[k]; }); return patch;
                    }).filter(function (v) { return Object.keys(v).length > 1; }); return changes.length ? { rules: changes } : {};
                };
                f.save.setDisabled(false);
            });
        }
        function relieve(selected, filters, refresh) {
            const f = modal(selected ? '解封所選限制' : '解封全部符合篩選的限制', 640); if (!f) return;
            const path = '/cc/relieve' + (Object.keys(filters).length ? '?' + new URLSearchParams(filters).toString() : '');
            f.read(path, function (data) {
                const ids = selected ? data.values.ids.filter(function (id) { return selected.includes(id); }) : data.values.ids;
                f.body.add({ xtype: 'component', html: enc(ids.length ? '將解除 ' + ids.length + ' 筆目前仍生效的限制。後續請求會重新依防護規則判斷。' : '目前沒有可解封的限制。'), style: 'white-space:normal;line-height:1.7' });
                f.change = function () { return ids.length ? { ids: ids } : {}; }; f.save.setText('確認解封'); f.save.setDisabled(!ids.length); f.confirmed = refresh;
            });
        }
        return { list: list, edit: edit, subscribe: subscribe, configureCC: configureCC, relieve: relieve };
    };
}());
