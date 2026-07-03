// Settings view — edit user configuration
import { store } from '../store.js';
import { api } from '../api-client.js';

export async function render(main, params, { store: s, ws: w, api: a }) {
    let config = s.get('config') || {};
    if (!config.pi_agent) {
        try {
            config = await api.getConfig();
            s.set('config', config);
        } catch (e) {
            console.warn('Failed to load config:', e);
            config = { pi_agent: {}, web_ui: {}, tui: {} };
        }
    }

    const pi = config.pi_agent || {};
    const web = config.web_ui || {};
    const tui = config.tui || {};

    // Load models for selector
    let models = [];
    try {
        const result = await api.getPiModels();
        models = result.models || [];
    } catch (e) {
        console.debug('Could not load models:', e);
    }

    const modelOptions = models.length > 0
        ? models.map(m => `<option value="${m.id}" ${pi.default_model === m.id ? 'selected' : ''}>${m.provider}/${m.id}</option>`).join('')
        : '<option value="">No models available (pi not connected)</option>';

    main.innerHTML = `
        <div class="page-header">
            <h2>⚙️ Settings</h2>
            <div>
                <button class="btn btn-primary" id="btn-save-settings">💾 Save</button>
                <button class="btn btn-ghost" id="btn-reset-settings">↺ Reset Defaults</button>
            </div>
        </div>

        <div class="settings-section">
            <h3>Pi Agent</h3>
            <div class="settings-grid">
                <div class="form-group">
                    <label>Binary Path</label>
                    <input type="text" class="form-input" id="pi-binary" value="${pi.binary_path || 'pi'}">
                </div>
                <div class="form-group">
                    <label>Default Model</label>
                    <select class="form-select" id="pi-model">
                        <option value="">Auto-detect</option>
                        ${modelOptions}
                    </select>
                </div>
                <div class="form-group">
                    <label>Startup Timeout (seconds)</label>
                    <input type="number" class="form-input" id="pi-startup-timeout" value="${pi.startup_timeout || 30}" min="5">
                </div>
                <div class="form-group">
                    <label>Request Timeout (seconds)</label>
                    <input type="number" class="form-input" id="pi-request-timeout" value="${pi.request_timeout || 120}" min="10">
                </div>
                <div class="form-group">
                    <label>Thinking Level</label>
                    <select class="form-select" id="pi-thinking-level">
                        <option value="off" ${pi.thinking_level === 'off' ? 'selected' : ''}>Off</option>
                        <option value="minimal" ${pi.thinking_level === 'minimal' ? 'selected' : ''}>Minimal</option>
                        <option value="low" ${pi.thinking_level === 'low' ? 'selected' : ''}>Low</option>
                        <option value="medium" ${pi.thinking_level === 'medium' ? 'selected' : ''}>Medium</option>
                        <option value="high" ${pi.thinking_level === 'high' ? 'selected' : ''}>High</option>
                    </select>
                </div>
                <div class="form-group form-checkbox">
                    <input type="checkbox" id="pi-auto-compaction" ${pi.auto_compaction !== false ? 'checked' : ''}>
                    <label>Auto Compaction</label>
                </div>
            </div>
        </div>

        <div class="settings-section">
            <h3>Web UI</h3>
            <div class="settings-grid">
                <div class="form-group">
                    <label>Host</label>
                    <input type="text" class="form-input" id="web-host" value="${web.host || '0.0.0.0'}">
                </div>
                <div class="form-group">
                    <label>Port</label>
                    <input type="number" class="form-input" id="web-port" value="${web.port || 8080}">
                </div>
                <div class="form-group">
                    <label>Theme</label>
                    <select class="form-select" id="web-theme">
                        <option value="dark" ${web.theme === 'dark' ? 'selected' : ''}>Dark</option>
                        <option value="light" ${web.theme === 'light' ? 'selected' : ''}>Light</option>
                    </select>
                </div>
                <div class="form-group form-checkbox">
                    <input type="checkbox" id="web-auto-scroll" ${web.auto_scroll !== false ? 'checked' : ''}>
                    <label>Auto-scroll Agent Output</label>
                </div>
                <div class="form-group">
                    <label>Max Log Entries</label>
                    <input type="number" class="form-input" id="web-max-logs" value="${web.max_log_entries || 1000}" min="100" max="100000">
                </div>
            </div>
        </div>

        <div class="settings-section">
            <h3>TUI</h3>
            <div class="settings-grid">
                <div class="form-group">
                    <label>Theme</label>
                    <select class="form-select" id="tui-theme">
                        <option value="textual-dark" ${tui.theme === 'textual-dark' ? 'selected' : ''}>Dark</option>
                        <option value="textual-light" ${tui.theme === 'textual-light' ? 'selected' : ''}>Light</option>
                    </select>
                </div>
                <div class="form-group form-checkbox">
                    <input type="checkbox" id="tui-compact" ${tui.compact_view ? 'checked' : ''}>
                    <label>Compact View</label>
                </div>
            </div>
        </div>
    `;

    // Theme live preview
    main.querySelector('#web-theme')?.addEventListener('change', (e) => {
        document.documentElement.setAttribute('data-theme', e.target.value);
    });

    // Save
    main.querySelector('#btn-save-settings')?.addEventListener('click', async () => {
        const partial = {
            pi_agent: {
                binary_path: main.querySelector('#pi-binary')?.value || 'pi',
                default_model: main.querySelector('#pi-model')?.value || null,
                startup_timeout: parseFloat(main.querySelector('#pi-startup-timeout')?.value) || 30,
                request_timeout: parseFloat(main.querySelector('#pi-request-timeout')?.value) || 120,
                thinking_level: main.querySelector('#pi-thinking-level')?.value || 'medium',
                auto_compaction: main.querySelector('#pi-auto-compaction')?.checked !== false,
            },
            web_ui: {
                host: main.querySelector('#web-host')?.value || '0.0.0.0',
                port: parseInt(main.querySelector('#web-port')?.value) || 8080,
                theme: main.querySelector('#web-theme')?.value || 'dark',
                auto_scroll: main.querySelector('#web-auto-scroll')?.checked !== false,
                max_log_entries: parseInt(main.querySelector('#web-max-logs')?.value) || 1000,
            },
            tui: {
                theme: main.querySelector('#tui-theme')?.value || 'textual-dark',
                compact_view: main.querySelector('#tui-compact')?.checked || false,
            },
        };

        try {
            await api.updateConfig(partial);
            s.set('config', { ...s.get('config'), ...partial });
            alert('Settings saved!');
        } catch (e) {
            alert(`Failed to save: ${e.message}`);
        }
    });

    // Reset
    main.querySelector('#btn-reset-settings')?.addEventListener('click', async () => {
        if (!confirm('Reset all settings to defaults?')) return;
        await api.resetConfig();
        const config = await api.getConfig();
        s.set('config', config);
        location.reload();
    });
}
