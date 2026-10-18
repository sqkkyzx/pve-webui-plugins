(function () {
    // ExtJS callParent uses caller metadata; do not make these overrides strict.
    const Ext = window.Ext;
    const marker = '__pveExtraSitesLoaded';
    const viewId = 'pve-extra-sites';
    const api = '/pve-komodo/api/sites';
    if (window[marker] || !Ext || !Ext.ClassManager || typeof Ext.ClassManager.get !== 'function' || typeof Ext.define !== 'function') return;
    const Selector = Ext.ClassManager.get('PVE.form.ViewSelector');
    const Tree = Ext.ClassManager.get('PVE.tree.ResourceTree');
    const Workspace = Ext.ClassManager.get('PVE.StdWorkspace');
    if (!Selector || !Tree || !Workspace || !Selector.prototype || !Tree.prototype || !Workspace.prototype ||
        typeof Selector.prototype.initComponent !== 'function' || typeof Tree.prototype.initComponent !== 'function' ||
        typeof Workspace.prototype.onLogin !== 'function') return;

    const states = new Set();
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
    function validSite(site) {
        return site && typeof site.id === 'string' && /^[0-9a-f]{32}$/.test(site.id) && typeof site.name === 'string' &&
            site.name.trim().length > 0 && site.name.length <= 100 && !/[\u0000-\u001f\u007f]/.test(site.name) && urlValue(site.url) !== null &&
            Number.isSafeInteger(site.revision) && site.revision > 0;
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
        const ready = state.active && state.loaded && !!state.csrf && currentUser(state) === state.user && !state.write && !confirming(state);
        state.panel.down('#pve-extra-sites-new').setDisabled(!ready);
        state.panel.down('#pve-extra-sites-edit').setDisabled(!ready || !selectedSite(state));
        if (alive(state.page)) ['refresh', 'page-edit', 'remove'].forEach(function (action) {
            const button = state.page.down('#pve-extra-sites-' + action);
            if (alive(button)) button.setDisabled(!pageSite(state, state.page));
        });
    }
    function siteToolbar(state, getPage) {
        return [{ text: '刷新', itemId: 'pve-extra-sites-refresh', iconCls: 'fa fa-refresh', disabled: true,
            handler: function () { const site = pageSite(state, getPage()); if (site) openSite(state, site, true); } },
            { text: '编辑', itemId: 'pve-extra-sites-page-edit', iconCls: 'fa fa-pencil', disabled: true,
                handler: function () { const site = pageSite(state, getPage()); if (site) openEditor(state, site); } },
            { text: '移除', itemId: 'pve-extra-sites-remove', iconCls: 'fa fa-trash', disabled: true,
                handler: function () { const site = pageSite(state, getPage()); if (site) confirmDelete(state, null, site); } }];
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
        if (alive(state.panel)) state.panel.getStore().loadData([]);
        controls(state);
    }
    function leave(state) {
        state.active = false;
        cancel(state); clear(state);
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
        if (!validSite(site) || !current(state, state.generation, state.user)) return;
        state.selected = site.id;
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
                    dom.setAttribute('title', site.name);
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
    function parse(response) { try { return JSON.parse(response.responseText); } catch (error) { return null; } }
    function loadSites(state, notice) {
        if (!state.active || !alive(state.panel)) return;
        const user = currentUser(state);
        if (!user) { leave(state); status(state, '请先登录 PVE'); return; }
        if (state.user !== user) state.selected = null;
        cancel(state); clear(state);
        state.user = user;
        const generation = state.generation;
        status(state, '正在读取附加站点…');
        showPlaceholder(state, '选择左侧站点，或点击“新建”添加可信站点。');
        let completed = false;
        function complete(options, success, response) {
            if (completed) return;
            completed = true;
            if (!current(state, generation, user)) { if (state.active && generation === state.generation) { leave(state); status(state, '会话已改变，请重新进入附加站点'); } return; }
            state.read = null;
            if (response && (response.status === 401 || response.status === 403)) {
                clear(state); status(state, response.status === 401 ? 'PVE 会话已过期' : '无访问权限'); return;
            }
            const data = success ? parse(response) : null;
            if (!data || data.user !== user || !Array.isArray(data.sites) || data.sites.length > 100 ||
                !data.sites.every(validSite) || new Set(data.sites.map(function (site) { return site.id; })).size !== data.sites.length ||
                typeof data.csrf_token !== 'string' || !data.csrf_token || data.csrf_token.length > 2048) {
                clear(state); status(state, '站点列表读取失败，请重新进入附加站点'); return;
            }
            state.loaded = true; state.csrf = data.csrf_token; state.sites = data.sites;
            state.panel.getStore().loadData(data.sites);
            const selected = selectedSite(state) || data.sites[0];
            if (selected) {
                state.selected = selected.id;
                const record = state.panel.getStore().getById(selected.id);
                if (record) state.panel.getSelectionModel().select(record);
                else openSite(state, selected);
            } else { state.selected = null; status(state, '尚无附加站点'); }
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
                if (state.active && generation === state.generation) { leave(state); status(state, '会话已改变，请重新进入附加站点'); }
                return;
            }
            state.write = null;
            if (editor && !alive(editor)) { loadSites(state); return; }
            if (response && (response.status === 401 || response.status === 403)) {
                clear(state); status(state, response.status === 401 ? 'PVE 会话已过期' : '无访问权限或会话令牌已改变，请重新进入附加站点'); return;
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
                headers: { 'X-PVE-KOMODO-CSRF': state.csrf }, timeout: 45000, callback: complete });
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
        if (!state.loaded || !state.csrf || state.write || !current(state, state.generation, state.user)) return;
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
    function ensurePanel(native) {
        if (native._pveExtraSites) return native._pveExtraSites;
        const owner = native.ownerCt, workspace = native.up('pveStdWorkspace');
        if (!owner || typeof owner.add !== 'function' || !workspace || typeof workspace.setContent !== 'function') return null;
        const state = { native: native, workspace: workspace, panel: null, active: false, generation: 0, user: null,
            loaded: false, csrf: null, sites: [], selected: null, page: null, editor: null, read: null, write: null };
        const panel = Ext.create('Ext.grid.Panel', { itemId: 'pve-extra-sites-panel', hidden: true, flex: 1, border: false, hideHeaders: true,
            store: { fields: ['id', 'name', 'url', 'revision'], data: [], sorters: [{ property: 'name', direction: 'ASC' }] },
            columns: [{ text: '站点', dataIndex: 'name', flex: 1, renderer: encoded }],
            viewConfig: { enableTextSelection: true, emptyText: '尚无附加站点', deferEmptyText: false },
            tbar: [{ text: '新建', itemId: 'pve-extra-sites-new', disabled: true, handler: function () { openEditor(state, null); } },
                { text: '编辑', itemId: 'pve-extra-sites-edit', disabled: true, handler: function () { openEditor(state, selectedSite(state)); } }],
            bbar: [{ xtype: 'tbtext', itemId: 'pve-extra-sites-status', text: '', style: 'white-space:normal;' }],
            listeners: { selectionchange: function (model, selected) {
                if (selected && selected[0]) openSite(state, selected[0].data);
                controls(state);
            }, itemclick: function (view, record) { openSite(state, record.data); } },
        });
        state.panel = panel; native._pveExtraSites = state; states.add(state); owner.add(panel);
        native.on('beforedestroy', function () { leave(state); states.delete(state); if (alive(panel)) panel.destroy(); });
        return state;
    }
    function switchView(native, view) {
        const state = ensurePanel(native);
        if (!state) return;
        const active = !!view && view.id === viewId;
        state.panel.setVisible(active);
        if (active) {
            native.setVisible(false);
            // The prior Komodo wrapper owns this component and hides it when
            // leaving container view. Keep the shared side column unambiguous.
            const owner = native.ownerCt;
            const containers = owner && typeof owner.down === 'function' && owner.down('#pve-komodo-container-tree');
            if (alive(containers)) containers.setVisible(false);
            if (!state.active) { state.active = true; loadSites(state); }
        } else if (state.active) leave(state);
    }
    try {
        Ext.define('PVEKomodo.override.ExtraSitesSelector', { override: 'PVE.form.ViewSelector', initComponent: function () {
            const result = this.callParent(arguments), store = this.getStore();
            if (!store.findRecord('key', viewId, 0, false, true, true)) store.add({ key: viewId, value: '附加站点' });
            return result;
        } });
        Ext.define('PVEKomodo.override.ExtraSitesTree', { override: 'PVE.tree.ResourceTree', initComponent: function () {
            const result = this.callParent(arguments), native = this, previous = this.setViewFilter;
            if (typeof previous !== 'function') return result;
            this.setViewFilter = function (view) {
                const state = native._pveExtraSites;
                if (state && state.active && (!view || view.id !== viewId)) leave(state);
                let returned;
                if (view && view.id === viewId) {
                    const selection = native.getSelectionModel();
                    selection.suspendEvents(false);
                    try { returned = previous.call(this, { id: viewId, groups: ['node'], getFilterFn: function () { return function () { return false; }; } }); }
                    finally { selection.resumeEvents(); }
                } else returned = previous.apply(this, arguments);
                switchView(native, view);
                return returned;
            };
            this.on('afterrender', function () {
                if (native.viewFilter && native.viewFilter.id === viewId) native.setViewFilter(native.viewFilter);
            });
            return result;
        } });
        Ext.define('PVEKomodo.override.ExtraSitesWorkspace', { override: 'PVE.StdWorkspace', onLogin: function (loginData) {
            const workspace = this;
            workspace._pveExtraSitesLoggedOut = !loginData;
            if (!loginData) states.forEach(function (state) { if (state.workspace === workspace) { leave(state); state.selected = null; status(state, '请先登录 PVE'); } });
            const result = this.callParent(arguments);
            if (loginData) states.forEach(function (state) {
                if (state.workspace === workspace && state.native.viewFilter && state.native.viewFilter.id === viewId) {
                    state.active = true; state.panel.setVisible(true); loadSites(state);
                }
            });
            return result;
        } });
        window[marker] = true;
    } catch (error) {
        if (window.console && typeof window.console.warn === 'function') window.console.warn('PVE extra sites could not be registered.', error);
    }
}());
