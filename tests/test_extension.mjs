import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const extension = readFileSync(new URL('../plugins/komodo/static/extension.js', import.meta.url), 'utf8');
const dockerId = 'pve-komodo-docker';

function treeNode(data) {
    return {
        data: { leaf: true, expanded: false, ...data },
        childNodes: [],
        parentNode: null,
        findChild(field, value) {
            return this.childNodes.find((node) => node.data[field] === value) || null;
        },
        appendChild(child) {
            child.parentNode = this;
            this.childNodes.push(child);
            // Ext.data.NodeInterface.appendChild changes a leaf into a parent.
            this.data.leaf = false;
        },
    };
}

// Model ExtJS's per-method owner/previous metadata, not a global callParent stub.
// That catches a change which accidentally calls the subclass's parent twice or
// loses `this` when overriding an inherited method.
function createExt() {
    const classes = new Map();
    const stack = [];
    const requests = [];
    const guestRequests = [];
    const dialogs = [];
    let definitions = 0;

    function wrap(owner, name, implementation, previous) {
        return function (...args) {
            stack.push({ owner, name, previous, receiver: this });
            try {
                return implementation.apply(this, args);
            } finally {
                stack.pop();
            }
        };
    }

    function define(name, definition) {
        definitions++;
        if (definition.override) {
            const target = classes.get(definition.override);
            for (const [method, implementation] of Object.entries(definition)) {
                if (typeof implementation === 'function') {
                    const previous = Object.hasOwn(target.prototype, method) ?
                        target.prototype[method] : undefined;
                    target.prototype[method] = wrap(target, method, implementation, previous);
                }
            }
            // ExtJS createOverride applies methods without registering an ordinary
            // class under the override name in ClassManager.classes.
            return target;
        }

        const parent = definition.extend && classes.get(definition.extend);
        function Component(config = {}) {
            Object.assign(this, config);
        }
        Component.superclass = parent && parent.prototype;
        Component.prototype = Object.create(parent ? parent.prototype : null);
        Component.prototype.constructor = Component;
        Component.prototype.callParent = function (args = []) {
            const frame = stack.at(-1);
            assert.equal(frame.receiver, this, 'parent must use the same component');
            const parentMethod = frame.previous || frame.owner.superclass[frame.name];
            return parentMethod.apply(this, args);
        };
        for (const [method, implementation] of Object.entries(definition)) {
            if (typeof implementation === 'function') {
                Component.prototype[method] = wrap(Component, method, implementation);
            }
        }
        classes.set(name, Component);
        return Component;
    }

    define('Ext.panel.Panel', {
        initComponent: function () {
            this.panelInitialized = true;
            return 'panel-initialized';
        },
    });

    // Preserve the relevant PVE 9 lifecycle and grouping semantics from fixed
    // pve-manager aa7db891 dc/Config.js and panel/ConfigPanel.js. Groups contain
    // parent itemIds, are consumed via shift(), and require parents first.
    define('PVE.panel.Config', {
        extend: 'Ext.panel.Panel',
        initComponent: function () {
            this.savedItems = {};
            this.root = treeNode({ id: 'root', expanded: true });
            this.navigation = this.root.childNodes;
            this.insertNodes(this.items);
            delete this.items;
            return this.callParent(arguments);
        },
        insertNodes: function (items) {
            this.baseInsertCalls = (this.baseInsertCalls || 0) + 1;
            if (!Array.isArray(items)) {
                return items;
            }
            for (const item of items) {
                assert.equal(this.savedItems[item.itemId], undefined, 'PVE rejects duplicate ids');
                item.header = false;
                this.savedItems[item.itemId] = item;
                let parent = this.root;
                while (Array.isArray(item.groups) && item.groups.length > 0) {
                    const child = parent.findChild('id', item.groups.shift());
                    if (child === null) break;
                    parent = child;
                }
                assert.equal(parent.findChild('id', item.itemId), null);
                parent.appendChild(treeNode({
                    id: item.itemId, text: item.title, iconCls: item.iconCls,
                    expanded: item.expandedOnInit === true,
                }));
            }
            return items;
        },
        activateCard: function (id) {
            this.activeCard = this.savedItems[id];
            return this.activeCard;
        },
    });

    for (const [name, originalId] of [
        ['PVE.dc.Config', 'summary'],
        ['PVE.qemu.Config', 'hardware'],
        ['PVE.lxc.Config', 'resources'],
    ]) {
        define(name, {
            extend: 'PVE.panel.Config',
            initComponent: function () {
                this.items = this.sourceItems || [{ xtype: 'panel', itemId: originalId, title: originalId }];
                this.originalItems = this.items.slice();
                return this.callParent(arguments);
            },
        });
    }

    return {
        requests,
        guestRequests,
        dialogs,
        Msg: { YESNO: 'yesno', WARNING: 'warning' },
        Ajax: {
            request(options) { (options.url.startsWith('/pve-komodo/api/guest-links') ? guestRequests : requests).push(options); return { options }; },
            abort(request) {
                request.options.aborted = true;
                request.options.callback(request.options, false, { status: 0, responseText: '' });
            },
        },
        String: {
            htmlEncode: (value) => String(value).replace(/[&<>"']/g, (character) => ({
                '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
            })[character]),
        },
        Date: { format: (date) => date.toISOString().slice(0, 19).replace('T', ' ') },
        ClassManager: { get: (name) => classes.get(name) },
        define,
        get definitions() { return definitions; },
        create(name, config) {
            if (name === 'Ext.window.Window') {
                let window;
                const children = new Map();
                const items = [...(config.items || []), ...(config.buttons || [])].map((item) => {
                    const child = {
                        ...item, destroyed: false, value: item.xtype === 'checkbox' ? item.checked : item.value,
                        getValue() { return this.xtype === 'combobox' && (this.value === '' || this.value === undefined) ? null : this.value; },
                        setValue(value) {
                            this.value = this.xtype === 'numberfield' && typeof value === 'number' ? Number(value.toFixed(2)) :
                                ['textfield', 'textareafield'].includes(this.xtype) ? String(value) : value;
                            if (this.listeners?.change) this.listeners.change(this);
                        },
                        setDisabled(value) { assert.equal(this.destroyed, false); this.disabled = value; },
                        update(html) { assert.equal(this.destroyed, false); this.html = html; },
                        up(selector) { return selector === 'window' ? window : undefined; },
                    };
                    if (item.store) {
                        const store = { data: item.store.data || [], loadData(data) { assert.equal(child.destroyed, false); this.data = data; },
                            indexOf(record) { return this.data.indexOf(record.data || record); } };
                        child.getStore = () => store;
                        const selection = { selected: [], getSelection() { return this.selected; }, select(row) {
                            this.selected = [{ data: row, get: (name) => row[name] }];
                        } };
                        child.getSelectionModel = () => selection;
                        child.tbar = (item.tbar || []).map((button) => ({ ...button, up: () => child }));
                    }
                    children.set('#' + item.itemId, child);
                    return child;
                });
                return window = {
                    ...config, items, destroyed: false,
                    down: (selector) => children.get(selector),
                    show() { dialogs.push(this); return this; },
                    close() { if (this.listeners?.beforeclose?.() !== false) this.destroy(); },
                    destroy() {
                        if (this.destroyed || this.destroying) return;
                        this.destroying = true;
                        if (this.listeners?.beforedestroy) this.listeners.beforedestroy(this);
                        this.destroyed = true;
                        items.forEach((item) => { item.destroyed = true; });
                    },
                };
            }
            if (name === 'Ext.window.MessageBox') {
                return {
                    destroyed: false,
                    show(options) { this.options = options; dialogs.push(this); return this; },
                    destroy() { this.destroyed = true; },
                };
            }
            const component = new (classes.get(name))(config);
            component.initializationResult = component.initComponent();
            return component;
        },
    };
}

function runtime(Ext = createExt()) {
    const warnings = [];
    const forbiddenRequest = () => { throw new Error('blank pages must not make requests'); };
    const context = vm.createContext({
        window: {
            Ext,
            crypto: { randomUUID: () => 'ceaa63ca-3693-4ef0-8cbf-4de83911a76b' },
            Proxmox: { Utils: { format_size: (value) => value + ' B' } },
            console: { warn: (...args) => warnings.push(args) },
        },
        fetch: forbiddenRequest,
        XMLHttpRequest: forbiddenRequest,
    });
    return { Ext, warnings, context, load: () => vm.runInContext(extension, context) };
}

function datacenterItems() {
    return [
        { xtype: 'panel', itemId: 'summary', title: 'Summary' },
        { xtype: 'panel', itemId: 'ceph', title: 'Ceph' },
        { xtype: 'panel', itemId: 'options', title: 'Options' },
        { xtype: 'panel', itemId: 'permissions', title: 'Permissions', expandedOnInit: true },
        { xtype: 'panel', itemId: 'users', title: 'Users', groups: ['permissions'] },
    ];
}

const children = [
    ['stats', '统计', '/stats'], ['containers', '容器', '/containers'], ['alerts', '告警', '/alerts'],
    ['updates', '操作', '/updates'], ['schedules', '计划任务', '/schedules'],
];
const menuIds = (nodes) => nodes.map((node) => node.data.id);

function assertBlank(card) {
    assert.equal(card.xtype, 'panel');
    assert.equal(card.header, false);
    assert.equal(card.border, false);
    assert.equal(card.items.length, 0);
    for (const property of ['html', 'loader', 'listeners', 'tbar', 'bbar', 'dockedItems',
        'buttons', 'store', 'href', 'src', 'url', 'autoEl']) {
        assert.equal(card[property], undefined, `blank card must not have ${property}`);
    }
}

test('Docker is directly below Ceph with five ordered children and a clickable native dashboard', () => {
    const browser = runtime();
    browser.load();
    const originals = datacenterItems();
    const dc = browser.Ext.create('PVE.dc.Config', { sourceItems: originals });
    assert.deepEqual(menuIds(dc.navigation), ['summary', 'ceph', dockerId, 'options', 'permissions']);
    const docker = dc.root.findChild('id', dockerId);
    assert.equal(docker.data.text, 'Docker');
    assert.equal(docker.parentNode, dc.root, 'Docker is beside Ceph, not inside it');
    assert.equal(docker.data.leaf, false, 'appending children makes the parent expandable');
    assert.equal(docker.data.expanded, true);
    assert.deepEqual(menuIds(docker.childNodes), children.map(([id]) => dockerId + '-' + id));
    assert.deepEqual(docker.childNodes.map((node) => node.data.text), children.map(([, title]) => title));
    for (const node of docker.childNodes) {
        assert.equal(node.parentNode, docker);
        assert.equal(node.data.leaf, true);
    }
    assert.equal(dc.activateCard(dockerId), dc.savedItems[dockerId], 'parent retains its own page');
    assert.deepEqual(Array.from(dc.activeCard.items, (grid) => grid.title), ['服务器', '应用栈', '流程']);
    assert.equal(browser.Ext.requests.length, 0, 'menu registration does not load dashboard data');
    for (const [suffix] of children) {
        const card = dc.activateCard(dockerId + '-' + suffix);
        if (!['containers', 'alerts', 'updates'].includes(suffix)) assertBlank(card);
        else assert.equal(card.items[0].xtype, 'gridpanel');
    }
    for (const item of originals) {
        assert.equal(dc.savedItems[item.itemId], item, 'keep official card identity');
    }
    assert.deepEqual(menuIds(dc.root.findChild('id', 'permissions').childNodes), ['users']);
    assert.equal(dc.items, undefined, 'normal PVE lifecycle still consumes items');
    assert.equal(dc.baseInsertCalls, 1);
    assert.equal(dc.panelInitialized, true);
    assert.equal(dc.initializationResult, 'panel-initialized');
});

test('every blank page records its future Komodo route without navigation or content', () => {
    const browser = runtime();
    browser.load();
    const dc = browser.Ext.create('PVE.dc.Config');
    assert.equal(dc.savedItems[dockerId].pveKomodoRoute, '/');
    assert.equal(dc.savedItems[dockerId + '-servers'], undefined);
    assert.equal(dc.savedItems[dockerId + '-stacks'], undefined);
    for (const [suffix, title, route] of children) {
        const card = dc.savedItems[dockerId + '-' + suffix];
        assert.equal(card.title, title);
        assert.equal(card.pveKomodoRoute, route);
        if (!['containers', 'alerts', 'updates'].includes(suffix)) assertBlank(card);
        assert.equal(card.groups.length, 0, 'PVE consumes each own groups array');
    }
});

test('when Ceph is unavailable Docker is appended without rearranging existing menus', () => {
    const browser = runtime();
    browser.load();
    const originals = datacenterItems().filter((item) => item.itemId !== 'ceph');
    const dc = browser.Ext.create('PVE.dc.Config', { sourceItems: originals });
    assert.deepEqual(menuIds(dc.navigation), ['summary', 'options', 'permissions', dockerId]);
    assert.equal(dc.root.findChild('id', dockerId).childNodes.length, 5);
});

test('VM and CT menus with no guest context retain their original cards', () => {
    const browser = runtime();
    browser.load();
    for (const [name, originalId] of [['PVE.qemu.Config', 'hardware'], ['PVE.lxc.Config', 'resources']]) {
        const component = browser.Ext.create(name);
        assert.deepEqual(menuIds(component.navigation), [originalId]);
        assert.equal(component.savedItems[dockerId], undefined);
        assert.equal(component.baseInsertCalls, 1);
    }
});

test('reloading registers once and separate datacenters receive fresh group arrays and cards', () => {
    const browser = runtime();
    const definitions = browser.Ext.definitions;
    browser.load();
    browser.load();
    assert.equal(browser.Ext.definitions, definitions + 3);
    const first = browser.Ext.create('PVE.dc.Config');
    const second = browser.Ext.create('PVE.dc.Config');
    for (const dc of [first, second]) {
        assert.equal(dc.navigation.filter((node) => node.data.id === dockerId).length, 1);
        assert.equal(dc.root.findChild('id', dockerId).childNodes.length, 5);
    }
    assert.notEqual(first.savedItems[dockerId], second.savedItems[dockerId]);
    assert.notEqual(first.savedItems[dockerId + '-stats'].groups, second.savedItems[dockerId + '-stats'].groups);
});

test('an existing reserved parent or child id leaves the original menu untouched', () => {
    for (const reservedId of [dockerId, dockerId + '-stats']) {
        const browser = runtime();
        browser.load();
        const originals = datacenterItems();
        originals.push({ xtype: 'panel', itemId: reservedId, title: 'Existing extension' });
        const dc = browser.Ext.create('PVE.dc.Config', { sourceItems: originals });
        assert.deepEqual(Object.keys(dc.savedItems), originals.map((item) => item.itemId));
        assert.equal(dc.savedItems[reservedId], originals.at(-1));
    }
});

test('the override preserves parent return values and unknown inputs', () => {
    const browser = runtime();
    browser.load();
    const dc = browser.Ext.create('PVE.dc.Config');
    const unknownItems = { unknown: true };
    assert.equal(dc.insertNodes(unknownItems), unknownItems);
    assert.equal(dc.baseInsertCalls, 2);
});

test('the method works with ExtJS caller-based callParent in an ordinary browser script', () => {
    let parentReceiver;
    function Datacenter() {}
    const parentInsertNodes = function (items) {
        parentReceiver = this;
        return items;
    };
    Datacenter.prototype.insertNodes = parentInsertNodes;
    // Function() produces a non-strict function even though this test is an ESM.
    // Like Ext.Base.callParent, strict overriding callers cannot be resolved.
    Datacenter.prototype.callParent = Function('args', `
        var method = this.callParent.caller;
        if (!method) throw new Error('ExtJS cannot resolve a strict overriding caller');
        return (method.$previous || method.$owner.superclass[method.$name]).apply(this, args);
    `);
    const browser = runtime({
        ClassManager: { get: (name) => name === 'PVE.dc.Config' ? Datacenter : undefined },
        define(name, definition) {
            const method = definition.insertNodes;
            method.$owner = { superclass: { insertNodes: parentInsertNodes } };
            method.$name = 'insertNodes';
            Datacenter.prototype.insertNodes = method;
        },
    });
    browser.load();
    const dc = new Datacenter();
    const items = dc.insertNodes([{ itemId: 'summary' }]);
    assert.equal(parentReceiver, dc);
    assert.equal(items.length, 7);
    assert.equal(items[1].itemId, dockerId);
});

test('missing or changed PVE interfaces leave the original page untouched', () => {
    for (const Ext of [undefined, {}, { ClassManager: { get: () => ({}) }, define() {
        assert.fail('changed PVE constructor must not register an override');
    } }, { ClassManager: { get: () => undefined }, define() {
        assert.fail('unknown PVE must not register an override');
    } }]) {
        const browser = runtime(Ext === undefined ? null : Ext);
        assert.doesNotThrow(browser.load);
        assert.equal(browser.warnings.length, 0);
    }
});

test('ExtJS refusing registration does not prevent the PVE page from starting', () => {
    const Ext = createExt();
    Ext.define = () => { throw new Error('simulated incompatible class registry'); };
    const browser = runtime(Ext);
    assert.doesNotThrow(browser.load);
    assert.equal(browser.warnings.length, 1);
    const dc = Ext.create('PVE.dc.Config');
    assert.deepEqual(menuIds(dc.navigation), ['summary']);
});

function dashboard(browser = runtime(), page = 'dashboard', suppliedConfig = null, owner = null) {
    browser.load();
    const dc = browser.Ext.create('PVE.dc.Config');
    const config = suppliedConfig || dc.savedItems[page === 'dashboard' ? dockerId : dockerId + '-' + page];
    const components = new Map();
    const allComponents = [];
    const panel = {
        ...config,
        pveKomodoPage: config.pveKomodoPage,
        pveKomodoGuestOptions: config.pveKomodoGuestOptions,
        pveKomodoOptionsPath: config.pveKomodoOptionsPath,
        pveKomodoGuestSummary: config.pveKomodoGuestSummary,
        pveKomodoGuestBase: config.pveKomodoGuestBase,
        destroyed: false,
        down: (selector) => components.get(selector),
        up: (selector) => selector === '[pveKomodoStackPage]' && (owner || panel).pveKomodoStackPage ? owner || panel :
            selector === '[pveKomodoContainerPage]' && (owner || panel).pveKomodoContainerPage ? owner || panel :
            selector === '[pveKomodoServerTerminalPage]' && (owner || panel).pveKomodoServerTerminalPage ? owner || panel :
            selector === '[workspace]' && (owner || panel).workspace ? owner || panel : undefined,
    };
    function store() {
        return {
            data: [], destroyed: false, updateEvents: 0,
            loadData(rows) {
                assert.equal(this.destroyed, false);
                this.data = rows;
            },
            each(fn) { this.data.forEach((row) => fn({ get: (name) => row[name], set: (name, value) => { if (row[name] !== value) this.updateEvents++; row[name] = value; } })); },
            findRecord(field, value) { const row = this.data.find((row) => row[field] === value); return row && { get: (name) => row[name] }; },
        };
    }
    function component(options, ownerGrid = null) {
        const result = {
            ...options, destroyed: false, mutations: 0,
            value: options.xtype === 'checkbox' ? options.checked : options.value || '',
            disabled: !!options.disabled,
            up: (selector) => selector === '[pveKomodoStackPage]' && (owner || panel).pveKomodoStackPage ? owner || panel :
                selector === '[pveKomodoContainerPage]' && (owner || panel).pveKomodoContainerPage ? owner || panel :
                selector === '[pveKomodoServerTerminalPage]' && (owner || panel).pveKomodoServerTerminalPage ? owner || panel :
                ['[pveKomodoPage]', '[pveKomodoGuestOptions]', '[pveKomodoGuestSummary]', '[pveKomodoGuestInventory]', '[pveKomodoStackTab]', '[pveKomodoContainerTab]', '#' + config.itemId].includes(selector) ? panel : undefined,
            getEl: () => ({ dom: {} }),
            getValue() { return this.value; },
            setValue(value) {
                assert.equal(this.destroyed, false);
                const changed = value !== this.value;
                this.value = value;
                if (changed && this.listeners?.change) this.listeners.change(this);
            },
            setDisabled(value) { assert.equal(this.destroyed, false); this.disabled = value; this.mutations++; },
            setText(value) { assert.equal(this.destroyed, false); this.text = value; this.mutations++; },
            setTitle(value) { assert.equal(this.destroyed, false); this.title = value; this.mutations++; },
            update(value) { assert.equal(this.destroyed, false); this.html = value; this.mutations++; },
            updateValue(value, usage) { assert.equal(this.destroyed, false); this.text = value; this.usage = usage; this.mutations++; },
        };
        if (options.store) {
            const dataStore = store();
            result.getStore = () => dataStore;
        }
        if (options.xtype === 'gridpanel') {
            const model = {
                selected: [],
                getSelection() { return this.selected; },
                select(rows) {
                    rows = Array.isArray(rows) ? rows : [rows];
                    this.selected = rows.map((row) => typeof row.get === 'function' ? row : { get: (name) => row[name] })
                        .filter((record) => result.selModel?.listeners?.beforeselect?.(this, record) !== false);
                    if (result.listeners.selectionchange) result.listeners.selectionchange.call(result, this, this.selected);
                },
                deselectAll() {
                    if (this.selected.length) {
                        this.selected = [];
                        if (result.listeners.selectionchange) result.listeners.selectionchange.call(result, this, this.selected);
                    }
                },
            };
            result.getSelectionModel = () => model;
            const buttons = new Map();
            [...(Array.isArray(options.tbar) ? options.tbar : options.tbar?.items || []), ...(options.bbar || [])].forEach((options) => {
                const button = component(options, result);
                if (options.itemId) buttons.set('#' + options.itemId, button);
            });
            result.down = (selector) => buttons.get(selector);
        }
        if (!ownerGrid || result.itemId?.startsWith('pve-komodo-guest-')) components.set('#' + result.itemId, result);
        if (options.xtype !== 'gridpanel') {
            (Array.isArray(options.tbar) ? options.tbar : options.tbar?.items || []).filter((item) => item.itemId).forEach((item) => component(item));
            (options.bbar || []).filter((item) => item.itemId).forEach((item) => component(item));
        }
        (options.items || []).forEach((item) => component(item));
        (options.tools || []).forEach((item) => component(item));
        allComponents.push(result);
        return result;
    }
    (Array.isArray(config.tbar) ? config.tbar : config.tbar?.items || []).filter((item) => item.itemId).forEach((options) => component(options));
    (config.bbar || []).forEach((options) => component(options));
    config.items.forEach((options) => component(options));
    return {
        browser, config, panel, components,
        control: (name) => panel.down('#pve-komodo-' + name),
        grid: (section) => panel.down('#pve-komodo-' + section + '-grid'),
        activate: () => config.listeners.activate(panel),
        deactivate: () => config.listeners.deactivate(panel),
        refresh() { const button = this.control('refresh'); button.handler(button); },
        select(section, ids) {
            const grid = this.grid(section);
            grid.getSelectionModel().select(grid.getStore().data.filter((row) => ids.includes(row.id)));
        },
        action(section, name) { return this.grid(section).down('#pve-komodo-action-' + name); },
        click(section, name) {
            const button = this.action(section, name);
            if (!button.disabled) button.handler(button);
        },
        answer(value) { this.browser.Ext.dialogs.at(-1).options.fn(value); },
        destroy() {
            config.listeners.beforedestroy(panel);
            panel.destroyed = true;
            allComponents.forEach((child) => {
                child.destroyed = true;
                if (child.getStore) child.getStore().destroyed = true;
            });
        },
        respond(payload, code = 200, index = browser.Ext.requests.length - 1) {
            const request = browser.Ext.requests[index];
            request.callback(request, code >= 200 && code < 300, {
                status: code,
                responseText: typeof payload === 'string' ? payload : JSON.stringify(payload),
            });
        },
    };
}

function sampleDashboard() {
    return {
        user: 'root@pam',
        csrf_token: 'csrf-test-token',
        servers: [{
            id: 's1', name: 'alpha', state: 'Ok', cpu_percent: 0, memory_percent: null,
            disk_percent: 66.5, load_average: [1.23, 0.9, 0.8], core_count: 4,
            network_bytes_per_second: 1024, version: '2.3.3', tags: ['prod'], template: false,
        }, {
            id: 's2', name: 'beta', state: 'Disabled', cpu_percent: null, memory_percent: null,
            disk_percent: null, load_average: null, core_count: null, network_bytes_per_second: null,
            version: null, tags: ['staging'], template: true,
        }],
        stacks: [{ id: 'a1', name: 'alpha-app', source: 'repository', server_id: 's1',
            server_name: 'alpha', state: 'running', tags: ['prod'], template: false }],
        procedures: [{ id: 'p1', name: 'deploy', state: 'Ok', next_run_at: 1790000000123,
            tags: ['prod'], template: false }],
        updated_at: '2026-10-17T10:20:30Z', errors: [],
    };
}

test('the native dashboard creates isolated sortable, collapsible grids with percentage widgets', () => {
    const first = dashboard();
    const second = dashboard();
    assert.equal(first.browser.Ext.requests.length, 0);
    assert.deepEqual(Array.from(first.config.items, (grid) => grid.title), ['服务器', '应用栈', '流程']);
    for (const section of ['servers', 'stacks', 'procedures']) {
        const grid = first.grid(section);
        assert.equal(grid.xtype, 'gridpanel');
        assert.equal(grid.collapsible, true);
        assert.equal(grid.store.autoDestroy, true);
        assert.notEqual(grid.store, second.grid(section).store, 'fresh store config for every lazy card');
        assert.equal(grid.store.sorters[0].property, 'name');
        assert.ok(grid.columns.every((column) => column.dataIndex && (column.sortable !== false || column.dataIndex === 'pve_guest')));
    }
    const cpu = first.grid('servers').columns.find((column) => column.dataIndex === 'cpu_percent_value');
    assert.equal(cpu.xtype, 'widgetcolumn');
    assert.equal(cpu.widget.xtype, 'progressbarwidget');
    assert.equal(cpu.widget.cls, 'pve-komodo-usage-progress');
    assert.equal(cpu.widget.bind, undefined);
    assert.equal(typeof cpu.onWidgetAttach, 'function');
    assert.equal(first.control('tag').listConfig.itemTpl, '{label:htmlEncode}');
});

test('server count columns render run and stop separately, preserve zero, and keep unknown values unknown', () => {
    const grid = dashboard().grid('servers');
    const containers = grid.columns.find((column) => column.dataIndex === 'container_count');
    const resources = grid.columns.find((column) => column.dataIndex === 'resource_count');
    const record = (running, stopped, other) => ({ get: (key) => ({
        containers_running: running, containers_stopped: stopped, containers_other: other,
    })[key] });
    assert.equal(containers.text, '容器数量');
    assert.equal(resources.text, '资源数量');
    assert.equal(containers.renderer(1, {}, record(1, 0, 0)), '1 run 0 stop');
    assert.equal(containers.renderer(0, {}, record(0, 0, 0)), '0 run 0 stop');
    assert.equal(containers.renderer(3, {}, record(1, 1, 1)), '1 run 1 stop 1 other');
    assert.equal(containers.renderer(null, {}, record(null, null, null)), '-');
    assert.equal(containers.renderer(2, {}, record(1, 0, 0)), '-');
    assert.equal(containers.renderer(1, {}, record('1', 0, 0)), '-');
    assert.equal(resources.renderer(0), '0');
    assert.equal(resources.renderer(3), '3');
    assert.equal(resources.renderer(null), '-');
    assert.equal(resources.renderer('3'), '-');
    assert.match(resources.tooltip, /应用栈、部署和代码仓库/);
});

test('stack metadata columns distinguish zero services, disabled auto update, and unknown values', () => {
    const grid = dashboard().grid('stacks');
    const count = grid.columns.find((column) => column.dataIndex === 'service_count');
    const update = grid.columns.find((column) => column.dataIndex === 'auto_update');
    assert.equal(count.text, '服务数量');
    assert.equal(update.text, '自动更新');
    assert.ok(grid.store.fields.includes('service_count'));
    assert.ok(grid.store.fields.includes('auto_update'));
    assert.equal(count.renderer(0), '0');
    assert.equal(count.renderer(4), '4');
    for (const value of [null, undefined, -1, 1.5, '4', false]) assert.equal(count.renderer(value), '-');
    assert.equal(update.renderer(true), '已启用');
    assert.equal(update.renderer(false), '已禁用');
    for (const value of [null, undefined, 'true', 0, 1]) assert.equal(update.renderer(value), '未知');
    assert.equal(update.xtype, undefined, 'status is read-only text, not an editable checkbox');
});

test('progress widgets receive explicit values and text on attachment and reset when recycled for missing metrics', () => {
    const card = dashboard();
    for (const field of ['cpu_percent', 'memory_percent', 'disk_percent']) {
        const column = card.grid('servers').columns.find((entry) => entry.dataIndex === field + '_value');
        const widget = {
            value: 1, text: 'stale value',
            setValue(value) { this.value = value; },
            setText(value) { this.text = value; },
        };
        const record = (value, label) => ({ get: (name) => (
            name === field + '_value' ? value : name === field + '_text' ? label : undefined
        ) });
        column.onWidgetAttach(column, widget, record(0.375, '37.5%'));
        assert.equal(widget.value, 0.375);
        assert.equal(widget.text, '37.5%');
        column.onWidgetAttach(column, widget, record(null, '-'));
        assert.equal(widget.value, 0, 'recycled bar is empty instead of retaining an old percentage');
        assert.equal(widget.text, '-', 'unknown metric is not displayed as zero percent');
        column.onWidgetAttach(column, widget, record(0, '0.0%'));
        assert.equal(widget.value, 0);
        assert.equal(widget.text, '0.0%', 'real zero remains distinguishable from unknown');
    }
});

test('native widget updates after a row change keep normalized progress and original percentage sorting', () => {
    const card = dashboard();
    card.activate();
    const payload = sampleDashboard();
    Object.assign(payload.servers[0], {cpu_percent: 2, memory_percent: 44.6, disk_percent: 60.1});
    card.respond(payload);
    const row = card.grid('servers').getStore().data[0];
    const record = {get: (key) => row[key]};
    for (const field of ['cpu_percent', 'memory_percent', 'disk_percent']) {
        const column = card.grid('servers').columns.find((entry) => entry.dataIndex === field + '_value');
        assert.equal(column.sorter.property, field, 'sort using the original percentage, including values above 100');
        const widget = {
            defaultBindProperty: 'value',
            setValue(value) { this.value = value; },
            setText(value) { this.text = value; },
            // ExtJS ProgressBase.applyValue maps null to zero.
            setConfig(property, value) { this[property] = value || 0; },
        };
        column.onWidgetAttach(column, widget, record);
        // ExtJS WidgetColumn.updateWidget does this on an unrelated record.set,
        // without invoking the attachment hook again (e.g. async guest links).
        widget.setConfig(widget.defaultBindProperty, record.get(column.dataIndex));
        assert.equal(widget.value, row[field] / 100);
        assert.ok(widget.value > 0 && widget.value < 1, 'partial usage stays a partial fill');
        assert.equal(widget.text, row[field].toFixed(1) + '%');
        row[field + '_value'] = null;
        widget.setConfig(widget.defaultBindProperty, record.get(column.dataIndex));
        assert.equal(widget.value, 0, 'missing metric remains an empty bar on later updates');
    }
});

test('memory and disk labels include used / total and preserve missing values and real zero', () => {
    const card = dashboard();
    card.activate();
    const payload = sampleDashboard();
    Object.assign(payload.servers[0], {
        memory_percent: 25, memory_used_bytes: 1024, memory_total_bytes: 4096,
        disk_percent: 0, disk_used_bytes: 0, disk_total_bytes: 8192,
    });
    card.respond(payload);
    const row = card.grid('servers').getStore().data[0];
    assert.equal(row.memory_percent_text, '25.0% (1024 B / 4096 B)');
    assert.equal(row.disk_percent_text, '0.0% (0 B / 8192 B)');
    for (const resource of ['memory', 'disk']) {
        const column = card.grid('servers').columns.find((entry) => entry.dataIndex === resource + '_percent_value');
        assert.ok(column.width >= 250, 'capacity text has visible space within the native progress column');
        assert.ok(card.grid('servers').store.fields.includes(resource + '_used_bytes'));
        assert.ok(card.grid('servers').store.fields.includes(resource + '_total_bytes'));
    }
    card.refresh();
    Object.assign(payload.servers[0], {
        memory_percent: null, memory_used_bytes: 0, memory_total_bytes: null,
        disk_percent: null, disk_used_bytes: null, disk_total_bytes: null,
    });
    card.respond(payload);
    assert.equal(card.grid('servers').getStore().data[0].memory_percent_text, '- (0 B / -)');
    assert.equal(card.grid('servers').getStore().data[0].disk_percent_text, '-');
});

test('only an active parent loads the read-only API and reactivation or explicit refresh can reload', () => {
    const card = dashboard();
    card.refresh();
    assert.equal(card.browser.Ext.requests.length, 0);
    card.activate();
    card.refresh();
    assert.equal(card.browser.Ext.requests.length, 1, 'pending load is not duplicated');
    const request = card.browser.Ext.requests[0];
    assert.equal(request.url, '/pve-komodo/api/dashboard');
    assert.equal(request.method, 'GET');
    assert.equal(request.headers, undefined);
    assert.equal(request.jsonData, undefined);
    assert.equal(card.control('refresh').disabled, true);
    card.respond(sampleDashboard());
    assert.equal(card.control('refresh').disabled, false);
    assert.equal(card.grid('servers').getStore().data.length, 1, 'templates excluded by default');
    assert.equal(card.grid('stacks').getStore().data.length, 1);
    assert.equal(card.grid('procedures').getStore().data.length, 1);
    assert.match(card.control('dashboard-status').text, /^已更新：/);
    card.refresh();
    assert.equal(card.browser.Ext.requests.length, 2);
    assert.equal(card.grid('servers').getStore().data.length, 0, 'old snapshot cleared before refresh');
    card.respond(sampleDashboard());
    card.deactivate();
    card.activate();
    assert.equal(card.browser.Ext.requests.length, 3);
});

test('search, tag selection, and excluding templates filter all three native stores locally', () => {
    const card = dashboard();
    card.activate();
    card.respond(sampleDashboard());
    card.control('exclude-templates').setValue(false);
    assert.equal(card.grid('servers').getStore().data.length, 2);
    card.control('search').setValue('ALPHA');
    assert.equal(card.grid('servers').getStore().data[0].id, 's1');
    assert.equal(card.grid('stacks').getStore().data[0].id, 'a1');
    assert.equal(card.grid('procedures').getStore().data.length, 0);
    card.control('search').setValue('正常');
    assert.equal(card.grid('servers').getStore().data[0].id, 's1');
    assert.equal(card.grid('procedures').getStore().data[0].id, 'p1');
    card.control('search').setValue('');
    card.control('tag').setValue('staging');
    assert.equal(card.grid('servers').getStore().data[0].id, 's2');
    assert.equal(card.grid('stacks').getStore().data.length, 0);
    card.control('exclude-templates').setValue(true);
    assert.equal(card.grid('servers').getStore().data.length, 0);
    assert.equal(card.browser.Ext.requests.length, 1, 'all filtering is local');
});

test('missing metrics remain missing while zero, percent bars, rates, and epoch milliseconds render correctly', () => {
    const card = dashboard();
    card.activate();
    card.respond(sampleDashboard());
    const server = card.panel._pveKomodoRows.servers[0];
    assert.equal(server.cpu_percent_value, 0);
    assert.equal(server.cpu_percent_text, '0.0%');
    assert.equal(server.memory_percent, null);
    assert.equal(server.memory_percent_value, null);
    assert.equal(server.memory_percent_text, '-');
    assert.equal(server.disk_percent_value, 0.665);
    const column = (section, name) => card.grid(section).columns.find((entry) => entry.dataIndex === name);
    assert.equal(column('servers', 'network_bytes_per_second').renderer(null), '-');
    assert.equal(column('servers', 'network_bytes_per_second').renderer(0), '0 B/s');
    assert.equal(column('servers', 'network_bytes_per_second').renderer(1024), '1024 B/s');
    assert.equal(column('servers', 'load_average').renderer(null), '-');
    assert.equal(column('servers', 'load_average').renderer([1.2, null, 3]), '1.20 / - / 3.00');
    assert.equal(column('procedures', 'next_run_at').renderer(null), '-');
    assert.equal(column('procedures', 'next_run_at').renderer(1790000000123), '2026-09-21 14:13:20');
});

test('all external cell text is HTML-encoded and partial errors expose no raw details', () => {
    const card = dashboard();
    card.activate();
    const payload = sampleDashboard();
    payload.errors = ['PRIVATE_ERROR https://private.invalid'];
    card.respond(payload);
    for (const section of ['servers', 'stacks', 'procedures']) {
        for (const field of ['name', 'state', 'tags', 'source', 'server_name', 'version']) {
            const column = card.grid(section).columns.find((entry) => entry.dataIndex === field);
            if (!column) continue;
            const value = '<img onerror="run()"> & test';
            assert.equal(column.renderer(field === 'tags' ? [value] : value),
                '&lt;img onerror=&quot;run()&quot;&gt; &amp; test');
        }
    }
    assert.match(card.control('dashboard-status').text, /部分数据不可用/);
    assert.doesNotMatch(card.control('dashboard-status').text, /PRIVATE|private\.invalid/);
});

test('authentication, unconfigured, upstream, and malformed failures clear stale data and permit retry', () => {
    for (const [code, body, expected] of [
        [401, { detail: 'PRIVATE' }, 'PVE 未登录或会话已过期'],
        [403, { detail: 'PRIVATE' }, '无访问权限'],
        [503, { detail: 'PRIVATE' }, 'PVE 会话验证或集成服务暂不可用'],
        [503, { detail: 'not_configured' }, 'Komodo 未配置凭据'],
        [502, { detail: 'PRIVATE' }, 'Docker 数据加载失败，请重试'],
        [200, { servers: [], stacks: 'PRIVATE', procedures: [] }, 'Docker 数据加载失败，请重试'],
        [200, 'malformed PRIVATE', 'Docker 数据加载失败，请重试'],
    ]) {
        const card = dashboard();
        card.activate();
        card.respond(sampleDashboard());
        card.refresh();
        assert.equal(card.grid('servers').getStore().data.length, 0);
        card.respond(body, code);
        assert.equal(card.control('dashboard-status').text, expected);
        for (const section of ['servers', 'stacks', 'procedures']) {
            assert.equal(card.grid(section).getStore().data.length, 0);
        }
        assert.equal(card.control('refresh').disabled, false);
        card.refresh();
        card.respond(sampleDashboard());
        assert.equal(card.grid('servers').getStore().data.length, 1);
    }
});

test('deactivation and destruction cancel requests before callbacks and ignore late responses', () => {
    for (const action of ['deactivate', 'destroy']) {
        const card = dashboard();
        card.activate();
        card[action]();
        assert.equal(card.browser.Ext.requests[0].aborted, true);
        const mutations = Array.from(card.components.values(), (child) => child.mutations);
        const generation = card.panel._pveKomodoGeneration;
        assert.doesNotThrow(() => card.respond(sampleDashboard()));
        assert.deepEqual(Array.from(card.components.values(), (child) => child.mutations), mutations);
        assert.equal(card.panel._pveKomodoGeneration, generation);
        if (action === 'deactivate') {
            card.activate();
            card.respond(sampleDashboard(), 200, 0);
            assert.equal(card.grid('servers').getStore().data.length, 0, 'old generation cannot update new activation');
            card.respond(sampleDashboard(), 200, 1);
            assert.equal(card.grid('servers').getStore().data.length, 1);
        }
    }
});

test('a synchronous request failure uses a safe message and releases refresh for retry', () => {
    const card = dashboard();
    const request = card.browser.Ext.Ajax.request;
    card.browser.Ext.Ajax.request = () => { throw new Error('PRIVATE_SECRET'); };
    assert.doesNotThrow(card.activate);
    assert.equal(card.control('dashboard-status').text, 'Docker 数据加载失败，请重试');
    assert.equal(card.control('refresh').disabled, false);
    card.browser.Ext.Ajax.request = request;
    card.refresh();
    card.respond(sampleDashboard());
    assert.equal(card.grid('servers').getStore().data.length, 1);
});

test('each grid has a flat scrolling action toolbar and checkbox selection; server creation has a real handler', () => {
    const card = dashboard();
    const expected = {
        servers: ['新建服务器', '清理容器', '清理网络', '清理存储卷', '清理镜像', '清理系统', '重启所有容器', '停止所有容器', '删除'],
        stacks: ['新建应用栈', '检查更新', '拉取', '部署', '重启', '停止', '销毁', '删除'],
        procedures: ['新建流程', '运行', '删除'],
    };
    for (const section of ['servers', 'stacks', 'procedures']) {
        const grid = card.grid(section);
        assert.equal(grid.selModel.selType, 'checkboxmodel');
        assert.equal(grid.selModel.mode, 'MULTI');
        assert.equal(grid.tbar.overflowHandler, 'scroller');
        assert.deepEqual(Array.from(grid.tbar.items, (button) => button.text), section === 'servers' ? expected[section].concat([undefined, '关联 VM/CT', '解除关联', '打开 PVE 对象']) : expected[section]);
        assert.equal(grid.tbar.items[0].disabled, true);
        assert.equal(typeof grid.tbar.items[0].handler, section === 'servers' ? 'function' : 'undefined');
        assert.ok(grid.tbar.items[0].tooltip);
        assert.ok(grid.tbar.items.every((button) => button.menu === undefined));
    }
    card.activate();
    card.respond(sampleDashboard());
    assert.equal(card.action('servers', 'delete').disabled, true);
    card.select('servers', ['s1']);
    assert.equal(card.action('servers', 'delete').disabled, false);
    card.control('search').setValue('not present');
    assert.equal(card.grid('servers').getSelectionModel().getSelection().length, 0);
    assert.equal(card.action('servers', 'delete').disabled, true);
    card.control('search').setValue('');
    card.select('servers', ['s1']);
    card.refresh();
    assert.equal(card.grid('servers').getSelectionModel().getSelection().length, 0);
    assert.equal(card.action('servers', 'delete').disabled, true);
});

test('confirmation lists every encoded target and states destructive effects; cancel never sends a POST', () => {
    for (const [section, action, effect] of [
        ['servers', 'prune_system', /清理未使用容器、网络、镜像及存储卷，卷数据可能被删除/],
        ['servers', 'prune_volumes', /卷内数据可能被永久删除/],
        ['servers', 'restart_all_containers', /包括原先已停止的容器/],
        ['servers', 'delete', /仅删除 Komodo 中的服务器定义/],
        ['stacks', 'destroy', /默认不删除存储卷/],
        ['stacks', 'delete', /停止并移除所选应用栈的实际部署.*运行中的应用会中断/],
        ['procedures', 'delete', /移除流程的计划/],
    ]) {
        const card = dashboard();
        card.activate();
        const payload = sampleDashboard();
        payload[section][0].name = '<unsafe & "name">';
        card.respond(payload);
        card.control('exclude-templates').setValue(false);
        card.select(section, payload[section].map((row) => row.id));
        card.click(section, action);
        const dialog = card.browser.Ext.dialogs.at(-1);
        assert.match(dialog.options.message, /&lt;unsafe &amp; &quot;name&quot;&gt;/);
        for (const row of payload[section].slice(1)) assert.ok(dialog.options.message.includes('<li>' + row.name + '</li>'));
        assert.match(dialog.options.message, effect);
        assert.equal(card.control('search').disabled, true);
        card.answer('no');
        assert.equal(dialog.destroyed, true);
        assert.equal(card.browser.Ext.requests.length, 1);
        assert.equal(card.control('search').disabled, false);
    }
});

test('only one confirmed POST uses frozen ids, a secure UUID, and the integration CSRF token', () => {
    const card = dashboard();
    card.activate();
    card.respond(sampleDashboard());
    card.control('exclude-templates').setValue(false);
    card.select('servers', ['s1', 's2']);
    card.click('servers', 'stop_all_containers');
    card.select('servers', ['s1']);
    card.answer('yes');
    assert.equal(card.browser.Ext.requests.length, 1, 'selection changes invalidate confirmation');
    card.select('servers', ['s1', 's2']);
    card.click('servers', 'stop_all_containers');
    card.answer('yes');
    card.answer('yes');
    assert.equal(card.browser.Ext.requests.length, 2);
    const request = card.browser.Ext.requests[1];
    assert.equal(request.method, 'POST');
    assert.equal(request.url, '/pve-komodo/api/action');
    assert.equal(request.timeout, 120000);
    assert.deepEqual(Object.keys(request.headers), ['X-PVE-Komodo-CSRF']);
    assert.equal(request.headers['X-PVE-Komodo-CSRF'], 'csrf-test-token');
    assert.equal(request.jsonData.section, 'servers');
    assert.equal(request.jsonData.action, 'stop_all_containers');
    assert.deepEqual(Array.from(request.jsonData.ids), ['s1', 's2']);
    assert.match(request.jsonData.request_id, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    card.control('search').setValue('beta');
    card.refresh();
    card.click('procedures', 'run');
    assert.equal(card.browser.Ext.requests.length, 2, 'pending action cannot be retried or refreshed');
    assert.equal(card.grid('servers').getStore().data.length, 2, 'pending actions prevent hidden target filtering');
    card.respond({ results: [
        { id: 's1', ok: true, status: 'submitted', operation_id: 'opaque' },
        { id: 's2', ok: false, status: 'outcome_unknown', error_code: 'PRIVATE_ERROR' },
    ] });
    assert.match(card.control('dashboard-status').text, /已提交 1，已完成 0.*结果未知 1/);
    assert.match(card.control('dashboard-status').text, /Komodo 的操作记录.*不要重复提交/);
    assert.doesNotMatch(card.control('dashboard-status').text, /任务成功|PRIVATE_ERROR|opaque/);
    assert.equal(card.grid('servers').getSelectionModel().getSelection().length, 0);
    assert.equal(card.browser.Ext.requests.length, 2, 'no automatic reload or resubmission');
});

test('operation result counts distinguish accepted work from completed, failed, and unsubmitted work', () => {
    const card = dashboard();
    card.activate();
    const payload = sampleDashboard();
    payload.servers = Array.from({ length: 4 }, (_, index) => ({ ...payload.servers[0], id: 's' + index }));
    card.respond(payload);
    card.select('servers', payload.servers.map((row) => row.id));
    card.click('servers', 'prune_images');
    card.answer('yes');
    card.respond({ results: ['submitted', 'completed', 'failed', 'not_submitted'].map((status, index) => ({
        id: 's' + index, status, ok: index < 2,
    })) });
    assert.match(card.control('dashboard-status').text, /已提交 1，已完成 1，失败 1，未提交 1，结果未知 0/);
    assert.match(card.control('dashboard-status').text, /请手动刷新观察结果/);
    assert.equal(card.browser.Ext.requests.length, 2);
});

test('timeouts, malformed action results, and idempotency conflicts never auto-retry or expose error bodies', () => {
    for (const [status, body, expected] of [
        [0, '', /请求结果尚未确认/],
        [200, { results: [] }, /请求结果尚未确认/],
        [409, { detail: 'action_already_requested', secret: 'PRIVATE' }, /该请求已提交或结果尚未确认/],
        [409, { detail: 'idempotency_conflict', secret: 'PRIVATE' }, /请求标识冲突/],
        [500, { detail: 'PRIVATE' }, /请求结果尚未确认/],
    ]) {
        const card = dashboard();
        card.activate(); card.respond(sampleDashboard());
        card.select('procedures', ['p1']); card.click('procedures', 'run'); card.answer('yes');
        card.respond(body, status);
        assert.match(card.control('dashboard-status').text, expected);
        assert.match(card.control('dashboard-status').text, /Komodo 的操作记录.*不要重复提交/);
        assert.doesNotMatch(card.control('dashboard-status').text, /PRIVATE|idempotency_conflict|action_already_requested/);
        assert.equal(card.browser.Ext.requests.length, 2);
    }
});

test('switching cards cancels an unconfirmed operation but never aborts a submitted POST', () => {
    const cancelled = dashboard();
    cancelled.activate(); cancelled.respond(sampleDashboard());
    cancelled.select('procedures', ['p1']); cancelled.click('procedures', 'run');
    const dialog = cancelled.browser.Ext.dialogs.at(-1);
    cancelled.deactivate(); cancelled.answer('yes');
    assert.equal(dialog.destroyed, true);
    assert.equal(cancelled.browser.Ext.requests.length, 1);

    const old = dashboard();
    old.activate(); old.respond(sampleDashboard());
    old.select('procedures', ['p1']); old.click('procedures', 'run'); old.answer('yes');
    old.destroy();
    const mutations = Array.from(old.components.values(), (component) => component.mutations);
    assert.equal(old.browser.Ext.requests[1].aborted, undefined, 'POST is not a cancellable dashboard read');
    const current = dashboard(old.browser);
    current.activate();
    assert.equal(current.control('refresh').disabled, true, 'pending POST remains visible across card recreation');
    assert.equal(old.browser.Ext.requests.length, 2);
    old.respond({ results: [{ id: 'p1', ok: true, status: 'submitted' }] }, 200, 1);
    assert.deepEqual(Array.from(old.components.values(), (component) => component.mutations), mutations);
    assert.match(current.control('dashboard-status').text, /已提交 1，已完成 0/);
    assert.equal(current.control('refresh').disabled, false);
});

test('writes require a dashboard CSRF token and secure randomness; secure byte fallback produces UUID v4', () => {
    const missing = dashboard();
    missing.activate();
    const payload = sampleDashboard(); delete payload.csrf_token;
    missing.respond(payload); missing.select('procedures', ['p1']);
    missing.action('procedures', 'run').handler(missing.action('procedures', 'run'));
    assert.equal(missing.browser.Ext.dialogs.length, 0);
    assert.equal(missing.browser.Ext.requests.length, 1);
    for (const available of [true, false]) {
        const card = dashboard();
        card.browser.context.window.crypto = available ? { getRandomValues: (bytes) => bytes.fill(42) } : {};
        card.activate(); card.respond(sampleDashboard());
        card.select('procedures', ['p1']); card.click('procedures', 'run'); card.answer('yes');
        if (available) {
            assert.match(card.browser.Ext.requests[1].jsonData.request_id,
                /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
        } else {
            assert.equal(card.browser.Ext.requests.length, 1);
            assert.match(card.control('dashboard-status').text, /无法生成安全请求标识，未提交/);
        }
    }
});

test('a rejected integration CSRF token reports no submission and requires a manual dashboard refresh', () => {
    const card = dashboard();
    card.activate(); card.respond(sampleDashboard());
    card.select('procedures', ['p1']); card.click('procedures', 'run'); card.answer('yes');
    card.respond({ detail: 'invalid_csrf_token' }, 403);
    assert.equal(card.control('dashboard-status').text, '会话校验已更新，本次未提交，请先刷新 Docker 页面再操作');
    assert.equal(card.browser.Ext.requests.length, 2, 'the rejected POST is not retried');
    card.select('procedures', ['p1']);
    assert.equal(card.action('procedures', 'run').disabled, true);
    card.refresh();
    assert.equal(card.browser.Ext.requests[2].method, 'GET');
    const refreshed = sampleDashboard(); refreshed.csrf_token = 'csrf-refreshed-token';
    card.respond(refreshed);
    card.select('procedures', ['p1']);
    assert.equal(card.action('procedures', 'run').disabled, false);
    assert.doesNotMatch(card.control('dashboard-status').text, /请先刷新/);
    assert.equal(card.browser.Ext.requests.length, 3);
});

test('more than fifty selected resources never opens confirmation or sends a POST', () => {
    const card = dashboard();
    card.activate();
    const payload = sampleDashboard();
    payload.procedures = Array.from({ length: 51 }, (_, index) => ({
        ...payload.procedures[0], id: 'p' + index, name: 'procedure-' + index,
    }));
    card.respond(payload);
    card.select('procedures', payload.procedures.map((row) => row.id));
    card.click('procedures', 'run');
    assert.equal(card.control('dashboard-status').text, '每次最多选择 50 个资源，请减少选择后再操作');
    assert.equal(card.browser.Ext.dialogs.length, 0);
    assert.equal(card.browser.Ext.requests.length, 1);
    card.select('procedures', payload.procedures.slice(0, 50).map((row) => row.id));
    card.click('procedures', 'run');
    assert.equal(card.browser.Ext.dialogs.length, 1, 'exactly fifty can be confirmed');
    card.answer('no');
    assert.equal(card.browser.Ext.requests.length, 1);
});

function sampleContainers() {
    const serverA = '1'.repeat(24);
    const serverB = '2'.repeat(24);
    const row = (server, container, name, state) => ({
        id: server + ':' + container.repeat(64), container_id: container.repeat(64),
        server_id: server, server_name: server === serverA ? 'host-a' : 'host-b',
        name, state, image: 'registry/app:latest', networks: ['frontend'],
        ports: ['0.0.0.0:8080->80/tcp'], volumes: ['/data:/app/data'],
        tags: [server === serverA ? 'prod' : 'staging'],
    });
    return {
        containers: [row(serverA, 'a', 'api', 'running'), row(serverB, 'a', 'api', 'exited'),
            row(serverA, 'b', 'worker', 'paused'), row(serverA, 'c', 'mystery', '<unknown>')],
        servers: [{ id: serverA, name: 'host-a' }, { id: serverB, name: 'host-b' }],
        updated_at: '2026-10-17T10:20:30Z', errors: [], csrf_token: 'container-csrf', user: 'root@pam',
    };
}

test('containers uses a lazy native grid with the requested columns and six flat actions without a create button', () => {
    const card = dashboard(runtime(), 'containers');
    const grid = card.grid('containers');
    assert.equal(card.config.pveKomodoRoute, '/containers');
    assert.equal(card.config.pveKomodoPage, 'containers');
    assert.equal(grid.xtype, 'gridpanel');
    assert.equal(grid.selModel.selType, 'checkboxmodel');
    assert.deepEqual(Array.from(grid.columns, (column) => column.text), ['名称', '服务器', '状态', '镜像', '网络', '端口', '存储卷']);
    assert.deepEqual(Array.from(grid.tbar.items, (button) => button.text), ['启动', '重启', '暂停', '恢复', '停止', '销毁']);
    assert.equal(grid.tbar.overflowHandler, 'scroller');
    assert.equal(card.browser.Ext.requests.length, 0);
    card.activate();
    assert.equal(card.browser.Ext.requests[0].url, '/pve-komodo/api/containers');
    assert.equal(card.browser.Ext.requests[0].method, 'GET');
    card.respond(sampleContainers());
    assert.equal(grid.getStore().data.length, 4);
    assert.equal(card.control('server').getStore().data.length, 3);
    assert.equal(card.control('server').listConfig.itemTpl, '{label:htmlEncode}');
    assert.equal(card.control('tag').fieldLabel, '服务器标签');
    assert.ok(grid.tbar.items.every((button) => card.action('containers', button.itemId.replace('pve-komodo-action-', '')).disabled));
});

test('container server, server-tag, and search filters compose locally and clear selections', () => {
    const card = dashboard(runtime(), 'containers');
    card.activate();
    const payload = sampleContainers(); card.respond(payload);
    const grid = card.grid('containers');
    card.select('containers', [payload.containers[0].id]);
    assert.equal(card.action('containers', 'stop').disabled, false);
    card.control('server').setValue(payload.servers[1].id);
    assert.equal(grid.getStore().data.length, 1);
    assert.equal(grid.getStore().data[0].server_name, 'host-b');
    assert.equal(grid.getSelectionModel().getSelection().length, 0);
    card.control('tag').setValue('prod');
    assert.equal(grid.getStore().data.length, 0);
    card.control('server').setValue('');
    assert.equal(grid.getStore().data.length, 3);
    card.control('search').setValue('WORKER');
    assert.equal(grid.getStore().data[0].name, 'worker');
    card.control('search').setValue('8080');
    assert.equal(grid.getStore().data.length, 3);
    card.control('search').setValue('/app/data');
    assert.equal(grid.getStore().data.length, 3);
    assert.equal(card.browser.Ext.requests.length, 1);
});

test('container actions apply to every selected state and unknown states remain escaped and non-actionable', () => {
    const card = dashboard(runtime(), 'containers');
    card.activate(); const payload = sampleContainers(); card.respond(payload);
    const enabled = () => ['start', 'restart', 'pause', 'unpause', 'stop', 'destroy']
        .filter((name) => !card.action('containers', name).disabled);
    card.select('containers', [payload.containers[0].id]);
    assert.deepEqual(enabled(), ['restart', 'pause', 'stop', 'destroy']);
    card.select('containers', [payload.containers[1].id]);
    assert.deepEqual(enabled(), ['start', 'destroy']);
    card.select('containers', [payload.containers[2].id]);
    assert.deepEqual(enabled(), ['unpause', 'stop', 'destroy']);
    card.select('containers', [payload.containers[0].id, payload.containers[2].id]);
    assert.deepEqual(enabled(), ['stop', 'destroy']);
    card.select('containers', [payload.containers[3].id]);
    assert.deepEqual(enabled(), []);
    const unsafe = '<img onerror="run()">';
    card.grid('containers').columns.forEach((column) => {
        const value = ['networks', 'ports', 'volumes'].includes(column.dataIndex) ? [unsafe] : unsafe;
        assert.equal(column.renderer(value), '&lt;img onerror=&quot;run()&quot;&gt;');
    });
    card.action('containers', 'destroy').handler(card.action('containers', 'destroy'));
    assert.equal(card.browser.Ext.dialogs.length, 0);
});

test('container confirmation disambiguates equal names by server and cancel never writes', () => {
    const card = dashboard(runtime(), 'containers');
    card.activate(); const payload = sampleContainers();
    payload.containers[0].server_name = '<host-a>';
    card.respond(payload);
    card.select('containers', payload.containers.slice(0, 2).map((row) => row.id));
    card.click('containers', 'destroy');
    const dialog = card.browser.Ext.dialogs.at(-1);
    assert.match(dialog.options.message, /api（&lt;host-a&gt;）/);
    assert.match(dialog.options.message, /api（host-b）/);
    assert.match(dialog.options.message, /停止并删除所选容器.*可写层.*不删除存储卷/);
    card.answer('no');
    assert.equal(card.browser.Ext.requests.length, 1);
    card.panel._pveKomodoRows.containers[0].id = 'api';
    card.control('search').setValue('api');
    card.select('containers', ['api']);
    card.click('containers', 'destroy');
    assert.equal(card.browser.Ext.dialogs.length, 1, 'no name fallback for invalid composite ids');
    assert.match(card.control('dashboard-status').text, /容器标识无效/);
});

test('container empty, partial, malformed, and authentication states never retain stale rows or raw error bodies', () => {
    const card = dashboard(runtime(), 'containers');
    card.activate(); card.respond(sampleContainers());
    card.refresh(); card.respond({ ...sampleContainers(), containers: [], errors: ['PRIVATE server_unavailable'] });
    assert.equal(card.grid('containers').getStore().data.length, 0);
    assert.match(card.control('dashboard-status').text, /部分数据不可用/);
    assert.doesNotMatch(card.control('dashboard-status').text, /PRIVATE/);
    assert.match(card.control('dashboard-status').text, /Komodo 监控缓存，离线服务器可能不显示/);
    assert.equal(card.grid('containers').viewConfig.emptyText, '暂无可显示的容器数据');
    for (const [status, body, expected] of [
        [401, { detail: 'PRIVATE' }, 'PVE 未登录或会话已过期'],
        [403, { detail: 'PRIVATE' }, '无访问权限'],
        [503, { detail: 'PRIVATE' }, 'PVE 会话验证或集成服务暂不可用'],
        [200, { ...sampleContainers(), servers: null }, '容器数据加载失败，请重试'],
    ]) {
        card.refresh(); card.respond(body, status);
        assert.equal(card.grid('containers').getStore().data.length, 0);
        assert.equal(card.control('dashboard-status').text, expected);
    }
});

test('a mock container POST uses composite ids and the same CSRF/UUID path and remains pending when returning home', () => {
    const old = dashboard(runtime(), 'containers');
    old.activate(); const payload = sampleContainers(); old.respond(payload);
    const id = payload.containers[0].id;
    old.select('containers', [id]); old.click('containers', 'stop'); old.answer('yes');
    const request = old.browser.Ext.requests[1];
    assert.equal(request.url, '/pve-komodo/api/action');
    assert.equal(request.jsonData.section, 'containers');
    assert.equal(request.jsonData.action, 'stop');
    assert.deepEqual(Array.from(request.jsonData.ids), [id]);
    assert.equal(request.headers['X-PVE-Komodo-CSRF'], 'container-csrf');
    assert.match(request.jsonData.request_id, /^[0-9a-f-]{36}$/);
    old.destroy();
    assert.equal(request.aborted, undefined);
    const home = dashboard(old.browser);
    home.activate();
    assert.equal(home.control('refresh').disabled, true);
    assert.equal(old.browser.Ext.requests.length, 2);
    old.respond({ results: [{ id, ok: true, status: 'submitted' }] }, 200, 1);
    assert.match(home.control('dashboard-status').text, /已提交 1，已完成 0/);
    assert.equal(home.control('refresh').disabled, false);
    assert.equal(old.browser.Ext.requests.length, 2, 'no automatic resubmission or reload');
});

function sampleAlerts(page = 0) {
    const row = (id, ts) => ({
        id, ts, resolved_ts: null, resolved: false, level: 'WARNING', resource_type: 'Server',
        resource_id: '1'.repeat(24), resource_name: 'host-a', alert_type: 'ServerCpu',
        details: { percentage: 85 }, details_omitted: false,
    });
    return {
        alerts: page === 0 ? [row('newest', 1790000000123), row('older', 90000)] : [row('oldest', 0)],
        page, next_page: page === 0 ? 1 : null, updated_at: '2026-10-17T10:20:30Z',
    };
}

function alertsPage(browser = runtime()) {
    const card = dashboard(browser, 'alerts');
    card.press = (name) => {
        const button = card.control('alerts-' + name);
        if (!button.disabled) button.handler(button);
    };
    card.openDetails = (row, action = false) => {
        const grid = card.grid('alerts');
        const record = { data: row };
        if (action) {
            grid.columns[0].items[0].handler({ up: () => grid }, 0, 0, null, null, record);
        } else {
            grid.listeners.itemdblclick.call(grid, grid, record);
        }
        return browser.Ext.dialogs.at(-1);
    };
    return card;
}

test('alerts is a lazy native read-only grid preserving server time order without misleading local sorting', () => {
    const card = alertsPage();
    const grid = card.grid('alerts');
    assert.equal(card.config.pveKomodoRoute, '/alerts');
    assert.deepEqual(Array.from(grid.columns, (column) => column.text), ['详情', '发生时间', '资源', '状态', '级别', '告警类型', '解决时间']);
    assert.ok(grid.columns.every((column) => column.sortable === false));
    assert.equal(grid.store.remoteSort, true);
    assert.equal(grid.store.sorters, undefined);
    assert.equal(grid.columns.find((column) => column.dataIndex === 'ts').width, 180);
    assert.equal(grid.columns.find((column) => column.dataIndex === 'resolved_ts').width, 180);
    assert.equal(grid.columns[0].xtype, 'actioncolumn');
    assert.equal(grid.selModel, undefined, 'read-only alert rows have no mutation selection');
    assert.equal(card.control('alerts-resource').store.data.length, 13);
    assert.equal(card.control('alerts-type').store.data.length, 23);
    assert.equal(card.browser.Ext.requests.length, 0);
    card.activate();
    const request = card.browser.Ext.requests[0];
    assert.equal(request.url, '/pve-komodo/api/alerts');
    assert.equal(request.method, 'GET');
    assert.equal(request.disableCaching, false, 'ExtJS must not append _dc to the strict alert query');
    assert.deepEqual(JSON.parse(JSON.stringify(request.params)), { page: 0 });
    assert.equal(request.headers, undefined);
    card.respond(sampleAlerts());
    assert.deepEqual(Array.from(grid.getStore().data, (row) => row.id), ['newest', 'older']);
    assert.equal(card.control('alerts-first').disabled, true);
    assert.equal(card.control('alerts-previous').disabled, true);
    assert.equal(card.control('alerts-next').disabled, false);
    assert.equal(card.control('alerts-page').text, '第 1 页（每页 100 条）');
    card.press('next');
    assert.equal(card.browser.Ext.requests[1].params.page, 1);
    card.respond(sampleAlerts(1));
    assert.equal(card.control('alerts-page').text, '第 2 页（每页 100 条）');
    assert.equal(card.control('alerts-first').disabled, false);
    assert.equal(card.control('alerts-next').disabled, true);
    assert.deepEqual(Array.from(grid.getStore().data, (row) => row.id), ['oldest']);
    card.press('previous');
    assert.equal(card.browser.Ext.requests[2].params.page, 0);
});

test('alert filters reset to page zero, cancel prior GETs, and omit unselected filters including resolved', () => {
    const card = alertsPage();
    card.activate(); card.respond(sampleAlerts());
    card.press('next');
    card.control('alerts-resource').setValue('Server');
    assert.equal(card.browser.Ext.requests[1].aborted, true);
    assert.equal(card.browser.Ext.requests[2].params.page, 0);
    card.control('alerts-type').setValue('ServerCpu');
    card.control('alerts-unresolved').setValue(true);
    const latest = card.browser.Ext.requests.at(-1);
    assert.deepEqual(JSON.parse(JSON.stringify(latest.params)), {
        page: 0, resource_type: 'Server', alert_type: 'ServerCpu', unresolved_only: true,
    });
    card.respond(sampleAlerts(1), 200, 1);
    assert.equal(card.grid('alerts').getStore().data.length, 0, 'cancelled page cannot replace filtered data');
    card.respond(sampleAlerts());
    const beforeReset = card.browser.Ext.requests.length;
    card.press('reset');
    assert.equal(card.browser.Ext.requests.length, beforeReset + 1, 'reset loads only once after changing three controls');
    assert.deepEqual(JSON.parse(JSON.stringify(card.browser.Ext.requests.at(-1).params)), { page: 0 });
    card.respond(sampleAlerts());
    assert.equal(card.control('alerts-unresolved').getValue(), false);
    assert.equal(card.control('alerts-type').getValue(), '');
    assert.equal(card.control('alerts-resource').getValue(), '');
    assert.ok(card.browser.Ext.requests.every((request) => request.method === 'GET'));
});

test('alert timestamps retain Unix milliseconds and real zero while native Date formatting uses browser local time', () => {
    const card = alertsPage();
    const observed = [];
    const localFormat = (date) => [date.getFullYear(), date.getMonth() + 1, date.getDate(),
        date.getHours(), date.getMinutes(), date.getSeconds()].join('|');
    card.browser.Ext.Date.format = (date, format) => {
        assert.equal(format, 'Y-m-d H:i:s');
        observed.push(date.getTime());
        return localFormat(date);
    };
    const columns = card.grid('alerts').columns;
    for (const name of ['ts', 'resolved_ts']) {
        const column = columns.find((entry) => entry.dataIndex === name);
        assert.equal(column.renderer(1790000000123), localFormat(new Date(1790000000123)));
        assert.equal(column.renderer(0), localFormat(new Date(0)), 'Unix epoch is not a missing timestamp');
        for (const invalid of [null, undefined, false, '1790000000123', -1, 1.5]) {
            assert.equal(column.renderer(invalid), '-');
        }
    }
    assert.deepEqual(observed, [1790000000123, 0, 1790000000123, 0]);
});

test('alert details and unknown types encode every value and show duration only when resolution times are valid', () => {
    const card = alertsPage(); card.activate(); card.respond(sampleAlerts());
    const unsafe = '<script>private & "value"</script>';
    const row = { ...sampleAlerts().alerts[0], ts: 0, resolved_ts: 61000, resolved: true,
        resource_type: unsafe, resource_name: unsafe, level: unsafe, alert_type: unsafe,
        details: { safe_metadata: unsafe }, details_omitted: true };
    const window = card.openDetails(row, true);
    assert.equal(window.title, '告警详情');
    assert.equal(window.modal, true);
    assert.equal(window.buttons, undefined);
    const html = window.items[0].html;
    assert.match(html, /0 天 0 小时 1 分 1 秒/);
    assert.match(html, /部分详情已省略/);
    assert.doesNotMatch(html, /<script>|<a |iframe|解警/);
    assert.match(html, /&lt;script&gt;private &amp; &quot;value&quot;&lt;\/script&gt;/);
    assert.match(html, /&quot;safe_metadata&quot;/);
    assert.equal(card.browser.Ext.requests.length, 1, 'details use only the safe DTO already on this page');
    const columns = card.grid('alerts').columns;
    assert.equal(columns.find((column) => column.dataIndex === 'resolved').renderer(null), '未知');
    for (const field of ['level', 'alert_type']) {
        assert.equal(columns.find((column) => column.dataIndex === field).renderer(unsafe),
            '&lt;script&gt;private &amp; &quot;value&quot;&lt;/script&gt;');
        assert.equal(columns.find((column) => column.dataIndex === field).renderer('unknown'), '未知');
    }
    for (const invalid of [
        { resolved: true, ts: 0, resolved_ts: null }, { resolved: false, ts: 0, resolved_ts: 61000 },
        { resolved: true, ts: 61000, resolved_ts: 0 }, { resolved: true, ts: null, resolved_ts: 61000 },
    ]) {
        const details = card.openDetails({ ...row, ...invalid }).items[0].html;
        assert.match(details, /<strong>持续时间：<\/strong>-<\/p>/);
    }
    const resolvedWithoutTime = card.openDetails({ ...row, resolved_ts: null }).items[0].html;
    assert.match(resolvedWithoutTime, /<strong>状态：<\/strong>已解决/);
    assert.match(resolvedWithoutTime, /<strong>解决时间：<\/strong>-/);
});

test('alert empty and error pages clear old rows and navigation; malformed or failed responses reveal no body text', () => {
    const card = alertsPage(); card.activate(); card.respond(sampleAlerts());
    card.press('refresh');
    card.respond({ ...sampleAlerts(), alerts: [], next_page: null });
    assert.equal(card.grid('alerts').getStore().data.length, 0);
    assert.equal(card.control('alerts-next').disabled, true);
    assert.equal(card.grid('alerts').viewConfig.emptyText, '暂无符合条件的告警');
    for (const [status, body, expected] of [
        [401, { detail: 'PRIVATE' }, 'PVE 未登录或会话已过期'],
        [403, { detail: 'PRIVATE' }, '无访问权限'],
        [503, { detail: 'PRIVATE' }, 'PVE 会话验证或集成服务暂不可用'],
        [503, { detail: 'not_configured' }, 'Komodo 未配置凭据'],
        [502, { detail: 'PRIVATE' }, '告警数据加载失败，请重试'],
        [200, { ...sampleAlerts(), page: 8 }, '告警数据加载失败，请重试'],
        [200, { ...sampleAlerts(), next_page: true }, '告警数据加载失败，请重试'],
        [200, { ...sampleAlerts(), next_page: 2 }, '告警数据加载失败，请重试'],
        [200, { ...sampleAlerts(), next_page: 1000001 }, '告警数据加载失败，请重试'],
        [200, 'PRIVATE malformed', '告警数据加载失败，请重试'],
    ]) {
        card.press('refresh'); card.respond(body, status);
        assert.equal(card.control('alerts-status').text, expected);
        assert.equal(card.grid('alerts').getStore().data.length, 0);
        assert.equal(card.control('alerts-next').disabled, true);
        assert.equal(card.control('alerts-refresh').disabled, false);
    }
});

test('leaving alerts cancels its GET and details window and ignores late callbacks without changing write state', () => {
    const home = dashboard(); home.activate(); home.respond(sampleDashboard());
    home.select('procedures', ['p1']); home.click('procedures', 'run'); home.answer('yes');
    home.destroy();
    const card = alertsPage(home.browser);
    card.activate();
    assert.equal(card.browser.Ext.requests.at(-1).url, '/pve-komodo/api/alerts');
    card.respond(sampleAlerts());
    const details = card.openDetails(sampleAlerts().alerts[0]);
    card.press('refresh');
    assert.equal(details.destroyed, true);
    card.destroy();
    const request = card.browser.Ext.requests.at(-1);
    assert.equal(request.aborted, true);
    const before = Array.from(card.components.values(), (component) => component.mutations);
    assert.doesNotThrow(() => card.respond(sampleAlerts()));
    home.respond({ results: [{ id: 'p1', ok: true, status: 'submitted' }] }, 200, 1);
    assert.deepEqual(Array.from(card.components.values(), (component) => component.mutations), before);
    assert.equal(home.browser.Ext.requests[1].aborted, undefined, 'read-only alert lifecycle never aborts a submitted write');
    const current = dashboard(home.browser); current.activate(); current.respond(sampleDashboard());
    assert.match(current.control('dashboard-status').text, /已提交 1，已完成 0/);
});

function sampleUpdates(page = 0) {
    const row = (id, start_ts) => ({
        id, operation: 'RestartContainer', start_ts, status: 'Complete', success: true,
        username: 'operator', resource_type: 'Server', resource_id: '1'.repeat(24), resource_name: 'host-a',
    });
    return {
        updates: page === 0 ? [row('a'.repeat(24), 1790000000123), row('b'.repeat(24), 90000)] : [row('c'.repeat(24), 0)],
        page, next_page: page === 0 ? 1 : null, updated_at: '2026-10-17T10:20:30Z', errors: [],
    };
}

function sampleUpdateDetail(id = 'a'.repeat(24)) {
    return {
        update: { ...sampleUpdates().updates[0], id, start_ts: 0, end_ts: 61000,
            version: { major: 2, minor: 3, patch: 3 }, commit_hash: 'a'.repeat(40), details_omitted: true,
            stages: [{ stage: 'restart container', success: true, start_ts: 0, end_ts: 61000 }],
        }, updated_at: '2026-10-17T10:20:30Z', errors: [],
    };
}

function updatesPage(browser = runtime()) {
    const card = dashboard(browser, 'updates');
    card.press = (name) => {
        const button = card.control('updates-' + name);
        if (!button.disabled) button.handler(button);
    };
    card.openDetails = (row, action = false) => {
        const grid = card.grid('updates');
        const record = { data: row, get: (name) => row[name] };
        if (action) grid.columns[0].items[0].handler({ up: () => grid }, 0, 0, null, null, record);
        else grid.listeners.itemdblclick.call(grid, grid, record);
        return browser.Ext.dialogs.at(-1);
    };
    return card;
}

test('updates is a lazy read-only server-paged list with complete operation filters and explicit cache-buster suppression', () => {
    const card = updatesPage();
    const grid = card.grid('updates');
    assert.equal(card.config.pveKomodoRoute, '/updates');
    assert.deepEqual(Array.from(grid.columns, (column) => column.text), ['详情', '资源', '操作', '结果', '开始时间', '操作人']);
    assert.ok(grid.columns.every((column) => column.sortable === false));
    assert.equal(grid.store.remoteSort, true);
    assert.equal(grid.store.sorters, undefined);
    assert.equal(grid.store.fields.includes('end_ts'), false, 'list API cannot invent an end time');
    assert.equal(grid.columns.find((column) => column.dataIndex === 'start_ts').width, 180);
    assert.equal(card.control('updates-resource').store.data.length, 13);
    assert.equal(card.control('updates-operation').store.data.length, 138);
    assert.equal(card.browser.Ext.requests.length, 0);
    card.activate();
    const request = card.browser.Ext.requests[0];
    assert.equal(request.url, '/pve-komodo/api/updates');
    assert.equal(request.method, 'GET');
    assert.equal(request.disableCaching, false, 'actual ExtJS must not append an unsupported _dc query');
    assert.deepEqual(JSON.parse(JSON.stringify(request.params)), { page: 0 });
    card.respond(sampleUpdates());
    assert.equal(card.browser.Ext.requests.length, 1, 'list rows do not eager-load individual detail requests');
    assert.deepEqual(Array.from(grid.getStore().data, (row) => row.id), ['a'.repeat(24), 'b'.repeat(24)]);
    assert.equal(card.control('updates-page').text, '第 1 页（每页 100 条）');
    card.press('next');
    assert.equal(card.browser.Ext.requests[1].params.page, 1);
    card.respond(sampleUpdates(1));
    assert.equal(card.control('updates-page').text, '第 2 页（每页 100 条）');
    assert.equal(card.control('updates-first').disabled, false);
    assert.equal(card.control('updates-next').disabled, true);
    card.press('first');
    assert.equal(card.browser.Ext.requests[2].params.page, 0);
});

test('updates filters reset pagination and cancel obsolete reads while reset performs one request without empty query keys', () => {
    const card = updatesPage(); card.activate(); card.respond(sampleUpdates()); card.press('next');
    card.control('updates-resource').setValue('Server');
    assert.equal(card.browser.Ext.requests[1].aborted, true);
    card.control('updates-operation').setValue('RestartContainer');
    assert.equal(card.browser.Ext.requests[2].aborted, true);
    const request = card.browser.Ext.requests.at(-1);
    assert.equal(request.disableCaching, false);
    assert.deepEqual(JSON.parse(JSON.stringify(request.params)), {
        page: 0, resource_type: 'Server', operation: 'RestartContainer',
    });
    assert.equal(new URLSearchParams(request.params).toString(), 'page=0&resource_type=Server&operation=RestartContainer');
    card.respond(sampleUpdates(1), 200, 1);
    assert.equal(card.grid('updates').getStore().data.length, 0);
    card.respond(sampleUpdates());
    const before = card.browser.Ext.requests.length;
    card.press('reset');
    assert.equal(card.browser.Ext.requests.length, before + 1);
    assert.deepEqual(JSON.parse(JSON.stringify(card.browser.Ext.requests.at(-1).params)), { page: 0 });
    assert.ok(card.browser.Ext.requests.every((entry) => entry.method === 'GET' && entry.disableCaching === false));
});

test('update results evaluate success only after completion and cell text including unknown operations stays encoded', () => {
    const card = updatesPage();
    const columns = card.grid('updates').columns;
    const result = columns.find((column) => column.dataIndex === 'status');
    for (const [status, success, expected] of [
        ['Queued', false, '排队中'], ['Queued', true, '排队中'], ['InProgress', false, '进行中'],
        ['Complete', true, '成功'], ['Complete', false, '失败'], ['Complete', null, '未知'], ['unknown', false, '未知'],
    ]) assert.equal(result.renderer(status, null, { data: { status, success } }), expected);
    const unsafe = '<img src=x onerror="run()">';
    assert.equal(columns.find((column) => column.dataIndex === 'operation').renderer(unsafe), '&lt;img src=x onerror=&quot;run()&quot;&gt;');
    assert.equal(columns.find((column) => column.dataIndex === 'username').renderer(unsafe), '&lt;img src=x onerror=&quot;run()&quot;&gt;');
    assert.equal(columns.find((column) => column.dataIndex === 'operation').renderer('unknown'), '未知');
    const resource = columns.find((column) => column.dataIndex === 'resource_name');
    assert.equal(resource.renderer(null, null, { data: { resource_type: unsafe, resource_name: unsafe } }),
        '&lt;img src=x onerror=&quot;run()&quot;&gt;：&lt;img src=x onerror=&quot;run()&quot;&gt;');
    const time = columns.find((column) => column.dataIndex === 'start_ts');
    const observed = [];
    const local = (date) => [date.getFullYear(), date.getMonth(), date.getDate(), date.getHours(), date.getMinutes(), date.getSeconds()].join('|');
    card.browser.Ext.Date.format = (date) => { observed.push(date.getTime()); return local(date); };
    assert.equal(time.renderer(1790000000123), local(new Date(1790000000123)));
    assert.equal(time.renderer(0), local(new Date(0)));
    assert.equal(time.renderer(null), '-');
    assert.deepEqual(observed, [1790000000123, 0]);
});

test('update details load on demand into a read-only native window and show only safe metadata and stage results', () => {
    const card = updatesPage(); card.activate(); card.respond(sampleUpdates());
    const window = card.openDetails({ ...sampleUpdates().updates[0], username: null }, true);
    assert.equal(window.title, '操作详情');
    assert.equal(window.buttons, undefined);
    assert.equal(window.down('#pve-komodo-update-summary').html, '正在加载操作详情…');
    const request = card.browser.Ext.requests[1];
    assert.equal(request.url, '/pve-komodo/api/updates/' + 'a'.repeat(24));
    assert.equal(request.method, 'GET'); assert.equal(request.disableCaching, false);
    assert.equal(request.params, undefined);
    const payload = sampleUpdateDetail();
    payload.update.username = null;
    payload.update.operator_id = 'e'.repeat(24);
    payload.update.resource_name = '<script>unsafe</script>';
    payload.update.stages[0].stage = '<img onerror="run()">';
    payload.update.command = 'PRIVATE_COMMAND'; payload.update.stdout = 'PRIVATE_OUTPUT'; payload.update.prev_toml = 'PRIVATE_CONFIG';
    card.respond(payload);
    const html = window.down('#pve-komodo-update-summary').html;
    assert.match(html, /0 天 0 小时 1 分 1 秒/);
    assert.match(html, /<strong>版本：<\/strong>2\.3\.3/);
    assert.match(html, /用户 ID：eeeeeeeeeeeeeeeeeeeeeeee/);
    assert.match(html, /仅显示操作阶段与结果/);
    assert.match(html, /&lt;script&gt;unsafe&lt;\/script&gt;/);
    assert.doesNotMatch(html, /PRIVATE_COMMAND|PRIVATE_OUTPUT|PRIVATE_CONFIG|<script>/);
    const stages = window.down('#pve-komodo-update-stages');
    assert.equal(stages.xtype, 'gridpanel');
    assert.deepEqual(Array.from(stages.columns, (column) => column.text), ['阶段', '结果', '开始时间', '结束时间']);
    assert.equal(stages.getStore().data.length, 1);
    assert.equal(stages.columns[0].renderer(payload.update.stages[0].stage), '&lt;img onerror=&quot;run()&quot;&gt;');
    assert.equal(stages.columns[2].width, 180); assert.equal(stages.columns[3].width, 180);
    assert.equal(stages.columns[2].renderer(0), '1970-01-01 00:00:00');
    assert.equal(stages.columns[3].renderer(61000), '1970-01-01 00:01:01');
    assert.equal(card.browser.Ext.requests.length, 2);
    for (const change of [
        { end_ts: null }, { status: 'InProgress' }, { start_ts: 61000, end_ts: 0 },
    ]) {
        const next = card.openDetails(sampleUpdates().updates[0]);
        card.respond({ ...payload, update: { ...payload.update, ...change } });
        assert.match(next.down('#pve-komodo-update-summary').html, /<strong>持续时间：<\/strong>-/);
        if (change.end_ts === null) assert.match(next.down('#pve-komodo-update-summary').html, /<strong>结束时间：<\/strong>-/);
    }
});

test('update details prefer the matching detail username, then the clicked row snapshot, then operator ID', () => {
    const card = updatesPage(); card.activate(); card.respond(sampleUpdates());
    const row = { ...sampleUpdates().updates[0], username: '<list-user>' };
    const fallback = card.openDetails(row);
    row.username = 'changed-after-click';
    const payload = sampleUpdateDetail();
    payload.update.username = null;
    payload.update.operator_id = 'e'.repeat(24);
    card.respond(payload);
    const html = fallback.down('#pve-komodo-update-summary').html;
    assert.match(html, /<strong>操作人：<\/strong>&lt;list-user&gt;/);
    assert.doesNotMatch(html, /changed-after-click|用户 ID：|<list-user>/);
    const preferred = card.openDetails(row);
    card.respond({ ...payload, update: { ...payload.update, username: '<detail-user>' } });
    assert.match(preferred.down('#pve-komodo-update-summary').html, /<strong>操作人：<\/strong>&lt;detail-user&gt;/);
    const mismatch = card.openDetails(row);
    card.respond({ ...payload, update: { ...payload.update, id: 'b'.repeat(24) } });
    assert.equal(mismatch.down('#pve-komodo-update-summary').html, '操作详情加载失败，请关闭后重试');
    assert.equal(card.browser.Ext.requests.length, 4, 'username fallback makes no additional user lookup');
});

test('update detail loading, missing records, window closure, and filter changes cancel old reads without late mutations', () => {
    const card = updatesPage(); card.activate(); card.respond(sampleUpdates());
    const first = card.openDetails(sampleUpdates().updates[0]);
    first.destroy();
    assert.equal(card.browser.Ext.requests[1].aborted, true);
    assert.doesNotThrow(() => card.respond(sampleUpdateDetail(), 200, 1));
    const second = card.openDetails(sampleUpdates().updates[0]);
    card.control('updates-resource').setValue('Server');
    assert.equal(second.destroyed, true);
    assert.equal(card.browser.Ext.requests[2].aborted, true);
    assert.doesNotThrow(() => card.respond(sampleUpdateDetail(), 200, 2));
    card.respond(sampleUpdates());
    const missing = card.openDetails(sampleUpdates().updates[0]);
    card.respond({ detail: 'PRIVATE' }, 404);
    assert.equal(missing.down('#pve-komodo-update-summary').html, '操作记录不存在或已失效');
    assert.equal(missing.down('#pve-komodo-update-stages').getStore().data.length, 0);
    const replacement = card.openDetails(sampleUpdates().updates[0]);
    card.respond({ ...sampleUpdateDetail(), update: { ...sampleUpdateDetail().update, id: 'b'.repeat(24) } });
    assert.equal(replacement.down('#pve-komodo-update-summary').html, '操作详情加载失败，请关闭后重试');
    const before = card.browser.Ext.requests.length;
    card.openDetails({ ...sampleUpdates().updates[0], id: '../private' });
    assert.equal(card.browser.Ext.requests.length, before, 'details accept only canonical 24-hex identifiers');
    assert.match(card.control('updates-status').text, /操作记录标识无效/);
    card.destroy();
    assert.equal(replacement.destroyed, true);
    assert.ok(card.browser.Ext.requests.every((entry) => entry.method === 'GET'));
});

test('update list failures clear old rows and only this page reads can be cancelled while a global write remains untouched', () => {
    const home = dashboard(); home.activate(); home.respond(sampleDashboard());
    home.select('procedures', ['p1']); home.click('procedures', 'run'); home.answer('yes'); home.destroy();
    const card = updatesPage(home.browser); card.activate(); card.respond(sampleUpdates());
    for (const [status, body, expected] of [
        [401, { detail: 'PRIVATE' }, 'PVE 未登录或会话已过期'], [403, { detail: 'PRIVATE' }, '无访问权限'],
        [503, { detail: 'PRIVATE' }, 'PVE 会话验证或集成服务暂不可用'],
        [503, { detail: 'not_configured' }, 'Komodo 未配置凭据'], [502, { detail: 'PRIVATE' }, '操作记录加载失败，请重试'],
        [200, { ...sampleUpdates(), next_page: 3 }, '操作记录加载失败，请重试'],
        [200, { ...sampleUpdates(), page: 6 }, '操作记录加载失败，请重试'], [200, 'PRIVATE invalid', '操作记录加载失败，请重试'],
    ]) {
        card.press('refresh'); card.respond(body, status);
        assert.equal(card.grid('updates').getStore().data.length, 0);
        assert.equal(card.control('updates-status').text, expected);
        assert.equal(card.control('updates-next').disabled, true);
    }
    card.press('refresh'); card.respond({ ...sampleUpdates(), updates: [], next_page: null, errors: ['PRIVATE'] });
    assert.match(card.control('updates-status').text, /部分数据不可用/);
    assert.doesNotMatch(card.control('updates-status').text, /PRIVATE/);
    card.press('refresh'); card.destroy();
    assert.equal(card.browser.Ext.requests.at(-1).aborted, true);
    assert.doesNotThrow(() => card.respond(sampleUpdates()));
    assert.equal(home.browser.Ext.requests[1].aborted, undefined);
    home.respond({ results: [{ id: 'p1', ok: true, status: 'submitted' }] }, 200, 1);
    const current = dashboard(home.browser); current.activate(); current.respond(sampleDashboard());
    assert.match(current.control('dashboard-status').text, /已提交 1，已完成 0/);
});

function sampleGuestLinks() {
    return {
        links: [{ server_id: 's1', guest_type: 'qemu', vmid: 102, guest_name: 'alpha-vm', node: 'node-1',
            guest_status: 'running', status: 'linked', revision: 3, identity_strength: 'strong', can_navigate: true }],
        guests: [{ guest_type: 'qemu', vmid: 102, name: 'alpha-vm', node: 'node-1', status: 'running', template: false },
            { guest_type: 'lxc', vmid: 252, name: 'alpha', node: 'node-2', status: 'running', template: false },
            { guest_type: 'qemu', vmid: 300, name: 'template', node: 'node-1', status: 'stopped', template: true }],
        servers: [{ id: 's1', name: 'alpha', template: false, link_revision: 3 },
            { id: 's2', name: 'beta', template: true, link_revision: 0 }],
        csrf_token: 'guest-links-token',
    };
}

function respondGuest(card, payload = sampleGuestLinks(), code = 200, index = card.browser.Ext.guestRequests.length - 1) {
    const request = card.browser.Ext.guestRequests[index];
    request.callback(request, code >= 200 && code < 300, { status: code, responseText: JSON.stringify(payload) });
}

function guestControl(card, name) { return card.grid('servers').down('#pve-komodo-guest-' + name); }
function pressGuest(card, name) { const button = guestControl(card, name); if (!button.disabled) button.handler(button); }
function readyGuestDashboard() {
    const card = dashboard(); card.activate(); card.respond(sampleDashboard()); respondGuest(card);
    return card;
}

test('server guest links load independently without cache busters and update records without losing selection', () => {
    const card = dashboard(); card.activate(); card.respond(sampleDashboard()); card.select('servers', ['s1']);
    const selected = card.grid('servers').getSelectionModel().getSelection()[0];
    const request = card.browser.Ext.guestRequests[0];
    assert.equal(request.method, 'GET'); assert.equal(request.disableCaching, false);
    assert.equal(guestControl(card, 'bind').disabled, true);
    respondGuest(card);
    assert.equal(card.grid('servers').getStore().data.length, 1);
    assert.equal(card.grid('servers').getSelectionModel().getSelection()[0], selected);
    assert.equal(card.grid('servers').getStore().data[0].pve_guest.vmid, 102);
    for (const name of ['bind', 'unlink', 'open']) assert.equal(guestControl(card, name).disabled, false);
});

test('guest links may finish before dashboard rows and failed links never clear the Docker tables or claim no link', () => {
    const card = dashboard(); card.activate(); respondGuest(card); card.respond(sampleDashboard());
    assert.equal(card.grid('servers').getStore().data[0].pve_guest.vmid, 102);
    assert.ok(card.grid('servers').getStore().updateEvents > 0, 'shared loadData objects must still emit ExtJS record updates');
    card.refresh(); card.respond(sampleDashboard()); respondGuest(card, { detail: 'private error text' }, 503);
    assert.equal(card.grid('servers').getStore().data.length, 1);
    const link = card.grid('servers').getStore().data[0].pve_guest;
    assert.equal(link.status, 'unavailable'); assert.equal(link.vmid, 102);
    assert.match(card.control('dashboard-status').text, /关联状态未知/);
    assert.doesNotMatch(card.control('dashboard-status').text, /private/);
    card.select('servers', ['s1']); assert.equal(guestControl(card, 'bind').disabled, true);
    const fresh = dashboard(); fresh.activate(); fresh.respond(sampleDashboard()); respondGuest(fresh, {}, 500);
    const column = fresh.grid('servers').columns.find((item) => item.dataIndex === 'pve_guest');
    assert.equal(column.renderer(fresh.grid('servers').getStore().data[0].pve_guest), '关联未知');
});

test('PVE guest labels encode names and distinguish missing, unverified and unreadable objects', () => {
    const column = dashboard().grid('servers').columns.find((item) => item.dataIndex === 'pve_guest');
    const link = { guest_type: 'lxc', vmid: 252, guest_name: '<img src=x>', status: 'linked' };
    assert.equal(column.renderer(null), '未关联');
    assert.match(column.renderer(link), /CT 252（&lt;img src=x&gt;）/);
    assert.match(column.renderer({ ...link, status: 'needs_confirmation' }), /待确认/);
    assert.match(column.renderer({ ...link, status: 'missing' }), /目标不存在或不可访问/);
    assert.match(column.renderer({ ...link, status: 'unavailable' }), /暂时无法校验/);
    assert.equal(column.renderer({ status: 'unknown' }), '关联未知');
});

test('single selection and permission checked mappings gate navigation using migration-stable PVE IDs', () => {
    const card = readyGuestDashboard(); const calls = [];
    card.panel.up = () => ({ workspace: { selectById: (id) => calls.push(id) } });
    assert.equal(guestControl(card, 'open').disabled, true);
    card.select('servers', ['s1']); pressGuest(card, 'open'); assert.deepEqual(calls, ['qemu/102']);
    for (const mutation of [{ status: 'needs_confirmation' }, { status: 'missing' }, { status: 'unavailable' }, { can_navigate: false }]) {
        const payload = sampleGuestLinks(); Object.assign(payload.links[0], mutation);
        card.refresh(); card.respond(sampleDashboard()); respondGuest(card, payload); card.select('servers', ['s1']);
        assert.equal(guestControl(card, 'open').disabled, true);
        // Even a direct cell click cannot bypass mapping validation.
        card.grid('servers').listeners.cellclick.call(card.grid('servers'), {
            getHeaderCt: () => ({ getHeaderAtIndex: () => ({ dataIndex: 'pve_guest' }) }),
        }, null, 0, { get: () => payload.links[0] });
    }
    assert.deepEqual(calls, ['qemu/102']);
});

test('binding requires an explicit candidate and confirmation, with CT identity warning and latest tombstone revision', () => {
    const card = readyGuestDashboard(); const payload = sampleGuestLinks(); payload.links = []; payload.servers[0].link_revision = 9;
    respondGuest(card, payload); card.select('servers', ['s1']);
    assert.equal(guestControl(card, 'unlink').disabled, true); pressGuest(card, 'bind');
    const chooser = card.browser.Ext.dialogs.at(-1);
    const combo = chooser.down('#pve-komodo-guest-target'); const save = chooser.down('#pve-komodo-guest-save');
    assert.equal(combo.getValue(), null); assert.equal(save.disabled, true);
    assert.equal(combo.store.data.length, 2, 'exclude PVE templates');
    assert.match(combo.store.data[0].label, /同名候选/);
    assert.equal(card.browser.Ext.guestRequests.length, 1, 'opening chooser never writes');
    combo.setValue('lxc/252'); assert.equal(save.disabled, false); save.handler();
    const confirm = card.browser.Ext.dialogs.at(-1);
    assert.match(confirm.options.message, /alpha.*CT 252/); assert.match(confirm.options.message, /较弱的身份校验/);
    assert.equal(confirm.options.defaultFocus, 'no'); assert.equal(chooser.destroyed, true);
    assert.equal(card.browser.Ext.guestRequests.length, 1); confirm.options.fn('yes');
    const request = card.browser.Ext.guestRequests.at(-1);
    assert.equal(request.method, 'POST'); assert.equal(request.url, '/pve-komodo/api/guest-links');
    assert.deepEqual(JSON.parse(JSON.stringify(request.jsonData)), { server_id: 's1', expected_revision: 9, guest_type: 'lxc', vmid: 252 });
    assert.equal(request.headers['X-PVE-Komodo-CSRF'], 'guest-links-token');
    assert.equal(card.browser.Ext.requests.length, 1, 'metadata never uses Komodo actions');
});

test('binding cancellation and selection changes never submit and unlink states its exact local-only effect', () => {
    const card = readyGuestDashboard(); card.select('servers', ['s1']); pressGuest(card, 'bind');
    const chooser = card.browser.Ext.dialogs.at(-1);
    chooser.down('#pve-komodo-guest-target').setValue('qemu/102'); chooser.down('#pve-komodo-guest-save').handler();
    card.browser.Ext.dialogs.at(-1).options.fn('no');
    assert.equal(card.browser.Ext.guestRequests.length, 1);
    pressGuest(card, 'unlink');
    const confirm = card.browser.Ext.dialogs.at(-1);
    assert.match(confirm.options.message, /仅删除本扩展中的映射/); assert.match(confirm.options.message, /不会修改 VM\/CT 或 Docker 服务/);
    card.grid('servers').getSelectionModel().deselectAll(); confirm.options.fn('yes');
    assert.equal(card.browser.Ext.guestRequests.length, 1, 'changed selection invalidates confirmation');
    card.select('servers', ['s1']); pressGuest(card, 'unlink'); card.browser.Ext.dialogs.at(-1).options.fn('yes');
    const request = card.browser.Ext.guestRequests.at(-1);
    assert.equal(request.url, '/pve-komodo/api/guest-links/unlink');
    assert.deepEqual(JSON.parse(JSON.stringify(request.jsonData)), { server_id: 's1', expected_revision: 3 });
    assert.equal(request.jsonData.guest_type, undefined);
});

test('guest-link GETs and dialogs are cancelled on leaving or destroying while late callbacks stay inert', () => {
    for (const operation of ['deactivate', 'destroy']) {
        const card = dashboard(); card.activate(); card.respond(sampleDashboard()); const request = card.browser.Ext.guestRequests[0];
        card[operation](); assert.equal(request.aborted, true);
        assert.doesNotThrow(() => respondGuest(card));
        assert.equal(card.panel._pveKomodoGuestReady, false);
        const ready = readyGuestDashboard(); ready.select('servers', ['s1']); pressGuest(ready, 'bind');
        const chooser = ready.browser.Ext.dialogs.at(-1); ready[operation](); assert.equal(chooser.destroyed, true);
        const confirmCard = readyGuestDashboard(); confirmCard.select('servers', ['s1']); pressGuest(confirmCard, 'unlink');
        const confirm = confirmCard.browser.Ext.dialogs.at(-1); confirmCard[operation](); assert.equal(confirm.destroyed, true);
        assert.doesNotThrow(() => confirm.options.fn('yes')); assert.equal(confirmCard.browser.Ext.guestRequests.length, 1);
    }
});

test('metadata POST survives navigation without retries or destroyed-card mutations and refreshes only the current home', () => {
    const old = readyGuestDashboard(); old.select('servers', ['s1']); pressGuest(old, 'unlink'); old.browser.Ext.dialogs.at(-1).options.fn('yes');
    const post = old.browser.Ext.guestRequests.at(-1); old.destroy(); assert.equal(post.aborted, undefined);
    const current = dashboard(old.browser); current.activate(); current.respond(sampleDashboard()); respondGuest(current);
    current.select('servers', ['s1']); assert.equal(guestControl(current, 'bind').disabled, true, 'pending write locks new card');
    post.callback(post, true, { status: 200, responseText: '{"removed":true}' });
    assert.equal(post.aborted, undefined); assert.equal(old.browser.Ext.guestRequests.at(-1).method, 'GET');
    assert.match(current.control('dashboard-status').text, /已解除关联/);
    const result = sampleGuestLinks(); result.links = []; result.servers[0].link_revision = 4; respondGuest(current, result);
    assert.equal(guestControl(current, 'bind').disabled, false);
    assert.equal(old.browser.Ext.guestRequests.filter((request) => request.method === 'POST').length, 1);
});

test('metadata conflicts require explicit refresh and never expose response bodies or retry the write', () => {
    for (const [code, detail, pattern] of [[409, 'guest_identity_unavailable', /无法核验/], [409, 'revision_conflict', /刷新后重新确认/],
        [403, 'invalid_csrf_token', /会话校验失败/], [500, '<script>PRIVATE</script>', /保存结果尚未确认/]]) {
        const card = readyGuestDashboard(); card.select('servers', ['s1']); pressGuest(card, 'unlink'); card.browser.Ext.dialogs.at(-1).options.fn('yes');
        respondGuest(card, { detail }, code);
        assert.match(card.control('dashboard-status').text, pattern); assert.doesNotMatch(card.control('dashboard-status').text, /PRIVATE/);
        assert.equal(card.browser.Ext.guestRequests.filter((request) => request.method === 'POST').length, 1);
        assert.equal(guestControl(card, 'bind').disabled, true, 'mapping must be refreshed before another manual write');
    }
});

test('VM and CT native Docker summary cards preserve official menus and options without eager network requests', () => {
    for (const type of ['qemu', 'lxc']) {
        const browser = runtime(); browser.load();
        const config = browser.Ext.create(type === 'qemu' ? 'PVE.qemu.Config' : 'PVE.lxc.Config', {
            pveSelNode: { data: { type, vmid: type === 'qemu' ? 102 : 252, node: 'node-1' } },
        });
        assert.deepEqual(menuIds(config.navigation), [type === 'qemu' ? 'hardware' : 'resources', 'pve-komodo-guest-docker']);
        assert.equal(config.baseInsertCalls, 1);
        const card = config.activateCard('pve-komodo-guest-docker');
        assert.equal(card, config.savedItems['pve-komodo-guest-docker'], 'Docker remains a selectable native card');
        assert.equal(card.title, 'Docker');
        assert.equal(card.pveKomodoGuestSummary, true);
        assert.equal(card.scrollable, true);
        assert.deepEqual(Array.from(card.items[0].items, (item) => item.title), ['当前', '备注', '操作记录']);
        const parent = config.root.findChild('id', 'pve-komodo-guest-docker');
        assert.equal(parent.data.expanded, true);
        assert.deepEqual(menuIds(parent.childNodes), ['pve-komodo-guest-docker-options', 'pve-komodo-guest-docker-docker', 'pve-komodo-guest-docker-resources', 'pve-komodo-guest-docker-terminal']);
        assert.equal(config.savedItems['pve-komodo-guest-docker-options'].title, '选项');
        assert.deepEqual(Array.from(parent.childNodes.slice(1), (node) => node.data.text), ['DOCKER', '资源', '终端']);
        assert.equal(browser.Ext.guestRequests.length, 0);
        assert.equal(browser.Ext.requests.length, 0);
    }
});

test('one-to-one binding never steals another server mapping and orphan cleanup requires its own local-only confirmation', () => {
    const card = readyGuestDashboard(); const payload = sampleGuestLinks();
    payload.links = [{ ...payload.links[0], server_id: 'retired-server', server_unavailable: true, server_name: null,
        status: 'needs_confirmation', revision: 12, can_navigate: false }];
    respondGuest(card, payload); card.select('servers', ['s1']); pressGuest(card, 'bind');
    const chooser = card.browser.Ext.dialogs.at(-1); const combo = chooser.down('#pve-komodo-guest-target');
    combo.setValue('qemu/102');
    assert.equal(chooser.down('#pve-komodo-guest-save').disabled, true);
    assert.equal(chooser.down('#pve-komodo-guest-cleanup').disabled, false);
    assert.match(combo.store.data.find((guest) => guest.key === 'qemu/102').label, /需先清理映射/);
    chooser.down('#pve-komodo-guest-cleanup').handler();
    const confirm = card.browser.Ext.dialogs.at(-1);
    assert.match(confirm.options.message, /retired-server/); assert.match(confirm.options.message, /仅删除本扩展中的映射/);
    assert.equal(card.browser.Ext.guestRequests.length, 1); confirm.options.fn('yes');
    const post = card.browser.Ext.guestRequests.at(-1);
    assert.equal(post.url, '/pve-komodo/api/guest-links/unlink');
    assert.deepEqual(JSON.parse(JSON.stringify(post.jsonData)), { server_id: 'retired-server', expected_revision: 12 });
    assert.equal(card.browser.Ext.requests.length, 1, 'cleanup never dispatches a Docker operation');
});

test('active mappings owned by another readable server cannot be rebound or cleaned as an orphan', () => {
    const card = readyGuestDashboard(); const payload = sampleGuestLinks();
    payload.servers.push({ id: 'other-server', name: 'other', template: false, link_revision: 2 });
    payload.links[0].server_id = 'other-server'; payload.links[0].revision = 2;
    respondGuest(card, payload); card.select('servers', ['s1']); pressGuest(card, 'bind');
    const chooser = card.browser.Ext.dialogs.at(-1); const combo = chooser.down('#pve-komodo-guest-target');
    combo.setValue('qemu/102'); assert.equal(chooser.down('#pve-komodo-guest-save').disabled, true);
    assert.equal(chooser.down('#pve-komodo-guest-cleanup').disabled, true);
    chooser.down('#pve-komodo-guest-save').handler(); chooser.down('#pve-komodo-guest-cleanup').handler();
    assert.equal(card.browser.Ext.guestRequests.length, 1);
});

test('multiple selected servers and Komodo templates never enable association writes', () => {
    const card = readyGuestDashboard(); card.control('exclude-templates').setValue(false);
    card.select('servers', ['s1', 's2']);
    for (const name of ['bind', 'unlink', 'open']) assert.equal(guestControl(card, name).disabled, true);
    card.select('servers', ['s2']); assert.equal(guestControl(card, 'bind').disabled, true);
    pressGuest(card, 'bind'); assert.equal(card.browser.Ext.dialogs.length, 0);
});

function sampleGuestOptions() {
    return {
        binding_revision: 7, server_id: 'a'.repeat(24), server_name: '<host>', server_state: 'Ok',
        version: 'b'.repeat(64), can_write: true, can_rotate: true, csrf_token: 'options-csrf', public_key: 'public-key',
        config: { enabled: true, auto_rotate_keys: false, address: 'wss://periphery:8120', insecure_tls: true,
            external_address: '', region: 'test', ignore_mounts: ['/boot', '/var/lib'], stats_monitoring: true,
            auto_prune: false, send_unreachable_alerts: true, send_version_mismatch_alerts: false,
            send_cpu_alerts: false, cpu_warning: 0, cpu_critical: 92.75, send_mem_alerts: true,
            mem_warning: 80, mem_critical: 95, send_disk_alerts: true, disk_warning: 75, disk_critical: 90,
            maintenance_windows: [{ name: '<backup>', description: 'existing', schedule_type: 'Weekly', day_of_week: 'Sunday',
                date: '2030-12-01', hour: 0, minute: 0, timezone: '', duration_minutes: 30, enabled: false }],
        },
    };
}

function guestOptionsPage(browser = runtime(), type = 'qemu', vmid = 102) {
    browser.load();
    const config = browser.Ext.create(type === 'qemu' ? 'PVE.qemu.Config' : 'PVE.lxc.Config', {
        pveSelNode: { data: { type, vmid, node: 'node-1' } },
    }).savedItems['pve-komodo-guest-docker-options'];
    const card = dashboard(browser, '', config);
    card.grid = () => card.panel.down('#pve-komodo-guest-options-grid');
    card.control = (name) => card.panel.down('#pve-komodo-guest-options-' + name);
    card.press = (name) => { const button = card.control(name); if (!button.disabled) button.handler(button); };
    card.selectOption = (key) => card.grid().getSelectionModel().select(card.grid().getStore().data.find((row) => row.key === key));
    card.edit = (key) => { card.selectOption(key); card.press('edit'); return browser.Ext.dialogs.at(-1); };
    return card;
}

test('guest options loads only on activation for the selected VM or CT and displays the complete real UI configuration', () => {
    for (const type of ['qemu', 'lxc']) {
        const page = guestOptionsPage(runtime(), type, type === 'qemu' ? 102 : 252);
        assert.equal(page.browser.Ext.requests.length, 0);
        page.activate();
        assert.equal(page.browser.Ext.requests[0].url, '/pve-komodo/api/guests/' + type + '/' + (type === 'qemu' ? 102 : 252) + '/options');
        assert.equal(page.browser.Ext.requests[0].method, 'GET');
        assert.equal(page.browser.Ext.requests[0].disableCaching, false);
        const payload = sampleGuestOptions(); payload.config.passkey = 'PRIVATE'; payload.config.links = ['https://example.test'];
        page.respond(payload);
        const rows = page.grid().getStore().data;
        assert.equal(rows.filter((row) => row.kind !== 'heading').length, 22);
        assert.deepEqual(Array.from(rows.filter((row) => row.kind !== 'heading'), (row) => row.key).sort(), Object.keys(payload.config).filter((key) => !['passkey', 'links'].includes(key)).concat('public_key').sort());
        assert.match(page.control('status').text, /关联服务器：&lt;host&gt;/);
        assert.doesNotMatch(JSON.stringify(rows), /PRIVATE|https:\/\/example/);
        const valueColumn = page.grid().columns[1];
        const render = (key) => { const row = rows.find((row) => row.key === key); return valueColumn.renderer(row.value, {}, { get: (name) => row[name] }); };
        assert.equal(render('auto_rotate_keys'), '否'); assert.equal(render('cpu_warning'), '0');
        assert.equal(render('region'), 'test'); assert.equal(render('maintenance_windows'), '1 个维护窗口');
        page.press('refresh'); page.respond({ ...payload, config: { ...payload.config, address: 'ws://periphery', cpu_warning: null } });
        assert.equal(page.grid().getStore().data.some((row) => row.key === 'insecure_tls'), false);
        page.selectOption('cpu_warning'); assert.equal(page.control('edit').disabled, true, 'a missing value never turns into an editable fake zero');
    }
});

test('server configuration follows two read-only heading levels with shared stack styles and unindented descriptions', () => {
    const page = guestOptionsPage(); page.activate(); page.respond(sampleGuestOptions());
    const grid = page.grid(), rows = grid.getStore().data;
    assert.match(grid.cls, /pve-komodo-stack-config-grid/);
    assert.match(grid.cls, /pve-komodo-guest-options-grid/);
    assert.deepEqual(Array.from(rows.filter((row) => row.kind === 'heading'), (row) => [row.label, row.heading]), [
        ['常规', 'major'], ['已启用', 'minor'], ['认证', 'minor'], ['地址', 'minor'], ['磁盘', 'minor'], ['监控', 'minor'], ['清理', 'minor'],
        ['告警', 'major'], ['无法连接', 'minor'], ['版本', 'minor'], ['CPU', 'minor'], ['内存', 'minor'], ['磁盘', 'minor'], ['维护', 'minor'],
    ]);
    assert.deepEqual(Array.from(rows.filter((row) => row.kind !== 'heading'), (row) => row.key), [
        'enabled', 'public_key', 'auto_rotate_keys', 'address', 'insecure_tls', 'external_address', 'region', 'ignore_mounts',
        'stats_monitoring', 'auto_prune', 'send_unreachable_alerts', 'send_version_mismatch_alerts', 'send_cpu_alerts', 'cpu_warning', 'cpu_critical',
        'send_mem_alerts', 'mem_warning', 'mem_critical', 'send_disk_alerts', 'disk_warning', 'disk_critical', 'maintenance_windows',
    ]);
    const dialogCount = page.browser.Ext.dialogs.length;
    for (const row of rows.filter((row) => row.kind === 'heading')) {
        const record = { get: (key) => row[key] };
        assert.equal(row.available, false);
        let cleared = false;
        assert.equal(grid.selModel.listeners.beforeselect({ deselectAll() { cleared = true; } }, record), false);
        assert.equal(cleared, true);
        assert.equal(grid.viewConfig.getRowClass(record), 'pve-komodo-stack-heading-' + row.heading);
        const description = grid.columns[1].renderer(undefined, {}, record);
        if (row.heading === 'minor') {
            assert.ok(row.description);
            assert.match(description, /^<span class="pve-komodo-stack-heading-description">[^ （].+[^）]<\/span>$/);
        } else assert.equal(description, '');
        grid.listeners.itemdblclick.call(grid, null, record);
    }
    assert.equal(page.browser.Ext.dialogs.length, dialogCount, 'heading rows never open editors');
    assert.equal(grid.viewConfig.variableRowHeight, true, 'long section descriptions can wrap');
    page.selectOption('region'); assert.equal(page.control('edit').disabled, false);
});

test('single-option editors retain false and zero, reset to real values, and confirm before discarding dirty changes', () => {
    const page = guestOptionsPage(); page.activate(); page.respond(sampleGuestOptions());
    const editor = page.edit('cpu_warning');
    const input = editor.down('#pve-komodo-option-value');
    assert.equal(input.getValue(), '0');
    input.setValue(34.5); editor.down('#pve-komodo-option-reset').handler(); assert.equal(input.getValue(), '0');
    editor.down('#pve-komodo-option-save').handler();
    assert.equal(page.browser.Ext.requests.length, 1); assert.match(page.control('status').text, /配置未改变/);
    input.setValue(44); editor.close();
    assert.match(page.browser.Ext.dialogs.at(-1).options.message, /尚未保存/);
    page.answer('no'); assert.equal(editor.destroyed, false);
    editor.down('#pve-komodo-option-cancel').handler(); page.answer('yes'); assert.equal(editor.destroyed, true);
    const checkbox = page.edit('auto_rotate_keys').down('#pve-komodo-option-value');
    assert.equal(checkbox.xtype, 'checkbox'); assert.equal(checkbox.getValue(), false);
    assert.equal(page.browser.Ext.requests.length, 1);
});

test('a confirmed option save sends only the changed field with the expected association, version, UUID and CSRF', () => {
    const page = guestOptionsPage(); page.activate(); page.respond(sampleGuestOptions());
    const editor = page.edit('cpu_warning'); editor.down('#pve-komodo-option-value').setValue(12.5);
    editor.down('#pve-komodo-option-save').handler();
    assert.match(page.browser.Ext.dialogs.at(-1).options.message, /&lt;host&gt;/);
    assert.equal(page.browser.Ext.requests.length, 1);
    page.answer('no'); assert.equal(page.browser.Ext.requests.length, 1);
    editor.down('#pve-komodo-option-save').handler(); page.answer('yes');
    const request = page.browser.Ext.requests[1];
    assert.equal(request.url, page.config.pveKomodoOptionsPath); assert.equal(request.method, 'POST');
    assert.equal(request.timeout, 120000); assert.equal(request.headers['X-PVE-Komodo-CSRF'], 'options-csrf');
    assert.deepEqual(JSON.parse(JSON.stringify(request.jsonData)), {
        action: 'save', server_id: 'a'.repeat(24), binding_revision: 7, base_version: 'b'.repeat(64),
        request_id: 'ceaa63ca-3693-4ef0-8cbf-4de83911a76b', config: { cpu_warning: 12.5 },
    });
    assert.equal(editor.destroyed, true); assert.equal(page.control('refresh').disabled, true);
    page.respond({ ok: true, status: 'completed', operation_id: 'c'.repeat(24) });
    assert.equal(page.browser.Ext.requests[2].method, 'GET', 'only confirmed completed saves refresh the authoritative configuration');
    page.respond({ ...sampleGuestOptions(), config: { ...sampleGuestOptions().config, cpu_warning: 12.5 } });
    assert.match(page.control('status').text, /配置已保存/);
    assert.equal(page.grid().getStore().data.find((row) => row.key === 'cpu_warning').value, 12.5);
});

test('threshold editing preserves precision beyond Ext NumberField defaults and rejects partial or non-finite numbers', () => {
    const page = guestOptionsPage(); page.activate();
    const payload = sampleGuestOptions(); payload.config.cpu_warning = 90.123456789;
    page.respond(payload);
    const editor = page.edit('cpu_warning'), input = editor.down('#pve-komodo-option-value');
    assert.equal(input.getValue(), '90.123456789');
    for (const invalid of ['', '90abc', 'Infinity', '0x12']) {
        input.setValue(invalid); editor.down('#pve-komodo-option-save').handler();
        assert.match(editor.down('#pve-komodo-option-error').html, /输入值无效/);
        assert.equal(page.browser.Ext.requests.length, 1);
    }
    editor.down('#pve-komodo-option-reset').handler(); assert.equal(input.getValue(), '90.123456789');
    input.setValue('89.123456789'); editor.down('#pve-komodo-option-save').handler(); page.answer('yes');
    assert.equal(page.browser.Ext.requests[1].jsonData.config.cpu_warning, 89.123456789);
    page.respond({ ok: true, status: 'completed' }); page.respond(payload);
});

test('public key editing and rotation are separate confirmed actions and never expose or request a private key', () => {
    const page = guestOptionsPage(); page.activate(); page.respond(sampleGuestOptions());
    const editor = page.edit('public_key'); editor.down('#pve-komodo-option-value').setValue('');
    editor.down('#pve-komodo-option-save').handler();
    assert.match(page.browser.Ext.dialogs.at(-1).options.message, /私钥不匹配/); page.answer('yes');
    assert.equal(page.browser.Ext.requests[1].jsonData.action, 'public_key');
    assert.equal(page.browser.Ext.requests[1].jsonData.public_key, '');
    assert.equal(page.browser.Ext.requests[1].jsonData.config, undefined);
    page.respond({ ok: true, status: 'completed' }); page.respond(sampleGuestOptions());
    page.press('rotate'); assert.match(page.browser.Ext.dialogs.at(-1).options.message, /Periphery 将在本机生成并保存新的私钥/);
    page.answer('no'); assert.equal(page.browser.Ext.requests.length, 3);
    page.press('rotate'); page.answer('yes'); assert.equal(page.browser.Ext.requests[3].jsonData.action, 'rotate_keys');
    assert.equal(page.browser.Ext.requests[3].jsonData.public_key, undefined);
    page.respond({ ok: true, status: 'completed' }); page.respond({ ...sampleGuestOptions(), can_rotate: false });
    assert.equal(page.control('rotate').disabled, true);
    page.press('refresh'); page.respond({ ...sampleGuestOptions(), can_write: false, can_rotate: false });
    page.selectOption('enabled'); assert.equal(page.control('edit').disabled, true);
    assert.match(page.control('status').text, /只读/);
});

test('maintenance windows preserve every field and Core timezone while native list edits remain drafts until confirmed', () => {
    const page = guestOptionsPage(); page.activate(); page.respond(sampleGuestOptions());
    const editor = page.edit('maintenance_windows');
    const grid = editor.down('#pve-komodo-maintenance-grid');
    assert.equal(grid.xtype, 'gridpanel'); assert.equal(grid.getStore().data[0].enabled, false);
    assert.ok(grid.columns.every((column) => column.sortable === false), 'native column sorting cannot change the window chosen for an index-based edit or deletion');
    assert.equal(grid.columns[0].renderer('<backup>'), '&lt;backup&gt;'); assert.equal(grid.columns[2].renderer(''), 'Core 时区');
    grid.getSelectionModel().select(grid.getStore().data[0]); grid.tbar[1].handler(grid.tbar[1]);
    const child = page.browser.Ext.dialogs.at(-1);
    assert.equal(child.down('#pve-komodo-maintenance-hour').getValue(), 0);
    assert.equal(child.down('#pve-komodo-maintenance-enabled').getValue(), false);
    child.down('#pve-komodo-maintenance-name').setValue('updated'); child.close(); page.answer('no');
    assert.equal(child.destroyed, false); child.down('#pve-komodo-maintenance-apply').handler();
    assert.equal(child.destroyed, true); assert.equal(grid.getStore().data[0].date, '2030-12-01', 'an unrelated schedule field is preserved');
    assert.equal(grid.getStore().data[0].timezone, '');
    editor.down('#pve-komodo-option-reset').handler(); assert.equal(grid.getStore().data[0].name, '<backup>');
    grid.tbar[0].handler(); const unchanged = page.browser.Ext.dialogs.at(-1);
    assert.equal(unchanged.down('#pve-komodo-maintenance-day_of_week').getValue(), null, 'ExtJS empty combo returns null');
    unchanged.close(); assert.equal(unchanged.destroyed, true, 'an untouched Daily window closes without a false dirty confirmation');
    grid.tbar[0].handler(); const added = page.browser.Ext.dialogs.at(-1);
    added.down('#pve-komodo-maintenance-hour').setValue(24); added.down('#pve-komodo-maintenance-apply').handler();
    assert.equal(added.destroyed, false); assert.match(added.down('#pve-komodo-maintenance-error').html, /输入值无效/);
    added.down('#pve-komodo-maintenance-hour').setValue(5); added.down('#pve-komodo-maintenance-name').setValue('new');
    added.down('#pve-komodo-maintenance-apply').handler(); assert.equal(grid.getStore().data.length, 2);
    grid.getSelectionModel().select(grid.getStore().data[0]); grid.tbar[2].handler(grid.tbar[2]);
    assert.equal(grid.getStore().data.length, 1);
    editor.down('#pve-komodo-option-save').handler(); assert.equal(page.browser.Ext.requests.length, 1);
    page.answer('yes');
    assert.deepEqual(Object.keys(page.browser.Ext.requests[1].jsonData.config), ['maintenance_windows']);
    assert.deepEqual(JSON.parse(JSON.stringify(page.browser.Ext.requests[1].jsonData.config.maintenance_windows)), [{
        name: 'new', description: '', schedule_type: 'Daily', day_of_week: '', date: '', hour: 5, minute: 0,
        timezone: '', duration_minutes: 60, enabled: true,
    }]);
    page.respond({ ok: true, status: 'completed' }); page.respond(sampleGuestOptions());
});

test('options failures cannot edit unbound, stale or unreadable targets and every GET lifecycle ignores late callbacks', () => {
    const page = guestOptionsPage(); page.activate();
    const first = page.browser.Ext.requests[0]; page.deactivate(); assert.equal(first.aborted, true);
    page.respond(sampleGuestOptions(), 200, 0); assert.equal(page.grid().getStore().data.length, 0);
    page.activate(); page.respond(sampleGuestOptions());
    const editor = page.edit('region'); editor.down('#pve-komodo-option-value').setValue('new-region');
    editor.down('#pve-komodo-option-save').handler(); const confirmation = page.browser.Ext.dialogs.at(-1);
    page.deactivate(); assert.equal(editor.destroyed, true); assert.equal(confirmation.destroyed, true);
    confirmation.options.fn('yes'); assert.equal(page.browser.Ext.requests.length, 2, 'leaving during confirmation never submits');
    page.activate();
    for (const [code, payload, pattern] of [
        [409, { detail: 'guest_not_linked' }, /尚未关联/], [409, { detail: 'guest_identity_changed' }, /已变化/],
        [403, { detail: 'PRIVATE' }, /无访问权限/], [502, { detail: '<script>PRIVATE' }, /读取失败/],
        [200, { ...sampleGuestOptions(), version: 'bad' }, /读取失败/],
    ]) {
        page.respond(payload, code); assert.equal(page.grid().getStore().data.length, 0);
        assert.match(page.control('status').text, pattern); assert.doesNotMatch(page.control('status').text, /PRIVATE|<script>/);
        assert.equal(page.control('edit').disabled, true); page.press('refresh');
    }
    const last = page.browser.Ext.requests.at(-1); page.destroy(); assert.equal(last.aborted, true);
    assert.doesNotThrow(() => page.respond(sampleGuestOptions()));
});

test('submitted option writes survive navigation with no abort or retry and unknown results require a manual read', () => {
    const browser = runtime(); const page = guestOptionsPage(browser); page.activate(); page.respond(sampleGuestOptions());
    const editor = page.edit('region'); editor.down('#pve-komodo-option-value').setValue('changed');
    editor.down('#pve-komodo-option-save').handler(); page.answer('yes');
    const post = browser.Ext.requests[1]; page.destroy(); assert.equal(post.aborted, undefined);
    const current = guestOptionsPage(browser); current.activate(); current.respond(sampleGuestOptions());
    current.selectOption('region'); assert.equal(current.control('edit').disabled, true, 'a new card cannot duplicate a pending write');
    current.respond({ ok: false, status: 'outcome_unknown', error_code: 'PRIVATE' }, 200, 1);
    assert.match(current.control('status').text, /结果尚未确认.*不要重复提交/);
    assert.doesNotMatch(current.control('status').text, /PRIVATE/);
    assert.equal(current.control('edit').disabled, true, 'a manual refresh is required before another edit');
    assert.equal(browser.Ext.requests.length, 3); assert.equal(post.aborted, undefined);
    current.press('refresh'); current.respond(sampleGuestOptions());
    const changed = current.edit('region'); changed.down('#pve-komodo-option-value').setValue('next');
    changed.down('#pve-komodo-option-save').handler(); current.answer('yes');
    current.respond({ detail: 'invalid_csrf_token' }, 403);
    assert.match(current.control('status').text, /本次未提交/);
    assert.equal(browser.Ext.requests.filter((request) => request.method === 'POST').length, 2);
});

test('a write completed on one guest always releases controls on a different active guest, including an unreadable page', () => {
    const browser = runtime(); const first = guestOptionsPage(browser); first.activate(); first.respond(sampleGuestOptions());
    first.press('rotate'); first.answer('yes'); first.destroy();
    const other = guestOptionsPage(browser, 'lxc', 252); other.activate(); other.respond({ detail: 'guest_not_linked' }, 409);
    assert.equal(other.control('refresh').disabled, true);
    other.respond({ ok: true, status: 'completed' }, 200, 1);
    assert.equal(other.control('refresh').disabled, false);
    assert.match(other.control('status').text, /尚未关联/, 'a different guest keeps its own status and data');
    assert.equal(browser.Ext.requests.length, 3, 'completion does not reload a different target');
});

test('completion of existing Docker actions or association writes releases an active guest option page', () => {
    for (const kind of ['action', 'binding']) {
        const home = readyGuestDashboard();
        if (kind === 'action') {
            home.select('procedures', ['p1']); home.click('procedures', 'run'); home.answer('yes');
        } else {
            home.select('servers', ['s1']); pressGuest(home, 'unlink'); home.answer('yes');
        }
        home.destroy();
        const page = guestOptionsPage(home.browser); page.activate(); page.respond({ detail: 'guest_not_linked' }, 409);
        assert.equal(page.control('refresh').disabled, true);
        if (kind === 'action') page.respond({ results: [{ id: 'p1', ok: true, status: 'submitted' }] }, 200, 1);
        else respondGuest(home, { removed: true });
        assert.equal(page.control('refresh').disabled, false);
        assert.equal(page.control('edit').disabled, true);
    }
});

function sampleGuestSummary() {
    return {
        server_id: 'a'.repeat(24), server_name: '<linked-host>', server_state: 'Ok', binding_revision: 4,
        description: '<script>unsafe()</script>\nsecond line', description_version: 'f'.repeat(64), can_execute: true, can_write: false, csrf_token: 'summary-csrf',
        current: { cpu_percent: 0, load_average: [0, 0.25, null], memory_used_gb: 0, memory_total_gb: 16, memory_percent: 0,
            disk_used_gb: null, disk_total_gb: 100, disk_percent: null, network_ingress_bytes_per_second: 512,
            network_egress_bytes_per_second: null, version: '<2.3.3>' },
        containers: [{ id: 'c'.repeat(64), name: '<api>', state: 'running', cpu_percent: '2.3%', memory_percent: '0%',
            memory_usage: '0MiB / 1GiB', network_io: '3kB / 4kB', block_io: '1B / 2B', pids: '3' },
            { id: 'd'.repeat(64), name: 'worker', state: 'paused', cpu_percent: null, memory_percent: null, memory_usage: null }],
        disks: [{ mount: '<root>', file_system: 'ext4', used_gb: 0, total_gb: 100, percent: 0 }],
        processes: [{ pid: 1, name: '<service>', exe: '/usr/bin/<service>', cpu_percent: 234.5, memory_mb: 0 },
            { pid: 9, name: 'other', exe: '/usr/bin/other', cpu_percent: null, memory_mb: null }],
        updates: [{ id: 'e'.repeat(24), operation: 'RestartAllContainers', status: 'InProgress', success: false, start_ts: 0, username: 'operator' }],
        updated_at: '2026-10-17T10:20:30Z', errors: [],
    };
}
function sampleGuestHistory(interval = '1-day') {
    return { server_id: 'a'.repeat(24), binding_revision: 4, interval, network_unit: 'bytes_per_sample',
        points: [{ ts: 1700000000123, load_average: [1, 2, 3], cpu_percent: 12.5, memory_used_gb: 2, memory_cache_gb: 1,
            memory_arc_gb: null, disk_percent: 55, network_ingress_bytes: 4096, network_egress_bytes: 1024 },
        { ts: 0, load_average: [0, null, 0], cpu_percent: 0, memory_used_gb: 0, memory_cache_gb: null, memory_arc_gb: 0,
            disk_percent: null, network_ingress_bytes: 0, network_egress_bytes: null }], errors: [], updated_at: '2026-10-17T10:20:30Z' };
}
function guestSummaryPage(browser = runtime(), type = 'qemu', vmid = 102) {
    browser.load();
    const config = browser.Ext.create(type === 'qemu' ? 'PVE.qemu.Config' : 'PVE.lxc.Config', {
        pveSelNode: { data: { type, vmid, node: 'node-1' } },
    }).savedItems['pve-komodo-guest-docker'];
    const page = dashboard(browser, '', config);
    page.control = (name) => page.panel.down('#pve-komodo-guest-' + name);
    page.grid = (section) => page.control(section + '-grid');
    page.chart = (name) => page.control('chart-' + name);
    page.press = (name) => { const button = page.control('summary-' + name); if (!button.disabled) button.handler(button); };
    return page;
}

test('guest Docker summary uses three overview columns, two horizontally collapsible grids, and lazy linked endpoints', () => {
    const page = guestSummaryPage(runtime(), 'lxc', 252);
    assert.equal(page.browser.Ext.requests.length, 0);
    assert.deepEqual(Array.from(page.config.tbar.items.filter((item) => item.itemId).slice(0, 5), (item) => item.text),
        ['重启容器', '暂停容器', '停止容器', '清理 Buildx', '清理系统']);
    assert.equal(page.config.tbar.overflowHandler, 'scroller');
    assert.deepEqual(Array.from(page.config.items[0].items, (item) => item.title), ['当前', '备注', '操作记录']);
    assert.deepEqual(Array.from(page.config.items[0].items, (item) => item.flex), [1, 1, 1]);
    assert.deepEqual(Array.from(page.config.items[1].items, (item) => item.title), ['容器', '进程']);
    assert.deepEqual(Array.from(page.config.items[1].items, (item) => item.flex), [1, 1]);
    assert.deepEqual(Array.from(page.config.items[1].items, (item) => item.collapseDirection), ['left', 'right']);
    assert.ok(page.config.items[1].items.every((item) => item.collapsible && item.animCollapse === false));
    assert.ok(page.control('recent-grid').columns.every((column) => column.flex > 0 && !column.width));
    assert.equal(page.grid('disks'), undefined);
    assert.ok(page.config.items[1].items.every((item) => item.height === 300));
    assert.deepEqual(Array.from(page.config.items[2].items, (row) => row.items.length), [3, 3]);
    assert.equal(page.config.tbar.items.at(-2), '->');
    assert.equal(page.config.tbar.items.at(-1).itemId, 'pve-komodo-guest-history-interval');
    assert.equal(page.config.items[2].tbar, undefined);
    const charts = ['load', 'cpu', 'memory', 'disk', 'ingress', 'egress'].map((name) => page.chart(name));
    assert.equal(new Set(charts.map((chart) => chart.store)).size, 6);
    assert.ok(charts.every((chart) => chart.xtype === 'proxmoxRRDChart' && chart.store.autoDestroy && !chart.store.proxy && !chart.rrdurl));
    assert.ok(charts.every((chart) => chart.noTool !== true), 'native RRD redraw callbacks retain the undo tool they expect');
    assert.ok(charts.every((chart) => chart.fields.every((field) => field.type === 'line' && field.fill === false && field.xField === 'time')));
    assert.equal(charts[0].fields.length, 3); assert.equal(charts[2].fields.length, 3);
    assert.equal(page.control('history-interval').getValue(), '1-day'); assert.equal(page.control('history-interval').store.data.length, 10);
    assert.match(page.control('history-interval').fieldLabel, /采样间隔/);
    page.activate(); assert.equal(page.browser.Ext.requests[0].url, '/pve-komodo/api/guests/lxc/252/summary');
    assert.equal(page.browser.Ext.requests[0].disableCaching, false);
    page.respond(sampleGuestSummary());
    assert.equal(page.browser.Ext.requests[1].url, '/pve-komodo/api/guests/lxc/252/history');
    assert.equal(page.browser.Ext.requests[1].disableCaching, false);
    assert.deepEqual(JSON.parse(JSON.stringify(page.browser.Ext.requests[1].params)), { interval: '1-day' });
    assert.ok(page.browser.Ext.requests.every((request) => request.method === 'GET'));
    const other = guestSummaryPage();
    assert.notEqual(other.chart('load').store, page.chart('load').store);
    assert.notEqual(other.chart('load').axes, page.chart('load').axes);
});

test('summary renders real zero and missing metrics, safe descriptions and fields, and filters only its local container and process data', () => {
    const page = guestSummaryPage(); page.activate(); page.respond(sampleGuestSummary());
    assert.equal(page.control('current-cpu').text, '0.0%'); assert.equal(page.control('current-cpu').usage, 0);
    assert.match(page.control('current-memory').text, /0\.00 GiB \/ 16\.00 GiB/);
    assert.match(page.control('current-disk').text, /^-（- \/ 100\.00 GiB）$/);
    assert.equal(page.control('current-disk').usage, 0);
    assert.equal(page.control('current-load').text, '0.00 / 0.25 / -');
    assert.equal(page.control('current-version').text, '&lt;2.3.3&gt;');
    assert.equal(page.control('description').html, '&lt;script&gt;unsafe()&lt;/script&gt;<br>second line');
    assert.match(page.control('summary-status').text, /&lt;linked-host&gt;.*监控缓存/);
    const container = page.grid('containers'); assert.equal(container.collapsible, true);
    assert.equal(container.columns[0].renderer('<api>'), '&lt;api&gt;');
    assert.equal(container.columns.find((column) => column.dataIndex === 'network_io').renderer('3kB / 4kB'), '3kB / 4kB');
    const processes = page.grid('processes');
    assert.equal(processes.columns.find((column) => column.dataIndex === 'cpu_percent').renderer(234.5), '234.5%');
    assert.equal(processes.columns.find((column) => column.dataIndex === 'memory_mb').renderer(0), '0.00 MiB');
    page.control('containers-search').setValue('worker'); assert.equal(container.getStore().data.length, 1);
    assert.equal(container.getStore().data[0].name, 'worker');
    page.control('processes-search').setValue('service'); assert.equal(processes.getStore().data.length, 1);
    assert.equal(processes.getStore().data[0].pid, 1);
    assert.equal(page.grid('disks'), undefined);
    assert.equal(page.browser.Ext.requests.length, 2, 'search never sends a request or changes guest association');
    assert.equal(page.control('recent-grid').columns[1].renderer(null, {}, { data: sampleGuestSummary().updates[0] }), '进行中');
    assert.equal(page.control('summary-stop_all_containers').disabled, false, 'Execute permission does not require configuration Write permission');
});

test('current More opens only the current disk snapshot in a native grid and closes it when the guest data changes', () => {
    const page = guestSummaryPage(); page.activate();
    assert.equal(page.control('current-more').disabled, true);
    page.respond(sampleGuestSummary()); page.respond(sampleGuestHistory());
    const button = page.control('current-more'); assert.equal(button.disabled, false); button.handler(button);
    const dialog = page.browser.Ext.dialogs.at(-1); assert.equal(dialog.title, '磁盘信息');
    const grid = dialog.down('#pve-komodo-guest-disks-grid');
    assert.equal(grid.getStore().data.length, 1); assert.equal(grid.getStore().data[0].mount, '<root>');
    assert.equal(grid.columns[0].renderer('<root>'), '&lt;root&gt;');
    assert.equal(grid.columns.find((column) => column.dataIndex === 'used_gb').renderer(0), '0.00 GiB');
    assert.equal(grid.columns.find((column) => column.dataIndex === 'percent').renderer(null), '-');
    assert.equal(page.browser.Ext.requests.length, 2, 'opening disk details never sends an API request');
    button.handler(button); assert.equal(page.browser.Ext.dialogs.at(-1), dialog);
    page.press('refresh'); assert.equal(dialog.destroyed, true); assert.equal(button.disabled, true);
    page.respond({ ...sampleGuestSummary(), disks: [] }); page.respond(sampleGuestHistory());
    button.handler(button); const empty = page.browser.Ext.dialogs.at(-1);
    assert.equal(empty.down('#pve-komodo-guest-disks-grid').getStore().data.length, 0);
    page.deactivate(); assert.equal(empty.destroyed, true);
    assert.equal(page.browser.Ext.requests.filter((request) => request.method === 'POST').length, 0);
});

function descriptionEditor(page) {
    const tool = page.control('description-edit');
    if (!tool.disabled) tool.callback(tool);
    return page.browser.Ext.dialogs.at(-1);
}

test('description editing uses a plain text draft with reset, unchanged-save and dirty cancel without writes', () => {
    const page = guestSummaryPage(); page.activate(); page.respond(sampleGuestSummary()); page.respond(sampleGuestHistory());
    assert.equal(page.control('description-edit').disabled, true, 'Execute permission cannot edit metadata');
    page.press('refresh'); const data = { ...sampleGuestSummary(), can_write: true };
    page.respond(data); page.respond(sampleGuestHistory());
    let editor = descriptionEditor(page);
    const value = editor.down('#pve-komodo-description-value');
    assert.equal(value.getValue(), data.description); assert.equal(value.xtype, 'textareafield');
    assert.equal(page.control('summary-refresh').disabled, true);
    value.setValue('unsaved'); editor.down('#pve-komodo-description-reset').handler();
    assert.equal(value.getValue(), data.description);
    editor.down('#pve-komodo-description-save').handler(); assert.equal(editor.destroyed, true);
    assert.match(page.control('summary-status').text, /未改变/);
    editor = descriptionEditor(page); editor.down('#pve-komodo-description-value').setValue('new draft');
    editor.down('#pve-komodo-description-cancel').handler(); page.answer('no'); assert.equal(editor.destroyed, false);
    editor.close(); page.answer('yes'); assert.equal(editor.destroyed, true);
    assert.equal(page.browser.Ext.requests.filter((request) => request.method === 'POST').length, 0);
    assert.equal(page.control('description').html, '&lt;script&gt;unsafe()&lt;/script&gt;<br>second line');
});

test('description saves one fixed bound metadata payload then reloads authoritative text, and conflicts require refresh', () => {
    const page = guestSummaryPage(); page.activate(); page.respond({ ...sampleGuestSummary(), can_write: true }); page.respond(sampleGuestHistory());
    let editor = descriptionEditor(page); editor.down('#pve-komodo-description-value').setValue('中文\n<b>description</b>');
    editor.down('#pve-komodo-description-save').handler();
    const post = page.browser.Ext.requests[2];
    assert.equal(post.url, '/pve-komodo/api/guests/qemu/102/description');
    assert.equal(post.method, 'POST'); assert.equal(post.headers['X-PVE-Komodo-CSRF'], 'summary-csrf');
    assert.deepEqual(JSON.parse(JSON.stringify(post.jsonData)), { server_id: 'a'.repeat(24), binding_revision: 4,
        base_version: 'f'.repeat(64), description: '中文\n<b>description</b>', request_id: 'ceaa63ca-3693-4ef0-8cbf-4de83911a76b' });
    page.respond({ ok: true, status: 'completed', operation_id: null, error_code: null });
    assert.equal(page.browser.Ext.requests[3].method, 'GET'); assert.equal(page.browser.Ext.requests[3].url, '/pve-komodo/api/guests/qemu/102/summary');
    page.respond({ ...sampleGuestSummary(), can_write: true, description: '中文\n<b>description</b>', description_version: 'e'.repeat(64) });
    page.respond(sampleGuestHistory()); assert.equal(page.control('description').html, '中文<br>&lt;b&gt;description&lt;/b&gt;');
    editor = descriptionEditor(page); editor.down('#pve-komodo-description-value').setValue(''); editor.down('#pve-komodo-description-save').handler();
    assert.equal(page.browser.Ext.requests.at(-1).jsonData.description, '', 'blank description explicitly clears metadata');
    page.respond({ detail: 'server_description_conflict' }, 409);
    assert.match(page.control('summary-status').text, /其他页面修改.*未保存/); assert.equal(page.control('description-edit').disabled, true);
    assert.equal(page.browser.Ext.requests.filter((request) => request.method === 'POST').length, 2);
});

test('description navigation destroys unsaved dialogs and preserves in-flight writes without retry or stale callbacks', () => {
    const browser = runtime(); const page = guestSummaryPage(browser); page.activate();
    page.respond({ ...sampleGuestSummary(), can_write: true }); page.respond(sampleGuestHistory());
    let editor = descriptionEditor(page); editor.down('#pve-komodo-description-value').setValue('draft'); editor.close();
    const confirmation = browser.Ext.dialogs.at(-1); page.deactivate(); assert.equal(editor.destroyed, true); assert.equal(confirmation.destroyed, true);
    confirmation.options.fn('yes'); assert.equal(browser.Ext.requests.filter((request) => request.method === 'POST').length, 0);
    page.activate(); page.respond({ ...sampleGuestSummary(), can_write: true }); page.respond(sampleGuestHistory());
    editor = descriptionEditor(page); editor.down('#pve-komodo-description-value').setValue('save'); editor.down('#pve-komodo-description-save').handler();
    const postIndex = browser.Ext.requests.length - 1; const post = browser.Ext.requests[postIndex]; page.destroy(); assert.equal(post.aborted, undefined);
    const options = guestOptionsPage(browser); options.activate(); options.respond(sampleGuestOptions()); options.selectOption('region');
    assert.equal(options.control('edit').disabled, true);
    options.respond({ ok: false, status: 'outcome_unknown', operation_id: null }, 200, postIndex);
    assert.equal(options.control('edit').disabled, false);
    assert.equal(browser.Ext.requests.filter((request) => request.method === 'POST').length, 1);
});

test('history preserves irregular Unix millisecond times, real zero, gaps and binary units without manufacturing network rates', () => {
    const page = guestSummaryPage(); page.activate(); page.respond(sampleGuestSummary()); page.respond(sampleGuestHistory());
    const rows = page.chart('memory').getStore().data;
    assert.deepEqual(Array.from(rows, (row) => row.time), [0, 1700000000123]);
    assert.equal(rows[0].load1, 0); assert.equal(rows[0].load5, null); assert.equal(rows[0].memory_used, 0);
    assert.equal(rows[0].memory_cache, null); assert.equal(rows[0].memory_arc, 0);
    assert.equal(rows[1].memory_used, 2 * 1073741824); assert.equal(rows[1].memory_cache, 1073741824);
    assert.equal(rows[1].memory_arc, null); assert.equal(rows[1].cpu_percent, 12.5);
    assert.equal(rows[1].network_ingress_bytes, 4096); assert.equal(rows[0].network_egress_bytes, null);
    assert.equal(page.chart('memory').unit, 'bytes'); assert.equal(page.chart('memory').powerOfTwo, true);
    assert.equal(page.chart('ingress').unit, 'bytes'); assert.match(page.chart('ingress').title, /采样/);
    assert.match(page.control('history-status').text, /2 个真实采样点/);
    assert.equal(page.chart('cpu').store.fields.includes('time'), true);
    assert.equal(page.chart('cpu').store.model, undefined, 'PVE guest models must not multiply CPU or interpret timestamps as seconds');
});

test('history interval changes cancel obsolete reads and reject data from a different guest association', () => {
    const page = guestSummaryPage(); page.activate(); page.respond(sampleGuestSummary());
    const old = page.browser.Ext.requests[1];
    page.control('history-interval').setValue('5-min');
    assert.equal(old.aborted, true); assert.equal(page.browser.Ext.requests[2].params.interval, '5-min');
    page.respond(sampleGuestHistory(), 200, 1); assert.equal(page.chart('cpu').getStore().data.length, 0);
    page.respond({ ...sampleGuestHistory('5-min'), binding_revision: 5 });
    assert.equal(page.chart('cpu').getStore().data.length, 0); assert.match(page.control('history-status').text, /读取失败/);
    page.control('history-interval').setValue('1-min');
    page.respond({ ...sampleGuestHistory('1-min'), server_id: 'b'.repeat(24) });
    assert.equal(page.chart('cpu').getStore().data.length, 0);
    page.control('history-interval').setValue('15-min'); page.respond({ ...sampleGuestHistory('15-min'), points: [] });
    assert.match(page.control('history-status').text, /暂无历史采样/);
    assert.ok(page.browser.Ext.requests.every((request) => request.disableCaching === false));
});

test('summary refresh and leaving cancel only reads and clear stale data without late callbacks changing destroyed components', () => {
    const page = guestSummaryPage(); page.activate(); page.respond(sampleGuestSummary());
    const history = page.browser.Ext.requests[1]; page.press('refresh'); assert.equal(history.aborted, true);
    assert.equal(page.grid('containers').getStore().data.length, 0); assert.equal(page.chart('cpu').getStore().data.length, 0);
    page.respond(sampleGuestHistory(), 200, 1); assert.equal(page.chart('cpu').getStore().data.length, 0);
    for (const [code, payload, pattern] of [[409, { detail: 'guest_not_linked' }, /尚未关联/],
        [403, { detail: '<script>PRIVATE' }, /无访问权限/], [502, { detail: 'PRIVATE' }, /概要读取失败/],
        [200, { ...sampleGuestSummary(), binding_revision: null }, /概要读取失败/]]) {
        page.respond(payload, code); assert.match(page.control('summary-status').text, pattern);
        assert.doesNotMatch(page.control('summary-status').text, /PRIVATE|<script>/);
        assert.equal(page.control('summary-prune_system').disabled, true); page.press('refresh');
    }
    const final = page.browser.Ext.requests.at(-1); page.destroy(); assert.equal(final.aborted, true);
    assert.doesNotThrow(() => page.respond(sampleGuestSummary()));
});

test('all five guest summary actions explain their real effects, require confirmation, and submit a frozen linked target', () => {
    const page = guestSummaryPage(); page.activate(); page.respond(sampleGuestSummary()); page.respond(sampleGuestHistory());
    for (const [action, pattern] of [['restart_all_containers', /包括原先已停止的容器.*komodo\.skip/],
        ['pause_all_containers', /冻结容器进程.*可能操作失败/], ['stop_all_containers', /中断相关服务/],
        ['prune_buildx', /未使用的 Buildx 构建缓存/], ['prune_system', /--volumes.*卷数据可能被永久删除/]]) {
        page.press(action); const confirmation = page.browser.Ext.dialogs.at(-1);
        assert.match(confirmation.options.message, pattern); assert.match(confirmation.options.message, /&lt;linked-host&gt;/);
        page.answer('no'); assert.equal(page.browser.Ext.requests.length, 2);
    }
    page.press('restart_all_containers'); const confirmation = page.browser.Ext.dialogs.at(-1);
    page.answer('yes'); confirmation.options.fn('yes');
    const post = page.browser.Ext.requests[2];
    assert.equal(post.url, '/pve-komodo/api/guests/qemu/102/actions'); assert.equal(post.method, 'POST');
    assert.equal(post.headers['X-PVE-Komodo-CSRF'], 'summary-csrf');
    assert.deepEqual(JSON.parse(JSON.stringify(post.jsonData)), { action: 'restart_all_containers', server_id: 'a'.repeat(24),
        binding_revision: 4, request_id: 'ceaa63ca-3693-4ef0-8cbf-4de83911a76b' });
    page.respond({ section: 'servers', action: 'restart_all_containers', results: [{ id: 'a'.repeat(24), ok: true, status: 'submitted' }] });
    assert.match(page.control('summary-status').text, /已提交，尚未确认任务完成/);
    assert.equal(page.browser.Ext.requests.length, 3, 'no automatic POST retry or refresh after submission');
    assert.equal(page.control('summary-restart_all_containers').disabled, true, 'a manual authoritative refresh precedes another action');
});

test('guest summary POSTs survive navigation and globally lock home and options until a safe callback unlocks current controls', () => {
    const browser = runtime(); const page = guestSummaryPage(browser); page.activate(); page.respond(sampleGuestSummary()); page.respond(sampleGuestHistory());
    page.press('prune_system'); page.answer('yes'); const post = browser.Ext.requests[2]; page.destroy();
    assert.equal(post.aborted, undefined);
    const options = guestOptionsPage(browser); options.activate(); options.respond(sampleGuestOptions()); options.selectOption('region');
    assert.equal(options.control('edit').disabled, true);
    options.respond({ section: 'servers', action: 'prune_system', results: [{ id: 'a'.repeat(24), ok: false, status: 'outcome_unknown', error_code: 'PRIVATE' }] }, 200, 2);
    assert.equal(options.control('edit').disabled, false);
    assert.equal(post.aborted, undefined); assert.equal(browser.Ext.requests.filter((request) => request.method === 'POST').length, 1);
    options.destroy(); const current = guestSummaryPage(browser); current.activate(); current.respond(sampleGuestSummary()); current.respond(sampleGuestHistory());
    current.press('pause_all_containers'); const confirmation = browser.Ext.dialogs.at(-1); current.destroy();
    assert.equal(confirmation.destroyed, true); confirmation.options.fn('yes');
    assert.equal(browser.Ext.requests.filter((request) => request.method === 'POST').length, 1, 'leaving a confirmation never submits later');
});

test('other global writes unlock an active summary page and stale or unknown action responses never trigger retries', () => {
    const browser = runtime(); const options = guestOptionsPage(browser); options.activate(); options.respond(sampleGuestOptions());
    options.press('rotate'); options.answer('yes'); options.destroy();
    const page = guestSummaryPage(browser); page.activate(); page.respond(sampleGuestSummary()); page.respond(sampleGuestHistory());
    assert.equal(page.control('summary-refresh').disabled, true);
    page.respond({ ok: true, status: 'completed' }, 200, 1); assert.equal(page.control('summary-refresh').disabled, false);
    page.press('stop_all_containers'); page.answer('yes');
    page.respond({ detail: 'invalid_csrf_token' }, 403);
    assert.match(page.control('summary-status').text, /本次未提交/);
    page.press('refresh'); page.respond(sampleGuestSummary()); page.respond(sampleGuestHistory());
    page.press('stop_all_containers'); page.answer('yes'); page.respond('PRIVATE malformed body', 502);
    assert.match(page.control('summary-status').text, /结果尚未确认.*不要重复提交/);
    assert.doesNotMatch(page.control('summary-status').text, /PRIVATE/);
    assert.equal(browser.Ext.requests.filter((request) => request.method === 'POST').length, 3);
});

function containerViewBrowser() {
    const Ext = createExt();
    const on = function (name, fn) { (this.events ||= new Map()).set(name, [...(this.events.get(name) || []), fn]); };
    const fire = function (name, ...args) {
        (this.events?.get(name) || []).forEach((fn) => fn.apply(this, args));
        this.listeners?.[name]?.apply(this, args);
    };
    const node = (data, parent = null) => {
        const record = { data: { ...data }, parentNode: parent, get(key) { return this.data[key]; }, getId() { return this.data.id; },
            isExpanded() { return this.data.expanded === true; }, expand() { this.data.expanded = true; }, collapse() { this.data.expanded = false; },
            cascadeBy(fn) { fn(this); this.childNodes.forEach((child) => child.cascadeBy(fn)); } };
        record.childNodes = (data.children || []).map((child) => node(child, record));
        return record;
    };
    Ext.define('Ext.tree.Panel', { extend: 'Ext.panel.Panel', on, fire,
        initComponent: function () {
            this.controls = new Map(); this.storeConfig = this.store; this.mutations = 0;
            this.setRootNode(this.store.root);
            const tree = this;
            this.statusToolbar = { destroyed: false, visible: true, setVisible(value) { this.visible = value; } };
            this.dataStore = { getNodeById(id) { let found; tree.rootNode.cascadeBy((record) => { if (record.getId() === id) found = record; }); return found; } };
            this.selection = { selected: [], getSelection() { return this.selected; }, select(record) { this.selected = [record]; } };
            for (const item of [...this.tbar, ...this.bbar]) this.controls.set('#' + item.itemId, {
                ...item, value: '', destroyed: false,
                getValue() { return this.value; }, setValue(value) { this.value = value; item.listeners?.change?.(this); },
                setDisabled(value) { assert.equal(tree.destroyed, undefined); this.disabled = value; },
                setText(value) { assert.equal(tree.destroyed, undefined); this.text = value; tree.mutations++; },
                up(selector) { return selector === 'toolbar' ? tree.statusToolbar : null; },
            });
            return this.callParent(arguments);
        },
        setRootNode: function (data) { assert.equal(this.destroyed, undefined); this.rootNode = node(data); this.mutations++; },
        getRootNode: function () { return this.rootNode; }, getStore: function () { return this.dataStore; },
        getSelectionModel: function () { return this.selection; }, down: function (selector) { return this.controls.get(selector); },
        setVisible: function (value) { this.hidden = !value; },
        destroy: function () { this.fire('beforedestroy'); this.destroyed = true; },
    });
    Ext.define('PVE.form.ViewSelector', { extend: 'Ext.panel.Panel',
        initComponent: function () {
            const rows = ['server', 'folder', 'pool', 'tags'].map((key) => ({ key, value: key }));
            this.store = { data: rows, findRecord(field, value) { return rows.find((row) => row[field] === value); }, add(row) { rows.push(row); } };
            this.value = 'server'; return this.callParent(arguments);
        }, getStore: function () { return this.store; }, getValue: function () { return this.value; },
        applyState: function (value) { if (this.store.findRecord('key', value)) this.value = value; },
        getViewFilter: function () { return { id: this.value, groups: this.value === 'folder' ? ['type'] : ['node'] }; },
    });
    Ext.define('PVE.StdWorkspace', { extend: 'Ext.panel.Panel',
        initComponent: function () { this.nativeSelections = []; this.menuSelections = []; this.contentRebuilds = 0; this.configPanels = []; return this.callParent(arguments); },
        onLogin: function (data) { this.lastLogin = data; return 'login-return'; },
        setContent: function (config) { this.content = config; this.onSetContent?.(config); },
        selectById: function (id) { return this.native.selectById(id); }, query: function () { return this.configPanels; },
    });
    Ext.define('PVE.tree.ResourceTree', { extend: 'Ext.panel.Panel', on, fire,
        initComponent: function () {
            this.filterCalls = []; this.nativeResourceIds = ['root', 'qemu/102', 'lxc/252'];
            const original = this;
            this.selection = { suspended: 0, suspendEvents() { this.suspended++; }, resumeEvents() { this.suspended--; } };
            this.setViewFilter = function (view) {
                assert.equal(this, original, 'original instance filter keeps its receiver');
                this.viewFilter = view; this.filterCalls.push(view);
                if (!this.selection.suspended) this.workspace.contentRebuilds++;
                return 'native-filter-return';
            };
            return this.callParent(arguments);
        }, up: function () { return this.workspace; }, setVisible: function (value) { this.hidden = !value; },
        getSelectionModel: function () { return this.selection; },
        selectById: function (id) {
            if (!this.nativeResourceIds.includes(id)) return null;
            this.workspace.nativeSelections.push(id); this.workspace.onNativeSelect?.(id);
            return { data: { id } };
        },
        destroy: function () { this.fire('beforedestroy'); this.destroyed = true; },
    });
    const browser = runtime(Ext);
    browser.authenticated = true;
    browser.context.window.Proxmox.UserName = 'root@pam';
    browser.context.window.Proxmox.Utils.authOK = () => browser.authenticated;
    browser.load();
    return browser;
}
function containerView(browser = containerViewBrowser(), initial = { id: 'server', groups: ['node'] }) {
    const workspace = browser.Ext.create('PVE.StdWorkspace');
    const native = browser.Ext.create('PVE.tree.ResourceTree', { workspace, viewFilter: initial });
    workspace.native = native;
    native.ownerCt = { items: [native], add(tree) { this.items.push(tree); return tree; } };
    native.fire('afterrender');
    const state = native._pveKomodoContainerTree;
    return { browser, workspace, native, tree: state.tree,
        switch(id) { return native.setViewFilter({ id, groups: id === 'folder' ? ['type'] : ['node'] }); },
        node: (id) => state.tree.getStore().getNodeById(id),
        click(id) { const record = this.node(id); assert.ok(record, 'expected tree node ' + id); state.tree.getSelectionModel().select(record); state.tree.fire('itemclick', state.tree, record); },
        control: (name) => state.tree.down('#pve-komodo-tree-' + name),
        refresh() { this.control('refresh').handler(); },
        respond(data, status = 200, kind = 'containers', index) {
            const requests = kind === 'links' ? browser.Ext.guestRequests : browser.Ext.requests;
            const request = requests[index ?? requests.length - 1];
            request.callback(request, status >= 200 && status < 300, { status, responseText: JSON.stringify(data) });
        },
    };
}
function sampleContainerTree() {
    const data = sampleContainers();
    data.stacks = [{ id: '3'.repeat(24), name: '<stack-a>', server_id: '1'.repeat(24) },
        { id: '4'.repeat(24), name: 'empty-stack', server_id: '1'.repeat(24) }];
    data.containers.forEach((row, index) => {
        row.stack_id = [0, 2].includes(index) ? '3'.repeat(24) : null;
        row.stack_association = [0, 2].includes(index) ? 'matched' : index === 3 ? 'unknown' : 'unassigned';
    });
    return data;
}
function sampleTreeLinks() {
    return { links: [{ server_id: '1'.repeat(24), guest_type: 'qemu', vmid: 102, guest_name: '<vm-name>',
        guest_status: 'running', status: 'linked', can_navigate: true, revision: 1 }], errors: [] };
}

test('container view appends one state-restorable selector option and uses an independent three-level tree without native resources or rid mutations', () => {
    const view = containerView(); const { browser } = view;
    const selector = browser.Ext.create('PVE.form.ViewSelector');
    assert.deepEqual(selector.store.data.map((row) => row.key), ['server', 'folder', 'pool', 'tags', 'pve-komodo-containers']);
    selector.applyState('pve-komodo-containers'); assert.equal(selector.getViewFilter().id, 'pve-komodo-containers');
    assert.equal(selector.store.data.at(-1).value, '容器视图');
    assert.equal(view.native.ownerCt.items.length, 2); assert.equal(view.tree.hidden, true);
    assert.equal(view.tree.storeConfig.model, 'Ext.data.TreeModel'); assert.equal(view.tree.stateful, false);
    assert.equal(browser.Ext.requests.length, 0); assert.equal(browser.Ext.guestRequests.length, 0);
    view.switch('pve-komodo-containers');
    assert.equal(view.native.hidden, true); assert.equal(view.tree.hidden, false);
    view.respond(sampleContainerTree()); view.respond(sampleTreeLinks(), 200, 'links');
    assert.equal(view.control('status').text, '', 'successful tree loads have no persistent footer explanation');
    assert.equal(view.tree.statusToolbar.visible, false);
    const server = view.node('server:' + '1'.repeat(24));
    assert.equal(server.get('binding'), 'VM 102（<vm-name>）');
    assert.equal(server.get('text'), '102 <vm-name> [host-a]');
    assert.equal(server.get('iconCls'), 'fa fa-desktop running');
    assert.deepEqual(Array.from(server.childNodes, (record) => record.get('pveKomodoKind')), ['stack', 'stack', 'containers']);
    assert.equal(server.childNodes[0].childNodes.length, 2);
    assert.equal(server.childNodes[2].get('text'), '容器');
    assert.equal(server.childNodes[2].childNodes[0].get('association'), '应用栈归属暂时无法核验');
    assert.equal(view.node('server:' + '2'.repeat(24)).get('binding'), '未关联 CT 或 VM');
    assert.deepEqual(view.native.nativeResourceIds, ['root', 'qemu/102', 'lxc/252']);
    assert.equal(view.workspace.nativeSelections.length, 0);
    const renderer = view.tree.columns[0].renderer;
    assert.match(renderer('<stack-a>', {}, server.childNodes[0]), /&lt;stack-a&gt;/);
    assert.doesNotMatch(renderer(server.get('text'), {}, server), /<vm-name>/);
    assert.equal(renderer(server.get('text'), {}, server), '102 &lt;vm-name&gt; [host-a]');
    assert.ok([...browser.Ext.requests, ...browser.Ext.guestRequests].every((request) => request.method === 'GET' && request.disableCaching === false));
});

test('container leaves use native status icons and escaped tooltips without appending status to names', () => {
    const cases = [
        ['running', 'running'], ['paused', 'paused'], ['created', 'stopped'], ['exited', 'stopped'], ['stopped', 'stopped'],
        ['restarting', 'pending'], ['stopping', 'pending'], ['removing', 'pending'], ['dead', 'error'],
        ['Running', 'running'], ['', 'unknown'], [null, 'unknown'], ['"><img src=x onerror="bad"> &', 'unknown'],
    ];
    const data = sampleContainers(), base = data.containers[0];
    data.containers = cases.map(([state], index) => {
        const containerId = index.toString(16).padStart(64, '0');
        return { ...base, id: base.server_id + ':' + containerId, container_id: containerId, state, name: '<app "' + index + '">' };
    });
    const view = containerView(); view.switch('pve-komodo-containers');
    view.respond(data); view.respond(sampleTreeLinks(), 200, 'links');
    const renderer = view.tree.columns[0].renderer;
    cases.forEach(([state, nativeStatus], index) => {
        const row = data.containers[index], record = view.node('container:' + row.id), meta = {};
        assert.equal(record.get('iconCls'), 'fa fa-cube ' + nativeStatus);
        assert.equal(record.get('state'), state, 'the raw state remains available for filtering');
        assert.equal(renderer(record.get('text'), meta, record), '&lt;app &quot;' + index + '&quot;&gt;');
        assert.match(meta.tdAttr, /^data-qtip="[^"]*"$/);
        if (state === 'running') assert.equal(meta.tdAttr, 'data-qtip="运行中"');
        if (state === 'paused') assert.equal(meta.tdAttr, 'data-qtip="已暂停"');
        if (index === cases.length - 1) {
            assert.doesNotMatch(meta.tdAttr, /<img|src=x onerror="/);
            assert.match(meta.tdAttr, /&amp;lt;img/,
                'the attribute and the tooltip HTML each need an escaping layer');
        }
    });
});

test('container tree refresh updates status icons while state search and selection remain local', () => {
    const data = sampleContainerTree(), intended = data.containers[0];
    const view = containerView(); view.switch('pve-komodo-containers');
    view.respond(data); view.respond(sampleTreeLinks(), 200, 'links');
    const id = 'container:' + intended.id;
    view.tree.getSelectionModel().select(view.node(id));
    assert.equal(view.node(id).get('iconCls'), 'fa fa-cube running');
    const updated = { ...data, containers: data.containers.map((row) => row.id === intended.id ? { ...row, state: 'restarting' } : row) };
    view.refresh(); view.respond(updated); view.respond(sampleTreeLinks(), 200, 'links');
    assert.equal(view.node(id).get('iconCls'), 'fa fa-cube pending');
    assert.equal(view.tree.getSelectionModel().getSelection()[0].getId(), id);
    const requests = view.browser.Ext.requests.length + view.browser.Ext.guestRequests.length;
    view.control('search').setValue('restarting');
    assert.equal(view.tree.getRootNode().childNodes.length, 1);
    assert.equal(view.node(id).get('state'), 'restarting');
    assert.equal(view.tree.columns[0].renderer(view.node(id).get('text'), {}, view.node(id)), intended.name,
        'state filtering still works after the visible state suffix is removed');
    assert.equal(view.node('container:' + data.containers[2].id), undefined);
    assert.equal(view.browser.Ext.requests.length + view.browser.Ext.guestRequests.length, requests);
    view.control('search').setValue(''); assert.ok(view.node('container:' + data.containers[2].id));
});

test('entering a restored custom view keeps full native server filtering without rebuilding content or leaking selection suspension', () => {
    const view = containerView(undefined, { id: 'pve-komodo-containers', groups: ['node'] });
    assert.equal(view.native.filterCalls.length, 0); assert.equal(view.browser.Ext.requests.length, 1);
    view.switch('folder'); assert.equal(view.native.hidden, false); assert.equal(view.tree.hidden, true);
    assert.equal(view.native.setViewFilter({ id: 'folder', groups: ['type'] }), 'native-filter-return');
    const rebuilds = view.workspace.contentRebuilds;
    view.switch('pve-komodo-containers');
    assert.equal(view.workspace.contentRebuilds, rebuilds, 'preparing the hidden native tree does not recreate the center card');
    assert.equal(view.native.selection.suspended, 0);
    assert.deepEqual(Array.from(view.native.viewFilter.groups), ['node']);
    const calls = view.native.filterCalls.length; view.switch('pve-komodo-containers');
    assert.equal(view.native.filterCalls.length, calls, 'a full hidden server tree is reused');
});

test('three-level tree refresh preserves expansion and highlight without navigating, and search and group clicks stay local', () => {
    const view = containerView(); view.switch('pve-komodo-containers');
    view.respond(sampleContainerTree()); view.respond(sampleTreeLinks(), 200, 'links');
    const stackId = 'stack:' + '1'.repeat(24) + ':' + '3'.repeat(24);
    const arrow = { getTarget(selector) { assert.equal(selector, '.x-tree-expander'); return {}; } };
    const beforeArrow = view.browser.Ext.guestRequests.length;
    view.tree.fire('itemclick', view.tree, view.node(stackId), null, 0, arrow);
    assert.equal(view.node(stackId).isExpanded(), true, 'native expander handling must not be toggled a second time by itemclick');
    view.tree.fire('itemclick', view.tree, view.node('server:' + '1'.repeat(24)), null, 0, arrow);
    assert.equal(view.browser.Ext.guestRequests.length, beforeArrow, 'a server expander never starts navigation');
    // Simulate the native expander after the guarded itemclick: stack text now
    // opens its management page instead of toggling this branch.
    view.node(stackId).collapse();
    const selected = view.node('container:' + sampleContainerTree().containers[0].id);
    view.tree.getSelectionModel().select(selected);
    view.refresh(); view.respond(sampleContainerTree()); view.respond(sampleTreeLinks(), 200, 'links');
    assert.equal(view.node(stackId).isExpanded(), false);
    assert.equal(view.tree.getSelectionModel().getSelection()[0].getId(), selected.getId());
    assert.equal(view.workspace.nativeSelections.length, 0);
    const before = view.browser.Ext.requests.length + view.browser.Ext.guestRequests.length;
    view.control('search').setValue('worker'); assert.equal(view.tree.getRootNode().childNodes.length, 1);
    assert.equal(view.node(stackId).childNodes.length, 1);
    view.control('search').setValue('');
    view.click('containers:' + '1'.repeat(24));
    assert.equal(view.browser.Ext.requests.length + view.browser.Ext.guestRequests.length, before);
});

test('view read failures distinguish unavailable associations from unlinked servers and never assert an empty or online inventory', () => {
    const view = containerView(); view.switch('pve-komodo-containers');
    view.respond(sampleContainerTree()); view.respond({ detail: 'PRIVATE' }, 502, 'links');
    assert.equal(view.node('server:' + '1'.repeat(24)).get('binding'), '关联状态未知');
    assert.match(view.control('status').text, /关联状态未知/); assert.doesNotMatch(view.control('status').text, /PRIVATE/);
    view.refresh(); view.respond({ ...sampleContainerTree(), containers: [], servers: [], stacks: [], errors: ['containers_upstream_unavailable'] });
    view.respond(sampleTreeLinks(), 200, 'links');
    assert.equal(view.tree.getRootNode().childNodes.length, 0); assert.match(view.control('status').text, /部分容器数据不可用/);
    view.refresh(); view.respond({ detail: 'PRIVATE' }, 502); view.respond(sampleTreeLinks(), 200, 'links');
    assert.match(view.control('status').text, /容器数据读取失败/); assert.equal(view.tree.getRootNode().childNodes.length, 0);
    assert.equal(view.tree.statusToolbar.visible, true, 'failures remain visible');
});

test('leaving, logout, auth rejection and user changes cancel only tree GETs and reject every late callback', () => {
    const view = containerView(); view.switch('pve-komodo-containers');
    const first = view.browser.Ext.requests[0]; const links = view.browser.Ext.guestRequests[0];
    view.switch('server'); assert.equal(first.aborted, true); assert.equal(links.aborted, true);
    view.respond(sampleContainerTree(), 200, 'containers', 0); view.respond(sampleTreeLinks(), 200, 'links', 0);
    assert.equal(view.tree.getRootNode().childNodes.length, 0);
    view.switch('pve-komodo-containers'); view.respond(sampleContainerTree()); view.respond({ detail: 'PRIVATE' }, 403, 'links');
    assert.equal(view.tree.getRootNode().childNodes.length, 0); assert.equal(view.control('status').text, '无访问权限');
    view.refresh(); view.respond(sampleContainerTree()); view.respond(sampleTreeLinks(), 200, 'links');
    view.refresh(); const pending = view.browser.Ext.requests.at(-1), pendingLinks = view.browser.Ext.guestRequests.at(-1);
    assert.equal(view.workspace.onLogin(null), 'login-return'); assert.equal(view.tree.getRootNode().childNodes.length, 0);
    assert.equal(pending.aborted, true); assert.equal(pendingLinks.aborted, true);
    view.respond(sampleContainerTree()); view.respond(sampleTreeLinks(), 200, 'links');
    assert.equal(view.tree.getRootNode().childNodes.length, 0, 'logout rejects both late inventory responses');
    const count = view.browser.Ext.requests.length; view.refresh(); assert.equal(view.browser.Ext.requests.length, count);
    view.browser.context.window.Proxmox.UserName = 'other@pam'; view.workspace.onLogin({ username: 'other@pam' });
    view.respond(sampleContainerTree()); view.respond(sampleTreeLinks(), 200, 'links');
    view.browser.context.window.Proxmox.UserName = 'third@pam'; view.click('server:' + '1'.repeat(24));
    assert.equal(view.tree.getRootNode().childNodes.length, 0); assert.match(view.control('status').text, /会话已变化/);
    view.native.destroy(); assert.equal(view.tree.destroyed, true);
});

test('server clicks revalidate mapping, use native VM identities and reject a summary that changes server before completion', () => {
    const view = containerView(); view.switch('pve-komodo-containers');
    view.respond(sampleContainerTree()); view.respond(sampleTreeLinks(), 200, 'links');
    let summary;
    view.workspace.configPanels = [{ pveSelNode: { data: { id: 'qemu/102', type: 'qemu', vmid: 102 } },
        selectById(id) { assert.equal(id, 'pve-komodo-guest-docker'); summary = guestSummaryPage(view.browser); summary.activate(); } }];
    view.click('server:' + '1'.repeat(24)); assert.equal(view.workspace.nativeSelections.length, 0);
    view.respond(sampleTreeLinks(), 200, 'links');
    assert.deepEqual(view.workspace.nativeSelections, ['qemu/102']);
    summary.respond({ ...sampleGuestSummary(), server_id: '2'.repeat(24) });
    assert.match(summary.control('summary-status').text, /关联已变化/);
    assert.equal(summary.grid('containers').getStore().data.length, 0);
    assert.equal(summary.control('summary-stop_all_containers').disabled, true);
});

test('returning from a dedicated page reselects the guest even when the hidden native tree already selects it', () => {
    const view = containerView(); view.switch('pve-komodo-containers'); view.respond(sampleContainerTree()); view.respond(sampleTreeLinks(), 200, 'links');
    let selected = [{ get: (key) => key === 'id' ? 'qemu/102' : undefined }], cleared = 0;
    view.native.selection.getSelection = () => selected;
    view.native.selection.deselectAll = () => { selected = []; cleared++; };
    view.workspace.onNativeSelect = () => {
        assert.equal(selected.length, 0, 'a same-guest native selection must not suppress content reconstruction');
        view.workspace.configPanels = [{ pveSelNode: { data: { id: 'qemu/102' } }, selectById() {} }];
    };
    view.click('server:' + '1'.repeat(24)); view.respond(sampleTreeLinks(), 200, 'links');
    assert.equal(cleared, 1); assert.equal(view.workspace.nativeSelections.at(-1), 'qemu/102');
});

test('a container leaf aborts stale server navigation and opens an independent page for its exact server and full ID', () => {
    const view = containerView(); view.switch('pve-komodo-containers');
    view.respond(sampleContainerTree()); view.respond(sampleTreeLinks(), 200, 'links');
    view.click('server:' + '1'.repeat(24)); const stale = view.browser.Ext.guestRequests.at(-1);
    const intended = sampleContainerTree().containers[1];
    view.click('container:' + intended.id); assert.equal(stale.aborted, true);
    stale.callback(stale, true, { status: 200, responseText: JSON.stringify(sampleTreeLinks()) });
    assert.deepEqual(view.workspace.nativeSelections, []);
    const target = view.workspace.content;
    assert.equal(target.pveKomodoContainerPage, true);
    assert.equal(target.pveKomodoContainerServer, intended.server_id);
    assert.equal(target.pveKomodoContainerId, intended.id.split(':')[1]);
    assert.equal(target.pveKomodoStackPath, '/pve-komodo/api/servers/' + intended.server_id + '/containers/' + intended.id.split(':')[1]);
    assert.equal(target.hstateid, undefined); assert.match(target.pveSelNode.data.id, /^pve-komodo-container:/);
    assert.deepEqual(Array.from(target.items, (tab) => tab.title), ['概览', '日志', '检查', '终端']);
    assert.deepEqual(view.native.nativeResourceIds, ['root', 'qemu/102', 'lxc/252']);
});

test('unlinked servers open only Docker management while pending existing writes still prevent tree navigation', () => {
    const view = containerView(); view.switch('pve-komodo-containers');
    view.respond(sampleContainerTree()); view.respond(sampleTreeLinks(), 200, 'links');
    view.click('server:' + '2'.repeat(24)); view.respond(sampleTreeLinks(), 200, 'links');
    const config = view.workspace.content;
    assert.equal(config.pveKomodoServerPage, true);
    assert.equal(config.pveSelNode.data.id, 'pve-komodo-server:' + '2'.repeat(24));
    assert.deepEqual(Array.from(config.items, (item) => item.title), ['Docker', '选项', 'DOCKER', '资源', '终端']);
    assert.equal(config.items[0].pveKomodoGuestBase, '/pve-komodo/api/servers/' + '2'.repeat(24));
    assert.equal(config.items[1].pveKomodoOptionsPath, '/pve-komodo/api/servers/' + '2'.repeat(24) + '/options');
    assert.ok(config.items.every((item) => item.pveKomodoDirectServer === '2'.repeat(24)));
    assert.equal(config.items[4].pveKomodoStackPath, '/pve-komodo/api/servers/' + '2'.repeat(24));
    assert.equal(view.workspace.nativeSelections.length, 0, 'independent servers never invent a native VM id');
    const home = dashboard(view.browser); home.activate(); home.respond(sampleDashboard());
    home.select('procedures', ['p1']); home.click('procedures', 'run'); home.answer('yes');
    const before = view.browser.Ext.guestRequests.length;
    view.click('server:' + '1'.repeat(24));
    assert.equal(view.browser.Ext.guestRequests.length, before);
    assert.match(view.control('status').text, /操作正在提交/);
    home.respond({ results: [{ id: 'p1', ok: true, status: 'submitted' }] });
});

function stackPage(browser = containerViewBrowser()) {
    const view = containerView(browser); view.switch('pve-komodo-containers');
    view.respond(sampleContainerTree()); view.respond(sampleTreeLinks(), 200, 'links');
    view.click('stack:' + '1'.repeat(24) + ':' + '3'.repeat(24));
    const config = view.workspace.content;
    assert.ok(config?.pveKomodoStackPage, 'stack text opens a private native ConfigPanel');
    const page = dashboard(browser, 'stack', { ...config, items: [] });
    let current;
    return { ...page, view, original: config,
        tab(kind) {
            if (current) current.destroy();
            const definition = config.items.find((item) => item.pveKomodoStackTab === kind);
            current = dashboard(browser, 'stack-tab', definition, page.panel);
            current.activate(); return current;
        },
        press(name) { const button = page.control('stack-' + name); if (!button.disabled) button.handler(button); },
        destroy() { page.destroy(); if (current && !current.panel.destroyed) current.destroy(); },
    };
}
function sampleStackDetails() {
    const config = {};
    for (const field of ['auto_pull', 'run_build', 'poll_for_updates', 'auto_update', 'auto_update_all_services',
        'destroy_before_deploy', 'skip_secret_interp', 'git_https', 'reclone', 'webhook_enabled',
        'webhook_force_deploy', 'files_on_host', 'send_alerts']) config[field] = false;
    for (const field of ['swarm_id', 'server_id', 'project_name', 'linked_repo', 'git_provider', 'git_account', 'repo', 'branch',
        'commit', 'clone_path', 'webhook_secret', 'run_directory', 'env_file_path', 'registry_provider',
        'registry_account', 'compose_cmd_wrapper', 'file_contents', 'environment']) config[field] = '';
    for (const field of ['links', 'auto_update_skip_services', 'file_paths', 'extra_args', 'build_extra_args',
        'compose_cmd_wrapper_include', 'ignore_services', 'additional_env_files', 'config_files']) config[field] = [];
    config.server_id = '1'.repeat(24); config.project_name = '<project>'; config.environment = 'VALUE=<unsafe>'; config.files_on_host = true;
    config.pre_deploy = { path: '/srv/compose', command: 'echo before', shell_mode: true };
    config.post_deploy = { path: '', command: '', shell_mode: false };
    config.additional_env_files = [{ path: 'extra.env', track: false }];
    config.config_files = [{ path: 'app.conf', services: ['web'], requires: 'None' }];
    return { stack_id: '3'.repeat(24), stack_name: '<stack-a>', server_id: '1'.repeat(24), swarm_id: '', state: 'running',
        description: '<description>', config, version: 'a'.repeat(64), source_mode: 'host', can_write: true, can_execute: true, can_logs: true,
        services: ['web', 'worker'], service_choices: ['web', 'worker', 'init'], files: [{ path: 'compose.yaml', contents: 'services:\n  web:\n    image: nginx:<tag>\n', editable: true, version: 'b'.repeat(64) },
            { path: 'secret.env', contents: '[REDACTED_SECRET]', editable: false, redacted: true, version: 'c'.repeat(64) }],
        deployed_config: 'services:\n  web: {}\n', deployed_files: [], remote_errors: [], selectors: {
            servers: [{ id: '1'.repeat(24), name: '<server>' }], swarms: [], repos: [],
        }, redacted_fields: [], save_effects: ['config_source_changes_may_auto_deploy', 'source_file_changes_may_auto_deploy'],
        errors: [], updated_at: '2026-10-17T14:00:00Z', csrf_token: 'stack-csrf' };
}
function selectStackOption(tab, key) {
    const grid = tab.grid('stack-config');
    grid.getSelectionModel().select(grid.getStore().data.find((row) => row.key === key));
    const button = tab.control('stack-edit'); button.handler(button);
    return tab.browser.Ext.dialogs.at(-1);
}

test('stack tree text opens four native tabs with help context and never writes native resource state', () => {
    const page = stackPage();
    assert.equal(page.original.xtype, 'pvePanelConfig'); assert.equal(page.original.hstateid, undefined);
    assert.match(page.original.pveSelNode.data.id, /^pve-komodo-stack:/);
    assert.deepEqual(page.view.workspace.nativeSelections, []);
    assert.deepEqual(Array.from(page.original.items, (item) => item.title), ['配置', '信息', '日志', '终端']);
    assert.ok(page.original.items.every((item) => item.onlineHelp === 'chapter_gui'));
    assert.equal(page.original.onlineHelp, 'chapter_gui');
    assert.deepEqual(Array.from(page.original.tbar.slice(0, 6), (item) => item.text), ['部署', '拉取镜像', '重启', '暂停', '停止', '销毁']);
    assert.ok(page.original.tbar.slice(0, 6).every((item) => !item.menu));
    assert.equal(page.browser.Ext.requests.length, 1, 'opening a card config alone does not fetch detail or write');
    const tab = page.tab('config'), request = page.browser.Ext.requests.at(-1);
    assert.equal(request.url, '/pve-komodo/api/stacks/' + '3'.repeat(24)); assert.equal(request.method, 'GET'); assert.equal(request.disableCaching, false);
    page.respond(sampleStackDetails());
    const grid = tab.grid('stack-config'), rows = grid.getStore().data;
    assert.deepEqual(Array.from(rows.filter((row) => row.kind === 'heading'), (row) => [row.label, row.heading]), [
        ['常规', 'major'], ['服务器', 'minor'], ['文件', 'minor'], ['环境变量', 'minor'], ['配置文件', 'minor'],
        ['自动更新', 'minor'], ['链接', 'minor'], ['高级', 'major'], ['项目名称', 'minor'], ['部署前操作', 'minor'],
        ['部署后操作', 'minor'], ['命令包装器', 'minor'], ['额外参数', 'minor'], ['忽略服务', 'minor'],
        ['拉取镜像', 'minor'], ['构建镜像', 'minor'], ['销毁', 'minor'],
    ]);
    assert.deepEqual(Array.from(rows.filter((row) => row.kind !== 'heading'), (row) => row.key), [
        'server_id', 'run_directory', 'file_paths', 'environment', 'config_files', 'poll_for_updates', 'auto_update_skip_services',
        'auto_update', 'auto_update_all_services', 'links', 'project_name', 'pre_deploy.path', 'pre_deploy.shell_mode', 'pre_deploy.command',
        'post_deploy.path', 'post_deploy.shell_mode', 'post_deploy.command', 'compose_cmd_wrapper',
        'compose_cmd_wrapper_include', 'extra_args', 'ignore_services', 'registry_provider', 'auto_pull', 'run_build', 'destroy_before_deploy',
    ]);
    const dialogCount = page.browser.Ext.dialogs.length;
    for (const row of rows.filter((row) => row.kind === 'heading')) {
        const record = { get: (key) => row[key] };
        assert.equal(row.available, false);
        let cleared = false;
        assert.equal(grid.selModel.listeners.beforeselect({ deselectAll() { cleared = true; } }, record), false);
        assert.equal(cleared, true);
        assert.equal(grid.viewConfig.getRowClass(record), 'pve-komodo-stack-heading-' + row.heading);
        const description = grid.columns[1].renderer(undefined, {}, record);
        if (row.heading === 'minor') {
            assert.ok(row.description);
            assert.match(description, /class="pve-komodo-stack-heading-description">[^（].+[^）]<\/span>/);
        } else assert.equal(description, '');
        grid.listeners.itemdblclick.call(grid, null, record);
    }
    assert.equal(page.browser.Ext.dialogs.length, dialogCount, 'read-only headings never open an editor');
    assert.equal(grid.viewConfig.variableRowHeight, true, 'long header descriptions can wrap');
    assert.equal(page.control('stack-deploy').disabled, false);
    assert.match(page.control('stack-status').text, /&lt;stack-a&gt;/);
    assert.equal(page.view.native.nativeResourceIds.length, 3);
});

test('stack config editors preserve original values, reset and confirm only a changed partial field with version context', () => {
    const page = stackPage(), tab = page.tab('config'); page.respond(sampleStackDetails());
    const editor = selectStackOption(tab, 'environment');
    assert.equal(editor.down('#pve-komodo-stack-input-value').getValue(), 'VALUE=<unsafe>');
    editor.down('#pve-komodo-stack-input-value').setValue('VALUE=changed');
    editor.down('#pve-komodo-option-reset').handler();
    assert.equal(editor.down('#pve-komodo-stack-input-value').getValue(), 'VALUE=<unsafe>');
    editor.down('#pve-komodo-stack-input-value').setValue('VALUE=changed');
    editor.down('#pve-komodo-option-save').handler();
    const confirm = page.browser.Ext.dialogs.at(-1);
    assert.match(confirm.options.message, /可能按自动更新策略触发部署/);
    assert.doesNotMatch(confirm.options.message, /config_source_changes_may_auto_deploy|<stack-a>/);
    page.answer('no'); assert.equal(page.browser.Ext.requests.filter((request) => request.method === 'POST').length, 0);
    editor.down('#pve-komodo-option-save').handler(); page.answer('yes');
    const post = page.browser.Ext.requests.at(-1);
    assert.equal(post.url, page.original.pveKomodoStackPath); assert.equal(post.method, 'POST');
    assert.equal(post.headers['X-PVE-Komodo-CSRF'], 'stack-csrf');
    assert.deepEqual(JSON.parse(JSON.stringify(post.jsonData)), { action: 'config', server_id: '1'.repeat(24), base_version: 'a'.repeat(64),
        config: { environment: 'VALUE=changed' }, request_id: 'ceaa63ca-3693-4ef0-8cbf-4de83911a76b' });
    page.respond({ ok: true, status: 'completed' });
    assert.equal(page.browser.Ext.requests.at(-1).method, 'GET', 'authoritative detail is reloaded after a completed save');
    assert.equal(tab.grid('stack-config').getStore().data.length, 0, 'stale option values are cleared before refresh');
    page.respond({ ...sampleStackDetails(), config: { ...sampleStackDetails().config, environment: 'VALUE=changed' } });
    assert.equal(tab.grid('stack-config').getStore().data.find((row) => row.key === 'environment').value, 'VALUE=changed');
});

test('deployment command rows edit one member while preserving the other command settings', () => {
    for (const parent of ['pre_deploy', 'post_deploy']) for (const [member, label, value] of [
        ['path', '路径', '/changed'], ['shell_mode', '以 Shell 模式运行（sh -c）', false], ['command', '命令', 'printf changed'],
    ]) {
        const page = stackPage(), tab = page.tab('config'), data = sampleStackDetails();
        data.config[parent] = { path: '/srv/' + parent, shell_mode: true, command: 'echo original' };
        page.respond(data);
        const editor = selectStackOption(tab, parent + '.' + member), input = editor.down('#pve-komodo-stack-input-value');
        assert.equal(editor.title, (parent === 'pre_deploy' ? '部署前操作' : '部署后操作') + '：' + label);
        assert.equal(input.getValue(), data.config[parent][member]);
        input.setValue(value); editor.down('#pve-komodo-option-reset').handler();
        assert.equal(input.getValue(), data.config[parent][member]);
        input.setValue(value); editor.down('#pve-komodo-option-save').handler(); page.answer('yes');
        const post = page.browser.Ext.requests.at(-1);
        assert.deepEqual(JSON.parse(JSON.stringify(post.jsonData.config)), { [parent]: { ...data.config[parent], [member]: value } });
        assert.equal(post.jsonData.base_version, data.version);
        assert.deepEqual(data.config[parent], { path: '/srv/' + parent, shell_mode: true, command: 'echo original' });
    }
});

test('command wrapper scope offers native Compose and Swarm choices and saves only the chosen array', () => {
    for (const swarm of [false, true]) {
        const page = stackPage(), tab = page.tab('config'), data = sampleStackDetails();
        data.config.swarm_id = swarm ? '4'.repeat(24) : '';
        page.respond(data);
        const editor = selectStackOption(tab, 'compose_cmd_wrapper_include'), input = editor.down('#pve-komodo-stack-input-value');
        const choices = swarm ? ['config', 'deploy'] : ['config', 'build', 'pull', 'up', 'run'];
        assert.equal(input.xtype, 'combobox'); assert.equal(input.multiSelect, true); assert.equal(input.forceSelection, true);
        assert.deepEqual(Array.from(input.getStore().data, (row) => row.name), choices);
        assert.deepEqual(Array.from(input.getValue()), [], 'empty configuration is not changed to an implicit default');
        input.setValue(choices); input.triggers.clear.handler.call(input);
        assert.deepEqual(Array.from(input.getValue()), []);
        const selected = swarm ? ['deploy'] : ['config', 'up'];
        input.setValue(selected); editor.down('#pve-komodo-option-save').handler(); page.answer('yes');
        assert.deepEqual(JSON.parse(JSON.stringify(page.browser.Ext.requests.at(-1).jsonData.config)), { compose_cmd_wrapper_include: selected });
    }
    const page = stackPage(), tab = page.tab('config'), data = sampleStackDetails();
    data.config.compose_cmd_wrapper_include = ['legacy', 'up']; page.respond(data);
    const editor = selectStackOption(tab, 'compose_cmd_wrapper_include'), input = editor.down('#pve-komodo-stack-input-value');
    assert.deepEqual(Array.from(input.getValue()), ['up']);
    assert.deepEqual(Array.from(input.getStore().data, (row) => row.name), ['config', 'build', 'pull', 'up', 'run']);
    editor.down('#pve-komodo-option-save').handler();
    assert.equal(page.browser.Ext.requests.filter((request) => request.method === 'POST').length, 0, 'opening an old configuration does not rewrite it');
    assert.deepEqual(data.config.compose_cmd_wrapper_include, ['legacy', 'up']);
});

test('stack update exclusions offer configured services, preserve saved choices, and submit a clearable partial array', () => {
    const page = stackPage(), tab = page.tab('config'), data = sampleStackDetails();
    data.config.auto_update_skip_services = ['removed', 'web'];
    data.service_choices.push('web', '<encoded-service>');
    page.respond(data);
    const editor = selectStackOption(tab, 'auto_update_skip_services'), input = editor.down('#pve-komodo-stack-input-value');
    assert.equal(input.xtype, 'combobox'); assert.equal(input.multiSelect, true); assert.equal(input.forceSelection, true);
    assert.equal(input.queryMode, 'local'); assert.equal(input.editable, true);
    assert.deepEqual(Array.from(input.getStore().data, (row) => row.name), ['web', 'worker', 'init', '<encoded-service>', 'removed']);
    assert.equal(input.listConfig.itemTpl, '{name:htmlEncode}');
    assert.deepEqual(Array.from(input.getValue()), ['removed', 'web']);
    input.setValue(['worker', 'init']); editor.down('#pve-komodo-option-reset').handler();
    assert.deepEqual(Array.from(input.getValue()), ['removed', 'web']);
    input.setValue(['not-a-choice']); editor.down('#pve-komodo-option-save').handler();
    assert.match(editor.down('#pve-komodo-option-error').html, /输入值无效/);
    assert.equal(page.browser.Ext.requests.filter((request) => request.method === 'POST').length, 0);
    input.triggers.clear.handler.call(input); assert.deepEqual(Array.from(input.getValue()), []);
    editor.down('#pve-komodo-option-save').handler(); page.answer('yes');
    assert.deepEqual(JSON.parse(JSON.stringify(page.browser.Ext.requests.at(-1).jsonData.config)), { auto_update_skip_services: [] });
});

test('stack redacted configuration and files cannot be edited while plain Compose text uses native read-only fields and Git save warnings', () => {
    const page = stackPage(), config = page.tab('config'), data = sampleStackDetails();
    data.config.environment = '[REDACTED_SECRET]'; data.redacted_fields = ['environment', 'pre_deploy'];
    page.respond(data); const before = page.browser.Ext.dialogs.length;
    selectStackOption(config, 'environment'); assert.equal(page.browser.Ext.dialogs.length, before);
    for (const member of ['path', 'shell_mode', 'command']) selectStackOption(config, 'pre_deploy.' + member);
    assert.equal(page.browser.Ext.dialogs.length, before, 'all members of a redacted command remain read-only');
    const tab = page.tab('info');
    assert.equal(tab.control('stack-compose').readOnly, true); assert.equal(tab.control('stack-compose').getValue(), data.files[0].contents);
    assert.equal(tab.control('stack-deployed').readOnly, true); assert.equal(tab.control('stack-deployed').getValue(), data.deployed_config);
    tab.control('stack-file').setValue('secret.env'); assert.equal(tab.control('stack-edit-file').disabled, true);
    const blocked = tab.control('stack-edit-file'); blocked.handler(blocked); assert.equal(page.browser.Ext.dialogs.length, before);
    tab.control('stack-file').setValue('compose.yaml'); assert.equal(tab.control('stack-edit-file').disabled, false);
    page.panel._pveKomodoStackData.source_mode = 'git'; page.panel._pveKomodoStackData.save_effects.push('git_file_save_commits_and_pushes');
    blocked.handler(blocked); const editor = page.browser.Ext.dialogs.at(-1);
    editor.down('#pve-komodo-stack-file-editor').setValue('services: {}'); editor.down('#pve-komodo-option-save').handler();
    assert.match(page.browser.Ext.dialogs.at(-1).options.message, /Git 文件保存.*提交.*推送/);
    page.answer('no'); assert.equal(page.browser.Ext.requests.filter((request) => request.method === 'POST').length, 0);
});

test('stack structured file editors keep source order and full nested values through add, reset and cancel', () => {
    const page = stackPage(), tab = page.tab('config'); page.respond(sampleStackDetails());
    const editor = selectStackOption(tab, 'config_files'), grid = editor.down('#pve-komodo-stack-list');
    assert.ok(grid.columns.every((column) => column.sortable === false));
    grid.getSelectionModel().select(grid.getStore().data[0]); grid.tbar.find((button) => button.text === '编辑').handler(grid.tbar[1]);
    const child = page.browser.Ext.dialogs.at(-1);
    assert.equal(child.down('#pve-komodo-stack-list-requires').getValue(), 'None');
    assert.equal(child.down('#pve-komodo-stack-list-services').getValue(), 'web');
    child.down('#pve-komodo-stack-list-services').setValue('web\nworker'); child.down('#pve-komodo-stack-list-apply').handler();
    assert.deepEqual(Array.from(grid.getStore().data[0].services), ['web', 'worker']);
    editor.down('#pve-komodo-option-reset').handler(); assert.deepEqual(Array.from(grid.getStore().data[0].services), ['web']);
    editor.down('#pve-komodo-option-cancel').handler(); assert.equal(editor.destroyed, true);
    assert.equal(page.browser.Ext.requests.filter((request) => request.method === 'POST').length, 0);
});

test('stack detail actions require an explicit snapshot confirmation, use the contextual endpoint and survive card destruction without retries', () => {
    const browser = containerViewBrowser(), page = stackPage(browser); page.tab('config'); page.respond(sampleStackDetails());
    page.press('destroy'); const dialog = browser.Ext.dialogs.at(-1);
    assert.match(dialog.options.message, /停止并移除.*默认不删除存储卷/); assert.doesNotMatch(dialog.options.message, /<stack-a>/);
    page.answer('no'); assert.equal(browser.Ext.requests.filter((request) => request.method === 'POST').length, 0);
    page.press('pause'); page.answer('yes'); const post = browser.Ext.requests.at(-1);
    assert.equal(post.url, page.original.pveKomodoStackPath + '/actions');
    assert.deepEqual(JSON.parse(JSON.stringify(post.jsonData)), { action: 'pause', server_id: '1'.repeat(24), base_version: 'a'.repeat(64), request_id: 'ceaa63ca-3693-4ef0-8cbf-4de83911a76b' });
    page.destroy(); assert.equal(post.aborted, undefined);
    const options = guestOptionsPage(browser); options.activate(); options.respond(sampleGuestOptions()); options.selectOption('region');
    assert.equal(options.control('edit').disabled, true);
    post.callback(post, true, { status: 200, responseText: JSON.stringify({ section: 'stacks', action: 'pause', results: [{ id: '3'.repeat(24), ok: false, status: 'outcome_unknown' }] }) });
    assert.equal(options.control('edit').disabled, false); assert.equal(post.aborted, undefined);
    assert.equal(browser.Ext.requests.filter((request) => request.method === 'POST').length, 1);
});

test('stack refresh and logout invalidate pending reads, reject moved targets and never show stale file content', () => {
    const page = stackPage(); page.tab('config'); const old = page.browser.Ext.requests.at(-1);
    page.view.workspace.onLogin(null); assert.equal(old.aborted, true);
    old.callback(old, true, { status: 200, responseText: JSON.stringify(sampleStackDetails()) });
    assert.equal(page.panel._pveKomodoStackData, null);
    const moved = stackPage(); const tab = moved.tab('info'); moved.respond({ ...sampleStackDetails(), server_id: '2'.repeat(24) });
    assert.match(moved.control('stack-status').text, /所在服务器已变化/); assert.equal(tab.control('stack-compose').getValue(), '');
    moved.press('refresh'); moved.respond(sampleStackDetails()); assert.ok(tab.control('stack-compose').getValue());
    moved.press('refresh'); moved.respond({ detail: 'PRIVATE' }, 502);
    assert.equal(tab.control('stack-compose').getValue(), ''); assert.equal(moved.control('stack-deploy').disabled, true);
    assert.doesNotMatch(moved.control('stack-status').text, /PRIVATE/);
});

test('stack log queries cancel superseded reads, encode output, search only the recent 5000 lines and clean polling on leave', () => {
    const page = stackPage(), timers = new Map(); let timerId = 0;
    page.browser.context.window.setTimeout = (fn) => { const id = ++timerId; timers.set(id, fn); return id; };
    page.browser.context.window.clearTimeout = (id) => timers.delete(id);
    page.tab('config'); page.respond(sampleStackDetails()); const tab = page.tab('logs'), first = page.browser.Ext.requests.at(-1);
    assert.equal(first.disableCaching, false); assert.equal(first.url, page.original.pveKomodoStackPath + '/logs');
    assert.equal(first.params.tail, 100);
    tab.control('stack-log-services').setValue(['web', 'worker']); const second = page.browser.Ext.requests.at(-1);
    assert.equal(first.aborted, true); assert.deepEqual(Array.from(second.params.services), ['web', 'worker']);
    first.callback(first, true, { status: 200, responseText: JSON.stringify({ stdout: 'STALE', stderr: '' }) });
    assert.doesNotMatch(tab.control('stack-log-output').html, /STALE/);
    tab.respond({ stdout: '<img src=x onerror=bad>\nhello', stderr: 'error', success: true });
    assert.match(tab.control('stack-log-output').html, /&lt;img/); assert.doesNotMatch(tab.control('stack-log-output').html, /<img/);
    tab.control('stack-log-stream').setValue('stderr'); assert.match(tab.control('stack-log-output').html, /error/);
    tab.control('stack-log-search').setValue('needle'); tab.control('stack-log-invert').setValue(true);
    const search = page.browser.Ext.requests.at(-1); assert.equal(search.params.tail, 5000); assert.equal(search.params.search, 'needle'); assert.equal(search.params.invert, true);
    assert.equal(tab.control('stack-log-tail').disabled, true);
    tab.respond({ stdout: '', stderr: 'needle', success: true, search_scope: 'last_5000' });
    assert.match(page.control('stack-status').text, /最近 5000 行/); assert.doesNotMatch(page.control('stack-status').text, /全部可用/);
    tab.control('stack-log-poll').setValue(true); tab.respond({ stdout: '', stderr: '', success: true }); assert.equal(timers.size, 1);
    tab.destroy(); assert.equal(timers.size, 0);
});

test('stack terminal services gate creation and only an explicit new terminal sends a CSRF-protected full-ID grant request', () => {
    const page = stackPage(); page.tab('config'); page.respond(sampleStackDetails()); const tab = page.tab('terminal');
    const request = page.browser.Ext.requests.at(-1); assert.equal(request.method, 'GET'); assert.equal(request.disableCaching, false); assert.match(request.url, /terminal-services$/);
    assert.equal(tab.control('stack-new-terminal').disabled, true);
    tab.respond({ stack_id: '3'.repeat(24), server_id: '1'.repeat(24), services: [{ service: 'web', server_id: '1'.repeat(24), container_id: 'b'.repeat(64), name: '<web>', state: 'running', can_open: true }], shells: ['sh', 'bash'], csrf_token: 'terminal-csrf' });
    assert.equal(tab.control('stack-new-terminal').disabled, false);
    const button = tab.control('stack-new-terminal'); button.handler(button); const dialog = page.browser.Ext.dialogs.at(-1);
    assert.equal(page.browser.Ext.requests.filter((entry) => entry.method === 'POST').length, 0);
    dialog.down('#pve-komodo-terminal-service').setValue('web'); dialog.down('#pve-komodo-terminal-shell').setValue('bash');
    dialog.down('#pve-komodo-terminal-create').handler(); const post = page.browser.Ext.requests.at(-1);
    assert.match(post.url, /\/terminals$/); assert.equal(post.headers['X-PVE-Komodo-CSRF'], 'terminal-csrf');
    assert.deepEqual(JSON.parse(JSON.stringify(post.jsonData)), { service: 'web', server_id: '1'.repeat(24), container_id: 'b'.repeat(64), shell: 'bash', request_id: 'ceaa63ca-3693-4ef0-8cbf-4de83911a76b' });
    tab.destroy(); assert.equal(post.aborted, undefined);
    post.callback(post, true, { status: 200, responseText: JSON.stringify({ grant: 'unused-grant', session_id: 'ceaa63ca-3693-4ef0-8cbf-4de83911a76b', expires_in: 60 }) });
    assert.equal(page.browser.Ext.requests.filter((entry) => entry.method === 'POST').length, 1, 'leaving never retries a grant or opens a late terminal');
});

test('stack reference names are exact and encoded, and missing source files require an explicit initialization warning', () => {
    const page = stackPage(), config = page.tab('config'), data = sampleStackDetails();
    data.files[0].contents = null; data.remote_errors = [{ path: 'compose.yaml', error_code: 'file_unavailable' }]; page.respond(data);
    const row = config.grid('stack-config').getStore().data.find((entry) => entry.key === 'server_id');
    const renderer = config.grid('stack-config').columns.find((column) => column.dataIndex === 'value').renderer;
    const meta = {}; assert.equal(renderer(row.value, meta, { get: (key) => row[key] }), '&lt;server&gt;');
    assert.equal(meta.tdAttr, 'data-qtip="' + '1'.repeat(24) + '"');
    const tab = page.tab('info'); assert.equal(tab.control('stack-compose').getValue(), '');
    assert.match(page.control('stack-status').text, /尚未取得内容/);
    const button = tab.control('stack-edit-file'); button.handler(button); const editor = page.browser.Ext.dialogs.at(-1);
    assert.ok(editor.items.some((item) => item.html?.includes('尚未取得内容')));
    editor.down('#pve-komodo-stack-file-editor').setValue('services: {}'); editor.down('#pve-komodo-option-reset').handler();
    assert.equal(editor.down('#pve-komodo-stack-file-editor').getValue(), '');
    editor.down('#pve-komodo-stack-file-editor').setValue('services: {}'); editor.down('#pve-komodo-option-save').handler();
    assert.match(page.browser.Ext.dialogs.at(-1).options.message, /原文件未能读取.*初始化或覆盖/);
    page.answer('no'); assert.equal(page.browser.Ext.requests.filter((request) => request.method === 'POST').length, 0);
});

test('stack xterm loads same-origin assets and preserves binary output, authenticated input and owned-session cleanup without reconnect', async () => {
    const page = stackPage(), window = page.browser.context.window, terminals = [], sockets = [], assets = [], observers = [];
    const listeners = new Map(), documentListeners = new Map(), terminalHost = { clientWidth: 1000, clientHeight: 600 };
    class Terminal {
        constructor(options) { Object.assign(this, options); this.writes = []; terminals.push(this); }
        open(host) { this.host = host; this.element = { querySelector: (selector) => selector === '.xterm-screen' ?
            { getBoundingClientRect: () => ({ width: this.cols * 8, height: this.rows * 16 }) } : { offsetWidth: 100, clientWidth: 88 } }; }
        write(value) { this.writes.push(Array.from(value)); } focus() { this.focused = true; }
        onData(fn) { this.data = fn; } onResize(fn) { this.resized = fn; }
        resize(cols, rows) { this.cols = cols; this.rows = rows; this.resized({ cols, rows }); }
        dispose() { this.disposed = true; }
    }
    class WebSocket {
        constructor(url) { this.url = url; this.sent = []; this.readyState = 1; sockets.push(this); }
        send(value) { this.sent.push(JSON.parse(value)); }
        close(code) { this.closed = code ?? true; this.readyState = 3; this.onclose?.(); }
    }
    page.browser.context.URL = URL;
    window.location = { href: 'https://pve.example.test/' }; window.WebSocket = WebSocket;
    window.ResizeObserver = class { constructor(fn) { this.callback = fn; observers.push(this); } observe(value) { this.element = value; } disconnect() { this.disconnected = true; } };
    window.getComputedStyle = () => ({ paddingLeft: '8px', paddingRight: '8px', paddingTop: '8px', paddingBottom: '8px' });
    window.addEventListener = (name, fn) => listeners.set(name, fn); window.removeEventListener = (name) => listeners.delete(name);
    window.innerWidth = 1600; window.innerHeight = 1000;
    window.document = { getElementById: () => null, createElement: () => ({}), head: { appendChild(node) { assets.push(node.href); } },
        addEventListener: (name, fn) => documentListeners.set(name, fn), removeEventListener: (name) => documentListeners.delete(name),
        exitFullscreen() { this.fullscreenElement = null; documentListeners.get('fullscreenchange')?.(); return Promise.resolve(); } };
    page.browser.Ext.Loader = { loadScript(options) { assets.push(options.url); window.Terminal = Terminal; options.onLoad(); } };
    page.tab('config'); page.respond(sampleStackDetails()); const tab = page.tab('terminal');
    const target = { requestFullscreen() { window.document.fullscreenElement = target; documentListeners.get('fullscreenchange')?.(); return Promise.resolve(); } };
    tab.panel.getEl = () => ({ dom: target }); tab.panel.getSize = () => ({ width: 1000, height: 624 });
    tab.panel.setSize = (width, height) => { if (typeof width === 'object') ({ width, height } = width); terminalHost.clientWidth = width; terminalHost.clientHeight = height - 24; };
    tab.control('stack-terminal-output').getEl = () => ({ dom: terminalHost });
    assert.deepEqual(Array.from(tab.config.tbar, (item) => item.text), ['新建', '全屏']);
    tab.respond({ stack_id: '3'.repeat(24), server_id: '1'.repeat(24), services: [{ service: 'web', server_id: '1'.repeat(24), container_id: 'b'.repeat(64), name: 'web', can_open: true }], shells: ['sh', 'bash'], csrf_token: 'terminal-csrf' });
    const button = tab.control('stack-new-terminal'); button.handler(button); const dialog = page.browser.Ext.dialogs.at(-1);
    dialog.down('#pve-komodo-terminal-service').setValue('web'); dialog.down('#pve-komodo-terminal-create').handler();
    tab.respond({ grant: 'single-use-grant', session_id: 'ceaa63ca-3693-4ef0-8cbf-4de83911a76b', expires_in: 60 });
    await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
    assert.deepEqual(assets, ['/xtermjs/xterm.css', '/xtermjs/xterm.js']); assert.equal(sockets.length, 1);
    const socket = sockets[0], terminal = terminals[0];
    assert.equal(terminal.cols, 121); assert.equal(terminal.rows, 36, 'the terminal fits measured cells to the full available body');
    assert.equal(observers[0].element, terminalHost);
    assert.equal(socket.url, 'wss://pve.example.test/pve-komodo/api/stacks/' + '3'.repeat(24) + '/terminals/ws');
    assert.equal(socket.binaryType, 'arraybuffer'); socket.onopen();
    assert.deepEqual(socket.sent[0], { type: 'open', grant: 'single-use-grant', csrf_token: 'terminal-csrf' });
    terminal.data('before-ready'); assert.equal(socket.sent.length, 1);
    socket.onmessage({ data: JSON.stringify({ type: 'ready', session_id: 'ceaa63ca-3693-4ef0-8cbf-4de83911a76b' }) });
    terminal.data('ls\r'); terminal.resize(120, 40);
    assert.deepEqual(socket.sent.at(-2), { type: 'data', data: 'ls\r' }); assert.deepEqual(socket.sent.at(-1), { type: 'resize', cols: 120, rows: 40 });
    socket.onmessage({ data: Uint8Array.from([228, 184]).buffer }); socket.onmessage({ data: Uint8Array.from([173]).buffer });
    assert.deepEqual(terminal.writes, [[228, 184], [173]], 'UTF-8 bytes cross frames unchanged for xterm to decode');
    terminalHost.clientWidth = 1320; terminalHost.clientHeight = 800; observers[0].callback();
    assert.deepEqual(socket.sent.at(-1), { type: 'resize', cols: 161, rows: 49 }, 'a real container size change updates the existing PTY');
    const fullscreen = tab.control('stack-terminal-fullscreen'); assert.equal(fullscreen.disabled, false); fullscreen.handler(fullscreen);
    assert.equal(fullscreen.text, '退出全屏'); assert.equal(window.document.fullscreenElement, target);
    assert.deepEqual(socket.sent.at(-1), { type: 'resize', cols: 196, rows: 60 });
    fullscreen.handler(fullscreen); assert.equal(fullscreen.text, '全屏'); assert.equal(window.document.fullscreenElement, null);
    assert.deepEqual(socket.sent.at(-1), { type: 'resize', cols: 121, rows: 36 }, 'exit restores the normal body size');
    assert.equal(sockets.length, 1, 'resizing and fullscreen never reconnect or create another terminal');
    socket.onmessage({ data: JSON.stringify({ type: 'error', code: 'terminal_service_changed' }) });
    socket.onclose();
    assert.match(page.control('stack-status').text, /容器或服务已变化/);
    assert.doesNotMatch(page.control('stack-status').text, /已断开/);
    tab.destroy(); assert.equal(socket.closed, 1000); assert.equal(terminal.disposed, true);
    assert.equal(observers[0].disconnected, true); assert.equal(listeners.size, 0); assert.equal(documentListeners.size, 0);
    terminal.data('late'); assert.equal(sockets.length, 1); assert.notEqual(socket.sent.at(-1).data, 'late');
    page.destroy(); assert.equal(sockets.length, 1, 'no automatic connection or retry after leaving');
});

test('a newer terminal attempt supersedes a grant waiting for shared lazy xterm assets and cannot leave a hidden PTY', async () => {
    const page = stackPage(), window = page.browser.context.window, terminals = [], sockets = []; let assetRequest;
    class Terminal {
        constructor(options) { Object.assign(this, options); terminals.push(this); }
        open() {} onData() {} onResize() {} dispose() { this.disposed = true; }
    }
    class WebSocket {
        constructor(url) { this.url = url; this.sent = []; sockets.push(this); } send(value) { this.sent.push(JSON.parse(value)); } close() { this.closed = true; }
    }
    page.browser.context.URL = URL; window.location = { href: 'https://pve.example.test/' }; window.WebSocket = WebSocket;
    window.document = { getElementById: () => null, createElement: () => ({}), head: { appendChild() {} } };
    page.browser.Ext.Loader = { loadScript(options) { assert.equal(assetRequest, undefined); assetRequest = options; } };
    page.tab('config'); page.respond(sampleStackDetails()); const tab = page.tab('terminal');
    tab.respond({ stack_id: '3'.repeat(24), server_id: '1'.repeat(24), services: [{ service: 'web', server_id: '1'.repeat(24), container_id: 'b'.repeat(64), can_open: true }], csrf_token: 'terminal-csrf' });
    function create(grant) {
        const button = tab.control('stack-new-terminal'); button.handler(button); const dialog = page.browser.Ext.dialogs.at(-1);
        dialog.down('#pve-komodo-terminal-service').setValue('web'); dialog.down('#pve-komodo-terminal-create').handler();
        tab.respond({ grant, session_id: 'ceaa63ca-3693-4ef0-8cbf-4de83911a76b', expires_in: 60 });
    }
    create('grant-A'); create('grant-B'); assert.equal(sockets.length, 0);
    window.Terminal = Terminal; assetRequest.onLoad(); await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
    assert.equal(terminals.length, 1); assert.equal(sockets.length, 1, 'only the most recent explicit attempt creates a shell transport');
    sockets[0].onopen(); assert.equal(sockets[0].sent[0].grant, 'grant-B');
    tab.destroy(); assert.equal(sockets[0].closed, true); assert.equal(terminals[0].disposed, true);
});

function containerDetailsPage(browser = containerViewBrowser()) {
    const view = containerView(browser); view.switch('pve-komodo-containers');
    view.respond(sampleContainerTree()); view.respond(sampleTreeLinks(), 200, 'links');
    view.click('container:' + sampleContainerTree().containers[0].id);
    const config = view.workspace.content;
    const page = dashboard(browser, 'container-detail', { ...config, items: [] });
    let current;
    return { ...page, view, original: config,
        tab(kind) {
            if (current) current.destroy();
            current = dashboard(browser, 'container-tab', config.items.find((item) => item.pveKomodoContainerTab === kind), page.panel);
            current.activate(); return current;
        },
        press(action) { const button = page.control('container-' + action); if (!button.disabled) button.handler(button); },
        destroy() { page.destroy(); if (current && !current.panel.destroyed) current.destroy(); },
    };
}
function sampleContainerDetails() {
    const row = sampleContainerTree().containers[0];
    return { server_id: row.server_id, server_name: '<server>', container_id: row.id.split(':')[1], container_name: '<web>',
        state: 'running', status: 'Up 1 day', version: 'a'.repeat(64), description: '<original description>', description_version: 'b'.repeat(64),
        description_scope: 'server', can_execute: true, can_edit_description: true, can_logs: true, can_inspect: true, can_terminal: true,
        information: { image: 'nginx:<tag>', image_id: 'sha256:abc', server_name: '<server>', stack_id: '3'.repeat(24), stack_name: '<stack>',
            created: 0, started_at: '2026-10-18T00:00:00Z', finished_at: '0001-01-01T00:00:00Z', restart_count: 0, platform: 'linux', driver: 'overlay2',
            hostname: 'web', user: '', working_dir: '/', entrypoint: ['/entrypoint.sh'], command: ['nginx', '-g', 'daemon off;'],
            network_mode: 'bridge', networks: ['frontend'], ports: ['80/tcp'], volumes: ['app-data'], restart_policy: 'unless-stopped',
            exit_code: 0, oom_killed: false, pid: 123, labels: { unsafe: '<img onerror=bad>' }, health: null },
        stats: { cpu_percent: '0.00%', memory_percent: null, memory_usage: '12 MiB / 1 GiB', network_io: '0 B / 2 kB', block_io: null, pids: '3' },
        updates: [{ id: 'd'.repeat(24), operation: 'RestartContainer', start_ts: 0, status: 'Complete', success: true, username: '<operator>',
            resource_type: 'Server', resource_id: row.server_id, resource_name: '<server>' }], updates_scope: 'server',
        errors: [], updated_at: '2026-10-18T00:00:00Z', csrf_token: 'container-csrf' };
}

test('container overview is native, identity scoped, distinguishes unknown from zero and safely renders shared description and server updates', () => {
    const page = containerDetailsPage(), tab = page.tab('overview'), request = page.browser.Ext.requests.at(-1), data = sampleContainerDetails();
    assert.equal(request.url, page.original.pveKomodoStackPath); assert.equal(request.method, 'GET'); assert.equal(request.disableCaching, false);
    assert.equal(page.original.pveKomodoStackPage, undefined); assert.ok(page.original.pveKomodoContainerPage);
    assert.deepEqual(Array.from(page.original.tbar.slice(0, 4), (button) => button.text), ['重启', '暂停', '停止', '销毁']);
    assert.ok(page.original.tbar.slice(0, 4).every((button) => !button.menu));
    page.respond(data);
    const information = tab.grid('container-information'), config = tab.grid('container-configuration');
    const value = (grid, label) => grid.getStore().data.find((row) => row.label === label).value;
    const render = config.columns[1].renderer;
    assert.equal(value(information, '容器 ID'), data.container_id);
    assert.match(value(information, '创建时间'), /^1970-01-01/);
    assert.equal(tab.control('container-current-state').text, '运行中');
    assert.equal(tab.control('container-current-status').text, 'Up 1 day');
    assert.equal(tab.control('container-current-health').text, '-');
    assert.equal(tab.control('container-current-restarts').text, '0');
    assert.match(tab.control('container-current-started').text, /^2026-10-18/);
    assert.equal(tab.control('container-current-finished').text, '-', 'the Docker zero-time sentinel is unknown, while real Unix zero remains valid');
    assert.ok(information.getStore().data.every((row) => !['状态', '状态详情', '健康状态', '启动时间', '结束时间', '重启次数'].includes(row.label)));
    assert.equal(tab.control('container-current-cpu').text, '0.00%'); assert.equal(tab.control('container-current-cpu').usage, 0);
    assert.equal(tab.control('container-current-memory').text, '-（12 MiB / 1 GiB）'); assert.equal(tab.control('container-current-memory').usage, 0);
    assert.equal(tab.control('container-current-network').text, '0 B / 2 kB');
    assert.equal(tab.control('container-current-block').text, '-'); assert.equal(tab.control('container-current-pids').text, '3');
    const overview = page.original.items.find((item) => item.pveKomodoContainerTab === 'overview');
    assert.equal(overview.items[0].items[0].itemId, 'pve-komodo-container-current');
    assert.equal(overview.items[0].items[0].title, '当前状态');
    assert.deepEqual(Array.from(overview.items[1].items, (item) => item.title), ['容器信息', 'Docker 配置']);
    assert.ok(overview.items[1].height > 400);
    assert.equal(render(value(config, 'OOM 终止')), '否'); assert.equal(render(value(config, '退出代码')), '0');
    assert.match(render(value(config, '标签')), /&lt;img/); assert.doesNotMatch(render(value(config, '标签')), /<img/);
    assert.match(tab.control('container-description').html, /&lt;original description&gt;/);
    const updates = tab.grid('container-updates'); assert.equal(updates.title, '服务器操作记录');
    assert.equal(updates.getStore().data[0].start_ts, 0); assert.ok(updates.columns.every((column) => column.sortable === false));
    assert.equal(page.control('container-restart').disabled, false);
    updates.listeners.itemdblclick.call(updates, null, { get: (key) => data.updates[0][key] });
    const detail = page.browser.Ext.requests.at(-1);
    assert.equal(detail.url, '/pve-komodo/api/updates/' + data.updates[0].id); assert.equal(detail.method, 'GET');
    tab.destroy(); assert.equal(detail.aborted, true, 'leaving closes and cancels the private operation detail');
});

test('container current metrics preserve Docker percentages and cumulative I/O without false zeros or unsafe markup', () => {
    for (const [cpu, memory, expectedCpu, expectedMemory, cpuBar, memoryBar] of [
        ['234.50%', '25.00%', '234.50%', '25.00%', 1, 0.25],
        [' 12.5% ', '0.00%', '12.5%', '0.00%', 0.125, 0],
        [null, null, '-', '-', 0, 0],
        ['', 'N/A', '-', '-', 0, 0],
        ['12%junk', '-1%', '-', '-', 0, 0],
        ['Infinity%', '<img src=x>', '-', '-', 0, 0],
    ]) {
        const page = containerDetailsPage(), tab = page.tab('overview'), data = sampleContainerDetails();
        data.stats = { cpu_percent: cpu, memory_percent: memory, memory_usage: '<memory>', network_io: '<network>', block_io: '0 B / 0 B', pids: '0' };
        page.respond(data);
        assert.equal(tab.control('container-current-cpu').text, expectedCpu); assert.equal(tab.control('container-current-cpu').usage, cpuBar);
        assert.equal(tab.control('container-current-memory').text, expectedMemory + '（&lt;memory&gt;）');
        assert.equal(tab.control('container-current-memory').usage, memoryBar);
        assert.equal(tab.control('container-current-network').text, '&lt;network&gt;');
        assert.equal(tab.control('container-current-block').text, '0 B / 0 B'); assert.equal(tab.control('container-current-pids').text, '0');
        data.status = '<status>'; data.information.health = '<health>'; data.information.restart_count = null;
        page.control('container-refresh').handler(page.control('container-refresh')); page.respond(data);
        assert.equal(tab.control('container-current-status').text, '&lt;status&gt;');
        assert.equal(tab.control('container-current-health').text, '&lt;health&gt;');
        assert.equal(tab.control('container-current-restarts').text, '-');
        const metrics = page.original.items.find((item) => item.pveKomodoContainerTab === 'overview').items[0].items[0].items;
        assert.ok(metrics.every((item) => item.xtype === 'pmxInfoWidget'));
        assert.deepEqual(Array.from(metrics.filter((item) => item.printBar), (item) => item.title), ['CPU', '内存']);
        page.control('container-refresh').handler(page.control('container-refresh'));
        assert.equal(tab.control('container-current-cpu').text, '-'); assert.equal(tab.control('container-current-cpu').usage, 0);
        assert.equal(tab.control('container-current-memory').text, '-'); assert.equal(tab.control('container-current-network').text, '-');
        assert.equal(tab.control('container-current-status').text, '-'); assert.equal(tab.control('container-current-health').text, '-');
        assert.equal(tab.control('container-current-started').text, '-'); assert.equal(tab.control('container-current-finished').text, '-');
        assert.equal(tab.control('container-current-restarts').text, '-');
    }
});

test('container actions confirm exact identity and preserve durable pending requests across navigation without retry', () => {
    for (const action of ['restart', 'pause', 'stop', 'destroy']) {
        const page = containerDetailsPage(), tab = page.tab('overview'), data = sampleContainerDetails(); page.respond(data);
        page.press(action); const confirmation = page.browser.Ext.dialogs.at(-1);
        assert.match(confirmation.options.message, /&lt;web&gt;.*&lt;server&gt;/);
        if (action === 'destroy') assert.match(confirmation.options.message, /可写层数据将被删除.*不删除存储卷/);
        page.answer('no'); assert.equal(page.browser.Ext.requests.filter((request) => request.method === 'POST').length, 0);
        page.press(action); page.answer('yes'); const post = page.browser.Ext.requests.at(-1);
        assert.equal(post.url, page.original.pveKomodoStackPath + '/actions'); assert.equal(post.headers['X-PVE-Komodo-CSRF'], 'container-csrf');
        assert.deepEqual(JSON.parse(JSON.stringify(post.jsonData)), { action, base_version: data.version, request_id: 'ceaa63ca-3693-4ef0-8cbf-4de83911a76b' });
        page.destroy(); assert.equal(post.aborted, undefined);
        const options = guestOptionsPage(page.browser); options.activate(); options.respond(sampleGuestOptions()); options.selectOption('region');
        assert.equal(options.control('edit').disabled, true);
        post.callback(post, true, { status: 200, responseText: JSON.stringify({ section: 'containers', action,
            results: [{ id: data.server_id + ':' + data.container_id, ok: false, status: 'outcome_unknown' }] }) });
        assert.equal(options.control('edit').disabled, false);
        assert.equal(page.browser.Ext.requests.filter((request) => request.method === 'POST').length, 1);
    }
});

test('container shared-server description resets safely and submits only a versioned metadata change', () => {
    const page = containerDetailsPage(), tab = page.tab('overview'), data = sampleContainerDetails(); page.respond(data);
    const edit = tab.control('container-description-edit'); edit.handler(edit);
    const editor = page.browser.Ext.dialogs.at(-1); assert.equal(editor.title, '编辑服务器描述');
    assert.match(editor.items[0].html, /同一服务器的其他容器/);
    const input = editor.down('#pve-komodo-container-description-input'); assert.equal(input.getValue(), data.description);
    input.setValue('changed'); editor.down('#pve-komodo-option-reset').handler(); assert.equal(input.getValue(), data.description);
    input.setValue('changed'); editor.down('#pve-komodo-option-save').handler(); page.answer('yes');
    const post = page.browser.Ext.requests.at(-1); assert.equal(post.url, page.original.pveKomodoStackPath);
    assert.deepEqual(JSON.parse(JSON.stringify(post.jsonData)), { action: 'description', description: 'changed', base_version: data.description_version,
        request_id: 'ceaa63ca-3693-4ef0-8cbf-4de83911a76b' });
    page.respond({ ok: true, status: 'completed' }); assert.equal(page.browser.Ext.requests.at(-1).method, 'GET');
    page.respond({ ...data, description: 'changed' }); assert.match(tab.control('container-description').html, /changed/);
    assert.equal(data.description, '<original description>');
});

test('the four container action slots adapt to paused, exited and created states with versioned resume or start confirmations', () => {
    for (const [state, slot, action, label] of [
        ['paused', 'pause', 'unpause', '恢复'], ['exited', 'stop', 'start', '启动'], ['created', 'stop', 'start', '启动'],
    ]) {
        const page = containerDetailsPage(); page.tab('overview'); const data = { ...sampleContainerDetails(), state, version: 'e'.repeat(64) };
        page.respond(data);
        assert.equal(page.original.tbar.filter((item) => ['container-restart', 'container-pause', 'container-stop', 'container-destroy']
            .some((name) => item.itemId === 'pve-komodo-' + name)).length, 4);
        assert.equal(page.control('container-' + slot).text, label); assert.equal(page.control('container-' + slot).disabled, false);
        assert.equal(page.control('container-restart').disabled, true);
        if (state === 'paused') assert.equal(page.control('container-stop').disabled, false);
        else assert.equal(page.control('container-pause').disabled, true);
        page.press(slot); const confirmation = page.browser.Ext.dialogs.at(-1);
        assert.equal(confirmation.options.title, '确认' + label);
        assert.match(confirmation.options.message, state === 'paused' ? /恢复此容器的进程/ : /启动此容器及其配置的主进程/);
        page.answer('yes'); const post = page.browser.Ext.requests.at(-1);
        assert.equal(post.url, page.original.pveKomodoStackPath + '/actions');
        assert.deepEqual(JSON.parse(JSON.stringify(post.jsonData)), { action, base_version: data.version,
            request_id: 'ceaa63ca-3693-4ef0-8cbf-4de83911a76b' });
        page.respond({ section: 'containers', action, results: [{ id: data.server_id + ':' + data.container_id, ok: true, status: 'submitted' }] });
        assert.match(page.control('stack-status').text, /操作已提交/);
        assert.equal(page.browser.Ext.requests.filter((request) => request.method === 'POST').length, 1);
    }
});

test('container inspection is a separately gated read-only JSON request and stale or wrong-identity replies cannot appear', () => {
    const page = containerDetailsPage(); page.tab('overview'); page.respond(sampleContainerDetails()); const tab = page.tab('inspect');
    const request = page.browser.Ext.requests.at(-1); assert.equal(request.url, page.original.pveKomodoStackPath + '/inspect');
    const field = tab.control('container-inspect'); assert.equal(field.readOnly, true);
    assert.equal(tab.config.layout, 'auto'); assert.equal(tab.config.scrollable, true); assert.equal(field.scrollable, undefined);
    const scrolling = [];
    tab.panel.updateLayout = () => scrolling.push('layout');
    tab.panel.getScrollable = () => ({ refresh: () => scrolling.push('refresh'), scrollTo: (x, y) => scrolling.push([x, y]) });
    tab.respond({ server_id: sampleContainerDetails().server_id, container_id: sampleContainerDetails().container_id, inspect: { Image: '<img>', Zero: 0 } });
    assert.deepEqual(scrolling, ['layout', 'refresh', [0, 0]]);
    assert.match(field.html, /tabindex="0"/);
    assert.match(field.html, /pve-komodo-json-line-number/); assert.match(field.html, /pve-komodo-json-key/);
    assert.match(field.html, /pve-komodo-json-string">&quot;&lt;img&gt;&quot;/);
    assert.match(field.html, /pve-komodo-json-number">0<\/span>/); assert.doesNotMatch(field.html, /<img>/);
    page.press('refresh'); page.respond({ ...sampleContainerDetails(), can_inspect: false });
    assert.equal(field.html, ''); assert.equal(page.browser.Ext.requests.at(-1).url, page.original.pveKomodoStackPath);
    const bad = containerDetailsPage(), badTab = bad.tab('overview'); bad.respond({ ...sampleContainerDetails(), container_id: 'f'.repeat(64) });
    assert.equal(badTab.grid('container-information').getStore().data.length, 0); assert.equal(bad.control('container-restart').disabled, true);
    bad.press('refresh'); const stale = bad.browser.Ext.requests.at(-1); bad.view.workspace.onLogin(null); assert.equal(stale.aborted, true);
    stale.callback(stale, true, { status: 200, responseText: JSON.stringify(sampleContainerDetails()) });
    assert.equal(bad.panel._pveKomodoStackData, null);
});

test('container cards can be destroyed after PVE detaches their parent without interrupting navigation', () => {
    for (const kind of ['overview', 'logs', 'inspect', 'terminal']) {
        const page = containerDetailsPage(); let tab = page.tab('overview'); page.respond(sampleContainerDetails());
        if (kind !== 'overview') tab = page.tab(kind);
        const pending = page.browser.Ext.requests.at(-1);
        tab.panel.up = () => undefined;
        assert.doesNotThrow(() => tab.destroy(), 'PVE must reach resume/updateLayout after removing its old card');
        if (kind !== 'overview') assert.equal(pending.aborted, true);
    }
});

test('container logs verify identity, merge search output visibly, encode content and stop polling on leave', () => {
    const page = containerDetailsPage(), timers = new Map(); let timerId = 0;
    page.browser.context.window.setTimeout = (fn) => { const id = ++timerId; timers.set(id, fn); return id; };
    page.browser.context.window.clearTimeout = (id) => timers.delete(id);
    page.tab('overview'); const data = sampleContainerDetails(); page.respond(data); const tab = page.tab('logs');
    const first = page.browser.Ext.requests.at(-1); assert.equal(first.url, page.original.pveKomodoStackPath + '/logs');
    assert.ok(['tail', 'timestamps', 'search', 'invert', 'stream', 'poll'].every((name) => tab.control('stack-log-' + name)));
    assert.equal(first.params.tail, 100); assert.equal(first.params.services, undefined); assert.equal(tab.control('stack-log-services'), undefined);
    tab.respond({ server_id: data.server_id, container_id: data.container_id, stdout: '<img>', stderr: 'error', success: true });
    assert.match(tab.control('stack-log-output').html, /&lt;img&gt;/);
    tab.control('stack-log-stream').setValue('stderr'); assert.match(tab.control('stack-log-output').html, /error/);
    tab.control('stack-log-search').setValue('needle'); tab.control('stack-log-invert').setValue(true);
    const search = page.browser.Ext.requests.at(-1); assert.equal(search.params.search, 'needle'); assert.equal(search.params.invert, true);
    assert.equal(tab.control('stack-log-stream').getValue(), 'stdout'); assert.equal(tab.control('stack-log-stream').disabled, true);
    tab.respond({ server_id: data.server_id, container_id: data.container_id, stdout: 'merged search', stderr: '', success: true, search_streams_merged: true });
    assert.match(tab.control('stack-log-output').html, /merged search/); assert.match(page.control('stack-status').text, /标准输出与标准错误/);
    tab.control('stack-log-search').setValue(''); tab.control('stack-log-poll').setValue(true);
    assert.equal(tab.control('stack-log-stream').disabled, false);
    tab.respond({ server_id: data.server_id, container_id: 'f'.repeat(64), stdout: 'WRONG', stderr: '', success: true });
    assert.doesNotMatch(tab.control('stack-log-output').html, /WRONG/); assert.equal(timers.size, 1);
    tab.destroy(); assert.equal(timers.size, 0);
});

test('container terminal modes send only an explicit validated mode and command with the existing CSRF and UUID grant contract', () => {
    for (const shell of ['sh', 'bash', 'attach', 'custom']) {
        const page = containerDetailsPage(); page.tab('overview'); const data = sampleContainerDetails(); page.respond(data); const tab = page.tab('terminal');
        const request = page.browser.Ext.requests.at(-1); assert.equal(request.url, page.original.pveKomodoStackPath + '/terminal-services');
        tab.respond({ server_id: data.server_id, container_id: data.container_id, can_open: true, shells: ['sh', 'bash', 'attach', 'custom'],
            options: { sh: true, bash: true, attach: true, custom: true }, csrf_token: 'terminal-csrf' });
        const button = tab.control('stack-new-terminal'); assert.equal(button.disabled, false); button.handler(button);
        const dialog = page.browser.Ext.dialogs.at(-1); dialog.down('#pve-komodo-terminal-shell').setValue(shell);
        const command = dialog.down('#pve-komodo-terminal-command'); assert.equal(command.disabled, shell !== 'custom');
        if (shell === 'custom') {
            command.setValue('bad\ncommand'); dialog.down('#pve-komodo-terminal-create').handler();
            assert.equal(page.browser.Ext.requests.filter((item) => item.method === 'POST').length, 0);
            assert.match(dialog.down('#pve-komodo-container-terminal-error').html, /有效命令/); command.setValue('/bin/sh -l');
        }
        dialog.down('#pve-komodo-terminal-create').handler(); const post = page.browser.Ext.requests.at(-1);
        assert.equal(post.url, page.original.pveKomodoStackPath + '/terminals'); assert.equal(post.headers['X-PVE-Komodo-CSRF'], 'terminal-csrf');
        assert.deepEqual(JSON.parse(JSON.stringify(post.jsonData)), { shell, ...(shell === 'custom' ? { command: '/bin/sh -l' } : {}),
            request_id: 'ceaa63ca-3693-4ef0-8cbf-4de83911a76b' });
        tab.respond({ detail: 'PRIVATE' }, 502); assert.doesNotMatch(page.control('stack-status').text, /PRIVATE/);
        assert.equal(page.browser.Ext.requests.filter((item) => item.method === 'POST').length, 1, 'a failed grant is never retried automatically');
    }
});

test('stack and container terminal dialogs wait for fullscreen exit and discard a late exit after navigation', async () => {
    for (const container of [false, true]) {
        const page = container ? containerDetailsPage() : stackPage();
        page.tab(container ? 'overview' : 'config');
        const data = container ? sampleContainerDetails() : sampleStackDetails(); page.respond(data);
        const tab = page.tab('terminal');
        tab.respond(container ? {
            server_id: data.server_id, container_id: data.container_id, can_open: true, shells: ['sh'],
            options: { sh: true }, csrf_token: 'terminal-csrf',
        } : {
            stack_id: '3'.repeat(24), server_id: '1'.repeat(24), shells: ['sh', 'bash'], csrf_token: 'terminal-csrf',
            services: [{ service: 'web', server_id: '1'.repeat(24), container_id: 'b'.repeat(64), can_open: true }],
        });
        const window = page.browser.context.window, target = {}; let exit, exitCount = 0;
        tab.panel.getEl = () => ({ dom: target });
        window.document = { fullscreenElement: target, exitFullscreen() {
            exitCount++; return new Promise((resolve) => { exit = () => { this.fullscreenElement = null; resolve(); }; });
        } };
        const button = tab.control('stack-new-terminal'), count = page.browser.Ext.dialogs.length;
        button.handler(button); button.handler(button);
        assert.equal(exitCount, 1, 'repeated clicks do not open overlapping fullscreen exits');
        assert.equal(page.browser.Ext.dialogs.length, count, 'the modal cannot render outside the fullscreen panel');
        exit(); await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
        assert.equal(page.browser.Ext.dialogs.length, count + 1);
        assert.equal(page.browser.Ext.requests.filter((request) => request.method === 'POST').length, 0, 'exiting fullscreen does not create a terminal');
        page.browser.Ext.dialogs.at(-1).destroy();
        window.document.fullscreenElement = target; button.handler(button); tab.destroy();
        exit(); await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
        assert.equal(page.browser.Ext.dialogs.length, count + 1, 'leaving the terminal tab cancels the pending dialog');
    }
});

function guestInventoryPage(kind = 'docker', browser = containerViewBrowser(), type = 'qemu', vmid = 102) {
    const workspace = { content: null, setContent(value) { this.content = value; } };
    const owner = { workspace };
    const native = browser.Ext.create(type === 'qemu' ? 'PVE.qemu.Config' : 'PVE.lxc.Config', {
        pveSelNode: { data: { type, vmid, node: 'node-1' } },
    });
    const page = dashboard(browser, 'guest-inventory', native.savedItems['pve-komodo-guest-docker-' + kind], owner);
    page.workspace = workspace;
    page.inventoryGrid = (section) => page.grid('guest-inventory-' + section);
    page.inventoryControl = (section, name) => page.inventoryGrid(section).down('#pve-komodo-guest-inventory-' + section + '-' + name);
    page.selectInventory = (section, ids) => {
        const grid = page.inventoryGrid(section);
        grid.getSelectionModel().select(grid.getStore().data.filter((row) => ids.includes(row.id)));
    };
    page.pressInventory = (section, name) => { const button = page.inventoryControl(section, name); if (!button.disabled) button.handler(button); };
    page.refreshInventory = () => { const button = page.control('guest-inventory-refresh'); if (!button.disabled) button.handler(button); };
    return page;
}
function sampleGuestInventory() {
    return {
        server_id: '1'.repeat(24), server_name: '<server>', binding_revision: 7, csrf_token: 'inventory-csrf', can_create_stack: true, errors: [],
        containers: [{ id: 'a'.repeat(64), name: '<browserless>', version: '1'.repeat(64), state: 'running', image: '<image>', network: 'host',
            allowed_actions: ['restart', 'pause', 'stop', 'destroy'] },
        { id: 'b'.repeat(64), name: 'other', version: '2'.repeat(64), state: 'stopped', image: 'alpine', network: 'bridge', allowed_actions: ['start', 'destroy'] }],
        networks: [{ id: 'c'.repeat(64), name: 'bridge', version: '3'.repeat(64), driver: 'bridge', scope: 'local', attachable: false, ipam_driver: 'default', builtin: true, allowed_actions: [] },
            { id: 'd'.repeat(64), name: '<custom>', version: '4'.repeat(64), driver: 'bridge', scope: 'local', attachable: true, ipam_driver: 'default', builtin: false, allowed_actions: ['delete'] }],
        volumes: [{ id: 'data', name: '<data>', version: '5'.repeat(64), driver: 'local', scope: 'local', allowed_actions: ['delete'] }],
        images: [{ id: 'sha256:' + 'e'.repeat(64), name: '<image>', version: '6'.repeat(64), allowed_actions: ['delete'] }],
        stacks: [{ id: '3'.repeat(24), name: '<stack>', version: '7'.repeat(64), source: '服务器上的文件', server_name: '<server>', state: 'running', tags: ['<prod>'],
            allowed_actions: ['check_update', 'pull', 'deploy', 'restart', 'stop', 'destroy', 'delete'] }],
        deployments: [{ id: '4'.repeat(24), name: '<deployment>', image: '<image>', server_name: '<server>', state: 'running', tags: [], allowed_actions: [] }],
        repos: [{ id: '5'.repeat(24), name: '<repo>', repo: 'owner/repo', branch: 'main', state: 'Ok', tags: [], allowed_actions: [] }],
    };
}
const inventoryRecord = (row) => ({ get: (name) => row[name] });

test('server Docker inventory lays out four selectable tables in two rows and resources retain three rows with flat actions', () => {
    for (const type of ['qemu', 'lxc']) {
        const page = guestInventoryPage('docker', containerViewBrowser(), type, type === 'qemu' ? 102 : 252);
        assert.equal(page.browser.Ext.requests.length, 0, 'registering server cards does not fetch data');
        assert.equal(page.config.scrollable, true); assert.equal(page.config.items.length, 2);
        assert.deepEqual(Array.from(page.config.items, (row) => Array.from(row.items, (grid) => grid.title)), [['容器', '网络'], ['存储卷', '镜像']]);
        assert.ok(page.config.items.every((row) => row.layout.type === 'hbox' && row.layout.align === 'stretch'));
        page.activate(); const request = page.browser.Ext.requests.at(-1);
        assert.equal(request.url, '/pve-komodo/api/guests/' + type + '/' + (type === 'qemu' ? 102 : 252) + '/docker');
        assert.equal(request.method, 'GET'); assert.equal(request.disableCaching, false);
        page.respond(sampleGuestInventory());
        const grid = page.inventoryGrid('containers');
        assert.equal(grid.selModel.selType, 'checkboxmodel'); assert.equal(grid.selModel.mode, 'MULTI'); assert.equal(grid.selModel.checkOnly, true);
        assert.deepEqual(Array.from(grid.columns.filter((column) => column.dataIndex), (column) => column.text), ['名称', '状态', '镜像', '网络']);
        assert.deepEqual(Array.from(grid.tbar.items, (button) => button.text), ['启动', '重启', '暂停', '恢复', '停止', '销毁']);
        assert.ok(grid.tbar.items.every((button) => !button.menu));
        assert.equal(grid.columns.at(-1).xtype, 'actioncolumn');
        assert.equal(grid.columns.at(-1).items[0].isDisabled(), false);
        assert.ok(['networks', 'volumes', 'images'].every((section) => page.inventoryGrid(section).tbar.items[0].text === '删除'));
        assert.match(page.control('guest-inventory-status').text, /&lt;server&gt;/);
    }
    const page = guestInventoryPage('resources');
    assert.deepEqual(Array.from(page.config.items, (grid) => grid.title), ['应用栈', '部署', '代码仓库']);
    assert.ok(page.config.items.every((grid) => grid.xtype === 'gridpanel' && grid.height >= 300));
    page.activate(); assert.match(page.browser.Ext.requests.at(-1).url, /\/resources$/); page.respond(sampleGuestInventory());
    const stacks = page.inventoryGrid('stacks');
    assert.deepEqual(Array.from(stacks.tbar.items, (button) => button.text), ['新建应用栈', '检查更新', '拉取', '部署', '重启', '停止', '销毁', '删除']);
    assert.ok(stacks.tbar.items.every((button) => !button.menu));
    for (const section of ['deployments', 'repos']) {
        assert.equal(page.inventoryControl(section, 'new').disabled, true);
        assert.equal(page.inventoryGrid(section).columns.at(-1).items[0].isDisabled(), true, 'future detail routes stay explicit placeholders');
    }
});

test('server inventory selections honor builtin and per-row permissions and confirm an immutable full target payload before any POST', () => {
    const page = guestInventoryPage(); page.activate(); const data = sampleGuestInventory(); page.respond(data);
    page.selectInventory('networks', ['c'.repeat(64)]);
    assert.equal(page.inventoryGrid('networks').getSelectionModel().getSelection().length, 0, 'builtin networks cannot be selected');
    assert.equal(page.inventoryControl('networks', 'delete').disabled, true);
    page.selectInventory('containers', ['a'.repeat(64), 'b'.repeat(64)]);
    assert.equal(page.inventoryControl('containers', 'restart').disabled, true);
    assert.equal(page.inventoryControl('containers', 'destroy').disabled, false);
    page.selectInventory('containers', ['a'.repeat(64)]);
    assert.equal(page.inventoryControl('containers', 'start').disabled, true);
    assert.equal(page.inventoryControl('containers', 'restart').disabled, false);
    page.pressInventory('containers', 'restart');
    assert.equal(page.browser.Ext.requests.filter((request) => request.method === 'POST').length, 0);
    const dialog = page.browser.Ext.dialogs.at(-1);
    assert.match(dialog.options.message, /&lt;browserless&gt;/); assert.doesNotMatch(dialog.options.message, /<browserless>/);
    assert.match(dialog.options.message, /中断服务/); page.answer('no');
    assert.equal(page.browser.Ext.requests.filter((request) => request.method === 'POST').length, 0);
    page.pressInventory('containers', 'restart');
    const selectedRow = page.inventoryGrid('containers').getStore().data[0]; selectedRow.version = 'f'.repeat(64);
    page.selectInventory('containers', ['b'.repeat(64)]);
    page.answer('yes'); const post = page.browser.Ext.requests.at(-1);
    assert.equal(post.url, '/pve-komodo/api/guests/qemu/102/inventory-actions'); assert.equal(post.method, 'POST');
    assert.equal(post.headers['X-PVE-Komodo-CSRF'], 'inventory-csrf');
    assert.deepEqual(JSON.parse(JSON.stringify(post.jsonData)), { server_id: data.server_id, binding_revision: 7, section: 'containers', action: 'restart',
        request_id: 'ceaa63ca-3693-4ef0-8cbf-4de83911a76b', targets: [{ id: 'a'.repeat(64), version: '1'.repeat(64) }] });
    assert.equal(page.control('guest-inventory-refresh').disabled, true);
    page.respond({ section: 'containers', action: 'restart', results: [{ id: 'a'.repeat(64), status: 'submitted', ok: true }] });
    assert.equal(page.inventoryGrid('containers').getStore().data.length, 0, 'a write invalidates the old inventory instead of reusing versions');
    assert.match(page.control('guest-inventory-status').text, /已提交/);
    assert.equal(page.browser.Ext.requests.filter((request) => request.method === 'POST').length, 1);
});

test('server inventory local search encodes display data, clears hidden selections and never sends a search request', () => {
    const page = guestInventoryPage(); page.activate(); page.respond(sampleGuestInventory());
    const grid = page.inventoryGrid('containers'), row = grid.getStore().data[0];
    const name = grid.columns.find((column) => column.dataIndex === 'name');
    assert.equal(name.renderer(row.name, {}, inventoryRecord(row)), '&lt;browserless&gt;');
    const network = page.inventoryGrid('networks'), builtin = network.getStore().data[0];
    assert.match(network.columns[0].renderer(builtin.name, {}, inventoryRecord(builtin)), /系统/);
    const attachable = network.columns.find((column) => column.dataIndex === 'attachable');
    assert.equal(attachable.renderer(false), '否'); assert.equal(attachable.renderer(null), '-');
    page.selectInventory('containers', [row.id]); const count = page.browser.Ext.requests.length;
    page.inventoryControl('containers', 'search').setValue('ALPINE');
    assert.deepEqual(Array.from(grid.getStore().data, (entry) => entry.name), ['other']);
    assert.equal(grid.getSelectionModel().getSelection().length, 0); assert.equal(page.inventoryControl('containers', 'destroy').disabled, true);
    assert.equal(page.browser.Ext.requests.length, count);
    page.inventoryControl('containers', 'search').setValue('missing'); assert.equal(grid.getStore().data.length, 0);
    page.inventoryControl('containers', 'search').setValue(''); assert.equal(grid.getStore().data.length, 2);
    const resources = guestInventoryPage('resources'); resources.activate(); resources.respond(sampleGuestInventory());
    const tags = resources.inventoryGrid('stacks').columns.find((column) => column.dataIndex === 'tags');
    assert.match(tags.renderer(['<prod>']), /&lt;prod&gt;/); assert.doesNotMatch(tags.renderer(['<prod>']), /<prod>/);
});

test('server inventory read-only rows stay disabled and native jump icons route only exact container and stack identities', () => {
    const page = guestInventoryPage(); page.activate(); const data = sampleGuestInventory(); data.containers[0].allowed_actions = []; page.respond(data);
    page.selectInventory('containers', ['a'.repeat(64)]); assert.equal(page.inventoryControl('containers', 'restart').disabled, true);
    const grid = page.inventoryGrid('containers'), action = grid.columns.at(-1).items[0], before = page.browser.Ext.requests.length;
    action.handler(grid, 0, 0, null, null, inventoryRecord(grid.getStore().data[0]));
    assert.equal(page.workspace.content.pveKomodoContainerPage, true); assert.equal(page.workspace.content.pveKomodoContainerServer, data.server_id);
    assert.equal(page.workspace.content.pveKomodoContainerId, 'a'.repeat(64));
    assert.equal(page.browser.Ext.requests.length, before, 'navigation itself never executes a resource action');
    page.workspace.content = null; action.handler(grid, 0, 0, null, null, inventoryRecord({ id: 'a'.repeat(12), name: 'short' }));
    assert.equal(page.workspace.content, null, 'short IDs never guess a container target');
    const resources = guestInventoryPage('resources'); resources.activate(); resources.respond(sampleGuestInventory());
    const stackGrid = resources.inventoryGrid('stacks');
    stackGrid.columns.at(-1).items[0].handler(stackGrid, 0, 0, null, null, inventoryRecord(stackGrid.getStore().data[0]));
    assert.equal(resources.workspace.content.pveKomodoStackPage, true); assert.equal(resources.workspace.content.pveKomodoStackId, '3'.repeat(24));
    assert.equal(resources.workspace.content._pveKomodoStackExpectedServer, data.server_id);
    resources.workspace.content = null;
    for (const section of ['deployments', 'repos']) {
        const placeholder = resources.inventoryGrid(section);
        placeholder.columns.at(-1).items[0].handler(placeholder, 0, 0, null, null, inventoryRecord(placeholder.getStore().data[0]));
        assert.equal(resources.workspace.content, null);
    }
});

test('server inventory create-stack uses a separate name dialog and confirmed create_stack request only', () => {
    const page = guestInventoryPage('resources'); page.activate(); page.respond(sampleGuestInventory());
    page.pressInventory('stacks', 'new'); const editor = page.browser.Ext.dialogs.at(-1);
    assert.equal(editor.title, '新建应用栈'); assert.equal(page.browser.Ext.requests.filter((request) => request.method === 'POST').length, 0);
    editor.down('#pve-komodo-inventory-stack-name').setValue('  <new-stack>  ');
    editor.buttons.find((button) => button.text === '创建').handler();
    assert.match(editor.down('#pve-komodo-inventory-create-error').html, /名称须/);
    assert.equal(page.browser.Ext.dialogs.at(-1), editor);
    editor.down('#pve-komodo-inventory-stack-name').setValue('  new-stack  ');
    editor.buttons.find((button) => button.text === '创建').handler();
    assert.match(page.browser.Ext.dialogs.at(-1).options.message, /new-stack/);
    assert.equal(page.browser.Ext.requests.filter((request) => request.method === 'POST').length, 0); page.answer('yes');
    const post = page.browser.Ext.requests.at(-1);
    assert.deepEqual(JSON.parse(JSON.stringify(post.jsonData)), { server_id: '1'.repeat(24), binding_revision: 7, section: 'stacks', action: 'create_stack',
        request_id: 'ceaa63ca-3693-4ef0-8cbf-4de83911a76b', name: 'new-stack' });
    assert.equal(editor.destroyed, true); page.respond({ section: 'stacks', action: 'create_stack', results: [{ id: 'new', status: 'completed' }] });
    const readOnly = guestInventoryPage('resources'); readOnly.activate(); readOnly.respond({ ...sampleGuestInventory(), can_create_stack: false });
    assert.equal(readOnly.inventoryControl('stacks', 'new').disabled, true);
});

test('server inventory cancels stale GETs and confirmations on leave, ignores auth changes and never aborts or retries a submitted write', () => {
    const page = guestInventoryPage(); page.activate(); const first = page.browser.Ext.requests.at(-1);
    page.deactivate(); assert.equal(first.aborted, true);
    first.callback(first, true, { status: 200, responseText: JSON.stringify(sampleGuestInventory()) });
    assert.equal(page.inventoryGrid('containers').getStore().data.length, 0);
    page.activate(); page.respond(sampleGuestInventory()); page.selectInventory('containers', ['a'.repeat(64)]); page.pressInventory('containers', 'stop');
    const confirm = page.browser.Ext.dialogs.at(-1); page.deactivate(); assert.equal(confirm.destroyed, true); confirm.options.fn('yes');
    assert.equal(page.browser.Ext.requests.filter((request) => request.method === 'POST').length, 0);
    page.activate(); const authRead = page.browser.Ext.requests.at(-1); page.browser.context.window.Proxmox.UserName = 'other@pam';
    authRead.callback(authRead, true, { status: 200, responseText: JSON.stringify(sampleGuestInventory()) });
    assert.equal(page.inventoryGrid('containers').getStore().data.length, 0);
    page.browser.context.window.Proxmox.UserName = 'root@pam'; page.deactivate(); page.activate(); page.respond(sampleGuestInventory());
    page.selectInventory('volumes', ['data']); page.pressInventory('volumes', 'delete');
    assert.match(page.browser.Ext.dialogs.at(-1).options.message, /永久删除卷内数据/); page.answer('yes');
    const post = page.browser.Ext.requests.at(-1); page.destroy(); assert.equal(post.aborted, undefined);
    assert.doesNotThrow(() => post.callback(post, true, { status: 200, responseText: '{"section":"volumes","action":"delete","results":[{"status":"submitted"}]}' }));
    assert.equal(page.browser.Ext.requests.filter((request) => request.method === 'POST').length, 1);
});

function guestServerTerminalPage(browser = containerViewBrowser(), type = 'qemu', vmid = 102) {
    const workspace = { _pveKomodoLoggedOut: false }, owner = { workspace };
    const native = browser.Ext.create(type === 'qemu' ? 'PVE.qemu.Config' : 'PVE.lxc.Config', { pveSelNode: { data: { type, vmid, node: 'node-1' } } });
    const config = native.savedItems['pve-komodo-guest-docker-terminal'];
    const page = dashboard(browser, 'guest-terminal', { ...config, items: [] }, owner);
    const tab = dashboard(browser, 'server-terminal-inner', config.items[0], page.panel);
    page.components.set('#pve-komodo-stack-terminal', tab.panel);
    page.tab = tab; page.original = config;
    return page;
}

test('server terminal keeps the shared inner terminal panel and grants only an explicit allowed server command with association context', () => {
    for (const shell of ['default', 'sh', 'bash', 'custom']) {
        const page = guestServerTerminalPage(), tab = page.tab;
        assert.equal(page.browser.Ext.requests.length, 0); assert.equal(page.original.layout, 'fit'); assert.equal(page.original.items.length, 1);
        assert.equal(page.original.items[0].pveKomodoStackTab, 'terminal');
        assert.equal(page.original.items[0].cls, 'pve-komodo-stack-terminal-tab');
        assert.deepEqual(Array.from(page.original.items[0].tbar, (button) => button.text), ['新建', '全屏']);
        page.activate(); const read = page.browser.Ext.requests.at(-1);
        assert.equal(read.url, '/pve-komodo/api/guests/qemu/102/terminal-services'); assert.equal(read.method, 'GET'); assert.equal(read.disableCaching, false);
        assert.equal(tab.control('stack-new-terminal').disabled, true);
        page.respond({ server_id: '1'.repeat(24), binding_revision: 7, can_open: true, shells: ['default', 'sh', 'bash', 'custom'],
            options: { default: true, sh: true, bash: true, custom: true }, csrf_token: 'server-terminal-csrf' });
        const button = tab.control('stack-new-terminal'); assert.equal(button.disabled, false); button.handler(button);
        const dialog = page.browser.Ext.dialogs.at(-1); assert.equal(dialog.title, '新建服务器终端');
        assert.deepEqual(Array.from(dialog.down('#pve-komodo-terminal-shell').store, (row) => row[0]), ['default', 'sh', 'bash', 'custom']);
        dialog.down('#pve-komodo-terminal-shell').setValue(shell);
        if (shell === 'custom') dialog.down('#pve-komodo-terminal-command').setValue('/bin/sh -l');
        assert.equal(page.browser.Ext.requests.filter((request) => request.method === 'POST').length, 0);
        dialog.down('#pve-komodo-terminal-create').handler(); const post = page.browser.Ext.requests.at(-1);
        assert.equal(post.url, '/pve-komodo/api/guests/qemu/102/terminals'); assert.equal(post.headers['X-PVE-Komodo-CSRF'], 'server-terminal-csrf');
        assert.deepEqual(JSON.parse(JSON.stringify(post.jsonData)), { shell, ...(shell === 'custom' ? { command: '/bin/sh -l' } : {}),
            server_id: '1'.repeat(24), binding_revision: 7, request_id: 'ceaa63ca-3693-4ef0-8cbf-4de83911a76b' });
        page.deactivate(); assert.equal(post.aborted, undefined); page.respond({ detail: 'PRIVATE' }, 502);
        assert.doesNotMatch(page.control('stack-status').text, /PRIVATE/);
        assert.equal(page.browser.Ext.requests.filter((request) => request.method === 'POST').length, 1);
    }
});

test('server terminal permissions and page leave disable creation and discard every late terminal-services response', () => {
    const page = guestServerTerminalPage(), tab = page.tab; page.activate();
    const read = page.browser.Ext.requests.at(-1); page.deactivate(); assert.equal(read.aborted, true);
    read.callback(read, true, { status: 200, responseText: JSON.stringify({ server_id: '1'.repeat(24), binding_revision: 7, can_open: true, shells: ['sh'], options: { sh: true }, csrf_token: 'server-terminal-csrf' }) });
    assert.equal(tab.control('stack-new-terminal').disabled, true);
    page.activate(); page.respond({ server_id: '1'.repeat(24), binding_revision: 7, can_open: false, shells: [], options: {}, csrf_token: 'server-terminal-csrf' });
    assert.equal(tab.control('stack-new-terminal').disabled, true); assert.match(page.control('stack-status').text, /终端已禁用/);
    const count = page.browser.Ext.dialogs.length; const button = tab.control('stack-new-terminal'); button.handler(button);
    assert.equal(page.browser.Ext.dialogs.length, count);
});

function serverCreateDialog() {
    const view = containerView(); view.switch('pve-komodo-containers');
    view.respond(sampleContainerTree()); view.respond(sampleTreeLinks(), 200, 'links');
    view.control('new').handler();
    const dialog = view.browser.Ext.dialogs.at(-1);
    const field = (name) => dialog.down('#pve-komodo-server-create-' + name);
    const respond = (payload, code = 200, request = view.browser.Ext.requests.at(-1)) => request.callback(request, code === 200, { status: code, responseText: JSON.stringify(payload) });
    const capabilities = { can_create: true, can_link: true, csrf_token: 'create-csrf', guests: [
        { guest_type: 'qemu', vmid: 301, name: '<VM>', node: 'pve-1' }, { guest_type: 'lxc', vmid: 302, name: 'CT', node: 'pve-2' },
    ] };
    return { ...view, dialog, field, respondCreate: respond, capabilities };
}

test('tree New opens the same explicit server creation form with an optional unselected CT or VM', () => {
    const view = serverCreateDialog();
    assert.equal(view.tree.tbar[0].text, '刷新'); assert.equal(view.tree.tbar[1].text, '新建');
    assert.equal(view.dialog.title, '新建服务器');
    assert.equal(view.browser.Ext.requests.at(-1).url, '/pve-komodo/api/server-create-options');
    assert.equal(view.field('submit').disabled, true);
    view.respondCreate(view.capabilities);
    assert.deepEqual(Array.from(view.field('guest').getStore().data, (row) => row.key), ['', 'qemu/301', 'lxc/302']);
    assert.match(view.field('guest').getStore().data[1].label, /VM 301 · <VM>/);
    assert.equal(view.field('guest').listConfig.itemTpl, '{label:htmlEncode}');
    assert.equal(view.field('guest').getValue(), null, 'a guest is never automatically selected');
    for (const invalid of ['', ' '.repeat(3), '1'.repeat(24), 'bad\nname']) {
        view.field('name').setValue(invalid); assert.equal(view.field('submit').disabled, true);
    }
    view.field('name').setValue('  New Server 中文  '); assert.equal(view.field('submit').disabled, false);
    view.field('submit').handler();
    const post = view.browser.Ext.requests.at(-1);
    assert.equal(post.method, 'POST'); assert.equal(post.url, '/pve-komodo/api/servers/create');
    assert.deepEqual(JSON.parse(JSON.stringify(post.jsonData)), { name: 'New Server 中文', request_id: 'ceaa63ca-3693-4ef0-8cbf-4de83911a76b' });
    assert.equal(post.headers['X-PVE-Komodo-CSRF'], 'create-csrf');
    view.dialog.close(); assert.equal(view.dialog.destroyed, false, 'a pending creation cannot be accidentally dismissed');
    view.field('submit').handler(); assert.equal(view.browser.Ext.requests.at(-1), post, 'repeated clicks never duplicate creation');
    view.respondCreate({ created: true, status: 'completed', server_id: 'a'.repeat(24), server_name: 'New Server 中文', link: null }, 200, post);
    assert.match(view.field('error').html, /已创建，未关联 CT 或 VM/);
    assert.equal(view.field('submit').disabled, true); view.dialog.close();
    assert.equal(view.workspace.content.pveKomodoServerId, 'a'.repeat(24));
});

test('server creation submits only an explicitly selected guest and preserves a created server after association failure', () => {
    for (const key of ['qemu/301', 'lxc/302']) {
        const view = serverCreateDialog(); view.respondCreate(view.capabilities); view.field('name').setValue('new-server');
        view.field('guest').setValue(key); view.field('submit').handler();
        const post = view.browser.Ext.requests.at(-1);
        assert.equal(post.jsonData.guest_type, key.split('/')[0]); assert.equal(post.jsonData.vmid, Number(key.split('/')[1]));
        view.respondCreate({ created: true, status: 'created_unlinked', server_id: 'a'.repeat(24), server_name: 'new-server', link: null, link_error: 'guest_identity_changed' }, 200, post);
        assert.match(view.field('error').html, /服务器已创建，关联未完成/);
        assert.equal(view.browser.Ext.requests.filter((request) => request.method === 'POST').length, 1);
        view.dialog.close(); assert.equal(view.workspace.content.pveKomodoServerPage, true);
    }
});

test('server creation permission, cancellation, uncertain replies and logout never permit a duplicate or a late mutation', () => {
    const denied = serverCreateDialog(); denied.respondCreate({ ...denied.capabilities, can_create: false });
    denied.field('name').setValue('new'); assert.equal(denied.field('submit').disabled, true); denied.dialog.close();
    const cancel = serverCreateDialog(); const read = cancel.browser.Ext.requests.at(-1); cancel.dialog.close();
    assert.equal(read.aborted, true); assert.doesNotThrow(() => cancel.respondCreate(cancel.capabilities, 200, read));
    const unknown = serverCreateDialog(); unknown.respondCreate(unknown.capabilities); unknown.field('name').setValue('new'); unknown.field('submit').handler();
    const post = unknown.browser.Ext.requests.at(-1); unknown.respondCreate({ created: null, status: 'outcome_unknown' }, 200, post);
    assert.match(unknown.field('error').html, /尚未确认/); assert.equal(unknown.field('submit').disabled, true);
    assert.equal(unknown.browser.Ext.requests.filter((request) => request.method === 'POST').length, 1); unknown.dialog.close();
    const logout = serverCreateDialog(); logout.respondCreate(logout.capabilities); logout.field('name').setValue('new'); logout.field('submit').handler();
    const active = logout.browser.Ext.requests.at(-1); logout.workspace.onLogin(null);
    assert.equal(logout.dialog.destroyed, true); assert.equal(active.aborted, undefined);
    assert.doesNotThrow(() => logout.respondCreate({ created: true, status: 'completed', server_id: 'a'.repeat(24), server_name: 'new' }, 200, active));
    assert.equal(logout.workspace.content, undefined, 'a logged-out callback cannot open the created resource');
});

test('independent server loaders reject a different server or a guest revision and accept only their pinned zero-revision target', () => {
    const view = containerView(); view.switch('pve-komodo-containers'); view.respond(sampleContainerTree()); view.respond(sampleTreeLinks(), 200, 'links');
    view.click('server:' + '2'.repeat(24)); view.respond(sampleTreeLinks(), 200, 'links');
    const owner = view.workspace.content;
    for (const kind of ['docker', 'resources']) {
        const config = owner.items.find((card) => card.pveKomodoGuestInventory === kind);
        const page = dashboard(view.browser, 'guest-inventory', config, owner); page.workspace = view.workspace;
        page.activate(); page.respond({ ...sampleGuestInventory(), server_id: '1'.repeat(24), binding_revision: 0 });
        assert.equal(page.panel._pveKomodoInventoryData, null);
        page.control('guest-inventory-refresh').handler({ up: () => page });
        page.respond({ ...sampleGuestInventory(), server_id: '2'.repeat(24), binding_revision: 1 });
        assert.equal(page.panel._pveKomodoInventoryData, null);
        page.control('guest-inventory-refresh').handler({ up: () => page });
        page.respond({ ...sampleGuestInventory(), server_id: '2'.repeat(24), binding_revision: 0 });
        assert.equal(page.panel._pveKomodoInventoryData.server_id, '2'.repeat(24)); page.deactivate();
    }
});

function summaryLinkFixture(serverId, linked = false, changes = {}) {
    const base = sampleGuestLinks();
    const link = { ...base.links[0], server_id: serverId, revision: 7 };
    return { ...base, servers: [{ id: serverId, name: '<fixed-server>', template: false, link_revision: 7 }],
        links: linked ? [link] : [], ...changes };
}
function summaryLinksPage({ direct = true, linked = false, activeTree = false } = {}) {
    const view = containerView();
    let owner, definition;
    const serverId = direct ? '2'.repeat(24) : 'a'.repeat(24);
    if (direct) {
        view.switch('pve-komodo-containers'); view.respond(sampleContainerTree()); view.respond(sampleTreeLinks(), 200, 'links');
        view.click('server:' + serverId); view.respond(sampleTreeLinks(), 200, 'links');
        owner = view.workspace.content; definition = owner.items.find((card) => card.pveKomodoGuestSummary);
        if (!activeTree) view.switch('server');
    } else {
        owner = { workspace: view.workspace };
        const native = view.browser.Ext.create('PVE.qemu.Config', { pveSelNode: { data: { type: 'qemu', vmid: 102, node: 'node-1' } } });
        definition = native.savedItems['pve-komodo-guest-docker'];
    }
    const page = dashboard(view.browser, 'summary-links', definition, owner);
    page.panel._pveKomodoExpectedServer = serverId;
    page.control = (name) => page.panel.down('#pve-komodo-guest-' + name);
    page.pressLink = (name) => { const button = page.control(name); if (!button.disabled) button.handler(button); };
    page.serverId = serverId; page.view = view; page.owner = owner; page.workspace = view.workspace;
    page.fixture = (flag = linked, changes = {}) => summaryLinkFixture(serverId, flag, changes);
    page.respondLinks = (payload = page.fixture(), status = 200, request = page.browser.Ext.guestRequests.at(-1)) => {
        request.callback(request, status >= 200 && status < 300, { status, responseText: JSON.stringify(payload) });
    };
    page.ready = (fixture = page.fixture()) => {
        page.activate();
        const request = page.browser.Ext.requests.at(-1);
        page.respond({ ...sampleGuestSummary(), server_id: serverId, binding_revision: direct ? 0 : 7 }, 200, page.browser.Ext.requests.indexOf(request));
        page.respondLinks(fixture);
    };
    page.refreshSummary = () => { const button = page.control('summary-refresh'); button.handler(button); };
    page.confirmBind = (key = 'lxc/252') => {
        page.pressLink('bind'); const dialog = page.browser.Ext.dialogs.at(-1);
        dialog.down('#pve-komodo-guest-target').setValue(key); dialog.down('#pve-komodo-guest-save').handler();
        return page.browser.Ext.dialogs.at(-1);
    };
    return page;
}

test('server summaries place the three real association controls beside refresh and gate them by the fresh fixed target', () => {
    const page = summaryLinksPage(); page.ready();
    const toolbar = page.config.tbar.items.filter((item) => item.itemId);
    const offset = toolbar.findIndex((item) => item.itemId === 'pve-komodo-guest-summary-refresh');
    assert.deepEqual(Array.from(toolbar.slice(offset + 1, offset + 4), (item) => item.text), ['关联 VM/CT', '解除关联', '打开 PVE 对象']);
    assert.equal(page.control('bind').disabled, false); assert.equal(page.control('unlink').disabled, true); assert.equal(page.control('open').disabled, true);
    assert.equal(page.panel.down('#pve-komodo-servers-grid'), undefined, 'a summary never fabricates a dashboard selection grid');
    const linked = summaryLinksPage({ direct: false, linked: true }); linked.ready();
    for (const name of ['bind', 'unlink', 'open']) assert.equal(linked.control(name).disabled, false);
    linked.pressLink('open'); assert.deepEqual(linked.workspace.nativeSelections, ['qemu/102']);
    for (const change of [{ servers: [{ id: page.serverId, name: 'template', template: true, link_revision: 7 }] }, { servers: [] }]) {
        const blocked = summaryLinksPage(); blocked.ready(blocked.fixture(false, change));
        for (const name of ['bind', 'unlink', 'open']) assert.equal(blocked.control(name).disabled, true);
    }
    const failed = summaryLinksPage(); failed.ready(); failed.refreshSummary(); failed.respondLinks({ detail: 'PRIVATE' }, 503);
    for (const name of ['bind', 'unlink', 'open']) assert.equal(failed.control(name).disabled, true);
    assert.doesNotMatch(failed.control('summary-status').text, /PRIVATE/);
});

test('summary association shares the CT identity warning and fixes server identity, revision and CSRF before the confirmed POST', () => {
    const page = summaryLinksPage(); page.ready();
    const confirmation = page.confirmBind();
    assert.match(confirmation.options.message, /CT 使用较弱的身份校验/);
    assert.match(confirmation.options.message, /&lt;fixed-server&gt;/);
    assert.equal(page.browser.Ext.guestRequests.filter((request) => request.method === 'POST').length, 0);
    page.panel._pveKomodoSummaryData.server_id = '9'.repeat(24);
    page.panel._pveKomodoGuestCsrf = 'new-token-after-dialog';
    page.answer('yes'); const post = page.browser.Ext.guestRequests.at(-1);
    assert.equal(post.url, '/pve-komodo/api/guest-links'); assert.equal(post.method, 'POST');
    assert.equal(post.headers['X-PVE-Komodo-CSRF'], 'guest-links-token');
    assert.deepEqual(JSON.parse(JSON.stringify(post.jsonData)), { server_id: page.serverId, expected_revision: 7, guest_type: 'lxc', vmid: 252 });
    assert.equal(page.control('bind').disabled, true); assert.equal(page.control('summary-refresh').disabled, true);
    page.respondLinks({ detail: 'PRIVATE' }, 409, post);
    assert.equal(page.browser.Ext.guestRequests.filter((request) => request.method === 'POST').length, 1);
    assert.equal(page.workspace.nativeSelections.length, 0, 'a rejected write does not guess a new target');
});

test('a summary binding switches direct server access to the freshly linked native guest Docker card', () => {
    const page = summaryLinksPage(); page.ready(); page.confirmBind(); page.answer('yes');
    const post = page.browser.Ext.guestRequests.at(-1);
    const menus = [];
    page.workspace.configPanels = [{ pveSelNode: { data: { id: 'lxc/252', type: 'lxc', vmid: 252 } }, selectById(id) { menus.push(id); } }];
    const link = { server_id: page.serverId, guest_type: 'lxc', vmid: 252, guest_name: 'CT', status: 'linked', can_navigate: true, revision: 8 };
    page.respondLinks({ link }, 200, post);
    assert.deepEqual(page.workspace.nativeSelections, [], 'the write receipt alone never changes routing');
    const refresh = page.browser.Ext.guestRequests.at(-1); assert.equal(refresh.method, 'GET');
    page.respondLinks(page.fixture(false, { links: [link], servers: [{ id: page.serverId, name: 'fixed', template: false, link_revision: 8 }] }), 200, refresh);
    assert.deepEqual(page.workspace.nativeSelections, ['lxc/252']);
    assert.deepEqual(menus, ['pve-komodo-guest-docker']);
    assert.equal(page.config.pveKomodoGuestBase, '/pve-komodo/api/servers/' + page.serverId, 'routing replaces a card rather than mutating its closed-over target');
});

test('a summary unlink switches every server card to the independent route after authoritative links confirm removal', () => {
    const page = summaryLinksPage({ direct: false, linked: true }); page.ready(); page.pressLink('unlink');
    assert.match(page.browser.Ext.dialogs.at(-1).options.message, /仅删除本扩展中的映射/);
    page.answer('yes'); const post = page.browser.Ext.guestRequests.at(-1);
    assert.equal(post.url, '/pve-komodo/api/guest-links/unlink');
    assert.deepEqual(JSON.parse(JSON.stringify(post.jsonData)), { server_id: page.serverId, expected_revision: 7 });
    page.respondLinks({ removed: true, revision: 8 }, 200, post);
    assert.equal(page.workspace.content, undefined, 'wait for a fresh binding snapshot');
    page.respondLinks(page.fixture(false, { servers: [{ id: page.serverId, name: 'fixed', template: false, link_revision: 8 }] }));
    const target = page.workspace.content;
    assert.equal(target.pveKomodoServerPage, true); assert.equal(target.pveKomodoServerId, page.serverId);
    assert.deepEqual(Array.from(target.items, (card) => card.title), ['Docker', '选项', 'DOCKER', '资源', '终端']);
    assert.ok(target.items.every((card) => card.pveKomodoDirectServer === page.serverId));
    assert.equal(target.items[0].pveKomodoGuestBase, '/pve-komodo/api/servers/' + page.serverId);
    assert.equal(target.items[1].pveKomodoOptionsPath, '/pve-komodo/api/servers/' + page.serverId + '/options');
    assert.equal(target.items[2].pveKomodoGuestBase, target.items[0].pveKomodoGuestBase);
    assert.equal(target.items[4].pveKomodoStackPath, target.items[0].pveKomodoGuestBase);
});

test('summary refresh, leave, user changes and logout invalidate link GETs and dialogs without accepting late callbacks', () => {
    const page = summaryLinksPage(); page.ready(); page.pressLink('bind'); const selector = page.browser.Ext.dialogs.at(-1);
    page.refreshSummary(); assert.equal(selector.destroyed, true);
    const first = page.browser.Ext.guestRequests.at(-1); page.refreshSummary(); assert.equal(first.aborted, true);
    page.respondLinks(page.fixture(), 200, first); assert.equal(page.control('bind').disabled, true);
    const pending = page.browser.Ext.guestRequests.at(-1); page.deactivate(); assert.equal(pending.aborted, true);
    assert.doesNotThrow(() => page.respondLinks(page.fixture(), 200, pending));
    page.ready(); const confirm = page.confirmBind(); page.deactivate(); assert.equal(confirm.destroyed, true);
    confirm.options.fn('yes'); assert.equal(page.browser.Ext.guestRequests.filter((request) => request.method === 'POST').length, 0);
    page.activate(); const userRead = page.browser.Ext.guestRequests.at(-1);
    page.browser.context.window.Proxmox.UserName = 'other@pam'; page.respondLinks(page.fixture(), 200, userRead);
    assert.equal(page.control('bind').disabled, true);
    page.browser.context.window.Proxmox.UserName = 'root@pam'; page.deactivate(); page.ready(); const activeConfirm = page.confirmBind();
    page.workspace.onLogin(null); assert.equal(activeConfirm.destroyed, true); activeConfirm.options.fn('yes');
    assert.equal(page.control('bind').disabled, true);
    assert.equal(page.browser.Ext.guestRequests.filter((request) => request.method === 'POST').length, 0);
});

test('a submitted summary association survives page destruction and a changed user without abort, retry or forced navigation', () => {
    for (const leave of ['destroy', 'user']) {
        const page = summaryLinksPage(); page.ready(); page.confirmBind(); page.answer('yes');
        const post = page.browser.Ext.guestRequests.at(-1), before = page.browser.Ext.guestRequests.length;
        if (leave === 'destroy') page.destroy(); else page.browser.context.window.Proxmox.UserName = 'other@pam';
        assert.equal(post.aborted, undefined);
        const link = { server_id: page.serverId, guest_type: 'lxc', vmid: 252, status: 'linked', revision: 8 };
        assert.doesNotThrow(() => page.respondLinks({ link }, 200, post));
        assert.equal(page.browser.Ext.guestRequests.length, before, 'late writes never reload a different card or account');
        assert.equal(page.browser.Ext.guestRequests.filter((request) => request.method === 'POST').length, 1);
        assert.deepEqual(page.workspace.nativeSelections, []);
    }
});

test('summary orphan cleanup updates candidates without navigating to or associating the unrelated missing server', () => {
    const page = summaryLinksPage();
    const orphanId = 'f'.repeat(24);
    const orphan = { server_id: orphanId, guest_type: 'lxc', vmid: 252, guest_name: 'CT', status: 'needs_confirmation', revision: 12,
        can_navigate: false, server_unavailable: true, server_name: null };
    page.ready(page.fixture(false, { links: [orphan] }));
    page.pressLink('bind'); const selector = page.browser.Ext.dialogs.at(-1);
    selector.down('#pve-komodo-guest-target').setValue('lxc/252');
    assert.equal(selector.down('#pve-komodo-guest-save').disabled, true);
    assert.equal(selector.down('#pve-komodo-guest-cleanup').disabled, false);
    selector.down('#pve-komodo-guest-cleanup').handler(); page.answer('yes');
    const post = page.browser.Ext.guestRequests.at(-1);
    assert.deepEqual(JSON.parse(JSON.stringify(post.jsonData)), { server_id: orphanId, expected_revision: 12 });
    page.respondLinks({ removed: true, revision: 13 }, 200, post); page.respondLinks(page.fixture(false));
    assert.deepEqual(page.workspace.nativeSelections, []);
    assert.equal(page.workspace.content.pveKomodoServerId, page.serverId, 'cleanup keeps the current server card');
    assert.equal(page.browser.Ext.guestRequests.filter((request) => request.method === 'POST').length, 1, 'cleanup never auto-binds the freed guest');
    assert.equal(page.control('bind').disabled, false);
});

test('a summary metadata write refreshes its owning container tree while retaining explicit fresh-link routing', () => {
    const page = summaryLinksPage({ activeTree: true }); page.ready(); page.confirmBind(); page.answer('yes');
    const post = page.browser.Ext.guestRequests.at(-1);
    const reads = page.browser.Ext.requests.length, linkReads = page.browser.Ext.guestRequests.filter((request) => request.method === 'GET').length;
    page.respondLinks({ link: { server_id: page.serverId, guest_type: 'lxc', vmid: 252, status: 'linked', revision: 8 } }, 200, post);
    assert.equal(page.browser.Ext.requests.length, reads + 1);
    assert.equal(page.browser.Ext.requests.at(-1).url, '/pve-komodo/api/containers');
    assert.equal(page.browser.Ext.guestRequests.filter((request) => request.method === 'GET').length, linkReads + 2,
        'the owning tree and current summary independently reload authoritative link snapshots');
    assert.deepEqual(page.workspace.nativeSelections, [], 'tree refresh does not infer navigation from a write receipt');
    assert.equal(page.browser.Ext.guestRequests.filter((request) => request.method === 'POST').length, 1);
});
