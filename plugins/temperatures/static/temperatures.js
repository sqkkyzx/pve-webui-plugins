(function () {
    // ExtJS callParent uses caller metadata. Keep override methods non-strict.
    const Ext = window.Ext;
    const registration = '__pveNodeTemperaturesLoaded';
    if (window[registration] || !Ext || !Ext.ClassManager || typeof Ext.ClassManager.get !== 'function' ||
        typeof Ext.define !== 'function') return;
    const Status = Ext.ClassManager.get('PVE.node.StatusView');
    const Info = Ext.ClassManager.get('Proxmox.widget.Info');
    if (!Status || !Status.prototype || typeof Status.prototype.initComponent !== 'function' ||
        !Info || !Info.prototype || typeof Info.prototype.updateValue !== 'function') return;

    const states = new Set();
    const interval = 10000;
    const categories = { cpu: 'CPU', board: '主板', disk: '磁盘', other: '其他' };
    function alive(component) { return component && !component.destroyed && !component.destroying; }
    function encoded(value) {
        return String(value == null ? '' : value).replace(/[&<>"']/g, function (character) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character];
        });
    }
    function user(state) {
        const proxmox = window.Proxmox;
        if (!proxmox || state.workspace && state.workspace._pveTemperaturesLoggedOut ||
            proxmox.Utils && typeof proxmox.Utils.authOK === 'function' && !proxmox.Utils.authOK()) return null;
        return typeof proxmox.UserName === 'string' && proxmox.UserName ? proxmox.UserName : null;
    }
    function nodeOf(view) { return view.pveSelNode && view.pveSelNode.data && view.pveSelNode.data.node; }
    function current(state, generation, expectedUser) {
        return state.active && alive(state.view) && generation === state.generation && expectedUser === user(state) &&
            state.node === nodeOf(state.view) && (!state.summary || !state.summary.isVisible || state.summary.isVisible(true));
    }
    function closeDetails(state) {
        const dialog = state.dialog;
        state.dialog = null;
        if (alive(dialog)) dialog.destroy();
    }
    function cancel(state) {
        state.generation++;
        if (state.timer !== null) window.clearTimeout(state.timer);
        state.timer = null;
        const request = state.request;
        state.request = null;
        if (request && Ext.Ajax && typeof Ext.Ajax.abort === 'function') Ext.Ajax.abort(request);
    }
    function valueText(value) { return Number(value.toFixed(1)) + ' °C'; }
    function colored(sensor, text, fresh) {
        let color = '';
        if (fresh && sensor.critical_celsius !== null && sensor.celsius >= sensor.critical_celsius) color = '#d9534f';
        else if (fresh && sensor.max_celsius !== null && sensor.celsius >= sensor.max_celsius) color = '#b56e00';
        return color ? '<span style="color:' + color + '">' + encoded(text) + '</span>' : encoded(text);
    }
    function highest(rows) {
        return rows.reduce(function (result, sensor) { return !result || sensor.celsius > result.celsius ? sensor : result; }, null);
    }
    function cpuCandidates(rows) {
        const chips = new Map();
        rows.forEach(function (sensor) {
            if (!chips.has(sensor.chip)) chips.set(sensor.chip, []);
            chips.get(sensor.chip).push(sensor);
        });
        const chosen = [];
        chips.forEach(function (sensors) {
            const packages = sensors.filter(function (sensor) { return /\bpackage(?:\s+id)?\b/i.test(sensor.label); });
            const dies = sensors.filter(function (sensor) { return /^tdie$/i.test(sensor.label.trim()); });
            const readings = sensors.filter(function (sensor) { return !/^tctl$/i.test(sensor.label.trim()); });
            chosen.push.apply(chosen, packages.length ? packages : dies.length ? dies : readings.length ? readings : sensors);
        });
        return chosen;
    }
    function summaryText(rows, category) {
        const systemChannels = category === 'board' ? rows.filter(function (sensor) {
            return /^(?:motherboard|mainboard|system|systin)(?:[\s_-]*(?:temp(?:erature)?|\d+))?$/i.test(sensor.label.trim());
        }) : [];
        const candidates = category === 'cpu' ? cpuCandidates(rows) : systemChannels.length ? systemChannels : rows;
        const sensor = highest(candidates);
        if (!sensor) return '未知';
        let text = (candidates.length > 1 ? '最高 ' : '') + valueText(sensor.celsius);
        if (category === 'cpu' && /^tctl$/i.test(sensor.label.trim())) text += '（控制温度）';
        else if ((category === 'cpu' && !/\bpackage(?:\s+id)?\b/i.test(sensor.label) && !/^tdie$/i.test(sensor.label.trim())) ||
            (category === 'board' && !systemChannels.length)) text += '（' + sensor.label.trim() + '）';
        if (category === 'disk') text += '（' + new Set(rows.map(function (row) { return row.chip; })).size + ' 块）';
        return colored(sensor, text, true);
    }
    function updateSummary(state, message) {
        if (!alive(state.view)) return;
        ['cpu', 'board', 'disk'].forEach(function (category) {
            const widget = state.view.down('#pve-node-temperature-' + category);
            if (alive(widget)) widget.updateValue(message || summaryText(state.data.sensors.filter(function (row) {
                return row.category === category;
            }), category));
        });
        const button = state.view.down('#pve-node-temperature-details');
        if (alive(button)) button.setDisabled(!state.active || !state.data || !['ok', 'stale', 'no_sensors'].includes(state.data.status));
    }
    function clear(state, message) {
        state.data = null;
        closeDetails(state);
        updateSummary(state, encoded(message));
    }
    function leave(state, message) {
        state.active = false;
        cancel(state);
        clear(state, message || '未知');
    }
    function validData(data, node) {
        if (!data || data.version !== 1 || data.node !== node || !['ok', 'not_configured', 'unavailable', 'stale', 'no_sensors'].includes(data.status) ||
            !(data.sampled_at === null || typeof data.sampled_at === 'number' && Number.isFinite(data.sampled_at) && data.sampled_at >= 0) ||
            !Array.isArray(data.sensors) || data.sensors.length > 512) return false;
        return data.sensors.every(function (sensor) {
            return sensor && ['id', 'chip', 'label'].every(function (key) { return typeof sensor[key] === 'string' && sensor[key].length <= 1024; }) &&
                Object.hasOwn(categories, sensor.category) && typeof sensor.celsius === 'number' && Number.isFinite(sensor.celsius) &&
                ['max_celsius', 'critical_celsius'].every(function (key) { return sensor[key] === null || typeof sensor[key] === 'number' && Number.isFinite(sensor[key]); });
        });
    }
    function sampledText(data) {
        if (data.sampled_at === null) return '未知';
        const date = new Date(data.sampled_at * 1000);
        return Number.isFinite(date.getTime()) ? date.toLocaleString() : '未知';
    }
    function detailStatus(data) {
        const status = { ok: '当前采样', stale: '数据已过期，请检查采集器', no_sensors: '未检测到温度传感器' }[data.status] || '不可用';
        return encoded(status + ' · 采样时间：' + sampledText(data));
    }
    function detailRows(data) {
        return data.sensors.map(function (sensor) { return Object.assign({}, sensor, { sampled_at: sampledText(data), stale: data.status === 'stale' }); });
    }
    function refreshDetails(state) {
        if (!alive(state.dialog) || !state.data) return;
        const grid = state.dialog.down('#pve-node-temperature-grid');
        const status = state.dialog.down('#pve-node-temperature-detail-status');
        if (alive(grid)) grid.getStore().loadData(detailRows(state.data));
        if (alive(status)) status.setText(detailStatus(state.data));
    }
    function showDetails(state) {
        if (!state.active || !state.data || !current(state, state.generation, state.user)) return;
        if (alive(state.dialog)) { state.dialog.show(); return; }
        const temperatureRenderer = function (value, meta, record) {
            const sensor = record.data;
            return colored(sensor, (sensor.stale ? '最近 ' : '') + valueText(value), !sensor.stale);
        };
        const limitRenderer = function (value) { return value === null ? '未知' : encoded(valueText(value)); };
        state.dialog = Ext.create('Ext.window.Window', {
            title: '温度明细 · ' + encoded(state.node), width: 900, height: 460, minWidth: 600, minHeight: 280,
            modal: true, constrain: true, layout: 'fit', closeAction: 'destroy',
            items: [{ xtype: 'grid', itemId: 'pve-node-temperature-grid', border: false,
                store: { fields: ['id', 'chip', 'label', 'category', 'celsius', 'max_celsius', 'critical_celsius', 'sampled_at', 'stale'], data: detailRows(state.data) },
                viewConfig: { enableTextSelection: true, emptyText: '暂无可显示的温度数据', deferEmptyText: false },
                columns: [
                    { text: '分类', dataIndex: 'category', width: 70, renderer: function (value) { return categories[value] || '其他'; } },
                    { text: '设备', dataIndex: 'chip', flex: 1, minWidth: 130, renderer: encoded },
                    { text: '通道', dataIndex: 'label', flex: 1, minWidth: 120, renderer: encoded },
                    { text: '温度', dataIndex: 'celsius', width: 105, renderer: temperatureRenderer },
                    { text: '警戒阈值', dataIndex: 'max_celsius', width: 95, renderer: limitRenderer },
                    { text: '严重阈值', dataIndex: 'critical_celsius', width: 95, renderer: limitRenderer },
                    { text: '采样时间', dataIndex: 'sampled_at', width: 170, renderer: encoded },
                ],
            }],
            bbar: [{ xtype: 'tbtext', itemId: 'pve-node-temperature-detail-status', text: detailStatus(state.data) }],
            listeners: { destroy: function (dialog) { if (state.dialog === dialog) state.dialog = null; } },
        });
        state.dialog.show();
    }
    function schedule(state, generation, expectedUser) {
        if (!current(state, generation, expectedUser)) { if (state.active) leave(state); return; }
        state.timer = window.setTimeout(function () { state.timer = null; read(state, generation, expectedUser); }, interval);
    }
    function read(state, generation, expectedUser) {
        if (!current(state, generation, expectedUser)) { if (state.active) leave(state); return; }
        let completed = false;
        function complete(options, success, response) {
            if (completed) return;
            completed = true;
            if (!current(state, generation, expectedUser)) { if (state.active && generation === state.generation) leave(state); return; }
            state.request = null;
            if (response && (response.status === 401 || response.status === 403)) {
                leave(state, response.status === 401 ? '会话已过期' : '无访问权限'); return;
            }
            let data = null;
            if (success) { try { data = JSON.parse(response.responseText); } catch (error) { /* Treat malformed responses as unavailable. */ } }
            if (!validData(data, state.node)) clear(state, '不可用');
            else {
                state.data = data;
                const message = { not_configured: '未配置', unavailable: '不可用', stale: '已过期', no_sensors: '未知' }[data.status];
                updateSummary(state, message);
                if (!['ok', 'stale', 'no_sensors'].includes(data.status)) closeDetails(state);
                refreshDetails(state);
            }
            schedule(state, generation, expectedUser);
        }
        try {
            const request = Ext.Ajax.request({ url: '/pve-plugins/api/temperatures/nodes/' + encodeURIComponent(state.node),
                method: 'GET', disableCaching: false, timeout: 15000, callback: complete });
            if (!completed) state.request = request;
        } catch (error) { complete(null, false, null); }
    }
    function activate(state) {
        if (!alive(state.view)) return;
        if (state.active && state.user === user(state) && state.node === nodeOf(state.view) &&
            (state.request || state.timer !== null)) return;
        cancel(state);
        state.active = true;
        state.user = user(state);
        clear(state, state.user ? '正在读取…' : '请先登录 PVE');
        if (!state.user) { state.active = false; return; }
        read(state, state.generation, state.user);
    }
    function attach(view) {
        if (view._pveNodeTemperatures || typeof view.insert !== 'function' || typeof view.down !== 'function') return;
        const node = nodeOf(view);
        if (typeof node !== 'string' || !node || node.length > 255) return;
        const state = { view: view, node: node, generation: 0, active: false, timer: null, request: null, dialog: null,
            data: null, user: null, summary: null, workspace: null, originalHeight: view.height };
        view._pveNodeTemperatures = state;
        const widgets = ['cpu', 'board', 'disk'].map(function (category) {
            return { xtype: 'pmxInfoWidget', itemId: 'pve-node-temperature-' + category, pveNodeTemperatureWidget: true,
                title: categories[category] + ' 温度', iconCls: 'fa fa-fw fa-thermometer-half', printBar: false,
                text: '未知', value: undefined };
        });
        widgets.push({ xtype: 'container', pveNodeTemperatureWidget: true, layout: { type: 'hbox', pack: 'end' }, padding: '0 10 5 10',
            items: [{ xtype: 'button', itemId: 'pve-node-temperature-details', text: '温度明细', iconCls: 'fa fa-list', disabled: true,
                handler: function () { showDetails(state); } }] });
        const anchor = view.down('#cpus');
        const index = anchor && view.items && typeof view.items.indexOf === 'function' ? view.items.indexOf(anchor) : -1;
        view.insert(index >= 0 ? index : view.items.length, widgets);
        const height = Number(view.height);
        if (typeof view.setHeight === 'function') view.setHeight((Number.isFinite(height) && height > 0 ? height : 350) + 50);
        states.add(state);
        view.on('afterrender', function () {
            state.summary = view.up('pveNodeSummary');
            state.workspace = view.up('pveStdWorkspace');
            if (state.summary) {
                view.mon(state.summary, 'activate', function () { activate(state); });
                view.mon(state.summary, 'deactivate', function () { leave(state); });
                view.mon(state.summary, 'hide', function () { leave(state); });
            }
            if (!view.isVisible || view.isVisible(true)) activate(state);
        });
        view.on('beforedestroy', function () { leave(state); states.delete(state); });
    }
    try {
        Ext.define('PVETemperatures.override.NodeStatusTemperatures', { override: 'PVE.node.StatusView', initComponent: function () {
            const result = this.callParent(arguments);
            try { attach(this); }
            catch (error) {
                const state = this._pveNodeTemperatures;
                if (state) {
                    leave(state); states.delete(state);
                    if (this.items && typeof this.items.each === 'function' && typeof this.remove === 'function') {
                        const added = [];
                        this.items.each(function (item) { if (item.pveNodeTemperatureWidget) added.push(item); });
                        added.forEach(function (item) { this.remove(item, true); }, this);
                    }
                    if (state.originalHeight !== undefined && typeof this.setHeight === 'function') this.setHeight(state.originalHeight);
                    delete this._pveNodeTemperatures;
                }
                if (window.console && typeof window.console.warn === 'function') window.console.warn('PVE node temperatures could not be attached.', error);
            }
            return result;
        } });
        const Summary = Ext.ClassManager.get('PVE.node.Summary');
        if (Summary && Summary.prototype && typeof Summary.prototype.initComponent === 'function') {
            Ext.define('PVETemperatures.override.NodeTemperatureSummaryLayout', { override: 'PVE.node.Summary', initComponent: function () {
                const result = this.callParent(arguments);
                const summary = this;
                const status = this.nodeStatus;
                if (status && status._pveNodeTemperatures && typeof this.query === 'function') {
                    // The native column layout floats cards. Equal first-row
                    // heights keep the next charts from flowing under CPU only.
                    const cpu = this.query('proxmoxRRDChart').find(function (chart) {
                        return Array.isArray(chart.fields) && chart.fields.includes('cpu') && chart.fields.includes('iowait');
                    });
                    if (cpu && typeof cpu.setHeight === 'function') {
                        const baseHeight = Number(status.height);
                        let aligning = false;
                        function align() {
                            if (aligning || !alive(summary) || !alive(status) || !alive(cpu)) return;
                            aligning = true;
                            try {
                                let height = baseHeight;
                                const table = status.rendered && status.getLayout().table;
                                if (table && status.body) {
                                    // A forced narrow column can wrap native values.
                                    // Grow both cards just enough to keep every row visible.
                                    height = Math.max(height, Math.ceil(table.getHeight() + status.body.getPadding('tb') +
                                        status.getHeight() - status.body.getHeight()));
                                }
                                if (Number.isFinite(height)) {
                                    if (status.height !== height) status.setHeight(height);
                                    if (cpu.height !== height) cpu.setHeight(height);
                                }
                            } finally { aligning = false; }
                        }
                        align();
                        this.mon(status, 'afterlayout', align);
                    }
                }
                return result;
            } });
        }
        window[registration] = true;
        const Workspace = Ext.ClassManager.get('PVE.StdWorkspace');
        if (Workspace && Workspace.prototype && typeof Workspace.prototype.onLogin === 'function') {
            Ext.define('PVETemperatures.override.NodeTemperatureSession', { override: 'PVE.StdWorkspace', onLogin: function (loginData) {
                const workspace = this;
                workspace._pveTemperaturesLoggedOut = !loginData;
                if (!loginData) states.forEach(function (state) {
                    if (!state.workspace || state.workspace === workspace) leave(state, '请先登录 PVE');
                });
                const result = this.callParent(arguments);
                if (loginData) states.forEach(function (state) {
                    if (state.workspace === workspace && alive(state.view) && (!state.summary || !state.summary.isVisible || state.summary.isVisible(true))) activate(state);
                });
                return result;
            } });
        }
    } catch (error) {
        if (window.console && typeof window.console.warn === 'function') window.console.warn('PVE node temperature extension could not be registered.', error);
    }
}());
