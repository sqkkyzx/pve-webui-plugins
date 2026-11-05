import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../plugins/sites/static/sites.js', import.meta.url), 'utf8');
const managerSource = readFileSync(new URL('../app/static/manager.js', import.meta.url), 'utf8');
const navigationSource = readFileSync(new URL('../app/static/navigation.js', import.meta.url), 'utf8');
const viewId = 'pve-extra-site-' + 'a'.repeat(32);
const api = '/pve-plugins/api/sites';

function browser({ manager = false, safeline = false, navigation = false } = {}) {
    const classes = new Map(), stack = [], requests = [], iconRequests = [], dialogs = [], warnings = [], removedLater = [], loadedFrames = [], openedWindows = [];
    let context, deferContentRender = false;
    function wrap(owner, name, implementation, previous) {
        return function (...args) {
            stack.push({ owner, name, previous, receiver: this });
            try { return implementation.apply(this, args); } finally { stack.pop(); }
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
        function Component(config = {}) { this.events = new Map(); this.destroyed = false; Object.assign(this, config); }
        Component.superclass = parent?.prototype;
        Component.prototype = Object.create(parent?.prototype || null);
        Component.prototype.callParent = function (args = []) {
            const frame = stack.at(-1); assert.equal(frame.receiver, this);
            return (frame.previous || frame.owner.superclass[frame.name]).apply(this, args);
        };
        for (const [key, value] of Object.entries(definition)) Component.prototype[key] = typeof value === 'function' ? wrap(Component, key, value) : value;
        classes.set(name, Component); return Component;
    }
    define('Ext.Component', {
        initComponent: function () {},
        on: function (name, callback) { if (!this.events.has(name)) this.events.set(name, []); this.events.get(name).push(callback); },
        fire: function (name, ...args) {
            let result;
            for (const callback of this.events.get(name) || []) if (callback.apply(this, args.length ? args : [this]) === false) result = false;
            return result;
        },
        fireEvent: function (name, ...args) { return this.fire(name, ...args); },
        up: function (xtype) { for (let parent = this.ownerCt; parent; parent = parent.ownerCt) if (parent.xtype === xtype) return parent; return null; },
        setVisible: function (visible) { this.hidden = !visible; this.fire(visible ? 'show' : 'hide'); },
        isVisible: function (deep) {
            if (this.hidden || this.destroyed || this.destroying) return false;
            return !deep || !this.ownerCt || this.ownerCt.isVisible(true);
        },
        setDisabled: function (disabled) { assert.equal(this.destroyed, false); this.disabled = disabled; },
        setTitle: function (title) { this.title = title; },
        setBoxLabel: function (label) { this.boxLabel = label; },
        setEmptyText: function (value) { this.emptyText = value; },
        setText: function (text) { assert.equal(this.destroyed, false); this.text = text; },
        getValue: function () { return this.value === undefined ? this.checked : this.value; }, setValue: function (value) { this.value = value; },
        update: function (html) { assert.equal(this.destroyed, false); this.html = html; },
        getEl: function () { return this.element; },
        show: function (options) { if (options) this.options = options; if (!dialogs.includes(this)) dialogs.push(this); this.render(); return this; },
        close: function () { if (this.fire('beforeclose') !== false) this.destroy(); },
        render: function () {
            if (this.rendered || this.destroyed) return;
            this.rendered = true;
            if (this.autoEl?.tag === 'iframe') {
                const attributes = {};
                this.element = { dom: { attributes, setAttribute(name, value) {
                    attributes[name] = value;
                    if (name === 'src' && value !== 'about:blank') loadedFrames.push(value);
                } } };
            }
            for (const child of this.children || []) child.render();
            this.fire('afterrender');
        },
        destroy: function () {
            if (this.destroyed || this.destroying) return;
            this.destroying = true; this.fire('beforedestroy');
            for (const child of this.children || []) child.destroy();
            if (this.ownerCt?.activePage === this) this.ownerCt.activePage = null;
            this.destroyed = true; this.fire('destroy');
        },
    });
    function store(config, grid) {
        function record(data) {
            return { data, get: (key) => data[key], getId: () => data.id, set(key, value) {
                if (typeof key === 'object') Object.assign(data, key); else data[key] = value;
            } };
        }
        const result = { rows: [], loadData(data) {
            this.rows = Array.from(data, record); if (grid?.selection) grid.selection.selected = [];
        }, add(data) { [].concat(data).forEach((entry) => this.rows.push(record(entry))); },
            remove(value) { const removed = [].concat(value); this.rows = this.rows.filter((row) => !removed.includes(row));
                if (grid?.selection) grid.selection.selected = grid.selection.selected.filter((row) => !removed.includes(row)); },
            getById(id) { return this.rows.find((row) => row.data.id === id); }, getRange() { return this.rows.slice(); },
            each(callback) { this.rows.forEach(callback); }, sort(sorter) { this.rows.sort(typeof sorter === 'function' ? sorter : sorter.sorterFn); } };
        result.loadData(config.data || []); return result;
    }
    const xtypeClasses = { component: 'Ext.Component', button: 'Ext.Component', textfield: 'Ext.Component', tbtext: 'Ext.Component',
        checkbox: 'Ext.Component', displayfield: 'Ext.Component', panel: 'Ext.panel.Panel', form: 'Ext.panel.Panel',
        toolbar: 'Ext.panel.Panel', treelist: 'Ext.list.Tree',
        container: 'Ext.panel.Panel', fieldcontainer: 'Ext.panel.Panel', grid: 'Ext.grid.Panel' };
    function create(name, config = {}) {
        const component = new (classes.get(name))(config);
        component.initComponent();
        for (const [event, handler] of Object.entries(config.listeners || {})) component.on(event, handler);
        return component;
    }
    function child(config, owner) {
        if (config.events instanceof Map) { config.ownerCt = owner; return config; }
        return create(xtypeClasses[config.xtype] || 'Ext.Component', { ...config, ownerCt: owner });
    }
    define('Ext.panel.Panel', {
        extend: 'Ext.Component',
        initComponent: function () {
            this.children = [...[].concat(this.items || []), ...(this.tbar || []), ...(this.bbar || []), ...(this.buttons || []), ...[].concat(this.dockedItems || [])]
                .filter((item) => item && typeof item === 'object').map((item) => child(item, this));
            if (this.layout === 'card') {
                this.activeCard = this.children[0];
                this.children.slice(0, [].concat(this.items || []).length).forEach((panel, index) => { panel.hidden = index !== 0; });
            }
            return 'native-panel-result';
        },
        getLayout: function () {
            const panel = this;
            return { getActiveItem() { return panel.activeCard; }, setActiveItem(card) {
                if (panel.activeCard === card) return;
                if (panel.activeCard) { panel.activeCard.fire('deactivate'); panel.activeCard.setVisible(false); }
                panel.activeCard = card; card.setVisible(true); card.fire('activate');
            } };
        },
        add: function (component) { component.ownerCt = this; this.children.push(component); if (this.rendered) component.render(); return component; },
        down: function (selector) {
            for (const item of this.children) {
                if (selector === '#' + item.itemId) return item;
                const found = item.down?.(selector); if (found) return found;
            }
            return null;
        },
    });
    define('Ext.tree.Panel', {
        extend: 'Ext.panel.Panel',
        initComponent: function () {
            const tree = this, nodes = new Map();
            function node(data, parent = null) {
                const record = { data, parentNode: parent, get(field) { return data[field]; }, getId() { return data.id; },
                    set(field, value) { if (typeof field === 'object') Object.assign(data, field); else data[field] = value; },
                    appendChild(value) { const child = node(value, this); this.childNodes.push(child); return child; },
                    remove() { if (this.parentNode) this.parentNode.childNodes = this.parentNode.childNodes.filter((row) => row !== this); nodes.delete(this.getId()); } };
                nodes.set(data.id, record); record.childNodes = (data.children || []).map((child) => node(child, record)); return record;
            }
            this.rootNode = node(this.store.root);
            this.dataStore = { getNodeById(id) { return nodes.get(id); }, getRange() { return Array.from(nodes.values()); },
                sort(sorter) { tree.rootNode.childNodes.sort(sorter.sorterFn); } };
            this.selection = { selected: [], select(record) {
                this.selected = [record]; tree.fire('selectionchange', this, this.selected);
            }, getSelection() { return this.selected; } };
            return this.callParent(arguments);
        },
        getStore: function () { return this.dataStore; }, getSelectionModel: function () { return this.selection; },
    });
    define('Ext.list.Tree', {
        extend: 'Ext.tree.Panel',
        getSelection: function () { return this.selectedNode || null; },
        setSelection: function (record) {
            if (this.selectedNode === record) return;
            this.selectedNode = record;
            this.fire('selectionchange', this, record);
        },
    });
    define('Ext.grid.Panel', {
        extend: 'Ext.panel.Panel',
        initComponent: function () {
            const grid = this;
            this.dataStore = store(this.store, this);
            this.selection = { selected: [], select(record) { this.selected = [record]; grid.fire('selectionchange', this, this.selected); }, getSelection() { return this.selected; } };
            return this.callParent(arguments);
        }, getStore: function () { return this.dataStore; }, getSelectionModel: function () { return this.selection; },
    });
    define('PVE.form.ViewSelector', {
        extend: 'Ext.panel.Panel', forceSelection: true, allowBlank: false,
        initComponent: function () {
            const selector = this; selector.removedKeys = [];
            const keys = ['server', 'folder', 'pool', 'tags', 'pve-komodo-containers'];
            if (safeline) keys.push('pve-safeline-waf');
            const record = (value) => value.get ? value : {
                ...value, get(field) { return this[field]; }, set(field, value) {
                    if (typeof field === 'object') Object.assign(this, field);
                    else this[field] = value;
                    if (selector.value === this.key) selector.rawValue = this.value;
                },
            };
            const rows = keys.map((key) => record({ key, value: key }));
            this.dataStore = {
                rows, getRange() { return rows.slice(); }, getCount() { return rows.length; },
                each(fn) { rows.forEach(fn); },
                findRecord(field, value) { return rows.find((row) => row[field] === value); },
                sort(sorter) { rows.sort(typeof sorter === 'function' ? sorter : sorter.sorterFn); },
                add(value) { rows.push(record(value)); },
                insert(index, values) { rows.splice(index, 0, ...[].concat(values).map(record)); },
                remove(values) { [].concat(values).forEach((value) => {
                    const index = rows.indexOf(value);
                    if (index < 0) return;
                    rows.splice(index, 1); selector.removedKeys.push(value.key);
                    // Ext ComboBox forceSelection discards a selection whose
                    // record is removed, even if a matching record is re-added.
                    if (selector.value === value.key) selector.setValue(null);
                }); },
            };
            this.setValue('server'); return this.callParent(arguments);
        }, getStore: function () { return this.dataStore; },
        setValue: function (value) {
            const old = this.value, selected = this.dataStore.findRecord('key', value);
            this.value = value; this.rawValue = selected ? selected.value : '';
            this.invalid = !selected && !this.allowBlank;
            if (old !== value) this.fire('change', this, value, old);
        },
        getRawValue: function () { return this.rawValue; },
        isValid: function () { return !this.invalid; },
        applyState: function (state, doSelect) {
            const record = state && this.dataStore.findRecord('key', state.value);
            if (record && this.value !== state.value) {
                this.setValue(state.value);
                if (doSelect) this.fireEvent('select', this, [record]);
            }
        },
        // Native selector's unknown view definition falls back to server groups,
        // retaining the stored key as its id (pve-manager aa7db891).
        getViewFilter: function () { return { id: this.value, groups: ['node'] }; },
    });
    define('PVE.StdWorkspace', {
        extend: 'Ext.panel.Panel', xtype: 'pveStdWorkspace',
        initComponent: function () { this.hash = '#v1:0:=node%2Fpve-a'; this.contentHistory = []; return this.callParent(arguments); },
        onLogin: function (data) { context.window.Proxmox.UserName = data ? data.username : null; return 'native-login-result'; },
        setContent: function (config) {
            const old = this.activePage;
            if (old && !old.destroyed) {
                old.fire('deactivate'); old.fire('hide'); removedLater.push(old);
            }
            const page = create('Ext.panel.Panel', { ...config, ownerCt: this });
            this.activePage = page; this.contentHistory.push(page);
            if (!deferContentRender) page.render();
        },
    });
    define('PVE.tree.ResourceTree', {
        extend: 'Ext.panel.Panel',
        initComponent: function () {
            this.nativeIds = ['root', 'node/pve-a', 'qemu/102']; this.filterCalls = [];
            this.selection = { suspended: 0, suspendEvents() { this.suspended++; }, resumeEvents() { this.suspended--; } };
            this.setViewFilter = function (view) {
                this.viewFilter = view; this.filterCalls.push(view);
                if (!this.selection.suspended) this.workspace.setContent({ nativePage: true });
                return 'native-filter-result';
            };
            return this.callParent(arguments);
        }, getSelectionModel: function () { return this.selection; },
        resourceTick: function () {
            if (!this.viewFilter?.getFilterFn || this.viewFilter.getFilterFn()({ data: { id: 'node/pve-a' } })) this.workspace.setContent({ nativePage: true });
        },
    });
    // Simulate the already-installed container view's *instance* wrapper. The
    // sites override must delegate to it rather than replacing its behavior.
    define('ExistingContainerView', { override: 'PVE.tree.ResourceTree', initComponent: function () {
        const result = this.callParent(arguments), previous = this.setViewFilter;
        this._pveKomodoContainerTree = { tree: create('Ext.panel.Panel', { itemId: 'pve-komodo-container-tree', hidden: true }) };
        this.ownerCt.add(this._pveKomodoContainerTree.tree);
        this.setViewFilter = function (view) {
            const result = previous.apply(this, arguments);
            const containerView = view?.id === 'pve-komodo-containers';
            this.setVisible(!containerView); this._pveKomodoContainerTree.tree.setVisible(containerView);
            return result;
        };
        return result;
    } });
    if (safeline) define('ExistingSafeLineView', { override: 'PVE.tree.ResourceTree', initComponent: function () {
        const result = this.callParent(arguments), previous = this.setViewFilter;
        this._pveSafeLine = { tree: create('Ext.panel.Panel', { itemId: 'pve-safeline-tree', hidden: true }) };
        this.ownerCt.add(this._pveSafeLine.tree);
        this.setViewFilter = function (view) {
            const result = previous.apply(this, arguments);
            const active = view?.id === 'pve-safeline-waf'; this._pveSafeLine.tree.setVisible(active);
            if (active) this.setVisible(false);
            return result;
        };
        return result;
    } });
    const Ext = { ClassManager: { get: (name) => classes.get(name) }, define, Msg: { YESNO: 'yesno', WARNING: 'warning' },
        create: (name, config) => create(name.startsWith('Ext.window.') ? 'Ext.panel.Panel' : name, config),
        Ajax: { request(options) {
            const request = { options };
            // Keep asynchronous icon discovery separate from business request
            // assertions. Dedicated icon tests exercise and complete this queue.
            const target = /^\/pve-plugins\/api\/sites\/[0-9a-f]{32}\/icon$/.test(options.url) ? iconRequests : requests;
            target.push(request); return request;
        }, abort(request) {
            request.aborted = true; request.options.callback(null, false, { status: 0, responseText: '' });
        } },
    };
    context = vm.createContext({ window: { Ext, URL, open(...args) { openedWindows.push(args); }, location: { protocol: 'https:', origin: 'https://pve.test' },
        Proxmox: { UserName: 'root@pam', Utils: { authOK: () => true } },
        document: { fullscreenElement: null, exits: 0, exitFullscreen() { this.exits++; this.fullscreenElement = null; return Promise.resolve(); } },
        console: { warn: (...args) => warnings.push(args) } },
        fetch() { throw new Error('site URLs must never be fetched by the extension'); } });
    function setup(initial = 'server') {
        const workspace = create('PVE.StdWorkspace');
        const owner = create('Ext.panel.Panel', { ownerCt: workspace }); owner.render();
        const native = create('PVE.tree.ResourceTree', { workspace, ownerCt: owner, viewFilter: { id: initial, groups: ['node'] } });
        owner.add(native);
        return { workspace, native, owner,
            switch(id) { return native.setViewFilter({ id, groups: ['node'] }); },
            get state() { return native._pveExtraSites; },
            get panel() { return native._pveExtraSites?.panel; },
            press(name) { native._pveExtraSites.panel.down('#pve-extra-sites-' + name).handler(); },
            get page() { return workspace.activePage; },
            get frame() { return workspace.activePage?.down('#pve-extra-sites-frame')?.getEl()?.dom; },
            get editor() { return native._pveExtraSites.editor; },
            select(id) { const record = native._pveExtraSites.panel.getStore().getById(id); assert.ok(record); native._pveExtraSites.panel.getSelectionModel().select(record); native._pveExtraSites.panel.fire('itemclick', null, record); },
        };
    }
    function respond(data, index = requests.length - 1, status = 200) { requests[index].options.callback(null, status >= 200 && status < 300, { status, responseText: typeof data === 'string' ? data : JSON.stringify(data) }); }
    return { Ext, classes, context, requests, iconRequests, dialogs, warnings, loadedFrames, openedWindows, removedLater, setup, respond,
        respondIcon(data, index = iconRequests.length - 1, status = 200) {
            iconRequests[index].responded = true;
            iconRequests[index].options.callback(null, status >= 200 && status < 300, { status, responseText: typeof data === 'string' ? data : JSON.stringify(data) });
        },
        create, load() { vm.runInContext(source, context); if (navigation) vm.runInContext(navigationSource, context); if (manager) vm.runInContext(managerSource, context); }, deferRender(value) { deferContentRender = value; },
        flushRemovals() { removedLater.splice(0).forEach((page) => page.destroy()); },
    };
}
function site(id = 'a', name = 'Grafana', url = 'https://grafana.example/d/default?orgId=1', revision = 1, metadata = {}) {
    return { id: id.repeat(32), name, url, revision, enabled: true, alias: '', icon: null, ...metadata };
}
function list(sites = [site()], user = 'root@pam') { return { user, sites, csrf_token: 'site-session-csrf' }; }
function editorButton(editor, name) { return editor.down('#pve-extra-sites-' + name); }
function pageButton(view, name) { return view.page.down('#pve-extra-sites-' + name); }
function siteInfoRows(panel) { return panel.down('#pve-extra-sites-info').getStore().getRange(); }
function siteInfoHtml(panel) {
    const grid = panel.down('#pve-extra-sites-info');
    return grid.getStore().getRange().map((record) => grid.columns[0].renderer(record.get('value'), {}, record)).join('');
}
function setEditor(editor, name, url) { editorButton(editor, 'name').setValue(name); editorButton(editor, 'url').setValue(url); }
function ready(ui = browser(), sites = [site()]) { ui.load(); const view = ui.setup(); view.switch(viewId); ui.respond(list(sites)); return { ui, view }; }
function managerReady(sites = [site()], ordering) {
    const ui = browser(); ui.load();
    if (ordering) ui.context.window.PVEPluginManagerNavigation = ordering;
    const view = ui.setup();
    const panel = ui.context.window.PVEPluginSites.createManager(view.workspace);
    view.workspace.add(panel); view.workspace.render();
    ui.respond(list(sites));
    return { ui, view, panel, state: panel._pveExtraSites,
        press(action) { panel.down('#pve-extra-sites-' + action).handler(); },
        select(id) { panel.getSelectionModel().select(panel.getStore().getById(id)); },
        get editor() { return panel._pveExtraSites.editor; } };
}


test('selector lists individual sites after native and module views, without a generic sites entry', () => {
    const ui = browser(); ui.load(); ui.load(); const view = ui.setup();
    const selector = ui.create('PVE.form.ViewSelector', { ownerCt: view.workspace }); selector.render();
    assert.equal(ui.requests[0].options.url, api);
    const second = site('b', '<Logs & team>', 'https://logs.example/'); ui.respond(list([site(), second]));
    assert.deepEqual(selector.getStore().rows.map((row) => row.key),
        ['server', 'folder', 'pool', 'tags', 'pve-komodo-containers', viewId, 'pve-extra-site-' + second.id]);
    assert.equal(selector.getStore().rows.at(-1).value, '<Logs & team>');
    assert.equal(selector.getStore().rows.some((row) => row.key === 'pve-extra-sites'), false);
    selector.applyState({ value: viewId }); assert.equal(selector.getViewFilter().id, viewId);
    assert.equal(ui.warnings.length, 0);
});

function restoringSite({ loggedIn = true } = {}) {
    const ui = browser({ navigation: true }); ui.load(); const view = ui.setup();
    if (!loggedIn) ui.context.window.Proxmox.UserName = null;
    // PVE restores the state before StdWorkspace attaches the selector, then
    // builds ResourceTree from the selector's initial fallback view filter.
    const selector = ui.create('PVE.form.ViewSelector');
    selector.applyState({ value: viewId });
    assert.equal(selector.getValue(), 'server');
    view.workspace.add(selector);
    let selections = 0;
    selector.on('select', function (combo) { selections++; view.native.setViewFilter(combo.getViewFilter()); });
    selector.render();
    return { ui, view, selector, selections: () => selections,
        pendingList() { return ui.requests.findLast((request) => request.options.url === api && !request.aborted); } };
}

test('startup restores a persisted direct site only after its authoritative menu record arrives', () => {
    const m = restoringSite();
    assert.equal(m.ui.loadedFrames.length, 0);
    const first = m.pendingList(); m.ui.respond(list(), m.ui.requests.indexOf(first));
    const record = m.selector.getStore().findRecord('key', viewId);
    assert.equal(m.selector.getValue(), viewId); assert.equal(m.selector.isValid(), true);
    assert.equal(m.view.native.viewFilter.id, viewId); assert.equal(m.selections(), 1);
    assert.notEqual(m.pendingList(), first, 'restoring uses the normal native select path and a fresh site-page read');
    m.ui.respond(list(), m.ui.requests.indexOf(m.pendingList()));
    assert.equal(m.view.frame.attributes.src, site().url); assert.equal(m.ui.loadedFrames.length, 1);
    m.ui.context.window.PVEPluginSites.refresh(m.view.workspace);
    m.ui.respond(list(), m.ui.requests.indexOf(m.pendingList()));
    assert.equal(m.selector.getStore().findRecord('key', viewId), record);
    assert.equal(m.selector.removedKeys.includes(viewId), false); assert.equal(m.selections(), 1);
    assert.equal(m.ui.loadedFrames.length, 1);
});

test('startup state can wait for the first login before restoring a permitted site', () => {
    const m = restoringSite({ loggedIn: false });
    assert.equal(m.pendingList(), undefined);
    m.view.workspace.onLogin({ username: 'root@pam' });
    m.ui.respond(list(), m.ui.requests.indexOf(m.pendingList()));
    assert.equal(m.selector.getValue(), viewId); assert.equal(m.selections(), 1);
    m.ui.respond(list(), m.ui.requests.indexOf(m.pendingList()));
    assert.equal(m.view.frame.attributes.src, site().url);
});

test('manual selection and subsequent native state changes take precedence over delayed site restoration', () => {
    for (const action of ['different-view', 'same-view', 'native-state']) {
        const m = restoringSite(), pending = m.pendingList();
        if (action === 'native-state') m.selector.applyState({ value: 'tags' }, true);
        else {
            m.selector.setValue(action === 'same-view' ? 'server' : 'tags');
            m.selector.fireEvent('select', m.selector, [m.selector.getStore().findRecord('key', m.selector.getValue())]);
        }
        const selected = m.selector.getValue();
        m.ui.respond(list(), m.ui.requests.indexOf(pending));
        assert.equal(m.selector.getValue(), selected, action);
        assert.equal(m.view.native.viewFilter.id, selected, action);
        assert.equal(m.selections(), 1); assert.equal(m.ui.loadedFrames.length, 0);
    }
});

test('logout, changed users and expired authentication discard pending restoration and stale replies', () => {
    for (const action of ['logout', 'changed-user', 'expired-auth']) {
        const m = restoringSite(), pending = m.pendingList();
        if (action === 'logout') {
            m.view.workspace.onLogin(null);
            m.view.workspace.onLogin({ username: 'other@pam' });
        } else if (action === 'changed-user') m.ui.context.window.Proxmox.UserName = 'other@pam';
        else m.ui.context.window.Proxmox.Utils.authOK = () => false;
        m.ui.respond(list(), m.ui.requests.indexOf(pending));
        assert.equal(m.selector.getValue(), 'server'); assert.equal(m.selections(), 0);
        m.ui.context.window.Proxmox.Utils.authOK = () => true;
        m.ui.context.window.PVEPluginSites.refresh(m.view.workspace);
        const user = m.ui.context.window.Proxmox.UserName;
        m.ui.respond(list([site()], user), m.ui.requests.indexOf(m.pendingList()));
        assert.equal(m.selector.getValue(), 'server', action); assert.equal(m.selections(), 0);
        assert.equal(m.ui.loadedFrames.length, 0);
    }
});

test('superseded list replies cannot restore a site and unavailable targets are consumed once', () => {
    const m = restoringSite(), first = m.pendingList();
    m.ui.context.window.PVEPluginSites.refresh(m.view.workspace); const current = m.pendingList();
    assert.equal(first.aborted, true); m.ui.respond(list(), m.ui.requests.indexOf(first));
    assert.equal(m.selector.getValue(), 'server'); assert.equal(m.selections(), 0);
    m.ui.respond(list(), m.ui.requests.indexOf(current));
    assert.equal(m.selector.getValue(), viewId); assert.equal(m.selections(), 1);
    for (const sites of [[], [site('a', 'Disabled', site().url, 1, { enabled: false })]]) {
        const unavailable = restoringSite();
        unavailable.ui.respond(list(sites), unavailable.ui.requests.indexOf(unavailable.pendingList()));
        unavailable.ui.context.window.PVEPluginSites.sync(unavailable.view.workspace, [site()]);
        assert.equal(unavailable.selector.getValue(), 'server'); assert.equal(unavailable.selections(), 0);
    }
    const failed = restoringSite();
    failed.ui.respond({}, failed.ui.requests.indexOf(failed.pendingList()), 403);
    failed.ui.context.window.PVEPluginSites.sync(failed.view.workspace, [site()]);
    assert.equal(failed.selector.getValue(), 'server'); assert.equal(failed.selections(), 0);
});

test('a valid 60-emoji site name coexists with ordinary sites through navigation and the normal site entry', () => {
    const ui = browser({ navigation: true }); ui.load(); const view = ui.setup();
    const selector = ui.create('PVE.form.ViewSelector', { ownerCt: view.workspace }); selector.render();
    const unicode = site('b', '😀'.repeat(60), 'https://unicode.example/');
    const unicodeView = 'pve-extra-site-' + unicode.id;
    assert.equal(ui.context.window.PVEPluginSites.sync(view.workspace, [site(), unicode]), true);
    selector.setValue(unicodeView);
    view.switch(unicodeView); ui.respond(list([site(), unicode]));
    assert.equal(view.state.loaded, true); assert.equal(view.frame.attributes.src, unicode.url);
    assert.equal(view.frame.attributes.title, unicode.name);
    const record = selector.getStore().findRecord('key', unicodeView);
    assert.equal(record.get('value'), unicode.name); assert.equal(selector.isValid(), true);
    assert.ok(selector.getStore().findRecord('key', viewId));
    assert.equal(ui.context.window.PVEPluginSites.sync(view.workspace, [site(), { ...unicode, name: '😀'.repeat(100) }]), true);
    assert.equal(ui.context.window.PVEPluginSites.sync(view.workspace, [site(), { ...unicode, name: '😀'.repeat(101) }]), false);
    assert.equal(selector.getStore().findRecord('key', unicodeView), record);
});

test('site lists and shared sync require enabled, alias and icon fields without filling older DTO defaults', () => {
    for (const field of ['enabled', 'alias', 'icon']) {
        const ui = browser({ navigation: true }); ui.load(); const view = ui.setup();
        const selector = ui.create('PVE.form.ViewSelector', { ownerCt: view.workspace }); selector.render();
        const api = ui.context.window.PVEPluginSites, incomplete = site('b');
        delete incomplete[field];
        assert.equal(api.sync(view.workspace, [site()]), true);
        assert.equal(api.sync(view.workspace, [site(), incomplete]), false, field);
        assert.equal(api.sync(view.workspace, [site(), { ...incomplete, [field]: undefined }]), false, field);
        assert.ok(selector.getStore().findRecord('key', viewId), 'rejected shared input cannot replace a validated list');
        view.switch(viewId); ui.respond(list([site(), incomplete]));
        assert.equal(view.state.loaded, false, field); assert.equal(view.frame, undefined);
        assert.equal(selector.getStore().findRecord('key', viewId), undefined);
    }
});

test('direct site views mount only their requested iframe and have refresh without CRUD controls', () => {
    const ui = browser(); ui.load(); const view = ui.setup(viewId);
    const second = site('b', 'Logs', 'https://logs.example/'); ui.respond(list([second, site()]));
    assert.equal(view.page.layout, 'fit'); assert.equal(view.frame.attributes.src, site().url);
    assert.equal(view.frame.attributes.title, site().name);
    assert.equal(view.frame.attributes.referrerpolicy, 'no-referrer');
    assert.equal(view.frame.attributes.allow, 'fullscreen');
    assert.match(view.frame.attributes.sandbox, /allow-scripts.*allow-forms.*allow-same-origin.*allow-popups.*allow-downloads/);
    assert.doesNotMatch(view.frame.attributes.sandbox, /allow-top-navigation|allow-popups-to-escape-sandbox/);
    assert.equal(pageButton(view, 'refresh').disabled, false);
    assert.equal(pageButton(view, 'page-edit'), null); assert.equal(pageButton(view, 'remove'), null);
    assert.equal(ui.loadedFrames.length, 1);
    const hash = view.workspace.hash, page = view.page;
    view.native.resourceTick(); assert.equal(view.page, page);
    const frame = view.frame;
    view.switch('pve-extra-site-' + second.id); ui.respond(list([site(), second]));
    assert.equal(frame.attributes.src, 'about:blank'); assert.equal(view.frame.attributes.src, second.url);
    assert.equal(view.workspace.hash, hash); assert.deepEqual(view.native.nativeIds, ['root', 'node/pve-a', 'qemu/102']);
    const refreshed = view.frame, count = ui.requests.length; pageButton(view, 'refresh').handler();
    assert.equal(refreshed.attributes.src, 'about:blank'); assert.equal(view.frame.attributes.src, second.url);
    assert.equal(ui.requests.length, count, 'refresh loads iframe without backend operations');
});

test('site, container and native filters compose and leave no hidden iframe running', () => {
    const { ui, view } = ready(); const frame = view.frame;
    assert.equal(view.native.hidden, true); assert.equal(view.native._pveKomodoContainerTree.tree.hidden, true);
    assert.equal(view.switch('pve-komodo-containers'), 'native-filter-result');
    assert.equal(frame.attributes.src, 'about:blank'); assert.equal(view.native.hidden, true);
    assert.equal(view.native._pveKomodoContainerTree.tree.hidden, false);
    view.switch('server'); assert.equal(view.native.hidden, false); assert.equal(view.page.nativePage, true);
    view.switch(viewId); ui.respond(list()); assert.equal(view.frame.attributes.src, site().url);
});

test('manager displays editable rows without embedding sites and creates with current session CSRF', () => {
    const m = managerReady([]); m.press('new'); const editor = m.editor;
    assert.equal(m.ui.loadedFrames.length, 0); assert.equal(editorButton(editor, 'delete'), null);
    for (const url of ['javascript:alert(1)', 'data:text/html,a', 'ftp://example/', 'https://user:pass@example/', 'https://example/\\evil']) {
        setEditor(editor, 'Site', url); editorButton(editor, 'save').handler();
    }
    assert.equal(m.ui.requests.length, 1);
    setEditor(editor, '  Logs  ', 'https://logs.example/query?x=1#tail'); editorButton(editor, 'save').handler();
    const request = m.ui.requests.at(-1).options;
    assert.equal(request.method, 'POST'); assert.equal(request.url, api);
    assert.deepEqual(JSON.parse(JSON.stringify(request.jsonData)), { name: 'Logs', url: 'https://logs.example/query?x=1#tail' });
    assert.equal(request.headers['X-PVE-Plugins-CSRF'], 'site-session-csrf');
    editor.close(); assert.equal(editor.destroyed, false);
    editorButton(editor, 'save').handler(); assert.equal(m.ui.requests.length, 2);
    const saved = site('b', 'Logs', request.jsonData.url); m.ui.respond({ site: saved }); m.ui.respond(list([saved]));
    assert.equal(editor.destroyed, true); assert.equal(m.panel.getStore().rows[0].data.name, 'Logs');
    assert.equal(m.ui.loadedFrames.length, 0);
});

test('manager edits with revision, confirms deletion once, and updates selector after every mutation', () => {
    const m = managerReady();
    const selector = m.ui.create('PVE.form.ViewSelector', { ownerCt: m.view.workspace }); selector.render(); m.ui.respond(list());
    m.select(site().id); m.press('edit'); const editor = m.editor;
    setEditor(editor, 'Updated', 'https://updated.example/'); editorButton(editor, 'save').handler();
    const request = m.ui.requests.at(-1).options;
    assert.equal(request.method, 'PATCH'); assert.equal(request.jsonData.expected_revision, 1);
    const updated = site('a', 'Updated', 'https://updated.example/', 2);
    m.ui.respond({ site: updated }); m.ui.respond(list([updated]));
    assert.equal(selector.getStore().rows.at(-1).value, 'Updated');
    m.select(updated.id); m.press('edit'); const deletionEditor = m.editor;
    editorButton(deletionEditor, 'delete').handler(); const cancelled = m.ui.dialogs.at(-1);
    cancelled.options.fn('no'); assert.equal(m.ui.requests.filter((r) => r.options.method === 'DELETE').length, 0);
    editorButton(deletionEditor, 'delete').handler(); const confirmation = m.ui.dialogs.at(-1);
    confirmation.options.fn('yes'); confirmation.options.fn('yes');
    const deletion = m.ui.requests.at(-1).options;
    assert.equal(deletion.method, 'DELETE'); assert.equal(deletion.url, api + '/' + updated.id);
    assert.deepEqual(JSON.parse(JSON.stringify(deletion.jsonData)), { expected_revision: 2 });
    assert.equal(m.ui.requests.filter((r) => r.options.method === 'DELETE').length, 1);
    m.ui.respond({ deleted: true, id: updated.id }); m.ui.respond(list([]));
    assert.equal(selector.getStore().rows.some((r) => r.key === viewId), false);
    assert.equal(m.ui.loadedFrames.length, 0);
});

test('manager conflicts retain drafts and lock retries until a new authoritative read', () => {
    const m = managerReady(); m.select(site().id); m.press('edit'); const editor = m.editor;
    setEditor(editor, 'Draft', 'https://draft.example/'); editorButton(editor, 'save').handler();
    m.ui.respond({ detail: 'site_revision_conflict' }, m.ui.requests.length - 1, 409);
    assert.equal(editorButton(editor, 'name').getValue(), 'Draft');
    assert.match(editorButton(editor, 'editor-status').html, /输入已保留/);
    assert.equal(editorButton(editor, 'save').disabled, true);
    editorButton(editor, 'save').handler(); assert.equal(m.ui.requests.length, 2);
    editor.close(); assert.equal(m.ui.requests.at(-1).options.method, 'GET');
    m.ui.respond(list([site('a', 'Other admin', 'https://new.example/', 2)]));
    m.select(site().id); m.press('edit'); assert.equal(editorButton(m.editor, 'name').getValue(), 'Other admin');
});

test('manager unknown write outcome never replays or displays backend error content', () => {
    const m = managerReady([]); m.press('new'); const editor = m.editor;
    setEditor(editor, 'New', 'https://new.example/'); editorButton(editor, 'save').handler();
    m.ui.respond('PRIVATE <script>failed</script>', m.ui.requests.length - 1, 502);
    assert.match(editorButton(editor, 'editor-status').html, /结果可能已保存/);
    assert.doesNotMatch(editorButton(editor, 'editor-status').html, /PRIVATE|<script>/);
    editorButton(editor, 'save').handler(); assert.equal(m.ui.requests.filter((r) => r.options.method === 'POST').length, 1);
});

test('authorization failures and malformed lists never enable CRUD or create frames', () => {
    for (const [code, payload] of [[401, {}], [403, {}], [200, list([site()], 'other@pam')], [200, list([site('a', 'Bad', 'javascript:alert(1)')])]]) {
        const ui = browser(); ui.load(); const view = ui.setup(viewId); ui.respond(payload, 0, code);
        assert.equal(view.frame, undefined); assert.equal(ui.loadedFrames.length, 0);
        assert.equal(view.panel.down('#pve-extra-sites-new'), null);
        assert.equal(view.panel.down('#pve-extra-sites-open').disabled, true);
        assert.equal(siteInfoRows(view.panel).length, 0);
    }
    const m = managerReady(); m.select(site().id); m.press('edit'); const editor = m.editor;
    setEditor(editor, 'Updated', 'https://updated.example/'); editorButton(editor, 'save').handler();
    m.ui.respond({ detail: 'invalid_csrf_token' }, m.ui.requests.length - 1, 403);
    assert.equal(editor.destroyed, true); assert.equal(m.state.csrf, null); assert.equal(m.panel.getStore().rows.length, 0);
});

test('logout clears selector entries, aborts reads and writes, closes confirmations and rejects late results', () => {
    const { ui, view } = ready(); const frame = view.frame;
    const selector = ui.create('PVE.form.ViewSelector', { ownerCt: view.workspace }); selector.render(); ui.respond(list());
    view.workspace.onLogin(null);
    assert.equal(frame.attributes.src, 'about:blank'); assert.equal(selector.getStore().rows.some((r) => r.key === viewId), false);
    view.workspace.onLogin({ username: 'root@pam' });
    const pending = ui.requests.at(-1); view.workspace.onLogin(null); assert.equal(pending.aborted, true);
    ui.respond(list()); assert.equal(view.frame, undefined);
    const m = managerReady(); m.select(site().id); m.press('edit');
    editorButton(m.editor, 'delete').handler(); const confirmation = m.ui.dialogs.at(-1);
    m.view.workspace.onLogin(null); assert.equal(confirmation.destroyed, true);
    confirmation.options.fn('yes'); assert.equal(m.ui.requests.length, 1);
    const w = managerReady([]); w.press('new'); const editor = w.editor;
    setEditor(editor, 'New', 'https://new.example/'); editorButton(editor, 'save').handler(); const write = w.ui.requests.at(-1);
    w.panel.fire('deactivate'); assert.equal(write.aborted, true); assert.equal(editor.destroyed, true);
    w.ui.respond({ site: site('b') }); assert.equal(w.state.sites.length, 0);
});

test('hidden cards, destruction and stale users cannot remount site frames or refresh content', () => {
    const { ui, view } = ready(); const oldPage = view.page, frame = view.frame;
    const refresh = pageButton(view, 'refresh'); ui.context.window.document.fullscreenElement = frame;
    view.workspace.setContent({ nativePage: true });
    assert.equal(frame.attributes.src, 'about:blank'); assert.equal(ui.context.window.document.exits, 1);
    refresh.handler(); assert.equal(ui.loadedFrames.length, 1);
    ui.flushRemovals(); assert.equal(oldPage.destroyed, true);
    const late = browser(); late.load(); const pending = late.setup(viewId);
    late.deferRender(true); late.respond(list()); const deferred = pending.page;
    pending.switch('server'); deferred.render(); assert.equal(deferred.destroyed, true); assert.equal(late.loadedFrames.length, 0);
    const stale = browser(); stale.load(); const changed = stale.setup(viewId);
    stale.context.window.Proxmox.UserName = 'other@pam'; stale.respond(list());
    assert.equal(stale.loadedFrames.length, 0); assert.equal(changed.state.csrf, null);
});

function moduleConfig() {
    return { revision: 1, csrf_token: 'manager-session-csrf', enabled_plugins: ['komodo'],
        navigation: { module_aliases: {}, module_icons: {}, entry_order: ['komodo', 'temperatures', 'safeline'] },
        modules: { komodo: { url: 'https://komodo.example', tls_verify: true, api_key_configured: true, api_secret_configured: true },
            temperatures: { nodes: {} } } };
}

test('real unified manager persists site metadata through the shared helpers and opens enabled sites explicitly', () => {
    const ui = browser({ manager: true, navigation: true, safeline: true }); ui.load(); const view = ui.setup();
    const selector = ui.create('PVE.form.ViewSelector', { ownerCt: view.workspace }); selector.render();
    const disabled = site('b', 'Disabled', 'https://disabled.example/', 1, { enabled: false });
    ui.respond(list([site(), disabled]), ui.requests.findIndex((request) => request.options.url === api));
    ui.respond({ revision: 1, navigation: moduleConfig().navigation }, ui.requests.findIndex((request) => request.options.url.endsWith('/navigation')));
    view.switch('pve-plugin-manager'); ui.respond(moduleConfig()); ui.respond(list([site(), disabled]));
    const page = view.page, grid = page.down('#plugin-entry-grid'), tree = view.owner.down('#pve-plugin-manager-tree');
    assert.ok(grid); assert.equal(page.down('#plugin-page-sites'), null); assert.equal(tree.getStore().getNodeById('sites'), undefined);
    const type = grid.columns.find((column) => column.dataIndex === 'kind');
    assert.equal(type.renderer('site').replace(/<[^>]*>/g, ''), '附加站点');
    assert.equal(type.renderer('module').replace(/<[^>]*>/g, ''), '功能模块');
    assert.equal(grid.getStore().getById('komodo').get('name'), 'Komodo');
    tree.getSelectionModel().select(tree.getStore().getNodeById(viewId));
    const record = grid.getStore().getById(viewId);
    assert.equal(grid.getSelectionModel().getSelection()[0], record); assert.equal(ui.loadedFrames.length, 0, 'a tree site row locates the administrator table');
    grid.down('#plugin-entry-edit').handler(); const editor = ui.dialogs.at(-1);
    editor.down('#entry-enabled').setValue(false); editor.down('#entry-alias').setValue('Dashboard alias');
    editor.down('#entry-icon-type').setValue('pve'); editor.down('#entry-icon-native').setValue('fa fa-server');
    editor.down('#entry-save').handler(); const write = ui.requests.at(-1).options;
    assert.equal(write.method, 'PATCH'); assert.equal(write.url, api + '/' + site().id);
    assert.deepEqual(JSON.parse(JSON.stringify(write.jsonData)), { name: site().name, url: site().url, enabled: false,
        alias: 'Dashboard alias', icon: { type: 'pve', value: 'fa fa-server' }, expected_revision: 1 });
    const changed = { ...site(), enabled: false, alias: 'Dashboard alias', icon: { type: 'pve', value: 'fa fa-server' }, revision: 2 };
    ui.respond({ site: changed });
    assert.equal(editor.destroyed, true); assert.equal(grid.getStore().getById(viewId), record);
    assert.equal(record.get('enabled'), false); assert.equal(record.get('alias'), 'Dashboard alias');
    assert.equal(selector.getStore().findRecord('key', viewId), undefined); assert.equal(grid.down('#plugin-entry-open').disabled, true);
    grid.down('#plugin-entry-edit').handler(); const enableEditor = ui.dialogs.at(-1);
    enableEditor.down('#entry-enabled').setValue(true); enableEditor.down('#entry-save').handler();
    const enabled = { ...changed, enabled: true, revision: 3 }; ui.respond({ site: enabled });
    assert.equal(selector.getStore().findRecord('key', viewId).get('value'), 'Dashboard alias');
    assert.equal(selector.getStore().findRecord('key', viewId).get('pluginIcon').value, 'fa fa-server');
    grid.down('#plugin-entry-open').handler(); assert.equal(page.destroyed, true); ui.respond(list([enabled, disabled]));
    assert.equal(view.frame.attributes.src, enabled.url); assert.equal(view.frame.attributes.title, enabled.alias);
    assert.equal(view.panel.hidden, false); assert.equal(tree.hidden, true); assert.equal(ui.warnings.length, 0);
});

test('real manager subscribes to asynchronous site icons and cleans its modal while switching operational views', () => {
    const ui = browser({ manager: true, navigation: true, safeline: true }); ui.load(); const view = ui.setup(viewId); ui.respond(list());
    const selector = ui.create('PVE.form.ViewSelector', { ownerCt: view.workspace });
    const oldFrame = view.frame; view.switch('pve-plugin-manager'); ui.respond(moduleConfig()); ui.respond(list());
    const page = view.page, grid = page.down('#plugin-entry-grid'), tree = view.owner.down('#pve-plugin-manager-tree');
    assert.equal(oldFrame.attributes.src, 'about:blank'); assert.equal(view.panel.hidden, true);
    const record = grid.getStore().getById(viewId);
    grid.getSelectionModel().select(record); grid.down('#plugin-entry-edit').handler(); const editor = ui.dialogs.at(-1);
    editor.down('#entry-alias').setValue('Unsaved alias'); const requests = ui.requests.length;
    ui.respondIcon(iconResult());
    assert.equal(grid.getStore().getById(viewId), record); assert.equal(record.get('pluginIcon').type, 'url');
    assert.equal(editor.down('#entry-alias').getValue(), 'Unsaved alias'); assert.equal(ui.requests.length, requests);
    view.switch('pve-safeline-waf'); assert.equal(editor.destroyed, true); assert.equal(page.destroyed, true);
    assert.equal(tree.hidden, true); assert.equal(view.native._pveSafeLine.tree.hidden, false); assert.equal(view.panel.hidden, true);
    view.switch(viewId); ui.respond(list()); assert.equal(view.frame.attributes.src, site().url); assert.equal(view.panel.hidden, false);
    const secondFrame = view.frame; view.switch('pve-komodo-containers');
    assert.equal(secondFrame.attributes.src, 'about:blank'); assert.equal(view.panel.hidden, true);
    assert.equal(view.native._pveKomodoContainerTree.tree.hidden, false); assert.equal(ui.warnings.length, 0);
});

test('direct site information uses sixteen native grid rows with separate gray labels and white values', () => {
    const longUrl = 'https://grafana.example:9443/dashboard/' + 'very-long-'.repeat(120) + '?filter=%22&label=<team>';
    const entry = site('a', '<Grafana & team>', longUrl);
    const { ui, view } = ready(browser(), [entry]); const panel = view.panel;
    assert.equal(panel.ownerCt, view.owner); assert.ok(view.owner.children.includes(panel));
    assert.equal(panel.hidden, false); assert.equal(panel.flex, 1); assert.match(panel.title, /站点信息/);
    assert.equal(panel.layout, 'fit');
    const grid = panel.down('#pve-extra-sites-info'), rows = siteInfoRows(panel);
    assert.equal(grid.xtype, 'grid'); assert.equal(grid.columns.length, 1);
    assert.equal(grid.columns[0].dataIndex, 'value'); assert.equal(grid.columns[0].flex, 1);
    assert.equal(grid.columns[0].cellWrap, true);
    assert.equal(grid.hideHeaders, true); assert.equal(grid.disableSelection, true);
    assert.equal(grid.columnLines, false); assert.equal(grid.rowLines, true);
    assert.equal(grid.viewConfig.stripeRows, false); assert.equal(grid.viewConfig.trackOver, false);
    assert.equal(grid.viewConfig.enableTextSelection, true); assert.equal(grid.viewConfig.markDirty, false);
    const labels = ['名称', '原始名称', '完整地址', '主机', '协议', '打开方式', '嵌入提示', '管理位置'];
    assert.equal(rows.length, 16);
    assert.deepEqual(Array.from(rows, (record) => record.get('label')), labels.flatMap((label) => [label, label]));
    assert.deepEqual(Array.from(rows, (record) => record.get('isLabel')), labels.flatMap(() => [true, false]));
    assert.deepEqual(Array.from(rows, (record) => grid.viewConfig.getRowClass(record)), labels.flatMap(() => ['x-grid-item-alt', '']));
    labels.forEach((label, index) => {
        const labelRow = rows[index * 2], valueRow = rows[index * 2 + 1];
        assert.equal(labelRow.get('value'), label, 'field names occupy their own rows');
        assert.equal(grid.viewConfig.getRowClass(labelRow, 1), 'x-grid-item-alt', 'label backgrounds depend on isLabel, including odd indices');
        assert.equal(grid.viewConfig.getRowClass(valueRow, 0), '', 'value rows keep the native white background at any index');
    });
    const fieldValue = (label) => rows.find((record) => record.get('label') === label && !record.get('isLabel')).get('value');
    assert.equal(fieldValue('完整地址'), longUrl); assert.equal(fieldValue('名称'), entry.name);
    assert.equal(fieldValue('原始名称'), entry.name); assert.equal(fieldValue('主机'), 'grafana.example:9443');
    assert.equal(fieldValue('协议'), 'HTTPS'); assert.equal(fieldValue('打开方式'), '嵌入页面');
    assert.equal(fieldValue('管理位置'), '--插件-- → 模块与站点');
    const render = (record) => grid.columns[0].renderer(record.get('value'), {}, record);
    assert.match(render(rows[0]), /名称/); assert.doesNotMatch(render(rows[0]), /Grafana/);
    assert.match(render(rows[1]), /&lt;Grafana &amp; team&gt;/); assert.doesNotMatch(render(rows[1]), /名称/);
    const labelHtml = grid.columns[0].renderer('<label & key>', {}, { get: (field) => field === 'isLabel' });
    const valueHtml = grid.columns[0].renderer('<value & "quoted">', {}, { get: () => false });
    assert.match(labelHtml, /&lt;label &amp; key&gt;/); assert.match(valueHtml, /&lt;value &amp; &quot;quoted&quot;&gt;/);
    assert.doesNotMatch(labelHtml + valueHtml, /<label|<value|background(?:-color)?\s*:/);
    assert.match(valueHtml, /overflow-wrap:\s*anywhere/); assert.match(valueHtml, /word-break:\s*break-word/);
    assert.match(valueHtml, /white-space:\s*normal/);
    const html = siteInfoHtml(panel);
    assert.match(html, /&lt;Grafana &amp; team&gt;/); assert.match(html, /&amp;label=&lt;team&gt;/);
    assert.doesNotMatch(html, /<Grafana|<team>/); assert.match(html, /grafana.example:9443/);
    assert.match(html, /完整地址/); assert.match(html, /overflow-wrap:\s*anywhere/);
    assert.ok(html.includes(longUrl.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')), 'long URLs are rendered completely without truncating their values');
    assert.match(html, /协议/); assert.match(html, /HTTPS/); assert.match(html, /嵌入页面/);
    assert.match(html, /禁止嵌入/); assert.match(html, /--插件-- → 模块与站点/);
    panel.down('#pve-extra-sites-open').handler();
    assert.deepEqual(ui.openedWindows[0], [entry.url, '_blank', 'noopener,noreferrer']);
    const frame = view.frame, count = ui.requests.length;
    panel.down('#pve-extra-sites-sidebar-refresh').handler();
    assert.equal(frame.attributes.src, 'about:blank'); assert.equal(view.frame.attributes.src, entry.url);
    assert.equal(ui.requests.length, count, 'sidebar actions do not probe sites or request health checks');
    const shown = view.page; view.switch(viewId); assert.equal(view.page, shown, 'selecting the same site keeps its iframe');
    assert.equal(ui.loadedFrames.length, 2);
    view.switch('server'); assert.equal(panel.hidden, true); assert.equal(siteInfoRows(panel).length, 0);
    assert.equal(panel.down('#pve-extra-sites-open').disabled, true);
    view.native.destroy(); assert.equal(panel.destroyed, true);
});

test('site sidebar clears on deletion, authorization failure and logout and stale actions cannot open a URL', () => {
    const { ui, view } = ready(); const panel = view.panel, open = panel.down('#pve-extra-sites-open');
    ui.context.window.Proxmox.UserName = 'other@pam'; open.handler(); assert.equal(ui.openedWindows.length, 0);
    ui.context.window.Proxmox.UserName = 'root@pam'; view.workspace.onLogin(null);
    assert.equal(panel.hidden, true); assert.equal(siteInfoRows(panel).length, 0);
    open.handler(); assert.equal(ui.openedWindows.length, 0);
    view.workspace.onLogin({ username: 'root@pam' }); ui.respond(list([]));
    assert.match(view.page.html, /已被删除/); assert.equal(siteInfoRows(panel).length, 0);
    assert.equal(open.disabled, true); open.handler(); assert.equal(ui.openedWindows.length, 0);
    view.switch('server'); view.switch(viewId); ui.respond({ detail: 'no_access' }, ui.requests.length - 1, 403);
    assert.equal(siteInfoRows(panel).length, 0); assert.equal(open.disabled, true);
    assert.equal(ui.loadedFrames.length, 1);
});


test('authoritative site lists preserve the selected combo record and display when a site opens or is renamed', () => {
    const ui = browser(); ui.load(); const view = ui.setup();
    const selector = ui.create('PVE.form.ViewSelector', { ownerCt: view.workspace }); selector.render(); ui.respond(list());
    const original = selector.getStore().findRecord('key', viewId);
    let selections = 0;
    selector.on('select', function (combo) { selections++; view.native.setViewFilter(combo.getViewFilter()); });
    selector.setValue(viewId); selector.fire('select', selector, [original]);
    assert.equal(selector.getRawValue(), site().name); assert.equal(selector.isValid(), true);
    ui.respond(list());
    assert.equal(selector.getValue(), viewId, 'opening the site must keep its selected combo value');
    assert.equal(selector.getRawValue(), site().name); assert.equal(selector.isValid(), true);
    assert.equal(selector.getStore().findRecord('key', viewId), original, 'existing records retain their identity');
    assert.equal(selector.removedKeys.includes(viewId), false, 'the selected record is never removed while still configured');
    const page = view.page, frame = view.frame, iframeCount = ui.loadedFrames.length;
    const renamed = site('a', '<Grafana & renamed>', site().url, 2);
    ui.context.window.PVEPluginSites.refresh(view.workspace); ui.respond(list([renamed]));
    assert.equal(selector.getStore().findRecord('key', viewId), original);
    assert.equal(selector.getValue(), viewId); assert.equal(selector.getRawValue(), '<Grafana & renamed>');
    assert.equal(selector.isValid(), true); assert.equal(selector.removedKeys.includes(viewId), false);
    assert.equal(selections, 1, 'updating list labels must not fire another select event');
    assert.equal(view.page, page); assert.equal(view.frame, frame); assert.equal(ui.loadedFrames.length, iframeCount);
});

test('site names and aliases stay literal in the selected input and are escaped only in the shared dropdown', () => {
    const ui = browser({ navigation: true }); ui.load(); const view = ui.setup();
    const selector = ui.create('PVE.form.ViewSelector', { ownerCt: view.workspace });
    const sites = ui.context.window.PVEPluginSites;
    const name = '<img src=x onerror="alert(1)"> & \'监控\'';
    const entry = site('a', name, site().url, 1, { icon: { type: 'pve', value: 'fa fa-server' } });
    assert.equal(sites.sync(view.workspace, [entry]), true);
    const record = selector.getStore().findRecord('key', viewId);
    selector.setValue(viewId);
    const htmlEncode = (value) => value.replace(/[&<>"']/g, (character) =>
        ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character]));
    function dropdown() {
        const values = { key: record.get('key'), value: record.get('value'), pluginIcon: record.get('pluginIcon') };
        return selector.listConfig.getInnerTpl().replace(/\{\[([\s\S]*?)\]\}/g, (_, expression) =>
            vm.runInNewContext(expression, { values, window: ui.context.window, Ext: { String: { htmlEncode } } }));
    }
    assert.equal(record.get('value'), name); assert.equal(selector.getRawValue(), name);
    assert.ok(dropdown().endsWith('&lt;img src=x onerror=&quot;alert(1)&quot;&gt; &amp; &#39;监控&#39;'));
    assert.doesNotMatch(dropdown(), /<img|<svg/);

    const alias = '<svg/onload=alert(1)> &lt;literal&gt;';
    assert.equal(sites.sync(view.workspace, [{ ...entry, alias, revision: 2 }]), true);
    assert.equal(selector.getStore().findRecord('key', viewId), record);
    assert.equal(selector.getRawValue(), alias); assert.equal(selector.getValue(), viewId); assert.equal(selector.isValid(), true);
    assert.ok(dropdown().endsWith('&lt;svg/onload=alert(1)&gt; &amp;lt;literal&amp;gt;'));
    assert.doesNotMatch(dropdown(), /<img|<svg/);

    assert.equal(sites.sync(view.workspace, [{ ...entry, revision: 3 }]), true);
    assert.equal(selector.getRawValue(), name); assert.equal(selector.getStore().findRecord('key', viewId), record);
    assert.equal(selector.removedKeys.includes(viewId), false);
    assert.match(dropdown(), /fa fa-server/);
});

test('reconciling site choices removes only deleted records and leaves unrelated current selections intact', () => {
    const ui = browser(); ui.load(); const view = ui.setup();
    const selector = ui.create('PVE.form.ViewSelector', { ownerCt: view.workspace }); selector.render();
    const second = site('b', 'Logs', 'https://logs.example/'); ui.respond(list([site(), second]));
    selector.setValue('pve-komodo-containers'); const nativeRecord = selector.getStore().findRecord('key', 'pve-komodo-containers');
    ui.context.window.PVEPluginSites.refresh(view.workspace); ui.respond(list([site()]));
    assert.equal(selector.getValue(), 'pve-komodo-containers'); assert.equal(selector.isValid(), true);
    assert.equal(selector.getStore().findRecord('key', 'pve-komodo-containers'), nativeRecord);
    assert.deepEqual(selector.removedKeys, ['pve-extra-site-' + second.id]);
    selector.setValue(viewId);
    ui.context.window.PVEPluginSites.refresh(view.workspace); ui.respond(list([]));
    assert.equal(selector.getStore().findRecord('key', viewId), undefined);
    assert.equal(selector.getValue(), null); assert.equal(selector.getRawValue(), ''); assert.equal(selector.isValid(), false);
    assert.equal(ui.loadedFrames.length, 0, 'choice reconciliation alone does not navigate or mount an iframe');
});


test('site reconciliation reapplies shared view ordering without removing the selected record or navigating', () => {
    const ui = browser(); ui.load(); const view = ui.setup();
    let applied = 0;
    const order = ['server', 'folder', 'pool', 'tags', 'pve-extra-site-' + 'b'.repeat(32), viewId, 'pve-komodo-containers'];
    ui.context.window.PVEPluginManagerNavigation = { applySelector(selector) {
        applied++;
        selector.getStore().sort({ sorterFn: (left, right) => order.indexOf(left.get('key')) - order.indexOf(right.get('key')) });
    }, sortSites(sites) { return sites.slice().sort((left, right) => right.id.localeCompare(left.id)); } };
    const selector = ui.create('PVE.form.ViewSelector', { ownerCt: view.workspace }); selector.render();
    const second = site('b', 'Logs', 'https://logs.example/'); ui.respond(list([site(), second]));
    assert.ok(applied >= 1); assert.deepEqual(selector.getStore().rows.map((row) => row.key), order);
    const selected = selector.getStore().findRecord('key', viewId);
    let selectionEvents = 0;
    selector.on('select', function (combo) { selectionEvents++; view.native.setViewFilter(combo.getViewFilter()); });
    selector.setValue(viewId); selector.fire('select', selector, [selected]); ui.respond(list([site(), second]));
    const initialApplied = applied, page = view.page, frame = view.frame, frameCount = ui.loadedFrames.length;
    ui.context.window.PVEPluginSites.applyOrder(view.workspace);
    assert.deepEqual(Array.from(view.state.sites, (entry) => entry.id), [second.id, site().id]);
    assert.equal(view.page, page); assert.equal(view.frame, frame); assert.equal(ui.loadedFrames.length, frameCount);
    ui.context.window.PVEPluginSites.refresh(view.workspace);
    ui.respond(list([site('a', 'Renamed', site().url, 2), second]));
    assert.equal(applied, initialApplied + 1); assert.equal(selector.getStore().findRecord('key', viewId), selected);
    assert.equal(selector.getValue(), viewId); assert.equal(selector.getRawValue(), 'Renamed'); assert.equal(selector.isValid(), true);
    assert.equal(selector.removedKeys.includes(viewId), false); assert.equal(ui.loadedFrames.length, frameCount);
    assert.equal(selectionEvents, 1); assert.equal(view.page, page); assert.equal(view.frame, frame);
    ui.context.window.PVEPluginSites.refresh(view.workspace); ui.respond(list([site()]));
    assert.equal(applied, initialApplied + 2); assert.deepEqual(selector.removedKeys, ['pve-extra-site-' + second.id]);
    assert.equal(selector.getValue(), viewId); assert.equal(selector.isValid(), true);
});

test('site manager uses shared site ordering and retains the authoritative DTO and selected edit target', () => {
    const first = site(), second = site('b', 'Logs', 'https://logs.example/');
    let sorts = 0, descending = true;
    const m = managerReady([first, second], { sortSites(sites) {
        sorts++;
        return sites.slice().sort((left, right) => descending ? right.id.localeCompare(left.id) : left.id.localeCompare(right.id));
    } });
    assert.equal(sorts, 1); assert.deepEqual(Array.from(m.panel.getStore().rows, (row) => row.data.id), [second.id, first.id]);
    assert.deepEqual(Array.from(m.state.sites, (entry) => entry.id), [first.id, second.id], 'sorting the grid does not mutate source DTOs');
    m.select(first.id); m.press('edit'); const editor = m.editor;
    const selectedRecord = m.panel.getSelectionModel().getSelection()[0];
    const requests = m.ui.requests.length;
    setEditor(editor, 'Renamed first', first.url);
    descending = false; m.ui.context.window.PVEPluginSites.applyOrder(m.view.workspace);
    assert.deepEqual(Array.from(m.panel.getStore().rows, (row) => row.data.id), [first.id, second.id]);
    assert.equal(m.panel.getSelectionModel().getSelection()[0], selectedRecord, 'preference updates preserve the selected grid Model');
    assert.equal(m.editor, editor); assert.equal(editorButton(editor, 'name').getValue(), 'Renamed first');
    assert.equal(m.ui.requests.length, requests); assert.equal(m.ui.loadedFrames.length, 0);
    descending = true; m.ui.context.window.PVEPluginSites.applyOrder(m.view.workspace);
    editorButton(editor, 'save').handler();
    assert.equal(m.ui.requests.at(-1).options.url, api + '/' + first.id);
    const renamed = { ...first, name: 'Renamed first', revision: 2 };
    m.ui.respond({ site: renamed }); m.ui.respond(list([renamed, second]));
    assert.equal(sorts, 4); assert.deepEqual(Array.from(m.panel.getStore().rows, (row) => row.data.id), [second.id, first.id]);
    assert.equal(m.panel.getSelectionModel().getSelection()[0].data.id, first.id);
    assert.equal(m.state.selected, first.id); assert.equal(m.ui.loadedFrames.length, 0);
});


test('real shared navigation preferences arriving after site data reorder choices and a live grid without losing drafts or frames', () => {
    const ui = browser({ navigation: true }); ui.load(); const view = ui.setup();
    const selector = ui.create('PVE.form.ViewSelector', { ownerCt: view.workspace }); selector.render();
    const second = site('b', 'Logs', 'https://logs.example/');
    const initialList = ui.requests.findIndex((request) => request.options.url === api);
    const navigationRead = ui.requests.findIndex((request) => request.options.url === '/pve-plugins/api/navigation');
    assert.ok(initialList >= 0); assert.ok(navigationRead >= 0); ui.respond(list([site(), second]), initialList);
    const selectedRecord = selector.getStore().findRecord('key', viewId);
    let selectionEvents = 0;
    selector.on('select', function (combo) { selectionEvents++; view.native.setViewFilter(combo.getViewFilter()); });
    selector.setValue(viewId); selector.fire('select', selector, [selectedRecord]); ui.respond(list([site(), second]));
    const page = view.page, frame = view.frame, frameCount = ui.loadedFrames.length;
    const navigation = { module_icons: {}, module_aliases: { komodo: '<集群 & 容器>' },
        entry_order: ['pve-extra-site-' + second.id, 'komodo', viewId, 'safeline', 'temperatures'] };
    ui.respond({ revision: 2, navigation }, navigationRead);
    const komodoChoice = selector.getStore().findRecord('key', 'pve-komodo-containers');
    assert.equal(komodoChoice.get('value'), '<集群 & 容器>');
    assert.match(siteInfoHtml(view.panel), /--插件-- → 模块与站点/);
    assert.doesNotMatch(siteInfoHtml(view.panel), /<工具/);
    assert.equal(selector.getStore().findRecord('key', viewId).get('value'), site().name);
    assert.deepEqual(selector.getStore().rows.map((row) => row.key),
        ['server', 'folder', 'pool', 'tags', 'pve-extra-site-' + second.id, 'pve-komodo-containers', viewId]);
    assert.equal(selector.getStore().findRecord('key', viewId), selectedRecord);
    assert.equal(selector.getValue(), viewId); assert.equal(selector.isValid(), true); assert.equal(selectionEvents, 1);
    assert.equal(view.page, page); assert.equal(view.frame, frame); assert.equal(ui.loadedFrames.length, frameCount);
    const grid = ui.context.window.PVEPluginSites.createManager(view.workspace); view.workspace.add(grid); view.workspace.render();
    ui.respond(list([site(), second]));
    assert.deepEqual(Array.from(grid.getStore().rows, (row) => row.data.id), [second.id, site().id]);
    const gridRecord = grid.getStore().getById(site().id); grid.getSelectionModel().select(gridRecord);
    grid.down('#pve-extra-sites-edit').handler(); const editor = grid._pveExtraSites.editor;
    setEditor(editor, 'Unsaved name', site().url);
    const requests = ui.requests.length;
    assert.equal(ui.context.window.PVEPluginManagerNavigation.publish({ ...navigation, module_aliases: {},
        entry_order: [viewId, 'pve-extra-site-' + second.id, 'komodo', 'temperatures', 'safeline'] }, 3), true);
    assert.deepEqual(Array.from(grid.getStore().rows, (row) => row.data.id), [site().id, second.id]);
    assert.equal(selector.getStore().findRecord('key', 'pve-komodo-containers'), komodoChoice);
    assert.equal(komodoChoice.get('value'), 'Komodo', 'clearing the module alias restores the default label in place');
    assert.match(siteInfoHtml(view.panel), /--插件-- → 模块与站点/);
    assert.doesNotMatch(siteInfoHtml(view.panel), /工具/);
    assert.equal(grid.getSelectionModel().getSelection()[0], gridRecord); assert.equal(grid._pveExtraSites.editor, editor);
    assert.equal(editorButton(editor, 'name').getValue(), 'Unsaved name'); assert.equal(ui.requests.length, requests);
    assert.equal(view.page, page); assert.equal(view.frame, frame); assert.equal(ui.loadedFrames.length, frameCount);
    assert.equal(selector.getValue(), viewId); assert.equal(selector.isValid(), true); assert.equal(selectionEvents, 1);
});


function iconResult(entry = site(), iconUrl = 'https://assets.example/favicon.svg') {
    return { site_id: entry.id, revision: entry.revision, icon_url: iconUrl };
}

test('automatic site icons update existing dropdown, grid and sidebar without disturbing selected values, drafts or frames', () => {
    const ui = browser({ navigation: true }); ui.load(); const view = ui.setup();
    const selector = ui.create('PVE.form.ViewSelector', { ownerCt: view.workspace }); selector.render();
    ui.respond(list(), ui.requests.findIndex((request) => request.options.url === api));
    const selectedRecord = selector.getStore().findRecord('key', viewId);
    assert.equal(selectedRecord.get('pluginIcon').value, 'fa fa-globe');
    selector.on('select', function (combo) { view.native.setViewFilter(combo.getViewFilter()); });
    selector.setValue(viewId); selector.fire('select', selector, [selectedRecord]); ui.respond(list());
    const grid = ui.context.window.PVEPluginSites.createManager(view.workspace); view.workspace.add(grid); view.workspace.render();
    ui.respond(list()); const gridRecord = grid.getStore().getById(site().id); grid.getSelectionModel().select(gridRecord);
    grid.down('#pve-extra-sites-edit').handler(); const editor = grid._pveExtraSites.editor;
    setEditor(editor, 'Unsaved icon-time draft', site().url);
    assert.equal(ui.iconRequests.length, 1, 'the selector, site page and manager share one discovery request');
    assert.equal(ui.iconRequests[0].options.url, api + '/' + site().id + '/icon');
    const page = view.page, frame = view.frame, frames = ui.loadedFrames.length, requests = ui.requests.length;
    const iconUrl = 'https://assets.example/icon.svg?size=16&theme=light'; ui.respondIcon(iconResult(site(), iconUrl));
    assert.equal(selector.getStore().findRecord('key', viewId), selectedRecord);
    assert.equal(selectedRecord.get('pluginIcon').type, 'url'); assert.equal(selectedRecord.get('pluginIcon').value, iconUrl);
    assert.equal(selector.getValue(), viewId); assert.equal(selector.getRawValue(), site().name); assert.equal(selector.isValid(), true);
    assert.equal(grid.getStore().getById(site().id), gridRecord); assert.equal(gridRecord.get('pluginIcon').value, iconUrl);
    assert.equal(grid.getSelectionModel().getSelection()[0], gridRecord);
    assert.equal(grid._pveExtraSites.editor, editor); assert.equal(editorButton(editor, 'name').getValue(), 'Unsaved icon-time draft');
    const nameHtml = grid.columns[0].renderer('<Site name>', null, gridRecord);
    assert.match(nameHtml, /<img/); assert.match(nameHtml, /size=16&amp;theme=light/); assert.match(nameHtml, /&lt;Site name&gt;/);
    assert.match(nameHtml, /referrerpolicy="no-referrer"/); assert.match(view.panel.title, /<img/); assert.match(view.panel.title, /站点信息/);
    assert.equal(view.page, page); assert.equal(view.frame, frame); assert.equal(ui.loadedFrames.length, frames); assert.equal(ui.requests.length, requests);
    ui.context.window.PVEPluginSites.refresh(view.workspace); ui.respond(list());
    assert.equal(ui.iconRequests.length, 1, 'a successful result is cached for the same registered URL and revision');
});

test('icon discovery bounds concurrency, deduplicates repeated lists and uses native fallback on failed or invalid responses', () => {
    const ui = browser(); ui.load(); const view = ui.setup();
    const selector = ui.create('PVE.form.ViewSelector', { ownerCt: view.workspace }); selector.render();
    const entries = 'abcdef'.split('').map((id) => site(id, 'Site ' + id, 'https://' + id + '.example/'));
    ui.respond(list(entries)); assert.equal(ui.iconRequests.length, 4, 'only four discovery requests can be outstanding');
    ui.context.window.PVEPluginSites.refresh(view.workspace); ui.respond(list(entries)); assert.equal(ui.iconRequests.length, 4);
    ui.respondIcon('UNTRUSTED <script>error</script>', 0, 502); assert.equal(ui.iconRequests.length, 5);
    ui.respondIcon(iconResult(entries[1], 'javascript:alert(1)'), 1); assert.equal(ui.iconRequests.length, 6);
    ui.respondIcon({ ...iconResult(entries[2]), revision: 99 }, 2);
    ui.respondIcon({ ...iconResult(entries[3]), site_id: entries[0].id }, 3);
    ui.respondIcon(iconResult(entries[4], 'https://user:password@assets.example/icon.png'), 4);
    ui.respondIcon(iconResult(entries[5]), 5);
    entries.slice(0, 5).forEach((entry) => {
        const icon = selector.getStore().findRecord('key', 'pve-extra-site-' + entry.id).get('pluginIcon');
        assert.equal(icon.type, 'pve'); assert.equal(icon.value, 'fa fa-globe');
    });
    assert.equal(selector.getStore().findRecord('key', 'pve-extra-site-' + entries[5].id).get('pluginIcon').type, 'url');
    ui.context.window.PVEPluginSites.refresh(view.workspace); ui.respond(list(entries));
    assert.equal(ui.iconRequests.length, 6, 'failed results are not retried repeatedly for the same revision');
    assert.equal(ui.loadedFrames.length, 0);
});

test('revised or deleted sites cancel icon requests and late callbacks cannot overwrite newer choices', () => {
    const ui = browser(); ui.load(); const view = ui.setup();
    const selector = ui.create('PVE.form.ViewSelector', { ownerCt: view.workspace }); selector.render(); ui.respond(list());
    const first = ui.iconRequests[0], record = selector.getStore().findRecord('key', viewId);
    const renamed = site('a', 'Renamed', site().url, 2);
    ui.context.window.PVEPluginSites.refresh(view.workspace); ui.respond(list([renamed]));
    assert.equal(first.aborted, true); assert.equal(ui.iconRequests.length, 2);
    ui.respondIcon(iconResult(site(), 'https://old.example/icon.png'), 0);
    assert.equal(record.get('pluginIcon').type, 'pve'); assert.equal(record.get('value'), 'Renamed');
    const changed = site('a', 'Moved', 'https://new.example/', 3);
    ui.context.window.PVEPluginSites.refresh(view.workspace); ui.respond(list([changed]));
    assert.equal(ui.iconRequests[1].aborted, true); assert.equal(ui.iconRequests.length, 3);
    ui.respondIcon(iconResult(renamed, 'https://stale.example/icon.png'), 1); assert.equal(record.get('pluginIcon').type, 'pve');
    ui.respondIcon(iconResult(changed, 'https://new.example/site.png'), 2); assert.equal(record.get('pluginIcon').value, 'https://new.example/site.png');
    const next = { ...changed, revision: 4 };
    ui.context.window.PVEPluginSites.refresh(view.workspace); ui.respond(list([next]));
    ui.context.window.PVEPluginSites.refresh(view.workspace); ui.respond(list([]));
    assert.equal(ui.iconRequests[3].aborted, true); ui.respondIcon(iconResult(next), 3);
    assert.equal(selector.getStore().findRecord('key', viewId), undefined); assert.equal(ui.loadedFrames.length, 0);
});

test('site icon caches and queues are scoped to workspace and cleared on logout, destruction or identity loss', () => {
    const ui = browser(); ui.load(); const first = ui.setup(), second = ui.setup();
    const one = ui.create('PVE.form.ViewSelector', { ownerCt: first.workspace }); one.render(); ui.respond(list());
    const two = ui.create('PVE.form.ViewSelector', { ownerCt: second.workspace }); two.render(); ui.respond(list());
    assert.equal(ui.iconRequests.length, 2, 'workspaces have separate discovery caches');
    ui.respondIcon(iconResult(site(), 'https://first.example/icon.png'), 0);
    assert.equal(one.getStore().findRecord('key', viewId).get('pluginIcon').type, 'url');
    assert.equal(two.getStore().findRecord('key', viewId).get('pluginIcon').type, 'pve');
    second.workspace.destroy(); assert.equal(ui.iconRequests[1].aborted, true); ui.respondIcon(iconResult(), 1);
    first.workspace.onLogin(null); assert.equal(one.getStore().findRecord('key', viewId), undefined);
    first.workspace.onLogin({ username: 'root@pam' }); ui.respond(list());
    assert.equal(ui.iconRequests.length, 3, 'the new login must not reuse the prior session cache');
    ui.context.window.Proxmox.UserName = 'other@pam'; ui.respondIcon(iconResult(), 2);
    assert.equal(one.getStore().findRecord('key', viewId).get('pluginIcon').type, 'pve');
    const auth = browser(); auth.load(); const view = auth.setup();
    const selector = auth.create('PVE.form.ViewSelector', { ownerCt: view.workspace }); selector.render();
    const sites = 'abcde'.split('').map((id) => site(id)); auth.respond(list(sites));
    auth.respondIcon({}, 0, 403); assert.equal(auth.iconRequests.length, 4);
    assert.ok(auth.iconRequests.slice(1).every((request) => request.aborted));
});


test('site metadata helpers preserve full admin lists while disabled choices and manual icons skip discovery', () => {
    const ui = browser({ navigation: true }); ui.load(); const view = ui.setup();
    const selector = ui.create('PVE.form.ViewSelector', { ownerCt: view.workspace });
    const api = ui.context.window.PVEPluginSites;
    const manual = site('a', 'Original', site().url, 1, { alias: '<监控 & 面板>', icon: { type: 'pve', value: 'fa fa-server' } });
    const disabled = site('b', 'Disabled', 'https://disabled.example/', 1, { enabled: false });
    const auto = site('c', 'Auto', 'https://auto.example/');
    const received = [];
    const unsubscribe = api.subscribe(view.workspace, function (sites) {
        received.push(JSON.parse(JSON.stringify(sites)));
        if (sites[0]) { sites[0].alias = 'mutated subscriber copy'; if (sites[0].icon) sites[0].icon.value = 'fa fa-bug'; }
    });
    assert.equal(api.sync(view.workspace, [manual, disabled, auto]), true);
    assert.equal(received.at(-1).length, 3); assert.equal(received.at(-1)[1].enabled, false);
    assert.equal(selector.getStore().findRecord('key', viewId).get('value'), '<监控 & 面板>');
    assert.equal(selector.getStore().findRecord('key', 'pve-extra-site-' + disabled.id), undefined);
    assert.equal(api.siteIcon(view.workspace, manual).value, 'fa fa-server');
    assert.equal(ui.iconRequests.length, 1); assert.equal(ui.iconRequests[0].options.url, '/pve-plugins/api/sites/' + auto.id + '/icon');
    assert.equal(selector.getStore().findRecord('key', viewId).get('pluginIcon').value, 'fa fa-server');
    ui.respondIcon(iconResult(auto, 'https://auto.example/logo.svg'));
    assert.equal(api.siteIcon(view.workspace, auto).value, 'https://auto.example/logo.svg');
    assert.equal(received.at(-1)[0].alias, manual.alias, 'subscriber mutations cannot alter cached authoritative metadata');
    const record = selector.getStore().findRecord('key', viewId); selector.setValue(viewId);
    const urlIcon = { ...manual, revision: 2, icon: { type: 'url', value: 'https://cdn.example/manual.svg' } };
    api.sync(view.workspace, [urlIcon, disabled, auto]);
    assert.equal(selector.getStore().findRecord('key', viewId), record); assert.equal(selector.getValue(), viewId); assert.equal(selector.isValid(), true);
    assert.equal(record.get('pluginIcon').value, urlIcon.icon.value); assert.equal(ui.iconRequests.length, 1);
    const before = received.length;
    assert.equal(api.sync(view.workspace, [{ ...manual, enabled: 'false' }]), false);
    assert.equal(api.sync(view.workspace, [{ ...manual, icon: { type: 'url', value: 'javascript:x' } }]), false);
    assert.equal(api.sync(view.workspace, [{ ...manual, alias: 'bad\nname' }]), false);
    assert.equal(received.length, before);
    unsubscribe(); api.sync(view.workspace, []); assert.equal(received.length, before);
});

test('public open honors enabled sites and live metadata updates preserve an iframe until the site is disabled', () => {
    const ui = browser({ navigation: true }); ui.load(); const view = ui.setup();
    const selector = ui.create('PVE.form.ViewSelector', { ownerCt: view.workspace });
    const api = ui.context.window.PVEPluginSites;
    const original = site('a', 'Canonical name', site().url, 1, { alias: 'Alias', icon: { type: 'pve', value: 'fa fa-server' } });
    api.sync(view.workspace, [original]); view.switch('server');
    assert.equal(api.open(view.workspace, 'missing'), false);
    assert.equal(api.open(view.workspace, original.id), true); ui.respond(list([original]));
    assert.equal(selector.getValue(), viewId); assert.equal(view.frame.attributes.title, 'Alias');
    const frame = view.frame, page = view.page, requests = ui.requests.length, frames = ui.loadedFrames.length;
    const renamed = { ...original, alias: '<New alias>', revision: 2, icon: { type: 'url', value: 'https://cdn.example/manual.svg' } };
    api.sync(view.workspace, [renamed]);
    assert.equal(view.page, page); assert.equal(view.frame, frame); assert.equal(ui.loadedFrames.length, frames);
    assert.equal(frame.attributes.title, renamed.alias); assert.match(siteInfoHtml(view.panel), /&lt;New alias&gt;/);
    assert.match(view.panel.title, /manual.svg/); assert.equal(ui.iconRequests.length, 0); assert.equal(ui.requests.length, requests);
    const disabled = { ...renamed, enabled: false, revision: 3 }; api.sync(view.workspace, [disabled]);
    assert.equal(frame.attributes.src, 'about:blank'); assert.equal(view.frame, undefined); assert.equal(view.panel.hidden, true);
    assert.equal(selector.getStore().findRecord('key', viewId), undefined);
    assert.match(view.page.html, /已停用/); assert.equal(api.open(view.workspace, original.id), false);
    const enabled = { ...disabled, enabled: true, revision: 4 }; api.sync(view.workspace, [enabled]);
    assert.equal(api.open(view.workspace, enabled.id), true); ui.respond(list([enabled]));
    assert.equal(view.frame.attributes.src, enabled.url); assert.equal(ui.iconRequests.length, 0);
});

test('automatic discovery is canceled when manual icons or disablement replace a registered site', () => {
    const ui = browser(); ui.load(); const view = ui.setup();
    const selector = ui.create('PVE.form.ViewSelector', { ownerCt: view.workspace });
    const api = ui.context.window.PVEPluginSites;
    api.sync(view.workspace, [site()]); const pending = ui.iconRequests[0];
    const manual = site('a', 'Grafana', site().url, 2, { icon: { type: 'pve', value: 'fl fl-docker' } });
    api.sync(view.workspace, [manual]); assert.equal(pending.aborted, true);
    ui.respondIcon(iconResult(site(), 'https://stale.example/icon.svg'), 0);
    assert.equal(selector.getStore().findRecord('key', viewId).get('pluginIcon').value, 'fl fl-docker');
    const automatic = { ...manual, revision: 3, icon: null }; api.sync(view.workspace, [automatic]);
    assert.equal(ui.iconRequests.length, 2);
    api.sync(view.workspace, [{ ...automatic, enabled: false, revision: 4 }]);
    assert.equal(ui.iconRequests[1].aborted, true); assert.equal(ui.iconRequests.length, 2);
    ui.respondIcon(iconResult(automatic), 1); assert.equal(selector.getStore().findRecord('key', viewId), undefined);
});
