// Dashboard view — a live board of all running SOP tasks at a glance.
//
// Multiple tasks now run on their own dedicated pi subprocesses (backend
// process pool), so several SOPs can execute in parallel. This board shows
// each active task's progress bar, current node and per-node status dots,
// updating live from the same WebSocket event stream the detail page uses.
import { store } from '../store.js';
import { ws } from '../ws-client.js';
import { api } from '../api-client.js';

// Statuses we consider "on the board". Completed/failed are shown too (so a
// run's final state is visible) but sorted after the active ones.
const ACTIVE = new Set(['running', 'waiting_human', 'paused', 'pending', 'retrying']);

// Per-task progress state, keyed by task_id.
// { total, done, currentNode, status, nodes: [{id,name,status}] }
const progress = new Map();
let unsubscribers = [];

export async function render(main, params, { store: s, ws: w, api: a }) {
    // Clean up any prior subscriptions (view is re-entrant on hashchange).
    teardown();

    let tasks = s.get('tasks') || [];
    if (tasks.length === 0) {
        try {
            tasks = await api.listTasks();
            store.set('tasks', tasks);
        } catch (e) {
            console.warn('Dashboard: could not load tasks:', e);
        }
    }

    main.innerHTML = `
        <div class="page-header">
            <div>
                <h2>📊 Run Dashboard</h2>
                <p class="form-help">所有运行中的任务在各自独立的 pi 进程里并行执行，进度实时更新。</p>
            </div>
            <button class="btn btn-ghost" id="dash-refresh">↻ Refresh</button>
        </div>
        <div class="dashboard-grid" id="dashboard-grid"></div>
    `;

    main.querySelector('#dash-refresh')?.addEventListener('click', () => {
        location.reload();
    });

    const grid = main.querySelector('#dashboard-grid');

    const boardTasks = sortForBoard(tasks);
    if (boardTasks.length === 0) {
        grid.innerHTML = '<div class="empty-state">No tasks yet. Create a task from the Tasks page.</div>';
    } else {
        grid.innerHTML = boardTasks.map(t => renderCard(t)).join('');
    }

    // Initialize + subscribe each task for live events. Subscribing the active
    // ones is safe: the WS manager only filters when a connection has a
    // non-empty subscription set, and we add every board task here.
    for (const t of boardTasks) {
        initTaskProgress(t);
        w.subscribeTask(t.task_id);
        const unsub = s.subscribe(`events.${t.task_id}`, (events) => {
            if (!events || events.length === 0) return;
            applyEvent(t.task_id, events[events.length - 1]);
            paintCard(grid, t.task_id);
        });
        unsubscribers.push(unsub);
        // Load history so a task opened mid-run shows accurate progress.
        hydrateFromHistory(t.task_id, grid);
    }

    // React to task list changes (new task created / status changed).
    const unsubTasks = s.subscribe('tasks', () => {
        const updated = sortForBoard(store.get('tasks') || []);
        // Add cards for tasks not yet on the board.
        for (const t of updated) {
            if (!progress.has(t.task_id)) {
                initTaskProgress(t);
                w.subscribeTask(t.task_id);
                const unsub = s.subscribe(`events.${t.task_id}`, (events) => {
                    if (!events || events.length === 0) return;
                    applyEvent(t.task_id, events[events.length - 1]);
                    paintCard(grid, t.task_id);
                });
                unsubscribers.push(unsub);
                if (grid.querySelector('.empty-state')) grid.innerHTML = '';
                grid.insertAdjacentHTML('afterbegin', renderCard(t));
                hydrateFromHistory(t.task_id, grid);
            } else {
                // Update status badge if it changed.
                const st = progress.get(t.task_id);
                if (st && t.status && t.status !== st.status) {
                    st.status = t.status;
                    paintCard(grid, t.task_id);
                }
            }
        }
    });
    unsubscribers.push(unsubTasks);
}

function sortForBoard(tasks) {
    const rank = (t) => (ACTIVE.has(t.status) ? 0 : 1);
    return [...tasks].sort((a, b) => {
        const r = rank(a) - rank(b);
        if (r !== 0) return r;
        return (b.updated_at || b.created_at || 0) - (a.updated_at || a.created_at || 0);
    });
}

function initTaskProgress(task) {
    if (progress.has(task.task_id)) return;
    progress.set(task.task_id, {
        total: 0,
        done: 0,
        currentNode: '',
        status: task.status || 'pending',
        nodes: [],
    });
}

async function hydrateFromHistory(taskId, grid) {
    try {
        const [full, events] = await Promise.all([
            api.getTask(taskId).catch(() => null),
            api.getTaskEvents(taskId).catch(() => []),
        ]);
        const st = progress.get(taskId);
        if (!st) return;
        if (full && Array.isArray(full.nodes) && full.nodes.length) {
            st.nodes = full.nodes.map(n => ({ id: n.id, name: n.name || n.id, status: 'pending' }));
            st.total = st.nodes.length;
        }
        (events || []).forEach(evt => applyEvent(taskId, evt, /*silent*/ true));
        paintCard(grid, taskId);
    } catch (e) {
        console.debug('Dashboard hydrate failed:', e);
    }
}

function applyEvent(taskId, evt) {
    const st = progress.get(taskId);
    if (!st) return;
    const type = evt.event_type;
    const nodeId = evt.node_id;
    const data = parseData(evt.data);

    const setNode = (id, status) => {
        const n = st.nodes.find(x => x.id === id);
        if (n) n.status = status;
    };

    switch (type) {
        case 'node_started':
        case 'node_retry':
            setNode(nodeId, 'running');
            st.currentNode = data.node_name || nodeId || st.currentNode;
            break;
        case 'node_completed':
            setNode(nodeId, 'completed');
            break;
        case 'node_failed':
            setNode(nodeId, 'failed');
            break;
        case 'node_skipped':
            setNode(nodeId, 'skipped');
            break;
        case 'task_completed':
            st.status = 'completed';
            st.currentNode = '';
            break;
        case 'task_failed':
            st.status = 'failed';
            break;
        case 'task_cancelled':
            st.status = 'cancelled';
            break;
        case 'task_paused':
            st.status = 'paused';
            break;
        case 'task_started':
            if (st.status === 'pending') st.status = 'running';
            break;
    }

    // Recompute done count from node states (robust to out-of-order events).
    st.done = st.nodes.filter(n => n.status === 'completed').length;
    if (st.total === 0 && st.nodes.length) st.total = st.nodes.length;
}

function renderCard(task) {
    const st = progress.get(task.task_id) || { total: 0, done: 0, currentNode: '', status: task.status, nodes: [] };
    const prompt = task.metadata?.prompt || task.metadata?.input || '';
    const title = prompt ? compact(prompt, 80) : (task.sop_name || 'Task');
    return `
        <div class="dash-card status-${escapeAttr(st.status)}" data-task-id="${escapeAttr(task.task_id)}">
            ${cardInner(task, st, title)}
        </div>
    `;
}

function cardInner(task, st, title) {
    const pct = st.total > 0 ? Math.round((st.done / st.total) * 100) : (st.status === 'completed' ? 100 : 0);
    const barClass = st.status === 'failed' ? 'failed' : (st.status === 'completed' ? 'completed' : 'running');
    return `
        <div class="dash-card-head">
            <span class="badge badge-${escapeAttr(st.status)}">${escapeHtml(st.status)}</span>
            <span class="dash-title">${escapeHtml(title)}</span>
        </div>
        <div class="dash-progress">
            <div class="dash-progress-bar ${barClass}" style="width:${pct}%"></div>
        </div>
        <div class="dash-meta">
            <span class="dash-node">${st.currentNode ? '▶ ' + escapeHtml(st.currentNode) : (st.status === 'completed' ? '✓ done' : '—')}</span>
            <span class="dash-count">${st.done}/${st.total || '?'} nodes</span>
        </div>
        <div class="dash-dots">
            ${st.nodes.map(n => `<span class="dash-dot ${escapeAttr(n.status)}" title="${escapeAttr(n.name)}"></span>`).join('')}
        </div>
        <div class="dash-foot">
            <span class="task-id">${escapeHtml(task.task_id)}</span>
            <a class="dash-open" href="#/tasks/${escapeAttr(task.task_id)}">Open →</a>
        </div>
    `;
}

function paintCard(grid, taskId) {
    const card = grid.querySelector(`.dash-card[data-task-id="${cssEscape(taskId)}"]`);
    if (!card) return;
    const st = progress.get(taskId);
    if (!st) return;
    const task = store.getTask(taskId) || { task_id: taskId };
    const prompt = task.metadata?.prompt || task.metadata?.input || '';
    const title = prompt ? compact(prompt, 80) : (task.sop_name || 'Task');
    card.className = `dash-card status-${st.status}`;
    card.innerHTML = cardInner(task, st, title);
}

function teardown() {
    unsubscribers.forEach(fn => { try { fn(); } catch (_) {} });
    unsubscribers = [];
    progress.clear();
}

function parseData(raw) {
    if (!raw) return {};
    if (typeof raw !== 'string') return raw;
    try { return JSON.parse(raw); } catch (_) { return { text: raw }; }
}

function compact(value, max = 80) {
    const text = String(value || '').replace(/\s+/g, ' ').trim();
    return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str == null ? '' : String(str);
    return div.innerHTML;
}

function escapeAttr(str) {
    return escapeHtml(str).replace(/"/g, '&quot;');
}

function cssEscape(str) {
    if (window.CSS && typeof window.CSS.escape === 'function') return window.CSS.escape(str);
    return String(str).replace(/[^a-zA-Z0-9_-]/g, '\\$&');
}
