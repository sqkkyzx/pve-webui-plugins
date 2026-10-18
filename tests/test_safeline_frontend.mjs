import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import { webcrypto } from 'node:crypto';

const source = readFileSync(new URL('../plugins/safeline/static/safeline.js', import.meta.url), 'utf8');
const base = '/pve-plugins/api/safeline';

function browser() {
    const classes = new Map(), stack = [], requests = [], timers = new Set(), windows = [], warnings = [];
    function define(name, config) {
        if (config.override) {
            const type = classes.get(config.override);
            for (const [key, fn] of Object.entries(config)) {
                if (typeof fn !== 'function') continue;
                const previous = type.prototype[key];
                type.prototype[key] = function (...args) { stack.push(previous); try { return fn.apply(this, args); } finally { stack.pop(); } };
            }
            return;
        }
        const Parent = classes.get(config.extend);
        function Component(value) { Object.assign(this, value); this.events = {}; this.children = []; this.xtype ||= 'panel'; }
        if (Parent) Component.prototype = Object.create(Parent.prototype);
        for (const [key, value] of Object.entries(config)) {
            if (typeof value !== 'function') { Component.prototype[key] = value; continue; }
            const previous = Parent?.prototype[key];
            Component.prototype[key] = function (...args) { stack.push(previous); try { return value.apply(this, args); } finally { stack.pop(); } };
        }
        Component.prototype.callParent = function (args) { return stack.at(-1)?.apply(this, args); };
        classes.set(name, Component);
    }
    function node(data, parent) {
        return { data: { ...data }, parentNode: parent, childNodes: [], get(key) { return this.data[key]; }, getId() { return this.data.id; },
            set(value) { Object.assign(this.data, value); }, findChild(key, value) { return this.childNodes.find((n) => n.get(key) === value); },
            appendChild(value) { const n = node(value, this); this.childNodes.push(n); for (const c of value.children || []) n.appendChild(c); return n; },
            remove() { this.parentNode.childNodes = this.parentNode.childNodes.filter((n) => n !== this); }, removeAll() { this.childNodes = []; } };
    }
    function store(config = {}) {
        const s = { rows: [], loadData(data) { this.rows = data.map((v) => ({ data: v, get: (k) => v[k], getId: () => v.id })); },
            getById(id) { return this.rows.find((r) => r.getId() === id); }, clearFilter() {}, filterBy(fn) { this.rows = this.rows.filter(fn); } };
        s.each = (fn) => s.rows.forEach(fn);
        s.add = (v) => s.rows.push({ data: v, get: (k) => v[k], getId: () => v.id });
        s.remove = (rows) => { s.rows = s.rows.filter((row) => !rows.includes(row)); };
        s.loadData(config.data || []);
        if (config.root) {
            s.root = node(config.root); for (const c of config.root.children || []) s.root.appendChild(c);
            s.getNodeById = function (id) { const find = (n) => n.getId() === id ? n : n.childNodes.map(find).find(Boolean); return find(this.root); };
        }
        return s;
    }
    define('Ext.Component', {
        initComponent() {
            this.children = [];
            for (const key of ['items', 'tbar', 'bbar', 'buttons']) {
                const values = Array.isArray(this[key]) ? this[key] : this[key]?.items || [];
                for (const value of values) if (value && typeof value === 'object') this.add(value);
            }
            if (this.store) this.dataStore = store(this.store);
            this.selection = { selected: [], select(record) { this.selected = record ? [record] : []; }, getSelection() { return this.selected; }, suspendEvents() {}, resumeEvents() {} };
        },
        on(name, fn) { (this.events[name] ||= []).push(fn); }, fire(name, ...args) { let result; for (const fn of this.events[name] || []) result = fn.apply(this, args.length ? args : [this]); return result; },
        add(value) { const c = value.events ? value : create(value.xtype === 'grid' ? 'Ext.grid.Panel' : 'Ext.Component', value); c.ownerCt = this; this.children.push(c); if (this.rendered) c.render(); return c; },
        removeAll() { for (const c of this.children) c.destroy(); this.children = []; },
        down(selector) { for (const c of this.children) { if (selector === '#' + c.itemId || selector === c.xtype) return c; const found = c.down(selector); if (found) return found; } return null; },
        up(selector) { let p = this.ownerCt; while (p) { if (p.xtype === selector) return p; p = p.ownerCt; } return null; },
        getStore() { return this.dataStore; }, getRootNode() { return this.dataStore.root; }, getSelectionModel() { return this.selection; },
        setVisible(value) { this.hidden = !value; }, setText(value) { this.text = value; }, update(value) { this.html = value; }, setDisabled(value) { this.disabled = value; },
        getValue() { return this.xtype === 'checkbox' ? !!this.checked : this.value; }, setValue(value) { this.value = value; },
        getPlugins() { return []; },
        getEl() { return { on: (name, fn) => { this.elementEvents ||= {}; this.elementEvents[name] = fn; }, down: () => ({ focus() {} }) }; },
        getForm() { return { isValid: () => true, getValues: () => Object.fromEntries(this.children.filter((c) => c.name).map((c) => [c.name, c.getValue()])) }; },
        render() { if (this.rendered || this.destroyed) return; this.rendered = true; for (const c of this.children) c.render(); this.fire('afterrender'); },
        destroy() { if (this.destroyed || this.destroying) return; this.destroying = true; this.fire('beforedestroy'); for (const c of this.children) c.destroy(); this.destroyed = true; this.fire('destroy'); },
        show() { this.render(); windows.push(this); }, close() { if (this.fire('beforeclose') !== false) this.destroy(); },
    });
    for (const name of ['Ext.grid.Panel', 'Ext.tree.Panel', 'Ext.window.Window']) define(name, { extend: 'Ext.Component' });
    define('PVE.form.ViewSelector', { extend: 'Ext.Component', initComponent() {
        const rows = [{ key: 'server' }]; this.dataStore = { rows, add: (r) => rows.push(r), findRecord: (k, v) => rows.find((r) => r[k] === v) };
        return 'native-selector';
    } });
    define('PVE.StdWorkspace', { extend: 'Ext.Component', xtype: 'pveStdWorkspace', onLogin(data) { context.window.Proxmox.UserName = data?.username || null; return 'native-login'; },
        setContent(config) { const old = this.activePage; if (old && !old.destroyed) { old.fire('deactivate'); old.fire('hide'); old.destroy(); } this.activePage = create('Ext.Component', config); this.activePage.ownerCt = this; this.activePage.render(); } });
    define('PVE.tree.ResourceTree', { extend: 'Ext.Component', initComponent() {
        this.callParent(arguments);
        this.setViewFilter = function (view) { this.viewFilter = view; this.setVisible(view.id !== 'pve-komodo-containers'); this.ownerCt.down('#pve-komodo-container-tree').setVisible(view.id === 'pve-komodo-containers'); return 'native-filter'; };
        return 'native-tree';
    } });
    function create(name, config = {}) { const c = new (classes.get(name) || classes.get('Ext.Component'))(config); c.initComponent(); for (const [name, fn] of Object.entries(config.listeners || {})) c.on(name, fn); return c; }
    const context = vm.createContext({ URLSearchParams, window: { Ext: { define, create, ClassManager: { get: (name) => classes.get(name) }, Ajax: {
        request(options) { const request = { options, aborted: false }; requests.push(request); return request; }, abort(request) { request.aborted = true; },
    } }, crypto: webcrypto, Proxmox: { UserName: 'root@pam', Utils: { authOK: () => !!context.window.Proxmox.UserName } },
    setTimeout(fn) { const timer = { fn }; timers.add(timer); return timer; }, clearTimeout(timer) { timers.delete(timer); }, document: { hidden: false }, console: { warn: (...args) => warnings.push(args) } } });
    vm.runInContext(source, context);
    const workspace = create('PVE.StdWorkspace'); workspace.xtype = 'pveStdWorkspace';
    const owner = create('Ext.Component', { ownerCt: workspace });
    owner.add({ itemId: 'pve-komodo-container-tree', hidden: true }); owner.add({ itemId: 'pve-extra-sites-panel', hidden: true });
    const native = create('PVE.tree.ResourceTree', { ownerCt: owner }); owner.children.push(native);
    function enter() { native.setViewFilter({ id: 'pve-safeline-waf' }); return native._pveSafeLine; }
    function reply(path, data, status = 200, selectedRequest) {
        const r = selectedRequest || requests.find((r) => !r.replied && r.options.url === base + path);
        assert.ok(r, path); r.replied = true; r.options.callback(null, status >= 200 && status < 300, { status, responseText: JSON.stringify(status === 200 ? { user: 'root@pam', ...data } : data) }); return r;
    }
    return { context, create, workspace, native, requests, enter, reply, timers, windows, warnings, owner };
}

const connection = { configured: true, revision: 1, source: 'saved', csrf_token: 'csrf-value', connection: { url: 'https://10.101.100.101:9443', token_configured: true, tls_verify: true, tls_server_name: 'waf.example', ca_pem: '' } };
function inventory(name = '<img src=x onerror=alert(1)>', code = 'running') {
    return { resources: { applications: [{ id: '12', kind: 'applications', name, domains: ['app.example'], ports: ['443'], status: { code, label: '防護模式' } }], deny: [], allow: [], certificates: [], 'ip-groups': [] },
        policies: [], errors: {}, totals: {}, truncated: {}, origin_id: 'waf-origin', connection_revision: 1, updated_at: '2026-10-18T12:00:00Z' };
}

test('SafeLine view loads through its own API and restores the native and container views', () => {
    const b = browser(), state = b.enter();
    assert.equal(b.native.hidden, true); assert.equal(state.tree.hidden, false);
    b.reply('/connection', connection);
    assert.equal(state.tree.getStore().getNodeById('type:applications').get('text'), '防護應用', 'unloaded counts are not zero');
    b.reply('/inventory', inventory());
    assert.equal(state.tree.getRootNode().childNodes.length, 5);
    assert.equal(state.tree.getStore().getNodeById('applications:12').get('text'), '<img src=x onerror=alert(1)>');
    const renderer = state.tree.columns[0].renderer;
    assert.ok(renderer('<img>', {}, state.tree.getStore().getNodeById('applications:12')).includes('&lt;img&gt;'));
    assert.ok(b.requests.every((r) => r.options.url.startsWith(base + '/')));
    state.tree.fire('itemclick', {}, state.tree.getStore().getNodeById('applications:12'));
    assert.equal(state.selection.id, '12');
    b.native.setViewFilter({ id: 'pve-komodo-containers' });
    assert.equal(state.active, false); assert.equal(state.tree.hidden, true);
    assert.equal(b.owner.down('#pve-komodo-container-tree').hidden, false); assert.equal(b.timers.size, 0);
    assert.equal(state.selection.kind, 'root'); assert.equal(state.selection.id, undefined, 'a later connection cannot inherit a previous resource ID');
    b.native.setViewFilter({ id: 'server' }); assert.equal(b.native.hidden, false);
    assert.equal(b.warnings.length, 0);
});

test('refresh updates nodes in place and retains resource identity and expansion state', () => {
    const b = browser(), state = b.enter(); b.reply('/connection', connection); b.reply('/inventory', inventory('before'));
    const app = state.tree.getStore().getNodeById('applications:12'), folder = state.tree.getStore().getNodeById('type:applications');
    folder.set({ expanded: false }); state.selection = { kind: 'applications', id: '12' }; state.tab = 'summary';
    state.tree.tbar[0].handler(); b.reply('/inventory', inventory('renamed', 'paused'));
    assert.equal(state.tree.getStore().getNodeById('applications:12'), app);
    assert.equal(app.get('text'), 'renamed'); assert.equal(app.get('iconCls'), 'fa fa-globe paused');
    assert.equal(folder.get('expanded'), false); assert.equal(state.selection.id, '12');
});

test('logout cancels pending requests, wipes resources and ignores delayed callbacks', () => {
    const b = browser(), state = b.enter(); b.reply('/connection', connection);
    const pending = b.requests.find((r) => r.options.url === base + '/inventory');
    b.workspace.onLogin(null);
    assert.equal(state.active, false); assert.equal(state.connection, null); assert.equal(state.inventory, null);
    assert.equal(pending.aborted, true); assert.equal(b.timers.size, 0);
    b.reply('/inventory', inventory('must-never-appear'), 200, pending);
    assert.equal(state.inventory, null); assert.equal(state.tree.getRootNode().childNodes.length, 0);
});

test('a changed user without a login hook cannot retain or update prior private content', () => {
    const b = browser(), state = b.enter(); b.reply('/connection', connection); b.reply('/inventory', inventory());
    state.tree.tbar[0].handler();
    b.context.window.Proxmox.UserName = 'other@pam';
    b.reply('/inventory', inventory('private-late-response'));
    assert.equal(state.active, false); assert.equal(state.inventory, null); assert.equal(state.page, null);
    assert.equal(b.timers.size, 0);
});

test('saved credentials are never filled into the editor; new values submit to the backend with CSRF', () => {
    const b = browser(), state = b.enter(); b.reply('/connection', { ...connection, configured: false });
    const settings = state.page.down('#safeline-body').children[0]; settings.tbar[0].handler();
    const editor = state.editor, form = editor.down('form');
    assert.equal(form.children.find((c) => c.name === 'token').value, '');
    form.children.find((c) => c.name === 'token').value = 'new-private-token';
    editor.down('#safeline-test').handler();
    const submitted = b.requests.at(-1).options;
    assert.equal(submitted.url, base + '/connection/test'); assert.equal(submitted.jsonData.token, 'new-private-token');
    assert.equal(submitted.headers['X-PVE-Plugins-CSRF'], 'csrf-value');
    b.reply('/connection/test', { connected: true, version: '9.4.2' });
    assert.ok(editor.down('#safeline-edit-status').html.includes('尚未保存'));
    assert.equal(state.connection.revision, 1);
});

test('duplicate script registration does not wrap the selector again', () => {
    const b = browser(); vm.runInContext(source, b.context);
    const selector = b.create('PVE.form.ViewSelector');
    assert.equal(selector.getStore().rows.filter((row) => row.key === 'pve-safeline-waf').length, 1);
});

test('log filters use their native form, validate dates and submit documented query fields', () => {
    const b = browser(), state = b.enter(); b.reply('/connection', connection); b.reply('/inventory', inventory());
    state.logMode = 'records'; const nav = state.page.down('#safeline-nav'); nav.fire('itemclick', {}, nav.getStore().getById('events'));
    b.reply('/records?page=1&page_size=20&source=general', { items: [], total: 0, connection_revision: 1 });
    const table = state.page.down('#safeline-body').children[0]; table.tbar.items.find((item) => item.text === '篩選').handler();
    const dialog = state.editor, form = dialog.down('form');
    const field = (name) => form.children.find((c) => c.name === name);
    field('action').value = '1'; field('url').value = '/login'; field('start_time').value = 'invalid';
    const before = b.requests.length; dialog.buttons[0].handler(); assert.equal(b.requests.length, before);
    assert.ok(dialog.down('#safeline-filter-status').html.includes('ISO 8601'));
    field('start_time').value = '2026-10-17T00:00:00Z'; field('end_time').value = '2026-10-18T00:00:00Z';
    dialog.buttons[0].handler();
    const params = new URL('http://test.local' + b.requests.at(-1).options.url).searchParams;
    assert.equal(params.get('action'), '1'); assert.equal(params.get('url'), '/login');
    assert.equal(params.get('start'), String(Date.parse('2026-10-17T00:00:00Z')));
    assert.equal(dialog.destroyed, true); assert.equal(state.editor, null);
});

test('aggregate events query related logs by dimensions and never guess a record identifier', () => {
    const b = browser(), state = b.enter(); b.reply('/connection', connection); b.reply('/inventory', inventory());
    const nav = state.page.down('#safeline-nav'); nav.fire('itemclick', {}, nav.getStore().getById('events'));
    const event = { id: 162, ip: '10.0.0.1', host: 'app.example', dst_port: 443, start_at: 1792185285000, end_at: 1792185420007 };
    b.reply('/events?page=1&page_size=20&source=general', { items: [event], total: 1, connection_revision: 1 });
    const table = state.page.down('#safeline-body').children[0]; table.fire('itemdblclick', {}, table.getStore().rows[0]);
    state.editor.buttons[0].handler();
    const request = new URL('http://test.local' + b.requests.at(-1).options.url);
    assert.equal(request.pathname, base + '/records'); assert.equal(request.searchParams.get('ip'), event.ip);
    assert.equal(request.searchParams.get('start'), String(event.start_at)); assert.equal(request.searchParams.has('event_id'), false);
    assert.equal(state.editor, null);
});

test('partial refresh keeps stale node identity and its last successful timestamp', () => {
    const b = browser(), state = b.enter(); b.reply('/connection', connection); b.reply('/inventory', inventory());
    const previous = state.tree.getStore().getNodeById('applications:12'); state.tree.tbar[0].handler();
    const partial = inventory(); partial.resources.applications = []; partial.errors.applications = 'safeline_timeout'; partial.updated_at = '2026-10-18T13:00:00Z';
    b.reply('/inventory', partial);
    assert.equal(state.tree.getStore().getNodeById('applications:12'), previous);
    assert.equal(previous.get('iconCls'), 'fa fa-globe unknown');
    assert.equal(state.inventory.resources.applications[0].observed_at, '2026-10-18T12:00:00Z');
    state.page.destroy(); assert.equal(state.active, false); assert.equal(b.timers.size, 0);
});

function openResourceEditor(b, state, section = 'settings', values = { comment: 'before' }, kind = 'applications') {
    const data = inventory('before');
    if (kind !== 'applications') data.resources[kind] = [{ id: '12', kind, name: 'before', status: { code: 'running', label: '啟用' } }];
    b.reply('/connection', connection); b.reply('/inventory', data);
    state.tree.fire('itemclick', {}, state.tree.getStore().getNodeById(kind + ':12'));
    const button = state.page.down(section === 'mode' ? '#safeline-resource-mode' : section === 'switch' ? '#safeline-resource-switch' : '#safeline-resource-edit');
    assert.equal(button.hidden, false); assert.equal(button.disabled, false); button.handler();
    const path = '/resources/' + kind + '/12/edit?section=' + section;
    b.reply(path, { values, locked: [], fingerprint: 'a'.repeat(64), connection_revision: 1, certificate_type: kind === 'certificates' ? 2 : null });
    return { path, dialog: state.editor };
}

test('resource editor submits only changed fields with a revision, fingerprint, UUID and CSRF', () => {
    const b = browser(), state = b.enter();
    const { path, dialog } = openResourceEditor(b, state);
    dialog.down('#safeline-input-comment').value = 'after';
    dialog.down('#safeline-resource-save').handler();
    const options = b.requests.at(-1).options;
    assert.equal(options.url, base + path); assert.equal(options.method, 'PUT');
    assert.deepEqual(JSON.parse(JSON.stringify(options.jsonData.changes)), { comment: 'after' });
    assert.equal(options.jsonData.fingerprint, 'a'.repeat(64)); assert.equal(options.jsonData.expected_revision, 1);
    assert.match(options.jsonData.operation_id, /^[a-f0-9-]{36}$/); assert.equal(options.headers['X-PVE-Plugins-CSRF'], 'csrf-value');
    const count = b.requests.length; dialog.down('#safeline-resource-save').handler(); assert.equal(b.requests.length, count);
    b.reply(path, { outcome: 'confirmed', connection_revision: 1 }, 200, b.requests.at(-1));
    assert.equal(dialog.destroyed, true); assert.equal(state.editor, null);
    assert.ok(b.requests.some((request) => request.options.url === base + '/inventory' && !request.replied));
});

test('mode editor and policy switch use explicit selections and do not overwrite resource settings', () => {
    for (const [kind, section, values, key, next] of [['applications', 'mode', { mode: 0 }, 'mode', 2], ['deny', 'switch', { is_enabled: true }, 'is_enabled', false]]) {
        const b = browser(), state = b.enter(), { dialog } = openResourceEditor(b, state, section, values, kind);
        const field = dialog.down('#safeline-input-' + key); if (key === 'is_enabled') field.checked = next; else field.value = next;
        dialog.down('#safeline-resource-save').handler();
        assert.deepEqual(JSON.parse(JSON.stringify(b.requests.at(-1).options.jsonData.changes)), { [key]: next });
    }
});

test('resource conflicts and unknown writes keep the draft and prevent automatic resubmission', () => {
    for (const conflict of [true, false]) {
        const b = browser(), state = b.enter(), { path, dialog } = openResourceEditor(b, state);
        dialog.down('#safeline-input-comment').value = 'after'; dialog.down('#safeline-resource-save').handler();
        b.reply(path, conflict ? { detail: 'safeline_resource_changed' } : { outcome: 'unknown', connection_revision: 1 }, conflict ? 409 : 200, b.requests.at(-1));
        assert.equal(dialog.destroyed, undefined); assert.equal(dialog.down('#safeline-resource-save').disabled, true);
        assert.ok(dialog.down('#safeline-resource-edit-status').html.includes(conflict ? '資源已被修改' : '不會自動重送'));
        const count = b.requests.length; for (const timer of [...b.timers]) timer.fn();
        assert.equal(b.requests.length, count, 'polling stays paused while the edit window is open');
    }
});

test('unchanged policy conditions preserve whitespace, empty values and existing operators', () => {
    const b = browser(), state = b.enter();
    const pattern = [[{ k: 'req_header', op: 'future_operator', v: [' keep spaces ', ''] }]];
    const { dialog } = openResourceEditor(b, state, 'settings', { name: 'before', log: true, pattern }, 'deny');
    dialog.down('#safeline-input-name').value = 'renamed'; dialog.down('#safeline-resource-save').handler();
    assert.deepEqual(JSON.parse(JSON.stringify(b.requests.at(-1).options.jsonData.changes)), { name: 'renamed' });
});

test('certificate editor starts blank and only sends a new certificate when explicitly saved', () => {
    const b = browser(), state = b.enter(), { dialog } = openResourceEditor(b, state, 'settings', { crt: '', key: '' }, 'certificates');
    assert.equal(dialog.down('#safeline-input-key').value, '');
    const count = b.requests.length; dialog.down('#safeline-resource-save').handler(); assert.equal(b.requests.length, count);
    dialog.down('#safeline-input-crt').value = 'new-public-pem'; dialog.down('#safeline-resource-save').handler();
    assert.deepEqual(JSON.parse(JSON.stringify(b.requests.at(-1).options.jsonData.changes)), { crt: 'new-public-pem', key: '' });
    b.workspace.onLogin(null); assert.equal(dialog.destroyed, true); assert.equal(state.editor, null);
});

function dashboardData(edition = 2, filters = { preset: 'last1Day', site_id: null }) {
    const presets = ['last1Day', 'today', 'last7Day', 'last30Day'];
    return { sections: { access: { access: 1000, pv: 250, session: 100, ip: 40 }, attack: { attack_ip: 3, intercept: { block: 1, blacklist: 2, challenge: 3, rate_limit: 4, auth_defense: 5, offline: 6 } },
        error_upstream: { error_4xx: 10, error_5xx: 3 }, error_waf: { error_4xx: 5, error_5xx: 7 },
        qps: [{ time: '12:00:00', count: 3 }], access_trend: [{ time: 1792317600, count: 1000 }], intercept_trend: [{ time: 1792317600, count: 21 }],
        geo_world_access: [{ country: 'CN', province: '', count: 111 }], geo_world_intercept: [{ country: 'US', province: '', count: 22 }],
        geo_china_access: [{ country: 'CN', province: '福建省', count: 333 }], geo_china_intercept: [{ country: 'CN', province: '廣東省', count: 44 }],
        posture: { attack_deny: 1, attack_allow: 2, black_hit: 3, white_hit: 4, acl_hit: 5, waiting_hit: 6, challenge_deny: 7, challenge_allow: 8, auth_deny: 9, auth_allow: 10, anti_tamper: [] },
        posture_trend: { denied: [{ time: 1792317600, count: 1 }], allowed: [{ time: 1792317600, count: 2 }], sources: [{ name: '10.0.0.1', count: 3 }] },
        realtime: [{ type: 1, site_name: 'app.example', site_id: 12, time: 1792317600 }] },
        errors: {}, capabilities: { edition, traffic: { applications: edition !== 0, presets: edition ? presets : ['last1Day'] },
            security: { applications: edition === 2, presets: edition ? presets : ['last1Day', 'last7Day'] }, presets: edition ? presets : ['last1Day', 'last7Day'] },
        filters, begin_time: 1792240000, end_time: 1792326400, updated_at: '2026-10-18T12:00:00Z', connection_revision: 1 };
}
function replyDashboard(b, data, status = 200, request) {
    request ||= b.requests.findLast((r) => !r.replied && r.options.url.startsWith(base + '/overview?'));
    return b.reply(request.options.url.slice(base.length), data, status, request);
}
function startDashboard(b, state, data = dashboardData()) { b.reply('/connection', connection); b.reply('/inventory', inventory()); replyDashboard(b, data); return state.page.down('#safeline-dashboard'); }

test('combined overview displays traffic, error rates and security metrics from separate API sections', () => {
    const b = browser(), state = b.enter(), panel = startDashboard(b, state);
    const html = panel.down('#safeline-overview-body').html;
    for (const label of ['流量分析', '安全態勢', '請求次數', '4xx 錯誤', '4xx 攔截', '5xx 錯誤', '地理位置', '攻擊防護趨勢', '攻擊來源 IP', '實時事件']) assert.ok(html.includes(label), label);
    assert.ok(html.includes('1.00%') && html.includes('0.50%'), 'rates use the request count as denominator');
    assert.ok(html.includes('1.0k')); assert.equal(panel.down('#safeline-overview-application').getValue(), 'all');
    assert.equal(panel.down('#safeline-overview-application').disabled, false);
    assert.equal(b.requests.filter((r) => r.options.url.startsWith(base + '/overview?')).length, 1, 'inventory completion never cancels and duplicates the initial dashboard query');
});

test('four geography combinations toggle locally and preserve scope without fetching a globe', () => {
    const b = browser(), state = b.enter(), panel = startDashboard(b, state), body = panel.down('#safeline-overview-body');
    const count = b.requests.length;
    const click = (group, value) => body.elementEvents.click({ getTarget: () => ({ getAttribute: (key) => key === 'data-safeline-geo-' + group ? value : null }) });
    assert.ok(body.html.includes('世界 · 訪問') && body.html.includes('111'));
    click('mode', 'intercept'); assert.ok(body.html.includes('世界 · 僅攔截') && body.html.includes('22'));
    click('region', 'china'); assert.ok(body.html.includes('中國 · 僅攔截') && body.html.includes('廣東省'));
    click('mode', 'access'); assert.ok(body.html.includes('中國 · 訪問') && body.html.includes('福建省'));
    assert.equal(b.requests.length, count); assert.ok(!body.html.includes('<canvas'));
    assert.equal(state.overview.filters.preset, 'last1Day');
});

test('application and time selectors share one query and ignore late responses from previous scopes', () => {
    const b = browser(), state = b.enter(), panel = startDashboard(b, state), app = panel.down('#safeline-overview-application'), time = panel.down('#safeline-overview-time');
    app.value = '12'; app.fire('select', app);
    const appRequest = b.requests.at(-1), params = new URL('http://test.local' + appRequest.options.url).searchParams;
    assert.equal(params.get('site_id'), '12'); assert.equal(params.get('preset'), 'last1Day');
    assert.equal(state.overview, null); assert.ok(!panel.down('#safeline-overview-body').html.includes('1.0k'));
    time.value = 'last7Day'; time.fire('select', time);
    const current = b.requests.at(-1); assert.equal(appRequest.aborted, true);
    replyDashboard(b, dashboardData(2, { preset: 'last1Day', site_id: 12 }), 200, appRequest);
    assert.equal(state.overview, null);
    replyDashboard(b, dashboardData(2, { preset: 'last7Day', site_id: 12 }), 200, current);
    assert.equal(state.overview.filters.preset, 'last7Day'); assert.equal(state.overview.filters.site_id, 12);
    assert.equal(state.page.down('#safeline-dashboard'), panel); assert.equal(panel.down('#safeline-overview-time'), time, 'form controls are not replaced when data refreshes');
});

test('personal edition disables ignored application filters while retaining valid posture time ranges', () => {
    const b = browser(), state = b.enter(), panel = startDashboard(b, state, dashboardData(0));
    assert.equal(panel.down('#safeline-overview-application').disabled, true);
    assert.equal(panel.down('#safeline-overview-time').disabled, false);
    assert.equal(panel.down('#safeline-overview-time').getStore().rows.map((r) => r.data.id).join(','), 'last1Day,last7Day');
    assert.ok(panel.down('#safeline-overview-notice').html.includes('僅提供全站資料'));
    const time = panel.down('#safeline-overview-time'); time.value = 'last7Day'; time.fire('select', time);
    const data = dashboardData(0, { preset: 'last7Day', site_id: null }); delete data.sections.access; data.errors.access = 'safeline_statistics_range_unavailable';
    replyDashboard(b, data);
    assert.ok(panel.down('#safeline-overview-body').html.includes('目前版本不支援此區塊的統計時段'));
    assert.ok(panel.down('#safeline-overview-body').html.includes('安全態勢'));
});

test('dashboard encodes outside labels and never fabricates rates for missing counters', () => {
    const b = browser(), state = b.enter(), data = dashboardData();
    const unsafe = '<img src=x onerror=alert(1)>';
    data.sections.realtime[0].site_name = unsafe; data.sections.posture_trend.sources[0].name = unsafe; data.sections.geo_world_access[0].country = unsafe;
    data.sections.access.access = null; data.sections.error_upstream.error_4xx = null;
    const html = startDashboard(b, state, data).down('#safeline-overview-body').html;
    assert.ok(!html.includes(unsafe)); assert.ok(html.includes('&lt;img')); assert.ok(html.includes('不可用'));
    assert.ok(!html.includes('NaN') && !html.includes('Infinity'));
});

test('polling cannot overlap pending overview work and logout clears dashboard scopes', () => {
    const b = browser(), state = b.enter(); b.reply('/connection', connection); b.reply('/inventory', inventory());
    const count = b.requests.filter((r) => r.options.url.startsWith(base + '/overview?')).length;
    for (const timer of [...b.timers]) timer.fn();
    assert.equal(b.requests.filter((r) => r.options.url.startsWith(base + '/overview?')).length, count);
    replyDashboard(b, dashboardData()); state.geoRegion = 'china'; state.overviewFilters.siteId = '12';
    b.workspace.onLogin(null);
    assert.equal(state.overview, null); assert.equal(state.dashboardCaps, null); assert.equal(state.overviewFilters.siteId, null); assert.equal(state.geoRegion, 'world');
});
