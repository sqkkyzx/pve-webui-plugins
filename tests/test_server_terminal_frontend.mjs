/** Server terminal card lifecycle and binding contract with the real functions. */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../plugins/komodo/static/extension.js', import.meta.url), 'utf8');
function section(start, end) {
    const a = source.indexOf('    function ' + start), b = source.indexOf('    function ' + end, a);
    assert.ok(a >= 0 && b > a, 'real extension function boundary exists');
    return source.slice(a, b);
}
const functions = [section('serverPageBase(', 'openServerCreate('), section('stackRoot(', 'loadStackDetails('), section('stackTabConfig(', 'openStackManagement('),
    section('prepareTerminalDialog(', 'observeStackTerminal('), section('newContainerTerminal(', 'containerDetailTab('),
    section('serverTerminalControls(', 'blankCards(')].join('\n');

function setup() {
    const requests = [], dialogs = [], attached = [];
    let context;
    function component(config, owner = null) {
        const result = { ...config, ownerCt: owner, destroyed: false, value: config.value,
            down(selector) {
                for (const child of this.children || []) {
                    if (selector === '#' + child.itemId) return child;
                    const found = child.down(selector); if (found) return found;
                }
                return null;
            },
            up(selector) {
                for (let parent = this.ownerCt; parent; parent = parent.ownerCt) {
                    if (selector === 'window' ? parent.xtype === 'window' : parent[selector.slice(1, -1)] !== undefined) return parent;
                }
                return null;
            },
            setDisabled(value) { this.disabled = value; }, setText(value) { this.text = value; },
            getValue() { return this.value; }, setValue(value) { this.value = value; this.listeners?.change?.(this, value); },
            update(value) { this.html = value; },
            show() { dialogs.push(this); },
            destroy() {
                if (this.destroyed) return;
                this.listeners?.beforedestroy?.(this);
                for (const child of this.children) child.destroy();
                this.destroyed = true;
            },
        };
        result.children = [...(Array.isArray(config.items) ? config.items : []),
            ...(Array.isArray(config.tbar) ? config.tbar : []), ...(config.bbar || []), ...(config.buttons || [])]
            .filter((item) => item && typeof item === 'object').map((item) => component(item, result));
        return result;
    }
    const Ext = { Ajax: {
        request(options) { requests.push(options); return { options }; },
        abort(request) { request.options.aborted = true; request.options.callback(null, false, { status: 0, responseText: '' }); },
    }, create(type, config) { assert.equal(type, 'Ext.window.Window'); return component({ ...config, xtype: 'window' }); } };
    const window = { Proxmox: { UserName: 'root@pam', Utils: { authOK: () => true } }, clearTimeout() {} };
    context = vm.createContext({ Ext, window, requests, attached, console,
        alive: (item) => !!item && !item.destroyed, text: (value) => String(value),
        optionBusy: () => false, optionsError: () => 'fixed error', containerDetailError: () => 'fixed error',
        uuid: () => 'ceaa63ca-3693-4ef0-8cbf-4de83911a76b',
        closeOptionEditor(page) { if (page._pveKomodoOptionsEditor) page._pveKomodoOptionsEditor.destroy(); },
        guestOwner: (page) => page.up('[workspace]'),
        updateControls() {}, optionControls() {}, summaryControls() {},
        attachStackTerminal(tab, grant, csrf, user) {
            attached.push({ tab, grant, csrf, user });
            tab._pveKomodoSocket = { close() { this.closed = true; } };
            tab._pveKomodoTerminal = { dispose() { this.disposed = true; } };
        },
    });
    vm.runInContext(`let activeStackPage = null, activeDashboard = null, activeGuestOptions = null, activeGuestSummary = null;
        let stackWriteState = null; const stackPages = new Set(), guestItemId = 'pve-komodo-guest';
        ${functions}
        globalThis.api = { guestTerminalConfig, stackTabConfig, stackControls, stackPages };`, context);
    const config = context.api.guestTerminalConfig({ type: 'qemu', vmid: 119 });
    const owner = { workspace: { _pveKomodoLoggedOut: false }, ownerCt: null };
    const page = component(config, owner), tab = page.items[0] = page.children[0];
    function activate() { page.listeners.activate(page); }
    function respond(data, index = requests.length - 1, status = 200) {
        requests[index].callback(null, status === 200, { status, responseText: JSON.stringify(data) });
    }
    function ready() { activate(); respond(snapshot()); }
    const button = tab.down('#pve-komodo-stack-new-terminal');
    return { config, context, page, tab, requests, dialogs, attached, activate, respond, ready, button,
        leave: () => page.listeners.deactivate(page), component };
}
function snapshot(changes = {}) {
    return { server_id: 'b'.repeat(24), server_name: 'server', binding_revision: 3, can_open: true,
        options: { default: true, sh: true, bash: true, custom: true }, shells: ['default', 'sh', 'bash', 'custom'],
        csrf_token: 'guest-terminal-csrf', ...changes };
}
function pressNew(view) { view.button.handler(view.button); return view.dialogs.at(-1); }


test('guest terminal keeps the shared fit/fullscreen panel and loads the guest-bound endpoint', () => {
    const view = setup(), shared = view.context.api.stackTabConfig('terminal', '终端', 'fa-terminal');
    assert.equal(view.config.layout, 'fit'); assert.equal(view.config.pveKomodoServerTerminalPage, true);
    assert.equal(view.tab.layout, shared.layout); assert.equal(view.tab.cls, shared.cls);
    assert.equal(view.tab.bodyPadding, 0); assert.equal(view.tab.items[0].scrollable, false);
    assert.equal(view.tab.items[0].cls, 'pve-komodo-stack-terminal-output');
    assert.equal(view.tab.listeners.activate, undefined, 'nested activation cannot request stack details');
    view.activate(); assert.equal(view.requests.length, 1);
    assert.equal(view.requests[0].url, '/pve-plugins/api/komodo/guests/qemu/119/terminal-services');
    assert.equal(view.requests[0].disableCaching, false);
    assert.equal(view.button.disabled, true); view.respond(snapshot()); assert.equal(view.button.disabled, false);
});


test('server choices send only pinned binding and explicit modes and reject invalid custom commands', () => {
    for (const mode of ['default', 'sh', 'bash', 'custom']) {
        const view = setup(); view.ready(); const dialog = pressNew(view), field = dialog.down('#pve-komodo-terminal-shell');
        assert.equal(dialog.title, '新建服务器终端');
        assert.deepEqual(Array.from(field.store, (row) => row[0]), ['default', 'sh', 'bash', 'custom']);
        field.setValue(mode); const command = dialog.down('#pve-komodo-terminal-command');
        assert.equal(command.disabled, mode !== 'custom');
        if (mode === 'custom') {
            command.setValue('bad\ncommand'); dialog.down('#pve-komodo-terminal-create').handler();
            assert.equal(view.requests.filter((item) => item.method === 'POST').length, 0);
            command.setValue('/bin/sh -l');
        }
        dialog.down('#pve-komodo-terminal-create').handler();
        const post = view.requests.at(-1);
        assert.equal(post.url, '/pve-plugins/api/komodo/guests/qemu/119/terminals');
        assert.equal(post.headers['X-PVE-Plugins-CSRF'], 'guest-terminal-csrf');
        assert.deepEqual(JSON.parse(JSON.stringify(post.jsonData)), { server_id: 'b'.repeat(24), binding_revision: 3,
            shell: mode, ...(mode === 'custom' ? { command: '/bin/sh -l' } : {}), request_id: 'ceaa63ca-3693-4ef0-8cbf-4de83911a76b' });
        assert.ok(!('container_id' in post.jsonData) && !('service' in post.jsonData));
        view.respond({ detail: 'PRIVATE_ERROR' }, view.requests.length - 1, 502);
        assert.equal(view.requests.filter((item) => item.method === 'POST').length, 1, 'grant errors never trigger automatic retry');
    }
});


test('permission and binding failure leave New disabled, and stale reads cannot reactivate a left page', () => {
    const view = setup(); view.activate(); const old = view.requests[0]; view.leave();
    assert.equal(old.aborted, true); view.respond(snapshot(), 0);
    assert.equal(view.page._pveKomodoStackData, null); assert.equal(view.button.disabled, true);
    view.activate(); view.respond(snapshot({ can_open: false })); assert.equal(view.button.disabled, true);
    view.leave(); view.activate(); view.respond({ detail: 'guest_link_revision_conflict' }, view.requests.length - 1, 409);
    assert.equal(view.page._pveKomodoStackData, null); assert.equal(view.button.disabled, true);
});


test('leaving closes terminal, destroys the dialog, ignores a late grant and safely handles detached children', () => {
    const view = setup(); view.ready(); const dialog = pressNew(view);
    dialog.down('#pve-komodo-terminal-create').handler(); const pending = view.requests.length - 1;
    view.leave(); view.respond({ grant: 'late', session_id: '7042a2da-1fac-4356-b93f-1dd54d0ab237' }, pending);
    assert.equal(view.attached.length, 0); assert.equal(view.context.api.stackPages.size, 0);
    view.activate(); view.respond(snapshot()); const second = pressNew(view);
    second.down('#pve-komodo-terminal-create').handler();
    view.respond({ grant: 'fresh', session_id: '7042a2da-1fac-4356-b93f-1dd54d0ab237' });
    assert.equal(view.attached.length, 1); const terminal = view.tab._pveKomodoTerminal, socket = view.tab._pveKomodoSocket;
    const open = pressNew(view); assert.equal(open.destroyed, false);
    view.leave(); assert.equal(open.destroyed, true); assert.equal(terminal.disposed, true); assert.equal(socket.closed, true);
    view.tab.ownerCt = null; assert.doesNotThrow(() => view.tab.destroy());
});


test('server terminal waits for fullscreen exit and discards a pending dialog after leaving', async () => {
    const view = setup(); view.ready(); const target = {}; let exit, count = 0;
    view.tab.getEl = () => ({ dom: target });
    view.context.window.document = { fullscreenElement: target, exitFullscreen() {
        count++; return new Promise((resolve) => { exit = () => { this.fullscreenElement = null; resolve(); }; });
    } };
    pressNew(view); pressNew(view); assert.equal(count, 1); assert.equal(view.dialogs.length, 0);
    view.leave(); exit(); await Promise.resolve(); await Promise.resolve();
    assert.equal(view.dialogs.length, 0); assert.equal(view.requests.filter((item) => item.method === 'POST').length, 0);
});
