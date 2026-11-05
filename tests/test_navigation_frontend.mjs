import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../app/static/navigation.js', import.meta.url), 'utf8');
const siteA = 'pve-extra-site-' + 'a'.repeat(32);
const siteB = 'pve-extra-site-' + 'b'.repeat(32);
const defaults = {
    entry_order: ['komodo', 'temperatures', 'safeline'],
    module_aliases: {},
    module_icons: {},
};
const legacyDefaults = { module_order: ['komodo', 'temperatures', 'safeline', 'sites'], view_order: ['pve-plugin-manager', 'pve-komodo-containers', 'pve-safeline-waf'] };
const plain = (value) => JSON.parse(JSON.stringify(value));

function documentFixture() {
    function element(tag) {
        const classes = new Set();
        return { tagName: tag.toUpperCase(), children: [], attributes: {}, hidden: false, innerHTML: '',
            classList: { add: (...values) => values.forEach((value) => classes.add(value)), contains: (value) => classes.has(value),
                toggle(value, enabled) { if (enabled) classes.add(value); else classes.delete(value); } },
            set className(value) { classes.clear(); value.split(/\s+/).forEach((name) => classes.add(name)); },
            setAttribute(name, value) { this.attributes[name] = value; },
            appendChild(child) { child.parentNode = this; this.children.push(child); },
            insertBefore(child, before) { child.parentNode = this; this.children.splice(this.children.indexOf(before), 0, child); },
            remove() { if (this.parentNode) this.parentNode.children.splice(this.parentNode.children.indexOf(this), 1); this.parentNode = null; },
        };
    }
    return { head: element('head'), handlers: [], createElement: element,
        addEventListener(name, callback, capture) { this.handlers.push({ name, callback, capture }); } };
}
function browser({ dom = false } = {}) {
    const classes = new Map(), stack = [], requests = [], siteUpdates = [], warnings = [];
    const window = { URL, Proxmox: { UserName: 'root@pam', Utils: { authOK: () => true } },
        PVEPluginSites: { applyOrder: (workspace) => siteUpdates.push(workspace) },
        console: { warn: (...args) => warnings.push(args) } };
    if (dom) window.document = documentFixture();
    function Selector(workspace, keys) {
        const selector = this;
        this.workspace = workspace;
        this.listConfig = { maxHeight: 250 };
        this.events = new Map();
        this.updates = new Set();
        this.rows = keys.map((key) => ({ data: { key, value: key }, get(field) { return this.data[field]; },
            set(field, value) { this.data[field] = value; selector.updates.forEach((callback) => callback()); } }));
        this.selected = this.rows.find((row) => row.get('key') === siteA);
        this.store = { getRange: () => this.rows.slice(), sort: (sorter) => { assert.equal(sorter.direction, 'ASC'); this.rows.sort(sorter.sorterFn); },
            on: (name, callback) => { if (name === 'update') this.updates.add(callback); },
            un: (name, callback) => { if (name === 'update') this.updates.delete(callback); },
            remove: () => { throw new Error('Sorting must not remove a record'); } };
        if (dom) {
            this.inputEl = { dom: window.document.createElement('input') };
            window.document.createElement('div').appendChild(this.inputEl.dom);
        }
    }
    Selector.prototype = {
        initComponent() { return 'native-init'; }, getStore() { return this.store; }, up() { return this.workspace; },
        getValue() { return this.selected ? this.selected.get('key') : null; },
        on(name, callback) { const handlers = this.events.get(name) || []; handlers.push(callback); this.events.set(name, handlers); },
        fire(name) { for (const callback of this.events.get(name) || []) callback(); },
        setValue() { throw new Error('Sorting must not change combo value'); },
        fireEvent() { throw new Error('Sorting must not fire a select event'); },
        callParent(args) { return stack.at(-1).apply(this, args); },
    };
    function Workspace() {}
    Workspace.prototype = { onLogin(data) { window.Proxmox.UserName = data ? data.username : null; return 'native-login'; },
        callParent(args) { return stack.at(-1).apply(this, args); } };
    classes.set('PVE.form.ViewSelector', Selector); classes.set('PVE.StdWorkspace', Workspace);
    window.Ext = { ClassManager: { get: (name) => classes.get(name) },
        define(name, config) {
            const target = classes.get(config.override);
            for (const [method, callback] of Object.entries(config)) {
                if (typeof callback !== 'function') continue;
                const previous = target.prototype[method];
                target.prototype[method] = function (...args) {
                    stack.push(previous);
                    try { return callback.apply(this, args); } finally { stack.pop(); }
                };
            }
        },
        Ajax: { request(options) { const request = { options, aborted: false }; requests.push(request); return request; },
            abort(request) { request.aborted = true; } },
    };
    const context = vm.createContext({ window }); vm.runInContext(source, context);
    function selector(workspace = new Workspace(), keys = ['server', 'tags', 'pve-plugin-manager', 'pve-komodo-containers', 'pve-safeline-waf', siteA, siteB]) {
        const result = new Selector(workspace, keys); assert.equal(result.initComponent(), 'native-init'); return result;
    }
    function reply(request, navigation, revision, success = true) {
        request.options.callback({}, success, { responseText: JSON.stringify({ navigation, revision }) });
    }
    return { window, context, selector, reply, requests, siteUpdates, warnings, Workspace, api: window.PVEPluginManagerNavigation };
}

test('sorts plugin views in place after fixed native views and preserves selected record', () => {
    const ui = browser(), selector = ui.selector(), selected = selector.selected;
    selector.fire('afterrender');
    assert.equal(ui.requests.length, 1);
    assert.equal(ui.requests[0].options.method, 'GET');
    assert.equal(ui.requests[0].options.url, '/pve-plugins/api/navigation');
    assert.equal(ui.requests[0].options.headers, undefined);
    const navigation = { ...defaults, entry_order: [siteB, 'safeline', siteA, 'temperatures', 'komodo'] };
    ui.reply(ui.requests[0], navigation, 3);
    assert.deepEqual(selector.rows.map((row) => row.get('key')), ['server', 'tags', 'pve-plugin-manager', siteB, 'pve-safeline-waf', siteA, 'pve-komodo-containers']);
    assert.equal(selector.selected, selected);
    assert.ok(selector.rows.includes(selected));
    const sites = [{ id: 'a'.repeat(32) }, { id: 'c'.repeat(32) }, { id: 'b'.repeat(32) }];
    assert.deepEqual(ui.api.sortSites(sites).map((site) => site.id), ['b'.repeat(32), 'a'.repeat(32), 'c'.repeat(32)]);
    assert.equal(sites[0].id, 'a'.repeat(32));
    assert.deepEqual(ui.siteUpdates, [selector.workspace]);
});

test('one mixed entry order drives entries, site relatives and dropdown while its manager anchor stays fixed', () => {
    const ui = browser(), selector = ui.selector();
    selector.fire('afterrender');
    assert.equal(ui.api.publish({ ...defaults, entry_order: [siteB, 'temperatures', 'safeline', siteA, 'komodo'] }, 1), true);
    const entries = [{ id: 'komodo' }, { id: siteA }, { id: 'temperatures' }, { id: 'safeline' }, { id: siteB }, { id: 'unseen' }];
    const sorted = ui.api.sortEntries(entries);
    assert.deepEqual(sorted.map((entry) => entry.id), [siteB, 'temperatures', 'safeline', siteA, 'komodo', 'unseen']);
    assert.equal(sorted[0], entries[4]); assert.equal(entries[0].id, 'komodo');
    assert.deepEqual(selector.rows.map((record) => record.get('key')),
        ['server', 'tags', 'pve-plugin-manager', siteB, 'pve-safeline-waf', siteA, 'pve-komodo-containers']);
    assert.equal(ui.api.publish({ ...defaults, entry_order: [siteA, ...defaults.entry_order] }, 2), true);
    assert.deepEqual(plain(ui.api.get()).entry_order, [siteA, ...defaults.entry_order]);
    for (const entry_order of [['sites'], ['pve-plugin-manager'], ['pve-komodo-containers'], ['komodo', 'komodo'],
        Array.from({ length: 101 }, (_, index) => 'pve-extra-site-' + index.toString(16).padStart(32, '0'))]) {
        assert.equal(ui.api.publish({ ...defaults, entry_order }, 3), false);
    }
});

test('only the complete canonical preferences are accepted and legacy orders are rejected', () => {
    const ui = browser();
    for (const preferences of [legacyDefaults, { ...legacyDefaults, module_aliases: {}, module_icons: {} },
        { ...defaults, module_order: legacyDefaults.module_order }, { ...defaults, view_order: [] },
        { entry_order: defaults.entry_order }, { entry_order: defaults.entry_order, module_aliases: {} },
        { entry_order: defaults.entry_order, module_icons: {} }, { ...defaults, module_icons: undefined },
        { ...defaults, module_aliases: undefined }, { ...defaults, entry_order: [siteA] },
        { ...defaults, module_aliases: { sites: '旧站点组' } },
        { ...defaults, module_icons: { sites: { type: 'pve', value: 'fa fa-globe' } } }]) {
        assert.equal(ui.api.publish(preferences, 1), false);
        assert.deepEqual(plain(ui.api.get()), defaults);
    }
    const maximum = { ...defaults, entry_order: [...defaults.entry_order,
        ...Array.from({ length: 100 }, (_, index) => 'pve-extra-site-' + index.toString(16).padStart(32, '0'))] };
    assert.equal(ui.api.publish(maximum, 1), true);
    assert.deepEqual(plain(ui.api.get()), maximum);
    assert.equal(ui.api.publish({ ...maximum, entry_order: [...maximum.entry_order, siteB] }, 2), false);
});

test('publishes validated monotonic revisions and sends subscribers independent copies', () => {
    const ui = browser(), seen = [];
    const unsubscribe = ui.api.subscribe((value, revision) => { seen.push([value.entry_order[0], revision]); value.entry_order.reverse(); });
    ui.api.subscribe(() => { throw new Error('Subscriber failure'); });
    assert.equal(ui.api.publish(defaults, 2), true);
    assert.equal(ui.api.publish({ ...defaults }, 2), true);
    assert.equal(ui.api.publish({ ...defaults, entry_order: [siteA] }, 2), false);
    assert.equal(ui.api.publish(defaults, 1), false);
    assert.equal(ui.api.publish({ ...defaults, entry_order: ['sites'] }, 3), false);
    assert.equal(ui.api.publish({ ...defaults, entry_order: ['server'] }, 3), false);
    assert.equal(ui.api.publish(defaults, 1.5), false);
    assert.deepEqual(plain(ui.api.get()), defaults);
    const result = ui.api.get(); result.entry_order.reverse();
    assert.deepEqual(plain(ui.api.get()), defaults);
    assert.deepEqual(seen, [['komodo', 2]]);
    assert.equal(ui.warnings.length, 1);
    unsubscribe(); ui.api.publish(defaults, 3);
    assert.equal(seen.length, 1);
});

test('aliases rename module records in place and escape dropdown HTML without encoding the input value', () => {
    const ui = browser(), selector = ui.selector();
    selector.fire('afterrender');
    const container = selector.rows.find((record) => record.get('key') === 'pve-komodo-containers');
    const site = selector.rows.find((record) => record.get('key') === siteA);
    selector.selected = container;
    const label = 'Docker <生产> & 监控';
    const preferences = { ...defaults, module_aliases: { safeline: '  边界 WAF  ', komodo: label } };
    assert.equal(ui.api.publish(preferences, 1), true);
    assert.equal(selector.selected, container);
    assert.ok(selector.rows.includes(container));
    assert.equal(container.get('value'), label);
    assert.equal(site.get('value'), siteA);
    assert.equal(ui.api.moduleLabel('komodo'), label);
    assert.equal(ui.api.moduleLabel('safeline'), '边界 WAF');
    assert.equal(ui.api.moduleLabel('sites'), '附加站点');
    assert.equal(ui.api.moduleLabel('temperatures'), '节点温度');
    assert.equal(selector.listConfig.maxHeight, 250);
    const template = selector.listConfig.getInnerTpl();
    const htmlEncode = (value) => value.replace(/[&<>"']/g, (character) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character]));
    const render = (key, value) => template.replace(/\{\[([\s\S]*?)\]\}/g, (_, expression) => vm.runInNewContext(expression,
        { values: { key, value }, window: ui.window, Ext: { String: { htmlEncode } } }));
    assert.equal(render('pve-komodo-containers', label), 'Docker &lt;生产&gt; &amp; 监控');
    assert.equal(render('pve-safeline-waf', '<WAF>'), '&lt;WAF&gt;');
    assert.equal(render(siteA, '站点 <名称>'), '站点 &lt;名称&gt;');
    assert.equal(render(siteA, '站点 &lt;名称&gt;'), '站点 &amp;lt;名称&amp;gt;');
    assert.equal(render(siteB, '<img src=x onerror="alert(1)"> & \'单引号\''),
        '&lt;img src=x onerror=&quot;alert(1)&quot;&gt; &amp; &#39;单引号&#39;');
    assert.equal(render('tags', '标签视图'), '标签视图');
    assert.equal(render('server', '<span>原生视图</span>'), '<span>原生视图</span>');
    assert.deepEqual(plain(ui.api.get()).module_aliases, { komodo: label, safeline: '边界 WAF' });
    const value = ui.api.get(); value.module_aliases.komodo = 'changed';
    assert.equal(ui.api.moduleLabel('komodo'), label);
    assert.equal(ui.api.publish({ ...defaults, module_aliases: { komodo: label, safeline: '边界 WAF' } }, 1), true);
    assert.equal(ui.api.publish(defaults, 2), true);
    assert.equal(container.get('value'), 'Komodo');
});

test('validates canonical alias keys, controls, resets and Unicode length', () => {
    const ui = browser();
    assert.equal(ui.api.publish(defaults, 1), true);
    for (const module_aliases of [{ unknown: 'Alias' }, { komodo: 1 }, { komodo: 'a\nb' }, { komodo: '\u007f' }, { komodo: '\u0085' }, { komodo: '字'.repeat(101) }, [], null]) {
        assert.equal(ui.api.publish({ ...defaults, module_aliases }, 2), false);
    }
    assert.equal(ui.api.publish({ ...defaults, module_aliases: { komodo: '😀'.repeat(100) } }, 2), true);
    assert.equal(ui.api.moduleLabel('komodo'), '😀'.repeat(100));
    assert.equal(ui.api.publish({ ...defaults, module_aliases: { komodo: '　 ', safeline: ' 边界防护 ' } }, 3), true);
    assert.deepEqual(plain(ui.api.get()).module_aliases, { safeline: '边界防护' });
});

test('normalizes module icon preferences, validates URLs/classes and returns independent specs', () => {
    const ui = browser();
    assert.deepEqual(plain(ui.api.moduleIcon('komodo')), { type: 'pve', value: 'fa fa-cubes' });
    assert.deepEqual(plain(ui.api.moduleIcon('temperatures')), { type: 'pve', value: 'fa fa-thermometer-half' });
    assert.deepEqual(plain(ui.api.moduleIcon('safeline')), { type: 'pve', value: 'fa fa-shield' });
    assert.deepEqual(plain(ui.api.moduleIcon('sites')), { type: 'pve', value: 'fa fa-external-link' });
    for (const spec of [{ type: 'pve', value: 'fa fa-server' }, { type: 'pve', value: 'fl fl-linux' },
        { type: 'url', value: 'https://icons.example/image"one.png?size=16&x=2' }, { type: 'url', value: 'http://[::1]:9100/icon.png' }]) {
        assert.deepEqual(plain(ui.api.normalizeIcon(spec)), spec);
    }
    for (const spec of [null, {}, { type: 'pve', value: 'fa fa-server x-other' }, { type: 'pve', value: 'fa fa-server;display:none' },
        { type: 'url', value: 'javascript:alert(1)' }, { type: 'url', value: 'data:image/png,anything' },
        { type: 'url', value: 'https://user:password@icons.example/icon.png' }, { type: 'url', value: 'https://@icons.example/icon.png' },
        { type: 'url', value: 'https://icons.example/icon name.png' }, { type: 'url', value: 'https://icons.example/\u0085.png' },
        { type: 'url', value: 'https://icons.example/\\icon.png' }, { type: 'url', value: 'https://icons.example:0/icon.png' },
        { type: 'url', value: 'https://bad_host/icon.png' }, { type: 'url', value: 'https://icons.example/\ud800.png' },
        { type: 'url', value: 'https://icons.example/' + 'a'.repeat(2048) }, { type: 'pve', value: 'fa fa-star', extra: 'field' }]) {
        assert.equal(ui.api.normalizeIcon(spec), null, JSON.stringify(spec));
    }
    assert.equal(ui.api.publish(defaults, 1), true);
    const configured = { ...defaults, module_icons: { komodo: { type: 'pve', value: 'fl fl-linux' } } };
    assert.equal(ui.api.publish(configured, 2), true);
    const returned = ui.api.get(); returned.module_icons.komodo.value = 'fa fa-bug';
    const icon = ui.api.moduleIcon('komodo'); icon.value = 'fa fa-bug';
    assert.equal(ui.api.moduleIcon('komodo').value, 'fl fl-linux');
    assert.equal(ui.api.publish({ ...defaults, module_icons: { unknown: { type: 'pve', value: 'fa fa-star' } } }, 3), false);
    assert.equal(ui.api.publish({ ...defaults, module_icons: null }, 3), false);
});

test('renders icons safely and falls back on image failure with one delegated listener', () => {
    const ui = browser({ dom: true }), document = ui.window.document;
    const html = ui.api.renderIcon({ type: 'url', value: 'https://icons.example/a"b.png?x=1&y=2' });
    assert.match(html, /src="https:\/\/icons\.example\/a&quot;b\.png\?x=1&amp;y=2"/);
    assert.match(html, /referrerpolicy="no-referrer"/);
    assert.doesNotMatch(html, /\son(?:error|load)\s*=/i);
    assert.match(ui.api.renderIcon({ type: 'url', value: 'javascript:alert(1)' }), /fa fa-external-link/);
    assert.doesNotMatch(ui.api.renderIcon({ type: 'url', value: 'javascript:alert(1)' }), /<img/);
    const wrapper = document.createElement('span'), image = document.createElement('img');
    image.classList.add('pve-plugin-icon-image'); wrapper.appendChild(image);
    const listener = document.handlers.find((handler) => handler.name === 'error');
    assert.equal(listener.capture, true); listener.callback({ target: image });
    assert.equal(image.hidden, true); assert.equal(wrapper.classList.contains('pve-plugin-icon-failed'), true);
    const unrelated = document.createElement('img'); listener.callback({ target: unrelated });
    assert.equal(unrelated.hidden, false);
    assert.match(document.head.children[0].textContent, /pve-plugin-icon-image\[hidden\]\{display:none\}/);
    assert.match(document.head.children[0].textContent, /pve-plugin-managed-icon\{display:none!important\}/);
    vm.runInContext(source, ui.context);
    assert.equal(document.handlers.length, 1); assert.equal(document.head.children.length, 1);
});

test('selected combo icons follow module preferences and asynchronous site updates without changing selected models', () => {
    const ui = browser({ dom: true }), selector = ui.selector();
    const container = selector.rows.find((record) => record.get('key') === 'pve-komodo-containers');
    selector.selected = container; selector.fire('afterrender');
    const selectedIcon = selector._pvePluginSelectedIcon;
    assert.match(selectedIcon.innerHTML, /fa fa-cubes/);
    assert.equal(selector.inputEl.dom.classList.contains('pve-plugin-selector-input'), true);
    ui.api.publish({ ...defaults, module_aliases: { komodo: '<生产>&' }, module_icons: {
        komodo: { type: 'url', value: 'https://icons.example/docker.png' },
    } }, 1);
    assert.equal(selector.selected, container); assert.equal(container.get('value'), '<生产>&');
    assert.match(selectedIcon.innerHTML, /https:\/\/icons\.example\/docker\.png/);
    const site = selector.rows.find((record) => record.get('key') === siteA);
    site.set('pluginIcon', { type: 'pve', value: 'fa fa-globe' });
    selector.selected = site; selector.fire('change');
    assert.match(selectedIcon.innerHTML, /fa fa-globe/);
    site.set('pluginIcon', { type: 'url', value: 'https://icons.example/site.png' });
    assert.match(selectedIcon.innerHTML, /https:\/\/icons\.example\/site\.png/);
    assert.equal(selector.selected, site); assert.equal(site.get('value'), siteA);
    selector.workspace.onLogin(null);
    assert.equal(selectedIcon.hidden, true); assert.equal(selectedIcon.innerHTML, '');
    assert.equal(selector.inputEl.dom.classList.contains('pve-plugin-selector-input'), false);
    assert.deepEqual(plain(ui.api.get()), defaults);
    selector.fire('beforedestroy');
    assert.equal(selectedIcon.parentNode, null); assert.equal(selector.updates.size, 0);
});

test('logout cancels requests, resets preferences and ignores late responses across users', () => {
    const ui = browser(), selector = ui.selector(), workspace = selector.workspace;
    selector.fire('afterrender'); const beforeLogout = ui.requests[0];
    ui.api.publish({ ...defaults, entry_order: [siteB, siteA, ...defaults.entry_order] }, 6);
    assert.equal(workspace.onLogin(null), 'native-login');
    assert.equal(beforeLogout.aborted, true);
    assert.deepEqual(plain(ui.api.get()), defaults);
    const order = selector.rows.slice();
    assert.equal(ui.api.applySelector(selector), false);
    assert.deepEqual(selector.rows, order);
    ui.reply(beforeLogout, { ...defaults, entry_order: [siteA, ...defaults.entry_order] }, 7);
    assert.deepEqual(plain(ui.api.get()), defaults);
    workspace.onLogin({ username: 'other@pam' });
    assert.equal(ui.requests.length, 2);
    ui.reply(ui.requests[1], { ...defaults, entry_order: [siteA, ...defaults.entry_order] }, 1);
    assert.deepEqual(plain(ui.api.get()).entry_order, [siteA, ...defaults.entry_order]);
    selector.fire('beforedestroy');
    assert.equal(ui.api.publish(defaults, 2), true);
});

test('ignores stale and malformed replies, destroyed owners, and duplicate script loading', () => {
    const ui = browser(), selector = ui.selector();
    selector.fire('afterrender');
    ui.api.publish({ ...defaults, entry_order: [siteB, ...defaults.entry_order] }, 5);
    ui.reply(ui.requests[0], defaults, 4);
    assert.deepEqual(plain(ui.api.get()).entry_order, [siteB, ...defaults.entry_order]);
    selector.workspace.onLogin({ username: 'root@pam' });
    ui.reply(ui.requests[1], { ...defaults, entry_order: [siteA, siteA] }, 6);
    assert.deepEqual(plain(ui.api.get()).entry_order, [siteB, ...defaults.entry_order]);
    selector.workspace.onLogin({ username: 'root@pam' });
    selector.workspace.destroyed = true;
    ui.reply(ui.requests[2], defaults, 6);
    assert.deepEqual(plain(ui.api.get()).entry_order, [siteB, ...defaults.entry_order]);
    vm.runInContext(source, ui.context);
    const second = ui.selector(); second.fire('afterrender');
    assert.equal(ui.requests.length, 4, 'duplicate loading adds no second hook');
});

test('unsupported Ext surfaces and unavailable transport fail without registration or writes', () => {
    for (const Ext of [undefined, {}, { ClassManager: {} }, { ClassManager: { get: () => null }, define() {} }]) {
        const context = vm.createContext({ window: { Ext } });
        assert.doesNotThrow(() => vm.runInContext(source, context));
        assert.equal(context.window.PVEPluginManagerNavigation, undefined);
    }
    const ui = browser(), selector = ui.selector();
    ui.window.Ext.Ajax.request = () => { throw new Error('No transport'); };
    assert.doesNotThrow(() => selector.fire('afterrender'));
    assert.deepEqual(plain(ui.api.get()), defaults);
});
