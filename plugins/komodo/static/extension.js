(function () {
    // ExtJS callParent resolves method metadata through Function.caller, so its
    // overriding methods must remain ordinary, non-strict script functions.

    const overrideName = 'PVEKomodo.override.DatacenterConfig';
    const registrationKey = '__pveKomodoDatacenterExtensionLoaded';
    const itemId = 'pve-komodo-docker';
    const Ext = window.Ext;

    // This script must load after pvemanagerlib.js and before StdWorkspace starts.
    // An unknown PVE interface leaves the original page untouched.
    if (window[registrationKey] || !Ext || !Ext.ClassManager ||
        typeof Ext.ClassManager.get !== 'function' ||
        typeof Ext.define !== 'function') {
        return;
    }

    const DatacenterConfig = Ext.ClassManager.get('PVE.dc.Config');
    if (!DatacenterConfig || !DatacenterConfig.prototype ||
        typeof DatacenterConfig.prototype.insertNodes !== 'function') {
        return;
    }

    // Route metadata follows the Komodo page hierarchy for native PVE views.
    // These identifiers are not browser request destinations.
    const pages = [
        { id: itemId, title: 'Docker', route: '/', icon: 'fa fa-cubes' },
        { id: itemId + '-stats', title: '统计', route: '/stats', icon: 'fa fa-bar-chart' },
        { id: itemId + '-containers', title: '容器', route: '/containers', icon: 'fa fa-cubes' },
        { id: itemId + '-alerts', title: '告警', route: '/alerts', icon: 'fa fa-bell-o' },
        { id: itemId + '-updates', title: '操作', route: '/updates', icon: 'fa fa-history' },
        { id: itemId + '-schedules', title: '计划任务', route: '/schedules', icon: 'fa fa-calendar' },
    ];
    const pageIds = new Set(pages.map(function (page) { return page.id; }));

    const sections = ['servers', 'stacks', 'procedures'];
    const sectionTitles = { servers: '服务器', stacks: '应用栈', procedures: '流程', containers: '容器' };
    const actions = {
        servers: [
            ['prune_containers', '清理容器', '清理所有已停止的容器，容器可写层中未持久化的数据会被删除。'],
            ['prune_networks', '清理网络', '删除未被容器使用的 Docker 网络。'],
            ['prune_volumes', '清理存储卷', '删除未使用的 Docker 存储卷，卷内数据可能被永久删除。'],
            ['prune_images', '清理镜像', '清理 Docker 判定可清理的未使用镜像，之后可能需要重新拉取。'],
            ['prune_system', '清理系统', '清理未使用容器、网络、镜像及存储卷，卷数据可能被删除。'],
            ['restart_all_containers', '重启所有容器', '重启服务器上的所有 Docker 容器，包括原先已停止的容器；相关服务将短暂中断。'],
            ['stop_all_containers', '停止所有容器', '停止服务器上的所有 Docker 容器，会中断相关服务。'],
            ['delete', '删除', '仅删除 Komodo 中的服务器定义，不关闭服务器，也不删除 Docker 容器或数据。'],
        ],
        stacks: [
            ['check_update', '检查更新', '检查应用栈更新，不自动拉取或部署。'],
            ['pull', '拉取', '为应用栈拉取镜像，不立即部署。'],
            ['deploy', '部署', '部署应用栈，容器可能被创建或重建，相关服务可能中断。'],
            ['restart', '重启', '重启应用栈容器，相关服务将短暂中断。'],
            ['stop', '停止', '停止应用栈容器，会中断相关服务。'],
            ['destroy', '销毁', '停止并移除应用栈容器与关联网络；默认不删除存储卷。'],
            ['delete', '删除', '停止并移除所选应用栈的实际部署，再删除 Komodo 中的配置；运行中的应用会中断。'],
        ],
        procedures: [
            ['run', '运行', '执行流程中定义的步骤，可能触发部署、停止或其他变更。'],
            ['delete', '删除', '移除流程的计划和 Komodo 中的流程定义，不撤销此前已发出的操作。'],
        ],
        containers: [
            ['start', '启动', '启动所选容器及其中的应用服务。'],
            ['restart', '重启', '重启所选容器，相关服务将短暂中断。'],
            ['pause', '暂停', '暂停容器中的进程，相关服务将无法响应，直到恢复。'],
            ['unpause', '恢复', '恢复已暂停容器中的进程。'],
            ['stop', '停止', '停止所选容器，会中断相关服务。'],
            ['destroy', '销毁', '停止并删除所选容器，其可写层中未持久化的数据会被删除；不删除存储卷。'],
        ],
    };
    let activeDashboard = null;
    let actionState = null;
    let serverCreateState = null;
    let serverCreateWrite = null;
    const stateLabels = {
        Ok: '正常', NotOk: '不可用', Disabled: '已禁用', Unknown: '未知',
        Running: '运行中', Failed: '失败', running: '运行中', stopped: '已停止',
        paused: '已暂停', deploying: '部署中', restarting: '重启中',
        unhealthy: '不健康', down: '已停止', unknown: '未知',
        exited: '已退出', created: '已创建', dead: '异常', removing: '移除中',
    };

    function panelSections(panel) {
        return panel.pveKomodoPage === 'containers' ? ['containers'] : sections;
    }

    function ownerPanel(component) {
        return component.up('[pveKomodoPage]') || component.up('[pveKomodoContainerTab]');
    }

    function containerActionAllowed(action, selected) {
        const allowed = {
            start: ['created', 'exited', 'stopped'], restart: ['running'], pause: ['running'],
            unpause: ['paused'], stop: ['running', 'paused', 'restarting'],
            destroy: ['running', 'paused', 'created', 'exited', 'stopped', 'dead', 'restarting'],
        };
        return selected.every(function (record) {
            const state = record.get('state');
            return typeof state === 'string' && allowed[action].indexOf(state.toLowerCase()) !== -1;
        });
    }

    function alive(component) {
        return component && !component.destroyed && !component.destroying;
    }

    function numeric(value) {
        return typeof value === 'number' && Number.isFinite(value);
    }

    function text(value) {
        return Ext.String.htmlEncode(typeof value === 'string' || numeric(value) ? String(value) : '-');
    }

    function dateText(value) {
        if (value === null || value === undefined || value === '') {
            return '-';
        }
        const date = new Date(value);
        return Number.isNaN(date.getTime()) ? '-' : Ext.Date.format(date, 'Y-m-d H:i:s');
    }

    function progressColumn(title, field) {
        // WidgetColumn also reapplies dataIndex when any field in the row changes,
        // without calling onWidgetAttach. Always bind its normalized 0..1 value.
        return {
            text: title, dataIndex: field + '_value',
            sorter: { property: field }, width: field === 'cpu_percent' ? 125 : 260, sortable: true,
            xtype: 'widgetcolumn',
            widget: {
                xtype: 'progressbarwidget',
                cls: 'pve-komodo-usage-progress',
                text: '-',
            },
            onWidgetAttach: function (column, widget, record) {
                // PVE does not supply row ViewModels to these grids. Explicit
                // setters also reset recycled widgets when a metric is missing.
                const value = record.get(field + '_value');
                const label = record.get(field + '_text');
                widget.setValue(numeric(value) ? value : 0);
                widget.setText(typeof label === 'string' ? text(label) : '-');
            },
        };
    }

    function tagsText(value) {
        return Array.isArray(value) && value.length ? value.map(text).join('、') : '-';
    }

    function stateText(value) {
        return text(typeof value === 'string' && Object.prototype.hasOwnProperty.call(stateLabels, value) ?
            stateLabels[value] : value);
    }

    function actionBusy(panel) {
        return !!(panel._pveKomodoConfirmation || panel._pveKomodoGuestDialog ||
            serverCreateWrite && serverCreateWrite.pending ||
            guestInventoryWrite && guestInventoryWrite.pending ||
            guestWriteState && guestWriteState.pending || actionState && actionState.pending || guestOptionsWrite && guestOptionsWrite.pending ||
            guestSummaryWrite && guestSummaryWrite.pending || stackWriteState && stackWriteState.pending);
    }

    function updateControls(panel) {
        if (!alive(panel)) return;
        const busy = actionBusy(panel);
        ['refresh', 'search', 'tag', 'exclude-templates', 'server'].forEach(function (id) {
            const control = panel.down('#pve-komodo-' + id);
            if (alive(control)) control.setDisabled(busy || !!panel._pveKomodoLoading);
        });
        panelSections(panel).forEach(function (section) {
            const grid = panel.down('#pve-komodo-' + section + '-grid');
            if (!alive(grid)) return;
            const selected = grid.getSelectionModel().getSelection();
            actions[section].forEach(function (action) {
                const button = grid.down('#pve-komodo-action-' + action[0]);
                if (alive(button)) button.setDisabled(!selected.length ||
                    (section === 'containers' && !containerActionAllowed(action[0], selected)) ||
                    busy || !!panel._pveKomodoLoading ||
                    !panel._pveKomodoCsrf || !panel._pveKomodoActive);
            });
        });
        const create = panel.down('#pve-komodo-servers-new');
        if (alive(create)) create.setDisabled(busy || !panel._pveKomodoActive);
        guestControls(panel);
    }

    function uuid() {
        const crypto = window.crypto;
        if (!crypto) return null;
        try {
            if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
        } catch (error) { /* Try the secure byte API next. */ }
        if (typeof crypto.getRandomValues !== 'function') return null;
        try {
            const bytes = new Uint8Array(16);
            crypto.getRandomValues(bytes);
            bytes[6] = (bytes[6] & 15) | 64;
            bytes[8] = (bytes[8] & 63) | 128;
            const hex = Array.from(bytes, function (byte) { return byte.toString(16).padStart(2, '0'); }).join('');
            return hex.slice(0, 8) + '-' + hex.slice(8, 12) + '-' + hex.slice(12, 16) + '-' +
                hex.slice(16, 20) + '-' + hex.slice(20);
        } catch (error) { return null; }
    }

    function submitAction(panel, section, action, ids, csrf) {
        const requestId = uuid();
        if (!requestId) {
            statusText(panel, '浏览器无法生成安全请求标识，未提交操作');
            updateControls(panel);
            return;
        }
        const state = { pending: true, finished: false, message: '正在提交“' + action[1] + '”，请勿重复操作' };
        actionState = state;
        updateControls(panel);
        statusText(panel, state.message);
        function complete(options, success, response) {
            if (state.finished) return;
            state.finished = true;
            state.pending = false;
            const unknown = '请求结果尚未确认。请先到 Komodo 的操作记录核对，不要重复提交。';
            state.message = unknown;
            try {
                const result = response && JSON.parse(response.responseText);
                if (!success || !result || !Array.isArray(result.results) || result.results.length !== ids.length) {
                    if (response && response.status === 401) state.message = 'PVE 未登录或会话已过期。' + unknown;
                    if (response && response.status === 403) state.message = '无访问权限或会话校验失败。' + unknown;
                    if (response && response.status === 503) state.message = 'PVE 会话验证或集成服务暂不可用。' + unknown;
                    if (response && response.status === 403 && result && result.detail === 'invalid_csrf_token') {
                        state.requiresRefresh = true;
                        state.message = '会话校验已更新，本次未提交，请先刷新 Docker 页面再操作';
                    }
                    if (response && response.status === 409 && result && result.detail === 'action_already_requested') {
                        state.message = '该请求已提交或结果尚未确认，请先到 Komodo 的操作记录核对，不要重复提交。';
                    } else if (response && response.status === 409 && result && result.detail === 'idempotency_conflict') {
                        state.message = '请求标识冲突，请先到 Komodo 的操作记录核对，不要重复提交。';
                    }
                    throw new Error('action-response-unavailable');
                }
                const counts = { submitted: 0, completed: 0, failed: 0, outcome_unknown: 0, not_submitted: 0 };
                const seen = new Set();
                result.results.forEach(function (entry) {
                    if (!entry || ids.indexOf(entry.id) < 0 || seen.has(entry.id) ||
                        !Object.prototype.hasOwnProperty.call(counts, entry.status) ||
                        entry.ok !== (entry.status === 'submitted' || entry.status === 'completed')) {
                        throw new Error('invalid-action-result');
                    }
                    seen.add(entry.id);
                    counts[entry.status]++;
                });
                state.message = action[1] + '：已提交 ' + counts.submitted + '，已完成 ' + counts.completed +
                    '，失败 ' + counts.failed + '，未提交 ' + counts.not_submitted + '，结果未知 ' + counts.outcome_unknown + '。';
                state.message += counts.outcome_unknown ? unknown : '请手动刷新观察结果。';
            } catch (error) { /* Never display response bodies or exception text. */ }
            // POSTs outlive their old card. Update only the current live dashboard.
            const current = activeDashboard;
            if (alive(current) && current._pveKomodoActive) {
                if (state.requiresRefresh) current._pveKomodoCsrf = null;
                panelSections(current).forEach(function (name) {
                    const grid = current.down('#pve-komodo-' + name + '-grid');
                    if (alive(grid)) grid.getSelectionModel().deselectAll();
                });
                updateControls(current);
                statusText(current, state.message);
            }
            if (alive(activeGuestOptions)) optionControls(activeGuestOptions);
            if (alive(activeGuestSummary)) summaryControls(activeGuestSummary);
            if (alive(activeStackPage)) stackControls(activeStackPage);
        }
        try {
            // Keep this POST separate from the cancellable dashboard GET.
            Ext.Ajax.request({
                url: '/pve-komodo/api/action', method: 'POST', timeout: 120000,
                headers: { 'X-PVE-Komodo-CSRF': csrf },
                jsonData: { section: section, action: action[0], ids: ids, request_id: requestId },
                callback: complete,
            });
        } catch (error) { complete(null, false, null); }
    }

    function confirmAction(button, section, action) {
        const panel = ownerPanel(button);
        if (!alive(panel) || !panel._pveKomodoActive || panel._pveKomodoLoading || actionBusy(panel) ||
            !panel._pveKomodoCsrf) return;
        const grid = panel.down('#pve-komodo-' + section + '-grid');
        const selected = grid.getSelectionModel().getSelection();
        if (!selected.length) return;
        if (section === 'containers' && !containerActionAllowed(action[0], selected)) return;
        if (selected.length > 50) {
            statusText(panel, '每次最多选择 50 个资源，请减少选择后再操作');
            return;
        }
        const ids = selected.map(function (record) { return record.get('id'); });
        if (ids.some(function (id) { return typeof id !== 'string' || !id; }) || new Set(ids).size !== ids.length) return;
        if (section === 'containers' && ids.some(function (id) { return !/^[0-9a-f]{24}:[0-9a-f]{64}$/i.test(id); })) {
            statusText(panel, '容器标识无效，请刷新后重试');
            return;
        }
        const names = selected.map(function (record) {
            const name = record.get('name') || record.get('id');
            return section === 'containers' ? name + '（' + (record.get('server_name') || record.get('server_id')) + '）' : name;
        });
        const generation = panel._pveKomodoGeneration;
        const selectionGeneration = panel._pveKomodoSelectionGeneration;
        const csrf = panel._pveKomodoCsrf;
        const confirmation = { answered: false, dialog: null };
        panel._pveKomodoConfirmation = confirmation;
        updateControls(panel);
        let dialog;
        try {
            dialog = Ext.create('Ext.window.MessageBox', { closeAction: 'destroy' });
            confirmation.dialog = dialog;
            dialog.show({
                title: '确认' + action[1],
                message: text('对以下 ' + ids.length + ' 个' + sectionTitles[section] + '执行“' + action[1] + '”：') +
                    '<ul>' + names.map(function (name) { return '<li>' + text(name) + '</li>'; }).join('') + '</ul><p>' + text(action[2]) + '</p>',
                buttons: Ext.Msg.YESNO, icon: Ext.Msg.WARNING, defaultFocus: 'no',
                fn: function (answer) {
                    if (confirmation.answered) return;
                    confirmation.answered = true;
                    if (alive(dialog)) dialog.destroy();
                    if (!alive(panel) || !panel._pveKomodoActive || panel._pveKomodoConfirmation !== confirmation) return;
                    panel._pveKomodoConfirmation = null;
                    if (answer !== 'yes' || panel._pveKomodoGeneration !== generation ||
                        panel._pveKomodoSelectionGeneration !== selectionGeneration) {
                        updateControls(panel);
                        return;
                    }
                    submitAction(panel, section, action, ids, csrf);
                },
            });
        } catch (error) {
            confirmation.answered = true;
            panel._pveKomodoConfirmation = null;
            if (alive(dialog)) dialog.destroy();
            if (alive(panel) && panel._pveKomodoActive) {
                updateControls(panel);
                statusText(panel, '无法打开确认窗口，未提交操作');
            }
        }
    }

    // Guest mappings are local integration metadata, separate from Komodo actions.
    let guestWriteState = null;
    let requestedServer = null;
    let requestedContainer = null;
    let requestedNavigationNotice = null;
    let requestedGuestServer = null;
    const guestItemId = 'pve-komodo-guest-docker';

    function validGuest(value) {
        return value && (value.guest_type === 'qemu' || value.guest_type === 'lxc') &&
            Number.isSafeInteger(value.vmid) && value.vmid > 0;
    }

    function guestLabel(link) {
        if (!link) return '未关联';
        if (!validGuest(link)) return '关联未知';
        const label = (link.guest_type === 'qemu' ? 'VM ' : 'CT ') + link.vmid +
            (link.guest_name ? '（' + link.guest_name + '）' : '');
        if (link.status === 'linked') return label + (link.can_navigate === false ? ' · 不可访问' : '');
        if (link.status === 'needs_confirmation') return label + ' · 待确认';
        if (link.status === 'missing') return label + ' · 目标不存在或不可访问';
        return label + ' · 暂时无法校验';
    }

    function guestOwner(panel) {
        return panel && typeof panel.up === 'function' ? panel.up('[workspace]') : null;
    }

    function serverPageBase(node) {
        return node.server_id ? '/pve-komodo/api/servers/' + node.server_id : '/pve-komodo/api/guests/' + node.type + '/' + Number(node.vmid);
    }
    function serverReplyMatches(panel, data) {
        return !panel.pveKomodoDirectServer || data && data.server_id === panel.pveKomodoDirectServer && data.binding_revision === 0;
    }

    function openServerCreate(workspace) {
        const user = window.Proxmox && window.Proxmox.UserName;
        if (!user || !window.Proxmox.Utils.authOK() || workspace && workspace._pveKomodoLoggedOut || dockerNavigationBusy()) return;
        const state = { user: user, workspace: workspace, closed: false, loading: true, pending: false, finished: false, data: null, read: null };
        serverCreateState = state;
        let dialog;
        function current() { return !state.closed && alive(dialog) && serverCreateState === state &&
            window.Proxmox.UserName === user && window.Proxmox.Utils.authOK() && !(workspace && workspace._pveKomodoLoggedOut); }
        function message(value) { if (current()) dialog.down('#pve-komodo-server-create-error').update(text(value)); }
        function controls() {
            if (!current()) return;
            const name = String(dialog.down('#pve-komodo-server-create-name').getValue() || '').trim();
            const validName = name.length > 0 && name.length <= 512 && !/[\x00-\x1f\x7f]/.test(name) && !/^[0-9a-f]{24}$/i.test(name);
            dialog.down('#pve-komodo-server-create-submit').setDisabled(state.loading || state.pending || state.finished || !state.data || !state.data.can_create || !validName || optionBusy());
            dialog.down('#pve-komodo-server-create-name').setDisabled(state.pending || state.finished);
            dialog.down('#pve-komodo-server-create-guest').setDisabled(state.loading || state.pending || state.finished || !state.data || state.data.can_link !== true);
        }
        dialog = Ext.create('Ext.window.Window', { title: '新建服务器', width: 560, modal: true, closeAction: 'destroy', bodyPadding: 16, layout: 'anchor',
            items: [{ xtype: 'component', html: '<p>输入唯一名称，可选择关联一个 CT 或 VM，也可以不关联。</p>' },
                { xtype: 'textfield', itemId: 'pve-komodo-server-create-name', fieldLabel: '名称', anchor: '100%', maxLength: 512, enforceMaxLength: true,
                    emptyText: '输入服务器名称', listeners: { change: controls } },
                { xtype: 'combobox', itemId: 'pve-komodo-server-create-guest', fieldLabel: '关联 CT 或 VM', anchor: '100%', labelWidth: 105,
                    editable: false, forceSelection: true, queryMode: 'local', displayField: 'label', valueField: 'key', value: '', disabled: true,
                    store: { autoDestroy: true, fields: ['key', 'label'], data: [{ key: '', label: '不关联' }] }, listConfig: { itemTpl: '{label:htmlEncode}' } },
                { xtype: 'component', itemId: 'pve-komodo-server-create-error', html: '正在读取创建权限和可关联对象…' }],
            buttons: [{ text: '创建', itemId: 'pve-komodo-server-create-submit', disabled: true, handler: function () {
                if (!current() || state.pending || state.finished || !state.data || !state.data.can_create || optionBusy()) return;
                const name = String(dialog.down('#pve-komodo-server-create-name').getValue() || '').trim();
                if (!name || name.length > 512 || /[\x00-\x1f\x7f]/.test(name) || /^[0-9a-f]{24}$/i.test(name)) return;
                const key = dialog.down('#pve-komodo-server-create-guest').getValue() || '';
                const guest = state.data.guests.find(function (row) { return row.guest_type + '/' + row.vmid === key; });
                if (key && (!guest || state.data.can_link !== true)) { message('请选择可关联的 CT 或 VM，或选择不关联'); return; }
                const requestId = uuid(); if (!requestId) { message('浏览器无法生成安全请求标识，本次未提交'); return; }
                const payload = { name: name, request_id: requestId };
                if (guest) { payload.guest_type = guest.guest_type; payload.vmid = guest.vmid; }
                state.pending = true; serverCreateWrite = state; controls(); inventoryAllControls(); message('正在创建，请勿重复提交…');
                function complete(options, success, response) {
                    if (!state.pending) return;
                    state.pending = false; state.finished = true;
                    let notice = '创建结果尚未确认，请核对服务器列表后再操作，不要重复创建';
                    try {
                        const result = JSON.parse(response.responseText);
                        if (success && result && result.created === true && /^[0-9a-f]{24}$/i.test(result.server_id) && result.server_name === name &&
                            ['completed', 'created_unlinked'].includes(result.status)) {
                            const linked = guest && result.link && result.link.guest_type === guest.guest_type && result.link.vmid === guest.vmid;
                            notice = guest ? linked ? '服务器已创建并关联所选 CT 或 VM' : '服务器已创建，关联未完成；请到服务器列表关联，勿重复创建' : '服务器已创建，未关联 CT 或 VM';
                            state.created = { id: result.server_id, name: result.server_name, linked: linked };
                            if (current()) {
                                containerTreeStates.forEach(function (tree) { if (tree.workspace === workspace && tree.active) loadContainerTree(tree); });
                                if (alive(activeDashboard) && activeDashboard._pveKomodoActive) loadDashboard(activeDashboard);
                            }
                        } else if (!success || result && result.created === false) notice = optionsError(response, '创建失败，请刷新创建窗口后重试');
                    } catch (error) { /* Do not retry an uncertain creation. */ }
                    if (current()) { message(notice); controls(); }
                    inventoryAllControls();
                }
                try { Ext.Ajax.request({ url: '/pve-komodo/api/servers/create', method: 'POST', timeout: 120000,
                    headers: { 'X-PVE-Komodo-CSRF': state.data.csrf_token }, jsonData: payload, callback: complete }); }
                catch (error) { complete(null, false, null); }
            } }, { text: '关闭', handler: function () { if (!state.pending) dialog.close(); } }],
            listeners: { beforeclose: function () { return !state.pending; }, beforedestroy: function () {
                state.closed = true;
                if (state.read) Ext.Ajax.abort(state.read);
                if (serverCreateState === state) serverCreateState = null;
                if (state.created && !state.created.linked && user === window.Proxmox.UserName && window.Proxmox.Utils.authOK() && !(workspace && workspace._pveKomodoLoggedOut)) {
                    openServerManagement(workspace, state.created.id, state.created.name);
                }
            } },
        });
        state.dialog = dialog; dialog.show();
        let completed = false;
        function loaded(options, success, response) {
            completed = true; state.read = null;
            if (!current()) return;
            state.loading = false;
            try {
                const data = JSON.parse(response.responseText);
                if (success && data && typeof data.can_create === 'boolean' && Array.isArray(data.guests) &&
                    typeof data.csrf_token === 'string' && data.csrf_token && !/[\r\n]/.test(data.csrf_token)) {
                    state.data = Object.assign({}, data, { guests: data.guests.filter(function (guest) {
                        return guest && ['qemu', 'lxc'].includes(guest.guest_type) && Number.isSafeInteger(guest.vmid) && guest.vmid >= 100 && typeof guest.name === 'string';
                    }) });
                    dialog.down('#pve-komodo-server-create-guest').getStore().loadData([{ key: '', label: '不关联' }].concat(state.data.guests.map(function (guest) {
                        return { key: guest.guest_type + '/' + guest.vmid, label: (guest.guest_type === 'qemu' ? 'VM ' : 'CT ') + guest.vmid + ' · ' + guest.name + ' · ' + guest.node };
                    })));
                    message(data.can_create ? data.can_link === true && !(Array.isArray(data.errors) && data.errors.length) ? '' : '关联对象暂不可用，仍可选择不关联创建服务器' : '没有新建服务器的权限');
                } else message(optionsError(response, '创建权限读取失败，请关闭后重试'));
            } catch (error) { message('创建权限读取失败，请关闭后重试'); }
            controls();
        }
        try { const request = Ext.Ajax.request({ url: '/pve-komodo/api/server-create-options', method: 'GET', disableCaching: false, timeout: 60000, callback: loaded });
            if (!completed) state.read = request;
        } catch (error) { loaded(null, false, null); }
    }

    function openGuest(panel, link) {
        if (!validGuest(link) || link.status !== 'linked' || link.can_navigate !== true) return;
        const config = guestOwner(panel);
        if (config && config.workspace && typeof config.workspace.selectById === 'function') {
            config.workspace.selectById(link.guest_type + '/' + link.vmid);
        }
    }

    function openDockerHome(panel, serverId) {
        const config = guestOwner(panel);
        const workspace = config && config.workspace;
        openDockerDestination(workspace, serverId, null, null);
    }

    function dockerNavigationBusy() {
        return !!serverCreateState || optionBusy() || Array.from(guestInventoryPages).some(function (page) { return page._pveKomodoInventoryConfirmation || alive(page._pveKomodoInventoryEditor); }) || alive(activeDashboard) && actionBusy(activeDashboard) ||
            alive(activeGuestOptions) && !!activeGuestOptions._pveKomodoOptionsConfirmation ||
            alive(activeGuestSummary) && !!(activeGuestSummary._pveKomodoSummaryConfirmation || activeGuestSummary._pveKomodoGuestDialog) ||
            alive(activeStackPage) && !!activeStackPage._pveKomodoStackConfirmation;
    }

    function openDockerDestination(workspace, serverId, containerId, notice) {
        if (!workspace || typeof workspace.selectById !== 'function' || typeof workspace.query !== 'function') return;
        if (dockerNavigationBusy()) return;
        requestedGuestServer = null;
        requestedServer = containerId ? null : typeof serverId === 'string' ? serverId : null;
        requestedContainer = containerId ? { id: containerId, server_id: serverId, user: window.Proxmox && window.Proxmox.UserName } : null;
        requestedNavigationNotice = notice || null;
        // PVE.StdWorkspace creates the root Config synchronously on selection.
        // PVE.panel.Config.selectById selects the native menu and activates its card.
        workspace.selectById('root');
        const root = workspace.query('pvePanelConfig').filter(function (candidate) {
            return alive(candidate) && candidate.pveSelNode && candidate.pveSelNode.data.id === 'root';
        }).pop();
        if (root && typeof root.selectById === 'function') root.selectById(containerId ? itemId + '-containers' : itemId);
        if (alive(activeDashboard) && activeDashboard.pveKomodoPage === (containerId ? 'containers' : 'dashboard')) {
            if (containerId) {
                if (!activeDashboard._pveKomodoLoading) loadDashboard(activeDashboard);
            } else highlightRequestedServer(activeDashboard);
        }
    }

    function highlightRequestedServer(panel) {
        if (!requestedServer || !alive(panel) || panel._pveKomodoLoading || !panel._pveKomodoRows || dockerNavigationBusy()) return;
        const serverId = requestedServer;
        const exists = (panel._pveKomodoRows.servers || []).some(function (row) { return row.id === serverId; });
        if (!exists) {
            requestedServer = null;
            statusText(panel, '关联的服务器当前不可访问');
            return;
        }
        // Explicit navigation clears local filters so the selected server is visible.
        panel._pveKomodoUpdatingTags = true;
        try {
            panel.down('#pve-komodo-search').setValue('');
            panel.down('#pve-komodo-tag').setValue('');
        } finally { panel._pveKomodoUpdatingTags = false; }
        applyFilters(panel);
        const grid = panel.down('#pve-komodo-servers-grid');
        const store = grid.getStore();
        const record = typeof store.findRecord === 'function' ? store.findRecord('id', serverId, 0, false, true, true) : null;
        if (record) {
            grid.getSelectionModel().select(record);
            if (typeof grid.ensureVisible === 'function') grid.ensureVisible(record, { focus: true });
        }
        requestedServer = null;
        if (requestedNavigationNotice) { statusText(panel, requestedNavigationNotice); requestedNavigationNotice = null; }
    }

    function highlightRequestedContainer(panel) {
        if (!requestedContainer || !alive(panel) || panel._pveKomodoLoading || !panel._pveKomodoRows || dockerNavigationBusy()) return;
        const intent = requestedContainer;
        const currentUser = window.Proxmox && window.Proxmox.UserName;
        if (intent.user && intent.user !== currentUser) { requestedContainer = null; return; }
        const row = (panel._pveKomodoRows.containers || []).find(function (candidate) {
            return candidate.id === intent.id && candidate.server_id === intent.server_id;
        });
        requestedContainer = null;
        if (!row) { statusText(panel, '容器当前不可见，可能已被移除、服务器离线或权限已变化'); return; }
        panel._pveKomodoUpdatingTags = true;
        try {
            panel.down('#pve-komodo-search').setValue('');
            panel.down('#pve-komodo-tag').setValue('');
            panel.down('#pve-komodo-server').setValue(intent.server_id);
        } finally { panel._pveKomodoUpdatingTags = false; }
        applyFilters(panel);
        const grid = panel.down('#pve-komodo-containers-grid');
        const record = grid.getStore().findRecord('id', intent.id, 0, false, true, true);
        if (record) {
            grid.getSelectionModel().select(record);
            if (typeof grid.ensureVisible === 'function') grid.ensureVisible(record, { focus: true });
            statusText(panel, '已定位所选容器');
        } else statusText(panel, '容器当前不可见，可能已被移除、服务器离线或权限已变化');
    }

    function guestLinkActive(panel) {
        const owner = guestOwner(panel), proxmox = window.Proxmox;
        return alive(panel) && (panel.pveKomodoGuestSummary ? panel._pveKomodoSummaryActive : panel._pveKomodoActive) &&
            !(owner && owner.workspace && owner.workspace._pveKomodoLoggedOut) &&
            (!proxmox || !proxmox.Utils || typeof proxmox.Utils.authOK !== 'function' || proxmox.Utils.authOK());
    }
    function guestLinkCurrent(panel) {
        return guestLinkActive(panel) && panel._pveKomodoGuestUser === (window.Proxmox && window.Proxmox.UserName);
    }
    function guestLinkTarget(panel) {
        if (panel.pveKomodoGuestSummary) {
            const data = panel._pveKomodoSummaryData;
            const id = panel.pveKomodoDirectServer || panel._pveKomodoExpectedServer || data && data.server_id;
            return { server: id && panel._pveKomodoGuestServers && panel._pveKomodoGuestServers[id],
                link: id && panel._pveKomodoGuestLinks && panel._pveKomodoGuestLinks[id] };
        }
        const grid = panel.down('#pve-komodo-servers-grid');
        if (!alive(grid)) return {};
        const selected = grid.getSelectionModel().getSelection();
        const row = selected.length === 1 ? selected[0] : null;
        return { server: row && panel._pveKomodoGuestServers && panel._pveKomodoGuestServers[row.get('id')], link: row && row.get('pve_guest') };
    }
    function guestLinkStatus(panel, message) {
        if (panel.pveKomodoGuestSummary) summaryStatus(panel, message); else statusText(panel, message);
    }
    function guestLinkControls(panel) {
        if (panel.pveKomodoGuestSummary) summaryControls(panel); else updateControls(panel);
    }
    function guestControls(panel) {
        if (!alive(panel) || panel.pveKomodoPage !== 'dashboard' && !panel.pveKomodoGuestSummary) return;
        const target = guestLinkTarget(panel), server = target.server, link = target.link;
        const container = panel.pveKomodoGuestSummary ? panel : panel.down('#pve-komodo-servers-grid');
        if (!alive(container)) return;
        const blocked = actionBusy(panel) || panel._pveKomodoLoading || panel._pveKomodoSummaryLoading || panel._pveKomodoGuestLoading ||
            !guestLinkCurrent(panel) || !panel._pveKomodoGuestReady || !panel._pveKomodoGuestCsrf ||
            !server || server.template === true || !Number.isSafeInteger(server.link_revision);
        ['bind', 'unlink', 'open'].forEach(function (name) {
            const button = container.down('#pve-komodo-guest-' + name);
            if (!alive(button)) return;
            button.setDisabled(!!blocked || name === 'unlink' && !validGuest(link) ||
                name === 'open' && (!validGuest(link) || link.status !== 'linked' || link.can_navigate !== true));
        });
    }

    function applyGuestLinks(panel) {
        if (alive(panel) && panel.pveKomodoGuestSummary) { guestControls(panel); return; }
        if (!alive(panel) || panel.pveKomodoPage !== 'dashboard') return;
        function linkFor(id) {
            const old = panel._pveKomodoGuestLinks && panel._pveKomodoGuestLinks[id];
            return panel._pveKomodoGuestReady ? old || null : old ? Object.assign({}, old, { status: 'unavailable' }) : { status: 'unknown' };
        }
        const grid = panel.down('#pve-komodo-servers-grid');
        // Ext.data.Model can share the loadData source object. Set the records
        // first so ExtJS emits update events before the source rows are changed.
        if (alive(grid) && typeof grid.getStore().each === 'function') grid.getStore().each(function (record) {
            record.set('pve_guest', linkFor(record.get('id')));
        });
        if (panel._pveKomodoRows) (panel._pveKomodoRows.servers || []).forEach(function (row) {
            row.pve_guest = linkFor(row.id);
        });
        guestControls(panel);
    }

    function closeGuestDialog(panel) {
        const state = panel._pveKomodoGuestDialog;
        panel._pveKomodoGuestDialog = null;
        if (state) {
            state.closed = true;
            if (alive(state.dialog)) state.dialog.destroy();
        }
    }

    function cancelGuestLinks(panel) {
        panel._pveKomodoGuestGeneration = (panel._pveKomodoGuestGeneration || 0) + 1;
        const request = panel._pveKomodoGuestRequest;
        panel._pveKomodoGuestRequest = null;
        panel._pveKomodoGuestLoading = false;
        panel._pveKomodoGuestReady = false;
        panel._pveKomodoGuestCsrf = null;
        closeGuestDialog(panel);
        if (request) Ext.Ajax.abort(request);
    }

    function loadGuestLinks(panel, afterLoad) {
        if (!guestLinkActive(panel) || panel.pveKomodoPage !== 'dashboard' && !panel.pveKomodoGuestSummary) return;
        cancelGuestLinks(panel);
        panel._pveKomodoGuestUser = window.Proxmox && window.Proxmox.UserName;
        panel._pveKomodoGuestLoading = true;
        panel._pveKomodoGuestReady = false;
        panel._pveKomodoGuestCsrf = null;
        const generation = panel._pveKomodoGuestGeneration;
        applyGuestLinks(panel);
        let completed = false;
        function complete(options, success, response) {
            completed = true;
            if (!guestLinkCurrent(panel) || generation !== panel._pveKomodoGuestGeneration) return;
            panel._pveKomodoGuestLoading = false;
            panel._pveKomodoGuestRequest = null;
            try {
                const result = response && JSON.parse(response.responseText);
                if (!success || !result || !Array.isArray(result.links) || !Array.isArray(result.guests) ||
                    !Array.isArray(result.servers) || typeof result.csrf_token !== 'string' || !result.csrf_token ||
                    result.csrf_token.length > 4096 || /[\r\n]/.test(result.csrf_token)) throw new Error('invalid-guest-links');
                const links = Object.create(null);
                const servers = Object.create(null);
                result.servers.forEach(function (server) {
                    if (!server || typeof server.id !== 'string' || typeof server.name !== 'string' ||
                        !Number.isSafeInteger(server.link_revision) || server.link_revision < 0 || servers[server.id]) throw new Error('invalid-server');
                    servers[server.id] = server;
                });
                result.links.forEach(function (link) {
                    if (!validGuest(link) || typeof link.server_id !== 'string' ||
                        !servers[link.server_id] && link.server_unavailable !== true || links[link.server_id] ||
                        !Number.isSafeInteger(link.revision) || link.revision < 1 ||
                        ['linked', 'needs_confirmation', 'missing', 'unavailable'].indexOf(link.status) < 0) throw new Error('invalid-link');
                    links[link.server_id] = link;
                });
                panel._pveKomodoGuestLinks = links;
                panel._pveKomodoGuestServers = servers;
                panel._pveKomodoGuests = result.guests.filter(function (guest) { return validGuest(guest) && typeof guest.name === 'string'; });
                panel._pveKomodoGuestCsrf = result.csrf_token;
                panel._pveKomodoGuestReady = true;
            } catch (error) {
                guestLinkStatus(panel, 'PVE 关联读取失败，关联状态未知；请刷新后再操作');
            }
            applyGuestLinks(panel);
            guestLinkControls(panel);
            if (panel._pveKomodoGuestReady && typeof afterLoad === 'function') afterLoad();
        }
        try {
            const request = Ext.Ajax.request({ url: '/pve-komodo/api/guest-links', method: 'GET',
                disableCaching: false, timeout: 60000, callback: complete });
            if (!completed) panel._pveKomodoGuestRequest = request;
        } catch (error) { complete(null, false, null); }
    }

    function submitGuestLink(panel, payload, csrf, unlink) {
        const sourceUser = panel._pveKomodoGuestUser, owner = guestOwner(panel), workspace = owner && owner.workspace;
        const sourceTarget = guestLinkTarget(panel).server;
        const state = { pending: true };
        guestWriteState = state;
        guestLinkControls(panel);
        guestLinkStatus(panel, unlink ? '正在解除关联…' : '正在保存关联…');
        function complete(options, success, response) {
            if (!state.pending) return;
            state.pending = false;
            let message = '保存结果尚未确认，请刷新关联状态后核对，不要重复提交';
            try {
                const result = response && JSON.parse(response.responseText);
                if (success && (unlink ? result && result.removed === true : validGuest(result && result.link) && result.link.server_id === payload.server_id &&
                    result.link.guest_type === payload.guest_type && result.link.vmid === payload.vmid)) {
                    message = unlink ? '已解除关联，仅删除映射；VM/CT 和 Docker 服务保持原状' : '关联已保存';
                } else if (response && response.status === 409) {
                    message = result && result.detail === 'guest_identity_unavailable' ? '无法核验该 VM/CT 的身份，本次未保存关联；请检查客体配置后刷新' :
                        '关联或客体状态已变化，本次未保存，请刷新后重新确认';
                } else if (response && response.status === 403) message = '会话校验失败，本次未保存，请刷新后重新操作';
                else if (response && response.status === 401) message = 'PVE 会话已过期，请重新登录后核对关联状态';
            } catch (error) { /* Never display arbitrary server response text. */ }
            const sameUser = sourceUser === (window.Proxmox && window.Proxmox.UserName) && !(workspace && workspace._pveKomodoLoggedOut);
            if (sameUser) containerTreeStates.forEach(function (tree) { if (tree.workspace === workspace && tree.active) loadContainerTree(tree); });
            if (sameUser && panel.pveKomodoGuestSummary && guestLinkCurrent(panel) && activeGuestSummary === panel &&
                sourceTarget && guestLinkTarget(panel).server && guestLinkTarget(panel).server.id === sourceTarget.id) {
                loadGuestLinks(panel, function () {
                    guestLinkStatus(panel, message);
                    if (payload.server_id === sourceTarget.id) reconcileGuestSummary(panel, sourceTarget);
                });
            }
            const current = activeDashboard;
            if (sameUser && alive(current) && current._pveKomodoActive && current.pveKomodoPage === 'dashboard') {
                loadGuestLinks(current);
                statusText(current, message);
                updateControls(current);
            }
            if (alive(activeGuestOptions)) optionControls(activeGuestOptions);
            if (alive(activeGuestSummary)) summaryControls(activeGuestSummary);
            if (alive(activeStackPage)) stackControls(activeStackPage);
            guestInventoryPages.forEach(inventoryControls);
        }
        try {
            // A submitted metadata write survives card navigation; never retry it.
            Ext.Ajax.request({ url: '/pve-komodo/api/guest-links' + (unlink ? '/unlink' : ''), method: 'POST',
                headers: { 'X-PVE-Komodo-CSRF': csrf }, jsonData: payload, timeout: 60000, callback: complete });
        } catch (error) { complete(null, false, null); }
    }

    function confirmGuestLink(panel, server, guest, revision, csrf, unlink) {
        const generation = panel._pveKomodoGuestGeneration;
        const selectionGeneration = panel._pveKomodoSelectionGeneration;
        const state = { closed: false, dialog: null };
        panel._pveKomodoGuestDialog = state;
        guestLinkControls(panel);
        const message = unlink ? '解除服务器“' + server.name + '”与“' + guestLabel(guest) + '”的关联？仅删除本扩展中的映射，不会修改 VM/CT 或 Docker 服务。' :
            '将服务器“' + server.name + '”关联到“' + guestLabel(Object.assign({}, guest, { guest_name: guest.name, status: 'linked' })) + '”？' +
                (guest.guest_type === 'lxc' ? 'CT 使用较弱的身份校验；克隆、重建或标识变化后可能需要重新确认。' : 'VM 身份变化后需要重新确认。');
        const dialog = Ext.create('Ext.window.MessageBox', { closeAction: 'destroy' });
        state.dialog = dialog;
        dialog.show({ title: unlink ? '解除关联' : '确认关联 VM/CT', message: text(message),
            buttons: Ext.Msg.YESNO, icon: Ext.Msg.WARNING, defaultFocus: 'no', fn: function (answer) {
                if (state.closed) return;
                closeGuestDialog(panel);
                if (!guestLinkCurrent(panel) || generation !== panel._pveKomodoGuestGeneration ||
                    selectionGeneration !== panel._pveKomodoSelectionGeneration || answer !== 'yes') {
                    if (alive(panel)) guestLinkControls(panel);
                    return;
                }
                const payload = { server_id: server.id, expected_revision: revision };
                if (!unlink) { payload.guest_type = guest.guest_type; payload.vmid = guest.vmid; }
                submitGuestLink(panel, payload, csrf, unlink);
            } });
    }

    function guestButton(button, operation) {
        const panel = button.up('[pveKomodoGuestSummary]') || ownerPanel(button);
        if (!panel || !guestLinkCurrent(panel) || !panel._pveKomodoGuestReady || panel._pveKomodoGuestLoading || actionBusy(panel)) return;
        const target = guestLinkTarget(panel), server = target.server;
        if (!server || server.template === true || !Number.isSafeInteger(server.link_revision)) return;
        const link = target.link;
        if (operation === 'open') { openGuest(panel, link); return; }
        const csrf = panel._pveKomodoGuestCsrf;
        const revision = server.link_revision;
        if (operation === 'unlink') {
            if (validGuest(link)) confirmGuestLink(panel, server, link, revision, csrf, true);
            return;
        }
        const guests = panel._pveKomodoGuests.filter(function (guest) { return guest.template !== true; }).map(function (guest) {
            const existing = Object.values(panel._pveKomodoGuestLinks).find(function (link) {
                return link.guest_type === guest.guest_type && link.vmid === guest.vmid && link.server_id !== server.id;
            });
            return Object.assign({}, guest, { key: guest.guest_type + '/' + guest.vmid, occupied: existing || null,
                label: (guest.name.toLowerCase() === server.name.toLowerCase() ? '同名候选 · ' : '') +
                    (guest.guest_type === 'qemu' ? 'VM ' : 'CT ') + guest.vmid + ' · ' + guest.name + ' · ' + guest.node +
                    (existing ? existing.server_unavailable === true ? ' · 关联服务器不可访问，需先清理映射' : ' · 已关联其他服务器' : '') });
        }).sort(function (a, b) {
            return Number(b.name.toLowerCase() === server.name.toLowerCase()) - Number(a.name.toLowerCase() === server.name.toLowerCase()) || a.vmid - b.vmid;
        });
        const generation = panel._pveKomodoGuestGeneration;
        const selectionGeneration = panel._pveKomodoSelectionGeneration;
        const state = { closed: false, dialog: null };
        panel._pveKomodoGuestDialog = state;
        guestLinkControls(panel);
        let dialog;
        dialog = Ext.create('Ext.window.Window', {
            title: '关联 VM/CT', width: 580, modal: true, closeAction: 'destroy', bodyPadding: 16, layout: 'anchor',
            items: [{ xtype: 'component', html: '<p>服务器：' + text(server.name) + '</p><p>请选择 VM/CT；同名只作为推荐，不会自动建立关联。</p>' }, {
                xtype: 'combobox', itemId: 'pve-komodo-guest-target', anchor: '100%', fieldLabel: 'PVE 对象',
                queryMode: 'local', editable: false, forceSelection: true, displayField: 'label', valueField: 'key', value: null,
                store: { autoDestroy: true, fields: ['key', 'label'], data: guests }, listConfig: { itemTpl: '{label:htmlEncode}' },
                emptyText: guests.length ? '请选择 VM 或 CT' : '没有可关联的 VM/CT', listeners: { change: function (field) {
                    if (!alive(dialog)) return;
                    const selected = guests.find(function (guest) { return guest.key === field.getValue(); });
                    const save = dialog.down('#pve-komodo-guest-save');
                    const cleanup = dialog.down('#pve-komodo-guest-cleanup');
                    if (alive(save)) save.setDisabled(!selected || !!selected.occupied);
                    if (alive(cleanup)) cleanup.setDisabled(!selected || !selected.occupied || selected.occupied.server_unavailable !== true);
                } },
            }],
            buttons: [{ text: '关联', itemId: 'pve-komodo-guest-save', disabled: true, handler: function () {
                if (state.closed || !guestLinkCurrent(panel) || generation !== panel._pveKomodoGuestGeneration ||
                    selectionGeneration !== panel._pveKomodoSelectionGeneration) return;
                const guest = guests.find(function (candidate) { return candidate.key === dialog.down('#pve-komodo-guest-target').getValue(); });
                if (!guest || guest.occupied) return;
                closeGuestDialog(panel);
                confirmGuestLink(panel, server, guest, revision, csrf, false);
            } }, { text: '解除失效关联', itemId: 'pve-komodo-guest-cleanup', disabled: true, handler: function () {
                if (state.closed || !guestLinkCurrent(panel) || generation !== panel._pveKomodoGuestGeneration ||
                    selectionGeneration !== panel._pveKomodoSelectionGeneration) return;
                const guest = guests.find(function (candidate) { return candidate.key === dialog.down('#pve-komodo-guest-target').getValue(); });
                const orphan = guest && guest.occupied;
                if (!orphan || orphan.server_unavailable !== true) return;
                closeGuestDialog(panel);
                confirmGuestLink(panel, { id: orphan.server_id, name: orphan.server_name || '不可访问的服务器（' + orphan.server_id + '）' },
                    orphan, orphan.revision, csrf, true);
            } }, { text: '取消', handler: function () { closeGuestDialog(panel); guestLinkControls(panel); } }],
            listeners: { beforedestroy: function () {
                if (panel._pveKomodoGuestDialog === state) {
                    state.closed = true;
                    panel._pveKomodoGuestDialog = null;
                    if (alive(panel)) guestLinkControls(panel);
                }
            } },
        });
        state.dialog = dialog;
        dialog.show();
    }

    function guestCardConfig(node) {
        return Object.assign({ xtype: 'panel', itemId: guestItemId, title: 'Docker', iconCls: 'fa fa-cubes',
            border: false, expandedOnInit: true }, guestSummaryConfig(node));
    }

    function reconcileGuestSummary(panel, server) {
        if (!guestLinkCurrent(panel) || activeGuestSummary !== panel || dockerNavigationBusy()) return;
        const owner = guestOwner(panel), workspace = owner && owner.workspace;
        if (!workspace) return;
        const link = panel._pveKomodoGuestLinks[server.id];
        if (!link) {
            if (panel.pveKomodoDirectServer) loadGuestSummary(panel);
            else openServerManagement(workspace, server.id, server.name);
            return;
        }
        if (!validGuest(link) || link.status !== 'linked' || link.can_navigate !== true) {
            cancelGuestSummary(panel); clearSummary(panel); clearGuestHistory(panel);
            summaryStatus(panel, '关联待确认或 PVE 对象当前不可访问，请刷新后核对'); summaryControls(panel);
            return;
        }
        const nativeId = link.guest_type + '/' + link.vmid;
        const base = '/pve-komodo/api/guests/' + nativeId;
        if (panel.pveKomodoGuestBase === base) { loadGuestSummary(panel); return; }
        const tree = Array.from(containerTreeStates).find(function (state) { return state.workspace === workspace; });
        if (tree) {
            tree.selected = 'server:' + server.id;
            const selection = tree.native.getSelectionModel();
            if (typeof selection.deselectAll === 'function' && typeof selection.getSelection === 'function' &&
                selection.getSelection().some(function (row) { return row.get('id') === nativeId; })) selection.deselectAll();
        }
        requestedGuestServer = { base: base, server_id: server.id, user: window.Proxmox && window.Proxmox.UserName };
        workspace.selectById(nativeId);
        const config = workspace.query('pvePanelConfig').filter(function (candidate) {
            return alive(candidate) && candidate.pveSelNode && candidate.pveSelNode.data.id === nativeId;
        }).pop();
        if (config && typeof config.selectById === 'function') config.selectById(guestItemId);
        else openDockerDestination(workspace, server.id, null, '关联已保存；PVE 对象当前不可访问，请从服务器列表重新打开');
    }

    let activeGuestSummary = null;
    let guestSummaryWrite = null;
    const guestSummaryActions = [
        ['restart_all_containers', '重启容器', '重启服务器的所有容器，包括原先已停止的容器，可能启动这些容器并中断运行中的服务。'],
        ['pause_all_containers', '暂停容器', '冻结容器进程，相关服务将无法响应；已停止或已暂停的容器可能操作失败。'],
        ['stop_all_containers', '停止容器', '停止服务器的所有容器，会中断相关服务。'],
        ['prune_buildx', '清理 Buildx', '清理所有未使用的 Buildx 构建缓存（docker buildx prune -a -f），后续构建可能需要重建缓存。'],
        ['prune_system', '清理系统', '清理未使用的容器、网络、镜像及存储卷（docker system prune -a -f --volumes），卷数据可能被永久删除。'],
    ];
    const guestHistoryIntervals = [
        ['5-sec', '5 秒'], ['15-sec', '15 秒'], ['30-sec', '30 秒'], ['1-min', '1 分钟'], ['5-min', '5 分钟'],
        ['15-min', '15 分钟'], ['30-min', '30 分钟'], ['1-hr', '1 小时'], ['6-hr', '6 小时'], ['1-day', '1 天'],
    ];
    const guestChartDefinitions = [
        ['load', '平均负载', [['load1', '1 分钟'], ['load5', '5 分钟'], ['load15', '15 分钟']], null],
        ['cpu', 'CPU 使用率', [['cpu_percent', 'CPU']], 'percent'],
        ['memory', '内存使用量', [['memory_used', '已使用'], ['memory_cache', 'Cache / Buffers'], ['memory_arc', 'ZFS ARC']], 'bytes'],
        ['disk', '磁盘使用率', [['disk_percent', '磁盘']], 'percent'],
        ['ingress', '网络接收（B / 采样）', [['network_ingress_bytes', '接收']], 'bytes'],
        ['egress', '网络发送（B / 采样）', [['network_egress_bytes', '发送']], 'bytes'],
    ];
    function summaryOwner(component) { return component.up('[pveKomodoGuestSummary]'); }
    function summaryStatus(panel, message, history) {
        const component = panel.down('#pve-komodo-guest-' + (history ? 'history' : 'summary') + '-status');
        if (alive(component)) component.setText(text(message));
    }
    function summaryControls(panel) {
        if (!alive(panel)) return;
        const data = panel._pveKomodoSummaryData;
        const busy = optionBusy() || !!panel._pveKomodoGuestDialog || !!panel._pveKomodoSummaryConfirmation || alive(panel._pveKomodoDescriptionEditor);
        guestSummaryActions.forEach(function (action) {
            panel.down('#pve-komodo-guest-summary-' + action[0]).setDisabled(busy || panel._pveKomodoSummaryLoading ||
                !data || data.can_execute !== true || !panel._pveKomodoSummaryActive);
        });
        panel.down('#pve-komodo-guest-summary-refresh').setDisabled(busy || !!panel._pveKomodoSummaryLoading);
        panel.down('#pve-komodo-guest-history-interval').setDisabled(busy || !!panel._pveKomodoSummaryLoading || !data);
        panel.down('#pve-komodo-guest-current-more').setDisabled(busy || !!panel._pveKomodoSummaryLoading || !data || !panel._pveKomodoSummaryActive);
        panel.down('#pve-komodo-guest-description-edit').setDisabled(busy || !!panel._pveKomodoSummaryLoading ||
            !panel._pveKomodoSummaryActive || !data || data.can_write !== true || typeof data.description !== 'string' ||
            typeof data.description_version !== 'string' || !/^[0-9a-f]{64}$/.test(data.description_version));
        guestControls(panel);
    }
    function summarySize(value, unit) { return numeric(value) ? value.toFixed(2) + ' ' + unit : '-'; }
    function summaryPercent(value) { return numeric(value) ? value.toFixed(1) + '%' : '-'; }
    function summaryCurrent(panel, data) {
        const current = data && data.current || {};
        const values = {
            state: data ? stateText(data.server_state) : '-',
            load: Array.isArray(current.load_average) ? current.load_average.map(function (value) { return numeric(value) ? value.toFixed(2) : '-'; }).join(' / ') : '-',
            cpu: summaryPercent(current.cpu_percent),
            memory: summaryPercent(current.memory_percent) + '（' + summarySize(current.memory_used_gb, 'GiB') + ' / ' + summarySize(current.memory_total_gb, 'GiB') + '）',
            disk: summaryPercent(current.disk_percent) + '（' + summarySize(current.disk_used_gb, 'GiB') + ' / ' + summarySize(current.disk_total_gb, 'GiB') + '）',
            network: '↓ ' + summarySize(current.network_ingress_bytes_per_second, 'B/s') + ' / ↑ ' + summarySize(current.network_egress_bytes_per_second, 'B/s'),
            version: current.version ? text(current.version) : '-',
        };
        Object.keys(values).forEach(function (key) {
            const widget = panel.down('#pve-komodo-guest-current-' + key);
            const field = key === 'cpu' ? current.cpu_percent : key === 'memory' ? current.memory_percent : key === 'disk' ? current.disk_percent : null;
            if (alive(widget)) widget.updateValue(values[key], numeric(field) ? Math.max(0, Math.min(1, field / 100)) : 0);
        });
    }
    function summaryFilter(panel, section) {
        if (!alive(panel)) return;
        const all = panel._pveKomodoSummaryRows && panel._pveKomodoSummaryRows[section] || [];
        const search = panel.down('#pve-komodo-guest-' + section + '-search');
        const query = search ? String(search.getValue() || '').toLowerCase().trim() : '';
        const rows = query ? all.filter(function (row) {
            return [row.name, row.exe, row.pid, row.state].join(' ').toLowerCase().includes(query);
        }) : all;
        panel.down('#pve-komodo-guest-' + section + '-grid').getStore().loadData(rows);
    }
    function clearSummary(panel) {
        panel._pveKomodoSummaryData = null;
        panel._pveKomodoSummaryRows = { containers: [], disks: [], processes: [] };
        ['containers', 'processes'].forEach(function (section) { summaryFilter(panel, section); });
        panel.down('#pve-komodo-guest-recent-grid').getStore().loadData([]);
        panel.down('#pve-komodo-guest-description').update('-');
        summaryCurrent(panel, null);
    }
    function clearGuestHistory(panel) {
        guestChartDefinitions.forEach(function (definition) {
            panel.down('#pve-komodo-guest-chart-' + definition[0]).getStore().loadData([]);
        });
    }
    function cancelGuestHistory(panel) {
        panel._pveKomodoHistoryGeneration = (panel._pveKomodoHistoryGeneration || 0) + 1;
        const request = panel._pveKomodoHistoryRequest;
        panel._pveKomodoHistoryRequest = null;
        if (request) Ext.Ajax.abort(request);
    }
    function cancelGuestSummary(panel) {
        cancelGuestLinks(panel);
        panel._pveKomodoSummaryGeneration = (panel._pveKomodoSummaryGeneration || 0) + 1;
        const request = panel._pveKomodoSummaryRequest;
        panel._pveKomodoSummaryRequest = null;
        panel._pveKomodoSummaryLoading = false;
        if (request) Ext.Ajax.abort(request);
        cancelGuestHistory(panel);
        const confirmation = panel._pveKomodoSummaryConfirmation;
        panel._pveKomodoSummaryConfirmation = null;
        if (confirmation && alive(confirmation.dialog)) confirmation.dialog.destroy();
        const editor = panel._pveKomodoDescriptionEditor;
        panel._pveKomodoDescriptionEditor = null;
        if (alive(editor)) editor.destroy();
        const disks = panel._pveKomodoDisksWindow;
        panel._pveKomodoDisksWindow = null;
        if (alive(disks)) disks.destroy();
    }
    function loadGuestHistory(panel) {
        if (!alive(panel) || !panel._pveKomodoSummaryActive) return;
        const data = panel._pveKomodoSummaryData;
        if (!data) return;
        const interval = panel.down('#pve-komodo-guest-history-interval').getValue();
        if (!guestHistoryIntervals.some(function (item) { return item[0] === interval; })) return;
        cancelGuestHistory(panel);
        clearGuestHistory(panel);
        const generation = panel._pveKomodoHistoryGeneration;
        summaryStatus(panel, '正在读取历史采样…', true);
        let completed = false;
        function complete(options, success, response) {
            completed = true;
            if (!alive(panel) || !panel._pveKomodoSummaryActive || panel._pveKomodoSummaryData !== data ||
                generation !== panel._pveKomodoHistoryGeneration) return;
            panel._pveKomodoHistoryRequest = null;
            let message = optionsError(response, '历史数据读取失败，请刷新后重试');
            try {
                const result = response && JSON.parse(response.responseText);
                if (!success || !result || result.server_id !== data.server_id || result.interval !== interval ||
                    result.binding_revision !== data.binding_revision ||
                    !Array.isArray(result.points) || result.points.length > 200) throw new Error('invalid-history');
                const metric = function (value, factor) { const converted = numeric(value) ? value * (factor || 1) : null; return numeric(converted) ? converted : null; };
                const rows = result.points.filter(function (point) {
                    return point && Number.isSafeInteger(point.ts) && point.ts >= 0 && !Number.isNaN(new Date(point.ts).getTime());
                }).map(function (point) {
                    const load = Array.isArray(point.load_average) ? point.load_average : [];
                    return { time: point.ts, load1: metric(load[0]), load5: metric(load[1]), load15: metric(load[2]),
                        cpu_percent: metric(point.cpu_percent), disk_percent: metric(point.disk_percent),
                        memory_used: metric(point.memory_used_gb, 1073741824), memory_cache: metric(point.memory_cache_gb, 1073741824),
                        memory_arc: metric(point.memory_arc_gb, 1073741824), network_ingress_bytes: metric(point.network_ingress_bytes),
                        network_egress_bytes: metric(point.network_egress_bytes) };
                }).sort(function (a, b) { return a.time - b.time; });
                guestChartDefinitions.forEach(function (definition) { panel.down('#pve-komodo-guest-chart-' + definition[0]).getStore().loadData(rows); });
                message = rows.length ? '已读取 ' + rows.length + ' 个真实采样点；网络为每次采样的字节量' : '暂无历史采样数据';
                if (Array.isArray(result.errors) && result.errors.length) message += '（部分数据不可用）';
            } catch (error) { /* Never invent missing samples or display raw upstream errors. */ }
            summaryStatus(panel, message, true);
        }
        try {
            const request = Ext.Ajax.request({ url: panel.pveKomodoGuestBase + '/history', method: 'GET', disableCaching: false,
                params: { interval: interval }, timeout: 60000, callback: complete });
            if (!completed) panel._pveKomodoHistoryRequest = request;
        } catch (error) { complete(null, false, null); }
    }
    function loadGuestSummary(panel) {
        if (!alive(panel) || !panel._pveKomodoSummaryActive) return;
        if (requestedGuestServer && requestedGuestServer.base === panel.pveKomodoGuestBase) {
            if (requestedGuestServer.user === (window.Proxmox && window.Proxmox.UserName)) panel._pveKomodoExpectedServer = requestedGuestServer.server_id;
            requestedGuestServer = null;
        }
        const expectedServer = panel._pveKomodoExpectedServer;
        cancelGuestSummary(panel);
        clearSummary(panel); clearGuestHistory(panel);
        panel._pveKomodoSummaryLoading = true;
        const generation = panel._pveKomodoSummaryGeneration;
        summaryStatus(panel, '正在读取关联服务器概要…'); summaryStatus(panel, '尚未读取历史采样', true);
        summaryControls(panel);
        loadGuestLinks(panel);
        let completed = false;
        function complete(options, success, response) {
            completed = true;
            if (!alive(panel) || !panel._pveKomodoSummaryActive || generation !== panel._pveKomodoSummaryGeneration) return;
            panel._pveKomodoSummaryRequest = null; panel._pveKomodoSummaryLoading = false;
            let message = optionsError(response, '服务器概要读取失败，请刷新后重试');
            try {
                const data = response && JSON.parse(response.responseText);
                if (success && data && expectedServer && data.server_id !== expectedServer) {
                    message = '关联已变化，当前服务器与所选资源不一致，请重新核验关联';
                    throw new Error('guest-server-changed');
                }
                if (!success || !data || !serverReplyMatches(panel, data) || typeof data.server_id !== 'string' || !/^[0-9a-f]{24}$/i.test(data.server_id) ||
                    !Number.isSafeInteger(data.binding_revision) || data.binding_revision < 0 ||
                    typeof data.csrf_token !== 'string' || !data.csrf_token || data.csrf_token.length > 4096 || /[\r\n]/.test(data.csrf_token) ||
                    !data.current || typeof data.current !== 'object' || Array.isArray(data.current) || !['containers', 'disks', 'processes', 'updates'].every(function (key) {
                        return Array.isArray(data[key]) && data[key].every(function (row) { return row && typeof row === 'object' && !Array.isArray(row); });
                    }) || typeof data.can_execute !== 'boolean') throw new Error('invalid-summary');
                panel._pveKomodoSummaryData = data;
                panel._pveKomodoSummaryRows = { containers: data.containers, disks: data.disks, processes: data.processes };
                ['containers', 'processes'].forEach(function (section) { summaryFilter(panel, section); });
                panel.down('#pve-komodo-guest-recent-grid').getStore().loadData(data.updates);
                panel.down('#pve-komodo-guest-description').update(text(data.description).replace(/\r?\n/g, '<br>'));
                summaryCurrent(panel, data);
                message = (panel.pveKomodoDirectServer ? '服务器：' : '关联服务器：') + (data.server_name || data.server_id) + '；读取时间：' + dateText(data.updated_at) +
                    '（Komodo 监控缓存；缺测值显示 -）';
                if (Array.isArray(data.errors) && data.errors.length) message += '；部分数据不可用';
                loadGuestHistory(panel);
            } catch (error) { /* No stale rows or fabricated default metrics on failure. */ }
            summaryStatus(panel, message); summaryControls(panel);
        }
        try {
            const request = Ext.Ajax.request({ url: panel.pveKomodoGuestBase + '/summary', method: 'GET', disableCaching: false, timeout: 60000, callback: complete });
            if (!completed) panel._pveKomodoSummaryRequest = request;
        } catch (error) { complete(null, false, null); }
    }
    function confirmGuestSummary(panel, action) {
        const data = panel._pveKomodoSummaryData;
        if (!alive(panel) || !panel._pveKomodoSummaryActive || !data || !data.can_execute || optionBusy() || panel._pveKomodoSummaryConfirmation) return;
        const requestId = uuid(); if (!requestId) { summaryStatus(panel, '浏览器无法生成安全请求标识，本次未提交'); return; }
        const generation = panel._pveKomodoSummaryGeneration;
        const dialog = Ext.create('Ext.window.MessageBox');
        const confirmation = { dialog: dialog }; panel._pveKomodoSummaryConfirmation = confirmation;
        dialog.show({ title: '确认' + action[1], buttons: Ext.Msg.YESNO, icon: Ext.Msg.WARNING,
            message: text('服务器：' + (data.server_name || data.server_id) + '。' + action[2] +
                (action[0].endsWith('_containers') ? '带 komodo.skip 标签且值不为 false 的容器会被跳过。' : '')),
            fn: function (answer) {
                if (panel._pveKomodoSummaryConfirmation !== confirmation) return;
                panel._pveKomodoSummaryConfirmation = null; if (alive(dialog)) dialog.destroy();
                if (!alive(panel) || !panel._pveKomodoSummaryActive || generation !== panel._pveKomodoSummaryGeneration ||
                    data !== panel._pveKomodoSummaryData || optionBusy()) return;
                if (answer !== 'yes') { summaryControls(panel); return; }
                const state = { pending: true }; guestSummaryWrite = state;
                const path = panel.pveKomodoGuestBase;
                const payload = { action: action[0], server_id: data.server_id, binding_revision: data.binding_revision, request_id: requestId };
                summaryStatus(panel, '正在提交' + action[1] + '，请勿重复操作…'); summaryControls(panel);
                function complete(options, success, response) {
                    if (!state.pending) return; state.pending = false;
                    let message = '请求结果尚未确认，请先到 Komodo 的操作记录核对，不要重复提交';
                    try {
                        const result = response && JSON.parse(response.responseText);
                        const entry = result && Array.isArray(result.results) && result.results.length === 1 && result.results[0];
                        if (success && result.section === 'servers' && result.action === action[0] && entry && entry.id === data.server_id &&
                            ['submitted', 'completed', 'failed', 'outcome_unknown'].includes(entry.status) &&
                            entry.ok === ['submitted', 'completed'].includes(entry.status)) {
                            if (entry.status === 'submitted') message = action[1] + '已提交，尚未确认任务完成，请手动刷新观察';
                            else if (entry.status === 'completed') message = action[1] + '已完成，请手动刷新观察';
                            else if (entry.status === 'failed') message = action[1] + '失败，请到 Komodo 的操作记录核对';
                        } else if (response && [401, 403, 404, 409, 422].includes(response.status)) message = optionsError(response, '请求未通过校验，本次未提交，请刷新后重试');
                    } catch (error) { /* A submitted POST is never retried or treated as cancelled. */ }
                    const current = activeGuestSummary;
                    if (alive(current) && current._pveKomodoSummaryActive) {
                        if (current.pveKomodoGuestBase === path) { current._pveKomodoSummaryData = null; summaryStatus(current, message); }
                        summaryControls(current);
                    }
                    if (alive(activeGuestOptions)) optionControls(activeGuestOptions);
                    if (alive(activeDashboard)) updateControls(activeDashboard);
                    if (alive(activeStackPage)) stackControls(activeStackPage);
                }
                try {
                    Ext.Ajax.request({ url: path + '/actions', method: 'POST', timeout: 120000,
                        headers: { 'X-PVE-Komodo-CSRF': data.csrf_token }, jsonData: payload, callback: complete });
                } catch (error) { complete(null, false, null); }
            },
        });
        summaryControls(panel);
    }
    function editGuestDescription(panel) {
        const data = panel._pveKomodoSummaryData;
        if (!alive(panel) || !panel._pveKomodoSummaryActive || !data || data.can_write !== true || optionBusy() ||
            panel._pveKomodoSummaryLoading || panel._pveKomodoSummaryConfirmation || alive(panel._pveKomodoDescriptionEditor) ||
            typeof data.description !== 'string' || typeof data.description_version !== 'string' || !/^[0-9a-f]{64}$/.test(data.description_version)) return;
        const generation = panel._pveKomodoSummaryGeneration;
        const original = data.description;
        function requestClose() {
            if (editor.down('#pve-komodo-description-value').getValue() === original) { editor.destroy(); return; }
            if (alive(editor._discardDialog)) return;
            const dialog = Ext.create('Ext.window.MessageBox'); editor._discardDialog = dialog;
            dialog.show({ title: '放弃修改', message: '有尚未保存的描述，确定放弃？', buttons: Ext.Msg.YESNO, icon: Ext.Msg.WARNING,
                fn: function (answer) {
                    editor._discardDialog = null;
                    if (alive(dialog)) dialog.destroy();
                    if (answer === 'yes' && alive(editor)) editor.destroy();
                } });
        }
        const editor = Ext.create('Ext.window.Window', {
            title: '编辑描述', width: 650, modal: true, closeAction: 'destroy', bodyPadding: 12, layout: 'anchor',
            defaults: { anchor: '100%' }, items: [
                { xtype: 'displayfield', value: text('Komodo 服务器：' + (data.server_name || data.server_id)) },
                { xtype: 'textareafield', itemId: 'pve-komodo-description-value', value: original, height: 260,
                    maxLength: 16384, enforceMaxLength: true, allowBlank: true, ariaLabel: '服务器描述' },
                { xtype: 'component', itemId: 'pve-komodo-description-error', html: '', style: { color: '#c00' } },
            ], buttons: [{ text: '保存', itemId: 'pve-komodo-description-save', handler: function () {
                if (!alive(panel) || !panel._pveKomodoSummaryActive || data !== panel._pveKomodoSummaryData ||
                    generation !== panel._pveKomodoSummaryGeneration || optionBusy()) return;
                const value = editor.down('#pve-komodo-description-value').getValue();
                if (typeof value !== 'string' || value.length > 16384 || /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(value)) {
                    editor.down('#pve-komodo-description-error').update('描述最多 16384 个字符，不能包含控制字符'); return;
                }
                if (value === original) { editor.destroy(); summaryStatus(panel, '描述未改变，本次未提交'); return; }
                const requestId = uuid();
                if (!requestId) { editor.down('#pve-komodo-description-error').update('浏览器无法生成安全请求标识，本次未提交'); return; }
                const state = { pending: true }; guestSummaryWrite = state;
                const path = panel.pveKomodoGuestBase;
                const payload = { server_id: data.server_id, binding_revision: data.binding_revision,
                    base_version: data.description_version, description: value, request_id: requestId };
                editor.destroy(); summaryStatus(panel, '正在保存描述，请勿重复提交…'); summaryControls(panel);
                function complete(options, success, response) {
                    if (!state.pending) return; state.pending = false;
                    let accepted = false;
                    let message = '描述保存结果尚未确认，请刷新核对后再编辑，不要重复提交';
                    try {
                        const result = response && JSON.parse(response.responseText);
                        if (success && result && result.ok === true && result.status === 'completed') {
                            accepted = true; message = '描述已保存';
                        } else if (success && result && result.ok === false && result.status === 'failed') message = '描述保存失败，请刷新后核对';
                        else if (response && response.status === 409 && result && result.detail === 'server_description_conflict')
                            message = '描述已被其他页面修改，本次未保存，请刷新后重新编辑';
                        else if (response && [401, 403, 404, 409, 422].includes(response.status))
                            message = optionsError(response, '描述未通过校验，本次未保存，请刷新后核对');
                    } catch (error) { /* Never retry a metadata write or expose raw upstream responses. */ }
                    const current = activeGuestSummary;
                    if (alive(current) && current._pveKomodoSummaryActive) {
                        if (current.pveKomodoGuestBase === path) {
                            current._pveKomodoSummaryData = null;
                            if (accepted) loadGuestSummary(current);
                            summaryStatus(current, message);
                        }
                        summaryControls(current);
                    }
                    if (alive(activeGuestOptions)) optionControls(activeGuestOptions);
                    if (alive(activeDashboard)) updateControls(activeDashboard);
                    if (alive(activeStackPage)) stackControls(activeStackPage);
                }
                try {
                    Ext.Ajax.request({ url: path + '/description', method: 'POST', timeout: 90000,
                        headers: { 'X-PVE-Komodo-CSRF': data.csrf_token }, jsonData: payload, callback: complete });
                } catch (error) { complete(null, false, null); }
            } }, { text: '重置', itemId: 'pve-komodo-description-reset', handler: function () {
                editor.down('#pve-komodo-description-value').setValue(original); editor.down('#pve-komodo-description-error').update('');
            } }, { text: '取消', itemId: 'pve-komodo-description-cancel', handler: requestClose }],
            listeners: { beforeclose: function () { requestClose(); return false; }, beforedestroy: function () {
                if (alive(editor._discardDialog)) editor._discardDialog.destroy();
                if (panel._pveKomodoDescriptionEditor === editor) panel._pveKomodoDescriptionEditor = null;
                if (alive(panel)) summaryControls(panel);
            } },
        });
        panel._pveKomodoDescriptionEditor = editor; editor.show(); summaryControls(panel);
    }
    function guestChartConfig(definition) {
        return { xtype: 'proxmoxRRDChart', itemId: 'pve-komodo-guest-chart-' + definition[0], title: definition[1], flex: 1,
            height: 280, minWidth: 0, margin: '0 5 8 0', unit: definition[3], powerOfTwo: definition[3] === 'bytes',
            animation: false, collapsible: true, titleCollapse: true,
            fields: definition[2].map(function (field) { return { type: 'line', xField: 'time', yField: field[0], title: field[1], fill: false,
                style: { lineWidth: 1.5, opacity: 1 } }; }),
            axes: [{ type: 'numeric', position: 'left', grid: true, minimum: 0, renderer: 'leftAxisRenderer' },
                { type: 'time', position: 'bottom', grid: true, fields: ['time'], dateFormat: 'm-d H:i' }],
            store: { autoDestroy: true, fields: ['time'].concat(definition[2].map(function (field) { return field[0]; })), data: [] },
        };
    }
    function showGuestDisks(panel) {
        const data = panel._pveKomodoSummaryData;
        if (!alive(panel) || !panel._pveKomodoSummaryActive || !data || panel._pveKomodoSummaryLoading || alive(panel._pveKomodoDisksWindow)) return;
        const dialog = Ext.create('Ext.window.Window', {
            title: '磁盘信息', width: 900, height: 400, modal: true, constrain: true, closeAction: 'destroy', layout: 'fit',
            items: [{ xtype: 'gridpanel', itemId: 'pve-komodo-guest-disks-grid', border: false,
                store: { autoDestroy: true, fields: ['mount', 'file_system', 'used_gb', 'total_gb', 'percent'],
                    data: data.disks.map(function (row) { return Object.assign({}, row); }) },
                columns: [
                    { text: '路径', dataIndex: 'mount', flex: 2, minWidth: 160, renderer: text },
                    { text: '文件系统', dataIndex: 'file_system', flex: 1, minWidth: 100, renderer: text },
                    { text: '已使用', dataIndex: 'used_gb', flex: 1, minWidth: 120, renderer: function (value) { return summarySize(value, 'GiB'); } },
                    { text: '总计', dataIndex: 'total_gb', flex: 1, minWidth: 120, renderer: function (value) { return summarySize(value, 'GiB'); } },
                    { text: '使用率', dataIndex: 'percent', flex: 1, minWidth: 100, renderer: summaryPercent },
                ], viewConfig: { stripeRows: true, enableTextSelection: true, emptyText: '暂无可显示的磁盘数据', deferEmptyText: false },
            }], listeners: { beforedestroy: function (window) {
                if (panel._pveKomodoDisksWindow === window) panel._pveKomodoDisksWindow = null;
            } },
        });
        panel._pveKomodoDisksWindow = dialog; dialog.show();
    }
    function guestSummaryGrid(section, title, fields, columns, height) {
        const grid = { xtype: 'gridpanel', itemId: 'pve-komodo-guest-' + section + '-grid', title: title, height: height,
            flex: 1, minWidth: 0, collapsible: true, titleCollapse: true, animCollapse: false, floatable: false,
            collapseDirection: section === 'containers' ? 'left' : 'right', margin: '0 5 8 0',
            store: { autoDestroy: true, fields: fields, data: [] }, columns: columns,
            viewConfig: { stripeRows: true, enableTextSelection: true, emptyText: '暂无可显示的数据', deferEmptyText: false } };
        if (section === 'containers' || section === 'processes') grid.tbar = [{ xtype: 'textfield', itemId: 'pve-komodo-guest-' + section + '-search',
            fieldLabel: '搜索', labelWidth: 40, flex: 1, enableKeyEvents: true,
            listeners: { change: function (field) { summaryFilter(summaryOwner(field), section); } } }];
        return grid;
    }
    function guestSummaryConfig(node) {
        const current = { xtype: 'panel', title: '当前', flex: 1, minWidth: 0, height: 300, bodyPadding: 10,
            layout: { type: 'vbox', align: 'stretch' }, margin: '0 5 0 0',
            items: [['state', '状态'], ['load', '平均负载（1 / 5 / 15 分钟）'], ['cpu', 'CPU'], ['memory', '内存'],
                ['disk', '磁盘'], ['network', '网络'], ['version', 'Periphery 版本']].map(function (field) {
                return { xtype: 'pmxInfoWidget', itemId: 'pve-komodo-guest-current-' + field[0], title: field[1], text: '-', value: 0,
                    height: 30, printBar: ['cpu', 'memory', 'disk'].includes(field[0]) };
            }), bbar: ['->', { text: '更多', itemId: 'pve-komodo-guest-current-more', disabled: true,
                handler: function (button) { showGuestDisks(summaryOwner(button)); } }] };
        const diskPercent = function (value) { return summaryPercent(value); };
        const rows = [];
        for (let index = 0; index < guestChartDefinitions.length; index += 3) rows.push({ xtype: 'container',
            layout: { type: 'hbox', align: 'stretch' }, items: guestChartDefinitions.slice(index, index + 3).map(guestChartConfig) });
        const processes = guestSummaryGrid('processes', '进程', ['pid', 'name', 'exe', 'cpu_percent', 'memory_mb'], [
            { text: 'PID', dataIndex: 'pid', width: 90, renderer: text }, { text: '名称', dataIndex: 'name', flex: 1, minWidth: 150, renderer: text },
            { text: '可执行文件', dataIndex: 'exe', flex: 2, minWidth: 200, renderer: text },
            { text: 'CPU（100% 为一核心）', dataIndex: 'cpu_percent', width: 170, renderer: diskPercent },
            { text: '内存', dataIndex: 'memory_mb', width: 140, renderer: function (value) { return summarySize(value, 'MiB'); } },
        ], 300);
        processes.margin = '0 0 8 0';
        const recent = { xtype: 'gridpanel', title: '操作记录', itemId: 'pve-komodo-guest-recent-grid', flex: 1, minWidth: 0, height: 300,
            store: { autoDestroy: true, fields: ['id', 'operation', 'status', 'success', 'start_ts', 'username'], data: [] },
            columns: [{ text: '操作', dataIndex: 'operation', flex: 2, minWidth: 120, sortable: false, renderer: function (value) { return mappedText(operationLabels, value); } },
                { text: '结果', dataIndex: 'status', flex: 1, minWidth: 70, sortable: false, renderer: function (value, meta, record) { return updateResult(record.data); } },
                { text: '时间', dataIndex: 'start_ts', flex: 2, minWidth: 160, sortable: false, renderer: alertTime }],
            viewConfig: { stripeRows: true, enableTextSelection: true, emptyText: '暂无操作记录', deferEmptyText: false } };
        function leave(panel) {
            panel._pveKomodoSummaryActive = false;
            if (activeGuestSummary === panel) activeGuestSummary = null;
            cancelGuestSummary(panel);
        }
        return { pveKomodoGuestSummary: true, pveKomodoGuestBase: serverPageBase(node),
            scrollable: true, bodyPadding: 5, layout: 'anchor', defaults: { anchor: '100%' },
            tbar: { overflowHandler: 'scroller', items: guestSummaryActions.map(function (action) {
                return { text: action[1], itemId: 'pve-komodo-guest-summary-' + action[0], disabled: true,
                    handler: function (button) { confirmGuestSummary(summaryOwner(button), action); } };
            }).concat(['-', { text: '刷新', itemId: 'pve-komodo-guest-summary-refresh', handler: function (button) { loadGuestSummary(summaryOwner(button)); } }, '-',
                { text: '关联 VM/CT', itemId: 'pve-komodo-guest-bind', disabled: true, handler: function (button) { guestButton(button, 'bind'); } },
                { text: '解除关联', itemId: 'pve-komodo-guest-unlink', disabled: true, handler: function (button) { guestButton(button, 'unlink'); } },
                { text: '打开 PVE 对象', itemId: 'pve-komodo-guest-open', disabled: true, handler: function (button) { guestButton(button, 'open'); } }, '->',
                { xtype: 'combobox', itemId: 'pve-komodo-guest-history-interval', fieldLabel: '历史采样间隔', labelWidth: 100, width: 240,
                    value: '1-day', editable: false, queryMode: 'local', displayField: 'label', valueField: 'value',
                    store: { autoDestroy: true, fields: ['value', 'label'], data: guestHistoryIntervals.map(function (item) { return { value: item[0], label: item[1] }; }) },
                    listeners: { change: function (field) { loadGuestHistory(summaryOwner(field)); } } }]) },
            bbar: [{ xtype: 'tbtext', itemId: 'pve-komodo-guest-summary-status', text: '尚未读取关联服务器概要' }],
            items: [{ xtype: 'container', height: 308, layout: { type: 'hbox', align: 'stretch' }, items: [current,
                { xtype: 'panel', title: '备注', flex: 1, minWidth: 0, height: 300, margin: '0 5 0 0', bodyPadding: 10, scrollable: true,
                    tools: [{ itemId: 'pve-komodo-guest-description-edit', glyph: 'xf044@FontAwesome', tooltip: '编辑描述', disabled: true,
                        callback: function (view) { editGuestDescription(summaryOwner(view)); } }],
                    items: [{ xtype: 'component', itemId: 'pve-komodo-guest-description', html: '-', style: { 'overflow-wrap': 'anywhere' } }] },
                recent,
            ] }, { xtype: 'container', height: 308, layout: { type: 'hbox', align: 'stretch' }, items: [
                guestSummaryGrid('containers', '容器', ['id', 'name', 'state', 'cpu_percent', 'memory_percent', 'memory_usage', 'network_io', 'block_io', 'pids'], [
                { text: '名称', dataIndex: 'name', flex: 1, minWidth: 160, renderer: text }, { text: '状态', dataIndex: 'state', width: 90, renderer: stateText },
                { text: 'CPU', dataIndex: 'cpu_percent', width: 100, renderer: text },
                { text: '内存', dataIndex: 'memory_usage', width: 210, renderer: function (value, meta, record) { return text(record.get('memory_percent')) + '（' + text(value) + '）'; } },
                { text: '网络 I/O', dataIndex: 'network_io', width: 170, renderer: text },
                { text: '磁盘 I/O', dataIndex: 'block_io', width: 170, renderer: text }, { text: 'PIDs', dataIndex: 'pids', width: 70, renderer: text },
            ], 300), processes] }, { xtype: 'panel', title: '历史', border: false,
                bbar: [{ xtype: 'tbtext', itemId: 'pve-komodo-guest-history-status', text: '尚未读取历史采样' }],
                items: rows,
            }], listeners: { activate: function (panel) {
                panel._pveKomodoSummaryActive = true; activeGuestSummary = panel; loadGuestSummary(panel);
            }, deactivate: leave, beforedestroy: leave },
        };
    }

    const guestOptionDefinitions = [
        ['enabled', '已启用', 'boolean', '是否连接此服务器并在离线时告警。禁用后，关联资源的状态也会变为未知。'],
        ['auto_rotate_keys', '自动轮换密钥', 'boolean', '执行 RotateAllServerKeys 时也轮换此服务器的密钥。'],
        ['address', 'Periphery 地址', 'string', 'Core 主动连接 Periphery 时使用的地址；留空表示等待 Periphery 主动连接。'],
        ['insecure_tls', '跳过 TLS 证书验证', 'boolean', '仅用于 Core 主动连接 Periphery。'],
        ['external_address', '外部地址', 'string', '可选，容器链接使用的服务器地址。'],
        ['region', '区域', 'string', '可选，用于服务器分组显示。'],
        ['ignore_mounts', '忽略磁盘', 'list', '每行一个挂载点，从服务器统计中排除这些磁盘。'],
        ['stats_monitoring', '系统统计监控', 'boolean', '是否保存 CPU、内存和磁盘使用率的历史数据。'],
        ['auto_prune', '自动清理镜像', 'boolean', '每天 UTC 00:00 清理未使用的镜像。'],
        ['send_unreachable_alerts', '发送无法连接告警', 'boolean', '无法连接 Periphery 代理时发送告警。'],
        ['send_version_mismatch_alerts', '发送版本不匹配告警', 'boolean', 'Periphery 与 Core 的版本不同时发送告警。'],
        ['send_cpu_alerts', '发送 CPU 告警', 'boolean', 'CPU 使用率超过阈值时发送告警。'],
        ['cpu_warning', 'CPU 警告阈值', 'number', 'CPU 使用率触发警告的百分比。'],
        ['cpu_critical', 'CPU 严重阈值', 'number', 'CPU 使用率触发严重告警的百分比。'],
        ['send_mem_alerts', '发送内存告警', 'boolean', '内存使用率超过阈值时发送告警。'],
        ['mem_warning', '内存警告阈值', 'number', '内存使用率触发警告的百分比。'],
        ['mem_critical', '内存严重阈值', 'number', '内存使用率触发严重告警的百分比。'],
        ['send_disk_alerts', '发送磁盘告警', 'boolean', '任一挂载磁盘的使用率超过阈值时发送告警。'],
        ['disk_warning', '磁盘警告阈值', 'number', '磁盘使用率触发警告的百分比。'],
        ['disk_critical', '磁盘严重阈值', 'number', '磁盘使用率触发严重告警的百分比。'],
        ['maintenance_windows', '维护窗口', 'windows', '维护窗口生效时暂停此服务器告警；空时区使用 Core 时区。'],
        ['public_key', 'Periphery 公钥', 'public_key', '填写公钥时，需要将对应私钥配置在 Periphery。主动连接 Core 时必须配置公钥或 Core 默认公钥。'],
    ];
    const guestOptionSections = [
        ['general', '常规', [
            ['enabled', '已启用', '是否连接此主机并在离线时告警。禁用后，关联资源的状态也将变为未知。', ['enabled']],
            ['auth', '认证', '若填写 Periphery 公钥，需将对应私钥设置为 Periphery 的 private_key。Periphery 主动连接 Core 时，必须填写此项或在 Core 中配置 periphery_public_key；执行 RotateAllServerKeys 时也轮换此服务器的密钥。', ['public_key', 'auto_rotate_keys']],
            ['address', '地址', 'Core 主动连接 Periphery 时，指定网络中的 Periphery 地址；外部地址用于容器链接，区域用于服务器分组显示。', ['address', 'insecure_tls', 'external_address', 'region']],
            ['disk', '磁盘', '在此过滤不希望出现在服务器统计中的磁盘挂载点。', ['ignore_mounts']],
            ['monitoring', '监控', '是否保存 CPU、内存和磁盘使用率的历史数据。', ['stats_monitoring']],
            ['cleanup', '清理', '是否每天 UTC 00:00 清理未使用的镜像。', ['auto_prune']],
        ]],
        ['alerts', '告警', [
            ['unreachable', '无法连接', '无法连接 Periphery 代理时发送告警。', ['send_unreachable_alerts']],
            ['version', '版本', 'Periphery 与 Core 版本不同时发送告警。', ['send_version_mismatch_alerts']],
            ['cpu', 'CPU', 'CPU 使用率超过设定阈值时发送告警；警告阈值和严重阈值分别对应警告和严重告警。', ['send_cpu_alerts', 'cpu_warning', 'cpu_critical']],
            ['memory', '内存', '内存使用率超过设定阈值时发送告警；警告阈值和严重阈值分别对应警告和严重告警。', ['send_mem_alerts', 'mem_warning', 'mem_critical']],
            ['alert-disk', '磁盘', '任一已挂载磁盘的使用率超过设定阈值时发送告警；警告阈值和严重阈值分别对应警告和严重告警。', ['send_disk_alerts', 'disk_warning', 'disk_critical']],
            ['maintenance', '维护', '配置维护窗口，在计划维护期间暂停告警。维护窗口生效时，不发送此服务器的告警。', ['maintenance_windows']],
        ]],
    ];
    let activeGuestOptions = null;
    let guestOptionsWrite = null;

    function copyOption(value) { return JSON.parse(JSON.stringify(value)); }
    function optionBusy() {
        return !!(serverCreateWrite && serverCreateWrite.pending || guestInventoryWrite && guestInventoryWrite.pending || guestOptionsWrite && guestOptionsWrite.pending || guestWriteState && guestWriteState.pending || actionState && actionState.pending ||
            guestSummaryWrite && guestSummaryWrite.pending || stackWriteState && stackWriteState.pending);
    }
    function optionOwner(component) { return component.up('[pveKomodoGuestOptions]'); }
    function optionStatus(panel, message) {
        const status = panel.down('#pve-komodo-guest-options-status');
        if (alive(status)) status.setText(text(message));
    }
    function optionControls(panel) {
        if (!alive(panel)) return;
        const data = panel._pveKomodoOptionsData;
        const blocked = optionBusy() || panel._pveKomodoOptionsLoading || panel._pveKomodoOptionsConfirmation;
        const grid = panel.down('#pve-komodo-guest-options-grid');
        const row = grid.getSelectionModel().getSelection()[0];
        panel.down('#pve-komodo-guest-options-refresh').setDisabled(!!blocked);
        panel.down('#pve-komodo-guest-options-edit').setDisabled(!!blocked || !data || !data.can_write || !row || !row.get('available'));
        panel.down('#pve-komodo-guest-options-rotate').setDisabled(!!blocked || !data || !data.can_write || !data.can_rotate);
    }
    function optionValue(value, kind) {
        if (kind === 'boolean') return value === true ? '是' : value === false ? '否' : '-';
        if (kind === 'list') return Array.isArray(value) ? value.length ? value.map(text).join('、') : '无' : '-';
        if (kind === 'windows') return Array.isArray(value) ? value.length + ' 个维护窗口' : '-';
        return text(value);
    }
    function optionRows(data) {
        const rows = [];
        guestOptionSections.forEach(function (group) {
            rows.push({ key: 'heading-guest-' + group[0], label: group[1], kind: 'heading', heading: 'major', available: false });
            group[2].forEach(function (section) {
                rows.push({ key: 'heading-guest-' + section[0], label: section[1], kind: 'heading', heading: 'minor',
                    description: section[2], available: false });
                section[3].forEach(function (field) {
                    if (field === 'insecure_tls' && !(typeof data.config.address === 'string' &&
                        data.config.address !== '' && !data.config.address.startsWith('ws://'))) return;
                    const definition = guestOptionDefinitions.find(function (candidate) { return candidate[0] === field; });
                    const key = definition[0], kind = definition[2];
                    const value = kind === 'public_key' ? data.public_key : data.config[key];
                    const available = kind === 'boolean' ? typeof value === 'boolean' : kind === 'number' ? numeric(value) :
                        kind === 'list' ? Array.isArray(value) && value.every(function (item) { return typeof item === 'string'; }) :
                        kind === 'windows' ? Array.isArray(value) : typeof value === 'string';
                    rows.push({ key: key, label: definition[1], kind: kind, description: definition[3], value: value, available: available });
                });
            });
        });
        return rows;
    }
    function closeOptionEditor(panel) {
        const editor = panel._pveKomodoOptionsEditor;
        panel._pveKomodoOptionsEditor = null;
        if (alive(editor)) editor.destroy();
    }
    function cancelOptions(panel) {
        panel._pveKomodoOptionsGeneration = (panel._pveKomodoOptionsGeneration || 0) + 1;
        const request = panel._pveKomodoOptionsRequest;
        panel._pveKomodoOptionsRequest = null;
        panel._pveKomodoOptionsLoading = false;
        if (request) Ext.Ajax.abort(request);
        const confirmation = panel._pveKomodoOptionsConfirmation;
        panel._pveKomodoOptionsConfirmation = null;
        if (confirmation && alive(confirmation.dialog)) confirmation.dialog.destroy();
        closeOptionEditor(panel);
    }
    function optionsError(response, fallback) {
        let code;
        try { code = JSON.parse(response.responseText).detail; } catch (error) { /* Fixed messages only. */ }
        if (response && response.status === 401) return 'PVE 未登录或会话已过期';
        if (response && response.status === 403) return code === 'invalid_csrf_token' ?
            '会话校验已更新，本次未提交，请先刷新选项页面再操作' : '无访问权限';
        if (code === 'not_configured') return 'Komodo 未配置凭据';
        if (code === 'guest_not_linked') return '此 VM/CT 尚未关联 Komodo 服务器，请在数据中心 Docker 页面关联';
        if (code === 'server_link_requires_guest') return '此服务器已关联 CT 或 VM，请从容器视图重新打开对应的 PVE 对象';
        if (code === 'action_already_requested' || code === 'idempotency_conflict') return '该请求已提交或结果尚未确认，请先到 Komodo 的操作记录核对，不要重复提交';
        if (response && response.status === 404) return '此 VM/CT 尚未关联可用的 Komodo 服务器，请在数据中心 Docker 页面关联';
        if (response && response.status === 409) return '关联或服务器配置已变化，请刷新后重新确认';
        if (response && response.status === 503) return 'PVE 会话验证或集成服务暂不可用';
        return fallback;
    }
    function loadOptions(panel) {
        if (!alive(panel) || !panel._pveKomodoOptionsActive) return;
        cancelOptions(panel);
        panel._pveKomodoOptionsData = null;
        panel._pveKomodoOptionsLoading = true;
        const generation = panel._pveKomodoOptionsGeneration;
        const grid = panel.down('#pve-komodo-guest-options-grid');
        grid.getSelectionModel().deselectAll();
        grid.getStore().loadData([]);
        optionStatus(panel, '正在读取关联服务器配置…');
        optionControls(panel);
        let completed = false;
        function complete(options, success, response) {
            completed = true;
            if (!alive(panel) || !panel._pveKomodoOptionsActive || generation !== panel._pveKomodoOptionsGeneration) return;
            panel._pveKomodoOptionsRequest = null;
            panel._pveKomodoOptionsLoading = false;
            let message = optionsError(response, '服务器配置读取失败，请刷新后重试');
            try {
                const data = response && JSON.parse(response.responseText);
                if (!success || !data || !serverReplyMatches(panel, data) || typeof data.server_id !== 'string' || !/^[0-9a-f]{24}$/i.test(data.server_id) ||
                    typeof data.version !== 'string' || !/^[0-9a-f]{64}$/i.test(data.version) ||
                    !Number.isSafeInteger(data.binding_revision) || data.binding_revision < 0 ||
                    !data.config || typeof data.config !== 'object' || Array.isArray(data.config) ||
                    typeof data.can_write !== 'boolean' || typeof data.can_rotate !== 'boolean' ||
                    typeof data.csrf_token !== 'string' || !data.csrf_token || data.csrf_token.length > 4096 || /[\r\n]/.test(data.csrf_token)) {
                    throw new Error('invalid-options');
                }
                panel._pveKomodoOptionsData = copyOption(data);
                grid.getStore().loadData(optionRows(data));
                message = (panel._pveKomodoOptionsNotice ? panel._pveKomodoOptionsNotice + '；' : '') +
                    (panel.pveKomodoDirectServer ? '服务器：' : '关联服务器：') + (typeof data.server_name === 'string' ? data.server_name : data.server_id) +
                    (data.can_write ? '' : '（只读）');
                panel._pveKomodoOptionsNotice = null;
            } catch (error) { /* Keep unreadable data distinct from an unconfigured value. */ }
            optionStatus(panel, message);
            optionControls(panel);
        }
        try {
            const request = Ext.Ajax.request({ url: panel.pveKomodoOptionsPath, method: 'GET', disableCaching: false, timeout: 60000, callback: complete });
            if (!completed) panel._pveKomodoOptionsRequest = request;
        } catch (error) { complete(null, false, null); }
    }
    function submitOptions(panel, payload) {
        const path = panel.pveKomodoOptionsPath;
        const state = { pending: true, path: path };
        guestOptionsWrite = state;
        closeOptionEditor(panel);
        optionStatus(panel, '正在提交修改，请勿重复操作…');
        optionControls(panel);
        if (alive(activeDashboard)) updateControls(activeDashboard);
        let completed = false;
        function complete(options, success, response) {
            if (completed) return;
            completed = true;
            state.pending = false;
            let accepted = false;
            let message = '保存结果尚未确认，请先到 Komodo 的操作记录核对，不要重复提交';
            try {
                const result = response && JSON.parse(response.responseText);
                if (success && result && result.status === 'completed' && result.ok === true) {
                    accepted = true;
                    message = payload.action === 'rotate_keys' ? '密钥轮换已完成' : '配置已保存';
                } else if (result && result.status === 'failed') message = '修改失败，请刷新配置并核对 Komodo 的操作记录';
                else if (response && [401, 403, 404, 409, 422].includes(response.status)) message = optionsError(response,
                    '配置未通过校验，本次未保存，请刷新后核对');
            } catch (error) { /* No raw upstream errors or automatic retries. */ }
            const current = activeGuestOptions;
            if (alive(current) && current._pveKomodoOptionsActive && current.pveKomodoOptionsPath === path) {
                current._pveKomodoOptionsData = null;
                if (accepted) { current._pveKomodoOptionsNotice = message; loadOptions(current); }
                else { optionStatus(current, message); optionControls(current); }
            }
            if (alive(current)) optionControls(current);
            if (alive(activeGuestSummary)) summaryControls(activeGuestSummary);
            if (alive(activeStackPage)) stackControls(activeStackPage);
            if (alive(activeDashboard)) updateControls(activeDashboard);
        }
        try {
            Ext.Ajax.request({ url: path, method: 'POST', timeout: 120000,
                headers: { 'X-PVE-Komodo-CSRF': panel._pveKomodoOptionsData.csrf_token }, jsonData: payload, callback: complete });
        } catch (error) { complete(null, false, null); }
    }
    function confirmOptions(panel, action, values) {
        const data = panel._pveKomodoOptionsData;
        if (!alive(panel) || !panel._pveKomodoOptionsActive || !data || !data.can_write || optionBusy() ||
            panel._pveKomodoOptionsConfirmation || action === 'rotate_keys' && !data.can_rotate) return;
        const requestId = uuid();
        if (!requestId) { optionStatus(panel, '浏览器无法生成安全请求标识，本次未提交'); return; }
        const payload = Object.assign({ action: action, server_id: data.server_id, binding_revision: data.binding_revision,
            base_version: data.version, request_id: requestId }, copyOption(values || {}));
        const generation = panel._pveKomodoOptionsGeneration;
        const dialog = Ext.create('Ext.window.MessageBox');
        const state = { dialog: dialog };
        panel._pveKomodoOptionsConfirmation = state;
        const effect = action === 'rotate_keys' ? 'Periphery 将在本机生成并保存新的私钥，Core 同步新的公钥；不会在此页面展示私钥。' :
            action === 'public_key' ? '更新 Core 保存的 Periphery 公钥；若与 Periphery 的私钥不匹配，连接将失败。' : '将修改所编辑的服务器配置项。';
        dialog.show({ title: action === 'rotate_keys' ? '确认轮换密钥' : '确认保存',
            message: text('服务器：' + (data.server_name || data.server_id) + '。' + effect), buttons: Ext.Msg.YESNO, icon: Ext.Msg.WARNING,
            fn: function (answer) {
                if (panel._pveKomodoOptionsConfirmation !== state) return;
                panel._pveKomodoOptionsConfirmation = null;
                if (alive(dialog)) dialog.destroy();
                if (!alive(panel) || !panel._pveKomodoOptionsActive || generation !== panel._pveKomodoOptionsGeneration ||
                    panel._pveKomodoOptionsData !== data || optionBusy()) return;
                if (answer === 'yes') submitOptions(panel, payload);
                else optionControls(panel);
            },
        });
        optionControls(panel);
    }
    function optionEditor(panel, title, items, read, reset, save) {
        closeOptionEditor(panel);
        let original;
        function dirty() { try { return JSON.stringify(read(editor)) !== original; } catch (error) { return true; } }
        function requestClose() {
            if (!dirty()) { editor.destroy(); return; }
            if (editor._discardDialog) return;
            const dialog = Ext.create('Ext.window.MessageBox'); editor._discardDialog = dialog;
            dialog.show({ title: '放弃修改', message: '有尚未保存的修改，确定放弃？', buttons: Ext.Msg.YESNO, icon: Ext.Msg.WARNING,
                fn: function (answer) {
                    editor._discardDialog = null;
                    if (alive(dialog)) dialog.destroy();
                    if (answer === 'yes' && alive(editor)) editor.destroy();
                } });
        }
        const editor = Ext.create('Ext.window.Window', {
            title: title, width: 650, modal: true, closeAction: 'destroy', bodyPadding: 16, layout: 'anchor',
            defaults: { anchor: '100%', labelWidth: 180 }, items: items,
            buttons: [{ text: '保存', itemId: 'pve-komodo-option-save', handler: function () {
                try {
                    const value = read(editor);
                    if (JSON.stringify(value) === original) { optionStatus(panel, '配置未改变，本次未提交'); return; }
                    save(value, editor);
                } catch (error) { editor.down('#pve-komodo-option-error').update('输入值无效，请检查各字段'); }
            } }, { text: '重置', itemId: 'pve-komodo-option-reset', handler: function () {
                reset(editor); editor.down('#pve-komodo-option-error').update('');
            } }, { text: '取消', itemId: 'pve-komodo-option-cancel', handler: requestClose }],
            listeners: { beforeclose: function () { requestClose(); return false; }, beforedestroy: function () {
                if (alive(editor._discardDialog)) editor._discardDialog.destroy();
                if (alive(editor._maintenanceEditor)) editor._maintenanceEditor.destroy();
                if (panel._pveKomodoOptionsEditor === editor) panel._pveKomodoOptionsEditor = null;
            } },
        });
        panel._pveKomodoOptionsEditor = editor;
        reset(editor);
        original = JSON.stringify(read(editor));
        editor.show();
        return editor;
    }
    function editMaintenance(panel, row) {
        const initial = copyOption(row.get('value'));
        let draft = copyOption(initial);
        let editor;
        function refresh() { editor.down('#pve-komodo-maintenance-grid').getStore().loadData(copyOption(draft)); }
        function editWindow(index) {
            if (alive(editor._maintenanceEditor)) editor._maintenanceEditor.destroy();
            const value = index === -1 ? { name: '', description: '', schedule_type: 'Daily', day_of_week: '', date: '',
                hour: 5, minute: 0, timezone: '', duration_minutes: 60, enabled: true } : copyOption(draft[index]);
            if (value.day_of_week === null || value.day_of_week === undefined) value.day_of_week = '';
            const fields = [
                ['name', '名称', 'textfield'], ['description', '描述', 'textfield'],
                ['schedule_type', '计划类型', 'combobox', ['Daily', 'Weekly', 'OneTime']],
                ['day_of_week', '星期', 'combobox', ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']],
                ['date', '日期（YYYY-MM-DD）', 'textfield'], ['hour', '小时（0–23）', 'numberfield'],
                ['minute', '分钟（0–59）', 'numberfield'], ['timezone', '时区（空值使用 Core 时区）', 'textfield'],
                ['duration_minutes', '持续时间（分钟）', 'numberfield'], ['enabled', '已启用', 'checkbox'],
            ];
            const inputs = fields.map(function (field) {
                const config = { xtype: field[2], itemId: 'pve-komodo-maintenance-' + field[0], fieldLabel: field[1],
                    value: value[field[0]], checked: value[field[0]] === true };
                if (field[3]) { config.editable = false; config.queryMode = 'local'; config.store = field[3]; }
                if (field[2] === 'numberfield') { config.allowDecimals = false; config.allowBlank = false; }
                return config;
            });
            function childValues() {
                const updated = {};
                fields.forEach(function (field) { updated[field[0]] = child.down('#pve-komodo-maintenance-' + field[0]).getValue(); });
                if (updated.day_of_week === null || updated.day_of_week === undefined) updated.day_of_week = '';
                return updated;
            }
            function closeChild() {
                if (JSON.stringify(childValues()) === JSON.stringify(value)) { child.destroy(); return; }
                if (alive(child._discardDialog)) return;
                const dialog = Ext.create('Ext.window.MessageBox'); child._discardDialog = dialog;
                dialog.show({ title: '放弃修改', message: '有尚未保存的维护窗口修改，确定放弃？', buttons: Ext.Msg.YESNO, icon: Ext.Msg.WARNING,
                    fn: function (answer) {
                        child._discardDialog = null;
                        if (alive(dialog)) dialog.destroy();
                        if (answer === 'yes' && alive(child)) child.destroy();
                    } });
            }
            const child = Ext.create('Ext.window.Window', {
                title: index === -1 ? '添加维护窗口' : '编辑维护窗口', width: 600, modal: true, closeAction: 'destroy', bodyPadding: 16,
                layout: 'anchor', defaults: { anchor: '100%', labelWidth: 220 }, items: inputs.concat([
                    { xtype: 'component', itemId: 'pve-komodo-maintenance-error', html: '' },
                ]),
                buttons: [{ text: '确定', itemId: 'pve-komodo-maintenance-apply', handler: function () {
                    const updated = childValues();
                    if (typeof updated.name !== 'string' || !updated.name.trim() || typeof updated.description !== 'string' ||
                        !['Daily', 'Weekly', 'OneTime'].includes(updated.schedule_type) || typeof updated.enabled !== 'boolean' ||
                        !Number.isInteger(updated.hour) || updated.hour < 0 || updated.hour > 23 ||
                        !Number.isInteger(updated.minute) || updated.minute < 0 || updated.minute > 59 ||
                        !Number.isInteger(updated.duration_minutes) || updated.duration_minutes < 1 || updated.duration_minutes > 4294967295 ||
                        typeof updated.timezone !== 'string' || typeof updated.date !== 'string' || typeof updated.day_of_week !== 'string' ||
                        updated.schedule_type === 'Weekly' && !fields[3][3].includes(updated.day_of_week) ||
                        updated.schedule_type === 'OneTime' && !/^\d{4}-\d{2}-\d{2}$/.test(updated.date)) {
                        child.down('#pve-komodo-maintenance-error').update('输入值无效，请检查名称、计划与时间'); return;
                    }
                    if (index === -1) draft.push(updated); else draft[index] = updated;
                    refresh(); child.destroy();
                } }, { text: '取消', handler: closeChild }],
                listeners: { beforeclose: function () { closeChild(); return false; }, beforedestroy: function () {
                    if (alive(child._discardDialog)) child._discardDialog.destroy();
                } },
            });
            editor._maintenanceEditor = child; child.show();
        }
        editor = optionEditor(panel, '维护窗口', [{ xtype: 'gridpanel', itemId: 'pve-komodo-maintenance-grid', height: 300,
            store: { autoDestroy: true, fields: ['name', 'description', 'schedule_type', 'day_of_week', 'date', 'hour', 'minute', 'timezone', 'duration_minutes', 'enabled'], data: [] },
            tbar: [{ text: '添加', handler: function () { editWindow(-1); } }, { text: '编辑', handler: function (button) {
                const grid = button.up('gridpanel'), selected = grid.getSelectionModel().getSelection()[0];
                if (selected) editWindow(grid.getStore().indexOf(selected));
            } }, { text: '删除', handler: function (button) {
                const grid = button.up('gridpanel'), selected = grid.getSelectionModel().getSelection()[0];
                if (selected) { draft.splice(grid.getStore().indexOf(selected), 1); refresh(); }
            } }],
            columns: [{ text: '名称', dataIndex: 'name', flex: 1, sortable: false, renderer: text },
                { text: '计划', dataIndex: 'schedule_type', width: 90, sortable: false, renderer: text },
                { text: '时区', dataIndex: 'timezone', width: 150, sortable: false, renderer: function (value) { return value === '' ? 'Core 时区' : text(value); } },
                { text: '分钟', dataIndex: 'duration_minutes', width: 70, sortable: false, renderer: text },
                { text: '启用', dataIndex: 'enabled', width: 65, sortable: false, renderer: function (value) { return optionValue(value, 'boolean'); } }],
        }, { xtype: 'component', html: '<p>维护窗口生效时暂停告警。时间按窗口时区解释；空值使用 Core 时区。</p>' },
        { xtype: 'component', itemId: 'pve-komodo-option-error', html: '' }], function () { return draft; },
        function (window) { draft = copyOption(initial); window.down('#pve-komodo-maintenance-grid').getStore().loadData(copyOption(draft)); },
        function (value) { confirmOptions(panel, 'save', { config: { maintenance_windows: copyOption(value) } }); });
    }
    function editOption(panel, record) {
        const data = panel._pveKomodoOptionsData;
        if (!alive(panel) || !panel._pveKomodoOptionsActive || !data || !data.can_write || optionBusy() || !record || !record.get('available')) return;
        const key = record.get('key'), kind = record.get('kind'), initial = copyOption(record.get('value'));
        if (kind === 'windows') { editMaintenance(panel, record); return; }
        const input = { xtype: kind === 'boolean' ? 'checkbox' :
            kind === 'list' || kind === 'public_key' ? 'textareafield' : 'textfield',
            itemId: 'pve-komodo-option-value', fieldLabel: record.get('label'), allowBlank: kind !== 'number' };
        if (input.xtype === 'textareafield') input.height = 150;
        optionEditor(panel, record.get('label'), [{ xtype: 'component', html: '<p>' + text(record.get('description')) + '</p>' }, input,
            { xtype: 'component', itemId: 'pve-komodo-option-error', html: '' }], function (window) {
            const value = window.down('#pve-komodo-option-value').getValue();
            if (kind === 'number') {
                // Ext NumberField rounds to two decimals even before editing.
                // Parse the complete text instead, preserving stored thresholds.
                if (typeof value !== 'string' || !/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(value.trim()) ||
                    !numeric(Number(value))) throw new Error('invalid-number');
                return Number(value);
            }
            if (kind === 'boolean' && typeof value !== 'boolean' || kind !== 'boolean' && typeof value !== 'string') throw new Error('invalid-value');
            return kind === 'list' ? value.split(/\r?\n/).filter(function (line) { return line !== ''; }) : value;
        }, function (window) {
            window.down('#pve-komodo-option-value').setValue(kind === 'list' ? initial.join('\n') : kind === 'number' ? String(initial) : initial);
        }, function (value) {
            confirmOptions(panel, kind === 'public_key' ? 'public_key' : 'save',
                kind === 'public_key' ? { public_key: value } : { config: { [key]: value } });
        });
    }
    function guestOptionsConfig(node) {
        return { xtype: 'panel', itemId: guestItemId + '-options', title: '选项', iconCls: 'fa fa-gear', border: false,
            groups: [guestItemId], pveKomodoGuestOptions: true,
            pveKomodoOptionsPath: serverPageBase(node) + '/options', layout: 'fit',
            tbar: [{ text: '编辑', itemId: 'pve-komodo-guest-options-edit', disabled: true, handler: function (button) {
                const panel = optionOwner(button), grid = panel.down('#pve-komodo-guest-options-grid');
                editOption(panel, grid.getSelectionModel().getSelection()[0]);
            } }, { text: '轮换密钥', itemId: 'pve-komodo-guest-options-rotate', disabled: true,
                handler: function (button) { confirmOptions(optionOwner(button), 'rotate_keys'); } },
            { text: '刷新', itemId: 'pve-komodo-guest-options-refresh', handler: function (button) { loadOptions(optionOwner(button)); } }],
            bbar: [{ xtype: 'tbtext', itemId: 'pve-komodo-guest-options-status', text: '尚未读取关联服务器配置' }],
            items: [{ xtype: 'gridpanel', itemId: 'pve-komodo-guest-options-grid', cls: 'pve-komodo-stack-config-grid pve-komodo-guest-options-grid',
                store: { autoDestroy: true, fields: ['key', 'label', 'kind', 'heading', 'description', 'value', 'available'], data: [] },
                selModel: { listeners: { beforeselect: function (model, record) {
                    if (record.get('kind') !== 'heading') return true;
                    model.deselectAll(); return false;
                } } },
                columns: [{ text: '选项', dataIndex: 'label', width: 260, sortable: false, renderer: text },
                    { text: '值', dataIndex: 'value', flex: 1, sortable: false, renderer: stackFieldText }],
                viewConfig: { stripeRows: true, enableTextSelection: true, emptyText: '没有可显示的服务器配置', deferEmptyText: false,
                    variableRowHeight: true, getRowClass: function (record) {
                        return record.get('kind') === 'heading' ? 'pve-komodo-stack-heading-' + record.get('heading') : '';
                    } },
                listeners: { selectionchange: function () { optionControls(optionOwner(this)); },
                    itemdblclick: function (view, record) { editOption(optionOwner(this), record); } },
            }],
            listeners: { activate: function (panel) {
                panel._pveKomodoOptionsActive = true; activeGuestOptions = panel; loadOptions(panel);
            }, deactivate: function (panel) {
                panel._pveKomodoOptionsActive = false; if (activeGuestOptions === panel) activeGuestOptions = null; cancelOptions(panel);
            }, beforedestroy: function (panel) {
                panel._pveKomodoOptionsActive = false; if (activeGuestOptions === panel) activeGuestOptions = null; cancelOptions(panel);
            } },
        };
    }

    function gridConfig(section, fields, columns) {
        return {
            xtype: 'gridpanel',
            itemId: 'pve-komodo-' + section + '-grid',
            title: sectionTitles[section],
            collapsible: true,
            titleCollapse: true,
            flex: 1,
            minHeight: 180,
            margin: '0 0 8 0',
            store: {
                autoDestroy: true,
                fields: fields,
                data: [],
                sorters: [{ property: 'name', direction: 'ASC' }],
            },
            columns: columns,
            selModel: { selType: 'checkboxmodel', mode: 'MULTI', checkOnly: true },
            tbar: {
                overflowHandler: 'scroller',
                items: (section === 'containers' ? [] : [{
                    text: '新建' + (section === 'servers' ? '服务器' : section === 'stacks' ? '应用栈' : '流程'),
                    itemId: section === 'servers' ? 'pve-komodo-servers-new' : undefined,
                    disabled: true, tooltip: section === 'servers' ? '新建 Komodo 服务器，可选择关联 CT 或 VM' : '暂未实现创建功能',
                    handler: section === 'servers' ? function (button) { const owner = guestOwner(ownerPanel(button)); if (owner) openServerCreate(owner.workspace); } : undefined,
                }]).concat(actions[section].map(function (action) {
                    return {
                        itemId: 'pve-komodo-action-' + action[0], text: action[1], disabled: true,
                        handler: function (button) { confirmAction(button, section, action); },
                    };
                })).concat(section === 'servers' ? ['-', {
                    text: '关联 VM/CT', itemId: 'pve-komodo-guest-bind', disabled: true,
                    handler: function (button) { guestButton(button, 'bind'); },
                }, {
                    text: '解除关联', itemId: 'pve-komodo-guest-unlink', disabled: true,
                    handler: function (button) { guestButton(button, 'unlink'); },
                }, {
                    text: '打开 PVE 对象', itemId: 'pve-komodo-guest-open', disabled: true,
                    handler: function (button) { guestButton(button, 'open'); },
                }] : []),
            },
            listeners: {
                cellclick: function (view, cell, index, record) {
                    const column = view.getHeaderCt().getHeaderAtIndex(index);
                    const panel = ownerPanel(this);
                    if (column && column.dataIndex === 'pve_guest' && alive(panel) && panel._pveKomodoGuestReady) openGuest(panel, record.get('pve_guest'));
                },
                selectionchange: function () {
                    const panel = ownerPanel(this);
                    if (alive(panel)) {
                        panel._pveKomodoSelectionGeneration = (panel._pveKomodoSelectionGeneration || 0) + 1;
                        updateControls(panel);
                    }
                },
            },
            viewConfig: {
                stripeRows: true,
                enableTextSelection: true,
                emptyText: '暂无数据',
                deferEmptyText: false,
            },
        };
    }

    function applyFilters(panel, force) {
        if (!alive(panel) || !panel._pveKomodoRows || actionBusy(panel) && !force) {
            return;
        }
        const search = String(panel.down('#pve-komodo-search').getValue() || '').toLowerCase().trim();
        const tag = panel.down('#pve-komodo-tag').getValue() || '';
        const templateField = panel.down('#pve-komodo-exclude-templates');
        const excludeTemplates = templateField && templateField.getValue();
        const serverField = panel.down('#pve-komodo-server');
        const server = serverField && serverField.getValue();
        panelSections(panel).forEach(function (section) {
            const grid = panel.down('#pve-komodo-' + section + '-grid');
            if (!alive(grid)) {
                return;
            }
            grid.getSelectionModel().deselectAll();
            const rows = panel._pveKomodoRows[section] || [];
            const filtered = rows.filter(function (row) {
                return (!excludeTemplates || row.template !== true) &&
                    (!server || row.server_id === server) &&
                    (!tag || row.tags.indexOf(tag) !== -1) && (!search || row._search.indexOf(search) !== -1);
            });
            grid.getStore().loadData(filtered);
            grid.setTitle(sectionTitles[section] + '（' + filtered.length + ' / ' + rows.length + '）');
        });
        applyGuestLinks(panel);
        updateControls(panel);
    }

    function normalizeRows(rows) {
        return rows.map(function (source) {
            const row = Object.assign({}, source);
            row.tags = Array.isArray(row.tags) ? row.tags.filter(function (tag) {
                return typeof tag === 'string';
            }) : [];
            row._search = ['name', 'state', 'source', 'server_name', 'image'].map(function (field) {
                return typeof row[field] === 'string' ? row[field] : '';
            }).concat(row.tags, row.networks || [], row.ports || [], row.volumes || [], typeof row.state === 'string' &&
                Object.prototype.hasOwnProperty.call(stateLabels, row.state) ? stateLabels[row.state] : '')
                .join('\n').toLowerCase();
            ['cpu_percent', 'memory_percent', 'disk_percent'].forEach(function (field) {
                const value = row[field];
                const known = numeric(value) && value >= 0;
                row[field + '_value'] = known ? Math.min(value / 100, 1) : null;
                row[field + '_text'] = known ? value.toFixed(1) + '%' : '-';
            });
            ['memory', 'disk'].forEach(function (resource) {
                const used = row[resource + '_used_bytes'];
                const total = row[resource + '_total_bytes'];
                const size = function (value) {
                    return numeric(value) && value >= 0 ? window.Proxmox.Utils.format_size(value) : '-';
                };
                if (numeric(used) && used >= 0 || numeric(total) && total >= 0) {
                    row[resource + '_percent_text'] += ' (' + size(used) + ' / ' + size(total) + ')';
                }
            });
            return row;
        });
    }

    function statusText(panel, message) {
        const status = panel.down('#pve-komodo-dashboard-status');
        if (alive(status)) {
            status.setText(text(message));
        }
    }

    function clearRows(panel) {
        panel._pveKomodoCsrf = null;
        panel._pveKomodoRows = {};
        panelSections(panel).forEach(function (section) { panel._pveKomodoRows[section] = []; });
        applyFilters(panel);
    }

    function cancelDashboard(panel) {
        cancelGuestLinks(panel);
        // Invalidate callbacks before abort(): Ext.Ajax may call them from abort.
        panel._pveKomodoGeneration = (panel._pveKomodoGeneration || 0) + 1;
        const request = panel._pveKomodoRequest;
        panel._pveKomodoRequest = null;
        panel._pveKomodoLoading = false;
        const confirmation = panel._pveKomodoConfirmation;
        panel._pveKomodoConfirmation = null;
        if (confirmation) {
            confirmation.answered = true;
            if (alive(confirmation.dialog)) confirmation.dialog.destroy();
        }
        if (request && Ext.Ajax && typeof Ext.Ajax.abort === 'function') {
            Ext.Ajax.abort(request);
        }
    }

    function loadDashboard(panel) {
        if (!alive(panel) || !panel._pveKomodoActive || panel._pveKomodoLoading ||
            actionBusy(panel) && !(guestWriteState && guestWriteState.pending && !panel._pveKomodoRows)) {
            return;
        }
        const refresh = panel.down('#pve-komodo-refresh');
        if (!alive(refresh)) {
            return;
        }
        panel._pveKomodoLoading = true;
        if (panel.pveKomodoPage === 'dashboard') loadGuestLinks(panel);
        const generation = (panel._pveKomodoGeneration || 0) + 1;
        panel._pveKomodoGeneration = generation;
        refresh.setDisabled(true);
        clearRows(panel);
        statusText(panel, '正在加载…' + (actionState ? '；' + actionState.message : ''));
        let completed = false;

        function complete(options, success, response) {
            completed = true;
            if (!alive(panel) || !panel._pveKomodoActive || panel._pveKomodoGeneration !== generation) {
                return;
            }
            panel._pveKomodoLoading = false;
            panel._pveKomodoRequest = null;
            let loaded = false;
            if (alive(refresh)) {
                refresh.setDisabled(false);
            }
            let message = panel.pveKomodoPage === 'containers' ? '容器数据加载失败，请重试' : 'Docker 数据加载失败，请重试';
            if (response && response.status === 401) {
                message = 'PVE 未登录或会话已过期';
            } else if (response && response.status === 403) {
                message = '无访问权限';
            } else if (response && response.status === 503) {
                message = 'PVE 会话验证或集成服务暂不可用';
            }
            try {
                const result = response && JSON.parse(response.responseText);
                if (!success) {
                    if (response && response.status === 503 && result && result.detail === 'not_configured') {
                        message = 'Komodo 未配置凭据';
                    }
                    throw new Error('request-failed');
                }
                if (!result || !panelSections(panel).every(function (section) {
                    return Array.isArray(result[section]) && result[section].every(function (row) {
                        return row && typeof row === 'object' && !Array.isArray(row);
                    });
                })) {
                    throw new Error('invalid-dashboard');
                }
                if (panel.pveKomodoPage === 'containers' && !Array.isArray(result.servers)) {
                    throw new Error('invalid-container-servers');
                }
                const rows = {};
                const tags = new Set();
                panelSections(panel).forEach(function (section) {
                    rows[section] = normalizeRows(result[section]);
                    rows[section].forEach(function (row) { row.tags.forEach(function (tag) { tags.add(tag); }); });
                });
                panel._pveKomodoRows = rows;
                loaded = true;
                panel._pveKomodoCsrf = typeof result.csrf_token === 'string' && result.csrf_token.length > 0 &&
                    result.csrf_token.length <= 4096 && !/[\r\n]/.test(result.csrf_token) ? result.csrf_token : null;
                if (panel._pveKomodoCsrf && actionState && actionState.requiresRefresh) actionState = null;
                const selector = panel.down('#pve-komodo-tag');
                const selectedTag = selector.getValue() || '';
                panel._pveKomodoUpdatingTags = true;
                try {
                    selector.getStore().loadData([{ value: '', label: '全部标签' }].concat(
                        Array.from(tags).sort().map(function (tag) { return { value: tag, label: tag }; }),
                    ));
                    selector.setValue(tags.has(selectedTag) ? selectedTag : '');
                    if (panel.pveKomodoPage === 'containers') {
                        const serverSelector = panel.down('#pve-komodo-server');
                        const selectedServer = serverSelector.getValue() || '';
                        const servers = result.servers.filter(function (server) {
                            return server && typeof server.id === 'string' && typeof server.name === 'string';
                        });
                        serverSelector.getStore().loadData([{ value: '', label: '全部服务器' }].concat(
                            servers.map(function (server) { return { value: server.id, label: server.name }; }),
                        ));
                        serverSelector.setValue(servers.some(function (server) { return server.id === selectedServer; }) ? selectedServer : '');
                    }
                } finally {
                    panel._pveKomodoUpdatingTags = false;
                }
                applyFilters(panel, true);
                const updated = dateText(result.updated_at);
                message = updated === '-' ? '已加载，更新时间未知' : '已更新：' + updated;
                if (panel.pveKomodoPage === 'containers') {
                    message = (updated === '-' ? '已加载，读取时间未知' : '读取时间：' + updated) +
                        '（Komodo 监控缓存，离线服务器可能不显示）';
                }
                if (Array.isArray(result.errors) && result.errors.length) {
                    message += '（部分数据不可用）';
                }
            } catch (error) {
                // Neither API error bodies nor arbitrary exception text is shown.
                clearRows(panel);
            }
            updateControls(panel);
            statusText(panel, message + (actionState ? '；' + actionState.message : ''));
            if (panel.pveKomodoPage === 'dashboard') highlightRequestedServer(panel);
            if (panel.pveKomodoPage === 'containers' && loaded) highlightRequestedContainer(panel);
        }

        try {
            // This endpoint is outside /api2; use raw same-origin Ext.Ajax.
            const request = Ext.Ajax.request({
                url: '/pve-komodo/api/' + (panel.pveKomodoPage === 'containers' ? 'containers' : 'dashboard'),
                method: 'GET', disableCaching: false, timeout: 60000,
                callback: complete,
            });
            if (!completed) {
                panel._pveKomodoRequest = request;
            }
        } catch (error) {
            complete(null, false, null);
        }
    }

    function pageListeners() {
        return {
            activate: function (panel) {
                activeDashboard = panel;
                panel._pveKomodoActive = true;
                loadDashboard(panel);
                if (actionState && actionState.pending) statusText(panel, actionState.message);
                updateControls(panel);
            },
            deactivate: function (panel) {
                panel._pveKomodoActive = false;
                if (activeDashboard === panel) activeDashboard = null;
                cancelDashboard(panel);
            },
            beforedestroy: function (panel) {
                panel._pveKomodoActive = false;
                if (activeDashboard === panel) activeDashboard = null;
                cancelDashboard(panel);
            },
        };
    }

    function filterChanged(field) {
        const panel = ownerPanel(field);
        if (alive(panel) && !panel._pveKomodoUpdatingTags) applyFilters(panel);
    }

    function dashboardConfig() {
        const nameColumn = { text: '名称', dataIndex: 'name', flex: 1, minWidth: 150, renderer: text };
        const stateColumn = { text: '状态', dataIndex: 'state', width: 95, renderer: stateText };
        const tagsColumn = { text: '标签', dataIndex: 'tags', width: 150, renderer: tagsText };
        return {
            pveKomodoPage: 'dashboard',
            layout: { type: 'vbox', align: 'stretch' },
            scrollable: true,
            bodyPadding: 8,
            tbar: {
                overflowHandler: 'scroller',
                items: [{
                    text: '刷新', iconCls: 'fa fa-refresh', itemId: 'pve-komodo-refresh',
                    handler: function (button) { loadDashboard(ownerPanel(button)); },
                }, '-', {
                    xtype: 'textfield', itemId: 'pve-komodo-search', width: 230,
                    emptyText: '搜索名称、主机、状态或标签',
                    enableKeyEvents: true,
                    listeners: { change: filterChanged },
                }, {
                    xtype: 'combobox', itemId: 'pve-komodo-tag', width: 180,
                    fieldLabel: '标签', labelWidth: 35, queryMode: 'local', editable: false,
                    displayField: 'label', valueField: 'value', value: '',
                    store: { autoDestroy: true, fields: ['label', 'value'], data: [{ value: '', label: '全部标签' }] },
                    listConfig: { itemTpl: '{label:htmlEncode}' },
                    listeners: { change: filterChanged },
                }, {
                    xtype: 'checkbox', itemId: 'pve-komodo-exclude-templates',
                    boxLabel: '排除模板', checked: true,
                    listeners: { change: filterChanged },
                }],
            },
            bbar: [{ xtype: 'tbtext', itemId: 'pve-komodo-dashboard-status', text: '尚未加载' }],
            items: [
                gridConfig('servers', ['id', 'name', 'state', 'cpu_percent', 'memory_percent', 'disk_percent',
                    'cpu_percent_value', 'cpu_percent_text', 'memory_percent_value', 'memory_percent_text',
                    'disk_percent_value', 'disk_percent_text', 'load_average', 'core_count',
                    'memory_used_bytes', 'memory_total_bytes', 'disk_used_bytes', 'disk_total_bytes',
                    'network_bytes_per_second', 'version', 'tags', 'template',
                    'container_count', 'containers_running', 'containers_stopped', 'containers_other', 'resource_count', 'pve_guest'], [
                    Object.assign({}, nameColumn), Object.assign({}, stateColumn),
                    { text: 'PVE 对象', dataIndex: 'pve_guest', width: 240, sortable: false,
                        renderer: function (value) { return text(guestLabel(value)); } },
                    { text: '容器数量', dataIndex: 'container_count', width: 175,
                        tooltip: 'Komodo 监控缓存；stop 包括已退出、已创建和暂停，其他状态单列；离线时显示 -',
                        renderer: function (value, metadata, record) {
                            const counts = ['running', 'stopped', 'other'].map(function (state) {
                                return record.get('containers_' + state);
                            });
                            if (!Number.isSafeInteger(value) || value < 0 ||
                                counts.some(function (count) { return !Number.isSafeInteger(count) || count < 0; }) ||
                                counts.reduce(function (total, count) { return total + count; }, 0) !== value) return '-';
                            return counts[0] + ' run ' + counts[1] + ' stop' + (counts[2] ? ' ' + counts[2] + ' other' : '');
                        } },
                    { text: '资源数量', dataIndex: 'resource_count', width: 95, align: 'center',
                        tooltip: 'Komodo 中关联到此服务器的应用栈、部署和代码仓库总数（含模板）',
                        renderer: function (value) {
                            return Number.isSafeInteger(value) && value >= 0 ? text(value) : '-';
                        } },
                    progressColumn('CPU', 'cpu_percent'), progressColumn('内存', 'memory_percent'),
                    progressColumn('磁盘', 'disk_percent'),
                    { text: '网络速率', dataIndex: 'network_bytes_per_second', width: 115, renderer: function (value) {
                        return numeric(value) && value >= 0 ? text(window.Proxmox.Utils.format_size(value) + '/s') : '-';
                    } },
                    { text: '平均负载', dataIndex: 'load_average', width: 150, renderer: function (value) {
                        return Array.isArray(value) ? text(value.map(function (load) {
                            return numeric(load) ? load.toFixed(2) : '-';
                        }).join(' / ')) : '-';
                    } },
                    { text: 'CPU 核数', dataIndex: 'core_count', width: 90, renderer: text },
                    { text: '版本', dataIndex: 'version', width: 100, renderer: text },
                    Object.assign({}, tagsColumn),
                ]),
                gridConfig('stacks', ['id', 'name', 'source', 'server_id', 'server_name', 'state',
                    'service_count', 'auto_update', 'tags', 'template'], [
                    Object.assign({}, nameColumn),
                    { text: '来源', dataIndex: 'source', flex: 1, minWidth: 150, renderer: text },
                    { text: '主机', dataIndex: 'server_name', width: 160, renderer: text },
                    Object.assign({}, stateColumn),
                    { text: '服务数量', dataIndex: 'service_count', width: 95, align: 'center',
                        renderer: function (value) {
                            return Number.isSafeInteger(value) && value >= 0 ? text(value) : '-';
                        } },
                    { text: '自动更新', dataIndex: 'auto_update', width: 100, align: 'center',
                        renderer: function (value) {
                            return value === true ? '已启用' : value === false ? '已禁用' : '未知';
                        } },
                    Object.assign({}, tagsColumn),
                ]),
                gridConfig('procedures', ['id', 'name', 'state', 'next_run_at', 'tags', 'template'], [
                    Object.assign({}, nameColumn), Object.assign({}, stateColumn),
                    { text: '下次运行', dataIndex: 'next_run_at', width: 180,
                        renderer: function (value) { return text(numeric(value) ? dateText(value) : '-'); } },
                    Object.assign({}, tagsColumn),
                ]),
            ],
            listeners: pageListeners(),
        };
    }

    function containersConfig() {
        const grid = gridConfig('containers', ['id', 'container_id', 'server_id', 'server_name',
            'name', 'state', 'image', 'networks', 'ports', 'volumes', 'tags'], [
            { text: '名称', dataIndex: 'name', flex: 1, minWidth: 160, renderer: text },
            { text: '服务器', dataIndex: 'server_name', width: 160, renderer: text },
            { text: '状态', dataIndex: 'state', width: 100, renderer: stateText },
            { text: '镜像', dataIndex: 'image', flex: 1, minWidth: 200, renderer: text },
            { text: '网络', dataIndex: 'networks', width: 160, renderer: tagsText },
            { text: '端口', dataIndex: 'ports', width: 200, renderer: tagsText },
            { text: '存储卷', dataIndex: 'volumes', width: 220, renderer: tagsText },
        ]);
        grid.collapsible = false;
        grid.titleCollapse = false;
        grid.margin = 0;
        grid.viewConfig.emptyText = '暂无可显示的容器数据';
        return {
            pveKomodoPage: 'containers',
            layout: 'fit',
            bodyPadding: 8,
            tbar: {
                overflowHandler: 'scroller',
                items: [{
                    text: '刷新', iconCls: 'fa fa-refresh', itemId: 'pve-komodo-refresh',
                    handler: function (button) { loadDashboard(ownerPanel(button)); },
                }, '-', {
                    xtype: 'combobox', itemId: 'pve-komodo-server', width: 210,
                    fieldLabel: '服务器', labelWidth: 50, queryMode: 'local', editable: false,
                    displayField: 'label', valueField: 'value', value: '',
                    store: { autoDestroy: true, fields: ['label', 'value'], data: [{ value: '', label: '全部服务器' }] },
                    listConfig: { itemTpl: '{label:htmlEncode}' },
                    listeners: { change: filterChanged },
                }, {
                    xtype: 'combobox', itemId: 'pve-komodo-tag', width: 230,
                    fieldLabel: '服务器标签', labelWidth: 80, queryMode: 'local', editable: false,
                    displayField: 'label', valueField: 'value', value: '',
                    store: { autoDestroy: true, fields: ['label', 'value'], data: [{ value: '', label: '全部标签' }] },
                    listConfig: { itemTpl: '{label:htmlEncode}' },
                    listeners: { change: filterChanged },
                }, {
                    xtype: 'textfield', itemId: 'pve-komodo-search', width: 250,
                    emptyText: '搜索名称、镜像、网络或端口',
                    listeners: { change: filterChanged },
                }],
            },
            bbar: [{ xtype: 'tbtext', itemId: 'pve-komodo-dashboard-status', text: '尚未加载' }],
            items: [grid],
            listeners: pageListeners(),
        };
    }

    // Keep filters aligned with client/core/rs/src/entities/{alert.rs,mod.rs}.
    const alertResourceLabels = {
        System: '系统', Swarm: 'Swarm', Server: '服务器', Stack: '应用栈', Deployment: '部署',
        Build: '构建', Repo: '仓库', Procedure: '流程', Action: '操作', Builder: '构建器',
        Alerter: '告警器', ResourceSync: '资源同步',
    };
    const alertTypeLabels = {
        None: '无', Test: '测试告警', SwarmUnhealthy: 'Swarm 不健康',
        ServerUnreachable: '服务器无法连接', ServerCpu: '服务器 CPU 使用率',
        ServerMem: '服务器内存使用率', ServerDisk: '服务器磁盘使用率',
        ServerVersionMismatch: '服务器版本不匹配', ContainerStateChange: '容器状态变化',
        DeploymentImageUpdateAvailable: '部署镜像有更新', DeploymentAutoUpdated: '部署已自动更新',
        StackStateChange: '应用栈状态变化', StackImageUpdateAvailable: '应用栈镜像有更新',
        StackAutoUpdated: '应用栈已自动更新', AwsBuilderTerminationFailed: 'AWS 构建器终止失败',
        ResourceSyncPendingUpdates: '资源同步有待更新', BuildFailed: '构建失败',
        RepoBuildFailed: '仓库构建失败', ProcedureFailed: '流程失败', ActionFailed: '操作失败',
        ScheduleRun: '计划任务已运行', Custom: '自定义告警',
    };
    const alertLevelLabels = { OK: '正常', WARNING: '警告', CRITICAL: '严重', UNKNOWN: '未知' };

    function mappedText(labels, value) {
        if (value === 'unknown') return '未知';
        return text(typeof value === 'string' && Object.prototype.hasOwnProperty.call(labels, value) ? labels[value] : value);
    }

    function alertTime(value) {
        return numeric(value) && Number.isInteger(value) && value >= 0 ? text(dateText(value)) : '-';
    }

    function resolvedText(value) {
        return value === true ? '已解决' : value === false ? '未解决' : '未知';
    }

    function alertResource(row) {
        return mappedText(alertResourceLabels, row.resource_type) + '：' +
            text(row.resource_name || row.resource_id);
    }

    function alertDuration(row) {
        if (row.resolved !== true || !numeric(row.ts) || !Number.isInteger(row.ts) || row.ts < 0 ||
            !numeric(row.resolved_ts) || !Number.isInteger(row.resolved_ts) ||
            row.resolved_ts < row.ts || !Number.isFinite(new Date(row.ts).getTime()) ||
            !Number.isFinite(new Date(row.resolved_ts).getTime())) return '-';
        const seconds = Math.floor((row.resolved_ts - row.ts) / 1000);
        return Math.floor(seconds / 86400) + ' 天 ' + Math.floor(seconds % 86400 / 3600) + ' 小时 ' +
            Math.floor(seconds % 3600 / 60) + ' 分 ' + seconds % 60 + ' 秒';
    }

    function showAlertDetails(grid, record) {
        const panel = ownerPanel(grid);
        if (!alive(panel) || !panel._pveKomodoAlertsActive || !record) return;
        const row = record.data;
        const fields = [
            ['发生时间', alertTime(row.ts)], ['解决时间', alertTime(row.resolved_ts)],
            ['持续时间', text(alertDuration(row))], ['资源', alertResource(row)],
            ['状态', resolvedText(row.resolved)], ['级别', mappedText(alertLevelLabels, row.level)],
            ['告警类型', mappedText(alertTypeLabels, row.alert_type)],
        ];
        const details = row.details && typeof row.details === 'object' && !Array.isArray(row.details) ? row.details : {};
        const html = fields.map(function (field) {
            return '<p><strong>' + text(field[0]) + '：</strong>' + field[1] + '</p>';
        }).join('') + (row.details_omitted === true ? '<p>部分详情已省略。</p>' : '') +
            '<pre style="white-space:pre-wrap;overflow-wrap:anywhere">' + text(JSON.stringify(details, null, 2)) + '</pre>';
        if (alive(panel._pveKomodoAlertDetails)) panel._pveKomodoAlertDetails.destroy();
        panel._pveKomodoAlertDetails = Ext.create('Ext.window.Window', {
            title: '告警详情', width: 680, height: 520, modal: true, closeAction: 'destroy',
            layout: 'fit', items: [{ xtype: 'component', padding: 16, scrollable: true, html: html }],
        });
        panel._pveKomodoAlertDetails.show();
    }

    function alertsControls(panel) {
        const loading = !!panel._pveKomodoAlertsLoading;
        const page = panel._pveKomodoAlertsPage || 0;
        ['refresh', 'first', 'previous', 'next'].forEach(function (name) {
            const button = panel.down('#pve-komodo-alerts-' + name);
            if (alive(button)) button.setDisabled(loading || (name === 'first' || name === 'previous' ? page === 0 :
                name === 'next' ? panel._pveKomodoAlertsNext === null || panel._pveKomodoAlertsNext === undefined : false));
        });
        panel.down('#pve-komodo-alerts-page').setText('第 ' + (page + 1) + ' 页（每页 100 条）');
    }

    function alertsStatus(panel, message) {
        const status = panel.down('#pve-komodo-alerts-status');
        if (alive(status)) status.setText(text(message));
    }

    function cancelAlerts(panel) {
        panel._pveKomodoAlertsGeneration = (panel._pveKomodoAlertsGeneration || 0) + 1;
        const request = panel._pveKomodoAlertsRequest;
        panel._pveKomodoAlertsRequest = null;
        panel._pveKomodoAlertsLoading = false;
        if (request) Ext.Ajax.abort(request);
    }

    function loadAlerts(panel, page) {
        if (!alive(panel) || !panel._pveKomodoAlertsActive) return;
        cancelAlerts(panel);
        if (alive(panel._pveKomodoAlertDetails)) panel._pveKomodoAlertDetails.destroy();
        panel._pveKomodoAlertsPage = page;
        panel._pveKomodoAlertsNext = null;
        panel._pveKomodoAlertsLoading = true;
        const generation = panel._pveKomodoAlertsGeneration;
        const grid = panel.down('#pve-komodo-alerts-grid');
        grid.getStore().loadData([]);
        alertsControls(panel);
        alertsStatus(panel, '正在加载…');
        const params = { page: page };
        const resource = panel.down('#pve-komodo-alerts-resource').getValue();
        const type = panel.down('#pve-komodo-alerts-type').getValue();
        if (resource) params.resource_type = resource;
        if (type) params.alert_type = type;
        if (panel.down('#pve-komodo-alerts-unresolved').getValue()) params.unresolved_only = true;
        let completed = false;
        function complete(options, success, response) {
            completed = true;
            if (!alive(panel) || !panel._pveKomodoAlertsActive ||
                generation !== panel._pveKomodoAlertsGeneration || !alive(grid)) return;
            panel._pveKomodoAlertsRequest = null;
            panel._pveKomodoAlertsLoading = false;
            let message = '告警数据加载失败，请重试';
            if (response && response.status === 401) message = 'PVE 未登录或会话已过期';
            if (response && response.status === 403) message = '无访问权限';
            if (response && response.status === 503) message = 'PVE 会话验证或集成服务暂不可用';
            try {
                const result = response && JSON.parse(response.responseText);
                if (!success) {
                    if (response && response.status === 503 && result && result.detail === 'not_configured') message = 'Komodo 未配置凭据';
                    throw new Error('alerts-unavailable');
                }
                if (!result || result.page !== page || !Array.isArray(result.alerts) || result.alerts.length > 100 ||
                    !result.alerts.every(function (row) { return row && typeof row === 'object' && !Array.isArray(row); }) ||
                    !(result.next_page === null ||
                        Number.isInteger(result.next_page) && result.next_page === page + 1 &&
                            result.next_page <= 1000000)) throw new Error('invalid-alerts');
                // Preserve upstream timestamp-descending order across pages. Local
                // column sorting would misleadingly sort only these 100 records.
                grid.getStore().loadData(result.alerts);
                panel._pveKomodoAlertsNext = result.next_page;
                message = '已加载 ' + result.alerts.length + ' 条；按发生时间倒序' +
                    (dateText(result.updated_at) === '-' ? '' : '；读取时间：' + dateText(result.updated_at));
            } catch (error) {
                grid.getStore().loadData([]);
            }
            alertsControls(panel);
            alertsStatus(panel, message);
        }
        try {
            const request = Ext.Ajax.request({ url: '/pve-komodo/api/alerts', method: 'GET', params: params,
                // ExtJS 7 otherwise adds _dc, which is not an alert filter.
                // The API already returns private, no-store responses.
                disableCaching: false,
                timeout: 60000, callback: complete });
            if (!completed) panel._pveKomodoAlertsRequest = request;
        } catch (error) { complete(null, false, null); }
    }

    function alertsConfig() {
        function filterChange(field) {
            const panel = ownerPanel(field);
            if (alive(panel) && !panel._pveKomodoAlertsUpdating) loadAlerts(panel, 0);
        }
        function selector(id, title, labels) {
            return {
                xtype: 'combobox', itemId: 'pve-komodo-alerts-' + id, fieldLabel: title,
                labelWidth: 65, width: id === 'type' ? 290 : 220, queryMode: 'local', editable: false,
                displayField: 'label', valueField: 'value', value: '',
                store: { autoDestroy: true, fields: ['label', 'value'], data: [{ value: '', label: '全部' }].concat(
                    Object.keys(labels).map(function (value) { return { value: value, label: labels[value] }; }),
                ) },
                listConfig: { itemTpl: '{label:htmlEncode}' }, listeners: { change: filterChange },
            };
        }
        function navigate(button, where) {
            const panel = ownerPanel(button);
            if (!alive(panel) || panel._pveKomodoAlertsLoading) return;
            const page = where === 'first' ? 0 : where === 'previous' ? Math.max(0, (panel._pveKomodoAlertsPage || 0) - 1) :
                panel._pveKomodoAlertsNext;
            if (Number.isInteger(page) && page >= 0) loadAlerts(panel, page);
        }
        function leave(panel) {
            panel._pveKomodoAlertsActive = false;
            cancelAlerts(panel);
            if (alive(panel._pveKomodoAlertDetails)) panel._pveKomodoAlertDetails.destroy();
        }
        return {
            pveKomodoPage: 'alerts', layout: 'fit', bodyPadding: 8,
            tbar: { overflowHandler: 'scroller', items: [
                { text: '刷新', iconCls: 'fa fa-refresh', itemId: 'pve-komodo-alerts-refresh',
                    handler: function (button) { const panel = ownerPanel(button); loadAlerts(panel, panel._pveKomodoAlertsPage || 0); } },
                '-', selector('resource', '资源类型', alertResourceLabels), selector('type', '告警类型', alertTypeLabels),
                { xtype: 'checkbox', itemId: 'pve-komodo-alerts-unresolved', boxLabel: '仅显示未解决', checked: false,
                    listeners: { change: filterChange } },
                { text: '重置', itemId: 'pve-komodo-alerts-reset', handler: function (button) {
                    const panel = ownerPanel(button);
                    panel._pveKomodoAlertsUpdating = true;
                    try {
                        panel.down('#pve-komodo-alerts-resource').setValue('');
                        panel.down('#pve-komodo-alerts-type').setValue('');
                        panel.down('#pve-komodo-alerts-unresolved').setValue(false);
                    } finally { panel._pveKomodoAlertsUpdating = false; }
                    loadAlerts(panel, 0);
                } }, '-',
                { text: '首页', itemId: 'pve-komodo-alerts-first', disabled: true, handler: function (button) { navigate(button, 'first'); } },
                { text: '上一页', itemId: 'pve-komodo-alerts-previous', disabled: true, handler: function (button) { navigate(button, 'previous'); } },
                { xtype: 'tbtext', itemId: 'pve-komodo-alerts-page', text: '第 1 页（每页 100 条）' },
                { text: '下一页', itemId: 'pve-komodo-alerts-next', disabled: true, handler: function (button) { navigate(button, 'next'); } },
            ] },
            bbar: [{ xtype: 'tbtext', itemId: 'pve-komodo-alerts-status', text: '尚未加载' }],
            items: [{
                xtype: 'gridpanel', itemId: 'pve-komodo-alerts-grid', border: false,
                store: { autoDestroy: true, fields: ['id', 'ts', 'resolved_ts', 'resolved', 'level', 'resource_type',
                    'resource_id', 'resource_name', 'alert_type', 'details', 'details_omitted'], data: [], remoteSort: true },
                columns: [
                    { xtype: 'actioncolumn', text: '详情', width: 55, sortable: false, menuDisabled: true,
                        items: [{ iconCls: 'fa fa-search', tooltip: '查看详情', handler: function (view, rowIndex, colIndex, item, event, record) {
                            showAlertDetails(view.up('gridpanel'), record);
                        } }] },
                    { text: '发生时间', dataIndex: 'ts', width: 180, sortable: false, renderer: alertTime },
                    { text: '资源', dataIndex: 'resource_name', flex: 1, minWidth: 190, sortable: false,
                        renderer: function (value, meta, record) { return alertResource(record.data); } },
                    { text: '状态', dataIndex: 'resolved', width: 90, sortable: false, renderer: resolvedText },
                    { text: '级别', dataIndex: 'level', width: 90, sortable: false,
                        renderer: function (value) { return mappedText(alertLevelLabels, value); } },
                    { text: '告警类型', dataIndex: 'alert_type', width: 190, sortable: false,
                        renderer: function (value) { return mappedText(alertTypeLabels, value); } },
                    { text: '解决时间', dataIndex: 'resolved_ts', width: 180, sortable: false, renderer: alertTime },
                ],
                viewConfig: { stripeRows: true, enableTextSelection: true, emptyText: '暂无符合条件的告警', deferEmptyText: false },
                listeners: { itemdblclick: function (view, record) { showAlertDetails(this, record); } },
            }],
            listeners: {
                activate: function (panel) {
                    panel._pveKomodoAlertsActive = true;
                    loadAlerts(panel, panel._pveKomodoAlertsPage || 0);
                },
                deactivate: leave, beforedestroy: leave,
            },
        };
    }

    // Fixed Operation variants and labels from entities/mod.rs and zh-cn.json.
    const operationLabels = {
        None: "无",
        CreateSwarm: "创建集群",
        UpdateSwarm: "更新集群",
        RenameSwarm: "重命名集群",
        DeleteSwarm: "删除集群",
        RemoveSwarmNodes: "移除集群节点",
        UpdateSwarmNode: "更新集群节点",
        RemoveSwarmStacks: "移除集群应用栈",
        RemoveSwarmServices: "移除集群服务",
        CreateSwarmConfig: "创建集群配置",
        RotateSwarmConfig: "轮换集群配置",
        RemoveSwarmConfigs: "移除集群配置",
        CreateSwarmSecret: "创建集群密钥",
        RotateSwarmSecret: "轮换集群密钥",
        RemoveSwarmSecrets: "移除集群密钥",
        CreateServer: "创建服务器",
        UpdateServer: "更新服务器",
        UpdateServerKey: "更新服务器密钥",
        DeleteServer: "删除服务器",
        RenameServer: "重命名服务器",
        StartContainer: "启动容器",
        RestartContainer: "重启容器",
        PauseContainer: "暂停容器",
        UnpauseContainer: "恢复容器",
        StopContainer: "停止容器",
        DestroyContainer: "销毁容器",
        StartAllContainers: "启动所有容器",
        RestartAllContainers: "重启所有容器",
        PauseAllContainers: "暂停所有容器",
        UnpauseAllContainers: "恢复所有容器",
        StopAllContainers: "停止所有容器",
        PruneContainers: "清理容器",
        CreateNetwork: "创建网络",
        DeleteNetwork: "删除网络",
        PruneNetworks: "清理网络",
        DeleteImage: "删除镜像",
        PruneImages: "清理镜像",
        DeleteVolume: "删除存储卷",
        PruneVolumes: "清理存储卷",
        PruneDockerBuilders: "清理Docker 构建器",
        PruneBuildx: "清理Buildx 缓存",
        PruneSystem: "清理系统",
        CreateStack: "创建应用栈",
        UpdateStack: "更新应用栈",
        RenameStack: "重命名应用栈",
        DeleteStack: "删除应用栈",
        WriteStackContents: "写入应用栈文件",
        RefreshStackCache: "刷新应用栈缓存",
        PullStack: "拉取应用栈",
        DeployStack: "部署应用栈",
        StartStack: "启动应用栈",
        RestartStack: "重启应用栈",
        PauseStack: "暂停应用栈",
        UnpauseStack: "恢复应用栈",
        StopStack: "停止应用栈",
        DestroyStack: "销毁应用栈",
        RunStackService: "执行应用栈服务",
        CheckStackForUpdate: "检查应用栈更新",
        DeployStackService: "部署应用栈服务",
        PullStackService: "拉取应用栈服务",
        StartStackService: "启动应用栈服务",
        RestartStackService: "重启应用栈服务",
        PauseStackService: "暂停应用栈服务",
        UnpauseStackService: "恢复应用栈服务",
        StopStackService: "停止应用栈服务",
        DestroyStackService: "销毁应用栈服务",
        CreateDeployment: "创建部署",
        UpdateDeployment: "更新部署",
        RenameDeployment: "重命名部署",
        DeleteDeployment: "删除部署",
        Deploy: "部署",
        PullDeployment: "拉取部署",
        StartDeployment: "启动部署",
        RestartDeployment: "重启部署",
        PauseDeployment: "暂停部署",
        UnpauseDeployment: "恢复部署",
        StopDeployment: "停止部署",
        DestroyDeployment: "销毁部署",
        CheckDeploymentForUpdate: "检查部署更新",
        CreateBuild: "创建构建",
        UpdateBuild: "更新构建",
        RenameBuild: "重命名构建",
        DeleteBuild: "删除构建",
        RunBuild: "执行构建",
        CancelBuild: "取消构建",
        WriteDockerfile: "写入Dockerfile",
        CreateRepo: "创建代码仓库",
        UpdateRepo: "更新代码仓库",
        RenameRepo: "重命名代码仓库",
        DeleteRepo: "删除代码仓库",
        CloneRepo: "克隆代码仓库",
        PullRepo: "拉取代码仓库",
        BuildRepo: "构建代码仓库",
        CancelRepoBuild: "取消代码仓库构建",
        CreateProcedure: "创建流程",
        UpdateProcedure: "更新流程",
        RenameProcedure: "重命名流程",
        DeleteProcedure: "删除流程",
        RunProcedure: "执行流程",
        CancelProcedure: "取消流程",
        CreateAction: "创建操作",
        UpdateAction: "更新操作",
        RenameAction: "重命名操作",
        DeleteAction: "删除操作",
        RunAction: "执行操作",
        CancelAction: "取消操作",
        CreateResourceSync: "创建资源同步",
        UpdateResourceSync: "更新资源同步",
        RenameResourceSync: "重命名资源同步",
        DeleteResourceSync: "删除资源同步",
        WriteSyncContents: "写入同步文件",
        CommitSync: "提交资源同步",
        RunSync: "执行资源同步",
        CreateBuilder: "创建构建器",
        UpdateBuilder: "更新构建器",
        RenameBuilder: "重命名构建器",
        DeleteBuilder: "删除构建器",
        CreateAlerter: "创建告警器",
        UpdateAlerter: "更新告警器",
        RenameAlerter: "重命名告警器",
        DeleteAlerter: "删除告警器",
        TestAlerter: "测试告警器",
        SendAlert: "发送告警",
        ClearRepoCache: "清除代码仓库缓存",
        BackupCoreDatabase: "备份Core 数据库",
        GlobalAutoUpdate: "全局自动更新",
        RotateAllServerKeys: "轮换所有服务器密钥",
        RotateCoreKeys: "轮换Core 密钥",
        CreateVariable: "创建变量",
        UpdateVariableValue: "更新变量值",
        DeleteVariable: "删除变量",
        CreateGitProviderAccount: "创建Git 服务商账户",
        UpdateGitProviderAccount: "更新Git 服务商账户",
        DeleteGitProviderAccount: "删除Git 服务商账户",
        CreateDockerRegistryAccount: "创建镜像仓库账户",
        UpdateDockerRegistryAccount: "更新镜像仓库账户",
        DeleteDockerRegistryAccount: "删除镜像仓库账户",
    };

    function updateResult(row) {
        if (row.status === 'Queued') return '排队中';
        if (row.status === 'InProgress') return '进行中';
        if (row.status === 'Complete') return row.success === true ? '成功' : row.success === false ? '失败' : '未知';
        return '未知';
    }

    function updateDuration(row) {
        return alertDuration({ resolved: row.status === 'Complete', ts: row.start_ts, resolved_ts: row.end_ts });
    }

    function updateVersion(value) {
        if (value && typeof value === 'object' && ['major', 'minor', 'patch'].every(function (field) {
            return Number.isInteger(value[field]) && value[field] >= 0;
        })) return text(value.major + '.' + value.minor + '.' + value.patch);
        return text(typeof value === 'string' ? value : null);
    }

    function updatesStatus(panel, message) {
        const status = panel.down('#pve-komodo-updates-status');
        if (alive(status)) status.setText(text(message));
    }

    function cancelUpdateDetail(window) {
        window._pveKomodoDetailGeneration = (window._pveKomodoDetailGeneration || 0) + 1;
        const request = window._pveKomodoDetailRequest;
        window._pveKomodoDetailRequest = null;
        if (request) Ext.Ajax.abort(request);
    }

    function closeUpdateDetail(panel) {
        const window = panel._pveKomodoUpdateDetail;
        panel._pveKomodoUpdateDetail = null;
        if (alive(window)) window.destroy();
    }

    function showUpdateDetails(grid, record) {
        const panel = ownerPanel(grid);
        if (!alive(panel) || !panel._pveKomodoUpdatesActive || !record) return;
        const id = record.get('id');
        if (typeof id !== 'string' || !/^[0-9a-f]{24}$/i.test(id)) {
            updatesStatus(panel, '操作记录标识无效，请刷新后重试');
            return;
        }
        const listUsername = record.get('username');
        closeUpdateDetail(panel);
        const window = Ext.create('Ext.window.Window', {
            title: '操作详情', width: 900, height: 590, modal: true, closeAction: 'destroy',
            layout: { type: 'vbox', align: 'stretch' },
            items: [{
                xtype: 'component', itemId: 'pve-komodo-update-summary', padding: 16, height: 260,
                scrollable: true, html: '正在加载操作详情…',
            }, {
                xtype: 'gridpanel', itemId: 'pve-komodo-update-stages', title: '操作阶段', flex: 1, minHeight: 180,
                store: { autoDestroy: true, fields: ['stage', 'success', 'start_ts', 'end_ts'], data: [] },
                columns: [
                    { text: '阶段', dataIndex: 'stage', flex: 1, minWidth: 190, sortable: false, renderer: text },
                    { text: '结果', dataIndex: 'success', width: 80, sortable: false,
                        renderer: function (value) { return value === true ? '成功' : value === false ? '失败' : '未知'; } },
                    { text: '开始时间', dataIndex: 'start_ts', width: 180, sortable: false, renderer: alertTime },
                    { text: '结束时间', dataIndex: 'end_ts', width: 180, sortable: false, renderer: alertTime },
                ],
                viewConfig: { stripeRows: true, enableTextSelection: true, emptyText: '暂无可显示的操作阶段', deferEmptyText: false },
            }],
            listeners: { beforedestroy: function (window) { cancelUpdateDetail(window); } },
        });
        panel._pveKomodoUpdateDetail = window;
        window.show();
        const generation = window._pveKomodoDetailGeneration || 0;
        let completed = false;
        function complete(options, success, response) {
            completed = true;
            if (!alive(panel) || !panel._pveKomodoUpdatesActive || !alive(window) ||
                panel._pveKomodoUpdateDetail !== window || (window._pveKomodoDetailGeneration || 0) !== generation) return;
            window._pveKomodoDetailRequest = null;
            const summary = window.down('#pve-komodo-update-summary');
            const stages = window.down('#pve-komodo-update-stages');
            let message = '操作详情加载失败，请关闭后重试';
            if (response && response.status === 401) message = 'PVE 未登录或会话已过期';
            if (response && response.status === 403) message = '无访问权限';
            if (response && response.status === 404) message = '操作记录不存在或已失效';
            if (response && response.status === 503) message = 'PVE 会话验证或集成服务暂不可用';
            try {
                const result = response && JSON.parse(response.responseText);
                const update = result && result.update;
                if (!success || !update || update.id !== id || !Array.isArray(update.stages) ||
                    !update.stages.every(function (stage) { return stage && typeof stage === 'object' && !Array.isArray(stage); })) {
                    throw new Error('invalid-update-detail');
                }
                const fields = [
                    ['开始时间', alertTime(update.start_ts)], ['结束时间', alertTime(update.end_ts)],
                    ['持续时间', text(updateDuration(update))], ['资源', alertResource(update)],
                    ['操作', mappedText(operationLabels, update.operation)], ['结果', updateResult(update)],
                    ['操作人', text(update.username || listUsername || (update.operator_id ? '用户 ID：' + update.operator_id : null))],
                    ['版本', updateVersion(update.version)], ['提交', text(update.commit_hash)],
                ];
                summary.update(fields.map(function (field) {
                    return '<p><strong>' + text(field[0]) + '：</strong>' + field[1] + '</p>';
                }).join('') + (update.details_omitted === true ? '<p>仅显示操作阶段与结果。</p>' : ''));
                stages.getStore().loadData(update.stages);
                return;
            } catch (error) { /* Do not show raw error bodies, commands, or configuration. */ }
            stages.getStore().loadData([]);
            summary.update(text(message));
        }
        try {
            const request = Ext.Ajax.request({
                url: '/pve-komodo/api/updates/' + id, method: 'GET', disableCaching: false,
                timeout: 60000, callback: complete,
            });
            if (!completed) window._pveKomodoDetailRequest = request;
        } catch (error) { complete(null, false, null); }
    }

    function updatesControls(panel) {
        const loading = !!panel._pveKomodoUpdatesLoading;
        const page = panel._pveKomodoUpdatesPage || 0;
        ['refresh', 'first', 'previous', 'next'].forEach(function (name) {
            const button = panel.down('#pve-komodo-updates-' + name);
            if (alive(button)) button.setDisabled(loading || (name === 'first' || name === 'previous' ? page === 0 :
                name === 'next' ? panel._pveKomodoUpdatesNext === null || panel._pveKomodoUpdatesNext === undefined : false));
        });
        panel.down('#pve-komodo-updates-page').setText('第 ' + (page + 1) + ' 页（每页 100 条）');
    }

    function cancelUpdates(panel) {
        panel._pveKomodoUpdatesGeneration = (panel._pveKomodoUpdatesGeneration || 0) + 1;
        const request = panel._pveKomodoUpdatesRequest;
        panel._pveKomodoUpdatesRequest = null;
        panel._pveKomodoUpdatesLoading = false;
        if (request) Ext.Ajax.abort(request);
    }

    function loadUpdates(panel, page) {
        if (!alive(panel) || !panel._pveKomodoUpdatesActive) return;
        cancelUpdates(panel);
        closeUpdateDetail(panel);
        panel._pveKomodoUpdatesPage = page;
        panel._pveKomodoUpdatesNext = null;
        panel._pveKomodoUpdatesLoading = true;
        const generation = panel._pveKomodoUpdatesGeneration;
        const grid = panel.down('#pve-komodo-updates-grid');
        grid.getStore().loadData([]);
        updatesControls(panel);
        updatesStatus(panel, '正在加载…');
        const params = { page: page };
        const resource = panel.down('#pve-komodo-updates-resource').getValue();
        const operation = panel.down('#pve-komodo-updates-operation').getValue();
        if (resource) params.resource_type = resource;
        if (operation) params.operation = operation;
        let completed = false;
        function complete(options, success, response) {
            completed = true;
            if (!alive(panel) || !panel._pveKomodoUpdatesActive ||
                generation !== panel._pveKomodoUpdatesGeneration || !alive(grid)) return;
            panel._pveKomodoUpdatesRequest = null;
            panel._pveKomodoUpdatesLoading = false;
            let message = '操作记录加载失败，请重试';
            if (response && response.status === 401) message = 'PVE 未登录或会话已过期';
            if (response && response.status === 403) message = '无访问权限';
            if (response && response.status === 503) message = 'PVE 会话验证或集成服务暂不可用';
            try {
                const result = response && JSON.parse(response.responseText);
                if (!success) {
                    if (response && response.status === 503 && result && result.detail === 'not_configured') message = 'Komodo 未配置凭据';
                    throw new Error('updates-unavailable');
                }
                if (!result || result.page !== page || !Array.isArray(result.updates) || result.updates.length > 100 ||
                    !result.updates.every(function (row) { return row && typeof row === 'object' && !Array.isArray(row); }) ||
                    !(result.next_page === null || Number.isInteger(result.next_page) &&
                        result.next_page === page + 1 && result.next_page <= 1000000)) throw new Error('invalid-updates');
                grid.getStore().loadData(result.updates);
                panel._pveKomodoUpdatesNext = result.next_page;
                message = '已加载 ' + result.updates.length + ' 条；按开始时间倒序' +
                    (dateText(result.updated_at) === '-' ? '' : '；读取时间：' + dateText(result.updated_at));
                if (Array.isArray(result.errors) && result.errors.length) message += '（部分数据不可用）';
            } catch (error) { grid.getStore().loadData([]); }
            updatesControls(panel);
            updatesStatus(panel, message);
        }
        try {
            const request = Ext.Ajax.request({ url: '/pve-komodo/api/updates', method: 'GET', params: params,
                disableCaching: false, timeout: 60000, callback: complete });
            if (!completed) panel._pveKomodoUpdatesRequest = request;
        } catch (error) { complete(null, false, null); }
    }

    function updatesConfig() {
        function filterChange(field) {
            const panel = ownerPanel(field);
            if (alive(panel) && !panel._pveKomodoUpdatesUpdating) loadUpdates(panel, 0);
        }
        function selector(id, title, labels) {
            return {
                xtype: 'combobox', itemId: 'pve-komodo-updates-' + id, fieldLabel: title,
                labelWidth: id === 'resource' ? 65 : 35, width: id === 'operation' ? 290 : 220,
                queryMode: 'local', editable: false, displayField: 'label', valueField: 'value', value: '',
                store: { autoDestroy: true, fields: ['label', 'value'], data: [{ value: '', label: '全部' }].concat(
                    Object.keys(labels).map(function (value) { return { value: value, label: labels[value] }; }),
                ) }, listConfig: { itemTpl: '{label:htmlEncode}' }, listeners: { change: filterChange },
            };
        }
        function navigate(button, where) {
            const panel = ownerPanel(button);
            if (!alive(panel) || panel._pveKomodoUpdatesLoading) return;
            const page = where === 'first' ? 0 : where === 'previous' ? Math.max(0, (panel._pveKomodoUpdatesPage || 0) - 1) :
                panel._pveKomodoUpdatesNext;
            if (Number.isInteger(page) && page >= 0) loadUpdates(panel, page);
        }
        function leave(panel) {
            panel._pveKomodoUpdatesActive = false;
            cancelUpdates(panel);
            closeUpdateDetail(panel);
        }
        return {
            pveKomodoPage: 'updates', layout: 'fit', bodyPadding: 8,
            tbar: { overflowHandler: 'scroller', items: [
                { text: '刷新', iconCls: 'fa fa-refresh', itemId: 'pve-komodo-updates-refresh',
                    handler: function (button) { const panel = ownerPanel(button); loadUpdates(panel, panel._pveKomodoUpdatesPage || 0); } },
                '-', selector('resource', '资源类型', alertResourceLabels), selector('operation', '操作', operationLabels),
                { text: '重置', itemId: 'pve-komodo-updates-reset', handler: function (button) {
                    const panel = ownerPanel(button);
                    panel._pveKomodoUpdatesUpdating = true;
                    try {
                        panel.down('#pve-komodo-updates-resource').setValue('');
                        panel.down('#pve-komodo-updates-operation').setValue('');
                    } finally { panel._pveKomodoUpdatesUpdating = false; }
                    loadUpdates(panel, 0);
                } }, '-',
                { text: '首页', itemId: 'pve-komodo-updates-first', disabled: true, handler: function (button) { navigate(button, 'first'); } },
                { text: '上一页', itemId: 'pve-komodo-updates-previous', disabled: true, handler: function (button) { navigate(button, 'previous'); } },
                { xtype: 'tbtext', itemId: 'pve-komodo-updates-page', text: '第 1 页（每页 100 条）' },
                { text: '下一页', itemId: 'pve-komodo-updates-next', disabled: true, handler: function (button) { navigate(button, 'next'); } },
            ] },
            bbar: [{ xtype: 'tbtext', itemId: 'pve-komodo-updates-status', text: '尚未加载' }],
            items: [{
                xtype: 'gridpanel', itemId: 'pve-komodo-updates-grid', border: false,
                store: { autoDestroy: true, fields: ['id', 'operation', 'start_ts', 'status', 'success', 'username',
                    'resource_type', 'resource_id', 'resource_name'], data: [], remoteSort: true },
                columns: [
                    { xtype: 'actioncolumn', text: '详情', width: 55, sortable: false, menuDisabled: true,
                        items: [{ iconCls: 'fa fa-search', tooltip: '查看详情', handler: function (view, rowIndex, colIndex, item, event, record) {
                            showUpdateDetails(view.up('gridpanel'), record);
                        } }] },
                    { text: '资源', dataIndex: 'resource_name', flex: 1, minWidth: 190, sortable: false,
                        renderer: function (value, meta, record) { return alertResource(record.data); } },
                    { text: '操作', dataIndex: 'operation', width: 200, sortable: false,
                        renderer: function (value) { return mappedText(operationLabels, value); } },
                    { text: '结果', dataIndex: 'status', width: 90, sortable: false,
                        renderer: function (value, meta, record) { return updateResult(record.data); } },
                    { text: '开始时间', dataIndex: 'start_ts', width: 180, sortable: false, renderer: alertTime },
                    { text: '操作人', dataIndex: 'username', width: 170, sortable: false, renderer: text },
                ],
                viewConfig: { stripeRows: true, enableTextSelection: true, emptyText: '暂无符合条件的操作记录', deferEmptyText: false },
                listeners: { itemdblclick: function (view, record) { showUpdateDetails(this, record); } },
            }],
            listeners: {
                activate: function (panel) {
                    panel._pveKomodoUpdatesActive = true;
                    loadUpdates(panel, panel._pveKomodoUpdatesPage || 0);
                },
                deactivate: leave, beforedestroy: leave,
            },
        };
    }

    const containerViewId = 'pve-komodo-containers';
    const containerTreeStates = new Set();

    function containerTreeUser(state) {
        const proxmox = window.Proxmox;
        if (!alive(state.native) || !alive(state.tree) || !alive(state.workspace) || !state.active ||
            state.workspace._pveKomodoLoggedOut || !proxmox || !proxmox.Utils || typeof proxmox.Utils.authOK !== 'function') return null;
        try {
            return proxmox.Utils.authOK() && typeof proxmox.UserName === 'string' && proxmox.UserName ? proxmox.UserName : null;
        } catch (error) { return null; }
    }
    function containerTreeCurrent(state, generation, user) {
        return state.generation === generation && containerTreeUser(state) === user;
    }
    function containerTreeStatus(state, message) {
        if (!alive(state.tree)) return;
        const label = state.tree.down('#pve-komodo-tree-status');
        if (alive(label)) {
            label.setText(text(message));
            const toolbar = typeof label.up === 'function' ? label.up('toolbar') : null;
            if (alive(toolbar)) toolbar.setVisible(!!message);
        }
    }
    function cancelContainerTree(state) {
        state.generation++;
        state.navigationGeneration++;
        const pending = state.requests.slice(); state.requests = [];
        if (state.navigationRequest) pending.push(state.navigationRequest);
        state.navigationRequest = null; state.loading = false;
        pending.forEach(function (request) { Ext.Ajax.abort(request); });
    }
    function emptyContainerTree(state) {
        state.inventory = null; state.links = null; state.linksKnown = false;
        if (alive(state.tree)) state.tree.setRootNode({ id: containerViewId + '-root', expanded: true, children: [] });
    }
    function rememberContainerTree(state) {
        if (!alive(state.tree)) return;
        const root = state.tree.getRootNode();
        if (root && root.childNodes.length) root.cascadeBy(function (node) {
            if (['server', 'stack', 'containers'].includes(node.get('pveKomodoKind'))) state.expanded.set(node.getId(), node.isExpanded());
        });
        const selected = state.tree.getSelectionModel().getSelection()[0];
        if (selected) state.selected = selected.getId();
    }
    function containerTreeIcon(value) {
        const status = typeof value === 'string' ? value.toLowerCase() : '';
        let nativeStatus = 'unknown';
        if (status === 'running' || status === 'paused') nativeStatus = status;
        else if (['created', 'exited', 'stopped'].includes(status)) nativeStatus = 'stopped';
        else if (['restarting', 'stopping', 'removing'].includes(status)) nativeStatus = 'pending';
        else if (status === 'dead') nativeStatus = 'error';
        return 'fa fa-cube ' + nativeStatus;
    }
    function renderContainerTree(state) {
        if (!state.inventory || containerTreeUser(state) !== state.user || state.authBlocked) return;
        rememberContainerTree(state);
        const query = String(state.tree.down('#pve-komodo-tree-search').getValue() || '').trim().toLowerCase();
        const links = new Map();
        if (state.linksKnown) state.links.links.forEach(function (link) {
            if (link && typeof link.server_id === 'string') links.set(link.server_id, link);
        });
        const servers = new Map();
        state.inventory.servers.forEach(function (server) {
            if (server && typeof server.id === 'string' && /^[0-9a-f]{24}$/i.test(server.id) && typeof server.name === 'string' &&
                server.template !== true && !servers.has(server.id)) servers.set(server.id, { server: server, stacks: new Map(), containers: [] });
        });
        (Array.isArray(state.inventory.stacks) ? state.inventory.stacks : []).forEach(function (stack) {
            if (stack && typeof stack.id === 'string' && /^[0-9a-f]{24}$/i.test(stack.id) && typeof stack.name === 'string' && servers.has(stack.server_id)) {
                const stacks = servers.get(stack.server_id).stacks;
                if (!stacks.has(stack.id)) stacks.set(stack.id, { id: 'stack:' + stack.server_id + ':' + stack.id,
                    text: stack.name, iconCls: 'fa fa-cubes', pveKomodoKind: 'stack', server_id: stack.server_id, stack_id: stack.id, children: [] });
            }
        });
        state.inventory.containers.forEach(function (row) {
            if (!row || typeof row.server_id !== 'string' || !servers.has(row.server_id) || typeof row.container_id !== 'string' ||
                !/^[0-9a-f]{64}$/i.test(row.container_id) || row.id !== row.server_id + ':' + row.container_id || typeof row.name !== 'string') return;
            const server = servers.get(row.server_id);
            const child = { id: 'container:' + row.id, text: row.name, leaf: true,
                iconCls: containerTreeIcon(row.state), pveKomodoKind: 'container', server_id: row.server_id, container_id: row.id, state: row.state,
                association: row.stack_association === 'unknown' ? '应用栈归属暂时无法核验' : row.stack_association === 'conflict' ? '应用栈归属存在冲突' : null };
            if (row.stack_association === 'matched' && server.stacks.has(row.stack_id)) server.stacks.get(row.stack_id).children.push(child);
            else {
                if (row.stack_association === 'matched') child.association = '应用栈归属暂时无法核验';
                server.containers.push(child);
            }
        });
        const children = [];
        servers.forEach(function (entry, serverId) {
            const link = links.get(serverId);
            const binding = state.linksKnown ? link ? guestLabel(link) : '未关联 CT 或 VM' : '关联状态未知';
            const guest = state.linksKnown && validGuest(link) && link.status === 'linked' && link.can_navigate === true;
            const guestName = guest && typeof link.guest_name === 'string' && link.guest_name.trim() ? link.guest_name : '未命名';
            // Reuse PVE's VM/CT icon and status classes. The green running
            // marker must describe the PVE guest, not a Komodo container.
            const guestStatus = guest && ['running', 'stopped', 'paused'].includes(link.guest_status) ? link.guest_status : 'unknown';
            const groups = Array.from(entry.stacks.values());
            if (entry.containers.length) groups.push({ id: 'containers:' + serverId, text: '容器', pveKomodoKind: 'containers',
                server_id: serverId, iconCls: 'fa fa-folder-open-o', children: entry.containers });
            groups.forEach(function (group) {
                group.leaf = false;
                group.expanded = state.expanded.has(group.id) ? state.expanded.get(group.id) : true;
                if (!group.children.length) group.children = [{ id: 'notice:' + group.id, text: '未取得可显示的容器（监控缓存）',
                    iconCls: 'fa fa-info-circle', pveKomodoKind: 'notice', leaf: true }];
            });
            children.push({ id: 'server:' + serverId,
                text: guest ? link.vmid + ' ' + guestName + ' [' + entry.server.name + ']' : entry.server.name,
                pveKomodoKind: 'server', server_id: serverId, binding: binding, bindingWarning: !guest,
                iconCls: guest ? 'fa ' + (link.guest_type === 'qemu' ? 'fa-desktop' : 'fa-cube') + ' ' + guestStatus : 'fa fa-server', leaf: false,
                expanded: state.expanded.has('server:' + serverId) ? state.expanded.get('server:' + serverId) : true,
                children: groups.length ? groups : [{ id: 'notice:' + serverId, text: '未取得可显示的应用栈或容器（监控缓存）',
                    iconCls: 'fa fa-info-circle', pveKomodoKind: 'notice', leaf: true }],
            });
        });
        function filtered(node) {
            if (!query || [node.text, node.state].join(' ').toLowerCase().includes(query)) return node;
            const matches = (node.children || []).map(filtered).filter(Boolean);
            return matches.length ? Object.assign({}, node, { children: matches }) : null;
        }
        state.tree.setRootNode({ id: containerViewId + '-root', expanded: true, children: children.map(filtered).filter(Boolean) });
        if (state.selected) {
            const selected = state.tree.getStore().getNodeById(state.selected);
            if (selected) state.tree.getSelectionModel().select(selected);
        }
    }
    function containerTreeAuthFailure(state, response) {
        if (!response || response.status !== 401 && response.status !== 403) return false;
        cancelContainerTree(state); state.authBlocked = true; emptyContainerTree(state);
        containerTreeStatus(state, response.status === 401 ? 'PVE 未登录或会话已过期' : '无访问权限');
        state.tree.down('#pve-komodo-tree-refresh').setDisabled(false);
        return true;
    }
    function loadContainerTree(state) {
        const user = containerTreeUser(state);
        if (!user) { cancelContainerTree(state); emptyContainerTree(state); containerTreeStatus(state, '请先登录 PVE'); return; }
        if (state.user === user) rememberContainerTree(state);
        else { state.expanded.clear(); state.selected = null; }
        cancelContainerTree(state); emptyContainerTree(state);
        state.user = user; state.authBlocked = false; state.loading = true;
        const generation = state.generation;
        const results = {};
        state.tree.down('#pve-komodo-tree-refresh').setDisabled(true);
        containerTreeStatus(state, '正在读取 Docker 容器与服务器关联…');
        function finish() {
            if (!results.containers || !results.links || !containerTreeCurrent(state, generation, user)) return;
            state.loading = false; state.tree.down('#pve-komodo-tree-refresh').setDisabled(false);
            if (!results.containers.ok) { containerTreeStatus(state, '容器数据读取失败，请刷新后重试'); return; }
            state.inventory = results.containers.data;
            state.linksKnown = results.links.ok && !(Array.isArray(results.links.data.errors) && results.links.data.errors.length);
            state.links = state.linksKnown ? results.links.data : null;
            renderContainerTree(state);
            const notices = [];
            if (Array.isArray(state.inventory.errors) && state.inventory.errors.length) notices.push('部分容器数据不可用');
            if (!state.linksKnown) notices.push('关联状态未知');
            containerTreeStatus(state, notices.join('；'));
        }
        [['containers', '/pve-komodo/api/containers'], ['links', '/pve-komodo/api/guest-links']].forEach(function (entry) {
            if (!containerTreeCurrent(state, generation, user)) return;
            let request, completed = false;
            function complete(options, success, response) {
                completed = true;
                if (request) state.requests = state.requests.filter(function (pending) { return pending !== request; });
                if (!containerTreeCurrent(state, generation, user) || containerTreeAuthFailure(state, response)) return;
                let data, ok = false;
                try {
                    data = JSON.parse(response.responseText);
                    ok = success && data && (entry[0] === 'containers' ? Array.isArray(data.containers) && Array.isArray(data.servers) : Array.isArray(data.links));
                } catch (error) { /* Preserve readable container data when only associations fail. */ }
                results[entry[0]] = { ok: !!ok, data: data }; finish();
            }
            try {
                request = Ext.Ajax.request({ url: entry[1], method: 'GET', disableCaching: false, timeout: 60000, callback: complete });
                if (!completed) state.requests.push(request);
            } catch (error) { complete(null, false, null); }
        });
    }
    function selectContainerTreeNode(state, record) {
        const user = containerTreeUser(state);
        if (!user || state.authBlocked || state.loading || !state.inventory) return;
        if (state.user !== user) { cancelContainerTree(state); emptyContainerTree(state); containerTreeStatus(state, '会话已变化，请刷新容器视图'); return; }
        if (dockerNavigationBusy()) { containerTreeStatus(state, '操作正在提交或等待确认，请完成后再定位资源'); return; }
        const kind = record.get('pveKomodoKind'), serverId = record.get('server_id');
        state.navigationGeneration++;
        if (state.navigationRequest) Ext.Ajax.abort(state.navigationRequest);
        state.navigationRequest = null;
        requestedGuestServer = null;
        if (typeof serverId !== 'string' || !/^[0-9a-f]{24}$/i.test(serverId)) return;
        if (kind === 'container') {
            const id = record.get('container_id');
            if (typeof id === 'string' && new RegExp('^' + serverId + ':[0-9a-f]{64}$', 'i').test(id)) {
                openContainerManagement(state.workspace, serverId, id.slice(serverId.length + 1), record.get('text'));
            }
            return;
        }
        if (kind === 'stack') {
            const stackId = record.get('stack_id');
            if (typeof stackId === 'string' && /^[0-9a-f]{24}$/i.test(stackId)) openStackManagement(state.workspace, stackId, serverId, record.get('text'));
            return;
        }
        if (kind === 'containers') {
            if (record.isExpanded()) record.collapse(); else record.expand();
            return;
        }
        if (kind !== 'server') return;
        const generation = state.generation, navigation = state.navigationGeneration;
        let completed = false;
        containerTreeStatus(state, '正在重新核验服务器关联…');
        function complete(options, success, response) {
            completed = true;
            if (!containerTreeCurrent(state, generation, user) || navigation !== state.navigationGeneration ||
                containerTreeAuthFailure(state, response)) return;
            state.navigationRequest = null;
            if (dockerNavigationBusy()) { containerTreeStatus(state, '操作正在提交或等待确认，请完成后再定位资源'); return; }
            let link;
            try {
                const data = JSON.parse(response.responseText);
                if (!success || !data || !Array.isArray(data.links) || Array.isArray(data.errors) && data.errors.length) throw new Error('links-unavailable');
                link = data.links.find(function (candidate) { return candidate && candidate.server_id === serverId; });
                if (validGuest(link) && link.status === 'linked' && link.can_navigate === true) {
                    const nativeId = link.guest_type + '/' + link.vmid;
                    const base = '/pve-komodo/api/guests/' + link.guest_type + '/' + link.vmid;
                    requestedGuestServer = { base: base, server_id: serverId, user: user };
                    // A dedicated container/stack page can replace the guest
                    // while the hidden native tree still selects that guest.
                    const nativeSelection = state.native.getSelectionModel();
                    if (typeof nativeSelection.deselectAll === 'function' && nativeSelection.getSelection().some(function (record) { return record.get('id') === nativeId; })) nativeSelection.deselectAll();
                    const selected = state.native.selectById(nativeId);
                    if (selected) {
                        const config = state.workspace.query('pvePanelConfig').filter(function (candidate) {
                            const node = candidate.pveSelNode && candidate.pveSelNode.data;
                            return alive(candidate) && node && (node.id === nativeId || node.type === link.guest_type && Number(node.vmid) === link.vmid);
                        }).pop();
                        if (config && typeof config.selectById === 'function') {
                            config.selectById(guestItemId);
                            if (alive(activeGuestSummary) && activeGuestSummary.pveKomodoGuestBase === base &&
                                activeGuestSummary._pveKomodoExpectedServer !== serverId) {
                                activeGuestSummary._pveKomodoExpectedServer = serverId;
                                requestedGuestServer = null;
                                loadGuestSummary(activeGuestSummary);
                            }
                            return;
                        }
                    }
                    openDockerDestination(state.workspace, serverId, null, '关联的 VM/CT 当前不可访问，已转到 Komodo 服务器');
                    return;
                }
            } catch (error) {
                openDockerDestination(state.workspace, serverId, null, '关联状态暂时无法核验，已转到 Komodo 服务器'); return;
            }
            if (!link) openServerManagement(state.workspace, serverId, record.get('text'));
            else openDockerDestination(state.workspace, serverId, null, '关联待确认或客体不可访问，已转到 Komodo 服务器');
        }
        try {
            const request = Ext.Ajax.request({ url: '/pve-komodo/api/guest-links', method: 'GET', disableCaching: false, timeout: 60000, callback: complete });
            if (!completed) state.navigationRequest = request;
        } catch (error) { complete(null, false, null); }
    }
    function ensureContainerTree(native) {
        if (native._pveKomodoContainerTree) return native._pveKomodoContainerTree;
        const owner = native.ownerCt, workspace = native.up('pveStdWorkspace');
        if (!owner || typeof owner.add !== 'function' || !workspace) return null;
        const state = { native: native, workspace: workspace, tree: null, active: false, generation: 0, navigationGeneration: 0,
            requests: [], expanded: new Map(), selected: null, user: null, inventory: null, links: null };
        const tree = Ext.create('Ext.tree.Panel', { itemId: 'pve-komodo-container-tree', flex: 1, hidden: true, border: false,
            rootVisible: false, useArrows: true, stateful: false, animate: false,
            store: { model: 'Ext.data.TreeModel', autoDestroy: true, root: { id: containerViewId + '-root', expanded: true, children: [] } },
            columns: [{ xtype: 'treecolumn', dataIndex: 'text', flex: 1, sortable: false, renderer: function (value, meta, record) {
                let rendered = text(value);
                if (record.get('pveKomodoKind') === 'server' && record.get('bindingWarning')) rendered += ' · ' + text(record.get('binding'));
                if (record.get('pveKomodoKind') === 'container' && meta) meta.tdAttr = 'data-qtip="' + text(stateText(record.get('state'))) + '"';
                if (record.get('association')) rendered += ' · ' + text(record.get('association'));
                return rendered;
            } }], hideHeaders: true,
            viewConfig: { enableTextSelection: true, emptyText: '未取得可显示的数据；服务器可能离线或没有权限', deferEmptyText: false },
            tbar: [{ text: '刷新', itemId: 'pve-komodo-tree-refresh', handler: function () { loadContainerTree(state); } },
                { text: '新建', itemId: 'pve-komodo-tree-new', handler: function () { openServerCreate(workspace); } },
                { xtype: 'textfield', itemId: 'pve-komodo-tree-search', emptyText: '搜索服务器或容器', flex: 1,
                    listeners: { change: function () { if (state.inventory) renderContainerTree(state); } } }],
            bbar: [{ xtype: 'tbtext', itemId: 'pve-komodo-tree-status', text: '' }],
            listeners: { itemclick: function (view, record, item, index, event) {
                // TreeView toggles expander clicks itself after itemclick.
                if (event && typeof event.getTarget === 'function' && event.getTarget(view.expanderSelector || '.x-tree-expander')) return;
                selectContainerTreeNode(state, record);
            } },
        });
        state.tree = tree; native._pveKomodoContainerTree = state; containerTreeStates.add(state); owner.add(tree);
        native.on('beforedestroy', function () {
            state.active = false; cancelContainerTree(state); containerTreeStates.delete(state);
            if (alive(tree)) tree.destroy();
        });
        return state;
    }
    function switchContainerTree(native, view) {
        const state = ensureContainerTree(native);
        if (!state) return;
        const active = view && view.id === containerViewId;
        state.active = !!active;
        native.setVisible(!active); state.tree.setVisible(!!active);
        if (active) loadContainerTree(state);
        else { rememberContainerTree(state); cancelContainerTree(state); emptyContainerTree(state); }
    }
    function installContainerView() {
        const Selector = Ext.ClassManager.get('PVE.form.ViewSelector');
        const Tree = Ext.ClassManager.get('PVE.tree.ResourceTree');
        const Workspace = Ext.ClassManager.get('PVE.StdWorkspace');
        if (!Selector || !Tree || !Workspace || !Selector.prototype || !Tree.prototype || !Workspace.prototype ||
            typeof Selector.prototype.initComponent !== 'function' || typeof Tree.prototype.initComponent !== 'function' ||
            typeof Workspace.prototype.onLogin !== 'function') return;
        Ext.define('PVEKomodo.override.ViewSelector', { override: 'PVE.form.ViewSelector', initComponent: function () {
            const result = this.callParent(arguments), store = this.getStore();
            if (!store.findRecord('key', containerViewId, 0, false, true, true)) store.add({ key: containerViewId, value: '容器视图' });
            return result;
        } });
        Ext.define('PVEKomodo.override.ResourceTree', { override: 'PVE.tree.ResourceTree', initComponent: function () {
            const result = this.callParent(arguments), native = this, original = this.setViewFilter;
            if (typeof original !== 'function') return result;
            this.setViewFilter = function (view) {
                let returned;
                if (view && view.id === containerViewId) {
                    const current = native.viewFilter;
                    const fullServerFilter = current && Array.isArray(current.groups) && current.groups.length === 1 &&
                        current.groups[0] === 'node' && !current.getFilterFn && !current.itemMap;
                    if (!fullServerFilter) {
                        const selection = native.getSelectionModel();
                        selection.suspendEvents(false);
                        try { returned = original.call(this, { id: containerViewId, groups: ['node'] }); }
                        finally { selection.resumeEvents(); }
                    }
                } else returned = original.apply(this, arguments);
                switchContainerTree(native, view);
                return returned;
            };
            this.on('afterrender', function () { switchContainerTree(native, native.viewFilter); });
            return result;
        } });
        Ext.define('PVEKomodo.override.ContainerWorkspace', { override: 'PVE.StdWorkspace', onLogin: function (loginData) {
            const workspace = this;
            this._pveKomodoLoggedOut = !loginData;
            if (!loginData) {
                requestedServer = null; requestedContainer = null; requestedNavigationNotice = null; requestedGuestServer = null;
                if (serverCreateState && serverCreateState.workspace === workspace && alive(serverCreateState.dialog)) serverCreateState.dialog.destroy();
                if (alive(activeGuestSummary)) {
                    const owner = guestOwner(activeGuestSummary);
                    if (owner && owner.workspace === workspace) {
                        const panel = activeGuestSummary; panel._pveKomodoSummaryActive = false; cancelGuestSummary(panel); clearSummary(panel); clearGuestHistory(panel);
                        summaryStatus(panel, '请先登录 PVE'); summaryControls(panel); activeGuestSummary = null;
                    }
                }
                if (alive(activeGuestOptions)) {
                    const owner = guestOwner(activeGuestOptions);
                    if (owner && owner.workspace === workspace) {
                        const panel = activeGuestOptions; panel._pveKomodoOptionsActive = false; cancelOptions(panel); panel._pveKomodoOptionsData = null;
                        panel.down('#pve-komodo-guest-options-grid').getStore().loadData([]); optionStatus(panel, '请先登录 PVE'); optionControls(panel); activeGuestOptions = null;
                    }
                }
                containerTreeStates.forEach(function (state) {
                    if (state.workspace !== workspace) return;
                    cancelContainerTree(state); emptyContainerTree(state); state.user = null; state.selected = null;
                    state.expanded.clear(); state.authBlocked = true;
                    containerTreeStatus(state, '请先登录 PVE');
                });
                stackPages.forEach(function (page) {
                    if (page.workspace === workspace) { page._pveKomodoStackActive = false; cancelStackPage(page); stackStatus(page, '请先登录 PVE'); }
                });
                guestInventoryPages.forEach(function (page) {
                    if (page.workspace === workspace) { page._pveKomodoInventoryActive = false; cancelInventory(page); inventoryStatus(page, '请先登录 PVE'); inventoryControls(page); }
                });
            }
            const result = this.callParent(arguments);
            if (loginData) containerTreeStates.forEach(function (state) { if (state.workspace === workspace && state.active) loadContainerTree(state); });
            return result;
        } });
    }

    let activeStackPage = null;
    let stackWriteState = null;
    const stackPages = new Set();
    const stackActions = [
        ['deploy', '部署', '部署应用栈，容器可能被创建或重建，相关服务可能中断。'],
        ['pull', '拉取镜像', '为应用栈拉取镜像。'],
        ['restart', '重启', '重启应用栈容器，相关服务将短暂中断。'],
        ['pause', '暂停', '冻结应用栈容器进程，相关服务将无法响应。'],
        ['stop', '停止', '停止应用栈容器，会中断相关服务。'],
        ['destroy', '销毁', '停止并移除应用栈容器与网络；容器可写层中的数据会被删除，默认不删除存储卷。'],
    ];
    // Match the requested configuration sections. Unlisted backend fields stay
    // untouched because every edit submits only the selected field.
    const stackConfigSections = [
        ['general', '常规', [
            ['server', '服务器', [['server_id', '服务器', 'servers']]],
            ['files', '文件', [['run_directory', '运行目录', 'string'], ['file_paths', '文件路径', 'list']]],
            ['environment', '环境变量', [['environment', '环境变量', 'code']]],
            ['config-files', '配置文件', [['config_files', '配置文件', 'configfiles']]],
            ['auto-update', '自动更新', [
                ['poll_for_updates', '检查更新', 'boolean'], ['auto_update_skip_services', '自动更新时忽略服务', 'service-list'],
                ['auto_update', '自动更新', 'boolean'], ['auto_update_all_services', '完整应用栈自动更新', 'boolean'],
            ]],
            ['links', '链接', [['links', '链接', 'list']]],
        ]],
        ['advanced', '高级', [
            ['project', '项目名称', [['project_name', '项目名称', 'string']]],
            ['pre-deploy', '部署前操作', [
                ['pre_deploy.path', '路径', 'string'], ['pre_deploy.shell_mode', '以 Shell 模式运行（sh -c）', 'boolean'], ['pre_deploy.command', '命令', 'code'],
            ]],
            ['post-deploy', '部署后操作', [
                ['post_deploy.path', '路径', 'string'], ['post_deploy.shell_mode', '以 Shell 模式运行（sh -c）', 'boolean'], ['post_deploy.command', '命令', 'code'],
            ]],
            ['command-wrapper', '命令包装器', [['compose_cmd_wrapper', '命令包装器', 'code'], ['compose_cmd_wrapper_include', '应用范围', 'command-list']]],
            ['extra-args', '额外参数', [['extra_args', '额外参数', 'list']]],
            ['ignore-services', '忽略服务', [['ignore_services', '忽略服务', 'list']]],
            ['pull', '拉取镜像', [['registry_provider', '镜像仓库', 'string'], ['auto_pull', '部署前拉取镜像', 'boolean']]],
            ['build', '构建镜像', [['run_build', '部署前构建镜像', 'boolean']]],
            ['destroy', '销毁', [['destroy_before_deploy', '部署前销毁', 'boolean']]],
        ]],
    ];
    function stackSectionDescription(section, data) {
        const swarm = !!data.config.swarm_id, deploy = swarm ? 'docker stack deploy' : 'docker compose up';
        const descriptions = {
            server: '选择用于部署的服务器。',
            files: '设置运行 compose up 命令时的工作目录；添加通过 docker compose -f 使用的文件。文件路径相对于运行目录，留空时使用 compose.yaml。',
            environment: '将这些变量传入 docker compose 命令。',
            'config-files': '添加应用栈关联的其他配置文件，并在界面中编辑。路径相对于运行目录。',
            'auto-update': '在全局自动更新时检查镜像更新；发现新镜像时触发重新部署。忽略服务只影响全局检查，手动检查仍包含全部服务。',
            links: '在资源页顶部添加快捷链接。',
            project: '可选。设置其他 Compose 项目名称。导入现有应用栈时，需与主机上的 Compose 项目名称一致。',
            'pre-deploy': '运行 ' + deploy + ' 前执行 Shell 命令。路径相对于运行目录。',
            'post-deploy': '运行 ' + deploy + ' 后执行 Shell 命令。路径相对于运行目录。',
            'command-wrapper': '可选。为密钥管理工具配置命令包装器，并选择需要包装的子命令。',
            'extra-args': '传递额外参数给 ' + deploy + '。',
            'ignore-services': '若 Compose 文件中的初始化服务会提前退出，可在此忽略它们，以正确显示应用栈健康状态。',
            pull: swarm ? '登录镜像仓库以访问私有镜像。' : '登录镜像仓库以访问私有镜像；重新部署应用栈前运行 docker compose pull。也可在 Compose 文件中设置 pull_policy。',
            build: '重新部署应用栈前运行 docker compose build。也可将 --build 添加为额外参数。',
            destroy: '重新部署应用栈前运行 ' + (swarm ? 'docker stack rm' : 'docker compose down') + '。',
        };
        return descriptions[section] || '';
    }
    // Stack and container cards share the authenticated detail transport and
    // terminal lifecycle, while retaining independent native ConfigPanels.
    function stackRoot(component) { return component.up('[pveKomodoStackPage]') || component.up('[pveKomodoContainerPage]') || component.up('[pveKomodoServerTerminalPage]'); }
    function stackUser(page) {
        const proxmox = window.Proxmox;
        if (!alive(page) || !page._pveKomodoStackActive || page.workspace && page.workspace._pveKomodoLoggedOut ||
            !proxmox || !proxmox.Utils || typeof proxmox.Utils.authOK !== 'function') return null;
        return proxmox.Utils.authOK() && proxmox.UserName || null;
    }
    function stackStatus(page, value) {
        const label = page.down('#pve-komodo-stack-status'); if (alive(label)) label.setText(text(value));
    }
    function stackError(response, fallback) {
        let code;
        try { code = JSON.parse(response.responseText).detail; } catch (error) { /* Fixed messages only. */ }
        if (response && response.status === 403 && code === 'invalid_csrf_token') return '会话校验已更新，本次未提交，请先刷新应用栈页面再操作';
        if (response && response.status === 404) return '应用栈或所选服务已不存在，请刷新容器视图';
        if (response && response.status === 409 && !['action_already_requested', 'idempotency_conflict'].includes(code)) return '应用栈配置或所在服务器已变化，请刷新后重新确认';
        return optionsError(response, fallback);
    }
    function stackControls(page) {
        if (!alive(page)) return;
        if (page.pveKomodoServerTerminalPage) { serverTerminalControls(page); return; }
        if (page.pveKomodoContainerPage) { containerDetailControls(page); return; }
        const data = page._pveKomodoStackData;
        const busy = optionBusy() || !!page._pveKomodoStackConfirmation;
        stackActions.forEach(function (action) {
            const button = page.down('#pve-komodo-stack-' + action[0]);
            if (alive(button)) button.setDisabled(busy || !data || !data.can_execute || !stackUser(page));
        });
        const refresh = page.down('#pve-komodo-stack-refresh');
        if (alive(refresh)) refresh.setDisabled(busy || !!page._pveKomodoStackLoading);
        const tab = page._pveKomodoStackTab;
        if (!alive(tab)) return;
        ['edit', 'edit-file', 'new-terminal'].forEach(function (name) {
            const button = tab.down('#pve-komodo-stack-' + name);
            if (alive(button)) button.setDisabled(busy || !data || name !== 'new-terminal' && !data.can_write ||
                name === 'edit' && !tab.down('#pve-komodo-stack-config-grid').getSelectionModel().getSelection().some(function (row) { return row.get('available'); }) ||
                name === 'edit-file' && !(tab._pveKomodoFile && tab._pveKomodoFile.editable && !tab._pveKomodoFile.redacted) ||
                name === 'new-terminal' && !(tab._pveKomodoTerminalServices && tab._pveKomodoTerminalServices.services.some(function (service) { return service.can_open; })));
        });
    }
    function resetStackTerminal(tab) {
        tab._pveKomodoTerminalAttempt = (tab._pveKomodoTerminalAttempt || 0) + 1;
        if (tab._pveKomodoTerminalLayoutCleanup) { tab._pveKomodoTerminalLayoutCleanup(); tab._pveKomodoTerminalLayoutCleanup = null; }
        const fullscreen = tab.down('#pve-komodo-stack-terminal-fullscreen');
        if (alive(fullscreen)) { fullscreen.setDisabled(true); fullscreen.setText('全屏'); }
        if (tab._pveKomodoSocket) { tab._pveKomodoSocket.close(1000, 'view-closed'); tab._pveKomodoSocket = null; }
        if (tab._pveKomodoTerminal) { tab._pveKomodoTerminal.dispose(); tab._pveKomodoTerminal = null; }
        return tab._pveKomodoTerminalAttempt;
    }
    function cancelStackTab(tab) {
        tab._pveKomodoStackTabGeneration = (tab._pveKomodoStackTabGeneration || 0) + 1;
        tab._pveKomodoStackTabActive = false;
        if (tab._pveKomodoStackRead) Ext.Ajax.abort(tab._pveKomodoStackRead);
        tab._pveKomodoStackRead = null;
        if (tab._pveKomodoLogTimer !== undefined) window.clearTimeout(tab._pveKomodoLogTimer);
        tab._pveKomodoLogTimer = undefined;
        resetStackTerminal(tab);
        if (alive(tab._pveKomodoTerminalDialog)) tab._pveKomodoTerminalDialog.destroy();
        if (tab.pveKomodoContainerTab) {
            tab._pveKomodoUpdatesActive = false; closeUpdateDetail(tab);
            // PVE removes the old card from its owner before beforedestroy.
            // Do not interrupt navigation and leave the parent layout suspended.
            const page = stackRoot(tab); if (alive(page)) closeOptionEditor(page);
        }
    }
    function cancelStackPage(page) {
        page._pveKomodoStackGeneration = (page._pveKomodoStackGeneration || 0) + 1;
        if (page._pveKomodoStackRead) Ext.Ajax.abort(page._pveKomodoStackRead);
        page._pveKomodoStackRead = null; page._pveKomodoStackData = null; page._pveKomodoStackLoading = false;
        if (alive(page._pveKomodoStackTab)) cancelStackTab(page._pveKomodoStackTab);
        if (page._pveKomodoStackConfirmation && alive(page._pveKomodoStackConfirmation.dialog)) page._pveKomodoStackConfirmation.dialog.destroy();
        page._pveKomodoStackConfirmation = null; closeOptionEditor(page);
    }
    function stackRequest(page, suffix, params, callback, tab) {
        const user = stackUser(page), generation = tab ? tab._pveKomodoStackTabGeneration : page._pveKomodoStackGeneration;
        const pageGeneration = page._pveKomodoStackGeneration;
        if (!user) return;
        let completed = false;
        function complete(options, success, response) {
            completed = true;
            if (stackUser(page) !== user || page._pveKomodoStackGeneration !== pageGeneration ||
                tab && (!alive(tab) || !tab._pveKomodoStackTabActive || tab._pveKomodoStackTabGeneration !== generation) ||
                !tab && page._pveKomodoStackGeneration !== generation) return;
            if (tab) tab._pveKomodoStackRead = null; else page._pveKomodoStackRead = null;
            let data;
            try { data = JSON.parse(response.responseText); } catch (error) { /* Fixed errors only. */ }
            callback(success, data, response);
        }
        try {
            const request = Ext.Ajax.request({ url: page.pveKomodoStackPath + suffix, method: 'GET', disableCaching: false,
                params: params, timeout: 60000, callback: complete });
            if (!completed) { if (tab) tab._pveKomodoStackRead = request; else page._pveKomodoStackRead = request; }
        } catch (error) { complete(null, false, null); }
    }
    function loadStackDetails(page) {
        if (!stackUser(page) || page._pveKomodoStackLoading) return;
        page._pveKomodoStackGeneration = (page._pveKomodoStackGeneration || 0) + 1;
        page._pveKomodoStackLoading = true; page._pveKomodoStackData = null;
        const tab = page._pveKomodoStackTab;
        if (alive(tab)) {
            cancelStackTab(tab); tab._pveKomodoStackTabActive = true;
            tab._pveKomodoTerminalServices = null; tab._pveKomodoFile = null; tab._pveKomodoLog = null;
            const grid = tab.down('#pve-komodo-stack-config-grid'); if (alive(grid)) grid.getStore().loadData([]);
            ['compose', 'deployed'].forEach(function (name) { const field = tab.down('#pve-komodo-stack-' + name); if (alive(field)) field.setValue(''); });
            const output = tab.down('#pve-komodo-stack-log-output'); if (alive(output)) output.update('');
        }
        stackStatus(page, '正在读取应用栈…'); stackControls(page);
        stackRequest(page, '', undefined, function (success, data, response) {
            page._pveKomodoStackLoading = false;
            let message = stackError(response, '应用栈读取失败，请刷新后重试');
            if (success && data && data.stack_id === page.pveKomodoStackId && data.server_id === page._pveKomodoStackExpectedServer &&
                typeof data.version === 'string' && /^[0-9a-f]{64}$/i.test(data.version) && data.config &&
                typeof data.config === 'object' && !Array.isArray(data.config) && Array.isArray(data.files) && Array.isArray(data.services) &&
                typeof data.csrf_token === 'string' && data.csrf_token && !/[\r\n]/.test(data.csrf_token)) {
                page._pveKomodoStackData = data;
                message = '应用栈：' + (data.stack_name || data.stack_id) + '；' + (data.can_write ? '配置可编辑' : '配置只读');
                if (alive(page._pveKomodoStackTab)) renderStackTab(page._pveKomodoStackTab, data);
            } else if (success && data && data.server_id !== page._pveKomodoStackExpectedServer) message = '应用栈所在服务器已变化，请刷新容器视图后重新打开';
            stackStatus(page, message); stackControls(page);
        });
    }
    function stackConfirm(page, title, effect, payload, endpoint, after) {
        const data = page._pveKomodoStackData, user = stackUser(page);
        if (!data || !user || optionBusy() || page._pveKomodoStackConfirmation) return;
        const requestId = uuid(); if (!requestId) { stackStatus(page, '无法生成安全请求标识，本次未提交'); return; }
        const generation = page._pveKomodoStackGeneration;
        const frozen = copyOption(Object.assign({}, payload, { request_id: requestId }));
        const dialog = Ext.create('Ext.window.MessageBox'); const confirmation = { dialog: dialog };
        page._pveKomodoStackConfirmation = confirmation;
        dialog.show({ title: title, buttons: Ext.Msg.YESNO, icon: Ext.Msg.WARNING,
            message: text((page.pveKomodoContainerPage ? '容器：' + data.container_name + '；服务器：' + (data.server_name || data.server_id) :
                '应用栈：' + (data.stack_name || data.stack_id)) + '。' + effect), fn: function (answer) {
                if (page._pveKomodoStackConfirmation !== confirmation) return;
                page._pveKomodoStackConfirmation = null; if (alive(dialog)) dialog.destroy();
                if (stackUser(page) !== user || page._pveKomodoStackData !== data || page._pveKomodoStackGeneration !== generation || optionBusy()) return;
                if (answer !== 'yes') { stackControls(page); return; }
                const state = { pending: true }; stackWriteState = state;
                closeOptionEditor(page); stackStatus(page, '正在提交，请勿重复操作…'); stackControls(page);
                let finished = false;
                function complete(options, success, response) {
                    if (finished) return; finished = true; state.pending = false;
                    let result, message = '结果尚未确认，请先到 Komodo 的操作记录核对，不要重复提交', completed = false;
                    try {
                        result = JSON.parse(response.responseText);
                        if (endpoint) {
                            const entry = result && Array.isArray(result.results) && result.results.length === 1 && result.results[0];
                            const identifier = page.pveKomodoContainerPage ? data.server_id + ':' + data.container_id : data.stack_id;
                            if (success && result.section === (page.pveKomodoContainerPage ? 'containers' : 'stacks') && result.action === frozen.action && entry && entry.id === identifier &&
                                ['submitted', 'completed', 'failed', 'outcome_unknown', 'not_submitted'].includes(entry.status) &&
                                entry.ok === ['submitted', 'completed'].includes(entry.status)) {
                                if (entry.status === 'submitted') message = '操作已提交，尚未确认任务完成，请手动刷新观察';
                                if (entry.status === 'completed') { message = '操作已完成'; completed = true; }
                                if (entry.status === 'failed') message = '操作失败，请核对操作记录';
                            }
                        } else if (success && result.ok === true && result.status === 'completed') { completed = true; message = page.pveKomodoContainerPage ? '服务器描述已保存' : '保存已完成，请核对应用栈状态与操作记录'; }
                        else if (result.status === 'failed') message = '保存失败，请刷新后核对';
                        if (!success && response && [401, 403, 404, 409, 422].includes(response.status)) message = page.pveKomodoContainerPage ?
                            containerDetailError(response, '请求校验失败，本次未提交') : stackError(response, '请求校验失败，本次未提交');
                    } catch (error) { /* A submitted write is never retried. */ }
                    if (stackUser(page) === user) {
                        page._pveKomodoStackData = null;
                        if (completed && !endpoint) {
                            if (frozen.config && typeof frozen.config.server_id === 'string') page._pveKomodoStackExpectedServer = frozen.config.server_id;
                            if (page.pveKomodoContainerPage) loadContainerDetails(page); else loadStackDetails(page);
                        }
                        stackStatus(page, message); stackControls(page);
                        if (after) after(completed, result);
                    }
                    if (alive(activeStackPage)) stackControls(activeStackPage);
                    if (alive(activeDashboard)) updateControls(activeDashboard);
                    if (alive(activeGuestOptions)) optionControls(activeGuestOptions);
                    if (alive(activeGuestSummary)) summaryControls(activeGuestSummary);
                }
                try { Ext.Ajax.request({ url: endpoint || page.pveKomodoStackPath, method: 'POST', timeout: 120000,
                    headers: { 'X-PVE-Komodo-CSRF': data.csrf_token }, jsonData: frozen, callback: complete }); }
                catch (error) { complete(null, false, null); }
            } });
        stackControls(page);
    }
    function saveStack(page, kind, changes) {
        const data = page._pveKomodoStackData;
        if (!data || !data.can_write) return;
        const effectLabels = { config_source_changes_may_auto_deploy: '修改配置来源可能按自动更新策略触发部署',
            source_file_changes_may_auto_deploy: '修改来源文件可能按自动更新策略触发部署',
            git_file_save_commits_and_pushes: 'Git 文件保存会尝试提交并推送代码仓库' };
        const effects = Array.isArray(data.save_effects) ? data.save_effects.map(function (value) { return effectLabels[value]; }).filter(Boolean).join('；') : '';
        stackConfirm(page, '确认保存', effects + '。保存可能按应用栈策略触发检查或部署。' +
            (kind === 'file' && data.source_mode === 'git' ? 'Git 文件保存可能提交并推送代码仓库。' : '') +
            (kind === 'file' && data.files.some(function (file) { return file.path === changes.file_path && typeof file.contents !== 'string'; }) ?
                '原文件未能读取；保存将初始化或覆盖该路径文件，原内容尚未确认。' : ''),
        Object.assign({ action: kind, server_id: data.server_id, base_version: data.version }, changes));
    }
    function stackFieldAvailable(value, kind) {
        return kind === 'boolean' ? typeof value === 'boolean' : ['envfiles', 'configfiles', 'list', 'service-list', 'command-list'].includes(kind) ? Array.isArray(value) : typeof value === 'string';
    }
    function stackFieldText(value, meta, record) {
        const kind = record.get('kind');
        if (kind === 'heading') return record.get('heading') === 'minor' && record.get('description') ?
            '<span class="pve-komodo-stack-heading-description">' + text(record.get('description')) + '</span>' : '';
        if (['servers', 'swarms', 'repos'].includes(kind) && typeof record.get('reference_name') === 'string') {
            if (meta) meta.tdAttr = 'data-qtip="' + text(value) + '"';
            return text(record.get('reference_name'));
        }
        if (kind === 'secret') return value ? '已配置' : '未配置';
        if (kind === 'code') return typeof value === 'string' ? value.length + ' 个字符' : '-';
        if (kind === 'envfiles' || kind === 'configfiles') return Array.isArray(value) ? value.length + ' 项' : '-';
        if (['service-list', 'command-list'].includes(kind)) return optionValue(value, 'list');
        return optionValue(value, kind);
    }
    function stackScalarEditor(page, record) {
        const data = page._pveKomodoStackData;
        if (!data || !data.can_write || optionBusy() || !record || !record.get('available')) return;
        const key = record.get('key'), kind = record.get('kind'), initial = copyOption(record.get('value'));
        const [parent, member] = key.split('.');
        if (kind === 'envfiles' || kind === 'configfiles') { stackListEditor(page, record); return; }
        const choiceField = ['service-list', 'command-list'].includes(kind);
        const commands = data.config.swarm_id ? ['config', 'deploy'] : ['config', 'build', 'pull', 'up', 'run'];
        // Service names may disappear; command choices follow the native fixed enum.
        const choices = kind === 'command-list' ? commands : kind === 'service-list' ? Array.from(new Set((Array.isArray(data.service_choices) ? data.service_choices : [])
            .concat(initial).filter(function (name) { return typeof name === 'string' && name !== ''; }))) : [];
        const fields = [{ key: 'value', label: record.get('label'), kind: kind,
            value: kind === 'command-list' ? initial.filter(function (name) { return commands.includes(name); }) : initial }];
        const items = fields.map(function (field) {
            const input = { xtype: field.kind === 'boolean' ? 'checkbox' : ['code', 'list'].includes(field.kind) ? 'textareafield' : 'textfield',
                itemId: 'pve-komodo-stack-input-' + field.key, fieldLabel: field.label };
            if (input.xtype === 'textareafield') input.height = 240;
            if (field.kind === 'secret') input.inputType = 'password';
            if (['servers', 'swarms', 'repos'].includes(field.kind)) Object.assign(input, { xtype: 'combobox', queryMode: 'local', editable: true,
                valueField: 'id', displayField: 'name', store: { autoDestroy: true, fields: ['id', 'name'], data: data.selectors && data.selectors[field.kind] || [] },
                listConfig: { itemTpl: '{name:htmlEncode}' } });
            if (choiceField) Object.assign(input, { xtype: 'combobox', queryMode: 'local', multiSelect: true,
                editable: true, forceSelection: true, autoSelect: false, minChars: 0, queryDelay: 0, emptyText: kind === 'command-list' ? '选择命令' : '选择服务',
                valueField: 'name', displayField: 'name', store: { autoDestroy: true, fields: ['name'],
                    data: choices.map(function (name) { return { name: name }; }) },
                listConfig: { itemTpl: '{name:htmlEncode}', emptyText: kind === 'command-list' ? '暂无可选命令' : '暂无可选服务' },
                triggers: { clear: { cls: 'pmx-clear-trigger', weight: -1, handler: function () { this.setValue([]); } } } });
            return input;
        });
        if (kind === 'service-list') items.push({ xtype: 'component', margin: '8 0 0 0',
            html: '仅在全局自动更新检查时跳过这些服务，手动检查仍包含全部服务。' });
        if (kind === 'command-list') items.push({ xtype: 'component', margin: '8 0 0 0',
            html: '选择需要包装的 docker ' + (data.config.swarm_id ? 'stack' : 'compose') + ' 子命令。' });
        items.push({ xtype: 'component', itemId: 'pve-komodo-option-error', html: '' });
        const title = member ? (parent === 'pre_deploy' ? '部署前操作' : '部署后操作') + '：' + record.get('label') : record.get('label');
        optionEditor(page, title, items, function (editor) {
            const values = {};
            fields.forEach(function (field) {
                let value = editor.down('#pve-komodo-stack-input-' + field.key).getValue();
                if (['servers', 'swarms', 'repos'].includes(field.kind) && value === null) value = '';
                if (choiceField) {
                    if (value === null || value === '') value = [];
                    if (!Array.isArray(value) || !value.every(function (name) { return choices.includes(name); })) throw new Error('invalid-value');
                    values[field.key] = Array.from(new Set(value)); return;
                }
                if (field.kind === 'boolean' ? typeof value !== 'boolean' : typeof value !== 'string') throw new Error('invalid-value');
                values[field.key] = field.kind === 'list' ? value.split(/\r?\n/).filter(function (line) { return line !== ''; }) : value;
            });
            return values.value;
        }, function (editor) {
            fields.forEach(function (field) { editor.down('#pve-komodo-stack-input-' + field.key).setValue(field.kind === 'list' ? field.value.join('\n') : field.value); });
        }, function (value) {
            if (page._pveKomodoStackData !== data) { stackStatus(page, '配置已刷新，请重新打开编辑窗口'); return; }
            // The backend replaces a whole command object. Preserve its other
            // members when editing one of the three rows from this snapshot.
            const config = member ? { [parent]: Object.assign({}, copyOption(data.config[parent]), { [member]: value }) } : { [key]: value };
            if (key === 'server_id' && value) config.swarm_id = '';
            if (key === 'swarm_id' && value) config.server_id = '';
            saveStack(page, 'config', { config: config });
        });
    }
    function stackListEditor(page, record) {
        const kind = record.get('kind'), key = record.get('key'), initial = copyOption(record.get('value'));
        let draft = copyOption(initial), editor;
        const schema = kind === 'envfiles' ? [['path', '路径', 'string'], ['track', '跟踪', 'boolean']] :
            [['path', '路径', 'string'], ['services', '服务（每行一个）', 'list'], ['requires', '更改后操作', 'requires']];
        function refresh() { editor.down('#pve-komodo-stack-list').getStore().loadData(copyOption(draft)); }
        function edit(index) {
            const original = index < 0 ? kind === 'envfiles' ? { path: '', track: true } : { path: '', services: [], requires: 'Restart' } : copyOption(draft[index]);
            const values = schema.map(function (field) { return { xtype: field[2] === 'boolean' ? 'checkbox' : field[2] === 'list' ? 'textareafield' : field[2] === 'requires' ? 'combobox' : 'textfield',
                itemId: 'pve-komodo-stack-list-' + field[0], fieldLabel: field[1], value: field[2] === 'list' ? (original[field[0]] || []).join('\n') : original[field[0]],
                checked: original[field[0]] === true, queryMode: 'local', editable: false, store: field[2] === 'requires' ? ['None', 'Restart', 'Redeploy'] : undefined }; });
            const child = Ext.create('Ext.window.Window', { title: index < 0 ? '添加文件' : '编辑文件', width: 600, bodyPadding: 16, modal: true, closeAction: 'destroy',
                layout: 'anchor', defaults: { anchor: '100%', labelWidth: 180 }, items: values,
                buttons: [{ text: '确定', itemId: 'pve-komodo-stack-list-apply', handler: function () {
                    const value = {};
                    schema.forEach(function (field) { const raw = child.down('#pve-komodo-stack-list-' + field[0]).getValue(); value[field[0]] = field[2] === 'list' ? String(raw || '').split(/\r?\n/).filter(Boolean) : raw; });
                    if (typeof value.path !== 'string' || !value.path || kind === 'envfiles' && typeof value.track !== 'boolean' || kind === 'configfiles' && !['None', 'Restart', 'Redeploy'].includes(value.requires)) return;
                    if (index < 0) draft.push(value); else draft[index] = value; refresh(); child.destroy();
                } }, { text: '取消', handler: function () { child.destroy(); } }] });
            editor._maintenanceEditor = child; child.show();
        }
        editor = optionEditor(page, record.get('label'), [{ xtype: 'gridpanel', itemId: 'pve-komodo-stack-list', height: 330,
            store: { autoDestroy: true, fields: schema.map(function (field) { return field[0]; }), data: [] },
            columns: schema.map(function (field) { return { text: field[1], dataIndex: field[0], flex: 1, sortable: false,
                renderer: function (value) { return field[2] === 'boolean' ? optionValue(value, 'boolean') : field[2] === 'list' ? tagsText(value) : text(value); } }; }),
            tbar: [{ text: '添加', handler: function () { edit(-1); } }, { text: '编辑', handler: function (button) {
                const grid = button.up('gridpanel'), selected = grid.getSelectionModel().getSelection()[0], index = selected ? grid.getStore().indexOf(selected) : -1;
                if (index >= 0) edit(index);
            } }, { text: '删除', handler: function (button) {
                const grid = button.up('gridpanel'), selected = grid.getSelectionModel().getSelection()[0], index = selected ? grid.getStore().indexOf(selected) : -1;
                if (index >= 0) { draft.splice(index, 1); refresh(); }
            } }] }, { xtype: 'component', itemId: 'pve-komodo-option-error', html: '' }], function () { return draft; },
        function (window) { draft = copyOption(initial); window.down('#pve-komodo-stack-list').getStore().loadData(copyOption(draft)); },
        function (value) { saveStack(page, 'config', { config: { [key]: copyOption(value) } }); });
    }
    function renderStackFile(tab, data) {
        const selected = tab.down('#pve-komodo-stack-file').getValue();
        tab._pveKomodoFile = data.files.find(function (file) { return file.path === selected; });
        tab.down('#pve-komodo-stack-compose').setValue(tab._pveKomodoFile && typeof tab._pveKomodoFile.contents === 'string' ? tab._pveKomodoFile.contents : '');
        tab.down('#pve-komodo-stack-deployed').setValue(typeof data.deployed_config === 'string' ? data.deployed_config : '');
        stackControls(stackRoot(tab));
    }
    function editStackFile(tab) {
        const page = stackRoot(tab), data = page._pveKomodoStackData, file = tab._pveKomodoFile;
        if (!data || !data.can_write || !file || !file.editable || file.redacted || optionBusy()) return;
        optionEditor(page, '编辑文件：' + text(file.path), [{ xtype: 'component',
            html: typeof file.contents === 'string' ? '' : '<p>原文件未能读取，尚未取得内容。保存将初始化或覆盖该路径文件。</p>' },
        { xtype: 'textareafield', itemId: 'pve-komodo-stack-file-editor', height: 480,
            fieldStyle: 'font-family:monospace' }, { xtype: 'component', itemId: 'pve-komodo-option-error', html: '' }],
        function (editor) { return editor.down('#pve-komodo-stack-file-editor').getValue(); },
        function (editor) { editor.down('#pve-komodo-stack-file-editor').setValue(typeof file.contents === 'string' ? file.contents : ''); },
        function (contents) { saveStack(page, 'file', { file_path: file.path, contents: contents }); });
    }
    function renderStackLog(tab) {
        const result = tab._pveKomodoLog;
        const stream = tab.down('#pve-komodo-stack-log-stream').getValue() || 'stdout';
        const value = result && typeof result[stream] === 'string' ? result[stream] : '';
        tab.down('#pve-komodo-stack-log-output').update('<pre style="white-space:pre-wrap;overflow-wrap:anywhere;margin:0">' + text(value) + '</pre>');
    }
    function loadStackLog(tab) {
        const page = stackRoot(tab), data = page._pveKomodoStackData;
        if (!stackUser(page) || !tab._pveKomodoStackTabActive || !data) return;
        if (!data.can_logs) { stackStatus(page, page.pveKomodoContainerPage ? '没有读取此容器日志的权限' : '没有读取此应用栈日志的权限'); return; }
        tab._pveKomodoStackTabGeneration = (tab._pveKomodoStackTabGeneration || 0) + 1;
        if (tab._pveKomodoStackRead) Ext.Ajax.abort(tab._pveKomodoStackRead);
        tab._pveKomodoStackRead = null;
        if (tab._pveKomodoLogTimer !== undefined) window.clearTimeout(tab._pveKomodoLogTimer);
        tab._pveKomodoLogTimer = undefined; tab._pveKomodoLog = null; renderStackLog(tab);
        const servicesField = tab.down('#pve-komodo-stack-log-services');
        const services = servicesField && servicesField.getValue();
        const search = tab.down('#pve-komodo-stack-log-search').getValue() || '';
        tab.down('#pve-komodo-stack-log-tail').setDisabled(!!search);
        if (page.pveKomodoContainerPage) {
            const stream = tab.down('#pve-komodo-stack-log-stream'); stream.setDisabled(!!search);
            if (search && stream.getValue() !== 'stdout') stream.setValue('stdout');
        }
        const params = { tail: search ? 5000 : Number(tab.down('#pve-komodo-stack-log-tail').getValue()),
            timestamps: tab.down('#pve-komodo-stack-log-timestamps').getValue() === true,
            search: search, invert: tab.down('#pve-komodo-stack-log-invert').getValue() === true };
        if (Array.isArray(services) && services.length) params.services = services;
        stackStatus(page, '正在读取日志…');
        stackRequest(page, '/logs', params, function (success, result, response) {
            if (success && result && typeof result.stdout === 'string' && typeof result.stderr === 'string' &&
                (!page.pveKomodoContainerPage || result.server_id === data.server_id && result.container_id === data.container_id)) {
                tab._pveKomodoLog = result; renderStackLog(tab);
                stackStatus(page, (search ? '已搜索最近 5000 行日志；尾部行数选择不用于搜索' +
                    (page.pveKomodoContainerPage ? '；搜索结果包含标准输出与标准错误' : '') : '已读取日志尾部') +
                    (result.truncated ? '（输出已截断）' : '') + (result.success === false ? '；日志命令返回失败，请核对输出' : ''));
            } else stackStatus(page, page.pveKomodoContainerPage ? containerDetailError(response, '容器日志读取失败，请重试') : stackError(response, '日志读取失败，请重试'));
            if (tab.down('#pve-komodo-stack-log-poll').getValue() && tab._pveKomodoStackTabActive) tab._pveKomodoLogTimer = window.setTimeout(function () {
                tab._pveKomodoLogTimer = undefined; loadStackLog(tab);
            }, 10000);
        }, tab);
    }
    let xtermLoading = null;
    function loadXterm() {
        const constructor = window.Terminal && (window.Terminal.Terminal || window.Terminal);
        if (typeof constructor === 'function') return Promise.resolve(constructor);
        if (!xtermLoading) xtermLoading = new Promise(function (resolve, reject) {
            const document = window.document;
            if (!document || !Ext.Loader || typeof Ext.Loader.loadScript !== 'function') { reject(new Error('terminal-assets-unavailable')); return; }
            if (!document.getElementById('pve-komodo-xterm-css')) {
                const css = document.createElement('link'); css.id = 'pve-komodo-xterm-css'; css.rel = 'stylesheet'; css.href = '/xtermjs/xterm.css'; document.head.appendChild(css);
            }
            Ext.Loader.loadScript({ url: '/xtermjs/xterm.js', onLoad: function () {
                const loaded = window.Terminal && (window.Terminal.Terminal || window.Terminal);
                if (typeof loaded === 'function') resolve(loaded); else reject(new Error('terminal-assets-unavailable'));
            }, onError: function () { reject(new Error('terminal-assets-unavailable')); } });
        }).catch(function (error) { xtermLoading = null; throw error; });
        return xtermLoading;
    }
    function fitStackTerminal(tab) {
        const terminal = tab._pveKomodoTerminal, host = tab.down('#pve-komodo-stack-terminal-output');
        const element = alive(host) && host.getEl() && host.getEl().dom;
        const screen = terminal && terminal.element && terminal.element.querySelector('.xterm-screen');
        if (!element || !screen || typeof screen.getBoundingClientRect !== 'function') return;
        // Measure the cells xterm actually rendered, rather than assuming a font
        // width or relying on a private xterm API that can change with PVE.
        const bounds = screen.getBoundingClientRect(), cellWidth = bounds.width / terminal.cols, cellHeight = bounds.height / terminal.rows;
        if (!(cellWidth > 0 && cellHeight > 0 && element.clientWidth > 0 && element.clientHeight > 0)) return;
        const style = typeof window.getComputedStyle === 'function' ? window.getComputedStyle(element) : {};
        const padding = function (property) { return parseFloat(style[property]) || 0; };
        const viewport = terminal.element.querySelector('.xterm-viewport');
        const scrollbar = viewport ? Math.max(0, viewport.offsetWidth - viewport.clientWidth) : 0;
        const width = element.clientWidth - padding('paddingLeft') - padding('paddingRight') - scrollbar;
        const height = element.clientHeight - padding('paddingTop') - padding('paddingBottom');
        const cols = Math.max(1, Math.min(1000, Math.floor(width / cellWidth)));
        const rows = Math.max(1, Math.min(500, Math.floor(height / cellHeight)));
        if (cols !== terminal.cols || rows !== terminal.rows) terminal.resize(cols, rows);
    }
    function terminalFullscreen(tab) {
        const document = window.document, target = typeof tab.getEl === 'function' && tab.getEl().dom;
        if (!document || !target || typeof target.requestFullscreen !== 'function') return;
        try {
            if (document.fullscreenElement === target) {
                Promise.resolve(document.exitFullscreen()).catch(function () { stackStatus(stackRoot(tab), '无法退出全屏，请按 Esc'); });
            } else {
                tab._pveKomodoTerminalWindowSize = typeof tab.getSize === 'function' ? tab.getSize() : null;
                Promise.resolve(target.requestFullscreen()).catch(function () {
                    tab._pveKomodoTerminalWindowSize = null;
                    if (alive(tab)) stackStatus(stackRoot(tab), '浏览器未允许全屏，请再次点击全屏按钮');
                });
            }
        } catch (error) { stackStatus(stackRoot(tab), '浏览器未允许全屏，请再次点击全屏按钮'); }
    }
    function prepareTerminalDialog(tab, open) {
        if (tab._pveKomodoTerminalDialogPending) return true;
        const document = window.document, target = typeof tab.getEl === 'function' && tab.getEl().dom;
        if (!document || !target || document.fullscreenElement !== target) return false;
        // Ext windows render outside this panel, so leave browser fullscreen
        // before opening a modal that would otherwise be invisible.
        const generation = tab._pveKomodoStackTabGeneration;
        tab._pveKomodoTerminalDialogPending = true;
        function failed() {
            tab._pveKomodoTerminalDialogPending = false;
            if (alive(tab)) stackStatus(stackRoot(tab), '无法退出全屏，请按 Esc 后新建终端');
        }
        try {
            Promise.resolve(document.exitFullscreen()).then(function () {
                tab._pveKomodoTerminalDialogPending = false;
                if (alive(tab) && tab._pveKomodoStackTabActive && tab._pveKomodoStackTabGeneration === generation) open(tab);
            }, failed);
        } catch (error) { failed(); }
        return true;
    }
    function observeStackTerminal(tab, host, terminal) {
        const document = window.document, element = host.getEl().dom;
        const target = typeof tab.getEl === 'function' && tab.getEl().dom;
        let frame = null;
        function fit() {
            if (frame !== null || tab._pveKomodoTerminal !== terminal) return;
            if (typeof window.requestAnimationFrame === 'function') frame = window.requestAnimationFrame(function () {
                frame = null; if (tab._pveKomodoTerminal === terminal) fitStackTerminal(tab);
            });
            else fitStackTerminal(tab);
        }
        function restoreSize() {
            const size = tab._pveKomodoTerminalWindowSize; tab._pveKomodoTerminalWindowSize = null;
            if (!alive(tab)) return;
            if (size && typeof tab.setSize === 'function') tab.setSize(size);
            if (tab.ownerCt && typeof tab.ownerCt.updateLayout === 'function') tab.ownerCt.updateLayout();
        }
        function fullscreenChanged() {
            if (tab._pveKomodoTerminal !== terminal) return;
            const full = target && document.fullscreenElement === target;
            const button = tab.down('#pve-komodo-stack-terminal-fullscreen');
            if (alive(button)) button.setText(full ? '退出全屏' : '全屏');
            if (full && typeof tab.setSize === 'function') tab.setSize(window.innerWidth, window.innerHeight);
            else restoreSize();
            fit();
        }
        function windowResized() {
            if (target && document && document.fullscreenElement === target) fullscreenChanged();
            else fit();
        }
        const observer = typeof window.ResizeObserver === 'function' ? new window.ResizeObserver(fit) : null;
        if (observer) observer.observe(element);
        if (typeof window.addEventListener === 'function') window.addEventListener('resize', windowResized);
        if (document && typeof document.addEventListener === 'function') document.addEventListener('fullscreenchange', fullscreenChanged);
        const button = tab.down('#pve-komodo-stack-terminal-fullscreen');
        if (alive(button)) button.setDisabled(!(target && typeof target.requestFullscreen === 'function'));
        tab._pveKomodoTerminalFit = fit;
        tab._pveKomodoTerminalLayoutCleanup = function () {
            if (observer) observer.disconnect();
            if (frame !== null && typeof window.cancelAnimationFrame === 'function') window.cancelAnimationFrame(frame);
            if (typeof window.removeEventListener === 'function') window.removeEventListener('resize', windowResized);
            if (document && typeof document.removeEventListener === 'function') document.removeEventListener('fullscreenchange', fullscreenChanged);
            tab._pveKomodoTerminalFit = null;
            if (target && document && document.fullscreenElement === target && typeof document.exitFullscreen === 'function') {
                try { Promise.resolve(document.exitFullscreen()).catch(function () {}); } catch (error) { /* Browser can exit during navigation. */ }
            }
            restoreSize();
        };
        fit();
        if (document && document.fonts && document.fonts.ready) document.fonts.ready.then(fit, function () {});
    }
    function attachStackTerminal(tab, grant, csrf, user) {
        const page = stackRoot(tab), generation = tab._pveKomodoStackTabGeneration, attempt = resetStackTerminal(tab);
        loadXterm().then(function (Terminal) {
            if (stackUser(page) !== user || !alive(tab) || !tab._pveKomodoStackTabActive || tab._pveKomodoStackTabGeneration !== generation ||
                tab._pveKomodoTerminalAttempt !== attempt) return;
            const terminal = new Terminal({ cols: 100, rows: 28, cursorBlink: true, scrollback: 5000, fontSize: 13 });
            const host = tab.down('#pve-komodo-stack-terminal-output');
            terminal.open(host.getEl().dom); tab._pveKomodoTerminal = terminal;
            const url = new URL(page.pveKomodoStackPath + '/terminals/ws', window.location.href); url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
            const socket = new window.WebSocket(url.toString()); socket.binaryType = 'arraybuffer'; tab._pveKomodoSocket = socket;
            let ready = false, failure = null;
            const valid = function () { return stackUser(page) === user && alive(tab) && tab._pveKomodoStackTabActive &&
                tab._pveKomodoStackTabGeneration === generation && tab._pveKomodoTerminalAttempt === attempt && tab._pveKomodoSocket === socket; };
            socket.onopen = function () { if (valid()) socket.send(JSON.stringify({ type: 'open', grant: grant.grant, csrf_token: csrf })); else socket.close(); };
            socket.onmessage = function (event) {
                if (!valid()) return;
                if (typeof event.data !== 'string') { terminal.write(new Uint8Array(event.data)); return; }
                try {
                    const result = JSON.parse(event.data);
                    if (result.type === 'ready' && result.session_id === grant.session_id) { ready = true; terminal.focus(); socket.send(JSON.stringify({ type: 'resize', cols: terminal.cols, rows: terminal.rows })); stackStatus(page, '终端已连接；离开页面将关闭本次会话'); }
                    if (result.type === 'error') {
                        const messages = {
                            terminal_permission_denied: page.pveKomodoServerTerminalPage ? '没有访问此服务器终端的权限' : '没有访问此容器终端的权限',
                            terminal_service_changed: page.pveKomodoServerTerminalPage ? '服务器或关联已变化，请刷新后重新新建终端' : '容器或服务已变化，请刷新后重新新建终端',
                            terminal_creation_unconfirmed: '终端创建结果未确认，请核对后重新新建',
                            terminal_connection_failed: '连接 Komodo 终端失败，请重新新建',
                            terminal_upstream_unavailable: 'Komodo 终端服务暂时不可用，请稍后新建',
                            terminal_stream_closed: 'Komodo 终端连接已关闭，请重新新建',
                            terminal_session_expired: '终端会话已到期，请重新新建',
                            terminal_session_idle: '终端因长时间无活动已关闭，请重新新建',
                            terminal_grant_invalid: '终端授权已过期或已使用，请重新新建',
                        };
                        ready = false; failure = messages[result.code] || '终端会话失败，请重新确认后新建';
                        stackStatus(page, failure);
                    }
                } catch (error) { failure = '终端协议错误，请关闭后重新新建'; stackStatus(page, failure); socket.close(); }
            };
            socket.onerror = function () { if (valid()) { failure = failure || '终端连接失败，请重新确认后新建'; stackStatus(page, failure); } };
            socket.onclose = function () {
                const connected = ready; ready = false;
                if (valid()) stackStatus(page, failure || (connected ? '终端已断开；请重新新建' : '未能建立终端连接，请重新新建'));
            };
            terminal.onData(function (data) { if (valid() && ready && socket.readyState === 1) socket.send(JSON.stringify({ type: 'data', data: data })); });
            terminal.onResize(function (size) { if (valid() && ready && socket.readyState === 1) socket.send(JSON.stringify({ type: 'resize', cols: size.cols, rows: size.rows })); });
            observeStackTerminal(tab, host, terminal);
        }).catch(function () { if (stackUser(page) === user && alive(tab) && tab._pveKomodoStackTabActive &&
            tab._pveKomodoStackTabGeneration === generation && tab._pveKomodoTerminalAttempt === attempt) stackStatus(page, '终端组件加载失败，请刷新页面后重试'); });
    }
    function newStackTerminal(tab) {
        const page = stackRoot(tab), source = tab._pveKomodoTerminalServices;
        const data = page._pveKomodoStackData, user = stackUser(page), generation = tab._pveKomodoStackTabGeneration;
        if (!source || !data || optionBusy() || !user || alive(tab._pveKomodoTerminalDialog)) return;
        if (prepareTerminalDialog(tab, newStackTerminal)) return;
        const choices = source.services.filter(function (service) { return service.can_open && typeof service.container_id === 'string' && /^[0-9a-f]{64}$/i.test(service.container_id); });
        const dialog = Ext.create('Ext.window.Window', { title: '新建临时终端', width: 560, modal: true, bodyPadding: 16, closeAction: 'destroy', layout: 'anchor',
            defaults: { anchor: '100%', labelWidth: 100 }, items: [{ xtype: 'combobox', itemId: 'pve-komodo-terminal-service', fieldLabel: '服务',
                editable: false, queryMode: 'local', displayField: 'label', valueField: 'service', store: { fields: ['service', 'label'], data: choices.map(function (service) {
                    return { service: service.service, label: service.service + ' · ' + (service.name || service.container_id) };
                }) }, listConfig: { itemTpl: '{label:htmlEncode}' } }, { xtype: 'combobox', itemId: 'pve-komodo-terminal-shell', fieldLabel: 'Shell',
                editable: false, queryMode: 'local', store: ['sh', 'bash'], value: 'sh' },
                { xtype: 'component', html: '<p>将在所选服务的容器中打开交互式 Shell。离开页面会关闭并清理本次临时会话。</p>' }],
            buttons: [{ text: '新建', itemId: 'pve-komodo-terminal-create', handler: function () {
                const selected = dialog.down('#pve-komodo-terminal-service').getValue(), shell = dialog.down('#pve-komodo-terminal-shell').getValue();
                const service = choices.find(function (candidate) { return candidate.service === selected; });
                const requestId = uuid();
                if (!alive(dialog) || !service || !requestId || !['sh', 'bash'].includes(shell) || stackUser(page) !== user || optionBusy() ||
                    !tab._pveKomodoStackTabActive || tab._pveKomodoStackTabGeneration !== generation ||
                    tab._pveKomodoTerminalServices !== source || page._pveKomodoStackData !== data) return;
                dialog.destroy(); resetStackTerminal(tab);
                const state = { pending: true }; stackWriteState = state; stackControls(page);
                let finished = false;
                function complete(options, success, response) {
                    if (finished) return; finished = true; state.pending = false;
                    if (alive(activeStackPage)) stackControls(activeStackPage);
                    if (alive(activeDashboard)) updateControls(activeDashboard);
                    if (alive(activeGuestOptions)) optionControls(activeGuestOptions);
                    if (alive(activeGuestSummary)) summaryControls(activeGuestSummary);
                    if (stackUser(page) !== user || !alive(tab) || !tab._pveKomodoStackTabActive || tab._pveKomodoStackTabGeneration !== generation) return;
                    try {
                        const result = JSON.parse(response.responseText);
                        if (!success || !result || typeof result.grant !== 'string' || !result.grant ||
                            typeof result.session_id !== 'string' || !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(result.session_id)) throw new Error('grant-unavailable');
                        attachStackTerminal(tab, result, source.csrf_token, user);
                    } catch (error) { stackStatus(page, stackError(response, '终端创建结果未确认；不会自动重试，请核对后重新新建')); }
                }
                try { Ext.Ajax.request({ url: page.pveKomodoStackPath + '/terminals', method: 'POST', timeout: 60000,
                    headers: { 'X-PVE-Komodo-CSRF': source.csrf_token }, jsonData: { service: service.service, server_id: service.server_id,
                        container_id: service.container_id, shell: shell, request_id: requestId }, callback: complete }); }
                catch (error) { complete(null, false, null); }
            } }, { text: '取消', handler: function () { dialog.destroy(); } }] });
        tab._pveKomodoTerminalDialog = dialog; dialog.show();
    }
    function renderStackTab(tab, data) {
        const page = stackRoot(tab), kind = tab.pveKomodoStackTab;
        if (kind === 'config') {
            const rows = [];
            stackConfigSections.forEach(function (group) {
                rows.push({ key: 'heading-' + group[0], label: group[1], kind: 'heading', heading: 'major', available: false });
                group[2].forEach(function (section) {
                    rows.push({ key: 'heading-' + section[0], label: section[1], kind: 'heading', heading: 'minor',
                        description: stackSectionDescription(section[0], data), available: false });
                    section[2].forEach(function (definition) {
                        const [parent, member] = definition[0].split('.');
                        const source = data.config[parent], value = member ?
                            source && typeof source === 'object' && !Array.isArray(source) ? source[member] : undefined : source;
                        const reference = data.selectors && Array.isArray(data.selectors[definition[2]]) && data.selectors[definition[2]].find(function (entry) { return entry.id === value; });
                        rows.push({ key: definition[0], label: definition[1], kind: definition[2], value: value,
                            reference_name: reference && typeof reference.name === 'string' ? reference.name : null,
                            available: stackFieldAvailable(value, definition[2]) && !(data.redacted_fields || []).includes(parent) });
                    });
                });
            });
            tab.down('#pve-komodo-stack-config-grid').getStore().loadData(rows);
        } else if (kind === 'info') {
            tab._pveKomodoStackRendering = true;
            const selector = tab.down('#pve-komodo-stack-file'), selected = selector.getValue();
            selector.getStore().loadData(data.files.map(function (file) { return { path: file.path }; }));
            selector.setValue(data.files.some(function (file) { return file.path === selected; }) ? selected : data.files[0] && data.files[0].path || '');
            tab._pveKomodoStackRendering = false; renderStackFile(tab, data);
            if (Array.isArray(data.remote_errors) && data.remote_errors.length) stackStatus(page, '部分原文件未能读取，尚未取得内容；保存这类文件会初始化或覆盖未知原文');
        } else if (kind === 'logs') {
            const selector = tab.down('#pve-komodo-stack-log-services');
            selector.getStore().loadData(data.services.map(function (service) { return { service: service }; }));
            loadStackLog(tab);
        } else if (kind === 'terminal') {
            stackRequest(page, '/terminal-services', undefined, function (success, source, response) {
                if (success && source && source.stack_id === data.stack_id && source.server_id === data.server_id && Array.isArray(source.services) &&
                    typeof source.csrf_token === 'string' && source.csrf_token && !/[\r\n]/.test(source.csrf_token)) {
                    tab._pveKomodoTerminalServices = source; stackStatus(page, source.services.some(function (service) { return service.can_open; }) ? '选择新建以打开服务容器终端' : '没有可打开终端的运行中服务');
                } else stackStatus(page, stackError(response, '终端服务读取失败，请重试'));
                stackControls(page);
            }, tab);
        }
        stackControls(page);
    }
    function stackTabConfig(kind, title, icon) {
        const config = { xtype: 'panel', itemId: 'pve-komodo-stack-' + kind, title: title, iconCls: 'fa ' + icon, pveKomodoStackTab: kind,
            onlineHelp: 'chapter_gui', border: false, bodyPadding: 8, layout: 'fit', listeners: {
                activate: function (tab) {
                    const page = stackRoot(tab); page._pveKomodoStackActive = true; page._pveKomodoStackTab = tab; activeStackPage = page; stackPages.add(page);
                    tab._pveKomodoStackTabActive = true; tab._pveKomodoStackTabGeneration = (tab._pveKomodoStackTabGeneration || 0) + 1;
                    if (page._pveKomodoStackData) renderStackTab(tab, page._pveKomodoStackData); else loadStackDetails(page);
                }, deactivate: cancelStackTab, beforedestroy: cancelStackTab,
            } };
        if (kind === 'config') {
            config.tbar = [{ text: '编辑', itemId: 'pve-komodo-stack-edit', disabled: true, handler: function (button) {
                const tab = button.up('[pveKomodoStackTab]'), grid = tab.down('#pve-komodo-stack-config-grid');
                stackScalarEditor(stackRoot(tab), grid.getSelectionModel().getSelection()[0]);
            } }];
            config.items = [{ xtype: 'gridpanel', itemId: 'pve-komodo-stack-config-grid', cls: 'pve-komodo-stack-config-grid',
                store: { autoDestroy: true, fields: ['key', 'label', 'kind', 'heading', 'description', 'value', 'available', 'reference_name'], data: [] },
                selModel: { listeners: { beforeselect: function (model, record) {
                    if (record.get('kind') !== 'heading') return true;
                    model.deselectAll(); return false;
                } } },
                viewConfig: { variableRowHeight: true, getRowClass: function (record) {
                    return record.get('kind') === 'heading' ? 'pve-komodo-stack-heading-' + record.get('heading') : '';
                } },
                columns: [{ text: '选项', dataIndex: 'label', width: 250, sortable: false, renderer: text }, { text: '值', dataIndex: 'value', flex: 1, sortable: false, renderer: stackFieldText }],
                listeners: { selectionchange: function () { stackControls(stackRoot(this.up('[pveKomodoStackTab]'))); },
                    itemdblclick: function (view, record) { stackScalarEditor(stackRoot(this.up('[pveKomodoStackTab]')), record); } } }];
        } else if (kind === 'info') {
            config.layout = { type: 'vbox', align: 'stretch' };
            config.tbar = [{ xtype: 'combobox', itemId: 'pve-komodo-stack-file', fieldLabel: '文件', labelWidth: 40, width: 440,
                queryMode: 'local', editable: false, displayField: 'path', valueField: 'path', store: { autoDestroy: true, fields: ['path'], data: [] }, listConfig: { itemTpl: '{path:htmlEncode}' },
                listeners: { change: function (field) { const tab = field.up('[pveKomodoStackTab]'), page = stackRoot(tab);
                    if (!tab._pveKomodoStackRendering && page._pveKomodoStackData) renderStackFile(tab, page._pveKomodoStackData); } } },
                { text: '编辑原文', itemId: 'pve-komodo-stack-edit-file', disabled: true, handler: function (button) { editStackFile(button.up('[pveKomodoStackTab]')); } }];
            config.items = [{ xtype: 'textareafield', itemId: 'pve-komodo-stack-compose', fieldLabel: 'Compose / 配置文件原文', labelAlign: 'top', readOnly: true, flex: 1, fieldStyle: 'font-family:monospace' },
                { xtype: 'textareafield', itemId: 'pve-komodo-stack-deployed', fieldLabel: '上次部署配置（只读）', labelAlign: 'top', readOnly: true, flex: 1, fieldStyle: 'font-family:monospace' }];
        } else if (kind === 'logs') {
            function changed(field) { const tab = field.up('[pveKomodoStackTab]'); if (tab._pveKomodoStackTabActive) loadStackLog(tab); }
            config.tbar = { overflowHandler: 'scroller', items: [
                { text: '查询', handler: function (button) { loadStackLog(button.up('[pveKomodoStackTab]')); } },
                { xtype: 'combobox', itemId: 'pve-komodo-stack-log-services', emptyText: '全部服务', multiSelect: true, queryMode: 'local', editable: false,
                    valueField: 'service', displayField: 'service', width: 180, store: { autoDestroy: true, fields: ['service'], data: [] }, listConfig: { itemTpl: '{service:htmlEncode}' }, listeners: { change: changed } },
                { xtype: 'combobox', itemId: 'pve-komodo-stack-log-tail', queryMode: 'local', editable: false, width: 90, store: ['100', '500', '1000', '5000'], value: '100', listeners: { change: changed } },
                { xtype: 'checkbox', itemId: 'pve-komodo-stack-log-timestamps', boxLabel: '时间戳', checked: false, listeners: { change: changed } },
                { xtype: 'textfield', itemId: 'pve-komodo-stack-log-search', emptyText: '匹配日志（点击查询）', width: 200 },
                { xtype: 'checkbox', itemId: 'pve-komodo-stack-log-invert', boxLabel: '反向匹配', checked: false, listeners: { change: changed } },
                { xtype: 'combobox', itemId: 'pve-komodo-stack-log-stream', queryMode: 'local', editable: false, width: 120,
                    store: [['stdout', '标准输出'], ['stderr', '标准错误']], value: 'stdout', listeners: { change: function (field) { renderStackLog(field.up('[pveKomodoStackTab]')); } } },
                { xtype: 'checkbox', itemId: 'pve-komodo-stack-log-poll', boxLabel: '轮询（10秒）', checked: false, listeners: { change: changed } },
            ] };
            config.items = [{ xtype: 'component', itemId: 'pve-komodo-stack-log-output', scrollable: true, html: '' }];
        } else {
            config.cls = 'pve-komodo-stack-terminal-tab'; config.bodyPadding = 0;
            config.tbar = [{ text: '新建', itemId: 'pve-komodo-stack-new-terminal', disabled: true,
                handler: function (button) { newStackTerminal(button.up('[pveKomodoStackTab]')); } },
                { text: '全屏', itemId: 'pve-komodo-stack-terminal-fullscreen', disabled: true,
                    handler: function (button) { terminalFullscreen(button.up('[pveKomodoStackTab]')); } }];
            config.items = [{ xtype: 'component', itemId: 'pve-komodo-stack-terminal-output', cls: 'pve-komodo-stack-terminal-output',
                scrollable: false, html: '', listeners: { resize: function (host) {
                    const tab = host.up('[pveKomodoStackTab]'); if (tab._pveKomodoTerminalFit) tab._pveKomodoTerminalFit();
                } } }];
        }
        return config;
    }
    function openStackManagement(workspace, stackId, serverId, name) {
        if (!workspace || typeof workspace.setContent !== 'function' || dockerNavigationBusy()) return;
        const title = '应用栈：' + text(name || stackId);
        workspace.setContent({ xtype: 'pvePanelConfig', pveKomodoStackPage: true, pveKomodoStackId: stackId,
            pveKomodoStackPath: '/pve-komodo/api/stacks/' + stackId, _pveKomodoStackExpectedServer: serverId,
            _pveKomodoStackActive: true, workspace: workspace, title: title, onlineHelp: 'chapter_gui', showSearch: false, tbarSpacing: false,
            // Private ConfigPanel context only; never a native resource or rid.
            pveSelNode: { data: { id: 'pve-komodo-stack:' + stackId, text: title } },
            tbar: stackActions.map(function (action) { return { text: action[1], itemId: 'pve-komodo-stack-' + action[0], disabled: true,
                handler: function (button) { const page = stackRoot(button), data = page._pveKomodoStackData;
                    if (data && data.can_execute) stackConfirm(page, '确认' + action[1], action[2],
                        { action: action[0], server_id: data.server_id, base_version: data.version }, page.pveKomodoStackPath + '/actions'); } }; }).concat([
                { text: '刷新', itemId: 'pve-komodo-stack-refresh', handler: function (button) { loadStackDetails(stackRoot(button)); } },
            ]), bbar: [{ xtype: 'tbtext', itemId: 'pve-komodo-stack-status', text: '尚未读取应用栈' }],
            items: [stackTabConfig('config', '配置', 'fa-gear'), stackTabConfig('info', '信息', 'fa-info-circle'),
                stackTabConfig('logs', '日志', 'fa-file-text-o'), stackTabConfig('terminal', '终端', 'fa-terminal')],
            listeners: { afterrender: function (page) { if (page.helpButton) page.helpButton.setHidden(true); },
                beforedestroy: function (page) {
                    page._pveKomodoStackActive = false; if (activeStackPage === page) activeStackPage = null; stackPages.delete(page); cancelStackPage(page);
                } },
        });
    }

    const containerDetailActions = [
        ['restart', '重启', '重启此容器，服务将短暂中断。'],
        ['pause', '暂停', '冻结此容器的进程，服务将无法响应。'],
        ['stop', '停止', '停止此容器，会中断服务。'],
        ['destroy', '销毁', '停止并移除此容器，其可写层数据将被删除；默认不删除存储卷。'],
    ];
    function containerDetailActionSpec(data, slot) {
        if (slot === 'pause' && data && data.state === 'paused') return ['unpause', '恢复', '恢复此容器的进程，容器内服务将继续运行。'];
        if (slot === 'stop' && data && ['exited', 'created'].includes(data.state)) return ['start', '启动', '启动此容器及其配置的主进程。'];
        return containerDetailActions.find(function (action) { return action[0] === slot; });
    }
    function containerDetailError(response, fallback) {
        if (response && response.status === 404) return '容器已不存在，请刷新容器视图';
        if (response && response.status === 409) return '容器状态或服务器描述已变化，请刷新后重新确认';
        return optionsError(response, fallback);
    }
    function containerDetailActionAllowed(data, action) {
        const states = { restart: ['running'], pause: ['running'], unpause: ['paused'], stop: ['running', 'paused'], start: ['exited', 'created'],
            destroy: ['running', 'paused', 'created', 'exited', 'dead', 'restarting', 'stopping', 'removing'] };
        return data && data.can_execute === true && states[action].includes(data.state);
    }
    function containerDetailControls(page) {
        const data = page._pveKomodoStackData, busy = optionBusy() || !!page._pveKomodoStackConfirmation;
        containerDetailActions.forEach(function (action) {
            const button = page.down('#pve-komodo-container-' + action[0]);
            const current = containerDetailActionSpec(data, action[0]);
            if (alive(button)) {
                button.setText(current[1]);
                button.setDisabled(busy || !stackUser(page) || !containerDetailActionAllowed(data, current[0]));
            }
        });
        const refresh = page.down('#pve-komodo-container-refresh');
        if (alive(refresh)) refresh.setDisabled(busy || !!page._pveKomodoStackLoading);
        const tab = page._pveKomodoStackTab;
        if (!alive(tab)) return;
        const edit = tab.down('#pve-komodo-container-description-edit');
        if (alive(edit)) edit.setDisabled(busy || !data || data.can_edit_description !== true || typeof data.description !== 'string' ||
            typeof data.description_version !== 'string' || !/^[0-9a-f]{64}$/.test(data.description_version));
        const terminal = tab.down('#pve-komodo-stack-new-terminal'), source = tab._pveKomodoTerminalServices;
        if (alive(terminal)) terminal.setDisabled(busy || !data || data.can_terminal !== true || !source || source.can_open !== true);
    }
    function containerDetailValue(value) {
        if (value === null || value === undefined) return '-';
        if (typeof value === 'boolean') return value ? '是' : '否';
        if (Array.isArray(value)) return value.length ? text(value.map(function (item) { return typeof item === 'object' ? JSON.stringify(item) : item; }).join('\n')) : '无';
        if (typeof value === 'object') return text(JSON.stringify(value, null, 2));
        return text(value === '' ? '-' : value);
    }
    function containerDetailDate(value) {
        if (typeof value === 'string' && /^0001-01-01T00:00:00(?:\.0+)?(?:Z|[+-]00:00)$/.test(value)) return null;
        if (typeof value === 'number' && Number.isFinite(value) && value >= 0) return dateText(value * 1000);
        if (typeof value === 'string' && value && Number.isFinite(Date.parse(value))) return dateText(value);
        return null;
    }
    function containerInspectMarkup(value) {
        const lines = JSON.stringify(value, null, 2).split('\n');
        return '<pre class="pve-komodo-container-inspect-json" tabindex="0" role="region" aria-label="容器检查 JSON"><code>' + lines.map(function (line, index) {
            let html = '', position = 0;
            line.replace(/"(?:\\.|[^"\\])*"(?:\s*:)?|\b(?:true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g, function (token, offset) {
                html += text(line.slice(position, offset));
                const kind = token[0] === '"' ? /:\s*$/.test(token) ? 'key' : 'string' : /^(?:true|false|null)$/.test(token) ? 'literal' : 'number';
                html += '<span class="pve-komodo-json-' + kind + '">' + text(token) + '</span>'; position = offset + token.length;
                return token;
            });
            return '<span class="pve-komodo-json-line"><span class="pve-komodo-json-line-number" aria-hidden="true">' + (index + 1) +
                '</span><span>' + html + text(line.slice(position)) + '</span></span>';
        }).join('') + '</code></pre>';
    }
    function updateContainerInspect(tab, value) {
        const field = tab.down('#pve-komodo-container-inspect');
        if (!alive(field)) return;
        field.update(value === null ? '' : containerInspectMarkup(value));
        if (typeof tab.updateLayout === 'function') tab.updateLayout();
        const scroller = typeof tab.getScrollable === 'function' && tab.getScrollable();
        if (scroller && typeof scroller.refresh === 'function') scroller.refresh();
        if (scroller && typeof scroller.scrollTo === 'function') scroller.scrollTo(0, 0);
    }
    function containerPairGrid(id, title, fields, options) {
        return Object.assign({ xtype: 'gridpanel', itemId: 'pve-komodo-container-' + id + '-grid', title: title,
            minWidth: 0, border: true, store: { autoDestroy: true, fields: ['label', 'value'], data: [] },
            columns: [{ text: '项目', dataIndex: 'label', width: 125, sortable: false, renderer: text },
                { text: '值', dataIndex: 'value', flex: 1, sortable: false, renderer: containerDetailValue }],
            viewConfig: { stripeRows: true, enableTextSelection: true, variableRowHeight: true, emptyText: '暂无数据', deferEmptyText: false },
            pveKomodoContainerFields: fields, cls: 'pve-komodo-container-information' }, options || {});
    }
    function containerCurrent(tab, data) {
        const stats = data && data.stats || {}, information = data && data.information || {};
        function percent(value) {
            if (typeof value !== 'string' || !/^\d+(?:\.\d+)?%$/.test(value.trim())) return null;
            const number = Number(value.trim().slice(0, -1));
            return Number.isFinite(number) ? number : null;
        }
        const cpu = percent(stats.cpu_percent), memory = percent(stats.memory_percent);
        const healthLabels = { healthy: '健康', unhealthy: '不健康', starting: '检查中' };
        const values = {
            state: data ? stateText(data.state) : '-',
            status: text(data && data.status),
            health: text(typeof information.health === 'string' && Object.prototype.hasOwnProperty.call(healthLabels, information.health) ?
                healthLabels[information.health] : information.health),
            started: text(containerDetailDate(information.started_at)),
            finished: text(containerDetailDate(information.finished_at)),
            restarts: text(information.restart_count),
            cpu: cpu === null ? '-' : text(stats.cpu_percent.trim()),
            memory: (memory === null ? '-' : text(stats.memory_percent.trim())) +
                (typeof stats.memory_usage === 'string' && stats.memory_usage.trim() ? '（' + text(stats.memory_usage) + '）' : ''),
            network: text(stats.network_io), block: text(stats.block_io), pids: text(stats.pids),
        };
        Object.keys(values).forEach(function (key) {
            const widget = tab.down('#pve-komodo-container-current-' + key);
            const value = key === 'cpu' ? cpu : key === 'memory' ? memory : null;
            if (alive(widget)) widget.updateValue(values[key], value === null ? 0 : Math.max(0, Math.min(1, value / 100)));
        });
    }
    function clearContainerTab(tab) {
        ['information', 'configuration', 'updates'].forEach(function (name) {
            const grid = tab.down('#pve-komodo-container-' + name + '-grid'); if (alive(grid)) grid.getStore().loadData([]);
        });
        const description = tab.down('#pve-komodo-container-description'); if (alive(description)) description.update('');
        updateContainerInspect(tab, null);
        const log = tab.down('#pve-komodo-stack-log-output'); if (alive(log)) log.update('');
        containerCurrent(tab, null);
        tab._pveKomodoLog = null; tab._pveKomodoTerminalServices = null;
    }
    function loadContainerDetails(page) {
        if (!stackUser(page) || page._pveKomodoStackLoading) return;
        page._pveKomodoStackGeneration = (page._pveKomodoStackGeneration || 0) + 1;
        page._pveKomodoStackLoading = true; page._pveKomodoStackData = null;
        const tab = page._pveKomodoStackTab;
        if (alive(tab)) { cancelStackTab(tab); tab._pveKomodoStackTabActive = true; clearContainerTab(tab); }
        stackStatus(page, '正在读取容器…'); containerDetailControls(page);
        stackRequest(page, '', undefined, function (success, data, response) {
            page._pveKomodoStackLoading = false;
            let message = containerDetailError(response, '容器数据读取失败，请刷新后重试');
            if (success && data && data.server_id === page.pveKomodoContainerServer && data.container_id === page.pveKomodoContainerId &&
                typeof data.container_name === 'string' && typeof data.version === 'string' && /^[0-9a-f]{64}$/.test(data.version) &&
                data.information && typeof data.information === 'object' && !Array.isArray(data.information) &&
                data.stats && typeof data.stats === 'object' && !Array.isArray(data.stats) && Array.isArray(data.updates) &&
                typeof data.csrf_token === 'string' && data.csrf_token && !/[\r\n]/.test(data.csrf_token)) {
                page._pveKomodoStackData = data;
                message = '容器：' + data.container_name + (Array.isArray(data.errors) && data.errors.length ? '；部分数据暂时不可用' : '');
                if (alive(page._pveKomodoStackTab)) renderContainerTab(page._pveKomodoStackTab, data);
            }
            stackStatus(page, message); containerDetailControls(page);
        });
    }
    function editContainerDescription(tab) {
        const page = stackRoot(tab), data = page._pveKomodoStackData;
        if (!data || data.can_edit_description !== true || typeof data.description !== 'string' ||
            typeof data.description_version !== 'string' || !/^[0-9a-f]{64}$/.test(data.description_version) || optionBusy()) return;
        optionEditor(page, '编辑服务器描述', [{ xtype: 'component', html: '此描述属于 Komodo 服务器，同一服务器的其他容器页面也会显示。' },
            { xtype: 'textareafield', itemId: 'pve-komodo-container-description-input', height: 280, maxLength: 16384, enforceMaxLength: true },
            { xtype: 'component', itemId: 'pve-komodo-option-error', html: '' }], function (editor) {
            const value = editor.down('#pve-komodo-container-description-input').getValue();
            if (typeof value !== 'string' || value.length > 16384 || /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(value)) throw new Error('invalid-description');
            return value;
        }, function (editor) { editor.down('#pve-komodo-container-description-input').setValue(data.description); }, function (value) {
            if (page._pveKomodoStackData !== data) { stackStatus(page, '数据已刷新，请重新打开编辑窗口'); return; }
            stackConfirm(page, '确认保存服务器描述', '更新此服务器共享的描述，不改变 Docker 配置。',
                { action: 'description', description: value, base_version: data.description_version });
        });
    }
    function renderContainerTab(tab, data) {
        const page = stackRoot(tab), kind = tab.pveKomodoContainerTab;
        if (kind === 'overview') {
            tab._pveKomodoUpdatesActive = true;
            containerCurrent(tab, data);
            ['information', 'configuration'].forEach(function (name) {
                const grid = tab.down('#pve-komodo-container-' + name + '-grid');
                const source = Object.assign({}, data.information,
                    { container_id: data.container_id, container_name: data.container_name, state: data.state, status: data.status });
                grid.getStore().loadData(grid.pveKomodoContainerFields.map(function (field) {
                    const value = source[field[0]];
                    return { label: field[1], value: ['created', 'started_at', 'finished_at'].includes(field[0]) ? containerDetailDate(value) :
                        field[0] === 'state' ? stateText(value) : value };
                }));
            });
            tab.down('#pve-komodo-container-description').update('<pre style="white-space:pre-wrap;overflow-wrap:anywhere;margin:0">' +
                text(typeof data.description === 'string' ? data.description : null) + '</pre>');
            tab.down('#pve-komodo-container-updates-grid').getStore().loadData(data.updates);
        } else if (kind === 'logs') loadStackLog(tab);
        else if (kind === 'inspect') {
            if (data.can_inspect !== true) { stackStatus(page, '没有检查此容器的权限'); return; }
            stackStatus(page, '正在检查容器…');
            stackRequest(page, '/inspect', undefined, function (success, result, response) {
                if (success && result && result.server_id === data.server_id && result.container_id === data.container_id &&
                    result.inspect && typeof result.inspect === 'object' && !Array.isArray(result.inspect)) {
                    updateContainerInspect(tab, result.inspect); stackStatus(page, '已读取容器检查结果');
                } else { updateContainerInspect(tab, null); stackStatus(page, containerDetailError(response, '容器检查失败，请重试')); }
            }, tab);
        } else if (kind === 'terminal') {
            if (data.can_terminal !== true) { stackStatus(page, '当前容器不可打开终端'); return; }
            stackRequest(page, '/terminal-services', undefined, function (success, result, response) {
                if (success && result && result.server_id === data.server_id && result.container_id === data.container_id &&
                    result.options && typeof result.options === 'object' && Array.isArray(result.shells) &&
                    typeof result.csrf_token === 'string' && result.csrf_token && !/[\r\n]/.test(result.csrf_token)) {
                    tab._pveKomodoTerminalServices = result;
                    stackStatus(page, result.can_open ? '选择新建以打开此容器终端' : '当前容器不可打开终端');
                } else stackStatus(page, containerDetailError(response, '终端信息读取失败，请重试'));
                containerDetailControls(page);
            }, tab);
        }
        containerDetailControls(page);
    }
    function newContainerTerminal(tab) {
        const page = stackRoot(tab), data = page._pveKomodoStackData, source = tab._pveKomodoTerminalServices;
        const server = page.pveKomodoServerTerminalPage === true;
        const user = stackUser(page), generation = tab._pveKomodoStackTabGeneration;
        if (!user || !data || data.can_terminal !== true || !source || source.can_open !== true || optionBusy() || alive(tab._pveKomodoTerminalDialog)) return;
        if (prepareTerminalDialog(tab, newContainerTerminal)) return;
        const modes = (server ? [['default', '默认'], ['sh', 'sh'], ['bash', 'bash'], ['custom', '自定义命令']] :
            [['sh', 'sh'], ['bash', 'bash'], ['attach', '附加到主进程'], ['custom', '自定义命令']]).filter(function (mode) {
            return source.options[mode[0]] === true && source.shells.includes(mode[0]);
        });
        if (!modes.length) return;
        const dialog = Ext.create('Ext.window.Window', { title: server ? '新建服务器终端' : '新建容器终端', width: 560, modal: true, closeAction: 'destroy', bodyPadding: 16,
            layout: 'anchor', defaults: { anchor: '100%', labelWidth: 100 }, items: [
                { xtype: 'combobox', itemId: 'pve-komodo-terminal-shell', fieldLabel: '终端模式', editable: false, queryMode: 'local',
                    store: modes, value: modes[0][0], listeners: { change: function (field, value) {
                        const command = field.up('window').down('#pve-komodo-terminal-command'); command.setDisabled(field.getValue() !== 'custom');
                    } } },
                { xtype: 'textfield', itemId: 'pve-komodo-terminal-command', fieldLabel: '命令', emptyText: '例如 /bin/sh', disabled: modes[0][0] !== 'custom', maxLength: 2048, enforceMaxLength: true },
                { xtype: 'component', itemId: 'pve-komodo-container-terminal-error', html: '' },
                { xtype: 'component', html: server ? '连接关联服务器的 Periphery 终端。默认命令由 Periphery 配置决定；离开页面将关闭本次会话。' :
                    '附加模式连接容器主进程，其输入可能影响正在运行的服务。离开页面将清理本次终端会话。' },
            ], buttons: [{ text: '新建', itemId: 'pve-komodo-terminal-create', handler: function () {
                const shell = dialog.down('#pve-komodo-terminal-shell').getValue(), command = dialog.down('#pve-komodo-terminal-command').getValue();
                const requestId = uuid();
                if (!requestId || !modes.some(function (mode) { return mode[0] === shell; }) || stackUser(page) !== user || optionBusy() ||
                    !tab._pveKomodoStackTabActive || tab._pveKomodoStackTabGeneration !== generation || page._pveKomodoStackData !== data || tab._pveKomodoTerminalServices !== source) return;
                if (shell === 'custom' && (typeof command !== 'string' || !command.trim() || command.length > 2048 || /[\x00-\x1f\x7f]/.test(command))) {
                    dialog.down('#pve-komodo-container-terminal-error').update('请输入有效命令，最多 2048 个字符且不能包含控制字符'); return;
                }
                const payload = { shell: shell, request_id: requestId }; if (shell === 'custom') payload.command = command;
                if (server) { payload.server_id = data.server_id; payload.binding_revision = data.binding_revision; }
                dialog.destroy(); resetStackTerminal(tab);
                const state = { pending: true }; stackWriteState = state; stackControls(page);
                let finished = false;
                function complete(options, success, response) {
                    if (finished) return; finished = true; state.pending = false;
                    if (alive(activeStackPage)) stackControls(activeStackPage);
                    if (alive(activeDashboard)) updateControls(activeDashboard);
                    if (alive(activeGuestOptions)) optionControls(activeGuestOptions);
                    if (alive(activeGuestSummary)) summaryControls(activeGuestSummary);
                    if (stackUser(page) !== user || !alive(tab) || !tab._pveKomodoStackTabActive || tab._pveKomodoStackTabGeneration !== generation || page._pveKomodoStackData !== data) return;
                    let result;
                    try { result = JSON.parse(response.responseText); } catch (error) { /* Fixed response messages only. */ }
                    if (success && result && typeof result.grant === 'string' && result.grant && typeof result.session_id === 'string' &&
                        /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(result.session_id)) attachStackTerminal(tab, result, source.csrf_token, user);
                    else stackStatus(page, server ? optionsError(response, '终端创建结果未确认；请核对后重新新建，不会自动重试') :
                        containerDetailError(response, '终端创建结果未确认；请核对后重新新建，不会自动重试'));
                }
                try { Ext.Ajax.request({ url: page.pveKomodoStackPath + '/terminals', method: 'POST', timeout: 60000,
                    headers: { 'X-PVE-Komodo-CSRF': source.csrf_token }, jsonData: payload, callback: complete }); }
                catch (error) { complete(null, false, null); }
            } }, { text: '取消', handler: function () { dialog.destroy(); } }] });
        tab._pveKomodoTerminalDialog = dialog; dialog.show();
    }
    function containerDetailTab(kind, title, icon) {
        const config = kind === 'logs' || kind === 'terminal' ? stackTabConfig(kind, title, icon) :
            { xtype: 'panel', itemId: 'pve-komodo-container-' + kind, title: title, iconCls: 'fa ' + icon, border: false,
                bodyPadding: 8, layout: 'fit', onlineHelp: 'chapter_gui', pveKomodoStackTab: kind };
        config.pveKomodoContainerTab = kind;
        config.listeners = { activate: function (tab) {
            const page = stackRoot(tab); page._pveKomodoStackActive = true; page._pveKomodoStackTab = tab;
            activeStackPage = page; stackPages.add(page); tab._pveKomodoStackTabActive = true;
            tab._pveKomodoStackTabGeneration = (tab._pveKomodoStackTabGeneration || 0) + 1;
            clearContainerTab(tab);
            if (page._pveKomodoStackData) renderContainerTab(tab, page._pveKomodoStackData); else loadContainerDetails(page);
        }, deactivate: cancelStackTab, beforedestroy: cancelStackTab };
        if (kind === 'overview') {
            config.layout = { type: 'vbox', align: 'stretch' }; config.scrollable = true;
            const information = [['container_name', '名称'], ['container_id', '容器 ID'],
                ['server_name', '服务器'], ['stack_name', '应用栈'], ['image', '镜像'], ['image_id', '镜像 ID'],
                ['created', '创建时间']];
            const configuration = [['platform', '平台'], ['driver', '存储驱动'], ['hostname', '主机名'], ['user', '用户'], ['working_dir', '工作目录'],
                ['entrypoint', '入口点'], ['command', '命令'], ['network_mode', '网络模式'], ['networks', '网络'], ['ports', '端口'],
                ['volumes', '存储卷'], ['restart_policy', '重启策略'], ['exit_code', '退出代码'], ['oom_killed', 'OOM 终止'], ['pid', '进程 ID'], ['labels', '标签']];
            config.items = [{ xtype: 'container', layout: { type: 'hbox', align: 'stretch' }, height: 400, minHeight: 400, items: [
                { xtype: 'panel', title: '当前状态', itemId: 'pve-komodo-container-current', flex: 1, minWidth: 0,
                    margin: '0 8 0 0', bodyPadding: 10, layout: { type: 'vbox', align: 'stretch' },
                    items: [['state', '状态'], ['status', '状态详情'], ['health', '健康状态'], ['started', '启动时间'],
                        ['finished', '结束时间'], ['restarts', '重启次数'], ['cpu', 'CPU'], ['memory', '内存'], ['network', '网络 I/O'],
                        ['block', '磁盘 I/O'], ['pids', '进程数']].map(function (field) {
                        return { xtype: 'pmxInfoWidget', itemId: 'pve-komodo-container-current-' + field[0],
                            title: field[1], text: '-', value: 0, height: 30, printBar: ['cpu', 'memory'].includes(field[0]) };
                    }) },
                { xtype: 'panel', title: '服务器描述', flex: 1, minWidth: 0, margin: '0 8 0 0', layout: 'fit',
                    tbar: [{ text: '编辑', itemId: 'pve-komodo-container-description-edit', disabled: true,
                        handler: function (button) { editContainerDescription(button.up('[pveKomodoContainerTab]')); } }],
                    items: [{ xtype: 'component', itemId: 'pve-komodo-container-description', padding: 10, scrollable: true, html: '' }] },
                { xtype: 'gridpanel', itemId: 'pve-komodo-container-updates-grid', title: '服务器操作记录', flex: 1, minWidth: 0,
                    store: { autoDestroy: true, fields: ['id', 'operation', 'start_ts', 'status', 'success', 'username'], data: [] },
                    columns: [{ text: '时间', dataIndex: 'start_ts', width: 160, sortable: false, renderer: alertTime },
                        { text: '操作', dataIndex: 'operation', flex: 1, minWidth: 120, sortable: false, renderer: function (value) { return mappedText(operationLabels, value); } },
                        { text: '结果', dataIndex: 'status', width: 70, sortable: false, renderer: function (value, meta, record) { return updateResult(record.data); } },
                        { text: '操作人', dataIndex: 'username', width: 130, sortable: false, renderer: text }],
                    viewConfig: { stripeRows: true, enableTextSelection: true, emptyText: '暂无服务器操作记录', deferEmptyText: false },
                    listeners: { itemdblclick: function (view, record) { showUpdateDetails(this, record); } } },
            ] }, { xtype: 'container', layout: { type: 'hbox', align: 'stretch' }, height: 620, minHeight: 520, margin: '8 0 0 0', items: [
                containerPairGrid('information', '容器信息', information, { flex: 1, margin: '0 8 0 0' }),
                containerPairGrid('configuration', 'Docker 配置', configuration, { flex: 1 }),
            ] }];
        } else if (kind === 'inspect') {
            config.layout = 'auto'; config.scrollable = true;
            config.items = [{ xtype: 'component', itemId: 'pve-komodo-container-inspect', readOnly: true,
                ariaLabel: '容器检查 JSON', html: '' }];
        } else if (kind === 'logs') {
            config.tbar.items = config.tbar.items.filter(function (item) { return item.itemId !== 'pve-komodo-stack-log-services'; });
        } else if (kind === 'terminal') config.tbar[0].handler = function (button) { newContainerTerminal(button.up('[pveKomodoContainerTab]')); };
        return config;
    }
    function openContainerManagement(workspace, serverId, containerId, name) {
        if (!workspace || typeof workspace.setContent !== 'function' || dockerNavigationBusy() || !/^[0-9a-f]{24}$/i.test(serverId) || !/^[0-9a-f]{64}$/i.test(containerId)) return;
        const title = '容器：' + text(name || containerId);
        workspace.setContent({ xtype: 'pvePanelConfig', pveKomodoContainerPage: true, pveKomodoContainerServer: serverId, pveKomodoContainerId: containerId,
            pveKomodoStackPath: '/pve-komodo/api/servers/' + serverId + '/containers/' + containerId, _pveKomodoStackActive: true,
            workspace: workspace, title: title, onlineHelp: 'chapter_gui', showSearch: false, tbarSpacing: false,
            pveSelNode: { data: { id: 'pve-komodo-container:' + serverId + ':' + containerId, text: title } },
            tbar: containerDetailActions.map(function (action) { return { text: action[1], itemId: 'pve-komodo-container-' + action[0], disabled: true,
                handler: function (button) { const page = stackRoot(button), data = page._pveKomodoStackData;
                    const current = containerDetailActionSpec(data, action[0]);
                    if (containerDetailActionAllowed(data, current[0])) stackConfirm(page, '确认' + current[1], current[2],
                        { action: current[0], base_version: data.version }, page.pveKomodoStackPath + '/actions'); } }; }).concat([
                { text: '刷新', itemId: 'pve-komodo-container-refresh', handler: function (button) { loadContainerDetails(stackRoot(button)); } },
            ]), bbar: [{ xtype: 'tbtext', itemId: 'pve-komodo-stack-status', text: '尚未读取容器' }],
            items: [containerDetailTab('overview', '概览', 'fa-tachometer'), containerDetailTab('logs', '日志', 'fa-file-text-o'),
                containerDetailTab('inspect', '检查', 'fa-search'), containerDetailTab('terminal', '终端', 'fa-terminal')],
            listeners: { afterrender: function (page) { if (page.helpButton) page.helpButton.setHidden(true); }, beforedestroy: function (page) {
                page._pveKomodoStackActive = false; if (activeStackPage === page) activeStackPage = null; stackPages.delete(page); cancelStackPage(page);
            } },
        });
    }

    const guestInventoryPages = new Set();
    let guestInventoryWrite = null;
    const guestInventoryActions = {
        containers: [['start', '启动'], ['restart', '重启'], ['pause', '暂停'], ['unpause', '恢复'], ['stop', '停止'], ['destroy', '销毁']],
        networks: [['delete', '删除']], volumes: [['delete', '删除']], images: [['delete', '删除']],
        stacks: [['check_update', '检查更新'], ['pull', '拉取'], ['deploy', '部署'], ['restart', '重启'], ['stop', '停止'], ['destroy', '销毁'], ['delete', '删除']],
        deployments: [], repos: [],
    };
    const guestInventoryColumns = {
        containers: [['name', '名称'], ['state', '状态'], ['image', '镜像'], ['network_mode', '网络']],
        networks: [['name', '名称'], ['driver', '驱动'], ['scope', '作用域'], ['attachable', '允许连接'], ['ipam_driver', 'IPAM 驱动']],
        volumes: [['name', '名称'], ['driver', '驱动'], ['scope', '作用域']],
        images: [['name', '名称'], ['id', 'ID']],
        stacks: [['name', '名称'], ['source', '来源'], ['server_name', '主机'], ['state', '状态'], ['tags', '标签']],
        deployments: [['name', '名称'], ['image', '镜像'], ['server_name', '主机'], ['state', '状态'], ['tags', '标签']],
        repos: [['name', '名称'], ['repo', '代码仓库'], ['branch', '分支'], ['state', '状态'], ['tags', '标签']],
    };
    const guestInventoryTitles = { containers: '容器', networks: '网络', volumes: '存储卷', images: '镜像', stacks: '应用栈', deployments: '部署', repos: '代码仓库' };
    function inventoryOwner(component) { return component.up('[pveKomodoGuestInventory]'); }
    function inventoryUser(page) {
        const proxmox = window.Proxmox;
        return alive(page) && page._pveKomodoInventoryActive && !(page.workspace && page.workspace._pveKomodoLoggedOut) &&
            proxmox && proxmox.Utils && proxmox.Utils.authOK() && proxmox.UserName || null;
    }
    function inventorySections(page) { return page.pveKomodoGuestInventory === 'docker' ? ['containers', 'networks', 'volumes', 'images'] : ['stacks', 'deployments', 'repos']; }
    function inventoryStatus(page, message) {
        const label = page.down('#pve-komodo-guest-inventory-status'); if (alive(label)) label.setText(text(message));
    }
    function inventoryControls(page) {
        if (!alive(page)) return;
        const data = page._pveKomodoInventoryData;
        const busy = optionBusy() || !inventoryUser(page) || !!page._pveKomodoInventoryLoading || !!page._pveKomodoInventoryConfirmation;
        inventorySections(page).forEach(function (section) {
            const grid = page.down('#pve-komodo-guest-inventory-' + section + '-grid');
            if (!alive(grid)) return;
            const selected = grid.getSelectionModel().getSelection();
            guestInventoryActions[section].forEach(function (action) {
                const button = grid.down('#pve-komodo-guest-inventory-' + section + '-' + action[0]);
                if (alive(button)) button.setDisabled(busy || !data || !selected.length || selected.length > 50 ||
                    selected.some(function (row) { return !Array.isArray(row.get('allowed_actions')) || !row.get('allowed_actions').includes(action[0]); }));
            });
            const create = grid.down('#pve-komodo-guest-inventory-' + section + '-new');
            if (alive(create) && section === 'stacks') create.setDisabled(busy || !data || data.can_create_stack !== true);
        });
        const refresh = page.down('#pve-komodo-guest-inventory-refresh');
        if (alive(refresh)) refresh.setDisabled(busy);
    }
    function inventoryAllControls() {
        guestInventoryPages.forEach(inventoryControls);
        if (alive(activeStackPage)) stackControls(activeStackPage);
        if (alive(activeDashboard)) updateControls(activeDashboard);
        if (alive(activeGuestOptions)) optionControls(activeGuestOptions);
        if (alive(activeGuestSummary)) summaryControls(activeGuestSummary);
    }
    function cancelInventory(page) {
        page._pveKomodoInventoryGeneration = (page._pveKomodoInventoryGeneration || 0) + 1;
        if (page._pveKomodoInventoryRead) Ext.Ajax.abort(page._pveKomodoInventoryRead);
        page._pveKomodoInventoryRead = null; page._pveKomodoInventoryLoading = false; page._pveKomodoInventoryData = null;
        const confirmation = page._pveKomodoInventoryConfirmation; page._pveKomodoInventoryConfirmation = null;
        if (confirmation && alive(confirmation.dialog)) confirmation.dialog.destroy();
        if (alive(page._pveKomodoInventoryEditor)) page._pveKomodoInventoryEditor.destroy();
        page._pveKomodoInventoryEditor = null;
        inventorySections(page).forEach(function (section) {
            const grid = page.down('#pve-komodo-guest-inventory-' + section + '-grid');
            if (alive(grid)) { grid.getSelectionModel().deselectAll(); grid.getStore().loadData([]); }
        });
    }
    function filterInventory(page, section) {
        const data = page._pveKomodoInventoryData, grid = page.down('#pve-komodo-guest-inventory-' + section + '-grid');
        if (!alive(grid)) return;
        const field = grid.down('#pve-komodo-guest-inventory-' + section + '-search');
        const query = String(field && field.getValue() || '').toLowerCase();
        grid.getSelectionModel().deselectAll();
        grid.getStore().loadData(data ? data[section].filter(function (row) {
            return !query || guestInventoryColumns[section].some(function (column) {
                const value = row[column[0]]; return String(Array.isArray(value) ? value.join(' ') : value == null ? '' : value).toLowerCase().includes(query);
            });
        }) : []);
        inventoryControls(page);
    }
    function loadInventory(page) {
        const user = inventoryUser(page); if (!user || optionBusy() || page._pveKomodoInventoryConfirmation) return;
        cancelInventory(page); page._pveKomodoInventoryLoading = true;
        const generation = page._pveKomodoInventoryGeneration;
        inventoryStatus(page, '正在读取关联服务器的' + (page.pveKomodoGuestInventory === 'docker' ? ' Docker 数据…' : '资源…')); inventoryControls(page);
        let completed = false;
        function complete(options, success, response) {
            completed = true;
            if (inventoryUser(page) !== user || page._pveKomodoInventoryGeneration !== generation) return;
            page._pveKomodoInventoryLoading = false; page._pveKomodoInventoryRead = null;
            let data, message = optionsError(response, '数据读取失败，请刷新后重试');
            try {
                data = JSON.parse(response.responseText);
                if (success && data && serverReplyMatches(page, data) && /^[0-9a-f]{24}$/i.test(data.server_id) && Number.isSafeInteger(data.binding_revision) && (page.pveKomodoDirectServer ? data.binding_revision === 0 : data.binding_revision > 0) &&
                    typeof data.csrf_token === 'string' && data.csrf_token && !/[\r\n]/.test(data.csrf_token) &&
                    inventorySections(page).every(function (section) { return Array.isArray(data[section]); })) {
                    page._pveKomodoInventoryData = data;
                    inventorySections(page).forEach(function (section) { filterInventory(page, section); });
                    message = '服务器：' + (data.server_name || data.server_id) + (Array.isArray(data.errors) && data.errors.length ? '；部分数据不可用，请刷新核对' : '');
                }
            } catch (error) { /* Never expose raw upstream error text. */ }
            inventoryStatus(page, message); inventoryControls(page);
        }
        try {
            const request = Ext.Ajax.request({ url: page.pveKomodoGuestBase + '/' + page.pveKomodoGuestInventory,
                method: 'GET', disableCaching: false, timeout: 60000, callback: complete });
            if (!completed) page._pveKomodoInventoryRead = request;
        } catch (error) { complete(null, false, null); }
    }
    function confirmInventory(page, section, action, name) {
        const data = page._pveKomodoInventoryData, user = inventoryUser(page);
        if (!user || !data || optionBusy() || page._pveKomodoInventoryConfirmation) return;
        const grid = page.down('#pve-komodo-guest-inventory-' + section + '-grid');
        const selected = action === 'create_stack' ? [] : grid.getSelectionModel().getSelection();
        if (action === 'create_stack' ? !data.can_create_stack || typeof name !== 'string' || !/^[a-z0-9][a-z0-9_-]{0,255}$/.test(name.trim()) :
            !selected.length || selected.length > 50 || selected.some(function (row) { return !Array.isArray(row.get('allowed_actions')) || !row.get('allowed_actions').includes(action); })) return;
        const requestId = uuid(); if (!requestId) return;
        const payload = { server_id: data.server_id, binding_revision: data.binding_revision, section: section, action: action, request_id: requestId };
        if (action === 'create_stack') payload.name = name.trim();
        else payload.targets = selected.map(function (row) { return { id: row.get('id'), version: row.get('version') }; });
        const label = action === 'create_stack' ? '新建应用栈' : guestInventoryActions[section].find(function (item) { return item[0] === action; })[1];
        const effect = action === 'delete' ? '删除资源记录或 Docker 对象；删除存储卷会永久删除卷内数据。' :
            action === 'destroy' ? '销毁所选容器或应用栈容器，未持久化的数据会被删除。' : '此操作会作用于所选资源，可能改变运行状态或中断服务。';
        const generation = page._pveKomodoInventoryGeneration, dialog = Ext.create('Ext.window.MessageBox'), confirmation = { dialog: dialog };
        page._pveKomodoInventoryConfirmation = confirmation;
        dialog.show({ title: '确认' + label, buttons: Ext.Msg.YESNO, icon: Ext.Msg.WARNING,
            message: text('服务器：' + (data.server_name || data.server_id) + '。' + label + '：' + (name || selected.map(function (row) { return row.get('name') + ' [' + row.get('id') + ']'; }).join('、')) + '。' + effect),
            fn: function (answer) {
                if (page._pveKomodoInventoryConfirmation !== confirmation) return;
                page._pveKomodoInventoryConfirmation = null; if (alive(dialog)) dialog.destroy();
                if (inventoryUser(page) !== user || page._pveKomodoInventoryData !== data || page._pveKomodoInventoryGeneration !== generation || optionBusy()) return;
                if (answer !== 'yes') { inventoryControls(page); return; }
                if (alive(page._pveKomodoInventoryEditor)) page._pveKomodoInventoryEditor.destroy(); page._pveKomodoInventoryEditor = null;
                const state = { pending: true }; guestInventoryWrite = state; inventoryAllControls(); inventoryStatus(page, '正在提交，请勿重复操作…');
                let finished = false;
                function complete(options, success, response) {
                    if (finished) return; finished = true; state.pending = false;
                    let message = '结果尚未确认，请核对操作记录后刷新，不要重复提交';
                    try {
                        const result = JSON.parse(response.responseText);
                        const expected = payload.targets && payload.targets.map(function (target) { return target.id; });
                        if (success && result && result.section === section && result.action === action && Array.isArray(result.results) &&
                            (expected ? result.results.length === expected.length && new Set(result.results.map(function (row) { return row.id; })).size === expected.length &&
                                result.results.every(function (row) { return expected.includes(row.id); }) : result.results.length === 1 && /^[0-9a-f]{24}$/i.test(result.results[0].id)) &&
                            result.results.every(function (row) { return ['completed', 'submitted', 'failed', 'outcome_unknown', 'not_submitted'].includes(row.status) &&
                                row.ok === ['completed', 'submitted'].includes(row.status); })) {
                            const statuses = result.results.map(function (row) { return row.status; });
                            if (statuses.length && statuses.every(function (status) { return ['completed', 'submitted'].includes(status); })) message = '操作已提交，请刷新核对资源状态和操作记录';
                            else if (statuses.includes('failed')) message = '部分操作失败，请核对操作记录后刷新';
                        } else if (!success) message = optionsError(response, '提交失败，请核对资源状态后刷新');
                    } catch (error) { /* An uncertain write is never retried automatically. */ }
                    if (inventoryUser(page) === user && page._pveKomodoInventoryGeneration === generation) {
                        cancelInventory(page); inventoryStatus(page, message);
                    }
                    inventoryAllControls();
                }
                try { Ext.Ajax.request({ url: page.pveKomodoGuestBase + '/inventory-actions', method: 'POST', timeout: 120000,
                    headers: { 'X-PVE-Komodo-CSRF': data.csrf_token }, jsonData: copyOption(payload), callback: complete }); }
                catch (error) { complete(null, false, null); }
            } });
        inventoryControls(page);
    }
    function createInventoryStack(page) {
        const data = page._pveKomodoInventoryData;
        if (!inventoryUser(page) || !data || !data.can_create_stack || optionBusy() || alive(page._pveKomodoInventoryEditor)) return;
        const dialog = Ext.create('Ext.window.Window', { title: '新建应用栈', width: 420, modal: true, closeAction: 'destroy', bodyPadding: 12,
            items: [{ xtype: 'textfield', itemId: 'pve-komodo-inventory-stack-name', fieldLabel: '名称', anchor: '100%', maxLength: 256, enforceMaxLength: true },
                { xtype: 'component', itemId: 'pve-komodo-inventory-create-error', html: '' }],
            buttons: [{ text: '创建', handler: function () {
                const name = String(dialog.down('#pve-komodo-inventory-stack-name').getValue() || '').trim();
                if (!/^[a-z0-9][a-z0-9_-]{0,255}$/.test(name)) { dialog.down('#pve-komodo-inventory-create-error').update('名称须以小写字母或数字开头，仅包含小写字母、数字、下划线和连字符，最多 256 个字符'); return; }
                confirmInventory(page, 'stacks', 'create_stack', name);
            } },
                { text: '取消', handler: function () { dialog.destroy(); } }] });
        page._pveKomodoInventoryEditor = dialog; dialog.show();
    }
    function jumpInventory(page, section, record) {
        const data = page._pveKomodoInventoryData, config = guestOwner(page), workspace = config && config.workspace;
        if (!inventoryUser(page) || !data || !workspace || dockerNavigationBusy()) return;
        const id = record.get('id'), name = record.get('name');
        const selector = typeof workspace.down === 'function' && workspace.down('pveViewSelector');
        if (alive(selector) && typeof selector.getStore === 'function' && typeof selector.fireEvent === 'function') {
            const choice = selector.getStore().findRecord('key', containerViewId, 0, false, true, true);
            if (choice) { selector.setValue(containerViewId); selector.fireEvent('select', selector, [choice]); }
        }
        containerTreeStates.forEach(function (state) {
            if (state.workspace === workspace) state.selected = (section === 'containers' ? 'container:' : 'stack:') + data.server_id + ':' + id;
        });
        if (section === 'containers' && /^[0-9a-f]{64}$/i.test(id)) openContainerManagement(workspace, data.server_id, id, name);
        else if (section === 'stacks' && /^[0-9a-f]{24}$/i.test(id)) openStackManagement(workspace, id, data.server_id, name);
    }
    function inventoryGrid(section) {
        const toolbar = [];
        if (['stacks', 'deployments', 'repos'].includes(section)) toolbar.push({ text: '新建' + guestInventoryTitles[section], iconCls: 'fa fa-plus',
            itemId: 'pve-komodo-guest-inventory-' + section + '-new', disabled: true,
            tooltip: section === 'stacks' ? '在此服务器上新建应用栈' : '专属页面暂未开放', handler: function (button) { if (section === 'stacks') createInventoryStack(inventoryOwner(button)); } });
        guestInventoryActions[section].forEach(function (action) {
            toolbar.push({ text: action[1], itemId: 'pve-komodo-guest-inventory-' + section + '-' + action[0], disabled: true,
                handler: function (button) { confirmInventory(inventoryOwner(button), section, action[0]); } });
        });
        const columns = guestInventoryColumns[section].map(function (definition) { return {
            text: definition[1], dataIndex: definition[0], flex: definition[0] === 'image' || definition[0] === 'name' ? 2 : 1, minWidth: definition[0] === 'name' ? 130 : 100,
            renderer: function (value, meta, record) {
                if (definition[0] === 'state') return stateText(value);
                if (definition[0] === 'source') return mappedText({ host: '服务器上的文件', git: 'Git', ui: '界面配置', none: '无' }, value);
                if (definition[0] === 'tags') return tagsText(value);
                if (definition[0] === 'attachable') return value === true ? '是' : value === false ? '否' : '-';
                if (definition[0] === 'name' && record.get('builtin')) return text(value) + ' <span style="color:#808080">系统</span>';
                return text(value);
            },
        }; });
        if (['containers', 'stacks', 'deployments', 'repos'].includes(section)) columns.push({ xtype: 'actioncolumn', width: 36, sortable: false,
            items: [{ iconCls: 'fa fa-external-link', tooltip: ['containers', 'stacks'].includes(section) ? '跳转到专属页面' : '专属页面暂未开放',
                isDisabled: function () { return !['containers', 'stacks'].includes(section); }, handler: function (view, row, col, item, event, record) { jumpInventory(inventoryOwner(view), section, record); } }] });
        return { xtype: 'gridpanel', itemId: 'pve-komodo-guest-inventory-' + section + '-grid', title: guestInventoryTitles[section],
            height: 340, minWidth: 0, flex: 1, margin: '0 6 6 0',
            store: { autoDestroy: true, fields: ['id', 'name', 'version', 'allowed_actions', 'builtin'].concat(guestInventoryColumns[section].map(function (column) { return column[0]; })), data: [] },
            selModel: { selType: 'checkboxmodel', mode: 'MULTI', checkOnly: true, listeners: { beforeselect: function (model, record) { return record.get('builtin') !== true; } } },
            columns: columns, tbar: { overflowHandler: 'scroller', items: toolbar },
            bbar: [{ xtype: 'textfield', itemId: 'pve-komodo-guest-inventory-' + section + '-search', emptyText: '搜索…', flex: 1,
                listeners: { change: function (field) { filterInventory(inventoryOwner(field), section); } } }],
            viewConfig: { stripeRows: true, enableTextSelection: true, emptyText: '无匹配结果', deferEmptyText: false,
                getRowClass: function (record) { return record.get('builtin') ? 'pve-komodo-inventory-system-row' : ''; } },
            listeners: { selectionchange: function () { inventoryControls(inventoryOwner(this)); } },
        };
    }
    function guestInventoryConfig(node, kind) {
        const sections = kind === 'docker' ? ['containers', 'networks', 'volumes', 'images'] : ['stacks', 'deployments', 'repos'];
        const items = kind === 'docker' ? [0, 2].map(function (index) { return { xtype: 'container', height: 346,
            layout: { type: 'hbox', align: 'stretch' }, items: sections.slice(index, index + 2).map(inventoryGrid) }; }) : sections.map(inventoryGrid);
        function leave(page) { page._pveKomodoInventoryActive = false; cancelInventory(page); guestInventoryPages.delete(page); }
        return { xtype: 'panel', itemId: guestItemId + '-' + kind, title: kind === 'docker' ? 'DOCKER' : '资源', iconCls: kind === 'docker' ? 'fa fa-cubes' : 'fa fa-th',
            groups: [guestItemId], border: false, pveKomodoGuestInventory: kind, pveKomodoGuestBase: serverPageBase(node),
            layout: 'anchor', defaults: { anchor: '100%' }, scrollable: true, bodyPadding: 6, items: items,
            tbar: [{ text: '刷新', itemId: 'pve-komodo-guest-inventory-refresh', handler: function (button) { loadInventory(inventoryOwner(button)); } }],
            bbar: [{ xtype: 'tbtext', itemId: 'pve-komodo-guest-inventory-status', text: '尚未读取关联服务器' }],
            listeners: { activate: function (page) { const config = guestOwner(page); page.workspace = config && config.workspace;
                page._pveKomodoInventoryActive = true; guestInventoryPages.add(page); loadInventory(page); }, deactivate: leave, beforedestroy: leave },
        };
    }
    function serverTerminalControls(page) {
        const tab = page._pveKomodoStackTab, data = page._pveKomodoStackData;
        const button = alive(tab) && tab.down('#pve-komodo-stack-new-terminal');
        if (alive(button)) button.setDisabled(optionBusy() || !stackUser(page) || !data || !data.can_terminal || !tab._pveKomodoTerminalServices);
    }
    function loadServerTerminal(page) {
        const user = stackUser(page), tab = page.down('#pve-komodo-stack-terminal');
        if (!user || !alive(tab) || optionBusy()) return;
        cancelStackTab(tab); tab._pveKomodoStackTabActive = true; page._pveKomodoStackTab = tab;
        tab._pveKomodoTerminalServices = null; page._pveKomodoStackData = null;
        stackStatus(page, '正在读取服务器终端权限…'); serverTerminalControls(page);
        stackRequest(page, '/terminal-services', undefined, function (success, source, response) {
            if (success && source && serverReplyMatches(page, source) && /^[0-9a-f]{24}$/i.test(source.server_id) && Number.isSafeInteger(source.binding_revision) && (page.pveKomodoDirectServer ? source.binding_revision === 0 : source.binding_revision > 0) &&
                Array.isArray(source.shells) && source.options && typeof source.csrf_token === 'string' && source.csrf_token && !/[\r\n]/.test(source.csrf_token)) {
                page._pveKomodoStackData = Object.assign({}, source, { can_terminal: source.can_open === true });
                tab._pveKomodoTerminalServices = source;
                stackStatus(page, source.can_open ? '选择新建以打开服务器终端' : '没有访问此服务器终端的权限或终端已禁用');
            } else stackStatus(page, optionsError(response, '服务器终端权限读取失败，请刷新重试'));
            serverTerminalControls(page);
        }, tab);
    }
    function guestTerminalConfig(node) {
        const tab = stackTabConfig('terminal', '终端', 'fa-terminal');
        tab.header = false;
        tab.listeners = { beforedestroy: cancelStackTab };
        tab.tbar[0].handler = function (button) { newContainerTerminal(button.up('[pveKomodoStackTab]')); };
        function leave(page) { page._pveKomodoStackActive = false; if (activeStackPage === page) activeStackPage = null; stackPages.delete(page); cancelStackPage(page); }
        return { xtype: 'panel', itemId: guestItemId + '-terminal', title: '终端', iconCls: 'fa fa-terminal', groups: [guestItemId], border: false,
            pveKomodoServerTerminalPage: true, pveKomodoStackPath: serverPageBase(node), layout: 'fit', items: [tab],
            bbar: [{ xtype: 'tbtext', itemId: 'pve-komodo-stack-status', text: '尚未读取服务器终端权限' }],
            listeners: { activate: function (page) { const config = guestOwner(page); page.workspace = config && config.workspace;
                page._pveKomodoStackActive = true; page._pveKomodoStackGeneration = (page._pveKomodoStackGeneration || 0) + 1;
                stackPages.add(page); activeStackPage = page; loadServerTerminal(page); }, deactivate: leave, beforedestroy: leave },
        };
    }

    function openServerManagement(workspace, serverId, name) {
        if (!alive(workspace) || typeof workspace.setContent !== 'function' || dockerNavigationBusy() || !/^[0-9a-f]{24}$/i.test(serverId)) return;
        const node = { server_id: serverId }, title = '服务器：' + text(name || serverId);
        const cards = [guestCardConfig(node), guestOptionsConfig(node), guestInventoryConfig(node, 'docker'), guestInventoryConfig(node, 'resources'), guestTerminalConfig(node)];
        cards.forEach(function (card) { card.pveKomodoDirectServer = serverId; card._pveKomodoExpectedServer = serverId; });
        requestedGuestServer = null;
        workspace.setContent({ xtype: 'pvePanelConfig', pveKomodoServerPage: true, pveKomodoServerId: serverId,
            workspace: workspace, title: title, onlineHelp: 'chapter_gui', showSearch: false, tbarSpacing: false,
            pveSelNode: { data: { id: 'pve-komodo-server:' + serverId, text: title } }, items: cards,
            listeners: { afterrender: function (page) { if (page.helpButton) page.helpButton.setHidden(true); } },
        });
    }

    function blankCards() {
        return pages.map(function (page) {
            const card = {
                xtype: 'panel',
                itemId: page.id,
                title: page.title,
                iconCls: page.icon,
                border: false,
                items: [],
                pveKomodoRoute: page.route,
            };
            if (page.id === itemId) {
                Object.assign(card, dashboardConfig());
                card.expandedOnInit = true;
            } else {
                // PVE consumes groups using shift(), so every card needs its own
                // fresh array, with the parent declared before its children.
                card.groups = [itemId];
                if (page.id === itemId + '-containers') Object.assign(card, containersConfig());
                if (page.id === itemId + '-alerts') Object.assign(card, alertsConfig());
                if (page.id === itemId + '-updates') Object.assign(card, updatesConfig());
            }
            return card;
        });
    }

    try {
        // Crisp uses the same pale blue for progress fills and selected rows.
        // Scope the stronger fill to our widgets; keep native track/background
        // text colors and use PVE's existing fill variable in the dark theme.
        if (Ext.util && Ext.util.CSS && typeof Ext.util.CSS.createStyleSheet === 'function') {
            Ext.util.CSS.createStyleSheet(
                '.x-progress-default.pve-komodo-usage-progress .x-progress-bar-default {' +
                    'background-color:var(--pwt-gauge-default,#0060a4);' +
                '}' +
                '.x-progress-default.pve-komodo-usage-progress .x-progress-bar .x-progress-text {' +
                    'color:#fff;' +
                '}' +
                '.pve-komodo-stack-config-grid .pve-komodo-stack-heading-major .x-grid-cell {' +
                    'background-color:#666!important;color:#fff!important;border-color:#666!important;font-weight:600;' +
                '}' +
                '.pve-komodo-stack-config-grid .pve-komodo-stack-heading-minor .x-grid-cell {' +
                    'background-color:#e6e6e6!important;color:#333!important;border-color:#e6e6e6!important;font-weight:600;vertical-align:middle;' +
                '}' +
                '.pve-komodo-stack-config-grid .pve-komodo-stack-heading-minor .x-grid-cell:last-child {font-weight:inherit;}' +
                '.pve-komodo-stack-config-grid .pve-komodo-stack-heading-minor .x-grid-cell:last-child .x-grid-cell-inner {' +
                    'white-space:normal;' +
                '}' +
                '.pve-komodo-stack-config-grid .pve-komodo-stack-heading-description {' +
                    'color:#808080;' +
                '}' +
                '.pve-komodo-container-information .x-grid-cell-inner {white-space:pre-wrap;overflow-wrap:anywhere;}' +
                '.pve-komodo-inventory-system-row .x-grid-checkcolumn {opacity:.35;cursor:default;}' +
                '.pve-komodo-container-inspect-json {margin:0;padding:8px;box-sizing:border-box;min-width:100%;width:max-content;font:13px/20px monospace;white-space:pre;}' +
                '.pve-komodo-json-line {display:block;min-height:20px;}' +
                '.pve-komodo-json-line-number {display:inline-block;width:4em;padding-right:1em;color:#808080;text-align:right;user-select:none;}' +
                '.pve-komodo-json-key {color:#b14500;}.pve-komodo-json-string {color:#005cc5;}' +
                '.pve-komodo-json-number {color:#098658;}.pve-komodo-json-literal {color:#7c3aed;}' +
                '.pve-komodo-stack-terminal-output {background:#000;overflow:hidden;padding:8px;}' +
                '.pve-komodo-stack-terminal-output>.xterm {height:100%;}' +
                '.pve-komodo-stack-terminal-tab:fullscreen {width:100vw!important;height:100vh!important;background:#000;}' +
                '.pve-komodo-stack-terminal-tab:fullscreen>.x-panel-bodyWrap {display:flex;flex-direction:column;width:100%;height:100%;}' +
                '.pve-komodo-stack-terminal-tab:fullscreen>.x-panel-bodyWrap>.x-toolbar {' +
                    'position:relative!important;top:auto!important;left:auto!important;width:100%!important;flex:0 0 auto;' +
                '}' +
                '.pve-komodo-stack-terminal-tab:fullscreen>.x-panel-bodyWrap>.x-panel-body {' +
                    'position:relative!important;top:auto!important;left:auto!important;width:100%!important;height:0!important;min-height:0;flex:1 1 0;' +
                '}' +
                '.pve-komodo-stack-terminal-tab:fullscreen .pve-komodo-stack-terminal-output {width:100%!important;height:100%!important;}' +
                '.pve-komodo-stack-terminal-output .xterm-viewport {overflow-y:scroll;}',
                'pve-komodo-usage-progress-style',
            );
        }
        Ext.define(overrideName, {
            override: 'PVE.dc.Config',

            // PVE.dc.Config replaces me.items in initComponent. Its parent then
            // builds both the navigation tree and saved cards through insertNodes.
            // Override this inherited method only on the datacenter class, while
            // its declarative items still exist, rather than editing a live tree.
            // Verified with pve-manager 9.2.22 (aa7db891):
            // www/manager6/dc/Config.js and panel/ConfigPanel.js.
            insertNodes: function (items) {
                if (Array.isArray(items) && !items.some(function (item) {
                    return item && pageIds.has(item.itemId);
                })) {
                    // Ceph is a root card with itemId "ceph". Keep every official
                    // item in order; if capabilities hide Ceph, append safely.
                    const ceph = items.findIndex(function (item) {
                        return item && item.itemId === 'ceph';
                    });
                    const position = ceph < 0 ? items.length : ceph + 1;
                    items = items.slice(0, position).concat(blankCards(), items.slice(position));
                }

                return this.callParent([items]);
            },
        });
        ['PVE.qemu.Config', 'PVE.lxc.Config'].forEach(function (className) {
            const GuestConfig = Ext.ClassManager.get(className);
            if (!GuestConfig || !GuestConfig.prototype || typeof GuestConfig.prototype.insertNodes !== 'function') return;
            Ext.define('PVEKomodo.override.' + (className === 'PVE.qemu.Config' ? 'VMConfig' : 'CTConfig'), {
                override: className,
                insertNodes: function (items) {
                    const node = this.pveSelNode && this.pveSelNode.data;
                    if (Array.isArray(items) && node && (node.type === 'qemu' || node.type === 'lxc') &&
                        Number.isSafeInteger(Number(node.vmid)) && Number(node.vmid) > 0 &&
                        !items.some(function (item) { return item && (item.itemId === guestItemId || item.itemId === guestItemId + '-options'); })) {
                        items = items.concat([guestCardConfig(node), guestOptionsConfig(node), guestInventoryConfig(node, 'docker'), guestInventoryConfig(node, 'resources'), guestTerminalConfig(node)]);
                    }
                    return this.callParent([items]);
                },
            });
        });
        installContainerView();
        // ExtJS override declarations are not ordinary ClassManager classes.
        // Keep our own marker so a second script load cannot wrap the method again.
        window[registrationKey] = true;
    } catch (error) {
        // The extension must not stop PVE from booting if registration is refused.
        if (window.console && typeof window.console.warn === 'function') {
            window.console.warn('PVE Komodo extension could not be registered.', error);
        }
    }
}());
