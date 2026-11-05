(function () {
    // ExtJS callParent requires non-strict functions.
    const Ext = window.Ext, viewId = 'pve-plugin-manager', base = '/pve-plugins/api/';
    if (window.__pvePluginManagerLoaded || !Ext || !Ext.ClassManager || !Ext.define) return;
    if (!['PVE.form.ViewSelector', 'PVE.tree.ResourceTree', 'PVE.StdWorkspace'].every(function (name) { return Ext.ClassManager.get(name); })) return;
    const states = new Set();
    const labels = { komodo: 'Komodo', temperatures: '节点温度', safeline: 'SafeLine WAF' };
    const pageNames = { modules: '模块与站点', komodo: 'Komodo 连接', temperatures: '节点温度', safeline: 'SafeLine 连接' };
    const defaultIcons = { komodo: 'fa fa-cubes', temperatures: 'fa fa-thermometer-half', safeline: 'fa fa-shield', sites: 'fa fa-external-link' };
    // Release metadata belongs to the host so disabled modules remain documented
    // without loading their scripts or contacting their services.
    const moduleInfo = {
        komodo: {
            coverage: '部分管理', maturity: 'testing',
            summary: '将 Komodo Core 中的服务器、应用栈和 Docker 容器接入 PVE 风格的树、表格与终端。连接凭据由服务端保存。',
            features: ['浏览服务器、应用栈与容器，查看状态、日志、告警和操作记录。',
                '执行已支持的容器与应用栈操作，编辑配置并使用终端。', '将服务器关联到 PVE 的 VM/CT，从相应对象进入 Docker 页面。'],
            limits: ['统计、计划任务仍含占位页面，部署与代码仓库仍有未开放的管理功能。',
                '当前为部分管理集成，不能完整替代 Komodo 官方控制台；可用操作受 Core 服务账号权限限制。'],
            validation: '已覆盖自动化回归与 PVE 原生界面集成检查；完整外部管理流程仍需在所用 Core / Periphery 版本逐项验收。',
            setup: ['在“模块与站点”中编辑 Komodo，启用后保存。',
                '在“基本配置”填写 Core 地址、API Key、API Secret 与 TLS 设置，然后点击顶部“保存并应用”。',
                '凭据留空保留已存值；更换 Core 地址时需要同时填写新的 API Key 与 API Secret。'],
            usage: ['在左上角视图选择器进入 Komodo。设置的显示别名会用于菜单和树。',
                '按需将服务器关联到 VM/CT，再使用已开放的资源操作、日志和终端。'],
            troubleshooting: [['无法连接', '核对容器到 Core 的连通性、管理地址及 TLS 证书。'],
                ['资源或操作不可用', '核对 Core 服务账号权限及 Periphery 状态；占位功能仍需使用官方控制台。'],
                ['终端断开', '离页、注销或修改模块连接配置会终止本次终端连接。']],
        },
        temperatures: {
            coverage: '只读监测', maturity: 'testing',
            summary: '在 PVE 节点概要中显示 CPU、主板、磁盘温度及传感器明细。模块只读取监测数据。',
            features: ['从 PVE 自动发现在线节点的 IP，默认读取 node_exporter 的 TCP 9100 端口。',
                '显示传感器原始标签、采样时间及设备提供的阈值，区分真实零值、缺测和过期数据。',
                '支持为指定节点设置来源覆盖，接入其他端口、TLS、认证代理或自定义温度采集器。'],
            limits: ['温度来自硬件驱动暴露的 hwmon 数据；缺失的传感器读数无法由界面补出。',
                '此模块提供只读温度监测，不涉及硬件控制或完整节点管理。'],
            validation: '自动发现、指标解析及界面已有自动化回归；不同硬件、驱动与实际 node_exporter 部署仍需现场验证。',
            setup: ['在“模块与站点”中启用节点温度并保存。',
                '在各 PVE 节点准备带 hwmon 指标的 node_exporter，并确保插件容器可以访问节点 IP 的 TCP 9100。通常无需添加来源覆盖。',
                '特殊地址、端口、TLS 或认证场景在“基本配置”新增节点覆盖；对话框点“确定”后仍需点击顶部“保存并应用”。'],
            usage: ['进入 PVE 节点概要查看温度与明细；页面激活期间每 10 秒读取。',
                '删除来源覆盖并“保存并应用”后，该节点恢复自动发现。来源令牌留空可保留已存值。'],
            troubleshooting: [['不可用', '核对节点在线状态、PVE 返回的 IP、9100 监听、防火墙及 /metrics?collect[]=hwmon 中的温度指标。'],
                ['未知或类别缺失', '查看明细中的芯片及原始标签，核对硬件驱动是否提供对应数据。缺测不会补零。'],
                ['已过期', '样本超过 30 秒；核对采集服务、节点网络与最近采样时间。']],
        },
        safeline: {
            coverage: '部分管理', maturity: 'testing',
            summary: '在 PVE 中查看和管理 SafeLine WAF 的防护应用、规则、证书、IP 组，以及事件、日志和统计。',
            features: ['查看防护概况、事件、日志和统计，并使用目标版本开放的筛选能力。',
                '管理已支持的防护应用、规则、黑白名单、TLS 证书与 IP 组。',
                '提供原生编辑、连接测试、修订核对和操作回执。'],
            limits: ['当前覆盖部分管理能力，不能完整替代 SafeLine 官方控制台。具体能力取决于目标版本及授权。',
                '实际 9.4.2 CN 个人版验收以读取为主；写入流程已有自动化测试，仍需在目标版本逐项验收。'],
            validation: '测试中。已完成的界面与模拟读写测试不代表真实环境中所有写操作均已验收。',
            setup: ['在“模块与站点”中启用 SafeLine WAF 并保存，随后进入“基本配置”。',
                '填写固定 HTTPS 管理地址、API Token 与 TLS 设置；按需填写证书服务器名称和 PEM 格式 CA。',
                '点击“测试连接”核对连通性，再点击“保存连接”。测试不会保存配置；此连接由下方按钮单独保存。'],
            usage: ['从视图选择器进入 SafeLine WAF，按页面开放的能力管理资源。',
                'API Token 留空保留已存值；“清除连接”会删除已存连接配置。'],
            troubleshooting: [['连接或认证失败', '核对容器到管理地址的连通性、API Token、TLS 服务器名称和 CA。'],
                ['选项不可用', '核对目标 SafeLine 版本、授权及接口能力，部分统计仅支持全站或特定时间范围。'],
                ['操作结果未确认', '先在官方控制台核对实际状态，再重新读取；避免直接重复提交写操作。']],
        },
    };
    const maturityLabels = { stable: '稳定', testing: '测试中', experimental: '实验性', debug: '调试中' };
    const badgeColors = { module: ['#e7f0fb', '#174e83'], site: ['#fff0dc', '#804600'],
        stable: ['#e2f2e5', '#216035'], testing: ['#fff0dc', '#804600'],
        experimental: ['#eee7f8', '#64418a'], debug: ['#f9e5e5', '#8c3030'], neutral: ['#ededed', '#454545'] };
    const navigation = window.PVEPluginManagerNavigation;
    function defaultNavigation() { return { entry_order: Object.keys(labels), module_aliases: {}, module_icons: {} }; }
    function copy(value) { return JSON.parse(JSON.stringify(value)); }
    function moduleLabel(id, preferences) {
        if (navigation && navigation.moduleLabel) return navigation.moduleLabel(id, preferences);
        return preferences && preferences.module_aliases && preferences.module_aliases[id] || labels[id];
    }
    function moduleIcon(id, preferences) {
        if (navigation && navigation.moduleIcon) return navigation.moduleIcon(id, preferences);
        return preferences && preferences.module_icons && preferences.module_icons[id] || { type: 'pve', value: defaultIcons[id] };
    }
    function renderIcon(icon) { return navigation && navigation.renderIcon ? navigation.renderIcon(icon) : ''; }
    function pageTitle(state, id) {
        const label = Object.prototype.hasOwnProperty.call(labels, id) ? moduleLabel(id, state.navigation) : pageNames[id];
        return '插件管理 / ' + label;
    }
    function alive(c) { return c && !c.destroyed && !c.destroying; }
    function enc(value) { return String(value == null ? '' : value).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
    function badge(text, tone, tooltip) {
        const colors = badgeColors[tone] || badgeColors.neutral;
        return '<span title="' + enc(tooltip || text) + '" style="display:inline-block;padding:1px 6px;border-radius:3px;' +
            'background:' + colors[0] + ';color:' + colors[1] + ';line-height:18px">' + enc(text) + '</span>';
    }
    function progressDetail(info) { return info.limits.join(' ') + ' ' + info.validation; }
    function renderProgress(info, meta) {
        const detail = info ? progressDetail(info) : '外部站点的功能与成熟度由目标项目维护，本项目不作评级。';
        if (meta) meta.tdAttr = 'data-qtip="' + enc(detail) + '"';
        return info ? badge(info.coverage, 'neutral', detail) + ' ' + badge(maturityLabels[info.maturity], info.maturity, detail) : badge('外部站点', 'neutral', detail);
    }
    function workspaceOf(state) {
        // PVE constructs the resource tree before adding it to the workspace.
        // Resolve its owner when used, after Ext has attached the component.
        state.workspace = alive(state.native) ? state.native.up('pveStdWorkspace') : null;
        return state.workspace;
    }
    function user(state) {
        const workspace = workspaceOf(state), p = window.Proxmox;
        return alive(workspace) && !workspace._pvePluginManagerLoggedOut && p &&
            (!p.Utils || !p.Utils.authOK || p.Utils.authOK()) && p.UserName || null;
    }
    function current(state, generation, expectedUser) { return state.active && alive(state.page) && state.generation === generation && user(state) === expectedUser; }
    function notice(state, text) { if (alive(state.page)) state.page.down('#plugin-manager-status').setText(enc(text)); }
    function errorText(response) {
        if (!response) return '请求未获确认，请重新读取并核对结果。';
        if (response.status === 401) return 'PVE 会话已过期，请重新登录。';
        if (response.status === 403) return '无访问权限或会话令牌已改变，请重新进入插件管理。';
        if (response.status === 409) return '配置已被其他操作修改，请重新读取后编辑。';
        if (response.status === 422) return '配置无效，请检查地址、凭据和节点温度设置。';
        return '操作失败，请重新读取并核对配置。';
    }
    function request(state, path, method, body, csrf, done) {
        const generation = state.generation, expectedUser = state.user;
        let pending, completed = false;
        function complete(options, success, response) {
            completed = true; state.requests.delete(pending);
            if (!current(state, generation, expectedUser)) {
                if (state.active && state.generation === generation) leave(state);
                return;
            }
            let data;
            try { data = success && JSON.parse(response.responseText); } catch (e) { data = null; }
            done(data, response);
        }
        try {
            pending = Ext.Ajax.request({ url: base + path, method: method, jsonData: body,
                headers: csrf ? { 'X-PVE-Plugins-CSRF': csrf } : {}, timeout: 90000, disableCaching: false, callback: complete });
            if (!completed) state.requests.add(pending);
        } catch (e) { complete(null, false, null); }
    }
    const sitePrefix = 'pve-extra-site-';
    function siteTools() { return window.PVEPluginSites; }
    function closeEditor(state) {
        const editor = state.entryEditor; state.entryEditor = null;
        if (alive(editor)) editor.destroy();
        if (alive(state.confirmation)) state.confirmation.destroy();
        state.confirmation = null;
    }
    function leave(state) {
        state.active = false; state.generation++;
        state.requests.forEach(function (pending) { Ext.Ajax.abort(pending); }); state.requests.clear();
        if (state.unsubscribe) state.unsubscribe(); state.unsubscribe = null;
        closeEditor(state);
        if (alive(state.nodeEditor)) state.nodeEditor.destroy(); state.nodeEditor = null;
        const page = state.page; state.page = null;
        state.data = null; state.safe = null; state.nodes = {}; state.navigation = null;
        state.sites = []; state.sitesLoaded = false; state.siteCsrf = null; state.pendingOrder = null;
        if (alive(page)) page.destroy();
        if (alive(state.tree)) { state.tree.setVisible(false); updateNavigation(state); }
    }
    function selectedEntry(state) {
        return alive(state.page) && state.page.down('#plugin-entry-grid').getSelectionModel().getSelection()[0] || null;
    }
    function entryIcon(state, row) {
        if (row.kind === 'module') return row.icon || moduleIcon(row.id, { module_icons: {} });
        const sites = siteTools();
        return sites && sites.siteIcon ? sites.siteIcon(state.workspace, row.site) : row.icon || { type: 'pve', value: 'fa fa-globe' };
    }
    function entries(state) {
        const preferences = state.navigation || defaultNavigation();
        const rows = Object.keys(labels).map(function (id) {
            return { id: id, kind: 'module', name: labels[id], enabled: !!state.data && state.data.enabled_plugins.includes(id),
                alias: preferences.module_aliases[id] || '', icon: preferences.module_icons[id] || null, url: '', progress: moduleInfo[id] };
        });
        state.sites.forEach(function (site) {
            rows.push({ id: sitePrefix + site.id, kind: 'site', siteId: site.id, name: site.name, enabled: site.enabled,
                alias: site.alias, icon: site.icon, url: site.url, revision: site.revision, site: site });
        });
        rows.forEach(function (row) { row.pluginIcon = entryIcon(state, row); });
        return rows;
    }
    function sortStore(store, order) {
        const ranks = new Map(order.map(function (id, index) { return [id, index]; }));
        const original = new Map(store.getRange().map(function (record, index) { return [record.getId(), index]; }));
        store.sort({ direction: 'ASC', sorterFn: function (left, right) {
            const a = ranks.has(left.getId()) ? ranks.get(left.getId()) : ranks.size + original.get(left.getId());
            const b = ranks.has(right.getId()) ? ranks.get(right.getId()) : ranks.size + original.get(right.getId());
            return a - b;
        } });
    }
    function reconcileEntries(state) {
        if (!alive(state.page) || !state.data) return;
        const grid = state.page.down('#plugin-entry-grid'), store = grid.getStore();
        const pending = new Map(entries(state).map(function (row) { return [row.id, row]; }));
        store.getRange().forEach(function (record) {
            const row = pending.get(record.getId());
            if (row) { record.set(row); pending.delete(row.id); }
            else store.remove(record);
        });
        pending.forEach(function (row) { store.add(row); });
        sortStore(store, state.pendingOrder || state.navigation.entry_order);
        if (!state.pendingOrder) state.savedOrder = store.getRange().map(function (record) { return record.getId(); });
        entryControls(state); updateNavigation(state);
    }
    function selectPage(state, id, section) {
        state.selectedNode = id;
        const pageId = Object.prototype.hasOwnProperty.call(labels, id) ? id : 'modules';
        state.selectedPage = pageId;
        if (!alive(state.page)) return;
        const pages = state.page.down('#plugin-manager-pages');
        pages.getLayout().setActiveItem(pages.down('#plugin-page-' + pageId));
        if (pageId !== 'modules') {
            const module = pages.down('#plugin-page-' + pageId);
            activateModuleSection(module, section ? module.down('#plugin-' + section + '-' + pageId) : module.getLayout().getActiveItem());
        }
        state.page.setTitle(enc(pageTitle(state, pageId)));
        if (id.indexOf(sitePrefix) === 0) {
            const grid = state.page.down('#plugin-entry-grid');
            const record = grid.getStore().getById(id);
            if (record) grid.getSelectionModel().select(record);
        }
    }
    function updateNavigation(state) {
        if (!alive(state.tree)) return;
        const store = state.tree.getStore(), root = store.getNodeById('modules');
        const rows = entries(state), pending = new Map(rows.map(function (row) { return [row.id, row]; }));
        root.childNodes.slice().forEach(function (record) {
            const row = pending.get(record.getId());
            if (!row) record.remove();
        });
        rows.forEach(function (row) {
            const data = { id: row.id, text: row.alias || row.name, kind: row.kind, leaf: true,
                status: state.data ? row.enabled ? '已启用' : '已停用' : '', pluginIcon: row.pluginIcon,
                iconCls: navigation && navigation.renderIcon ? 'pve-plugin-managed-icon' : row.kind === 'site' ? 'fa fa-globe' : defaultIcons[row.id] };
            const record = store.getNodeById(row.id);
            if (record) record.set(data); else root.appendChild(data);
        });
        sortStore(store, ['modules'].concat((state.navigation || (navigation ? navigation.get() : defaultNavigation())).entry_order));
        if (alive(state.page)) state.page.setTitle(enc(pageTitle(state, state.selectedPage || 'modules')));
    }
    function ensureNavigation(state) {
        if (alive(state.tree)) return state.tree;
        const owner = state.native.ownerCt;
        if (!owner || typeof owner.add !== 'function') return null;
        state.tree = Ext.create('Ext.tree.Panel', {
            itemId: 'pve-plugin-manager-tree', flex: 1, border: false, rootVisible: true, useArrows: true, hideHeaders: true,
            store: { fields: ['text', 'kind', 'status', 'pluginIcon'], root: { id: 'modules', text: '插件管理', expanded: true,
                iconCls: 'fa fa-plug', children: [] } },
            columns: [{ xtype: 'treecolumn', dataIndex: 'text', flex: 1, renderer: function (value, meta, record) {
                if (record.getId() === 'modules') return enc(value);
                const prefix = record.get('kind') === 'site' ? '站点 / ' : '模块 / ';
                const status = record.get('status');
                return renderIcon(record.get('pluginIcon')) + enc(prefix + value) +
                    (status ? ' <span style="color:var(--pve-text-color-secondary,#777)">· ' + enc(status) + '</span>' : '');
            } }],
            listeners: { selectionchange: function (model, records) {
                if (state.active && records && records[0]) selectPage(state, records[0].getId());
            }, itemclick: function (view, record) { if (state.active) selectPage(state, record.getId()); } },
        });
        owner.add(state.tree); updateNavigation(state); return state.tree;
    }
    function entryControls(state) {
        if (!alive(state.page)) return;
        const row = selectedEntry(state), busy = !state.data || !state.sitesLoaded || state.saving || state.safeSaving;
        const grid = state.page.down('#plugin-entry-grid');
        grid.down('#plugin-entry-new').setDisabled(busy);
        grid.down('#plugin-entry-edit').setDisabled(busy || !row);
        grid.down('#plugin-entry-delete').setDisabled(busy || !row || row.get('kind') !== 'site');
        grid.down('#plugin-entry-open').setDisabled(busy || !row || row.get('kind') !== 'site' || !row.get('enabled'));
        grid.down('#plugin-entry-configure').setDisabled(busy || !row || row.get('kind') !== 'module');
        const rows = grid.getStore().getRange(), index = rows.indexOf(row);
        grid.down('#plugin-entry-up').setDisabled(busy || index <= 0);
        grid.down('#plugin-entry-down').setDisabled(busy || index < 0 || index === rows.length - 1);
    }
    function moveEntry(state, delta) {
        if (!state.data || !state.sitesLoaded || state.saving || state.safeSaving || alive(state.entryEditor)) return;
        const row = selectedEntry(state), store = state.page.down('#plugin-entry-grid').getStore();
        const ids = store.getRange().map(function (record) { return record.getId(); });
        const from = row ? ids.indexOf(row.getId()) : -1, to = from + delta;
        if (from < 0 || to < 0 || to >= ids.length) return;
        ids.splice(from, 1); ids.splice(to, 0, row.getId());
        state.pendingOrder = JSON.stringify(ids) === JSON.stringify(state.savedOrder) ? null : ids;
        sortStore(store, ids); entryControls(state);
        notice(state, '顺序已调整，点击“保存并应用”后同步到树和视图菜单。');
    }
    function readNavigation(state) {
        const value = copy(state.navigation);
        value.entry_order = state.page.down('#plugin-entry-grid').getStore().getRange().map(function (record) { return record.getId(); });
        return value;
    }
    function normalizeAlias(input) {
        if (typeof input !== 'string' || /[\u0000-\u001f\u007f-\u009f]/.test(input)) return null;
        const value = input.replace(/^\p{White_Space}+|\p{White_Space}+$/gu, '');
        return Array.from(value).length <= 100 ? value : null;
    }
    function editorIcon(editor) {
        const type = editor.down('#entry-icon-type').getValue();
        if (type === 'default') return null;
        if (type !== 'pve' && type !== 'url') return undefined;
        const value = String(editor.down(type === 'pve' ? '#entry-icon-native' : '#entry-icon-url').getValue() || '').trim();
        return navigation && navigation.normalizeIcon && navigation.normalizeIcon({ type: type, value: value }) || undefined;
    }
    function validSite(site) {
        return site && /^[0-9a-f]{32}$/.test(site.id) && normalizeAlias(site.name) && typeof site.url === 'string' &&
            (!navigation || !navigation.normalizeIcon || navigation.normalizeIcon({ type: 'url', value: site.url })) &&
            typeof site.enabled === 'boolean' && normalizeAlias(site.alias) !== null &&
            (site.icon === null || navigation && navigation.normalizeIcon && navigation.normalizeIcon(site.icon)) && Number.isSafeInteger(site.revision);
    }
    function publishSites(state) {
        const tools = siteTools();
        if (tools && tools.sync) tools.sync(state.workspace, state.sites);
        reconcileEntries(state);
    }
    function loadSites(state) {
        state.sitesLoaded = false; entryControls(state);
        request(state, 'sites', 'GET', undefined, null, function (data, response) {
            if (!data || data.user !== state.user || !Array.isArray(data.sites) || data.sites.length > 100 || !data.sites.every(validSite) || !data.csrf_token) {
                notice(state, '站点列表读取失败。' + errorText(response)); return;
            }
            state.sites = data.sites; state.siteCsrf = data.csrf_token; state.sitesLoaded = true;
            publishSites(state); entryControls(state);
            state.page.down('#plugin-manager-save').setDisabled(!state.data);
        });
    }
    function setBusy(state, busy) {
        state.saving = busy;
        state.page.down('#plugin-manager-save').setDisabled(busy || !state.data || !state.sitesLoaded);
        state.page.down('#plugin-manager-pages').setDisabled(busy || !state.data);
        state.page.down('#plugin-manager-reload').setDisabled(busy);
        if (!busy && state.data) Object.keys(labels).forEach(function (id) {
            const panel = state.page.down('#plugin-page-' + id);
            if (!activateModuleSection(panel, panel.getLayout().getActiveItem())) activateModuleSection(panel, panel.down('#plugin-intro-' + id));
        });
        entryControls(state);
    }
    function acceptConfig(state, data, response, editor, orderSaved) {
        if (response && response.status === 422) {
            setBusy(state, false);
            if (alive(editor)) {
                editor.down('#entry-error').setValue(errorText(response) + ' 输入已保留，修改后可再次保存。');
                editor.down('#entry-save').setDisabled(false);
                editor.down('#entry-cancel').setDisabled(false);
            }
            notice(state, errorText(response) + ' 输入已保留，修改后可再次保存。'); return;
        }
        if (!data || !Number.isSafeInteger(data.revision) || data.revision <= state.data.revision || !data.navigation ||
            !navigation || !navigation.publish(data.navigation, data.revision)) {
            state.data = null; setBusy(state, false);
            if (alive(editor)) {
                editor._blocked = true;
                editor.down('#entry-error').setValue(errorText(response) + ' 请关闭窗口并重新读取配置。');
                editor.down('#entry-cancel').setDisabled(false);
            }
            notice(state, errorText(response) + ' 当前输入已保留，重新读取后可继续。'); return;
        }
        if (orderSaved) {
            state.pendingOrder = null;
            state.connectionBaseline = JSON.stringify(moduleSettings(state));
        }
        state.data = data; state.navigation = copy(data.navigation);
        if (alive(editor)) editor.destroy();
        if (data.reload_required !== false) { notice(state, '已保存，正在刷新页面…'); window.location.reload(); return; }
        setBusy(state, false); reconcileEntries(state);
        notice(state, orderSaved && safeDraftChanged(state) ? '模块设置和排序已保存。SafeLine 连接草稿仍需点击“保存连接”。' : '设置已保存并生效。');
    }
    function saveEntry(state, editor, row, generation, expectedUser, source) {
        if (!alive(editor) || state.entryEditor !== editor || !current(state, generation, expectedUser) ||
            !state.data || state.saving || state.safeSaving || editor._blocked) return;
        const isSite = !row || row.get('kind') === 'site';
        if (row && !state.page.down('#plugin-entry-grid').getStore().getRange().includes(row)) return;
        const alias = normalizeAlias(String(editor.down('#entry-alias').getValue() || ''));
        const icon = editorIcon(editor);
        const name = isSite ? normalizeAlias(String(editor.down('#entry-name').getValue() || '')) : row.get('name');
        const url = String(editor.down('#entry-target').getValue() || '').trim();
        if (alias === null || !name || icon === undefined || isSite && (!navigation || !navigation.normalizeIcon({ type: 'url', value: url }))) {
            editor.down('#entry-error').setValue('请填写名称、有效的显示别名和图标；站点目标须为不含账号密码的 HTTP(S) URL。'); return;
        }
        const enabled = !!editor.down('#entry-enabled').getValue();
        if (!isSite && enabled !== state.data.enabled_plugins.includes(row.getId()) &&
            (state.pendingOrder || JSON.stringify(moduleSettings(state)) !== state.connectionBaseline ||
                safeDraftChanged(state))) {
            editor.down('#entry-error').setValue('有未保存的连接配置或排序，请先保存并应用，再更改模块启用状态。'); return;
        }
        let path, method, body, csrf;
        if (isSite) {
            path = 'sites' + (row ? '/' + source.id : ''); method = row ? 'PATCH' : 'POST'; csrf = state.siteCsrf;
            body = { name: name, url: url, enabled: enabled, alias: alias, icon: icon };
            if (row) body.expected_revision = source.revision;
        } else {
            path = 'config'; method = 'PUT'; csrf = state.data.csrf_token;
            const preferences = copy(state.data.navigation), id = row.getId();
            if (alias) preferences.module_aliases[id] = alias; else delete preferences.module_aliases[id];
            if (icon) preferences.module_icons[id] = icon; else delete preferences.module_icons[id];
            const plugins = state.data.enabled_plugins.filter(function (plugin) { return plugin !== id || enabled; });
            if (enabled && !plugins.includes(id)) plugins.push(id);
            body = { expected_revision: source.revision, enabled_plugins: plugins, navigation: preferences };
        }
        setBusy(state, true);
        editor.down('#entry-save').setDisabled(true); editor.down('#entry-cancel').setDisabled(true);
        request(state, path, method, body, csrf, function (data, response) {
            if (!isSite) { acceptConfig(state, data, response, editor, false); return; }
            if (data && validSite(data.site) && (!row || data.site.id === source.id && data.site.revision > source.revision)) {
                const index = state.sites.findIndex(function (site) { return site.id === data.site.id; });
                if (index < 0) state.sites.push(data.site); else state.sites[index] = data.site;
                editor.destroy(); setBusy(state, false); publishSites(state);
                state.selectedNode = sitePrefix + data.site.id;
                const treeRow = state.tree.getStore().getNodeById(state.selectedNode);
                if (treeRow) state.tree.getSelectionModel().select(treeRow);
                notice(state, '站点设置已保存。'); return;
            }
            setBusy(state, false); editor._blocked = true;
            editor.down('#entry-error').setValue(errorText(response) + ' 当前输入已保留，请关闭窗口并重新编辑。');
            editor.down('#entry-cancel').setDisabled(false);
        });
    }
    function editEntry(state, row) {
        if (!state.data || !state.sitesLoaded || state.saving || state.safeSaving || !current(state, state.generation, state.user)) return;
        closeEditor(state);
        const isSite = !row || row.get('kind') === 'site';
        const source = isSite ? row ? copy(row.get('site')) : null : { revision: state.data.revision };
        const generation = state.generation, expectedUser = state.user;
        const icon = row && row.get('icon'), defaultIcon = isSite ? { type: 'pve', value: 'fa fa-globe' } : moduleIcon(row.getId(), { module_icons: {} });
        let editor;
        function iconFields() {
            if (!alive(editor)) return;
            const type = editor.down('#entry-icon-type').getValue();
            editor.down('#entry-icon-native').setVisible(type === 'pve'); editor.down('#entry-icon-url').setVisible(type === 'url');
            editor.down('#entry-icon-preview').update(renderIcon(editorIcon(editor) || row && row.get('pluginIcon') || defaultIcon));
        }
        editor = Ext.create('Ext.window.Window', { itemId: 'plugin-entry-editor', title: row ? '编辑' + (isSite ? '附加站点' : '功能模块') : '新增附加站点',
            modal: true, constrain: true, width: 640, height: 510, layout: 'fit',
            items: [form(null, [field('entry-kind', '类型', { value: isSite ? '附加站点' : '功能模块', readOnly: true }),
                field('entry-name', '名称', { value: row ? row.get('name') : '', readOnly: !isSite }),
                field('entry-enabled', '启用', { xtype: 'checkbox', checked: row ? row.get('enabled') : true }),
                field('entry-alias', '显示别名', { value: row ? row.get('alias') : '' }),
                field('entry-target', '站点目标', { value: row ? row.get('url') : '', hidden: !isSite, emptyText: 'https://dashboard.example/' }),
                field('entry-icon-type', '图标来源', { xtype: 'combobox', editable: false, queryMode: 'local', value: icon ? icon.type : 'default',
                    store: [['default', isSite ? '自动获取站点图标' : '默认图标'], ['pve', 'PVE 原生图标'], ['url', '图标 URL']], listeners: { change: iconFields } }),
                field('entry-icon-native', 'PVE 图标', { xtype: 'combobox', editable: true, queryMode: 'local', value: icon && icon.type === 'pve' ? icon.value : defaultIcon.value,
                    store: [['fa fa-cubes', '容器组（fa-cubes）'], ['fa fa-server', '服务器（fa-server）'], ['fa fa-desktop', '虚拟机（fa-desktop）'],
                        ['fa fa-database', '存储（fa-database）'], ['fa fa-globe', '网站（fa-globe）'], ['fa fa-shield', '防护（fa-shield）'],
                        ['fa fa-cloud', '云（fa-cloud）'], ['fa fa-cog', '设置（fa-cog）'], ['fa fa-external-link', '外部链接（fa-external-link）']], listeners: { change: iconFields } }),
                field('entry-icon-url', '图标 URL', { value: icon && icon.type === 'url' ? icon.value : '', emptyText: 'https://example.com/icon.svg', listeners: { change: iconFields } }),
                { xtype: 'component', itemId: 'entry-icon-preview', margin: '4 0 12 140', html: '' },
                { xtype: 'displayfield', value: '显示别名留空时使用名称。选择自动或默认图标可恢复初始图标设置。' },
                { xtype: 'displayfield', value: isSite ? '站点设置保存后立即生效。' : '更改模块启用状态后会刷新页面，请先保存连接配置和排序。' },
                { xtype: 'displayfield', itemId: 'entry-error', value: '' }])],
            buttons: [{ text: '保存', itemId: 'entry-save', handler: function () { saveEntry(state, editor, row, generation, expectedUser, source); } },
                { text: '取消', itemId: 'entry-cancel', handler: function () { editor.destroy(); } }],
            listeners: { afterrender: iconFields, beforeclose: function () { return !state.saving; }, destroy: function () {
                if (state.entryEditor !== editor) return;
                state.entryEditor = null;
                if (editor._blocked && state.active && !state.saving) loadSites(state);
            } },
        });
        state.entryEditor = editor; editor.show();
    }
    function deleteEntry(state) {
        const row = selectedEntry(state);
        if (!row || row.get('kind') !== 'site' || state.saving || state.safeSaving || !state.data || !state.sitesLoaded) return;
        const source = copy(row.get('site')), generation = state.generation, expectedUser = state.user;
        const dialog = Ext.create('Ext.window.MessageBox', { closeAction: 'destroy' }); state.confirmation = dialog;
        dialog.show({ title: '删除附加站点', message: '确定删除站点“' + enc(source.alias || source.name) + '”？', buttons: Ext.Msg.YESNO, icon: Ext.Msg.WARNING,
            fn: function (answer) {
                if (state.confirmation !== dialog) return;
                state.confirmation = null; dialog.destroy();
                if (answer !== 'yes' || !current(state, generation, expectedUser) || state.saving || state.safeSaving) return;
                setBusy(state, true);
                request(state, 'sites/' + source.id, 'DELETE', { expected_revision: source.revision }, state.siteCsrf, function (data, response) {
                    setBusy(state, false);
                    if (data && data.deleted === true && data.id === source.id) {
                        state.sites = state.sites.filter(function (site) { return site.id !== source.id; });
                        publishSites(state); selectPage(state, 'modules'); notice(state, '站点已删除。');
                    } else { notice(state, errorText(response)); loadSites(state); }
                });
            } });
    }
    function entriesPage(state) {
        return { xtype: 'panel', title: '模块与站点', layout: 'fit', border: false, items: [{
            xtype: 'grid', itemId: 'plugin-entry-grid', border: false,
            store: { fields: ['id', 'kind', 'name', 'enabled', 'alias', 'icon', 'url', 'siteId', 'revision', 'site', 'pluginIcon', 'progress'], data: [] },
            columns: [{ xtype: 'rownumberer', text: '顺序', width: 55 },
                { text: '类型', dataIndex: 'kind', width: 110, renderer: function (value) { return badge(value === 'site' ? '附加站点' : '功能模块', value === 'site' ? 'site' : 'module'); } },
                { text: '名称', dataIndex: 'name', width: 200, renderer: function (value, meta, row) { return renderIcon(row.get('pluginIcon')) + enc(value); } },
                { text: '开发进度', dataIndex: 'progress', width: 220, sortable: false, renderer: renderProgress },
                { text: '启用状态', dataIndex: 'enabled', width: 100, renderer: function (value) { return value ? '已启用' : '已停用'; } },
                { text: '显示别名', dataIndex: 'alias', flex: 1, minWidth: 130, renderer: enc },
                { text: '图标', dataIndex: 'icon', width: 110, renderer: function (value, meta, row) { return value ? value.type === 'pve' ? 'PVE 原生图标' : '图标 URL' : row.get('kind') === 'site' ? '自动获取' : '默认图标'; } },
                { text: '站点目标', dataIndex: 'url', flex: 2, minWidth: 220, renderer: enc }],
            tbar: [{ text: '新增站点', itemId: 'plugin-entry-new', disabled: true, handler: function () { editEntry(state, null); } },
                { text: '编辑', itemId: 'plugin-entry-edit', disabled: true, handler: function () { editEntry(state, selectedEntry(state)); } },
                { text: '删除', itemId: 'plugin-entry-delete', disabled: true, handler: function () { deleteEntry(state); } },
                { text: '上移', itemId: 'plugin-entry-up', disabled: true, iconCls: 'fa fa-arrow-up', handler: function () { moveEntry(state, -1); } },
                { text: '下移', itemId: 'plugin-entry-down', disabled: true, iconCls: 'fa fa-arrow-down', handler: function () { moveEntry(state, 1); } },
                { text: '模块配置', itemId: 'plugin-entry-configure', disabled: true, handler: function () { const row = selectedEntry(state); if (row) selectPage(state, row.getId(), 'config'); } },
                { text: '打开站点', itemId: 'plugin-entry-open', disabled: true, iconCls: 'fa fa-external-link', handler: function () {
                    const row = selectedEntry(state), sites = siteTools(); if (row && row.get('enabled') && sites && sites.open) sites.open(state.workspace, row.get('siteId'));
                } }],
            bbar: [{ xtype: 'tbtext', text: '编辑后点击窗口中的“保存”生效。上下移动后点击顶部“保存并应用”，统一更新树和视图菜单的顺序。', style: 'white-space:normal;' }],
            listeners: { selectionchange: function () { entryControls(state); }, itemdblclick: function (view, row) { editEntry(state, row); } },
        }] };
    }
    function load(state) {
        if (!state.active || !user(state)) return;
        state.generation++;
        state.requests.forEach(function (pending) { Ext.Ajax.abort(pending); }); state.requests.clear();
        if (state.unsubscribe) state.unsubscribe(); state.unsubscribe = null;
        closeEditor(state);
        if (alive(state.nodeEditor)) state.nodeEditor.destroy();
        state.data = null; state.safe = null; state.saving = false; state.safeSaving = false; state.safeBaseline = undefined;
        state.sites = []; state.sitesLoaded = false; state.siteCsrf = null; state.pendingOrder = null;
        setBusy(state, false); notice(state, '正在读取模块和站点配置…');
        request(state, 'config', 'GET', undefined, null, function (data, response) {
            if (!data || !Number.isSafeInteger(data.revision) || !Array.isArray(data.enabled_plugins) || !data.modules || !data.csrf_token || !data.navigation ||
                !navigation || !navigation.publish(data.navigation, data.revision)) { notice(state, errorText(response)); return; }
            state.data = data; state.navigation = copy(data.navigation);
            const komodo = data.modules.komodo;
            put(state, 'komodo-url', komodo.url); put(state, 'komodo-tls', komodo.tls_verify);
            ['api_key', 'api_secret'].forEach(function (key) {
                const input = state.page.down('#komodo-' + key); input.setValue('');
                input.setEmptyText(komodo[key + '_configured'] ? '已设置，留空保留' : '尚未设置');
            });
            put(state, 'komodo-clear', false);
            state.nodes = copy(data.modules.temperatures.nodes || {}); nodeRows(state);
            state.connectionBaseline = JSON.stringify(moduleSettings(state));
            const safeDisabled = !data.enabled_plugins.includes('safeline');
            state.page.down('#plugin-config-safeline').setDisabled(safeDisabled);
            state.page.down('#plugin-module-menu-safeline').getStore().getNodeById('plugin-config-safeline').set('rowCls', safeDisabled ? 'x-item-disabled' : '');
            if (data.enabled_plugins.includes('safeline')) loadSafe(state);
            const sites = siteTools(), generation = state.generation, expectedUser = state.user;
            if (sites && sites.subscribe) state.unsubscribe = sites.subscribe(state.workspace, function (value) {
                if (!current(state, generation, expectedUser) || !Array.isArray(value) || !value.every(validSite)) return;
                state.sites = value; reconcileEntries(state);
            });
            reconcileEntries(state); setBusy(state, false); loadSites(state);
            notice(state, '站点功能内置启用。表格中的每个模块或站点可以单独启用、停用和设置显示信息。');
        });
    }
    function moduleSettings(state) {
        const nodes = {};
        Object.keys(state.nodes).forEach(function (name) {
            const source = state.nodes[name];
            nodes[name] = { url: source.url, type: source.type || 'collector', allow_http: !!source.allow_http, token: source.token || '', clear_token: !!source.clear_token };
            if (source.ca_file) nodes[name].ca_file = source.ca_file;
        });
        return { komodo: { url: String(get(state, 'komodo-url') || '').trim(), api_key: get(state, 'komodo-api_key') || '',
                api_secret: get(state, 'komodo-api_secret') || '', tls_verify: !!get(state, 'komodo-tls'), clear_credentials: !!get(state, 'komodo-clear') },
            temperatures: { nodes: nodes } };
    }
    function save(state) {
        if (!state.data || !state.sitesLoaded || state.saving || state.safeSaving || alive(state.entryEditor) || !current(state, state.generation, state.user)) return;
        const modules = moduleSettings(state);
        if (safeDraftChanged(state) && JSON.stringify(modules) !== state.connectionBaseline) {
            notice(state, 'SafeLine 连接有未保存的修改，请先点击其“保存连接”，再保存并应用模块设置。'); return;
        }
        const body = { expected_revision: state.data.revision, enabled_plugins: state.data.enabled_plugins, navigation: readNavigation(state), modules: modules };
        setBusy(state, true); notice(state, '正在保存并应用…');
        request(state, 'config', 'PUT', body, state.data.csrf_token, function (data, response) { acceptConfig(state, data, response, null, true); });
    }
    function field(name, label, extra) { return Object.assign({ xtype: 'textfield', itemId: name, fieldLabel: label, labelWidth: 140, anchor: '100%' }, extra || {}); }
    function get(state, name) { return state.page.down('#' + name).getValue(); }
    function put(state, name, value) { state.page.down('#' + name).setValue(value); }
    function form(title, items, extra) { return Object.assign({ xtype: 'form', title: title, bodyPadding: 20, border: false, autoScroll: true, defaults: { anchor: '100%' }, items: items }, extra || {}); }
    function nodeRows(state) {
        state.page.down('#plugin-temperature-grid').getStore().loadData(Object.keys(state.nodes).map(function (node) { return Object.assign({ node: node }, state.nodes[node]); }));
    }
    function editNode(state, record) {
        if (!state.data || state.saving || !current(state, state.generation, state.user)) return;
        if (alive(state.nodeEditor)) state.nodeEditor.destroy();
        const oldNode = record && record.get('node'), source = oldNode && state.nodes[oldNode] || {};
        const generation = state.generation, expectedUser = state.user;
        let editor;
        function value(id) { return editor.down('#' + id).getValue(); }
        editor = Ext.create('Ext.window.Window', { title: oldNode ? '编辑温度来源' : '新增温度来源', modal: true, constrain: true, width: 580, height: 440, layout: 'fit',
            items: [form(null, [field('node', 'PVE 节点名称', { value: oldNode || '', disabled: !!oldNode, allowBlank: false }),
                field('type', '来源类型', { xtype: 'combobox', editable: false, queryMode: 'local', value: source.type || 'node_exporter',
                    store: [['node_exporter', 'node_exporter'], ['collector', '温度采集器']] }),
                field('url', '服务地址', { value: source.url || '', emptyText: 'https://metrics.example:9100', allowBlank: false }),
                field('allow_http', '允许 HTTP', { xtype: 'checkbox', checked: !!source.allow_http }),
                field('token', '访问令牌', { inputType: 'password', emptyText: source.token_configured || source.token ? '已设置，留空保留' : '采集器需要 32–256 位令牌' }),
                field('clear_token', '清除已存令牌', { xtype: 'checkbox', checked: !!source.clear_token }),
                field('ca_file', 'CA 证书文件', { value: source.ca_file || '', emptyText: '可选，容器内 PEM 文件路径' }),
                { xtype: 'displayfield', itemId: 'node-error', value: '' }])],
            buttons: [{ text: '取消', handler: function () { editor.destroy(); } }, { text: '确定', handler: function () {
                if (!current(state, generation, expectedUser) || state.saving) { editor.destroy(); return; }
                const node = String(value('node') || '').trim(), url = String(value('url') || '').trim(), token = String(value('token') || ''), clear = !!value('clear_token');
                if (clear && (token || value('type') === 'collector')) {
                    editor.down('#node-error').setValue('清除令牌时请留空访问令牌，并选择 node_exporter；温度采集器需要访问令牌。'); return;
                }
                if (token && (token.length < 32 || token.length > 256 || /[^\x21-\x7e]/.test(token))) {
                    editor.down('#node-error').setValue('访问令牌须为 32–256 位可打印字符，不能包含空格。'); return;
                }
                if (!/^[a-zA-Z0-9][a-zA-Z0-9-]{0,63}$/.test(node) || !/^https?:\/\/[^\s]+$/.test(url) ||
                    (!oldNode && Object.prototype.hasOwnProperty.call(state.nodes, node))) {
                    editor.down('#node-error').setValue('请输入有效且不重复的节点名称及 HTTP(S) 服务地址。'); return;
                }
                state.nodes[node] = Object.assign({}, source, { url: url, type: value('type'), allow_http: !!value('allow_http'), ca_file: String(value('ca_file') || ''), clear_token: clear });
                if (token || clear) state.nodes[node].token = token;
                nodeRows(state); editor.destroy();
            } }], listeners: { destroy: function () { if (state.nodeEditor === editor) state.nodeEditor = null; } } });
        state.nodeEditor = editor; editor.show();
    }
    function safeControls(state, disabled) {
        if (!alive(state.page)) return;
        ['safe-save', 'safe-test', 'safe-clear'].forEach(function (id) { state.page.down('#' + id).setDisabled(disabled); });
    }
    function safeSettings(state) {
        const value = {};
        ['url', 'token', 'tls_verify', 'tls_server_name', 'ca_pem'].forEach(function (key) { value[key] = get(state, 'safe-' + key); });
        return value;
    }
    function safeDraftChanged(state) {
        return state.safeBaseline !== undefined && JSON.stringify(safeSettings(state)) !== state.safeBaseline;
    }
    function loadSafe(state) {
        state.safe = null; safeControls(state, true);
        request(state, 'safeline/connection', 'GET', undefined, null, function (data, response) {
            if (!data || !data.connection || !data.csrf_token || !Number.isSafeInteger(data.revision)) { notice(state, errorText(response)); return; }
            state.safe = data;
            ['url', 'tls_verify', 'tls_server_name', 'ca_pem'].forEach(function (key) { put(state, 'safe-' + key, data.connection[key]); });
            put(state, 'safe-token', '');
            state.safeBaseline = JSON.stringify(safeSettings(state));
            const token = state.page.down('#safe-token');
            if (token.setEmptyText) token.setEmptyText(data.connection.token_configured ? '已设置，留空保留' : '请输入 API Token');
            safeControls(state, false);
        });
    }
    function saveSafe(state, action) {
        if (!state.safe || state.safeSaving || state.saving || !current(state, state.generation, state.user)) return;
        const body = { expected_revision: state.safe.revision };
        if (action !== 'clear') ['url', 'token', 'tls_verify', 'tls_server_name', 'ca_pem'].forEach(function (key) { body[key] = get(state, 'safe-' + key); });
        state.safeSaving = true; safeControls(state, true);
        entryControls(state);
        state.page.down('#plugin-manager-save').setDisabled(true); state.page.down('#plugin-manager-reload').setDisabled(true);
        notice(state, action === 'test' ? '正在测试 SafeLine 连接…' : '正在保存 SafeLine 连接…');
        request(state, 'safeline/connection' + (action === 'test' ? '/test' : ''), action === 'clear' ? 'DELETE' : action === 'test' ? 'POST' : 'PUT', body, state.safe.csrf_token, function (data, response) {
            state.safeSaving = false;
            entryControls(state);
            state.page.down('#plugin-manager-save').setDisabled(!state.data); state.page.down('#plugin-manager-reload').setDisabled(false);
            if (response && response.status === 422) {
                safeControls(state, false);
                notice(state, 'SafeLine 连接配置无效。输入已保留，修改后可再次保存或测试。'); return;
            }
            if (!data) { state.safe = null; notice(state, errorText(response) + ' 请重新读取配置。'); return; }
            if (action === 'test') { safeControls(state, false); notice(state, data.connected ? 'SafeLine 连接正常。' : 'SafeLine 连接测试完成，请核对地址和 API Token。'); }
            else { loadSafe(state); notice(state, action === 'clear' ? '已清除 SafeLine 连接。' : '已保存 SafeLine 连接。'); }
        });
    }
    function helpList(items, ordered) {
        const tag = ordered ? 'ol' : 'ul';
        return '<' + tag + ' style="padding-left:24px">' + items.map(function (item) {
            return '<li style="margin-bottom:8px">' + enc(item) + '</li>';
        }).join('') + '</' + tag + '>';
    }
    function activateModuleSection(panel, card) {
        if (!card || card.disabled) return false;
        panel.getLayout().setActiveItem(card);
        const menu = panel.down('#plugin-module-menu-' + panel.pluginModuleId);
        const record = menu.getStore().getNodeById(card.itemId);
        if (record && menu.getSelection() !== record) menu.setSelection(record);
        return true;
    }
    function modulePage(id, config) {
        const info = moduleInfo[id], heading = ' style="font-size:15px;margin:20px 0 8px"';
        const intro = '<div style="max-width:960px;line-height:1.7;overflow-wrap:anywhere">' +
            '<h2 style="font-size:18px;margin:0 0 12px">' + enc(labels[id]) + '</h2><p>' + enc(info.summary) + '</p>' +
            renderProgress(info) + '<p>功能覆盖与成熟度分别标识本版本的实现范围和验证阶段，由项目随版本维护。</p>' +
            '<h3' + heading + '>已支持的功能</h3>' + helpList(info.features) +
            '<h3' + heading + '>功能边界</h3>' + helpList(info.limits) +
            '<h3' + heading + '>验证范围</h3><p>' + enc(info.validation) + '</p></div>';
        const help = '<div style="max-width:960px;line-height:1.7;overflow-wrap:anywhere">' +
            '<h2 style="font-size:18px;margin:0 0 12px">' + enc(labels[id]) + ' 使用帮助</h2>' +
            '<h3' + heading + '>启用与配置</h3>' + helpList(info.setup, true) +
            '<h3' + heading + '>使用方式</h3>' + helpList(info.usage) +
            '<h3' + heading + '>常见问题</h3><dl>' + info.troubleshooting.map(function (row) {
                return '<dt style="font-weight:600;margin-top:12px">' + enc(row[0]) + '</dt><dd style="margin:4px 0 12px">' + enc(row[1]) + '</dd>';
            }).join('') + '</dl><p>停用模块后仍可阅读介绍与帮助。连接与业务功能在启用并保存后生效。</p></div>';
        config.title = '基本配置'; config.itemId = 'plugin-config-' + id; config.header = false;
        const cards = [{ xtype: 'panel', title: '介绍', itemId: 'plugin-intro-' + id, iconCls: 'fa fa-info-circle',
            border: false, header: false, bodyPadding: 20, autoScroll: true, html: intro },
        config, { xtype: 'panel', title: '帮助文档', itemId: 'plugin-help-' + id, iconCls: 'fa fa-book',
            border: false, header: false, bodyPadding: 20, autoScroll: true, html: help }];
        config.iconCls = 'fa fa-cog';
        // Match PVE.panel.Config's native menu while retaining mounted cards:
        // replacing cards on navigation would discard pending form edits.
        return { xtype: 'panel', border: false, layout: 'card', pluginModuleId: id,
            dockedItems: [{ xtype: 'toolbar', dock: 'left', width: 160, overflowHandler: 'scroller', cls: 'pve-toolbar-bg',
                style: { padding: 0, margin: 0 }, items: [{ xtype: 'treelist', itemId: 'plugin-module-menu-' + id, width: 160,
                    ui: 'pve-nav', expanderOnly: true, expanderFirst: false, animation: false, singleExpand: false,
                    store: { root: { expanded: true, children: cards.map(function (card) {
                        return { id: card.itemId, text: card.title, iconCls: card.iconCls, leaf: true };
                    }) } }, listeners: { selectionchange: function (menu, record) {
                        if (!record) return;
                        const panel = menu.up('panel'), card = panel.down('#' + record.getId());
                        if (!activateModuleSection(panel, card)) activateModuleSection(panel, panel.getLayout().getActiveItem());
                    } } }] }], items: cards,
            listeners: { afterrender: function (panel) { activateModuleSection(panel, panel.down('#plugin-intro-' + id)); } } };
    }
    function showManager(state) {
        if (!user(state)) return;
        const navigation = ensureNavigation(state);
        if (!navigation) return;
        navigation.setVisible(true);
        if (state.active && alive(state.page)) return;
        state.active = true; state.user = user(state); state.generation++; state.safeSaving = false;
        const pages = [entriesPage(state), form('Komodo 连接', [field('komodo-url', 'Core 地址', { emptyText: 'https://komodo.example' }),
            field('komodo-api_key', 'API Key', { inputType: 'password' }), field('komodo-api_secret', 'API Secret', { inputType: 'password' }),
            field('komodo-tls', '验证 TLS 证书', { xtype: 'checkbox', checked: true }),
            field('komodo-clear', '清除已存凭据', { xtype: 'checkbox' }),
            { xtype: 'displayfield', value: '更换 Core 地址时请同时填写新的 API Key 和 API Secret。' }]),
        { xtype: 'panel', title: '节点温度', layout: 'fit', border: false, items: [{ xtype: 'grid', itemId: 'plugin-temperature-grid',
            store: { fields: ['node', 'url', 'type', 'allow_http'], data: [] },
            columns: [{ text: '节点', dataIndex: 'node', width: 160, renderer: enc }, { text: '服务地址', dataIndex: 'url', flex: 1, renderer: enc }, { text: '类型', dataIndex: 'type', width: 150, renderer: enc }],
            viewConfig: { emptyText: '自动从 PVE 发现节点，并使用 node_exporter 的 9100 端口。可在此添加覆盖配置。', deferEmptyText: false },
            tbar: [{ text: '新增', handler: function () { editNode(state); } }, { text: '编辑', handler: function () {
                const record = state.page.down('#plugin-temperature-grid').getSelectionModel().getSelection()[0]; if (record) editNode(state, record);
            } }, { text: '删除覆盖配置', handler: function () {
                const record = state.page.down('#plugin-temperature-grid').getSelectionModel().getSelection()[0];
                if (record && state.data && !state.saving) { delete state.nodes[record.get('node')]; nodeRows(state); }
            } }], listeners: { itemdblclick: function (view, record) { editNode(state, record); } } }] },
        form('SafeLine 连接', [{ xtype: 'displayfield', value: '启用 SafeLine WAF 并保存后，可以配置连接；下方按钮单独保存或测试此连接。' },
            field('safe-url', '管理地址'), field('safe-token', 'API Token', { inputType: 'password' }), field('safe-tls_verify', '验证 TLS 证书', { xtype: 'checkbox', checked: true }),
            field('safe-tls_server_name', 'TLS 服务器名称'), field('safe-ca_pem', 'CA 证书（PEM）', { xtype: 'textareafield', height: 150 }),
            { xtype: 'container', layout: 'hbox', items: [{ xtype: 'button', text: '保存连接', itemId: 'safe-save', disabled: true, handler: function () { saveSafe(state, 'save'); } },
                { xtype: 'button', text: '测试连接', itemId: 'safe-test', margin: '0 8', disabled: true, handler: function () { saveSafe(state, 'test'); } },
                { xtype: 'button', text: '清除连接', itemId: 'safe-clear', disabled: true, handler: function () { saveSafe(state, 'clear'); } }] }])];
        Object.keys(pageNames).forEach(function (id, index) {
            if (id !== 'modules') pages[index] = modulePage(id, pages[index]);
            pages[index].itemId = 'plugin-page-' + id;
            pages[index].header = false;
        });
        state.workspace.setContent({ xtype: 'panel', itemId: 'pve-plugin-manager-page', pvePluginManagerPage: true, title: '插件管理', layout: 'fit', border: false,
            tbar: [{ text: '保存并应用', itemId: 'plugin-manager-save', disabled: true, iconCls: 'fa fa-save', handler: function () { save(state); } },
                { text: '重新读取', itemId: 'plugin-manager-reload', iconCls: 'fa fa-refresh', handler: function () { if (!state.saving && !state.safeSaving) load(state); } }],
            bbar: [{ xtype: 'tbtext', itemId: 'plugin-manager-status', style: 'white-space:normal', text: '' }],
            items: [{ xtype: 'panel', itemId: 'plugin-manager-pages', layout: 'card', disabled: true, items: pages }],
            listeners: { afterrender: function (page) {
                state.page = page;
                const selected = state.selectedNode || 'modules';
                navigation.getSelectionModel().select(navigation.getStore().getNodeById(selected));
                selectPage(state, selected);
                load(state);
            },
                hide: function () { if (state.page === this) leave(state); },
                beforedestroy: function () { if (state.page === this) leave(state); } } });
    }
    try {
        Ext.define('PVEPlugins.override.Selector', { override: 'PVE.form.ViewSelector', initComponent: function () {
            const result = this.callParent(arguments), store = this.getStore();
            if (!store.findRecord('key', viewId, 0, false, true, true)) {
                let index = store.getCount();
                store.each(function (record, i) { if (/^pve-(komodo-containers|safeline-waf|extra-site)/.test(record.get('key'))) index = Math.min(index, i); });
                store.insert(index, [{ key: viewId, value: '--插件--' }]);
            }
            if (navigation) navigation.applySelector(this);
            return result;
        } });
        Ext.define('PVEPlugins.override.Tree', { override: 'PVE.tree.ResourceTree', initComponent: function () {
            const result = this.callParent(arguments), native = this, previous = this.setViewFilter;
            if (typeof previous !== 'function') return result;
            const state = { native: native, workspace: null, tree: null, page: null, selectedPage: 'modules', active: false,
                generation: 0, requests: new Set(), nodes: {}, navigation: null, sites: [], sitesLoaded: false, siteCsrf: null, pendingOrder: null, selectedNode: 'modules' };
            states.add(state);
            this.setViewFilter = function (view) {
                const active = view && view.id === viewId;
                if (!active && state.active) leave(state);
                let returned;
                if (active) {
                    const selection = native.getSelectionModel(); selection.suspendEvents(false);
                    try { returned = previous.call(this, { id: viewId, groups: ['node'], getFilterFn: function () { return function () { return false; }; } }); }
                    finally { selection.resumeEvents(); }
                    native.setVisible(false);
                    ['#pve-komodo-container-tree', '#pve-extra-sites-panel', '#pve-safeline-tree'].forEach(function (selector) {
                        const panel = native.ownerCt && native.ownerCt.down(selector); if (alive(panel)) panel.setVisible(false);
                    });
                    showManager(state);
                } else returned = previous.apply(this, arguments);
                return returned;
            };
            this.on('afterrender', function () { if (native.viewFilter && native.viewFilter.id === viewId) native.setViewFilter(native.viewFilter); });
            this.on('beforedestroy', function () {
                leave(state); states.delete(state);
                if (alive(state.tree)) state.tree.destroy();
            });
            return result;
        } });
        Ext.define('PVEPlugins.override.Workspace', { override: 'PVE.StdWorkspace', onLogin: function (data) {
            const workspace = this; workspace._pvePluginManagerLoggedOut = !data;
            if (!data) states.forEach(function (state) { if (workspaceOf(state) === workspace) leave(state); });
            const result = this.callParent(arguments);
            if (data) states.forEach(function (state) { if (workspaceOf(state) === workspace && state.native.viewFilter && state.native.viewFilter.id === viewId) showManager(state); });
            return result;
        } });
        if (navigation) navigation.subscribe(function () { states.forEach(updateNavigation); });
        window.__pvePluginManagerLoaded = true;
    } catch (error) { if (window.console) window.console.warn('Plugin manager could not be registered.', error); }
}());
