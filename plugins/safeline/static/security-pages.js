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
        const targetNames = { src_ip: '來源 IP', website: '防護應用', uri: 'URL（含查詢參數）', uri_no_query: 'URL 路徑', host: '域名', get_param: 'GET 參數', post_param: 'POST 參數',
            req_header: '請求 Header', req_body: '請求 Body', res_body: '回應 Body', req_raw_header: '完整請求 Header', res_raw_header: '完整回應 Header', method: '請求方法', ja4: 'JA4 指紋' };
        const operatorNames = { eq: '等於', not_eq: '不等於', match: '萬用字元匹配', cidr: '屬於網段', not_cidr: '不屬於網段', in: '屬於 IP 組', not_in: '不屬於 IP 組', geo_eq: '屬於地理位置', geo_not_eq: '不屬於地理位置',
            has: '包含', not_has: '不包含', prefix: '前綴匹配', re: '符合正則表達式', exist: '存在', not_exist: '不存在', ja4_in: '屬於指紋庫' };
        const ruleMeaning = '匹配任意條件組即命中規則；每個條件組內需要同時匹配所有條件。';
        function ruleName(c) { return (targetNames[c.k] || '匹配目標（' + c.k + '）') + (c.sub_k ? '「' + c.sub_k + '」' : '') + ' ' + (operatorNames[c.op] || '匹配方式（' + c.op + '）'); }
        function valueOptions(rule, options) {
            if (rule.k === 'src_ip' && ['in', 'not_in'].includes(rule.op)) return (options.groups || []).map(function (g) { return [String(g.id), g.comment || 'IP 組 #' + g.id]; });
            if (rule.k === 'website') return (options.sites || []).map(function (s) { return [String(s.id), (s.comment || s.title || '應用 #' + s.id) + ((s.server_names || []).length ? ' · ' + s.server_names.join(', ') : '')]; });
            if (rule.op === 'ja4_in') return (options.ja4 || []).map(function (g) { return [String(g.id), g.name || '指紋庫 #' + g.id]; });
            if (rule.k === 'method') return ['GET', 'HEAD', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS', 'TRACE', 'CONNECT'].map(function (s) { return [s, s]; });
            if (['geo_eq', 'geo_not_eq'].includes(rule.op)) return (options.geo || []).map(function (g) {
                let name = g.name; try { if (/^[A-Z]{2}$/.test(g.id) && typeof Intl.DisplayNames === 'function') name = new Intl.DisplayNames(['zh-Hant'], { type: 'region' }).of(g.id); } catch (e) { /* keep upstream name */ }
                return [g.id, name + ' · ' + g.id];
            });
            return null;
        }
        function ruleValues(rule, options) {
            if (['exist', 'not_exist'].includes(rule.op)) return '不需匹配內容';
            const choices = valueOptions(rule, options), values = Array.isArray(rule.v) ? rule.v : [];
            return values.map(function (v) { const match = choices && choices.find(function (c) { return c[0] === String(v); }); return match ? match[1] : v; }).join('\n');
        }
        function patternLabel(v) {
            if (!Array.isArray(v) || !v.length) return '—';
            if (!Array.isArray(v[0])) return '—';
            if (v.length > 1 || v[0].length > 1) return '複合條件';
            const c = v[0][0]; if (!c) return '—';
            return ruleName(c) + ' ' + (Array.isArray(c.v) ? c.v.join(', ') : '—');
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
                            const choices = valueOptions(rule, options);
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

        function localEditor(title, fields, collect, apply, owner) {
            if (alive(state.editor) || !alive(owner) || !ctx.user()) return null;
            const generation = state.generation, who = ctx.user(); let dialog;
            dialog = Ext.create('Ext.window.Window', { title: title, width: Math.min(730, (window.innerWidth || 1000) - 30),
                maxHeight: Math.min(680, (window.innerHeight || 800) - 30), modal: true, constrain: true, layout: 'fit', bodyPadding: 14,
                items: [form(fields)], bbar: [{ xtype: 'tbtext', itemId: 'safeline-rule-input-status', text: '', style: 'white-space:normal' }],
                buttons: [{ text: '套用至草稿', itemId: 'safeline-rule-input-apply', handler: function () {
                    if (!alive(owner) || !ctx.current(generation, who) || !dialog.down('form').getForm().isValid()) return;
                    let value; try { value = collect(dialog); } catch (error) { dialog.down('#safeline-rule-input-status').setText(enc(error.message)); return; }
                    dialog.destroy(); apply(value);
                } }, { text: '取消', handler: function () { dialog.close(); } }],
                listeners: { destroy: function () { if (state.editor === dialog) state.editor = null; } } });
            state.editor = dialog; dialog.show(); return dialog;
        }
        function editCondition(original, data, apply, owner, title) {
            let rule = copy(original || { k: 'src_ip', op: 'eq', sub_k: '', v: [] }), dialog;
            const options = data.options || {}, targets = options.targets || [], premium = data.capabilities.premium;
            function capture(window) {
                const value = function (key) { const field = window.down('[name=' + key + ']'); return field && field.getValue(); };
                rule.k = value('k'); rule.op = value('op'); rule.sub_k = value('sub_k') || '';
                const values = value('v'); rule.v = Array.isArray(values) ? values.map(String) : values == null || values === '' ? [] : String(values).split(/\r?\n/);
                return copy(rule);
            }
            function draw() {
                const form = dialog.down('form'), target = targets.find(function (t) { return t.id === rule.k; }); form.removeAll();
                function changed(key, field) {
                    capture(dialog); rule[key] = field.getValue();
                    if (key === 'k') { const next = targets.find(function (t) { return t.id === rule.k; }); rule.op = next.operators[0].id; rule.sub_k = ''; }
                    rule.v = []; draw();
                }
                form.add(Object.assign(choice('k', '匹配目標', rule.k, targets.map(function (t) { return [t.id, t.name]; })), { listeners: { select: function (f) { changed('k', f); } } }));
                if (['get_param', 'post_param', 'req_header'].includes(rule.k)) form.add({ xtype: 'textfield', name: 'sub_k', fieldLabel: rule.k === 'req_header' ? 'Header 名稱' : '參數名稱', value: rule.sub_k, allowBlank: false, maxLength: 1024 });
                form.add(Object.assign(choice('op', '匹配方式', rule.op, (target ? target.operators : []).map(function (op) { return [op.id, operatorNames[op.id] || op.name]; })), { listeners: { select: function (f) { changed('op', f); } } }));
                const choices = valueOptions(rule, options);
                if (['exist', 'not_exist'].includes(rule.op)) form.add({ xtype: 'displayfield', value: '不需填寫匹配內容' });
                else if (choices) form.add(Object.assign(choice('v', '匹配內容', rule.v, choices), { multiSelect: true, editable: true, allowBlank: false }));
                else form.add({ xtype: 'textareafield', name: 'v', fieldLabel: '匹配內容（每行一項）', value: rule.v.join('\n'), height: 130, maxLength: 65536, allowBlank: false });
                if (!premium && (['geo_eq', 'geo_not_eq'].includes(rule.op) || ['res_body', 'res_raw_header'].includes(rule.k))) form.add({ xtype: 'displayfield', value: enc(ctx.message('safeline_feature_unavailable')) });
            }
            dialog = localEditor(title || (original ? '編輯 AND 條件' : '添加 AND 條件'), [], function (window) {
                const result = capture(window);
                if (!['exist', 'not_exist'].includes(result.op) && (!result.v.length || result.v.some(function (v) { return v === ''; }))) throw new Error('請填寫匹配內容');
                if (!premium && (['geo_eq', 'geo_not_eq'].includes(result.op) || ['res_body', 'res_raw_header'].includes(result.k)) && !same(result, original)) throw new Error(ctx.message('safeline_feature_unavailable'));
                return result;
            }, apply, owner);
            if (dialog) { draw(); if (typeof dialog.center === 'function') dialog.center(); }
        }
        function ruleAction(resource, action, refresh) {
            const id = resource.id, f = modal(action === 'delete' ? '刪除規則' : action === 'enable' ? '啟用規則' : '禁用規則', 650); if (!f) return;
            const remove = action === 'delete';
            f.read('/policy-management/' + (remove ? 'delete' : 'switch') + '?id=' + encodeURIComponent(id), function (data) {
                const v = data.values, allowed = data.capabilities[remove ? 'can_delete' : 'can_switch'];
                f.body.add({ xtype: 'component', html: enc((remove ? '刪除' : action === 'enable' ? '啟用' : '禁用') + '「' + v.name + '」 · #' + v.id), margin: '0 0 14' });
                if (remove) {
                    if (v.cloud_id > 0) f.body.add({ xtype: 'component', html: '這會移除整個在線規則庫及其黑、白名單規則。', margin: '0 0 12' });
                    f.body.add(form([{ xtype: 'textfield', name: 'confirm_name', fieldLabel: '輸入規則名稱確認', labelWidth: 145, allowBlank: false, maxLength: 1024 }]));
                    f.change = function () { const name = fieldValue(f, 'confirm_name'); if (name !== v.name) throw new Error('請輸入完整的規則名稱'); return { confirm_name: name }; };
                } else f.change = function () { return v.is_enabled === (action === 'enable') ? {} : { is_enabled: action === 'enable' }; };
                f.save.setText(remove ? '確認刪除' : '確認' + (action === 'enable' ? '啟用' : '禁用')); f.save.setDisabled(!allowed);
                f.confirmed = function () { if (remove) ctx.showList(v.action === 0 ? 0 : 1); else refresh(); };
            });
        }
        function summary(resource, section, reload) {
            section = section === 'conditions' ? 'conditions' : 'summary';
            const resourceKey = resource.kind + ':' + resource.id, existing = state.page && state.page.down('#safeline-policy-summary');
            if (!reload && alive(existing) && existing.resourceKey === resourceKey && (existing.hasDraft() || existing.section !== section)) { existing.setSection(section); return; }
            const path = '/policy-management/summary?id=' + encodeURIComponent(resource.id);
            ctx.note(section === 'conditions' ? '正在讀取匹配條件…' : '正在讀取規則概要…');
            ctx.request(path, {}, function (data) {
                if (!data.values || !Array.isArray(data.locked) || !data.options || data.connection_revision !== state.connection.revision) { ctx.note(ctx.message('safeline_unexpected_response')); return; }
                const initial = copy(data.values), caps = data.capabilities, originalGroups = copy(data.values.pattern || data.preview || []);
                let draft = copy(initial), groups = copy(originalGroups), panel, body, dirty = false, pending = false, submitted = false, notice = '', conditions = section === 'conditions';
                function differences() {
                    const changes = {};
                    ['name', 'action', 'order'].forEach(function (key) { if (!same(draft[key], initial[key])) changes[key] = draft[key]; });
                    if (draft.action !== initial.action) changes.order = draft.order;
                    if (caps.can_edit_pattern && !same(groups, originalGroups)) changes.pattern = copy(groups);
                    return changes;
                }
                function status(message) { notice = message; if (alive(panel)) panel.down('#safeline-rule-status').setText(enc(message)); }
                function mutable() { return !pending && !submitted && !resource.stale; }
                function afterAction() { ctx.edited(resource.id, initial.action); }
                function save() {
                    const changes = differences(); if (!Object.keys(changes).length || !mutable() || !ctx.user()) return;
                    const operation = ctx.uuid(); if (!operation) return;
                    pending = true; submitted = true; draw(); status('正在儲存並回讀確認…');
                    ctx.request(path, { method: 'POST', headers: { 'X-PVE-Plugins-CSRF': state.connection.csrf_token }, jsonData: {
                        expected_revision: data.connection_revision, fingerprint: data.fingerprint, operation_id: operation, changes: changes } }, function (result) {
                        pending = false; if (!alive(panel)) { ctx.loadInventory(); return; }
                        if (result.outcome === 'confirmed') { dirty = false; submitted = false; status('已儲存並確認，正在更新…'); ctx.edited(resource.id, draft.action); }
                        else { draw(); status(ctx.message(result.code)); }
                    }, function (code) {
                        pending = false; if (!alive(panel)) return;
                        if (code.startsWith('invalid_') || ['safeline_feature_unavailable', 'safeline_edit_unsupported'].includes(code)) submitted = false;
                        draw(); status(ctx.message(code));
                    }, false);
                }
                function metadata() {
                    if (!caps.can_edit || !mutable()) return;
                    function limit(action) { return (data.rules || []).filter(function (r) { return String(r.id) !== String(resource.id) && r.action === action && sortable(r); }).length + 1; }
                    localEditor('編輯規則基本資訊', [{ xtype: 'textfield', name: 'name', fieldLabel: '名稱', value: draft.name, maxLength: 1024, allowBlank: false },
                        Object.assign(choice('action', '類型', draft.action, [[1, '黑名單（攔截）'], [0, '白名單（放行）']]), { listeners: { select: function (field) {
                            const input = field.up('form').down('[name=order]'), max = limit(Number(field.getValue())); if (typeof input.setMaxValue === 'function') input.setMaxValue(max);
                            if (Number(input.getValue()) > max) input.setValue(max);
                        } } }), number('order', '順序序號', draft.order || 1, 1, limit(draft.action))], function (window) {
                        return { name: window.down('[name=name]').getValue(), action: Number(window.down('[name=action]').getValue()), order: Number(window.down('[name=order]').getValue()) };
                    }, function (v) { Object.assign(draft, v); draw(); }, panel);
                }
                function wrapped(value, meta) { textCell(value, meta); return '<div style="white-space:pre-wrap;overflow-wrap:anywhere">' + enc(value) + '</div>'; }
                function rowsGrid(rows, title, handler) {
                    const grid = ctx.grid(rows, [{ text: '項目', dataIndex: 'name', width: 310, renderer: wrapped }, { text: '值', dataIndex: 'value', flex: 1,
                        renderer: function (v, m, r) { return typeof v === 'boolean' ? ctx.switchMark(v, m, r) : wrapped(v == null ? '—' : String(v), m); } }], handler);
                    Object.assign(grid, { title: title, border: false, shrinkWrap: 2, scrollable: false, bufferedRenderer: false, margin: '0 0 14', tbar: null, bbar: null });
                    grid.viewConfig.scrollable = false; return grid;
                }
                function draw() {
                    if (!alive(panel)) return;
                    dirty = !!Object.keys(differences()).length; const writable = mutable();
                    ['enable', 'disable', 'delete'].forEach(function (key) { panel.down('#safeline-rule-' + key).setVisible(!conditions); });
                    panel.down('#safeline-rule-add-or').setVisible(conditions);
                    panel.down('#safeline-rule-enable').setDisabled(!caps.can_switch || initial.is_enabled !== false || dirty || !writable);
                    panel.down('#safeline-rule-disable').setDisabled(!caps.can_switch || initial.is_enabled !== true || dirty || !writable);
                    panel.down('#safeline-rule-delete').setDisabled(!caps.can_delete || dirty || !writable);
                    panel.down('#safeline-rule-add-or').setDisabled(!caps.can_edit_pattern || !writable || groups.length >= 64);
                    panel.down('#safeline-rule-save').setDisabled(!dirty || !writable); panel.down('#safeline-rule-reset').setDisabled(pending || !dirty && !submitted);
                    body.removeAll();
                    const metadataRows = [['名稱', draft.name, true], ['類型', ({ 0: '白名單（放行）', 1: '黑名單（攔截）', 999: '在線規則庫（黑／白名單）' })[draft.action] || '其他防護規則', true],
                        ['順序序號', caps.can_edit ? draft.order : '—', true], ['規則 ID', initial.id], ['啟用', initial.is_enabled], ['記錄日誌', initial.log],
                        ['今日命中', ctx.amount(initial.req_count)], ['更新時間', ctx.date(initial.updated_at)]];
                    if (!conditions) {
                        const basic = rowsGrid(metadataRows.map(function (r, i) { return { id: i, name: r[0], value: r[1], editable: r[2] }; }), '基本資訊', function (row) { if (row.editable) metadata(); });
                        basic.itemId = 'safeline-rule-metadata'; basic.tbar = [{ text: '編輯基本資訊', iconCls: 'fa fa-pencil', disabled: !caps.can_edit || !writable, handler: metadata }]; body.add(basic);
                    }
                    body.add({ xtype: 'component', html: '<h3 style="margin:0 8px 8px;font-size:14px">' + (conditions ? '匹配條件' : '匹配條件預覽') + '</h3><p style="margin:0 8px 12px">' + enc(ruleMeaning) + '</p>' +
                        (initial.negate ? '<p style="margin:0 8px 12px">此規則已啟用條件取反：以上匹配結果將取反後判斷。</p>' : '') });
                    if (!caps.can_edit_pattern) body.add({ xtype: 'component', html: '<p style="margin:0 8px 12px">' + enc(initial.builtin || initial.cloud_id > 0 ? '此規則的匹配內容由內建功能或在線規則庫管理，僅供檢視。' : '匹配規則含敏感或不支援的內容，保留現有設定。') + '</p>' });
                    if (!groups.length) body.add({ xtype: 'component', html: '<p style="margin:8px">上游未提供可展示的匹配規則。</p>' });
                    if (!conditions) body.add({ xtype: 'component', itemId: 'safeline-rule-preview', html: '<div style="margin:0 8px 14px;line-height:1.8;overflow-wrap:anywhere">' + groups.map(function (group, index) {
                        return (index ? '<div style="font-weight:600;margin:8px 0">或者（OR）</div>' : '') + group.map(function (rule, i) {
                            return '<div><span style="opacity:.65">' + enc((i ? '並且 ' : '') + ruleName(rule)) + '</span>　' + enc(ruleValues(rule, data.options)).replace(/\n/g, ', ') + '</div>';
                        }).join('');
                    }).join('') + '</div>' });
                    if (conditions) groups.forEach(function (group, index) {
                        if (index) body.add({ xtype: 'component', html: '<p style="margin:0 8px 12px;font-weight:600">或者（OR）</p>' });
                        const rows = group.map(function (rule, i) { return { id: i, name: (i ? '並且 ' : '') + ruleName(rule), value: ruleValues(rule, data.options) }; });
                        const grid = rowsGrid(rows, '條件組 ' + (index + 1) + '（AND）', function (row) {
                            if (caps.can_edit_pattern && mutable()) editCondition(group[row.id], data, function (v) { group[row.id] = v; draw(); }, panel);
                        });
                        grid.itemId = 'safeline-rule-group-' + index; grid.columns[0].text = '匹配規則'; grid.columns[1].text = '匹配內容'; grid.selModel = { mode: 'MULTI' };
                        const allowed = caps.can_edit_pattern && writable;
                        grid.tbar = { enableOverflow: true, items: [{ text: '添加 AND 條件', disabled: !allowed || group.length >= 64, handler: function () {
                            if (caps.can_edit_pattern && mutable()) editCondition(null, data, function (v) { group.push(v); draw(); }, panel);
                        } }, { text: '刪除選中條件', itemId: 'safeline-rule-delete-conditions-' + index, disabled: true, handler: function () {
                            if (!caps.can_edit_pattern || !mutable()) return;
                            const table = panel.down('#safeline-rule-group-' + index), indices = table.getSelectionModel().getSelection().map(function (r) { return r.data.id; });
                            if (!indices.length || indices.length >= group.length) return;
                            groups[index] = group.filter(function (r, i) { return !indices.includes(i); }); draw();
                        } }, { text: '刪除本條件組', disabled: !allowed || groups.length <= 1, handler: function () { if (caps.can_edit_pattern && mutable() && groups.length > 1) { groups.splice(index, 1); draw(); } } }] };
                        grid.listeners.selectionchange = function (model, records) { const button = panel.down('#safeline-rule-delete-conditions-' + index); if (button) button.setDisabled(!allowed || !records.length || records.length >= group.length); };
                        body.add(grid);
                    });
                    status(submitted && notice ? notice : dirty ? '尚未儲存；按「儲存」後生效。' : conditions ? '預設只讀；雙擊條件列編輯。' : '預設只讀；雙擊基本資訊列編輯。');
                }
                ctx.show({ xtype: 'panel', itemId: 'safeline-policy-summary', border: false, layout: 'border',
                    items: [{ xtype: 'panel', region: 'north', border: false, tbar: { enableOverflow: true, items: [
                        { text: '啟用', itemId: 'safeline-rule-enable', iconCls: 'fa fa-play', handler: function () { if (!dirty && mutable()) ruleAction(resource, 'enable', afterAction); } },
                        { text: '禁用', itemId: 'safeline-rule-disable', iconCls: 'fa fa-stop', handler: function () { if (!dirty && mutable()) ruleAction(resource, 'disable', afterAction); } },
                        { text: '刪除', itemId: 'safeline-rule-delete', iconCls: 'fa fa-trash', handler: function () { if (!dirty && mutable()) ruleAction(resource, 'delete', afterAction); } }, '-',
                        { text: '添加 OR 條件組', itemId: 'safeline-rule-add-or', handler: function () { if (caps.can_edit_pattern && mutable() && groups.length < 64) editCondition(null, data, function (v) { groups.push([v]); draw(); }, panel, '添加 OR 條件組'); } }, '->',
                        { text: '儲存', itemId: 'safeline-rule-save', iconCls: 'fa fa-save', disabled: true, handler: save },
                        { text: '取消變更', itemId: 'safeline-rule-reset', disabled: true, handler: function () { if (!pending) summary(resource, panel.section, true); } }] } },
                        { xtype: 'panel', itemId: 'safeline-rule-body', region: 'center', border: false, scrollable: true, layout: 'anchor', defaults: { anchor: '100%' } }],
                    bbar: [{ xtype: 'tbtext', itemId: 'safeline-rule-status', text: '' }] });
                panel = state.page.down('#safeline-policy-summary'); body = panel.down('#safeline-rule-body'); panel.hasDraft = function () { return dirty || pending || submitted; };
                panel.resourceKey = resourceKey; panel.section = section; panel.setSection = function (value) { panel.section = value; conditions = value === 'conditions'; draw(); }; draw();
            }, function (code) { ctx.note(ctx.message(code)); }, true);
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
                    { text: '匹配規則', dataIndex: 'pattern', flex: 2, minWidth: 250, renderer: function (v, m, r) { return textCell(r.data.cloud_id > 0 ? '在線規則庫：' + (r.data.black_rule || 0) + ' 黑／' + (r.data.white_rule || 0) + ' 白' : patternLabel(v), m); } },
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
        return { list: list, edit: edit, summary: summary, subscribe: subscribe, configureCC: configureCC, relieve: relieve };
    };
}());
