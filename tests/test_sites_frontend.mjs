import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../plugins/sites/static/sites.js', import.meta.url), 'utf8');
const viewId = 'pve-extra-sites';
const api = '/pve-plugins/api/sites';

function browser() {
    const classes = new Map(), stack = [], requests = [], dialogs = [], warnings = [], removedLater = [], loadedFrames = [];
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
        up: function (xtype) { for (let parent = this.ownerCt; parent; parent = parent.ownerCt) if (parent.xtype === xtype) return parent; return null; },
        setVisible: function (visible) { this.hidden = !visible; this.fire(visible ? 'show' : 'hide'); },
        setDisabled: function (disabled) { assert.equal(this.destroyed, false); this.disabled = disabled; },
        setText: function (text) { assert.equal(this.destroyed, false); this.text = text; },
        getValue: function () { return this.value; }, setValue: function (value) { this.value = value; },
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
        const result = { rows: [], loadData(data) {
            this.rows = data.map((data) => ({ data, get: (key) => data[key], getId: () => data.id }));
            if (grid?.selection) grid.selection.selected = [];
        }, getById(id) { return this.rows.find((row) => row.data.id === id); } };
        result.loadData(config.data || []); return result;
    }
    const xtypeClasses = { component: 'Ext.Component', button: 'Ext.Component', textfield: 'Ext.Component', tbtext: 'Ext.Component', panel: 'Ext.panel.Panel' };
    function create(name, config = {}) {
        const component = new (classes.get(name))(config);
        component.initComponent();
        for (const [event, handler] of Object.entries(config.listeners || {})) component.on(event, handler);
        return component;
    }
    function child(config, owner) { return create(xtypeClasses[config.xtype] || 'Ext.Component', { ...config, ownerCt: owner }); }
    define('Ext.panel.Panel', {
        extend: 'Ext.Component',
        initComponent: function () {
            this.children = [...(this.items || []), ...(this.tbar || []), ...(this.bbar || []), ...(this.buttons || [])]
                .filter((item) => item && typeof item === 'object').map((item) => child(item, this));
            return 'native-panel-result';
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
        extend: 'Ext.panel.Panel',
        initComponent: function () {
            const rows = ['server', 'folder', 'pool', 'tags', 'pve-komodo-containers'].map((key) => ({ key, value: key }));
            this.dataStore = { rows, findRecord(field, value) { return rows.find((row) => row[field] === value); }, add(row) { rows.push(row); } };
            this.value = 'server'; return this.callParent(arguments);
        }, getStore: function () { return this.dataStore; },
        applyState: function (value) { if (this.dataStore.findRecord('key', value)) this.value = value; },
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
        this._pveKomodoContainerTree = { tree: create('Ext.panel.Panel', { hidden: true }) };
        this.setViewFilter = function (view) {
            const result = previous.apply(this, arguments);
            const containerView = view?.id === 'pve-komodo-containers';
            this.setVisible(!containerView); this._pveKomodoContainerTree.tree.setVisible(containerView);
            return result;
        };
        return result;
    } });
    const Ext = { ClassManager: { get: (name) => classes.get(name) }, define, Msg: { YESNO: 'yesno', WARNING: 'warning' },
        create: (name, config) => create(name.startsWith('Ext.window.') ? 'Ext.panel.Panel' : name, config),
        Ajax: { request(options) { const request = { options }; requests.push(request); return request; }, abort(request) {
            request.aborted = true; request.options.callback(null, false, { status: 0, responseText: '' });
        } },
    };
    context = vm.createContext({ window: { Ext, URL, location: { protocol: 'https:', origin: 'https://pve.test' },
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
    return { Ext, classes, context, requests, dialogs, warnings, loadedFrames, removedLater, setup, respond,
        create, load: () => vm.runInContext(source, context), deferRender(value) { deferContentRender = value; },
        flushRemovals() { removedLater.splice(0).forEach((page) => page.destroy()); },
    };
}
function site(id = 'a', name = 'Grafana', url = 'https://grafana.example/d/default?orgId=1', revision = 1) {
    return { id: id.repeat(32), name, url, revision };
}
function list(sites = [site()], user = 'root@pam') { return { user, sites, csrf_token: 'site-session-csrf' }; }
function editorButton(editor, name) { return editor.down('#pve-extra-sites-' + name); }
function pageButton(view, name) { return view.page.down('#pve-extra-sites-' + name); }
function setEditor(editor, name, url) { editorButton(editor, 'name').setValue(name); editorButton(editor, 'url').setValue(url); }
function ready(ui = browser(), sites = [site()]) { ui.load(); const view = ui.setup(); view.switch(viewId); ui.respond(list(sites)); return { ui, view }; }

test('selector registration is idempotent and retains every native and container view', () => {
    const ui = browser(); ui.load(); ui.load();
    const selector = ui.create('PVE.form.ViewSelector');
    assert.deepEqual(selector.getStore().rows.map((row) => row.key), ['server', 'folder', 'pool', 'tags', 'pve-komodo-containers', viewId]);
    assert.equal(selector.getStore().rows.at(-1).value, '附加站点');
    selector.applyState(viewId); assert.equal(selector.getViewFilter().id, viewId);
    assert.equal(ui.requests.length, 0); assert.equal(ui.warnings.length, 0);
});

test('sites -> containers -> native views delegate correctly without changing native ids or hash', () => {
    const { ui, view } = ready(); const hash = view.workspace.hash;
    assert.equal(view.native.hidden, true); assert.equal(view.panel.hidden, false);
    assert.equal(view.native._pveKomodoContainerTree.tree.hidden, true);
    const page = view.page; view.native.resourceTick(); assert.equal(view.page, page, 'hidden native refresh must not replace the frame');
    assert.equal(view.switch('pve-komodo-containers'), 'native-filter-result');
    assert.equal(view.panel.hidden, true); assert.equal(view.native.hidden, true); assert.equal(view.native._pveKomodoContainerTree.tree.hidden, false);
    assert.equal(view.switch('server'), 'native-filter-result');
    assert.equal(view.native.hidden, false); assert.equal(view.native._pveKomodoContainerTree.tree.hidden, true);
    assert.equal(view.page.nativePage, true);
    view.switch(viewId); ui.respond(list()); assert.equal(view.frame.attributes.src, site().url);
    assert.equal(view.workspace.hash, hash); assert.deepEqual(view.native.nativeIds, ['root', 'node/pve-a', 'qemu/102']);
});

test('persisted sites view loads a fit iframe with DOM attributes and the site action toolbar', () => {
    const ui = browser(); ui.load(); const view = ui.setup(viewId);
    assert.equal(ui.requests[0].options.url, api);
    const entry = site('a', '<Grafana & "team">', 'https://grafana.example/?q=%22%3E%3Cimg%3E'); ui.respond(list([entry]));
    assert.equal(view.page.layout, 'fit'); assert.equal(view.page.header, false);
    for (const [action, text] of [['refresh', '刷新'], ['page-edit', '编辑'], ['remove', '移除']]) {
        const button = pageButton(view, action);
        assert.equal(button.text, text); assert.equal(button.disabled, false);
    }
    assert.equal(view.page.bbar, undefined);
    assert.equal(view.frame.attributes.src, entry.url); assert.equal(view.frame.attributes.title, entry.name);
    assert.equal(view.frame.attributes.referrerpolicy, 'no-referrer');
    assert.equal(view.frame.attributes.allow, 'fullscreen');
    assert.match(view.frame.attributes.sandbox, /allow-scripts.*allow-forms.*allow-same-origin.*allow-popups.*allow-downloads/);
    assert.doesNotMatch(view.frame.attributes.sandbox, /allow-top-navigation|allow-popups-to-escape-sandbox/);
    assert.equal(view.panel.columns[0].renderer(entry.name), '&lt;Grafana &amp; &quot;team&quot;&gt;');
    assert.equal(ui.requests.length, 1, 'only the local list API is requested; the target belongs exclusively to the iframe');
});

test('keyboard selection changes the displayed site and clicking that same row does not reload it', () => {
    const second = site('b', 'Logs', 'https://logs.example/');
    const { ui, view } = ready(browser(), [site(), second]);
    assert.equal(ui.loadedFrames.length, 1, 'initial selection mounts one iframe');
    const record = view.panel.getStore().getById(second.id);
    view.panel.getSelectionModel().select(record);
    assert.equal(view.frame.attributes.src, second.url);
    assert.equal(view.state.selected, second.id);
    const page = view.page;
    view.panel.fire('itemclick', null, record);
    assert.equal(view.page, page); assert.equal(ui.loadedFrames.length, 2);
    pageButton(view, 'page-edit').handler();
    assert.equal(editorButton(view.editor, 'name').getValue(), second.name);
    assert.equal(editorButton(view.editor, 'url').getValue(), second.url);
    assert.ok(editorButton(view.editor, 'delete'), 'the page action opens the existing edit form');
    assert.equal(ui.requests.length, 1, 'opening the edit form does not reload or write the list');
});

test('page refresh rebuilds only the selected iframe without reading or writing the list', () => {
    const second = site('b', 'Logs', 'https://logs.example/', 3);
    const { ui, view } = ready(browser(), [site(), second]);
    view.select(second.id);
    const oldPage = view.page, oldFrame = view.frame, hash = view.workspace.hash;
    const requests = ui.requests.length, frames = ui.loadedFrames.length;
    pageButton(view, 'refresh').handler();
    assert.notEqual(view.page, oldPage); assert.notEqual(view.frame, oldFrame);
    assert.equal(oldFrame.attributes.src, 'about:blank');
    assert.equal(view.frame.attributes.src, second.url); assert.equal(view.state.selected, second.id);
    assert.equal(ui.loadedFrames.length, frames + 1);
    assert.equal(ui.requests.length, requests, 'iframe refresh performs no backend operation');
    assert.equal(view.workspace.hash, hash);
});

test('new site has two fields and no delete; invalid URLs never submit, and save uses session CSRF', () => {
    const { ui, view } = ready(browser(), []); view.press('new'); const editor = view.editor;
    assert.equal(editorButton(editor, 'delete'), null);
    for (const url of ['javascript:alert(1)', 'data:text/html,a', 'ftp://example/', 'https://user:pass@example/', 'https://example/\\evil']) {
        setEditor(editor, 'Site', url); editorButton(editor, 'save').handler();
    }
    assert.equal(ui.requests.length, 1);
    assert.match(editorButton(editor, 'editor-status').html, /有效的 HTTP/);
    setEditor(editor, '  Logs  ', 'https://logs.example/query?x=1#tail'); editorButton(editor, 'save').handler();
    const request = ui.requests.at(-1).options;
    assert.equal(request.method, 'POST'); assert.equal(request.url, api);
    assert.deepEqual(JSON.parse(JSON.stringify(request.jsonData)), { name: 'Logs', url: 'https://logs.example/query?x=1#tail' });
    assert.equal(request.headers['X-PVE-Plugins-CSRF'], 'site-session-csrf');
    editor.close(); assert.equal(editor.destroyed, false, 'a pending save cannot be accidentally dismissed');
    editorButton(editor, 'save').handler(); assert.equal(ui.requests.length, 2, 'pending writes are not duplicated');
    const saved = site('b', 'Logs', request.jsonData.url); ui.respond({ site: saved });
    assert.equal(editor.destroyed, true); assert.equal(ui.requests.at(-1).options.method, 'GET');
    ui.respond(list([saved])); assert.equal(view.frame.attributes.src, saved.url);
});

test('edit sends the selected revision and successful URL changes replace the previous frame', () => {
    const { ui, view } = ready(); const oldFrame = view.frame; view.press('edit'); const editor = view.editor;
    assert.equal(editorButton(editor, 'name').getValue(), 'Grafana'); assert.ok(editorButton(editor, 'delete'));
    setEditor(editor, 'Grafana updated', 'https://grafana.example/new'); editorButton(editor, 'save').handler();
    const request = ui.requests.at(-1).options;
    assert.equal(request.method, 'PATCH'); assert.equal(request.url, api + '/' + site().id);
    assert.equal(request.jsonData.expected_revision, 1);
    const updated = site('a', 'Grafana updated', 'https://grafana.example/new', 2);
    ui.respond({ site: updated }); ui.respond(list([updated]));
    assert.equal(oldFrame.attributes.src, 'about:blank'); assert.equal(view.frame.attributes.src, updated.url);
});

test('the edit form retains confirmation before a revision-bound DELETE', () => {
    const { ui, view } = ready(); assert.equal(view.panel.down('#pve-extra-sites-delete'), null);
    view.press('edit'); const editor = view.editor; editorButton(editor, 'delete').handler();
    const cancelled = ui.dialogs.at(-1); cancelled.options.fn('no'); assert.equal(ui.requests.length, 1);
    editorButton(editor, 'delete').handler(); const confirmation = ui.dialogs.at(-1);
    confirmation.options.fn('yes'); confirmation.options.fn('yes');
    const request = ui.requests.at(-1).options;
    assert.equal(request.method, 'DELETE'); assert.equal(request.url, api + '/' + site().id);
    assert.deepEqual(JSON.parse(JSON.stringify(request.jsonData)), { expected_revision: 1 });
    assert.equal(ui.requests.length, 2);
    ui.respond({ deleted: true, id: site().id }); ui.respond(list([]));
    assert.equal(view.frame, undefined); assert.match(view.page.html, /选择左侧站点/);
});

test('page removal confirms first and deletes only the selected site and revision', () => {
    const second = site('b', 'Logs <team>', 'https://logs.example/', 3);
    const { ui, view } = ready(browser(), [site(), second]); view.select(second.id);
    const frame = view.frame;
    pageButton(view, 'remove').handler();
    const cancelled = ui.dialogs.at(-1);
    assert.equal(view.editor, null, 'removal does not need an edit form');
    assert.match(cancelled.options.message, /Logs &lt;team&gt;/);
    assert.equal(ui.requests.length, 1, 'confirmation itself does not write');
    for (const action of ['refresh', 'page-edit', 'remove']) assert.equal(pageButton(view, action).disabled, true);
    cancelled.options.fn('no');
    assert.equal(ui.requests.length, 1); assert.equal(view.frame, frame);
    for (const action of ['refresh', 'page-edit', 'remove']) assert.equal(pageButton(view, action).disabled, false);
    pageButton(view, 'remove').handler(); const confirmation = ui.dialogs.at(-1);
    confirmation.options.fn('yes'); confirmation.options.fn('yes');
    assert.equal(ui.requests.length, 2, 'a repeated confirmation callback cannot duplicate the write');
    const request = ui.requests.at(-1).options;
    assert.equal(request.method, 'DELETE'); assert.equal(request.url, api + '/' + second.id);
    assert.deepEqual(JSON.parse(JSON.stringify(request.jsonData)), { expected_revision: 3 });
    assert.equal(request.headers['X-PVE-Plugins-CSRF'], 'site-session-csrf');
    ui.respond({ deleted: true, id: second.id });
    assert.equal(ui.requests.at(-1).options.method, 'GET', 'the confirmed result is followed by an authoritative list read');
    ui.respond(list([site()]));
    assert.equal(frame.attributes.src, 'about:blank'); assert.equal(view.frame.attributes.src, site().url);
});

test('an old removal confirmation cannot remove another selection or survive logout', () => {
    const second = site('b', 'Logs', 'https://logs.example/', 3);
    const { ui, view } = ready(browser(), [site(), second]);
    pageButton(view, 'remove').handler(); const oldConfirmation = ui.dialogs.at(-1);
    view.select(second.id);
    assert.equal(oldConfirmation.destroyed, true);
    pageButton(view, 'remove').handler(); const currentConfirmation = ui.dialogs.at(-1);
    oldConfirmation.options.fn('yes');
    assert.equal(ui.requests.length, 1);
    assert.equal(currentConfirmation.destroyed, false, 'a late callback must not consume the current confirmation');
    assert.equal(pageButton(view, 'remove').disabled, true);
    view.workspace.onLogin(null);
    assert.equal(currentConfirmation.destroyed, true);
    currentConfirmation.options.fn('yes');
    assert.equal(ui.requests.length, 1, 'a logged-out confirmation cannot send the previous session token');
});

test('page actions are disabled during a write and stale handlers cannot bypass that guard', () => {
    const { ui, view } = ready();
    const buttons = ['refresh', 'page-edit', 'remove'].map((action) => pageButton(view, action));
    buttons[1].handler(); const editor = view.editor, frame = view.frame;
    setEditor(editor, 'Updated', 'https://updated.example/'); editorButton(editor, 'save').handler();
    assert.ok(buttons.every((button) => button.disabled));
    const requests = ui.requests.length, dialogs = ui.dialogs.length, frames = ui.loadedFrames.length;
    buttons.forEach((button) => button.handler());
    assert.equal(ui.requests.length, requests); assert.equal(ui.dialogs.length, dialogs);
    assert.equal(ui.loadedFrames.length, frames); assert.equal(view.editor, editor); assert.equal(view.frame, frame);
    const updated = site('a', 'Updated', 'https://updated.example/', 2);
    ui.respond({ site: updated }); ui.respond(list([updated]));
    for (const action of ['refresh', 'page-edit', 'remove']) assert.equal(pageButton(view, action).disabled, false);
});

test('page action handlers cannot run after a session change, view switch or card deactivation', () => {
    for (const change of ['user', 'auth', 'view', 'card']) {
        const { ui, view } = ready();
        const buttons = ['refresh', 'page-edit', 'remove'].map((action) => pageButton(view, action));
        const frame = view.frame;
        if (change === 'user') ui.context.window.Proxmox.UserName = 'other@pam';
        if (change === 'auth') ui.context.window.Proxmox.Utils.authOK = () => false;
        if (change === 'view') view.switch('server');
        if (change === 'card') view.workspace.setContent({ nativePage: true });
        buttons.forEach((button) => button.handler());
        assert.equal(ui.requests.length, 1, change + ': stale controls cannot call the backend');
        assert.equal(ui.loadedFrames.length, 1, change + ': stale refresh cannot remount the iframe');
        assert.equal(ui.dialogs.length, 0, change + ': stale edit and remove cannot open dialogs');
        assert.equal(view.editor, null);
        if (change === 'view' || change === 'card') assert.equal(frame.attributes.src, 'about:blank');
    }
});

test('conflicts retain drafts but never retry; closing reloads once for a new authoritative revision', () => {
    const { ui, view } = ready(); view.press('edit'); const editor = view.editor;
    setEditor(editor, 'Draft', 'https://draft.example/'); editorButton(editor, 'save').handler();
    ui.respond({ detail: 'site_revision_conflict' }, ui.requests.length - 1, 409);
    assert.equal(editorButton(editor, 'name').getValue(), 'Draft');
    assert.match(editorButton(editor, 'editor-status').html, /输入已保留/);
    assert.equal(editorButton(editor, 'save').disabled, true);
    editorButton(editor, 'save').handler(); assert.equal(ui.requests.length, 2);
    editor.close(); assert.equal(ui.requests.length, 3); assert.equal(ui.requests.at(-1).options.method, 'GET');
    ui.respond(list([site('a', 'Other admin update', 'https://new.example/', 2)]));
    view.press('edit'); assert.equal(editorButton(view.editor, 'name').getValue(), 'Other admin update');
});

test('unknown write outcome locks replay and never displays an untrusted error body', () => {
    const { ui, view } = ready(browser(), []); view.press('new'); const editor = view.editor;
    setEditor(editor, 'New', 'https://new.example/'); editorButton(editor, 'save').handler();
    ui.respond('PRIVATE <script>failed</script>', ui.requests.length - 1, 502);
    assert.match(editorButton(editor, 'editor-status').html, /结果可能已保存/);
    assert.doesNotMatch(editorButton(editor, 'editor-status').html, /PRIVATE|<script>/);
    editorButton(editor, 'save').handler(); assert.equal(ui.requests.filter((request) => request.options.method === 'POST').length, 1);
});

test('read authorization failures and invalid public DTOs do not create iframes or enable editing', () => {
    for (const [status, payload] of [[401, {}], [403, {}], [200, list([site()], 'other@pam')], [200, list([site('a', 'Unsafe', 'javascript:alert(1)')])]]) {
        const ui = browser(); ui.load(); const view = ui.setup(); view.switch(viewId); ui.respond(payload, 0, status);
        assert.equal(view.frame, undefined); assert.equal(view.panel.down('#pve-extra-sites-new').disabled, true);
        assert.equal(ui.loadedFrames.length, 0);
    }
});

test('write authorization loss immediately clears prior frame, URLs and editor', () => {
    const { ui, view } = ready(); const frame = view.frame; view.press('edit'); const editor = view.editor;
    setEditor(editor, 'Changed', 'https://changed.example/'); editorButton(editor, 'save').handler();
    ui.respond({ detail: 'invalid_csrf_token' }, ui.requests.length - 1, 403);
    assert.equal(frame.attributes.src, 'about:blank'); assert.equal(editor.destroyed, true);
    assert.equal(view.panel.getStore().rows.length, 0); assert.equal(view.state.csrf, null);
});

test('hide and deactivate stop an iframe before native delayed card destruction, including fullscreen', () => {
    const { ui, view } = ready(); const oldPage = view.page, frame = view.frame;
    ui.context.window.document.fullscreenElement = frame;
    view.workspace.setContent({ nativePage: true });
    assert.equal(oldPage.destroyed, false); assert.equal(frame.attributes.src, 'about:blank');
    assert.equal(ui.context.window.document.exits, 1);
    ui.flushRemovals(); assert.equal(oldPage.destroyed, true);
});

test('logout aborts reads and writes, closes confirmation, clears frames and ignores late results', () => {
    const { ui, view } = ready(); const frame = view.frame; view.press('edit'); const editor = view.editor;
    editorButton(editor, 'delete').handler(); const confirmation = ui.dialogs.at(-1);
    assert.equal(view.workspace.onLogin(null), 'native-login-result');
    assert.equal(editor.destroyed, true); assert.equal(confirmation.destroyed, true); assert.equal(frame.attributes.src, 'about:blank');
    confirmation.options.fn('yes'); assert.equal(ui.requests.length, 1);
    view.workspace.onLogin({ username: 'root@pam' }); const read = ui.requests.at(-1);
    view.workspace.onLogin(null); assert.equal(read.aborted, true);
    ui.respond(list()); assert.equal(view.frame, undefined);
    view.workspace.onLogin({ username: 'other@pam' }); ui.respond(list([], 'other@pam'));
    view.press('new'); const newEditor = view.editor;
    setEditor(newEditor, 'New', 'https://new.example/'); editorButton(newEditor, 'save').handler(); const write = ui.requests.at(-1);
    view.workspace.onLogin(null); assert.equal(write.aborted, true); assert.equal(newEditor.destroyed, true);
    ui.respond({ site: site('b') }); assert.equal(view.state.sites.length, 0);
});

test('an in-flight write also clears controls and private content if identity or auth changes without onLogin', () => {
    for (const change of ['user', 'auth']) {
        const { ui, view } = ready(); const frame = view.frame;
        view.press('edit'); const editor = view.editor;
        setEditor(editor, 'Updated', 'https://updated.example/'); editorButton(editor, 'save').handler();
        if (change === 'user') ui.context.window.Proxmox.UserName = 'other@pam';
        else ui.context.window.Proxmox.Utils.authOK = () => false;
        ui.respond({ site: site('a', 'Updated', 'https://updated.example/', 2) });
        assert.equal(view.state.write, null); assert.equal(view.state.csrf, null); assert.equal(view.state.active, false);
        assert.equal(editor.destroyed, true); assert.equal(frame.attributes.src, 'about:blank');
        assert.equal(ui.requests.length, 2, 'a changed session must not trigger another request with the old token');
    }
});

test('destroy and late render cannot leave a hidden or stale-user iframe running', () => {
    const { ui, view } = ready(); const frame = view.frame;
    view.native.destroy(); assert.equal(frame.attributes.src, 'about:blank'); assert.equal(view.panel.destroyed, true);
    const other = browser(); other.load(); const pending = other.setup(); pending.switch(viewId);
    other.deferRender(true); other.respond(list()); const deferred = pending.page;
    pending.switch('server'); deferred.render();
    assert.equal(deferred.destroyed, true); assert.equal(other.loadedFrames.length, 0);
    const late = browser(); late.load(); const userChanged = late.setup(); userChanged.switch(viewId);
    late.context.window.Proxmox.UserName = 'other@pam'; late.respond(list());
    assert.equal(late.loadedFrames.length, 0); assert.equal(userChanged.state.csrf, null);
});

test('HTTP embedding limits are stated without treating iframe load as successful rendering', () => {
    const { ui, view } = ready(browser(), [site('a', 'Legacy', 'http://legacy.example/')]);
    assert.match(view.panel.down('#pve-extra-sites-status').text, /HTTPS.*HTTP/);
    view.press('edit'); assert.match(view.editor.items[2].html, /禁止嵌入/);
    assert.equal(view.page.down('#pve-extra-sites-frame').events.has('load'), false);
    const unsupported = browser(); unsupported.classes.delete('PVE.tree.ResourceTree');
    assert.doesNotThrow(() => unsupported.load()); assert.equal(unsupported.requests.length, 0);
});
