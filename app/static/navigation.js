(function () {
    // ExtJS callParent requires non-strict functions.
    const Ext = window.Ext;
    const marker = '__pvePluginNavigationLoaded';
    if (window[marker] || !Ext || !Ext.ClassManager || typeof Ext.ClassManager.get !== 'function' || typeof Ext.define !== 'function') return;
    const Selector = Ext.ClassManager.get('PVE.form.ViewSelector');
    const Workspace = Ext.ClassManager.get('PVE.StdWorkspace');
    if (!Selector || !Workspace || !Selector.prototype || !Workspace.prototype ||
        typeof Selector.prototype.initComponent !== 'function' || typeof Workspace.prototype.onLogin !== 'function') return;

    const entryModules = ['komodo', 'temperatures', 'safeline'];
    const labels = { komodo: 'Komodo', temperatures: '节点温度', safeline: 'SafeLine WAF', sites: '附加站点' };
    const defaultIcons = { komodo: 'fa fa-cubes', temperatures: 'fa fa-thermometer-half', safeline: 'fa fa-shield', sites: 'fa fa-external-link' };
    const views = ['pve-plugin-manager', 'pve-komodo-containers', 'pve-safeline-waf'];
    const viewModules = new Map([['pve-komodo-containers', 'komodo'], ['pve-safeline-waf', 'safeline']]);
    const siteView = /^pve-extra-site-[0-9a-f]{32}$/;
    const selectors = new Map();
    const subscribers = new Set();
    let navigation = defaults(), revision = -1, preferenceUser = null;

    function defaults() { return { entry_order: entryModules.slice(), module_aliases: {}, module_icons: {} }; }
    function alias(value) {
        if (typeof value !== 'string' || /[\u0000-\u001f\u007f-\u009f]/.test(value)) return null;
        const trimmed = value.replace(/^\p{White_Space}+|\p{White_Space}+$/gu, '');
        return Array.from(trimmed).length <= 100 ? trimmed : null;
    }
    function aliases(value) {
        if (!value || typeof value !== 'object' || Array.isArray(value) || Object.keys(value).some(function (id) { return !entryModules.includes(id); })) return null;
        const result = {};
        for (const id of entryModules) {
            if (!Object.prototype.hasOwnProperty.call(value, id)) continue;
            const label = alias(value[id]);
            if (label === null) return null;
            if (label) result[id] = label;
        }
        return result;
    }
    function normalizeIcon(value) {
        if (!value || typeof value !== 'object' || Array.isArray(value) ||
            Object.keys(value).length !== 2 || !Object.prototype.hasOwnProperty.call(value, 'type') ||
            !Object.prototype.hasOwnProperty.call(value, 'value') || typeof value.value !== 'string') return null;
        if (value.type === 'pve') {
            return /^(?:fa fa-[a-z0-9-]+|fl fl-[a-z0-9-]+)$/.test(value.value) ? { type: 'pve', value: value.value } : null;
        }
        const characters = Array.from(value.value);
        if (value.type !== 'url' || !characters.length || characters.length > 2048 ||
            /[\\\p{White_Space}\u0000-\u001f\u007f-\u009f]/u.test(value.value) ||
            characters.some(function (character) { const code = character.codePointAt(0); return code >= 0xd800 && code <= 0xdfff; })) return null;
        try {
            const parsed = new window.URL(value.value);
            const authority = /^https?:\/\/([^/?#]*)/i.exec(value.value);
            if (!authority || !['http:', 'https:'].includes(parsed.protocol) || !parsed.hostname ||
                parsed.username || parsed.password || /[@%]/.test(authority[1]) || authority[1].endsWith(':') ||
                parsed.port && (Number(parsed.port) < 1 || Number(parsed.port) > 65535)) return null;
            const hostname = parsed.hostname;
            if (!hostname.startsWith('[')) {
                const dns = hostname.replace(/\.$/, '');
                if (!dns || dns.length > 253 || dns.split('.').some(function (part) {
                    return !/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/i.test(part);
                })) return null;
            }
            return { type: 'url', value: value.value };
        } catch (error) { return null; }
    }
    function icons(value) {
        if (!value || typeof value !== 'object' || Array.isArray(value) || Object.keys(value).some(function (id) { return !entryModules.includes(id); })) return null;
        const result = {};
        for (const id of entryModules) {
            if (!Object.prototype.hasOwnProperty.call(value, id)) continue;
            const icon = normalizeIcon(value[id]);
            if (!icon) return null;
            result[id] = icon;
        }
        return result;
    }
    function clone(value) {
        return { entry_order: value.entry_order.slice(), module_aliases: aliases(value.module_aliases), module_icons: icons(value.module_icons) };
    }
    function moduleLabel(id, preferences) {
        const value = preferences || navigation;
        const label = value.module_aliases && alias(value.module_aliases[id]);
        return label || (Object.prototype.hasOwnProperty.call(labels, id) ? labels[id] : id);
    }
    function moduleIcon(id, preferences) {
        const value = preferences || navigation;
        const configured = value.module_icons && normalizeIcon(value.module_icons[id]);
        return configured || { type: 'pve', value: defaultIcons[id] || 'fa fa-external-link' };
    }
    function encoded(value) {
        return value.replace(/[&<>"']/g, function (character) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character];
        });
    }
    function renderIcon(value) {
        const icon = normalizeIcon(value) || { type: 'pve', value: 'fa fa-external-link' };
        if (icon.type === 'pve') return '<span aria-hidden="true" class="pve-plugin-icon ' + icon.value + '"></span>';
        return '<span aria-hidden="true" class="pve-plugin-icon pve-plugin-icon-url">' +
            '<span class="pve-plugin-icon-fallback fa fa-external-link"></span>' +
            '<img class="pve-plugin-icon-image" src="' + encoded(icon.value) + '" alt="" referrerpolicy="no-referrer"></span>';
    }
    function installIcons() {
        const document = window.document;
        if (!document || !document.head || typeof document.createElement !== 'function' || typeof document.addEventListener !== 'function') return;
        const style = document.createElement('style');
        style.textContent = '.pve-plugin-icon{display:inline-block;width:16px;height:16px;line-height:16px;text-align:center;vertical-align:middle;margin-right:6px;flex-shrink:0}' +
            '.pve-plugin-icon-url{position:relative}.pve-plugin-icon-image{display:block;width:16px;height:16px;object-fit:contain}.pve-plugin-icon-image[hidden]{display:none}' +
            '.pve-plugin-icon-fallback{display:none}.pve-plugin-icon-failed .pve-plugin-icon-fallback{display:inline-block}' +
            '.pve-plugin-selector-wrap{position:relative}.pve-plugin-selector-icon{position:absolute;left:6px;top:50%;transform:translateY(-50%);pointer-events:none}' +
            '.pve-plugin-selector-icon .pve-plugin-icon{margin:0}.pve-plugin-selector-input{padding-left:28px!important}' +
            '.pve-plugin-managed-icon{display:none!important}';
        document.head.appendChild(style);
        document.addEventListener('error', function (event) {
            const image = event.target;
            if (!image || !image.classList || !image.classList.contains('pve-plugin-icon-image')) return;
            image.hidden = true;
            if (image.parentNode && image.parentNode.classList) image.parentNode.classList.add('pve-plugin-icon-failed');
        }, true);
    }
    function selectedIcon(selector) {
        const document = window.document, inputEl = selector.inputEl, inputWrap = selector.inputWrap;
        const input = inputEl && inputEl.dom;
        if (!document || typeof document.createElement !== 'function' || !input || !input.parentNode ||
            typeof inputEl.addCls !== 'function' || typeof inputEl.removeCls !== 'function' ||
            !inputWrap || typeof inputWrap.addCls !== 'function' || typeof selector.getValue !== 'function') return;
        const record = user(workspaceOf(selector)) && selector.getStore().getRange().find(function (row) { return row.get('key') === selector.getValue(); });
        const icon = record && record.get('pluginIcon');
        let element = selector._pvePluginSelectedIcon;
        if (!element) {
            element = document.createElement('span'); element.className = 'pve-plugin-selector-icon';
            element.setAttribute('aria-hidden', 'true'); inputWrap.addCls('pve-plugin-selector-wrap');
            input.parentNode.insertBefore(element, input); selector._pvePluginSelectedIcon = element;
        }
        element.hidden = !icon; element.innerHTML = icon ? renderIcon(icon) : '';
        // ExtJS rewrites cached classes on focus/blur; keep its cache in sync.
        if (icon) inputEl.addCls('pve-plugin-selector-input');
        else inputEl.removeCls('pve-plugin-selector-input');
    }
    function alive(component) { return component && !component.destroyed && !component.destroying; }
    function workspaceOf(selector) { return alive(selector) && typeof selector.up === 'function' ? selector.up('pveStdWorkspace') : null; }
    function user(workspace) {
        const proxmox = window.Proxmox;
        if (!alive(workspace) || workspace._pvePluginNavigationLoggedOut || !proxmox ||
            proxmox.Utils && typeof proxmox.Utils.authOK === 'function' && !proxmox.Utils.authOK()) return null;
        return typeof proxmox.UserName === 'string' && proxmox.UserName ? proxmox.UserName : null;
    }
    function normalize(value) {
        if (!value || typeof value !== 'object') return null;
        const names = Object.keys(value);
        if (names.length !== 3 || !['entry_order', 'module_aliases', 'module_icons'].every(function (key) {
            return Object.prototype.hasOwnProperty.call(value, key);
        })) return null;
        const configuredAliases = aliases(value.module_aliases), configuredIcons = icons(value.module_icons);
        if (configuredAliases === null || configuredIcons === null ||
            !Array.isArray(value.entry_order) || value.entry_order.length > 103 ||
            !value.entry_order.every(function (id) { return typeof id === 'string' && (entryModules.includes(id) || siteView.test(id)); }) ||
            !entryModules.every(function (id) { return value.entry_order.includes(id); }) ||
            new Set(value.entry_order).size !== value.entry_order.length) return null;
        return { entry_order: value.entry_order.slice(), module_aliases: configuredAliases, module_icons: configuredIcons };
    }
    function applySelector(selector) {
        if (!alive(selector) || !user(workspaceOf(selector)) || typeof selector.getStore !== 'function') return false;
        const store = selector.getStore();
        if (!store || typeof store.getRange !== 'function' || typeof store.sort !== 'function') return false;
        const records = store.getRange();
        records.forEach(function (record) {
            const id = viewModules.get(record.get('key'));
            if (!id) return;
            const label = moduleLabel(id);
            if (record.get('value') !== label) record.set('value', label);
            const icon = moduleIcon(id);
            if (JSON.stringify(record.get('pluginIcon')) !== JSON.stringify(icon)) record.set('pluginIcon', icon);
        });
        const original = new Map(records.map(function (record, index) { return [record, index]; }));
        const order = navigation.entry_order.filter(function (id) { return id !== 'temperatures'; }).map(function (id) {
            return id === 'komodo' ? 'pve-komodo-containers' : id === 'safeline' ? 'pve-safeline-waf' : id;
        });
        const positions = new Map(order.map(function (key, index) { return [key, index]; }));
        function rank(record) {
            const key = record.get('key');
            const previous = original.has(record) ? original.get(record) : records.length;
            if (!views.includes(key) && !siteView.test(key)) return previous;
            if (key === 'pve-plugin-manager') return records.length + 1;
            return records.length + 2 + (positions.has(key) ? positions.get(key) : order.length + previous);
        }
        // Sorting keeps the selected Model in the store. Removing it would
        // clear PVE's forceSelection ComboBox even if it were added again.
        store.sort({ direction: 'ASC', sorterFn: function (left, right) { return rank(left) - rank(right); } });
        selectedIcon(selector);
        return true;
    }
    function sortEntries(entries) {
        const positions = new Map(navigation.entry_order.map(function (key, index) { return [key, index]; }));
        return entries.map(function (entry, index) { return { entry: entry, index: index }; }).sort(function (left, right) {
            const a = positions.get(left.entry.id);
            const b = positions.get(right.entry.id);
            return (a === undefined ? positions.size + left.index : a) - (b === undefined ? positions.size + right.index : b);
        }).map(function (item) { return item.entry; });
    }
    function sortSites(sites) {
        return sortEntries(sites.map(function (site) { return { id: 'pve-extra-site-' + site.id, site: site }; })).map(function (entry) { return entry.site; });
    }
    function notify() {
        const workspaces = new Set();
        selectors.forEach(function (state, selector) {
            const workspace = workspaceOf(selector);
            if (user(workspace)) { applySelector(selector); workspaces.add(workspace); }
            else selectedIcon(selector);
        });
        if (window.PVEPluginSites && typeof window.PVEPluginSites.applyOrder === 'function') {
            workspaces.forEach(function (workspace) { window.PVEPluginSites.applyOrder(workspace); });
        }
        subscribers.forEach(function (callback) {
            try { callback(clone(navigation), revision); }
            catch (error) { if (window.console) window.console.warn('Plugin navigation subscriber failed.'); }
        });
    }
    function publish(value, nextRevision) {
        const normalized = normalize(value);
        if (!normalized || !Number.isSafeInteger(nextRevision) || nextRevision < 0 || nextRevision < revision) return false;
        if (nextRevision === revision) return JSON.stringify(normalized) === JSON.stringify(navigation);
        navigation = normalized; revision = nextRevision;
        preferenceUser = window.Proxmox && window.Proxmox.UserName || null;
        notify();
        return true;
    }
    function cancel(state) {
        state.generation++;
        const pending = state.request; state.request = null;
        if (pending) Ext.Ajax.abort(pending);
    }
    function reset() {
        navigation = defaults(); revision = -1; preferenceUser = null;
        notify();
    }
    function refresh(selector) {
        const state = selectors.get(selector);
        if (!state) return;
        cancel(state);
        const workspace = workspaceOf(selector), expectedUser = user(workspace);
        if (!expectedUser) return;
        if (preferenceUser && preferenceUser !== expectedUser) reset();
        applySelector(selector);
        const generation = state.generation;
        let pending, completed = false;
        function complete(options, success, response) {
            if (completed) return;
            completed = true;
            if (selectors.get(selector) !== state || state.generation !== generation ||
                workspaceOf(selector) !== workspace || user(workspace) !== expectedUser) return;
            state.request = null;
            let data;
            try { data = success && JSON.parse(response.responseText); } catch (error) { return; }
            if (data) publish(data.navigation, data.revision);
        }
        try {
            pending = Ext.Ajax.request({ url: '/pve-plugins/api/navigation', method: 'GET', disableCaching: false,
                timeout: 30000, callback: complete });
            if (!completed) state.request = pending;
        } catch (error) { complete(null, false, null); }
    }

    Ext.define('PVEPluginNavigation.override.Selector', { override: 'PVE.form.ViewSelector', initComponent: function () {
        // Module and site labels stay plain text in the input. Escape them at
        // the dropdown's HTML boundary; leave native PVE label rendering intact.
        this.listConfig = Object.assign({}, this.listConfig, { getInnerTpl: function () {
            return '{[values.pluginIcon ? window.PVEPluginManagerNavigation.renderIcon(values.pluginIcon) : ""]}' +
                '{[values.key === "pve-komodo-containers" || values.key === "pve-safeline-waf" || /^pve-extra-site-[0-9a-f]{32}$/.test(values.key) ? Ext.String.htmlEncode(values.value) : values.value]}';
        } });
        const result = this.callParent(arguments), selector = this;
        const state = { generation: 0, request: null };
        selectors.set(selector, state);
        const store = this.getStore();
        const updateIcon = function () { selectedIcon(selector); };
        if (store && typeof store.on === 'function') store.on('update', updateIcon);
        this.on('change', updateIcon);
        this.on('afterrender', function () { refresh(selector); });
        this.on('beforedestroy', function () {
            cancel(state); selectors.delete(selector);
            if (store && typeof store.un === 'function') store.un('update', updateIcon);
            if (selector._pvePluginSelectedIcon) selector._pvePluginSelectedIcon.remove();
        });
        return result;
    } });
    Ext.define('PVEPluginNavigation.override.Workspace', { override: 'PVE.StdWorkspace', onLogin: function (data) {
        const workspace = this;
        workspace._pvePluginNavigationLoggedOut = !data;
        selectors.forEach(function (state, selector) { if (workspaceOf(selector) === workspace) cancel(state); });
        if (!data) reset();
        const result = this.callParent(arguments);
        if (data) selectors.forEach(function (state, selector) { if (workspaceOf(selector) === workspace) refresh(selector); });
        return result;
    } });
    window.PVEPluginManagerNavigation = {
        get: function () { return clone(navigation); },
        moduleLabel: moduleLabel,
        moduleIcon: moduleIcon,
        normalizeIcon: normalizeIcon,
        renderIcon: renderIcon,
        publish: publish,
        applySelector: applySelector,
        sortSites: sortSites,
        sortEntries: sortEntries,
        subscribe: function (callback) { subscribers.add(callback); return function () { subscribers.delete(callback); }; },
    };
    installIcons();
    window[marker] = true;
}());
