import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const plugins = [
    ['komodo', '../plugins/komodo/static/extension.js', '__pveKomodoDatacenterExtensionLoaded', '_pveKomodoLoggedOut'],
    ['temperatures', '../plugins/temperatures/static/temperatures.js', '__pveNodeTemperaturesLoaded', '_pveTemperaturesLoggedOut'],
    ['sites', '../plugins/sites/static/sites.js', '__pveExtraSitesLoaded', '_pveExtraSitesLoggedOut'],
].map(([id, path, marker, logoutFlag]) => ({ id, source: readFileSync(new URL(path, import.meta.url), 'utf8'), marker, logoutFlag }));

function browser() {
    const classes = new Map(), stack = [], warnings = [];
    function define(name, config) {
        if (config.override) {
            const target = classes.get(config.override);
            assert.ok(target, config.override);
            for (const [method, fn] of Object.entries(config)) {
                if (typeof fn !== 'function') continue;
                const previous = target.prototype[method];
                target.prototype[method] = function (...args) {
                    stack.push(previous);
                    try { return fn.apply(this, args); } finally { stack.pop(); }
                };
            }
            return;
        }
        function Component() {}
        Component.prototype = { ...config, callParent(args) { return stack.at(-1).apply(this, args); } };
        classes.set(name, Component);
    }
    for (const name of ['PVE.dc.Config', 'PVE.qemu.Config', 'PVE.lxc.Config']) {
        define(name, { insertNodes(items) { return items; } });
    }
    define('PVE.form.ViewSelector', {
        initComponent() {
            const rows = ['server', 'folder', 'pool', 'tags'].map((key) => ({ key }));
            this.store = { rows, findRecord(key, value) { return rows.find((row) => row[key] === value); }, add(row) { rows.push(row); } };
            return 'native-selector';
        },
        getStore() { return this.store; },
    });
    define('PVE.tree.ResourceTree', { initComponent() {} });
    define('PVE.node.StatusView', { initComponent() {} });
    define('PVE.node.Summary', { initComponent() {} });
    define('Proxmox.widget.Info', { updateValue() {} });
    define('PVE.StdWorkspace', {
        onLogin(data) { this.nativeLoginCalls = (this.nativeLoginCalls || 0) + 1; return data ? 'native-login' : 'native-logout'; },
    });
    const context = vm.createContext({ window: {
        Ext: { define, ClassManager: { get: (name) => classes.get(name) } },
        console: { warn: (...args) => warnings.push(args) },
    } });
    return { classes, context, warnings };
}

for (let mask = 0; mask < 8; mask++) {
    const selected = plugins.filter((_, index) => mask & (1 << index));
    test(`real plugin scripts compose selector and login hooks: ${selected.map((item) => item.id).join(',') || 'none'}`, () => {
        const ui = browser();
        for (const plugin of selected) vm.runInContext(plugin.source, ui.context);
        // A reload must not wrap shared PVE hooks a second time.
        for (const plugin of selected) vm.runInContext(plugin.source, ui.context);
        assert.equal(ui.warnings.length, 0);
        for (const plugin of plugins) assert.equal(Boolean(ui.context.window[plugin.marker]), selected.includes(plugin));
        const selector = new (ui.classes.get('PVE.form.ViewSelector'))();
        assert.equal(selector.initComponent(), 'native-selector');
        const expected = ['server', 'folder', 'pool', 'tags'];
        if (selected.some((plugin) => plugin.id === 'komodo')) expected.push('pve-komodo-containers');
        if (selected.some((plugin) => plugin.id === 'sites')) expected.push('pve-extra-sites');
        assert.deepEqual(selector.getStore().rows.map((row) => row.key), expected);
        const workspace = new (ui.classes.get('PVE.StdWorkspace'))();
        assert.equal(workspace.onLogin({ username: 'root@pam' }), 'native-login');
        assert.equal(workspace.onLogin(null), 'native-logout');
        assert.equal(workspace.nativeLoginCalls, 2, 'every original PVE login hook runs exactly once');
        for (const plugin of selected) assert.equal(workspace[plugin.logoutFlag], true);
    });
}
