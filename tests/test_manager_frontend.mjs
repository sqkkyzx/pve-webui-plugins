import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../app/static/manager.js', import.meta.url), 'utf8');
const navigationSource = readFileSync(new URL('../app/static/navigation.js', import.meta.url), 'utf8');
const viewId = 'pve-plugin-manager';
const defaults = () => ({ entry_order: ['komodo', 'temperatures', 'safeline'], module_aliases: {}, module_icons: {} });
function browser({ restoredView = 'server', loggedIn = true, coordinator = false, realNavigation = true } = {}) {
    const classes = new Map(), stack = [], requests = [], warnings = [], created = [], delayedRemovals = [];
    const published = [], subscribers = [], siteSubscribers = new Set(), siteSyncs = [], siteOpens = [];
    let reloads = 0, context;
    function define(name, config) {
        if (config.override) {
            const target = classes.get(config.override);
            for (const [key, fn] of Object.entries(config)) if (typeof fn === 'function') {
                const previous = target.prototype[key];
                target.prototype[key] = function (...args) { stack.push(previous); try { return fn.apply(this, args); } finally { stack.pop(); } };
            }
            return;
        }
        class Component {
            constructor(value = {}) {
                Object.assign(this, value); this.events = {}; this.children = [];
                if (value.xtype === 'checkbox' && value.value === undefined) this.value = !!value.checked;
            }
            callParent(args) { return stack.at(-1).apply(this, args); }
            on(name, fn) { (this.events[name] ||= []).push(fn); }
            fire(name, ...args) { let result; for (const fn of this.events[name] || []) if (fn.apply(this, args.length ? args : [this]) === false) result = false; return result; }
            child(selector) { return this.children.find((component) => selector === '#' + component.itemId) || null; }
            down(selector) { for (const child of this.children) { if (selector === '#' + child.itemId) return child; const found = child.down(selector); if (found) return found; } return null; }
            up(xtype) { let parent = this.ownerCt; while (parent) { if (parent.xtype === xtype) return parent; parent = parent.ownerCt; } return null; }
            setVisible(visible) { this.hidden = !visible; }
            setValue(value) { const previous = this.value; this.value = value; if (value !== previous) this.fire('change', this, value, previous); }
            getValue() { return this.value; }
            setEmptyText(value) { this.emptyText = value; }
            setText(value) { this.text = value; }
            update(value) { this.html = value; }
            setBoxLabel(value) { this.boxLabel = value; }
            setTitle(value) { this.title = value; }
            setDisabled(value) { this.disabled = value; }
            getStore() { return this.dataStore; }
            getSelection() { return this.selectedNode || null; }
            setSelection(record) {
                if (this.selectedNode === record) return;
                this.selectedNode = record;
                this.fire('selectionchange', this, record);
            }
            add(value) {
                const child = value.events ? value : create('Ext.Component', value);
                child.ownerCt = this; this.children.push(child);
                if (this.rendered) child.render();
                return child;
            }
            getSelectionModel() {
                if (!this.selection) this.selection = {
                    selected: [], getSelection() { return this.selected; }, suspendEvents() {}, resumeEvents() {},
                    select: (record) => {
                        this.selection.selected = record ? [record] : [];
                        this.fire('selectionchange', this.selection, this.selection.selected);
                    },
                };
                return this.selection;
            }
            getLayout() {
                return { getActiveItem: () => this.activeItem || (this.cardChildren || this.children)[0], setActiveItem: (card) => {
                    const previous = this.activeItem || (this.cardChildren || this.children)[0];
                    if (previous === card) return;
                    previous.hidden = true; previous.fire('deactivate'); previous.fire('hide');
                    this.activeItem = card; card.hidden = false; card.fire('show'); card.fire('activate');
                } };
            }
            render() { if (this.rendered) return; this.rendered = true; for (const child of this.children) child.render(); this.fire('afterrender'); }
            destroy() {
                if (this.destroying || this.destroyed) return;
                this.destroying = true; this.fire('beforedestroy');
                for (const child of this.children.slice()) child.destroy();
                if (this.ownerCt) {
                    const index = this.ownerCt.children.indexOf(this);
                    if (index >= 0) this.ownerCt.children.splice(index, 1);
                    if (this.ownerCt.activeItem === this) this.ownerCt.activeItem = null;
                }
                this.destroyed = true; this.fire('destroy');
            }
            show(options) { if (options) this.dialogOptions = options; this.render(); }
            close() { if (this.fire('beforeclose') !== false) this.destroy(); }
        }
        Object.assign(Component.prototype, config); classes.set(name, Component);
    }
    function makeStore(rows = []) {
        const store = { rows: [], owner: null,
            getCount() { return this.rows.length; }, each(fn) { this.rows.forEach(fn); },
            getRange() { return this.rows.slice(); }, getById(id) { return this.rows.find((row) => row.getId() === id); },
            getNodeById(id) { return this.getById(id); },
            sort(options) { this.rows.sort(options.sorterFn); this.rows.forEach((row) => row.childNodes.sort(options.sorterFn)); },
            findRecord(key, value) { return this.rows.find((row) => row.get(key) === value); },
            add(value) { const record = value.get ? value : makeRecord(value); this.rows.push(record); return [record]; },
            insert(index, values) { this.rows.splice(index, 0, ...[values].flat().map((value) => value.get ? value : makeRecord(value))); },
            remove(values) {
                for (const row of [values].flat()) {
                    if (row.parentNode) row.parentNode.childNodes = row.parentNode.childNodes.filter((child) => child !== row);
                    this.rows = this.rows.filter((record) => record !== row);
                }
                if (this.owner?.selection) {
                    this.owner.selection.selected = this.owner.selection.selected.filter((row) => this.rows.includes(row));
                    this.owner.fire('selectionchange', this.owner.selection, this.owner.selection.selected);
                }
            },
            loadData(values) { this.rows = values.map((value) => makeRecord(value)); },
        };
        function makeRecord(value, parentNode = null) {
            const data = { ...value };
            const record = { data, parentNode, childNodes: [], get: (key) => data[key], getId: () => data.id,
                set(key, content) { if (typeof key === 'object') Object.assign(data, key); else data[key] = content; },
                appendChild(content) { const child = makeRecord(content, this); this.childNodes.push(child); store.rows.push(child); return child; },
                remove() { store.remove(this); },
            };
            return record;
        }
        store.loadData(rows); return store;
    }
    define('Ext.Component', { initComponent() {} });
    define('Ext.tree.Panel', { initComponent() {} });
    define('PVE.form.ViewSelector', { initComponent() {
        this.dataStore = makeStore(['server', 'folder', 'pool', 'tags', 'pve-komodo-containers', 'pve-safeline-waf'].map((key) => ({ key })));
        return 'native-selector';
    } });
    define('PVE.tree.ResourceTree', { initComponent() {
        this.setViewFilter = function (view) { this.viewFilter = view; this.setVisible(true); return 'native-view'; };
    } });
    define('PVE.StdWorkspace', {
        onLogin(data) { context.window.Proxmox.UserName = data?.username || null; if (!data) this.setContent(null); },
        // PVE 9 StdWorkspace.setContent adds a config or component to its card
        // container, advances the layout and removes the prior card later.
        setContent(comp) {
            const view = this.child('#content'), layout = view.getLayout(), current = layout.getActiveItem();
            if (!comp) { view.removeAll(); this.page = null; return; }
            comp.border = false;
            const page = view.add(comp); this.page = page;
            if (current !== null && layout.getNext()) {
                layout.next(); delayedRemovals.push(() => view.remove(current));
            }
        },
    });
    function create(name, config = {}) {
        const c = new (classes.get(name) || classes.get('Ext.Component'))(config);
        if (config.store) { c.dataStore = makeStore(config.store.data || []); c.dataStore.owner = c; }
        if (config.store?.root) {
            const root = config.store.root;
            c.dataStore.loadData([root]);
            const rootRecord = c.dataStore.getById(root.id);
            for (const child of root.children || []) rootRecord.appendChild(child);
        }
        for (const key of ['items', 'tbar', 'bbar', 'buttons', 'dockedItems']) {
            for (const child of [].concat(config[key] || [])) if (typeof child === 'object') { const component = create('Ext.Component', child); component.ownerCt = c; c.children.push(component); }
        }
        if (config.layout === 'card') {
            c.cardChildren = c.children.slice(0, [].concat(config.items || []).length);
            c.cardChildren.forEach((child, index) => { child.hidden = index > 0; });
        }
        for (const [key, fn] of Object.entries(config.listeners || {})) c.on(key, fn);
        c.className = name; c.initComponent?.(); created.push(c); return c;
    }
    context = vm.createContext({ window: { URL, Ext: { define, create, Msg: { YESNO: 'yesno', WARNING: 'warning' }, ClassManager: { get: (name) => classes.get(name) },
        Ajax: { request(options) { const request = { options }; requests.push(request); return request; }, abort(request) { request.aborted = true; } } },
        Proxmox: { UserName: loggedIn ? 'root@pam' : null, Utils: { authOK: () => !!context.window.Proxmox.UserName } },
        location: { reload() { reloads++; } }, console: { warn: (...args) => warnings.push(args) } } });
    if (realNavigation) {
        vm.runInContext(navigationSource, context);
        const api = context.window.PVEPluginManagerNavigation, publish = api.publish;
        api.publish = (value, revision) => { published.push({ value: JSON.parse(JSON.stringify(value)), revision }); return publish(value, revision); };
    }
    context.window.PVEPluginSites = {
        siteIcon(workspace, site) { return site.icon || { type: 'pve', value: 'fa fa-globe' }; },
        sync(workspace, sites) { siteSyncs.push({ workspace, sites }); siteSubscribers.forEach((subscription) => { if (subscription.workspace === workspace) subscription.callback(sites); }); },
        subscribe(workspace, callback) { const subscription = { workspace, callback }; siteSubscribers.add(subscription); return () => siteSubscribers.delete(subscription); },
        open(workspace, id) { siteOpens.push({ workspace, id }); },
    };
    if (coordinator) context.window.PVEPluginManagerNavigation = {
        get: defaults, applySelector() {}, subscribe(fn) { subscribers.push(fn); },
        publish(value, revision) { published.push({ value: JSON.parse(JSON.stringify(value)), revision }); subscribers.forEach((fn) => fn()); return true; },
    };
    vm.runInContext(source, context); vm.runInContext(source, context);
    const workspace = create('PVE.StdWorkspace', { xtype: 'pveStdWorkspace' });
    const content = create('Ext.Component', { itemId: 'content' }); content.ownerCt = workspace; content.activeItem = null;
    content.getLayout = () => ({
        getActiveItem: () => content.activeItem,
        getNext: () => content.children[content.children.indexOf(content.activeItem) + 1] || null,
        next() {
            const old = content.activeItem, next = this.getNext();
            if (old) { old.fire('deactivate'); old.fire('hide'); }
            content.activeItem = next;
        },
    });
    content.add = (value) => {
        const page = value.events ? value : create('Ext.Component', value);
        page.ownerCt = content; content.children.push(page);
        if (!content.activeItem) content.activeItem = page;
        page.render(); return page;
    };
    content.remove = (page) => page.destroy();
    content.removeAll = () => content.children.slice().forEach((page) => content.remove(page));
    workspace.children.push(content);
    // StdWorkspace creates ResourceTree before it creates/adds the side panel.
    const tree = create('PVE.tree.ResourceTree', { viewFilter: { id: restoredView, groups: ['node'] } });
    assert.equal(tree.ownerCt, undefined, 'native tree is initialized outside a container');
    const owner = create('Ext.Component'); owner.ownerCt = workspace; workspace.children.push(owner);
    tree.ownerCt = owner; owner.children.push(tree); tree.render();
    function respond(request, data, status = 200) { assert.ok(request, 'expected pending request'); request.responded = true; request.options.callback(null, status < 400, { status, responseText: JSON.stringify(data) }); }
    function open() { tree.setViewFilter({ id: viewId }); return workspace.page; }
    return { context, classes, create, tree, workspace, content, requests, warnings, created, published, siteSyncs, siteOpens, siteSubscribers, open, respond,
        pending(path, method = 'GET') { return requests.find((request) => !request.responded && !request.aborted && request.options.url === '/pve-plugins/api/' + path && request.options.method === method); },
        emitSites(sites) { siteSubscribers.forEach((subscription) => subscription.callback(sites)); },
        reloads: () => reloads,
        flushRemovals() { delayedRemovals.splice(0).forEach((remove) => remove()); } };
}

const plain = (value) => JSON.parse(JSON.stringify(value));
const sitePrefix = 'pve-extra-site-';
function config(enabled = ['komodo', 'sites']) {
    const plugins = enabled.slice(); if (!plugins.includes('sites')) plugins.push('sites');
    return { revision: 2, csrf_token: 'config-csrf', enabled_plugins: plugins, navigation: defaults(), modules: {
        komodo: { url: 'https://core.example', api_key_configured: true, api_secret_configured: true, tls_verify: true },
        temperatures: { nodes: { 'pve-a': { url: 'https://metrics.example', type: 'node_exporter', token_configured: true } } },
    } };
}
function site(id = 'a'.repeat(32), changes = {}) {
    return { id, name: '站点 ' + id[0].toUpperCase(), url: 'https://site-' + id[0] + '.example/app', revision: 1,
        enabled: true, alias: '', icon: null, ...changes };
}
function ready(ui, data = config(), sites = []) {
    const page = ui.workspace.page && !ui.workspace.page.destroyed ? ui.workspace.page : ui.open();
    ui.respond(ui.pending('config'), data);
    ui.respond(ui.pending('sites'), { user: 'root@pam', csrf_token: 'site-csrf', sites });
    return page;
}
function grid(page) { return page.down('#plugin-entry-grid'); }
function row(page, id) { return grid(page).getStore().getById(id); }
function editor(ui) { return ui.created.findLast((component) => component.itemId === 'plugin-entry-editor'); }
function edit(ui, page, id) {
    grid(page).getSelectionModel().select(row(page, id)); page.down('#plugin-entry-edit').handler(); return editor(ui);
}
function saveDialog(dialog) { dialog.down('#entry-save').handler(); }
function closeDialog(dialog) { dialog.down('#entry-cancel').handler(); }
function replyConfig(ui, request, data, revision = data.revision + 1, reload_required = false) {
    const result = { ...data, revision, navigation: plain(request.options.jsonData.navigation), reload_required };
    ui.respond(request, result); return result;
}

test('flat module/site tree and unified native table show explicit types and route rows to the right cards', () => {
    const ui = browser(), a = site(), b = site('b'.repeat(32), { enabled: false, alias: '备用站点' });
    const page = ready(ui, config(), [a, b]);
    const tree = ui.workspace.down('#pve-plugin-manager-tree');
    assert.deepEqual(tree.getStore().getNodeById('modules').childNodes.map((node) => node.getId()), ['komodo', 'temperatures', 'safeline', sitePrefix + a.id, sitePrefix + b.id]);
    assert.equal(tree.getStore().getNodeById('sites'), undefined);
    assert.equal(tree.getStore().getNodeById('ordering'), undefined);
    assert.deepEqual(Array.from(grid(page).columns, (column) => column.text), ['顺序', '类型', '名称', '开发进度', '启用状态', '显示别名', '图标', '站点目标']);
    const type = grid(page).columns.find((column) => column.dataIndex === 'kind');
    assert.match(type.renderer('module'), /功能模块/); assert.match(type.renderer('site'), /附加站点/);
    tree.getSelectionModel().select(tree.getStore().getNodeById('komodo'));
    assert.equal(page.down('#plugin-manager-pages').getLayout().getActiveItem().itemId, 'plugin-page-komodo');
    page.down('#komodo-url').setValue('https://unsaved.example');
    tree.getSelectionModel().select(tree.getStore().getNodeById(sitePrefix + a.id));
    assert.equal(page.down('#plugin-manager-pages').getLayout().getActiveItem().itemId, 'plugin-page-modules');
    assert.equal(grid(page).getSelectionModel().getSelection()[0], row(page, sitePrefix + a.id));
    page.down('#plugin-entry-open').handler(); assert.equal(ui.siteOpens.at(-1).id, a.id);
    grid(page).getSelectionModel().select(row(page, sitePrefix + b.id));
    assert.equal(page.down('#plugin-entry-open').disabled, true); assert.equal(page.down('#plugin-entry-delete').disabled, false);
    assert.equal(tree.getStore().getNodeById(sitePrefix + b.id).get('status'), '已停用');
    const renderer = tree.columns[0].renderer;
    assert.match(renderer('备用站点', {}, tree.getStore().getNodeById(sitePrefix + b.id)), /站点 \/ 备用站点/);
    tree.getSelectionModel().select(tree.getStore().getNodeById('komodo'));
    assert.equal(page.down('#komodo-url').getValue(), 'https://unsaved.example');
    ui.tree.setViewFilter({ id: 'server' }); assert.equal(page.destroyed, true); assert.equal(tree.hidden, true);
    assert.equal(ui.siteSubscribers.size, 0); ui.tree.destroy(); assert.equal(tree.destroyed, true);
});

test('type colors retain readable labels and progress distinguishes project coverage from external-site maturity', () => {
    const ui = browser(), a = site(), page = ready(ui, config(), [a]);
    const type = grid(page).columns.find((column) => column.dataIndex === 'kind');
    const module = type.renderer('module'), external = type.renderer('site');
    assert.match(module, /(?:color|background)[^=]*[:=]/);
    assert.match(external, /(?:color|background)[^=]*[:=]/);
    const style = (html) => [...html.matchAll(/style="([^"]+)"/g)].map((match) => match[1]);
    assert.notDeepEqual(style(module), style(external), 'type colors distinguish modules from sites');
    const progress = grid(page).columns.find((column) => column.dataIndex === 'progress');
    assert.ok(progress, 'development progress has its own column');
    for (const id of ['komodo', 'safeline']) {
        const record = row(page, id), meta = {};
        const html = progress.renderer(record.get('progress'), meta, record);
        assert.match(html, /部分管理/);
        assert.match(html, /测试中/);
        assert.match(html + (meta.tdAttr || ''), /title=|data-qtip=/, 'coverage limitations are available from the cell');
        assert.doesNotMatch(html.replace(/<[^>]*>/g, ''), /完全替代|完整替代/);
    }
    const record = row(page, sitePrefix + a.id), meta = {};
    const html = progress.renderer(record.get('progress'), meta, record);
    assert.match(html, /外部站点/);
    assert.doesNotMatch(html, /稳定|测试中|实验|调试/, 'host cannot assign maturity to the target application');
});

test('module introduction and help stay available when disabled and do not request optional-plugin APIs', () => {
    const ui = browser(), page = ready(ui, config(['sites']));
    const tree = ui.workspace.down('#pve-plugin-manager-tree');
    const count = ui.requests.length;
    for (const id of ['komodo', 'temperatures', 'safeline']) {
        tree.getSelectionModel().select(tree.getStore().getNodeById(id));
        const module = page.down('#plugin-page-' + id), menu = module.down('#plugin-module-menu-' + id);
        assert.equal(module.xtype, 'panel'); assert.equal(module.layout, 'card');
        const toolbar = module.children.find((child) => child.dock === 'left');
        assert.equal(toolbar.cls, 'pve-toolbar-bg'); assert.equal(toolbar.width, 160);
        assert.equal(menu.xtype, 'treelist'); assert.equal(menu.ui, 'pve-nav');
        const nodes = ['intro', 'config', 'help'].map((section) => menu.getStore().getNodeById('plugin-' + section + '-' + id));
        assert.deepEqual(nodes.map((node) => node.get('text')), ['介绍', '基本配置', '帮助文档']);
        assert.ok(nodes.every((node) => /^fa fa-/.test(node.get('iconCls'))));
        assert.ok(!module.disabled, 'disabled module still has readable documentation');
        assert.equal(module.getLayout().getActiveItem(), page.down('#plugin-intro-' + id));
        assert.equal(menu.getSelection(), nodes[0]);
        const help = page.down('#plugin-help-' + id);
        assert.ok(help.html?.length > 100, 'help contains module-specific instructions');
        menu.setSelection(nodes[2]); assert.equal(module.getLayout().getActiveItem(), help);
        if (id === 'safeline') {
            menu.setSelection(nodes[1]);
            assert.equal(module.getLayout().getActiveItem(), help, 'disabled configuration cannot replace documentation');
            assert.equal(menu.getSelection(), nodes[2], 'menu selection stays synchronized with the displayed card');
        }
    }
    assert.equal(page.down('#plugin-config-safeline').disabled, true);
    assert.equal(ui.requests.length, count, 'reading documentation performs no extra network requests');
    assert.equal(ui.requests.some((request) => request.options.url.includes('/safeline/')), false);
});

test('module-configure action opens basic configuration and native menu navigation preserves pending credentials', () => {
    const ui = browser(), page = ready(ui), record = row(page, 'komodo');
    grid(page).getSelectionModel().select(record); page.down('#plugin-entry-configure').handler();
    const module = page.down('#plugin-page-komodo'), menu = module.down('#plugin-module-menu-komodo');
    const configuration = page.down('#plugin-config-komodo');
    assert.equal(page.down('#plugin-manager-pages').getLayout().getActiveItem(), module);
    assert.equal(module.getLayout().getActiveItem(), configuration);
    assert.equal(menu.getSelection().getId(), configuration.itemId);
    page.down('#komodo-url').setValue('https://draft.example');
    page.down('#komodo-api_secret').setValue('pending-secret');
    menu.setSelection(menu.getStore().getNodeById('plugin-help-komodo'));
    const tree = ui.workspace.down('#pve-plugin-manager-tree');
    tree.getSelectionModel().select(tree.getStore().getNodeById('temperatures'));
    tree.getSelectionModel().select(tree.getStore().getNodeById('komodo'));
    menu.setSelection(menu.getStore().getNodeById('plugin-config-komodo'));
    assert.equal(module.getLayout().getActiveItem(), configuration, 'menu reuses the same form instance');
    assert.equal(page.down('#komodo-url').getValue(), 'https://draft.example');
    assert.equal(page.down('#komodo-api_secret').getValue(), 'pending-secret');
    assert.equal(ui.requests.some((request) => request.options.method !== 'GET'), false, 'documentation navigation never saves a draft');
    page.down('#plugin-manager-save').handler();
    const write = ui.pending('config', 'PUT');
    assert.equal(write.options.jsonData.modules.komodo.url, 'https://draft.example');
    assert.equal(write.options.jsonData.modules.komodo.api_secret, 'pending-secret');
});

test('module editor saves only its target entity and preserves pending connection and mixed-order drafts', () => {
    const ui = browser(), data = config(['sites', 'komodo', 'temperatures']), a = site();
    const page = ready(ui, data, [a]); const original = row(page, 'komodo');
    grid(page).getSelectionModel().select(row(page, sitePrefix + a.id));
    for (let index = 0; index < 3; index++) page.down('#plugin-entry-up').handler();
    page.down('#komodo-url').setValue('https://draft.example');
    const dialog = edit(ui, page, 'komodo');
    assert.equal(dialog.down('#entry-kind').getValue(), '功能模块'); assert.equal(dialog.down('#entry-name').readOnly, true);
    assert.equal(dialog.down('#entry-target').hidden, true);
    dialog.down('#entry-alias').setValue('  <生产>&  ');
    dialog.down('#entry-icon-type').setValue('pve'); dialog.down('#entry-icon-native').setValue('fa fa-server');
    saveDialog(dialog); const write = ui.pending('config', 'PUT');
    assert.equal(write.options.headers['X-PVE-Plugins-CSRF'], 'config-csrf');
    assert.equal(write.options.jsonData.expected_revision, data.revision);
    assert.deepEqual(plain(write.options.jsonData.enabled_plugins), data.enabled_plugins);
    assert.equal('modules' in write.options.jsonData, false);
    assert.deepEqual(plain(write.options.jsonData.navigation.entry_order), data.navigation.entry_order);
    assert.equal(write.options.jsonData.navigation.module_aliases.komodo, '<生产>&');
    assert.deepEqual(plain(write.options.jsonData.navigation.module_icons.komodo), { type: 'pve', value: 'fa fa-server' });
    const saved = replyConfig(ui, write, data);
    assert.equal(dialog.destroyed, true); assert.equal(ui.reloads(), 0); assert.equal(row(page, 'komodo'), original);
    assert.equal(original.get('alias'), '<生产>&'); assert.equal(original.get('name'), 'Komodo');
    assert.equal(page.down('#komodo-url').getValue(), 'https://draft.example');
    assert.equal(grid(page).getStore().getRange()[0].getId(), sitePrefix + a.id);
    page.down('#plugin-manager-save').handler(); const all = ui.pending('config', 'PUT');
    assert.equal(all.options.jsonData.expected_revision, saved.revision);
    assert.equal(all.options.jsonData.modules.komodo.url, 'https://draft.example');
    assert.equal(all.options.jsonData.navigation.entry_order[0], sitePrefix + a.id);
});

test('runtime enable changes guard unsaved connection/order drafts, then proceed after global save', () => {
    const ui = browser(), data = config(), a = site(); const page = ready(ui, data, [a]);
    page.down('#komodo-url').setValue('https://draft.example');
    let dialog = edit(ui, page, 'komodo'); dialog.down('#entry-enabled').setValue(false); saveDialog(dialog);
    assert.equal(ui.pending('config', 'PUT'), undefined); assert.match(dialog.down('#entry-error').getValue(), /未保存/);
    closeDialog(dialog); page.down('#komodo-url').setValue(data.modules.komodo.url);
    grid(page).getSelectionModel().select(row(page, sitePrefix + a.id)); page.down('#plugin-entry-up').handler();
    dialog = edit(ui, page, 'komodo'); dialog.down('#entry-enabled').setValue(false); saveDialog(dialog);
    assert.equal(ui.pending('config', 'PUT'), undefined); assert.match(dialog.down('#entry-error').getValue(), /未保存/);
    closeDialog(dialog); page.down('#plugin-manager-save').handler(); const all = ui.pending('config', 'PUT');
    const saved = replyConfig(ui, all, data);
    dialog = edit(ui, page, 'komodo'); dialog.down('#entry-enabled').setValue(false); saveDialog(dialog);
    const change = ui.pending('config', 'PUT'); assert.deepEqual(plain(change.options.jsonData.enabled_plugins), ['sites']);
    replyConfig(ui, change, saved, 4, true); assert.equal(ui.reloads(), 1);
});

test('module aliases/icons validate before writes and reset to defaults through immediate entity saves', () => {
    const ui = browser(), data = config([]), page = ready(ui, data);
    let dialog = edit(ui, page, 'komodo');
    for (const alias of ['😀'.repeat(101), 'control\u0085', 'line\nbreak']) {
        dialog.down('#entry-alias').setValue(alias); saveDialog(dialog);
        assert.equal(ui.pending('config', 'PUT'), undefined); assert.equal(dialog.destroyed, undefined);
    }
    dialog.down('#entry-alias').setValue('😀'.repeat(100));
    dialog.down('#entry-icon-type').setValue('url'); dialog.down('#entry-icon-url').setValue('javascript:alert(1)');
    saveDialog(dialog); assert.equal(ui.pending('config', 'PUT'), undefined);
    dialog.down('#entry-icon-url').setValue('https://icons.example/a"b.png'); saveDialog(dialog);
    const write = ui.pending('config', 'PUT'), saved = replyConfig(ui, write, data);
    assert.equal(row(page, 'komodo').get('alias'), '😀'.repeat(100));
    assert.equal(row(page, 'komodo').get('icon').value, 'https://icons.example/a"b.png');
    dialog = edit(ui, page, 'komodo'); dialog.down('#entry-alias').setValue(''); dialog.down('#entry-icon-type').setValue('default'); saveDialog(dialog);
    const reset = ui.pending('config', 'PUT');
    assert.equal('komodo' in reset.options.jsonData.navigation.module_aliases, false);
    assert.equal('komodo' in reset.options.jsonData.navigation.module_icons, false);
    replyConfig(ui, reset, saved, 4); assert.equal(row(page, 'komodo').get('alias'), ''); assert.equal(row(page, 'komodo').get('icon'), null);
    assert.equal(ui.reloads(), 0);
});

test('site editor creates and edits complete metadata through site CSRF and revision guards', () => {
    const ui = browser(), a = site(), page = ready(ui, config(), [a]);
    const original = row(page, sitePrefix + a.id); page.down('#komodo-url').setValue('https://draft.example');
    let dialog = edit(ui, page, original.getId());
    assert.equal(dialog.down('#entry-kind').getValue(), '附加站点'); assert.equal(dialog.down('#entry-name').readOnly, false);
    assert.equal(dialog.down('#entry-target').hidden, false);
    dialog.down('#entry-name').setValue('新站点名称'); dialog.down('#entry-target').setValue('https://new-site.example/dashboard');
    dialog.down('#entry-enabled').setValue(false); dialog.down('#entry-alias').setValue('<站点>&');
    dialog.down('#entry-icon-type').setValue('pve'); dialog.down('#entry-icon-native').setValue('fa fa-cloud');
    saveDialog(dialog); const patch = ui.pending('sites/' + a.id, 'PATCH');
    assert.equal(patch.options.headers['X-PVE-Plugins-CSRF'], 'site-csrf');
    assert.deepEqual(plain(patch.options.jsonData), { name: '新站点名称', url: 'https://new-site.example/dashboard', enabled: false,
        alias: '<站点>&', icon: { type: 'pve', value: 'fa fa-cloud' }, expected_revision: 1 });
    const changed = { ...a, ...plain(patch.options.jsonData), revision: 2 }; delete changed.expected_revision;
    ui.respond(patch, { site: changed });
    assert.equal(dialog.destroyed, true); assert.equal(row(page, original.getId()), original);
    assert.equal(original.get('enabled'), false); assert.equal(original.get('alias'), '<站点>&');
    assert.equal(page.down('#komodo-url').getValue(), 'https://draft.example');
    assert.equal(ui.workspace.down('#pve-plugin-manager-tree').getStore().getNodeById(original.getId()).get('status'), '已停用');
    page.down('#plugin-entry-new').handler(); dialog = editor(ui);
    dialog.down('#entry-name').setValue('新增站点'); dialog.down('#entry-target').setValue('https://created.example/');
    saveDialog(dialog); const create = ui.pending('sites', 'POST');
    assert.equal('expected_revision' in create.options.jsonData, false);
    const b = site('b'.repeat(32), plain(create.options.jsonData)); ui.respond(create, { site: b });
    assert.ok(row(page, sitePrefix + b.id)); assert.equal(grid(page).getSelectionModel().getSelection()[0].getId(), sitePrefix + b.id);
    assert.equal(ui.pending('config', 'PUT'), undefined, 'site writes cannot save connection/order drafts');
});

test('site deletion requires confirmation and preserves CAS, table and flat-tree consistency', () => {
    const ui = browser(), a = site(), page = ready(ui, config(), [a]);
    grid(page).getSelectionModel().select(row(page, sitePrefix + a.id));
    page.down('#plugin-entry-delete').handler(); let confirmation = ui.created.findLast((component) => component.dialogOptions);
    confirmation.dialogOptions.fn('no'); assert.equal(ui.pending('sites/' + a.id, 'DELETE'), undefined);
    page.down('#plugin-entry-delete').handler(); confirmation = ui.created.findLast((component) => component.dialogOptions);
    confirmation.dialogOptions.fn('yes'); const remove = ui.pending('sites/' + a.id, 'DELETE');
    assert.equal(remove.options.jsonData.expected_revision, 1); assert.equal(remove.options.headers['X-PVE-Plugins-CSRF'], 'site-csrf');
    ui.respond(remove, { deleted: true, id: a.id });
    assert.equal(row(page, sitePrefix + a.id), undefined);
    assert.equal(ui.workspace.down('#pve-plugin-manager-tree').getStore().getNodeById(sitePrefix + a.id), undefined);
    assert.equal(page.down('#plugin-manager-pages').getLayout().getActiveItem().itemId, 'plugin-page-modules');
});

test('mixed-order moves preserve selected models and one global save updates table/tree/menu order', () => {
    const ui = browser(), data = config([]), a = site(), b = site('b'.repeat(32), { enabled: false });
    const page = ready(ui, data, [a, b]), selected = row(page, sitePrefix + b.id);
    grid(page).getSelectionModel().select(selected);
    for (let index = 0; index < 4; index++) page.down('#plugin-entry-up').handler();
    assert.equal(grid(page).getSelectionModel().getSelection()[0], selected);
    assert.equal(ui.pending('config', 'PUT'), undefined);
    page.down('#plugin-manager-save').handler(); const write = ui.pending('config', 'PUT');
    assert.deepEqual(plain(write.options.jsonData.navigation.entry_order), [sitePrefix + b.id, 'komodo', 'temperatures', 'safeline', sitePrefix + a.id]);
    assert.equal('module_order' in write.options.jsonData.navigation, false); assert.equal('view_order' in write.options.jsonData.navigation, false);
    replyConfig(ui, write, data);
    assert.equal(ui.reloads(), 0); assert.equal(grid(page).getStore().getRange()[0], selected);
    assert.equal(ui.workspace.down('#pve-plugin-manager-tree').getStore().getNodeById('modules').childNodes[0].getId(), selected.getId());
    assert.deepEqual(plain(ui.context.window.PVEPluginManagerNavigation.get().entry_order), plain(write.options.jsonData.navigation.entry_order));
});

test('site conflict preserves input, allows closing and requires a fresh revision before retry', () => {
    const ui = browser(), a = site(), page = ready(ui, config(), [a]);
    const dialog = edit(ui, page, sitePrefix + a.id); dialog.down('#entry-alias').setValue('preserved draft'); saveDialog(dialog);
    const write = ui.pending('sites/' + a.id, 'PATCH'); ui.respond(write, { detail: 'site_revision_conflict' }, 409);
    assert.equal(dialog.down('#entry-alias').getValue(), 'preserved draft'); assert.equal(dialog.down('#entry-cancel').disabled, false);
    const count = ui.requests.length; saveDialog(dialog); assert.equal(ui.requests.length, count);
    closeDialog(dialog); ui.respond(ui.pending('sites'), { user: 'root@pam', sites: [{ ...a, revision: 5 }], csrf_token: 'fresh-site-csrf' });
    const fresh = edit(ui, page, sitePrefix + a.id); saveDialog(fresh);
    assert.equal(ui.pending('sites/' + a.id, 'PATCH').options.jsonData.expected_revision, 5);
});

test('manager refuses missing, partial and legacy navigation instead of generating compatible defaults', () => {
    for (const navigation of [undefined, { entry_order: defaults().entry_order },
        { module_order: ['komodo', 'temperatures', 'safeline', 'sites'], view_order: [] },
        { ...defaults(), entry_order: ['komodo'] }, { ...defaults(), module_aliases: { sites: '旧分组' } }]) {
        const ui = browser(), page = ui.open(), data = config();
        if (navigation === undefined) delete data.navigation; else data.navigation = navigation;
        ui.respond(ui.pending('config'), data);
        assert.equal(ui.pending('sites'), undefined);
        assert.equal(page.down('#plugin-manager-save').disabled, true);
        assert.equal(page.down('#plugin-manager-pages').disabled, true);
        assert.equal(grid(page).getStore().getRange().length, 0);
        assert.equal(ui.warnings.length, 0);
    }
});

test('manager rejects site DTOs missing enabled, alias or icon in lists and write replies', () => {
    for (const field of ['enabled', 'alias', 'icon']) {
        const ui = browser(), data = config(), page = ui.open(), incomplete = site(); delete incomplete[field];
        ui.respond(ui.pending('config'), data);
        ui.respond(ui.pending('sites'), { user: 'root@pam', sites: [incomplete], csrf_token: 'site-csrf' });
        assert.equal(row(page, sitePrefix + incomplete.id), undefined, field);
        assert.equal(page.down('#plugin-manager-save').disabled, true);
        page.down('#plugin-manager-reload').handler(); ready(ui, data, [site()]);
        const dialog = edit(ui, page, sitePrefix + incomplete.id);
        dialog.down('#entry-alias').setValue('保留草稿'); saveDialog(dialog);
        ui.respond(ui.pending('sites/' + incomplete.id, 'PATCH'), { site: { ...incomplete, revision: 2 } });
        assert.equal(dialog.destroyed, undefined); assert.equal(dialog.down('#entry-alias').getValue(), '保留草稿');
        assert.equal(row(page, sitePrefix + incomplete.id).get('revision'), 1);
    }
});

test('module conflict preserves editor input, enables cancel and blocks global duplicate submissions until reread', () => {
    const ui = browser(), page = ready(ui), dialog = edit(ui, page, 'komodo');
    dialog.down('#entry-alias').setValue('preserved alias'); saveDialog(dialog);
    ui.respond(ui.pending('config', 'PUT'), { detail: 'plugin_configuration_changed' }, 409);
    assert.equal(dialog.down('#entry-alias').getValue(), 'preserved alias'); assert.equal(dialog.down('#entry-cancel').disabled, false);
    const count = ui.requests.length; saveDialog(dialog); page.down('#plugin-manager-save').handler(); assert.equal(ui.requests.length, count);
    closeDialog(dialog); page.down('#plugin-manager-reload').handler();
    ui.respond(ui.pending('config'), config()); ui.respond(ui.pending('sites'), { user: 'root@pam', sites: [], csrf_token: 'site-csrf' });
    assert.equal(page.down('#plugin-manager-save').disabled, false);
});

test('configuration validation failures retain connection and order drafts for correction at the same revision', () => {
    const ui = browser(), data = config(), a = site(), page = ready(ui, data, [a]);
    grid(page).getSelectionModel().select(row(page, sitePrefix + a.id));
    page.down('#plugin-entry-up').handler();
    const order = plain(grid(page).getStore().getRange().map((record) => record.getId()));
    page.down('#komodo-url').setValue('not-a-url');
    page.down('#komodo-api_key').setValue('pending-key');
    page.down('#komodo-api_secret').setValue('pending-secret');
    page.down('#plugin-manager-save').handler();
    ui.respond(ui.pending('config', 'PUT'), { detail: 'invalid_plugin_configuration' }, 422);
    assert.equal(page.down('#plugin-manager-pages').disabled, false);
    assert.equal(page.down('#plugin-manager-save').disabled, false);
    assert.equal(page.down('#komodo-url').getValue(), 'not-a-url');
    assert.equal(page.down('#komodo-api_secret').getValue(), 'pending-secret');
    assert.deepEqual(plain(grid(page).getStore().getRange().map((record) => record.getId())), order);
    assert.match(page.down('#plugin-manager-status').text, /修改后可再次保存/);
    assert.equal(ui.pending('config'), undefined, 'validation failure does not reload and replace drafts');
    page.down('#komodo-url').setValue('https://corrected.example');
    page.down('#plugin-manager-save').handler();
    const retry = ui.pending('config', 'PUT');
    assert.equal(retry.options.jsonData.expected_revision, data.revision);
    assert.equal(retry.options.jsonData.modules.komodo.url, 'https://corrected.example');
    assert.equal(retry.options.jsonData.modules.komodo.api_secret, 'pending-secret');
    assert.deepEqual(plain(retry.options.jsonData.navigation.entry_order), order);
    replyConfig(ui, retry, data, 3, true); assert.equal(ui.reloads(), 1);
});

test('module editor validation failures restore save controls without invalidating the configuration revision', () => {
    const ui = browser(), data = config(), page = ready(ui, data), dialog = edit(ui, page, 'komodo');
    dialog.down('#entry-alias').setValue('pending alias'); saveDialog(dialog);
    ui.respond(ui.pending('config', 'PUT'), { detail: 'invalid_plugin_configuration' }, 422);
    assert.equal(dialog.down('#entry-alias').getValue(), 'pending alias');
    assert.equal(dialog.down('#entry-save').disabled, false);
    assert.equal(dialog.down('#entry-cancel').disabled, false);
    assert.equal(page.down('#plugin-manager-pages').disabled, false);
    dialog.down('#entry-alias').setValue('corrected alias'); saveDialog(dialog);
    const retry = ui.pending('config', 'PUT');
    assert.equal(retry.options.jsonData.expected_revision, data.revision);
    assert.equal(retry.options.jsonData.navigation.module_aliases.komodo, 'corrected alias');
    replyConfig(ui, retry, data); assert.equal(dialog.destroyed, true);
});

test('global configuration conflicts and unknown write outcomes still require rereading before retry', () => {
    for (const status of [409, 503]) {
        const ui = browser(), page = ready(ui);
        page.down('#komodo-api_secret').setValue('retained draft');
        page.down('#plugin-manager-save').handler();
        ui.respond(ui.pending('config', 'PUT'), { detail: 'configuration_not_confirmed' }, status);
        assert.equal(page.down('#plugin-manager-pages').disabled, true);
        assert.equal(page.down('#plugin-manager-save').disabled, true);
        assert.equal(page.down('#komodo-api_secret').getValue(), 'retained draft');
        const count = ui.requests.length; page.down('#plugin-manager-save').handler();
        assert.equal(ui.requests.length, count, String(status));
        assert.equal(ui.reloads(), 0);
        page.down('#plugin-manager-reload').handler(); assert.ok(ui.pending('config'));
    }
});

test('config write replies with noncanonical navigation retain the editor and require a reread', () => {
    for (const navigation of [undefined, { entry_order: defaults().entry_order },
        { module_order: ['komodo', 'temperatures', 'safeline', 'sites'], view_order: [] }]) {
        const ui = browser(), data = config(), page = ready(ui, data), dialog = edit(ui, page, 'komodo');
        dialog.down('#entry-alias').setValue('保留输入'); saveDialog(dialog);
        const result = { ...data, revision: data.revision + 1, navigation, reload_required: false };
        if (navigation === undefined) delete result.navigation;
        ui.respond(ui.pending('config', 'PUT'), result);
        assert.equal(dialog.destroyed, undefined); assert.equal(dialog.down('#entry-alias').getValue(), '保留输入');
        assert.equal(dialog.down('#entry-cancel').disabled, false); assert.equal(page.down('#plugin-manager-save').disabled, true);
        assert.equal(ui.reloads(), 0);
        const count = ui.requests.length; saveDialog(dialog); assert.equal(ui.requests.length, count);
    }
});

test('editor cancel and stale handlers after reread, navigation, logout or user change never dispatch writes', () => {
    for (const action of ['cancel', 'reread', 'leave', 'logout', 'account']) {
        const ui = browser(), page = ready(ui), dialog = edit(ui, page, 'komodo');
        dialog.down('#entry-alias').setValue('stale draft'); const submit = dialog.down('#entry-save').handler;
        if (action === 'cancel') closeDialog(dialog);
        else if (action === 'reread') page.down('#plugin-manager-reload').handler();
        else if (action === 'leave') ui.tree.setViewFilter({ id: 'server' });
        else if (action === 'logout') ui.workspace.onLogin(null);
        else ui.context.window.Proxmox.UserName = 'other@pam';
        const count = ui.requests.length; assert.doesNotThrow(submit); assert.equal(ui.requests.length, count, action);
        if (action !== 'account') assert.equal(dialog.destroyed, true, action);
    }
});

test('late site/config writes after leaving cannot update another page or trigger reload', () => {
    const ui = browser(), a = site(), page = ready(ui, config(), [a]), dialog = edit(ui, page, sitePrefix + a.id);
    saveDialog(dialog); const write = ui.pending('sites/' + a.id, 'PATCH');
    ui.tree.setViewFilter({ id: 'server' }); assert.equal(write.aborted, true); assert.equal(dialog.destroyed, true);
    ui.respond(write, { site: { ...a, revision: 2 } }); assert.equal(page.destroyed, true); assert.equal(ui.reloads(), 0);
    const second = ready(ui); const module = edit(ui, second, 'komodo'); saveDialog(module);
    const configWrite = ui.pending('config', 'PUT'); ui.context.window.Proxmox.UserName = 'other@pam';
    replyConfig(ui, configWrite, config(), 3, true); assert.equal(ui.reloads(), 0); assert.equal(second.destroyed, true);
});

test('site notifications update metadata in place without overwriting order and connection drafts', () => {
    const ui = browser(), a = site(), page = ready(ui, config(), [a]), selected = row(page, sitePrefix + a.id);
    grid(page).getSelectionModel().select(selected); page.down('#plugin-entry-up').handler();
    page.down('#komodo-url').setValue('https://draft.example');
    ui.emitSites([{ ...a, alias: '新别名', revision: 3, icon: { type: 'pve', value: 'fa fa-cloud' } }]);
    assert.equal(row(page, selected.getId()), selected); assert.equal(selected.get('alias'), '新别名');
    assert.equal(selected.get('pluginIcon').value, 'fa fa-cloud');
    assert.equal(grid(page).getStore().getRange()[2], selected);
    assert.equal(page.down('#komodo-url').getValue(), 'https://draft.example');
});

test('global connection save preserves credentials redaction and temperature token semantics', () => {
    const ui = browser(), page = ready(ui), key = page.down('#komodo-api_key');
    assert.equal(key.getValue(), ''); assert.equal(key.emptyText, '已设置，留空保留');
    page.down('#plugin-manager-save').handler(); const write = ui.pending('config', 'PUT');
    assert.equal(write.options.jsonData.modules.komodo.api_secret, '');
    assert.equal(write.options.jsonData.modules.temperatures.nodes['pve-a'].token, '');
    assert.equal('token_configured' in write.options.jsonData.modules.temperatures.nodes['pve-a'], false);
    replyConfig(ui, write, config(), 3, true); assert.equal(ui.reloads(), 1);
});

test('SafeLine connection editing and tests cannot be interrupted by global apply or reread', () => {
    const ui = browser(), page = ready(ui, config(['safeline']));
    ui.respond(ui.pending('safeline/connection'), { revision: 0, csrf_token: 'safe-csrf', connection: { url: '', tls_verify: true, tls_server_name: '', ca_pem: '', token_configured: false } });
    page.down('#safe-url').setValue('https://waf.example'); page.down('#safe-token').setValue('new-token');
    page.down('#safe-test').handler(); const write = ui.pending('safeline/connection/test', 'POST');
    const count = ui.requests.length; page.down('#plugin-manager-save').handler(); page.down('#plugin-manager-reload').handler(); assert.equal(ui.requests.length, count);
    assert.equal(write.options.headers['X-PVE-Plugins-CSRF'], 'safe-csrf');
    ui.respond(write, { connected: true }); assert.match(page.down('#plugin-manager-status').text, /连接正常/);
    page.down('#safe-save').handler(); assert.equal(ui.pending('safeline/connection', 'PUT').options.jsonData.token, 'new-token');
});

test('global runtime changes wait for the separate SafeLine draft to be saved', () => {
    const ui = browser(), data = config(['safeline', 'komodo']), page = ready(ui, data);
    const connection = { url: 'https://waf.example', tls_verify: true, tls_server_name: '', ca_pem: '', token_configured: true };
    ui.respond(ui.pending('safeline/connection'), { revision: 1, csrf_token: 'safe-csrf', connection });
    page.down('#safe-url').setValue('https://pending-waf.example');
    page.down('#safe-token').setValue('pending-waf-token');
    page.down('#komodo-url').setValue('https://pending-core.example');
    page.down('#komodo-api_key').setValue('pending-core-key');
    page.down('#komodo-api_secret').setValue('pending-core-secret');
    page.down('#plugin-manager-save').handler();
    assert.equal(ui.pending('config', 'PUT'), undefined);
    assert.equal(ui.reloads(), 0);
    assert.match(page.down('#plugin-manager-status').text, /SafeLine.*未保存.*保存连接/);
    assert.equal(page.down('#safe-token').getValue(), 'pending-waf-token');
    assert.equal(page.down('#komodo-api_secret').getValue(), 'pending-core-secret');
    page.down('#safe-save').handler();
    const safeWrite = ui.pending('safeline/connection', 'PUT');
    assert.equal(safeWrite.options.jsonData.token, 'pending-waf-token');
    ui.respond(safeWrite, { saved: true });
    ui.respond(ui.pending('safeline/connection'), { revision: 2, csrf_token: 'safe-csrf', connection: { ...connection, url: 'https://pending-waf.example' } });
    page.down('#plugin-manager-save').handler();
    const configWrite = ui.pending('config', 'PUT');
    assert.equal(configWrite.options.jsonData.modules.komodo.api_secret, 'pending-core-secret');
    replyConfig(ui, configWrite, data, 3, true); assert.equal(ui.reloads(), 1);
});

test('saving only display order keeps the SafeLine draft and identifies its separate save action', () => {
    const ui = browser(), data = config(['safeline']), a = site(), page = ready(ui, data, [a]);
    ui.respond(ui.pending('safeline/connection'), { revision: 1, csrf_token: 'safe-csrf', connection: {
        url: 'https://waf.example', tls_verify: true, tls_server_name: '', ca_pem: '', token_configured: true,
    } });
    page.down('#safe-token').setValue('pending-token');
    grid(page).getSelectionModel().select(row(page, sitePrefix + a.id));
    page.down('#plugin-entry-up').handler(); page.down('#plugin-manager-save').handler();
    const write = ui.pending('config', 'PUT'); assert.ok(write);
    replyConfig(ui, write, data);
    assert.equal(ui.reloads(), 0);
    assert.equal(page.down('#safe-token').getValue(), 'pending-token');
    assert.match(page.down('#plugin-manager-status').text, /SafeLine.*草稿.*保存连接/);
    assert.equal(ui.pending('safeline/connection', 'PUT'), undefined);
});

test('SafeLine save and test validation failures retain the draft and allow a corrected retry', () => {
    for (const action of ['save', 'test']) {
        const ui = browser(), page = ready(ui, config(['safeline']));
        ui.respond(ui.pending('safeline/connection'), { revision: 4, csrf_token: 'safe-csrf', connection: {
            url: 'https://waf.example', tls_verify: true, tls_server_name: '', ca_pem: '', token_configured: true,
        } });
        page.down('#safe-url').setValue('not-a-url'); page.down('#safe-token').setValue('pending-token');
        page.down('#safe-' + action).handler();
        const path = 'safeline/connection' + (action === 'test' ? '/test' : ''), method = action === 'test' ? 'POST' : 'PUT';
        ui.respond(ui.pending(path, method), { detail: 'invalid_connection' }, 422);
        assert.equal(page.down('#safe-save').disabled, false);
        assert.equal(page.down('#safe-test').disabled, false);
        assert.equal(page.down('#safe-token').getValue(), 'pending-token');
        assert.equal(ui.pending('safeline/connection'), undefined);
        page.down('#safe-url').setValue('https://corrected-waf.example'); page.down('#safe-' + action).handler();
        const retry = ui.pending(path, method);
        assert.equal(retry.options.jsonData.expected_revision, 4);
        assert.equal(retry.options.jsonData.url, 'https://corrected-waf.example');
        assert.equal(retry.options.jsonData.token, 'pending-token');
    }
});

test('SafeLine conflicts and unknown write outcomes still block repeated writes until reread', () => {
    for (const status of [409, 503]) {
        const ui = browser(), page = ready(ui, config(['safeline']));
        ui.respond(ui.pending('safeline/connection'), { revision: 1, csrf_token: 'safe-csrf', connection: {
            url: 'https://waf.example', tls_verify: true, tls_server_name: '', ca_pem: '', token_configured: true,
        } });
        page.down('#safe-token').setValue('retained-token'); page.down('#safe-save').handler();
        ui.respond(ui.pending('safeline/connection', 'PUT'), { detail: 'connection_not_confirmed' }, status);
        assert.equal(page.down('#safe-save').disabled, true);
        assert.equal(page.down('#safe-test').disabled, true);
        assert.equal(page.down('#safe-token').getValue(), 'retained-token');
        const count = ui.requests.length; page.down('#safe-save').handler(); page.down('#safe-test').handler();
        assert.equal(ui.requests.length, count, String(status));
    }
});

test('temperature source editor validates and clears tokens in the explicit global save payload', () => {
    const ui = browser(), page = ready(ui, config(['temperatures']));
    page.down('#plugin-temperature-grid').tbar[0].handler(); const node = ui.created.findLast((component) => component.title === '新增温度来源');
    node.down('#node').setValue('invalid_node'); node.down('#url').setValue('https://new-metrics.example'); node.buttons[1].handler();
    assert.equal(node.destroyed, undefined);
    node.down('#node').setValue('pve-b'); node.down('#type').setValue('node_exporter'); node.down('#allow_http').setValue(false); node.down('#clear_token').setValue(true);
    node.buttons[1].handler(); assert.equal(node.destroyed, true);
    page.down('#plugin-manager-save').handler();
    assert.deepEqual(plain(ui.pending('config', 'PUT').options.jsonData.modules.temperatures.nodes['pve-b']),
        { url: 'https://new-metrics.example', type: 'node_exporter', allow_http: false, token: '', clear_token: true });
});

test('native construction restores manager after parent attachment and login, then cancels reads on logout', () => {
    const ui = browser({ restoredView: viewId, loggedIn: false });
    assert.equal(ui.workspace.page, undefined); assert.equal(ui.requests.length, 0);
    ui.workspace.onLogin({ username: 'root@pam' }); const page = ready(ui);
    assert.equal(page.ownerCt, ui.content); assert.equal(ui.content.getLayout().getActiveItem(), page);
    assert.equal(ui.siteSubscribers.size, 1);
    page.down('#plugin-manager-reload').handler(); const read = ui.pending('config');
    ui.workspace.onLogin(null); assert.equal(read.aborted, true); assert.equal(page.destroyed, true); assert.equal(ui.siteSubscribers.size, 0);
    ui.respond(read, config()); assert.equal(ui.content.children.length, 0); assert.equal(ui.warnings.length, 0);
});
