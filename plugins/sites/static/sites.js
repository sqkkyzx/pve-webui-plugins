(function () {
    // ExtJS callParent uses caller metadata; do not make these overrides strict.
    const Ext = window.Ext;
    const marker = '__pveExtraSitesLoaded';
    const viewPrefix = 'pve-extra-site-';
    const api = '/pve-plugins/api/sites';
    if (window[marker] || !Ext || !Ext.ClassManager || typeof Ext.ClassManager.get !== 'function' || typeof Ext.define !== 'function') return;
    const Selector = Ext.ClassManager.get('PVE.form.ViewSelector');
    const Tree = Ext.ClassManager.get('PVE.tree.ResourceTree');
    const Workspace = Ext.ClassManager.get('PVE.StdWorkspace');
    if (!Selector || !Tree || !Workspace || !Selector.prototype || !Tree.prototype || !Workspace.prototype ||
        typeof Selector.prototype.initComponent !== 'function' || typeof Tree.prototype.initComponent !== 'function' ||
        typeof Workspace.prototype.onLogin !== 'function') return;

    const states = new Set(), selectors = new Set(), navigation = new Map(), iconWorkspaces = new Map(), siteSubscribers = new Map();
    const fallbackIcon = { type: 'pve', value: 'fa fa-globe' };
    const embedHint = '部分网站禁止嵌入；HTTPS 管理页面可能无法显示 HTTP 站点。';
    function alive(component) { return component && !component.destroyed && !component.destroying; }
    function encoded(value) {
        return String(value == null ? '' : value).replace(/[&<>"']/g, function (character) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character];
        });
    }
    function currentUser(state) {
        const proxmox = window.Proxmox;
        if (!proxmox || !alive(state.workspace) || state.workspace._pveExtraSitesLoggedOut ||
            proxmox.Utils && typeof proxmox.Utils.authOK === 'function' && !proxmox.Utils.authOK()) return null;
        return typeof proxmox.UserName === 'string' && proxmox.UserName ? proxmox.UserName : null;
    }
    function current(state, generation, user) {
        return state.active && alive(state.panel) && alive(state.native) && generation === state.generation && currentUser(state) === user;
    }
    function urlValue(value) {
        if (typeof value !== 'string') return null;
        const text = value.trim();
        if (!text || text.length > 2048 || !/^https?:\/\//i.test(text) || /[\u0000-\u0020\u007f\\]/.test(text)) return null;
        try {
            const parsed = new window.URL(text);
            return ['http:', 'https:'].includes(parsed.protocol) && parsed.hostname && !parsed.username && !parsed.password ? text : null;
        } catch (error) { return null; }
    }
    function normalizeSiteIcon(value) {
        if (value === null) return null;
        const shared = window.PVEPluginManagerNavigation;
        if (shared && typeof shared.normalizeIcon === 'function') return shared.normalizeIcon(value);
        if (!value || typeof value !== 'object' || Object.keys(value).length !== 2 || typeof value.value !== 'string') return null;
        if (value.type === 'pve' && /^(?:fa fa-[a-z0-9-]+|fl fl-[a-z0-9-]+)$/.test(value.value)) return { type: 'pve', value: value.value };
        if (value.type === 'url' && urlValue(value.value) === value.value) return { type: 'url', value: value.value };
        return null;
    }
    function siteEnabled(site) { return site.enabled; }
    function siteName(site) { return site.alias || site.name; }
    function copySite(site) {
        return { id: site.id, name: site.name, url: site.url, revision: site.revision,
            enabled: site.enabled, alias: site.alias, icon: normalizeSiteIcon(site.icon) };
    }
    function notifySites(workspace) {
        const callbacks = siteSubscribers.get(workspace), entry = navigation.get(workspace);
        if (!callbacks || !entry || entry.sites.length && entry.user !== currentUser({ workspace: workspace })) return;
        callbacks.forEach(function (callback) {
            try { callback(entry.sites.map(copySite)); }
            catch (error) { if (window.console) window.console.warn('Site subscriber failed.'); }
        });
    }
    function renderSiteIcon(spec) {
        const shared = window.PVEPluginManagerNavigation;
        return shared && typeof shared.renderIcon === 'function' ? shared.renderIcon(spec || fallbackIcon) :
            '<span class="fa fa-globe" aria-hidden="true" style="display:inline-block;width:16px;margin-right:6px"></span>';
    }
    function iconFor(workspace, site) {
        const manual = normalizeSiteIcon(site.icon);
        if (manual) return manual;
        if (!siteEnabled(site)) return fallbackIcon;
        const registry = iconWorkspaces.get(workspace);
        const entry = registry && registry.user === currentUser({ workspace: workspace }) && registry.entries.get(site.id);
        return entry && entry.url === site.url && entry.revision === site.revision ? entry.spec : fallbackIcon;
    }
    function updateIconWidgets(registry, entry) {
        selectors.forEach(function (selector) {
            if (!alive(selector) || selector.up('pveStdWorkspace') !== registry.workspace) return;
            const record = selector.getStore().findRecord('key', viewPrefix + entry.id, 0, false, true, true);
            if (record) record.set('pluginIcon', entry.spec);
        });
        states.forEach(function (state) {
            if (state.workspace !== registry.workspace || state.user !== registry.user || !state.loaded || !alive(state.panel)) return;
            if (state.manager) {
                const record = state.panel.getStore().getById(entry.id);
                if (record && record.get('url') === entry.url && record.get('revision') === entry.revision) record.set('pluginIcon', entry.spec);
            } else {
                const site = pageSite(state, state.page);
                if (site && site.id === entry.id && site.url === entry.url && site.revision === entry.revision) updateSiteInfo(state, site);
            }
        });
        notifySites(registry.workspace);
    }
    function clearSiteIcons(workspace) {
        const registry = iconWorkspaces.get(workspace);
        if (!registry) return;
        registry.generation++;
        const requests = Array.from(registry.entries.values()).map(function (entry) { return entry.request; });
        registry.entries.clear(); registry.active = 0; registry.user = null;
        requests.forEach(function (request) { if (request) Ext.Ajax.abort(request); });
    }
    function pumpSiteIcons(registry) {
        if (registry.pumping || registry.syncing) return;
        registry.pumping = true;
        try {
            while (registry.active < 4 && registry.user && currentUser({ workspace: registry.workspace }) === registry.user) {
                const entry = Array.from(registry.entries.values()).find(function (candidate) { return candidate.status === 'queued'; });
                if (!entry) break;
                entry.status = 'pending'; registry.active++;
                const generation = registry.generation;
                let completed = false;
                function complete(options, success, response) {
                    if (completed) return;
                    completed = true; entry.request = null;
                    if (registry.generation !== generation) return;
                    registry.active--;
                    if (currentUser({ workspace: registry.workspace }) !== registry.user || response && (response.status === 401 || response.status === 403)) {
                        clearSiteIcons(registry.workspace); return;
                    }
                    if (registry.entries.get(entry.id) === entry) {
                        const data = success ? parse(response) : null;
                        const url = data && data.site_id === entry.id && data.revision === entry.revision && urlValue(data.icon_url);
                        entry.status = 'done'; entry.spec = url ? { type: 'url', value: url } : fallbackIcon;
                        updateIconWidgets(registry, entry);
                    }
                    pumpSiteIcons(registry);
                }
                try {
                    const request = Ext.Ajax.request({ url: api + '/' + entry.id + '/icon', method: 'GET', disableCaching: false,
                        timeout: 15000, callback: complete });
                    if (!completed) entry.request = request;
                } catch (error) { complete(null, false, null); }
            }
        } finally { registry.pumping = false; }
    }
    function syncSiteIcons(workspace, sites) {
        sites = sites.filter(function (site) { return siteEnabled(site) && !site.icon; });
        const user = currentUser({ workspace: workspace });
        if (!user) { clearSiteIcons(workspace); return; }
        let registry = iconWorkspaces.get(workspace);
        if (!registry) {
            registry = { workspace: workspace, user: null, generation: 0, entries: new Map(), active: 0, pumping: false, syncing: false };
            iconWorkspaces.set(workspace, registry);
            workspace.on('beforedestroy', function () { clearSiteIcons(workspace); iconWorkspaces.delete(workspace); });
        }
        if (registry.user !== user) { clearSiteIcons(workspace); registry.user = user; }
        registry.syncing = true;
        try {
            const current = new Map(sites.map(function (site) { return [site.id, site]; }));
            registry.entries.forEach(function (entry, id) {
                const site = current.get(id);
                if (!site || site.url !== entry.url || site.revision !== entry.revision) {
                    registry.entries.delete(id);
                    if (entry.request) Ext.Ajax.abort(entry.request);
                }
            });
            sites.forEach(function (site) {
                if (!registry.entries.has(site.id)) registry.entries.set(site.id, {
                    id: site.id, url: site.url, revision: site.revision, spec: fallbackIcon, status: 'queued', request: null,
                });
            });
        } finally { registry.syncing = false; }
        pumpSiteIcons(registry);
    }
    function validSite(site) {
        return site && typeof site.id === 'string' && /^[0-9a-f]{32}$/.test(site.id) && typeof site.name === 'string' &&
            site.name.trim().length > 0 && Array.from(site.name).length <= 100 && !/[\u0000-\u001f\u007f]/.test(site.name) && urlValue(site.url) !== null &&
            Number.isSafeInteger(site.revision) && site.revision > 0 &&
            typeof site.enabled === 'boolean' &&
            (typeof site.alias === 'string' && site.alias === site.alias.trim() &&
                Array.from(site.alias).length <= 100 && !/[\u0000-\u001f\u007f-\u009f]/.test(site.alias) &&
                !Array.from(site.alias).some(function (character) { const point = character.codePointAt(0); return point >= 0xd800 && point <= 0xdfff; })) &&
            (site.icon === null || normalizeSiteIcon(site.icon) !== null);
    }
    function status(state, message) {
        if (!alive(state.panel)) return;
        const field = state.panel.down('#pve-extra-sites-status');
        if (alive(field)) field.setText(encoded(message || ''));
    }
    function selectedSite(state) { return state.sites.find(function (site) { return site.id === state.selected; }) || null; }
    function confirming(state) { return alive(state._siteConfirmation) || alive(state.editor) && alive(state.editor._siteConfirmation); }
    function pageSite(state, page) {
        const site = selectedSite(state);
        return alive(page) && state.page === page && current(state, state.generation, state.user) && state.loaded && state.csrf &&
            !state.write && !confirming(state) && site && page._siteFrameActive && page.pveExtraSiteId === site.id && page.pveExtraSiteUrl === site.url ? site : null;
    }
    function controls(state) {
        if (!alive(state.panel)) return;
        const ready = state.manager && state.active && state.loaded && !!state.csrf && currentUser(state) === state.user && !state.write && !confirming(state);
        if (state.manager) {
            state.panel.down('#pve-extra-sites-new').setDisabled(!ready);
            state.panel.down('#pve-extra-sites-edit').setDisabled(!ready || !selectedSite(state));
        } else {
            const available = !!pageSite(state, state.page);
            state.panel.down('#pve-extra-sites-sidebar-refresh').setDisabled(!available);
            state.panel.down('#pve-extra-sites-open').setDisabled(!available);
        }
        if (alive(state.page)) ['refresh'].forEach(function (action) {
            const button = state.page.down('#pve-extra-sites-' + action);
            if (alive(button)) button.setDisabled(!pageSite(state, state.page));
        });
    }
    function siteToolbar(state, getPage) {
        return [{ text: '刷新', itemId: 'pve-extra-sites-refresh', iconCls: 'fa fa-refresh', disabled: true,
            handler: function () { const site = pageSite(state, getPage()); if (site) openSite(state, site, true); } }];
    }
    function isSiteView(view) { return !!view && typeof view.id === 'string' && new RegExp('^' + viewPrefix + '[0-9a-f]{32}$').test(view.id); }
    function selectorRows(selector, sites) {
        const store = selector.getStore();
        const records = typeof store.getRange === 'function' ? store.getRange() : store.rows || [];
        const workspace = selector.up('pveStdWorkspace');
        const pending = new Map(sites.map(function (site) { return [viewPrefix + site.id, site]; }));
        records.slice().forEach(function (record) {
            const key = record.get ? record.get('key') : record.key;
            if (typeof key !== 'string' || key.indexOf(viewPrefix) !== 0) return;
            if (pending.has(key)) {
                // Removing a selected record clears PVE's forceSelection combo.
                // Keep its identity and let Ext update the label on rename.
                const site = pending.get(key), label = siteName(site);
                if (record.get('value') !== label) record.set('value', label);
                record.set('pluginIcon', iconFor(workspace, site));
                pending.delete(key);
            } else {
                if (typeof store.remove === 'function') store.remove(record);
                else if (store.rows) store.rows.splice(store.rows.indexOf(record), 1);
            }
        });
        pending.forEach(function (site, key) { store.add({ key: key, value: siteName(site), pluginIcon: iconFor(workspace, site) }); });
        const ordering = window.PVEPluginManagerNavigation;
        if (ordering && typeof ordering.applySelector === 'function') ordering.applySelector(selector);
    }
    function restoreSiteSelection(selector) {
        const pending = selector._pveExtraSitesRestore;
        if (!pending) return;
        delete selector._pveExtraSitesRestore;
        const user = currentUser({ workspace: selector.up('pveStdWorkspace') });
        if (!user || pending.user && pending.user !== user || selector.getValue() !== pending.initialValue) return;
        if (selector.getStore().findRecord('key', pending.value, 0, false, true, true)) selector.applyState({ value: pending.value }, true);
    }
    function syncNavigation(workspace, sites, restoreSelection) {
        sites = sites.map(copySite);
        let entry = navigation.get(workspace);
        if (!entry) { entry = { generation: 0, read: null, sites: [] }; navigation.set(workspace, entry); }
        entry.generation++;
        const pending = entry.read; entry.read = null;
        if (pending) Ext.Ajax.abort(pending);
        entry.sites = sites; entry.user = currentUser({ workspace: workspace });
        selectors.forEach(function (selector) {
            if (alive(selector) && typeof selector.up === 'function' && selector.up('pveStdWorkspace') === workspace) selectorRows(selector, sites.filter(siteEnabled));
        });
        syncSiteIcons(workspace, sites);
        states.forEach(function (state) {
            if (state.workspace !== workspace || state.manager || !state.active || !state.loaded) return;
            const selected = sites.find(function (site) { return site.id === state.selected; });
            state.sites = sites.map(copySite);
            if (!selected || !siteEnabled(selected)) {
                leave(state);
                state.workspace.setContent({ xtype: 'panel', border: false, bodyPadding: 20,
                    html: selected ? '此站点已停用，请选择其他视图。' : '此站点已被删除，请选择其他视图。' });
            } else if (alive(state.page) && state.page._siteFrameActive) {
                if (state.page.pveExtraSiteUrl !== selected.url) openSite(state, selected, true);
                else {
                    const frame = state.page.down('#pve-extra-sites-frame'), element = frame && frame.getEl && frame.getEl();
                    if (element && element.dom) element.dom.setAttribute('title', siteName(selected));
                    updateSiteInfo(state, selected);
                }
            }
        });
        notifySites(workspace);
        if (restoreSelection) selectors.forEach(function (selector) {
            if (alive(selector) && selector.up('pveStdWorkspace') === workspace) restoreSiteSelection(selector);
        });
    }
    function clearNavigation(workspace, keepRestore) {
        if (!keepRestore) selectors.forEach(function (selector) {
            if (selector.up('pveStdWorkspace') === workspace) delete selector._pveExtraSitesRestore;
        });
        clearSiteIcons(workspace);
        const entry = navigation.get(workspace);
        if (entry) { entry.generation++; const read = entry.read; entry.read = null; if (read) Ext.Ajax.abort(read); }
        syncNavigation(workspace, []);
    }
    function refreshNavigation(workspace) {
        if (!alive(workspace) || !currentUser({ workspace: workspace })) return;
        let entry = navigation.get(workspace);
        if (!entry) { syncNavigation(workspace, []); entry = navigation.get(workspace); }
        const user = currentUser({ workspace: workspace });
        const generation = ++entry.generation, oldRead = entry.read; entry.read = null;
        if (oldRead) Ext.Ajax.abort(oldRead);
        let completed = false;
        const request = Ext.Ajax.request({ url: api, method: 'GET', disableCaching: false, timeout: 30000,
            callback: function (options, success, response) {
                completed = true;
                if (generation !== entry.generation) return;
                entry.read = null;
                if (currentUser({ workspace: workspace }) !== user) { clearNavigation(workspace); return; }
                const data = success ? parse(response) : null;
                if (!validList(data, user)) { clearNavigation(workspace); return; }
                syncNavigation(workspace, data.sites, true);
            } });
        if (!completed) entry.read = request;
    }
    function stopFrame(page) {
        if (!page) return;
        const frame = page.down && page.down('#pve-extra-sites-frame');
        const element = frame && frame.getEl && frame.getEl();
        const dom = element && element.dom;
        if (dom) {
            dom.setAttribute('src', 'about:blank');
            const document = window.document;
            if (document && document.fullscreenElement === dom && typeof document.exitFullscreen === 'function') {
                const result = document.exitFullscreen();
                if (result && typeof result.catch === 'function') result.catch(function () {});
            }
        }
    }
    function closePage(state) {
        if (alive(state._siteConfirmation)) state._siteConfirmation.destroy();
        state._siteConfirmation = null;
        const page = state.page;
        state.page = null;
        stopFrame(page);
        if (alive(page)) page.destroy();
    }
    function closeEditor(state) {
        const editor = state.editor;
        state.editor = null;
        if (editor && alive(editor._siteConfirmation)) editor._siteConfirmation.destroy();
        if (alive(editor)) editor.destroy();
    }
    function cancel(state) {
        state.generation++;
        const requests = [state.read, state.write];
        state.read = null; state.write = null;
        requests.forEach(function (request) { if (request) Ext.Ajax.abort(request); });
    }
    function clear(state) {
        state.loaded = false; state.csrf = null; state.sites = [];
        closeEditor(state); closePage(state);
        if (alive(state.panel)) {
            if (state.manager) state.panel.getStore().loadData([]);
            else updateSiteInfo(state, null);
        }
        controls(state);
    }
    function leave(state) {
        state.active = false;
        cancel(state); clear(state);
        if (!state.manager && alive(state.panel)) state.panel.setVisible(false);
    }
    function showPlaceholder(state, message) {
        if (!current(state, state.generation, state.user)) return;
        const generation = state.generation, user = state.user;
        let page;
        closePage(state);
        state.workspace.setContent({ xtype: 'panel', itemId: 'pve-extra-sites-content', pveExtraSitesPage: true,
            border: false, header: false, bodyPadding: 20,
            tbar: siteToolbar(state, function () { return page; }),
            html: '<div>' + encoded(message) + '</div><p>' + encoded(embedHint) + '</p>',
            listeners: { afterrender: function (component) {
                page = component;
                if (current(state, generation, user)) { state.page = page; controls(state); }
                else if (alive(page)) page.destroy();
            } },
        });
    }
    function openSite(state, site, force) {
        if (!validSite(site) || !siteEnabled(site) || !current(state, state.generation, state.user)) return;
        state.selected = site.id;
        updateSiteInfo(state, site);
        if (!force && alive(state.page) && state.page.pveExtraSiteId === site.id && state.page.pveExtraSiteUrl === site.url && state.page._siteFrameActive) {
            controls(state); return;
        }
        closePage(state);
        const generation = state.generation, user = state.user;
        let page;
        function removeFrame() {
            if (page) page._siteFrameActive = false;
            if (state.page === page && alive(state._siteConfirmation)) { state._siteConfirmation.destroy(); state._siteConfirmation = null; }
            stopFrame(page); controls(state);
        }
        state.workspace.setContent({ xtype: 'panel', itemId: 'pve-extra-sites-content', pveExtraSitesPage: true,
            pveExtraSiteId: site.id, pveExtraSiteUrl: site.url, border: false, header: false, layout: 'fit',
            tbar: siteToolbar(state, function () { return page; }),
            items: [{ xtype: 'component', itemId: 'pve-extra-sites-frame',
                autoEl: { tag: 'iframe', style: 'display:block;width:100%;height:100%;border:0;' },
                listeners: { afterrender: function (frame) {
                    const element = frame.getEl();
                    if (!element || !element.dom) return;
                    const dom = element.dom;
                    if (!current(state, generation, user) || state.selected !== site.id) { dom.setAttribute('src', 'about:blank'); return; }
                    // Admin-curated sites need their own scripts, login and storage.
                    // This sandbox is a navigation restriction, not an isolation
                    // boundary for untrusted programs served from the PVE origin.
                    dom.setAttribute('sandbox', 'allow-scripts allow-forms allow-same-origin allow-popups allow-downloads');
                    dom.setAttribute('referrerpolicy', 'no-referrer');
                    dom.setAttribute('allow', 'fullscreen');
                    dom.setAttribute('allowfullscreen', '');
                    dom.setAttribute('title', siteName(site));
                    dom.setAttribute('src', site.url);
                }, beforedestroy: function (frame) {
                    const element = frame.getEl && frame.getEl();
                    if (element && element.dom) element.dom.setAttribute('src', 'about:blank');
                } },
            }],
            listeners: { afterrender: function (component) {
                page = component;
                if (current(state, generation, user) && state.selected === site.id) { page._siteFrameActive = true; state.page = page; controls(state); }
                else if (alive(page)) page.destroy();
            },
                deactivate: removeFrame, hide: removeFrame, beforedestroy: removeFrame,
                destroy: function (component) { if (state.page === component) state.page = null; } },
        });
        status(state, window.location && window.location.protocol === 'https:' && /^http:/i.test(site.url) ? embedHint : '');
        controls(state);
    }
    function validList(data, user) {
        return data && data.user === user && Array.isArray(data.sites) && data.sites.length <= 100 && data.sites.every(validSite) &&
            new Set(data.sites.map(function (site) { return site.id; })).size === data.sites.length &&
            typeof data.csrf_token === 'string' && !!data.csrf_token && data.csrf_token.length <= 2048;
    }
    function parse(response) { try { return JSON.parse(response.responseText); } catch (error) { return null; } }
    function loadSites(state, notice) {
        if (!state.active || !alive(state.panel)) return;
        const user = currentUser(state);
        if (!user) { leave(state); status(state, '请先登录 PVE'); return; }
        if (state.manager && state.user !== user) state.selected = null;
        cancel(state); clear(state);
        state.user = user;
        const generation = state.generation;
        status(state, '正在读取附加站点…');
        if (!state.manager) showPlaceholder(state, '正在读取站点…');
        let completed = false;
        function complete(options, success, response) {
            if (completed) return;
            completed = true;
            if (!current(state, generation, user)) { if (state.active && generation === state.generation) { leave(state); clearNavigation(state.workspace); status(state, '会话已改变，请重新进入附加站点'); } return; }
            state.read = null;
            if (response && (response.status === 401 || response.status === 403)) {
                clear(state); clearNavigation(state.workspace); status(state, response.status === 401 ? 'PVE 会话已过期' : '无访问权限'); return;
            }
            const data = success ? parse(response) : null;
            if (!validList(data, user)) {
                clear(state); clearNavigation(state.workspace); status(state, '站点列表读取失败，请重新进入附加站点'); return;
            }
            data.sites = data.sites.map(copySite);
            state.loaded = true; state.csrf = data.csrf_token; state.sites = data.sites;
            if (state.manager) {
                const ordering = window.PVEPluginManagerNavigation;
                const rows = ordering && typeof ordering.sortSites === 'function' ? ordering.sortSites(data.sites) : data.sites;
                state.panel.getStore().loadData(rows.map(function (site) {
                    return Object.assign({}, site, { pluginIcon: iconFor(state.workspace, site) });
                }));
            }
            syncNavigation(state.workspace, data.sites, true);
            const selected = selectedSite(state);
            if (state.manager) {
                const record = selected && state.panel.getStore().getById(selected.id);
                if (record) state.panel.getSelectionModel().select(record);
                status(state, data.sites.length ? '附加站点将直接显示在视图列表中。' : '尚无附加站点，点击“新建”添加。');
            } else if (selected && siteEnabled(selected)) openSite(state, selected);
            else if (selected) showPlaceholder(state, '此站点已停用，请选择其他视图。');
            else { showPlaceholder(state, '此附加站点已被删除，请选择其他视图。'); status(state, '站点不存在'); }
            if (notice) status(state, notice);
            controls(state);
        }
        try {
            const request = Ext.Ajax.request({ url: api, method: 'GET', disableCaching: false, timeout: 30000, callback: complete });
            if (!completed) state.read = request;
        } catch (error) { complete(null, false, null); }
    }
    function editorMessage(editor, message) {
        if (alive(editor)) editor.down('#pve-extra-sites-editor-status').update(encoded(message));
    }
    function editorControls(state, editor) {
        if (!alive(editor)) return;
        const blocked = !!state.write || editor._siteBlocked;
        editor.down('#pve-extra-sites-save').setDisabled(blocked);
        const remove = editor.down('#pve-extra-sites-delete');
        if (remove) remove.setDisabled(blocked);
        editor.down('#pve-extra-sites-cancel').setDisabled(!!state.write);
    }
    function mutate(state, editor, site, method) {
        if ((method !== 'DELETE' && !alive(editor)) || (editor && (!alive(editor) || editor._siteBlocked)) || state.write ||
            !current(state, state.generation, state.user) || !state.csrf) return;
        if (method === 'DELETE' && (!validSite(site) || !selectedSite(state) || selectedSite(state).id !== site.id || selectedSite(state).revision !== site.revision)) return;
        const name = method === 'DELETE' ? null : String(editor.down('#pve-extra-sites-name').getValue() || '').trim();
        const url = method === 'DELETE' ? null : urlValue(editor.down('#pve-extra-sites-url').getValue());
        if (method !== 'DELETE' && (!name || name.length > 100 || /[\u0000-\u001f\u007f]/.test(name) || !url)) {
            editorMessage(editor, '请输入名称和有效的 HTTP(S) URL；URL 不能包含账号密码。'); return;
        }
        const body = method === 'DELETE' ? { expected_revision: site.revision } : { name: name, url: url };
        if (method === 'PATCH') body.expected_revision = site.revision;
        const generation = state.generation, user = state.user;
        let completed = false;
        state.write = { pending: true };
        if (editor) editorMessage(editor, method === 'DELETE' ? '正在删除…' : '正在保存…');
        else status(state, '正在移除站点链接…');
        editorControls(state, editor); controls(state);
        function complete(options, success, response) {
            if (completed) return;
            completed = true;
            if (!current(state, generation, user)) {
                if (state.active && generation === state.generation) { leave(state); clearNavigation(state.workspace); status(state, '会话已改变，请重新进入附加站点'); }
                return;
            }
            state.write = null;
            if (editor && !alive(editor)) { loadSites(state); return; }
            if (response && (response.status === 401 || response.status === 403)) {
                clear(state); clearNavigation(state.workspace); status(state, response.status === 401 ? 'PVE 会话已过期' : '无访问权限或会话令牌已改变，请重新进入附加站点'); return;
            }
            const data = success ? parse(response) : null;
            const valid = method === 'DELETE' ? data && data.deleted === true && data.id === site.id :
                data && validSite(data.site) && (method === 'POST' || data.site.id === site.id && data.site.revision > site.revision);
            if (success && valid) {
                state.selected = method === 'DELETE' ? null : data.site.id;
                closeEditor(state); loadSites(state); return;
            }
            const conflict = response && (response.status === 409 || response.status === 404);
            if (!editor) {
                loadSites(state, conflict ? '站点已被其他操作改变，已重新读取列表；本次未自动重试。' :
                    '移除请求未获确认，已重新读取列表，请核对结果；本次未自动重试。');
                return;
            }
            editor._siteBlocked = true;
            editorMessage(editor, conflict ? '站点已被其他操作改变，当前输入已保留。请关闭窗口并重新编辑；本次未自动重试。' :
                '请求未完成或未获确认，结果可能已保存。请关闭窗口重新读取列表核对，勿重复提交。');
            editorControls(state, editor); controls(state);
        }
        try {
            const request = Ext.Ajax.request({ url: api + (site ? '/' + site.id : ''), method: method, jsonData: body,
                headers: { 'X-PVE-Plugins-CSRF': state.csrf }, timeout: 45000, callback: complete });
            if (!completed) state.write = request;
        } catch (error) { state.write = null; complete(null, false, null); }
    }
    function confirmDelete(state, editor, site) {
        if (!validSite(site) || (editor && (!alive(editor) || editor._siteBlocked)) || state.write || confirming(state) ||
            !current(state, state.generation, state.user) || !state.csrf) return;
        const owner = editor || state, generation = state.generation, user = state.user;
        const confirmation = Ext.create('Ext.window.MessageBox', { closeAction: 'destroy' });
        owner._siteConfirmation = confirmation;
        controls(state);
        confirmation.show({ title: '移除附加站点', message: '确定移除站点“' + encoded(site.name) + '”？此操作仅移除保存的链接。',
            buttons: Ext.Msg.YESNO, icon: Ext.Msg.WARNING, fn: function (answer) {
                const owned = owner._siteConfirmation === confirmation;
                if (owned) owner._siteConfirmation = null;
                if (alive(confirmation)) confirmation.destroy();
                controls(state);
                if (owned && answer === 'yes' && (!editor || alive(editor)) && current(state, generation, user)) mutate(state, editor, site, 'DELETE');
            } });
    }
    function openEditor(state, site) {
        if (!state.manager || !state.loaded || !state.csrf || state.write || !current(state, state.generation, state.user)) return;
        if (site && !validSite(site)) return;
        closeEditor(state);
        let editor;
        const buttons = [];
        if (site) buttons.push({ text: '删除', itemId: 'pve-extra-sites-delete', iconCls: 'fa fa-trash',
            handler: function () { confirmDelete(state, editor, site); } }, '->');
        buttons.push({ text: '保存', itemId: 'pve-extra-sites-save', handler: function () { mutate(state, editor, site, site ? 'PATCH' : 'POST'); } },
            { text: '取消', itemId: 'pve-extra-sites-cancel', handler: function () { editor.close(); } });
        editor = Ext.create('Ext.window.Window', { title: site ? '编辑附加站点' : '新建附加站点', width: 560,
            modal: true, constrain: true, closeAction: 'destroy', bodyPadding: 16, layout: 'anchor',
            defaults: { anchor: '100%', labelWidth: 60 },
            items: [{ xtype: 'textfield', itemId: 'pve-extra-sites-name', fieldLabel: '名称', value: site ? site.name : '',
                allowBlank: false, maxLength: 100, enforceMaxLength: true },
                { xtype: 'textfield', itemId: 'pve-extra-sites-url', fieldLabel: 'URL', value: site ? site.url : '',
                    emptyText: 'https://dashboard.example/', allowBlank: false, maxLength: 2048, enforceMaxLength: true,
                    validator: function (value) { return urlValue(value) ? true : '请输入不含账号密码的 HTTP(S) URL'; } },
                { xtype: 'component', html: encoded(embedHint), margin: '4 0 12 0' },
                { xtype: 'component', itemId: 'pve-extra-sites-editor-status', html: '', style: 'white-space:normal;' }],
            buttons: buttons, listeners: { beforeclose: function () { return !state.write; }, beforedestroy: function () {
                if (alive(editor._siteConfirmation)) editor._siteConfirmation.destroy();
            }, destroy: function () {
                const owned = state.editor === editor;
                if (owned) state.editor = null;
                if (owned && editor._siteBlocked && state.active && !state.write) loadSites(state);
            } },
        });
        state.editor = editor;
        editor.show();
    }
    function updateSiteInfo(state, site) {
        if (state.manager || !alive(state.panel)) return;
        const content = state.panel.down('#pve-extra-sites-info');
        if (!alive(content)) return;
        if (!site) { content.getStore().loadData([]); state.panel.setTitle('站点信息'); return; }
        state.panel.setTitle(renderSiteIcon(iconFor(state.workspace, site)) + '站点信息');
        const url = new window.URL(site.url);
        const fields = [['名称', siteName(site)], ['原始名称', site.name], ['完整地址', site.url], ['主机', url.host],
            ['协议', url.protocol.slice(0, -1).toUpperCase()], ['打开方式', '嵌入页面'],
            ['嵌入提示', embedHint], ['管理位置', '--插件-- → 模块与站点']];
        content.getStore().loadData(fields.flatMap(function (field) {
            return [{ label: field[0], value: field[0], isLabel: true }, { label: field[0], value: field[1], isLabel: false }];
        }));
    }
    function createSiteSidebar(state) {
        return Ext.create('Ext.panel.Panel', {
            itemId: 'pve-extra-sites-panel', title: '站点信息', hidden: true, flex: 1, border: false,
            layout: 'fit',
            items: [{ xtype: 'grid', itemId: 'pve-extra-sites-info', border: false, hideHeaders: true,
                disableSelection: true, columnLines: false, rowLines: true,
                store: { fields: ['label', 'value', 'isLabel'], data: [] },
                columns: [{ dataIndex: 'value', flex: 1, sortable: false, menuDisabled: true, cellWrap: true,
                    renderer: function (value) {
                        return '<div style="white-space:normal;overflow-wrap:anywhere;word-break:break-word;line-height:1.6">' +
                            encoded(value) + '</div>';
                    } }],
                viewConfig: { enableTextSelection: true, markDirty: false, stripeRows: false, trackOver: false,
                    getRowClass: function (record) { return record.get('isLabel') ? 'x-grid-item-alt' : ''; } },
            }],
            tbar: [{ text: '刷新', itemId: 'pve-extra-sites-sidebar-refresh', iconCls: 'fa fa-refresh', disabled: true,
                handler: function () { const site = pageSite(state, state.page); if (site) openSite(state, site, true); } },
                { text: '新标签页打开', itemId: 'pve-extra-sites-open', iconCls: 'fa fa-external-link', disabled: true,
                    handler: function () {
                        const site = pageSite(state, state.page);
                        if (site) window.open(site.url, '_blank', 'noopener,noreferrer');
                    } }],
            bbar: [{ xtype: 'tbtext', itemId: 'pve-extra-sites-status', text: '', style: 'white-space:normal;' }],
            listeners: { beforedestroy: function () { leave(state); states.delete(state); } },
        });
    }
    function createState(native, workspace, manager) {
        const state = { native: native, workspace: workspace, manager: manager, panel: null, active: false, generation: 0, user: null,
            loaded: false, csrf: null, sites: [], selected: null, page: null, editor: null, read: null, write: null };
        function activate() {
            if (manager && !state.active && alive(state.panel)) { state.active = true; loadSites(state); }
        }
        const panel = manager ? Ext.create('Ext.grid.Panel', { itemId: 'pve-extra-sites-panel', title: '附加站点', hidden: !manager, border: false,
            store: { fields: ['id', 'name', 'url', 'revision', 'enabled', 'alias', 'icon', 'pluginIcon'], data: [] },
            columns: [{ text: '名称', dataIndex: 'name', flex: 1, renderer: function (value, meta, record) {
                return renderSiteIcon(record && record.get('pluginIcon')) + encoded(record && record.get('alias') || value);
            } },
                { text: 'URL', dataIndex: 'url', flex: 2, renderer: encoded }],
            viewConfig: { enableTextSelection: true, emptyText: '尚无附加站点', deferEmptyText: false },
            tbar: [{ text: '新建', itemId: 'pve-extra-sites-new', disabled: true, handler: function () { openEditor(state, null); } },
                { text: '编辑', itemId: 'pve-extra-sites-edit', disabled: true, handler: function () { openEditor(state, selectedSite(state)); } },
                { text: '刷新', itemId: 'pve-extra-sites-reload', handler: function () { if (state.active && !state.write) loadSites(state); } }],
            bbar: [{ xtype: 'tbtext', itemId: 'pve-extra-sites-status', text: '', style: 'white-space:normal;' }],
            listeners: { selectionchange: function (model, selected) {
                if (selected && selected[0]) state.selected = selected[0].data.id;
                controls(state);
            }, itemdblclick: function (view, record) { if (manager) { state.selected = record.data.id; openEditor(state, record.data); } },
                show: activate, activate: activate,
                afterrender: function () { if (manager && (!panel.isVisible || panel.isVisible(true))) activate(); },
                hide: function () { if (manager) leave(state); }, deactivate: function () { if (manager) leave(state); },
                beforedestroy: function () { leave(state); states.delete(state); } },
        }) : createSiteSidebar(state);
        state.panel = panel; panel._pveExtraSites = state; states.add(state);
        return state;
    }
    function ensurePanel(native) {
        if (native._pveExtraSites) return native._pveExtraSites;
        const workspace = native.up('pveStdWorkspace'), owner = native.ownerCt;
        if (!workspace || typeof workspace.setContent !== 'function' || !owner || typeof owner.add !== 'function') return null;
        const state = createState(native, workspace, false);
        native._pveExtraSites = state; owner.add(state.panel);
        native.on('beforedestroy', function () { leave(state); states.delete(state); if (alive(state.panel)) state.panel.destroy(); });
        return state;
    }
    function switchView(native, view) {
        const state = ensurePanel(native);
        if (!state) return;
        const active = isSiteView(view);
        if (active) {
            native.setVisible(false);
            const owner = native.ownerCt;
            ['#pve-komodo-container-tree', '#pve-safeline-tree'].forEach(function (selector) {
                const other = owner && owner.down && owner.down(selector); if (alive(other)) other.setVisible(false);
            });
            const selected = view.id.slice(viewPrefix.length);
            if (!state.active || state.selected !== selected) {
                leave(state); state.selected = selected; state.active = true; state.panel.setVisible(true); loadSites(state);
            } else state.panel.setVisible(true);
        } else if (state.active) leave(state);
        else state.panel.setVisible(false);
    }
    try {
        Ext.define('PVESites.override.ExtraSitesSelector', { override: 'PVE.form.ViewSelector', initComponent: function () {
            const result = this.callParent(arguments), selector = this;
            selectors.add(selector);
            // PVE applies persisted state before the asynchronous site records
            // exist. Retry once after a validated list, unless navigation changes.
            const applyState = this.applyState;
            if (typeof applyState === 'function') this.applyState = function (state) {
                delete selector._pveExtraSitesRestore;
                const missing = state && isSiteView({ id: state.value }) && !selector.getStore().findRecord('key', state.value, 0, false, true, true);
                const returned = applyState.apply(this, arguments);
                if (missing) selector._pveExtraSitesRestore = { value: state.value, initialValue: selector.getValue(),
                    user: window.Proxmox && window.Proxmox.UserName || null };
                return returned;
            };
            this.on('change', function () { delete selector._pveExtraSitesRestore; });
            this.on('select', function () { delete selector._pveExtraSitesRestore; });
            this.on('afterrender', function () {
                const workspace = selector.up('pveStdWorkspace');
                if (workspace) refreshNavigation(workspace);
            });
            this.on('beforedestroy', function () { delete selector._pveExtraSitesRestore; selectors.delete(selector); });
            return result;
        } });
        Ext.define('PVESites.override.ExtraSitesTree', { override: 'PVE.tree.ResourceTree', initComponent: function () {
            const result = this.callParent(arguments), native = this, previous = this.setViewFilter;
            if (typeof previous !== 'function') return result;
            this.setViewFilter = function (view) {
                const state = native._pveExtraSites;
                if (state && state.active && (!isSiteView(view) || state.selected !== view.id.slice(viewPrefix.length))) leave(state);
                let returned;
                if (isSiteView(view)) {
                    const selection = native.getSelectionModel();
                    selection.suspendEvents(false);
                    try { returned = previous.call(this, { id: view.id, groups: ['node'], getFilterFn: function () { return function () { return false; }; } }); }
                    finally { selection.resumeEvents(); }
                } else returned = previous.apply(this, arguments);
                switchView(native, view);
                return returned;
            };
            this.on('afterrender', function () {
                if (isSiteView(native.viewFilter)) native.setViewFilter(native.viewFilter);
            });
            return result;
        } });
        Ext.define('PVESites.override.ExtraSitesWorkspace', { override: 'PVE.StdWorkspace', onLogin: function (loginData) {
            const workspace = this;
            workspace._pveExtraSitesLoggedOut = !loginData;
            states.forEach(function (state) { if (state.workspace === workspace) { leave(state); state.selected = null; } });
            clearNavigation(workspace, !!loginData);
            const result = this.callParent(arguments);
            if (loginData) {
                refreshNavigation(workspace);
                states.forEach(function (state) {
                    if (state.workspace !== workspace) return;
                    if (!state.manager && isSiteView(state.native.viewFilter)) switchView(state.native, state.native.viewFilter);
                    else if (state.manager && (!state.panel.isVisible || state.panel.isVisible(true))) { state.active = true; loadSites(state); }
                });
            }
            return result;
        } });
        window.PVEPluginSites = {
            sync: function (workspace, sites) {
                if (!currentUser({ workspace: workspace }) || !Array.isArray(sites) || sites.length > 100 || !sites.every(validSite) ||
                    new Set(sites.map(function (site) { return site.id; })).size !== sites.length) return false;
                syncNavigation(workspace, sites, true); return true;
            },
            siteIcon: function (workspace, site) { return validSite(site) ? Object.assign({}, iconFor(workspace, site)) : Object.assign({}, fallbackIcon); },
            subscribe: function (workspace, callback) {
                let callbacks = siteSubscribers.get(workspace);
                if (!callbacks) {
                    callbacks = new Set(); siteSubscribers.set(workspace, callbacks);
                    workspace.on('beforedestroy', function () { siteSubscribers.delete(workspace); });
                }
                callbacks.add(callback);
                const entry = navigation.get(workspace);
                callback(entry && entry.user === currentUser({ workspace: workspace }) ? entry.sites.map(copySite) : []);
                return function () { callbacks.delete(callback); };
            },
            open: function (workspace, siteId) {
                const entry = navigation.get(workspace), user = currentUser({ workspace: workspace });
                const site = entry && entry.user === user && entry.sites.find(function (candidate) { return candidate.id === siteId; });
                if (!user || !site || !siteEnabled(site)) return false;
                const state = Array.from(states).find(function (candidate) { return candidate.workspace === workspace && !candidate.manager && alive(candidate.native); });
                const native = state ? state.native : workspace.down && workspace.down('pveResourceTree');
                if (!native || typeof native.setViewFilter !== 'function') return false;
                const selector = Array.from(selectors).find(function (candidate) { return alive(candidate) && candidate.up('pveStdWorkspace') === workspace; });
                if (selector) selector.setValue(viewPrefix + site.id);
                native.setViewFilter({ id: viewPrefix + site.id, groups: ['node'] });
                return true;
            },
            applyOrder: function (workspace) {
                const ordering = window.PVEPluginManagerNavigation;
                if (!ordering || typeof ordering.sortSites !== 'function') return;
                states.forEach(function (state) {
                    if (state.workspace !== workspace || !state.loaded) return;
                    state.sites = ordering.sortSites(state.sites);
                    if (!state.manager) {
                        updateSiteInfo(state, pageSite(state, state.page));
                        return;
                    }
                    if (!alive(state.panel)) return;
                    const positions = new Map(state.sites.map(function (site, index) { return [site.id, index]; }));
                    state.panel.getStore().sort({ direction: 'ASC', sorterFn: function (left, right) {
                        return positions.get(left.get('id')) - positions.get(right.get('id'));
                    } });
                });
            },
            createManager: function (workspace) { return createState(workspace, workspace, true).panel; },
            refresh: refreshNavigation,
        };
        window[marker] = true;
    } catch (error) {
        if (window.console && typeof window.console.warn === 'function') window.console.warn('PVE extra sites could not be registered.', error);
    }
}());
