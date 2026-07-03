// Log viewer view — browse historical events
import { store } from '../store.js';
import { api } from '../api-client.js';

export async function render(main, params, { store: s, ws: w, api: a }) {
    const taskId = params.taskId || '';
    const tasks = s.get('tasks') || [];

    main.innerHTML = `
        <div class="page-header">
            <h2>📜 Logs</h2>
        </div>

        <div class="log-filters">
            <div class="form-group" style="margin-bottom:0">
                <label>Task</label>
                <select id="log-task-filter" class="form-select" style="width:200px">
                    <option value="">All tasks</option>
                    ${tasks.map(t => `<option value="${t.task_id}" ${t.task_id === taskId ? 'selected' : ''}>${t.sop_name} (${t.task_id.slice(0, 8)})</option>`).join('')}
                </select>
            </div>
            <div class="form-group" style="margin-bottom:0">
                <label>Event Type</label>
                <select id="log-type-filter" class="form-select" style="width:200px">
                    <option value="">All types</option>
                    <option value="task_started">task_started</option>
                    <option value="task_completed">task_completed</option>
                    <option value="task_failed">task_failed</option>
                    <option value="node_started">node_started</option>
                    <option value="node_completed">node_completed</option>
                    <option value="node_failed">node_failed</option>
                    <option value="node_retry">node_retry</option>
                    <option value="tool_call_start">tool_call_start</option>
                    <option value="human_intervention_required">human_intervention</option>
                    <option value="error">error</option>
                </select>
            </div>
            <button class="btn btn-primary" id="btn-search-logs">Search</button>
            <button class="btn btn-ghost" id="btn-refresh-logs">↻ Refresh</button>
        </div>

        <div id="log-results">
            <table class="log-table">
                <thead>
                    <tr>
                        <th class="seq">#</th>
                        <th class="timestamp">Time</th>
                        <th>Task ID</th>
                        <th class="event-type-col">Event Type</th>
                        <th>Node</th>
                        <th>Data</th>
                    </tr>
                </thead>
                <tbody id="log-table-body">
                    <tr><td colspan="6" class="empty-state">Use filters and click Search</td></tr>
                </tbody>
            </table>
        </div>

        <div id="log-stats" style="margin-top:24px"></div>
    `;

    // Search button
    main.querySelector('#btn-search-logs')?.addEventListener('click', () => searchLogs(main));
    main.querySelector('#btn-refresh-logs')?.addEventListener('click', () => searchLogs(main));

    // Initial search if taskId provided
    if (taskId) {
        searchLogs(main);
    }

    // Load stats
    loadStats(main);
}

async function searchLogs(main) {
    const taskId = main.querySelector('#log-task-filter')?.value || '';
    const eventType = main.querySelector('#log-type-filter')?.value || '';

    try {
        const results = await api.searchLogs({
            task_id: taskId || undefined,
            event_type: eventType || undefined,
            limit: 200,
        });

        const tbody = main.querySelector('#log-table-body');
        if (!tbody) return;

        if (results.length === 0) {
            tbody.innerHTML = '<tr><td colspan="6" class="empty-state">No events found</td></tr>';
            return;
        }

        tbody.innerHTML = results.map((evt, i) => {
            const time = evt.timestamp ? new Date(evt.timestamp * 1000).toISOString() : '';
            const dataObj = evt.data;
            const isObj = dataObj && typeof dataObj === 'object';
            const pretty = isObj ? JSON.stringify(dataObj, null, 2) : String(dataObj ?? '');
            const preview = pretty.replace(/\s+/g, ' ').trim();
            const hasDetail = preview.length > 0;

            return `
                <tr class="log-row ${hasDetail ? 'log-row-expandable' : ''}" data-log-index="${i}">
                    <td class="seq">${evt.seq || '-'}</td>
                    <td class="timestamp">${time}</td>
                    <td><code>${(evt.task_id || '').slice(0, 12)}</code></td>
                    <td>${escapeHtml(evt.event_type)}</td>
                    <td>${escapeHtml(evt.node_id || '-')}</td>
                    <td class="log-data-cell">
                        ${hasDetail ? `<span class="log-toggle">▶</span>` : ''}
                        <span class="log-data-preview">${escapeHtml(preview).slice(0, 100)}${preview.length > 100 ? '…' : ''}</span>
                    </td>
                </tr>
                ${hasDetail ? `
                <tr class="log-detail-row hidden" data-detail-index="${i}">
                    <td colspan="6"><pre class="log-detail-pre">${escapeHtml(pretty)}</pre></td>
                </tr>` : ''}
            `;
        }).join('');

        // Wire expand/collapse — click a row to reveal full formatted detail.
        tbody.querySelectorAll('.log-row-expandable').forEach(row => {
            row.addEventListener('click', () => {
                const idx = row.dataset.logIndex;
                const detail = tbody.querySelector(`.log-detail-row[data-detail-index="${idx}"]`);
                const toggle = row.querySelector('.log-toggle');
                if (detail) {
                    const nowHidden = detail.classList.toggle('hidden');
                    if (toggle) toggle.textContent = nowHidden ? '▶' : '▼';
                }
            });
        });
    } catch (e) {
        console.error('Log search failed:', e);
    }
}

async function loadStats(main) {
    try {
        const stats = await api.getLogStats();
        const container = main.querySelector('#log-stats');
        if (!container) return;

        container.innerHTML = `
            <div class="settings-section">
                <h3>Statistics</h3>
                <div class="settings-grid">
                    <div><strong>Total Tasks:</strong> ${stats.total_tasks || 0}</div>
                    <div><strong>Total Events:</strong> ${stats.total_events || 0}</div>
                </div>
            </div>
        `;
    } catch (e) {
        console.debug('Stats load failed:', e);
    }
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}
