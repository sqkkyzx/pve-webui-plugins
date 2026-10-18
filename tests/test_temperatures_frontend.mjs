import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../plugins/temperatures/static/temperatures.js', import.meta.url), 'utf8');

function browser() {
    const classes = new Map(), calls = [], requests = [], timers = new Map(), dialogs = [], warnings = [];
    let timerId = 0, context;
    function initialize(component, config) {
        component.events = new Map(); component.managed = []; component.destroyed = false;
        if (Array.isArray(component.items)) component.items = component.items.map((item) => ({ ...item }));
        Object.assign(component, config);
    }
    function wrap(owner, name, implementation, previous) {
        return function (...args) {
            calls.push({ owner, name, previous, receiver: this });
            try { return implementation.apply(this, args); } finally { calls.pop(); }
        };
    }
    function define(name, definition) {
        if (definition.override) {
            const target = classes.get(definition.override);
            for (const [method, implementation] of Object.entries(definition)) if (typeof implementation === 'function') {
                target.prototype[method] = wrap(target, method, implementation, Object.hasOwn(target.prototype, method) ? target.prototype[method] : null);
            }
            return target;
        }
        const parent = classes.get(definition.extend);
        function Component(config = {}) { initialize(this, config); }
        Component.superclass = parent?.prototype;
        Component.prototype = Object.create(parent?.prototype || null);
        Component.prototype.callParent = function (args = []) {
            const frame = calls.at(-1);
            assert.equal(frame.receiver, this);
            return (frame.previous || frame.owner.superclass[frame.name]).apply(this, args);
        };
        for (const [key, value] of Object.entries(definition)) Component.prototype[key] = typeof value === 'function' ? wrap(Component, key, value) : value;
        classes.set(name, Component);
        return Component;
    }
    define('Ext.Component', {
        on: function (name, callback, scope) {
            if (!this.events.has(name)) this.events.set(name, []);
            this.events.get(name).push({ callback, scope });
        },
        fire: function (name, ...args) { for (const event of [...(this.events.get(name) || [])]) event.callback.apply(event.scope || this, args.length ? args : [this]); },
        mon: function (target, name, callback, scope) { target.on(name, callback, scope); this.managed.push({ target, name, callback }); },
        up: function (xtype) { for (let parent = this.ownerCt; parent; parent = parent.ownerCt) if (parent.xtype === xtype) return parent; return null; },
        isVisible: function (deep) { return !this.hidden && (!deep || !this.ownerCt || this.ownerCt.isVisible(true)); },
        setHeight: function (height) { this.height = height; },
        setDisabled: function (disabled) { assert.equal(this.destroyed, false); this.disabled = disabled; },
        setText: function (text) { assert.equal(this.destroyed, false); this.text = text; },
        show: function () { this.hidden = false; if (!dialogs.includes(this)) dialogs.push(this); return this; },
        destroy: function () {
            if (this.destroyed) return;
            this.destroying = true; this.fire('beforedestroy');
            for (const item of this.items || []) item.destroy?.();
            for (const item of this.managed) item.target.events.set(item.name, (item.target.events.get(item.name) || []).filter((event) => event.callback !== item.callback));
            this.destroyed = true; this.fire('destroy');
        },
        initComponent: function () {},
    });
    const xtypeNames = { pmxInfoWidget: 'Proxmox.widget.Info', button: 'Ext.Component', tbtext: 'Ext.Component', grid: 'Ext.panel.Panel', container: 'Ext.panel.Panel' };
    function child(config, owner) {
        const component = create(xtypeNames[config.xtype] || 'Ext.Component', { ...config, ownerCt: owner });
        if (config.store) component.store = { rows: config.store.data || [], loadData(rows) { component.store.rows = rows; } };
        return component;
    }
    function create(name, config = {}) {
        const Component = classes.get(name);
        const component = new Component(config);
        component.initComponent();
        if (config.listeners) for (const [event, handler] of Object.entries(config.listeners)) component.on(event, handler);
        return component;
    }
    define('Ext.panel.Panel', {
        extend: 'Ext.Component',
        initComponent: function () {
            this.items = (this.items || []).map((item) => child(item, this));
            this.bottomItems = (this.bbar || []).map((item) => child(item, this));
            return 'original-panel-result';
        },
        insert: function (index, configs) { this.items.splice(index, 0, ...configs.map((config) => child(config, this))); },
        down: function (selector) {
            for (const item of [...this.items, ...this.bottomItems]) {
                if (selector === '#' + item.itemId) return item;
                const found = item.down?.(selector); if (found) return found;
            }
            return null;
        },
        query: function (xtype) {
            return this.items.flatMap((item) => [...(item.xtype === xtype ? [item] : []), ...(item.query?.(xtype) || [])]);
        },
        getStore: function () { return this.store; },
    });
    // The real InfoWidget updateValue stores rendered text and only updates its
    // progress bar when an explicit numeric usage is supplied (toolkit source).
    define('Proxmox.widget.Info', {
        extend: 'Ext.Component',
        initComponent: function () { this.updateValue(this.text, this.value); },
        updateValue: function (text, usage) {
            assert.equal(this.destroyed, false);
            if (this.lastText === text && this.lastUsage === usage) return;
            this.lastText = text; this.lastUsage = usage;
            if (usage !== undefined && this.printBar && typeof usage === 'number' && usage >= 0) this.barValue = usage;
        },
    });
    // Match the native StatusView's managed store listener and field selection:
    // unbound InfoWidgets must retain their independently supplied values.
    define('Proxmox.panel.StatusView', {
        extend: 'Ext.panel.Panel',
        updateField: function (field) {
            if (field.textField !== undefined) field.updateValue(this.rstore.values[field.textField]);
            else if (field.valueField !== undefined) field.updateValue(this.rstore.values[field.valueField], this.rstore.values[field.valueField]);
        },
        updateValues: function (store, records, success) { if (success) this.query('pmxInfoWidget').forEach((field) => this.updateField(field)); },
        initComponent: function () {
            const result = this.callParent(arguments);
            this.mon(this.rstore, 'load', this.updateValues, this);
            return result;
        },
    });
    // Relevant lifecycle/layout from pve-manager aa7db891 node/StatusView.js.
    define('PVE.node.StatusView', {
        extend: 'Proxmox.panel.StatusView', height: 350,
        items: [
            { xtype: 'pmxInfoWidget', itemId: 'cpu', valueField: 'cpu' },
            { xtype: 'pmxInfoWidget', itemId: 'memory', valueField: 'memory' },
            { xtype: 'pmxInfoWidget', itemId: 'cpus', textField: 'cpuinfo', colspan: 2 },
            { xtype: 'pmxInfoWidget', itemId: 'kernel', textField: 'kernel', colspan: 2 },
            { xtype: 'pmxInfoWidget', itemId: 'version', textField: 'pveversion', colspan: 2 },
        ],
        initComponent: function () {
            this.originalInitCalls = (this.originalInitCalls || 0) + 1;
            this.items.push({ xtype: 'Ext.Component', itemId: 'repositoryStatus' });
            return this.callParent(arguments);
        },
    });
    define('PVE.StdWorkspace', {
        extend: 'Ext.panel.Panel', xtype: 'pveStdWorkspace',
        onLogin: function (loginData) { context.window.Proxmox.UserName = loginData ? loginData.username : null; return 'native-login-result'; },
    });
    const Ext = { ClassManager: { get: (name) => classes.get(name) }, define,
        Ajax: {
            request(options) { const request = { options }; requests.push(request); return request; },
            abort(request) { request.aborted = true; request.options.callback(null, false, { status: 0 }); },
        },
        create(name, config) { return create(name === 'Ext.window.Window' ? 'Ext.panel.Panel' : name, config); },
    };
    context = vm.createContext({ window: { Ext, Proxmox: { UserName: 'root@pam', Utils: { authOK: () => true } },
        setTimeout(callback, delay) { const id = ++timerId; timers.set(id, { callback, delay }); return id; },
        clearTimeout(id) { timers.delete(id); }, console: { warn: (...args) => warnings.push(args) } } });
    function page(node = 'pve-test', hidden = false, workspace = create('PVE.StdWorkspace')) {
        const summary = create('Ext.panel.Panel', { xtype: 'pveNodeSummary', ownerCt: workspace, hidden });
        const store = create('Ext.Component', { values: { cpu: 0.25, memory: 0.4, cpuinfo: 'Original CPU model', kernel: 'Original kernel', pveversion: '9.2.22' } });
        const status = create('PVE.node.StatusView', { xtype: 'pveNodeStatus', ownerCt: summary, rstore: store, pveSelNode: { data: { node } } });
        status.fire('afterrender');
        return { status, summary, workspace, store,
            text: (category) => status.down('#pve-node-temperature-' + category).lastText,
            details: () => { status.down('#pve-node-temperature-details').handler(); return dialogs.at(-1); },
            activate() { summary.hidden = false; summary.fire('activate'); },
            deactivate() { summary.hidden = true; summary.fire('deactivate'); },
        };
    }
    function respond(data, index = requests.length - 1, status = 200) { requests[index].options.callback(null, status === 200, { status, responseText: JSON.stringify(data) }); }
    return { Ext, classes, context, requests, timers, dialogs, warnings, page, respond,
        load: () => vm.runInContext(source, context),
        tick() { const active = [...timers]; for (const [id, timer] of active) { timers.delete(id); timer.callback(); } },
    };
}
function sensor(id, category, label, celsius, extra = {}) {
    return { id, chip: id.split('/')[0], label, category, celsius, max_celsius: null, critical_celsius: null, ...extra };
}
function snapshot(sensors = [], changes = {}) { return { version: 1, node: 'pve-test', status: 'ok', sampled_at: 1792286400, sensors, ...changes }; }

test('native status content, height and managed store updates survive independently of Komodo', () => {
    const ui = browser(); ui.load(); ui.load(); const page = ui.page();
    assert.equal(page.status.originalInitCalls, 1);
    assert.equal(page.status.height, 400);
    assert.deepEqual(page.status.items.filter((item) => !item.itemId?.startsWith('pve-node-temperature') && item.xtype !== 'container').map((item) => item.itemId),
        ['cpu', 'memory', 'cpus', 'kernel', 'version', 'repositoryStatus']);
    assert.ok(page.status.items.indexOf(page.status.down('#pve-node-temperature-cpu')) < page.status.items.indexOf(page.status.down('#cpus')));
    assert.equal(ui.requests.length, 1);
    assert.equal(ui.requests[0].options.url, '/pve-plugins/api/temperatures/nodes/pve-test');
    page.activate();
    assert.equal(ui.requests.length, 1, 'afterrender followed by native activate must not issue duplicate reads');
    ui.respond(snapshot([sensor('cpu0/package', 'cpu', 'Package id 0', 48)]));
    assert.match(page.text('cpu'), /48 °C/);
    page.store.fire('load', page.store, [], true);
    assert.equal(page.status.down('#cpu').lastText, 0.25);
    assert.equal(page.status.down('#cpus').lastText, 'Original CPU model');
    assert.match(page.text('cpu'), /48 °C/, 'native store refresh must not erase separately fetched temperatures');
    assert.equal(ui.warnings.length, 0);
});

test('Intel package and AMD Tdie take precedence; multi CPU reports maximum and Tctl is explicit', () => {
    const ui = browser(); ui.load(); const page = ui.page();
    ui.respond(snapshot([sensor('coretemp0/package', 'cpu', 'Package id 0', 50), sensor('coretemp0/core0', 'cpu', 'Core 0', 90),
        sensor('coretemp1/package', 'cpu', 'Package id 1', 55), sensor('coretemp1/core0', 'cpu', 'Core 0', 70)]));
    assert.equal(page.text('cpu'), '最高 55 °C');
    ui.tick(); ui.respond(snapshot([sensor('k10temp/Tctl', 'cpu', 'Tctl', 85), sensor('k10temp/Tdie', 'cpu', 'Tdie', 65)]));
    assert.equal(page.text('cpu'), '65 °C');
    ui.tick(); ui.respond(snapshot([sensor('k10temp/Tctl', 'cpu', 'Tctl', 85)]));
    assert.equal(page.text('cpu'), '85 °C（控制温度）');
});

test('mainboard summary prefers explicit system channels over hotter CPU and chipset readings', () => {
    const ui = browser(); ui.load(); const page = ui.page();
    ui.respond(snapshot([sensor('nct/SYSTIN', 'board', 'SYSTIN', 41), sensor('nct/TSI0', 'board', 'TSI0_TEMP', 83.5),
        sensor('nct/smbus', 'board', 'SMBUSMASTER0', 83.5)]));
    assert.equal(page.text('board'), '41 °C');
    ui.tick(); ui.respond(snapshot([sensor('board/main', 'board', 'Motherboard', 38), sensor('board/cpu', 'board', 'CPU', 50),
        sensor('board/chipset', 'board', 'Chipset', 63)]));
    assert.equal(page.text('board'), '38 °C');
    ui.tick(); ui.respond(snapshot([sensor('board/main', 'board', 'mainboard', 0), sensor('board/cpu', 'board', 'CPU', 50)]));
    assert.equal(page.text('board'), '0 °C', 'an explicit zero-degree system reading is retained');
});

test('fallback summaries name the selected CPU or mainboard channel and encode its label', () => {
    const ui = browser(); ui.load(); const page = ui.page();
    ui.respond(snapshot([sensor('k10temp/Tctl', 'cpu', 'Tctl', 83), sensor('k10temp/Tccd1', 'cpu', 'Tccd1', 64.5),
        sensor('k10temp/Tccd2', 'cpu', 'Tccd2', 68.5), sensor('board/cpu', 'board', 'CPU', 50),
        sensor('board/chipset', 'board', 'Chipset', 63)]));
    assert.equal(page.text('cpu'), '最高 68.5 °C（Tccd2）');
    assert.equal(page.text('board'), '最高 63 °C（Chipset）');
    ui.tick(); ui.respond(snapshot([sensor('coretemp/core', 'cpu', 'Core 0', 52),
        sensor('board/unknown', 'board', '<channel>', 0, { critical_celsius: 90 })]));
    assert.equal(page.text('cpu'), '52 °C（Core 0）');
    assert.equal(page.text('board'), '0 °C（&lt;channel&gt;）');
});

test('unknown channels stay in details; zero is valid; device thresholds alone supply warning colors', () => {
    const ui = browser(); ui.load(); const page = ui.page();
    ui.respond(snapshot([sensor('cpu0/package', 'cpu', 'Package id 0', 95), sensor('nct/temp1', 'other', 'temp1', 100),
        sensor('nvme0/composite', 'disk', 'Composite', 0), sensor('nvme1/composite', 'disk', 'Composite', 45, { max_celsius: 44, critical_celsius: 90 })]));
    assert.equal(page.text('cpu'), '95 °C', 'no hard coded 80 degree warning');
    assert.equal(page.text('board'), '未知');
    assert.match(page.text('disk'), /color:#b56e00.*最高 45 °C（2 块）/);
    let dialog = page.details();
    assert.equal(dialog.down('#pve-node-temperature-grid').getStore().rows.length, 4);
    ui.tick(); ui.respond(snapshot([sensor('board/temp1', 'board', 'System', 0), sensor('cpu/package', 'cpu', 'Package id 0', 95, { critical_celsius: 90 })]));
    assert.equal(page.text('board'), '0 °C');
    assert.match(page.text('cpu'), /color:#d9534f/);
    assert.equal(dialog.down('#pve-node-temperature-grid').getStore().rows.length, 2, 'an open details window follows new samples');
});

test('configuration, unavailable, no-sensor and stale states never display old values as current', () => {
    const ui = browser(); ui.load(); const page = ui.page();
    ui.respond(snapshot([sensor('cpu/package', 'cpu', 'Package id 0', 50)]));
    ui.tick(); ui.respond(snapshot([], { status: 'not_configured', sampled_at: null }));
    assert.equal(page.text('cpu'), '未配置');
    assert.equal(page.status.down('#pve-node-temperature-details').disabled, true);
    ui.tick(); ui.respond(snapshot([], { status: 'unavailable', sampled_at: null })); assert.equal(page.text('cpu'), '不可用');
    ui.tick(); ui.respond(snapshot([], { status: 'no_sensors' })); assert.equal(page.text('cpu'), '未知');
    ui.tick(); ui.respond(snapshot([], { status: 'stale' }));
    assert.equal(page.text('cpu'), '已过期');
    const dialog = page.details();
    assert.match(dialog.down('#pve-node-temperature-detail-status').text, /数据已过期.*采样时间/);
    assert.doesNotMatch(dialog.down('#pve-node-temperature-detail-status').text, /采样时间：未知/);
});

test('details HTML encodes sensor labels and retains actual threshold metadata', () => {
    const ui = browser(); ui.load(); const page = ui.page();
    ui.respond(snapshot([sensor('<chip>/x', 'other', '<img src=x onerror=alert(1)>', -5.5, { max_celsius: 60 })]));
    const grid = page.details().down('#pve-node-temperature-grid');
    const row = grid.getStore().rows[0];
    const rendered = Object.fromEntries(grid.columns.map((column) => [column.dataIndex, column.renderer(row[column.dataIndex], {}, { data: row })]));
    assert.equal(rendered.chip, '&lt;chip&gt;');
    assert.match(rendered.label, /^&lt;img/); assert.doesNotMatch(rendered.label, /<img/);
    assert.equal(rendered.celsius, '-5.5 °C'); assert.equal(rendered.max_celsius, '60 °C'); assert.equal(rendered.critical_celsius, '未知');
});

test('only an active summary polls at ten seconds; deactivate clears values, request, timer and dialog', () => {
    const ui = browser(); ui.load(); const page = ui.page('pve-test', true);
    assert.equal(ui.requests.length, 0);
    page.activate(); assert.equal(ui.requests.length, 1); assert.equal(ui.timers.size, 0, 'no overlapping request interval');
    ui.respond(snapshot([sensor('cpu/package', 'cpu', 'Package id 0', 50)]));
    assert.equal(ui.timers.size, 1); assert.equal([...ui.timers.values()][0].delay, 10000);
    const dialog = page.details(); page.deactivate();
    assert.equal(ui.timers.size, 0); assert.equal(dialog.destroyed, true); assert.equal(page.text('cpu'), '未知');
    page.activate(); const pending = ui.requests.at(-1); page.deactivate(); assert.equal(pending.aborted, true);
    ui.respond(snapshot([sensor('cpu/package', 'cpu', 'Package id 0', 99)]));
    assert.equal(page.text('cpu'), '未知'); assert.equal(ui.timers.size, 0);
    page.activate(); ui.respond(snapshot()); page.status.destroy();
    assert.equal(ui.timers.size, 0); assert.equal(page.store.events.get('load').length, 0, 'native managed listener is released normally');
});

test('transport failures clear prior samples and retry, whereas 401 and 403 stop until reactivated', () => {
    const ui = browser(); ui.load(); const page = ui.page();
    ui.respond(snapshot([sensor('cpu/package', 'cpu', 'Package id 0', 50)]));
    const dialog = page.details(); ui.tick(); ui.respond(null, ui.requests.length - 1, 502);
    assert.equal(page.text('cpu'), '不可用'); assert.equal(dialog.destroyed, true); assert.equal(ui.timers.size, 1);
    ui.tick(); ui.respond(null, ui.requests.length - 1, 401);
    assert.equal(page.text('cpu'), '会话已过期'); assert.equal(ui.timers.size, 0);
    page.activate(); ui.respond(null, ui.requests.length - 1, 403);
    assert.equal(page.text('cpu'), '无访问权限'); assert.equal(ui.timers.size, 0);
});

test('node and user mismatches cannot publish late results and logout tears down active work', () => {
    const ui = browser(); ui.load(); const page = ui.page();
    ui.context.window.Proxmox.UserName = 'other@pam';
    ui.respond(snapshot([sensor('cpu/package', 'cpu', 'Package id 0', 99)]));
    assert.equal(page.text('cpu'), '未知'); assert.equal(ui.timers.size, 0);
    page.activate(); ui.respond(snapshot([], { node: 'wrong-node' })); assert.equal(page.text('cpu'), '不可用');
    ui.tick(); const pending = ui.requests.at(-1);
    assert.equal(page.workspace.onLogin(null), 'native-login-result');
    assert.equal(pending.aborted, true); assert.equal(page.text('cpu'), '请先登录 PVE'); assert.equal(ui.timers.size, 0);
    assert.equal(page.workspace.onLogin({ username: 'root@pam' }), 'native-login-result');
    assert.equal(ui.requests.length, 4); ui.respond(snapshot()); assert.equal(ui.timers.size, 1);
    page.status.pveSelNode.data.node = 'pve-other'; ui.tick(); assert.equal(ui.timers.size, 0); assert.equal(page.text('cpu'), '未知');
});

test('separate nodes keep isolated timers and mounted controls, and unsupported native APIs fail open', () => {
    const ui = browser(); ui.load(); const one = ui.page(), two = ui.page('pve-second');
    ui.respond(snapshot([sensor('cpu/package', 'cpu', 'Package id 0', 41)]), 0);
    ui.respond(snapshot([sensor('cpu/package', 'cpu', 'Package id 0', 62)], { node: 'pve-second' }), 1);
    assert.equal(one.text('cpu'), '41 °C'); assert.equal(two.text('cpu'), '62 °C');
    one.status.destroy(); assert.equal(ui.timers.size, 1); assert.equal(two.text('cpu'), '62 °C');
    const unsupported = browser(); unsupported.classes.delete('PVE.node.StatusView');
    assert.doesNotThrow(() => unsupported.load()); assert.equal(unsupported.requests.length, 0); assert.equal(unsupported.warnings.length, 0);
});

test('a refused optional widget cannot prevent native status initialization', () => {
    const ui = browser(); ui.load();
    ui.classes.get('Ext.panel.Panel').prototype.insert = function () { throw new Error('optional layout refused'); };
    const page = ui.page();
    assert.equal(page.status.originalInitCalls, 1);
    assert.equal(page.status.height, 350);
    assert.equal(page.status._pveNodeTemperatures, undefined);
    assert.equal(page.status.down('#cpus').textField, 'cpuinfo');
    assert.equal(ui.requests.length, 0); assert.equal(ui.timers.size, 0); assert.equal(ui.warnings.length, 1);
});
