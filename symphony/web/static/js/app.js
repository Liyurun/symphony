// Main app entry point
import { store } from './store.js';
import { router } from './router.js';
import { ws } from './ws-client.js';
import { api } from './api-client.js';

// Initialize
async function init() {
    // Load initial data
    try {
        const [tasks, sops, config] = await Promise.all([
            api.listTasks().catch(() => []),
            api.listSOPs().catch(() => []),
            api.getConfig().catch(() => ({})),
        ]);
        store.set('tasks', tasks);
        store.set('sopTemplates', sops);
        store.set('config', config);
        renderSidebarTasks();
    } catch (e) {
        console.warn('Failed to load initial data:', e);
    }

    // Connect WebSocket
    ws.connect();

    // Start router
    router.start();

    // Attach global event handlers
    setupGlobalHandlers();

    // Keep the sidebar task list clickable and in sync with live updates.
    store.subscribe('tasks', renderSidebarTasks);
    store.subscribe('activeTaskId', renderSidebarTasks);
}

function setupGlobalHandlers() {
    // New task button
    const btnNew = document.getElementById('btn-new-task');
    if (btnNew) {
        btnNew.addEventListener('click', showNewTaskModal);
    }

    // Modal close buttons
    document.querySelectorAll('.modal-close, .modal-cancel').forEach(btn => {
        btn.addEventListener('click', () => {
            btn.closest('.modal').classList.add('hidden');
        });
    });

    // Create task button
    const btnCreate = document.getElementById('btn-create-task');
    if (btnCreate) {
        btnCreate.addEventListener('click', createTaskFromModal);
    }

    // Human approval buttons
    const btnApprove = document.getElementById('btn-approve');
    const btnReject = document.getElementById('btn-reject');
    if (btnApprove) {
        btnApprove.addEventListener('click', () => handleApproval(true));
    }
    if (btnReject) {
        btnReject.addEventListener('click', () => handleApproval(false));
    }
}

function renderSidebarTasks() {
    const container = document.getElementById('task-list');
    if (!container) return;

    const tasks = store.get('tasks') || [];
    const activeTaskId = store.get('activeTaskId');
    if (tasks.length === 0) {
        container.innerHTML = '<div class="empty-state">No tasks yet</div>';
        return;
    }

    container.innerHTML = tasks.slice(0, 20).map(task => {
        const status = task.status || 'pending';
        const statusIcon = {
            pending: '○', running: '▶', completed: '✓', failed: '✗',
            cancelled: '⊘', paused: '⏸', waiting_human: '⏸',
        }[status] || '•';
        const statusClass = status === 'waiting_human' ? 'waiting_human' : status;
        const prompt = task.metadata?.prompt || task.metadata?.input || '';
        const title = prompt ? compactText(prompt, 46) : (task.sop_name || 'Task');
        const active = task.task_id === activeTaskId ? ' active' : '';
        return `
            <div class="task-item${active}" data-task-id="${escapeHtml(task.task_id)}" title="Open task details">
                <span class="task-status-icon badge-${statusClass}">${statusIcon}</span>
                <div class="task-info">
                    <div class="task-sop-name">${escapeHtml(title)}</div>
                    <div class="task-id">${escapeHtml(task.task_id)}</div>
                    <div class="task-time">${escapeHtml(formatTime(task.updated_at || task.created_at))}</div>
                </div>
            </div>
        `;
    }).join('');

    container.querySelectorAll('.task-item').forEach(item => {
        item.addEventListener('click', () => {
            location.hash = `#/tasks/${item.dataset.taskId}`;
        });
    });
}

function compactText(value, max = 80) {
    const text = String(value || '').replace(/\s+/g, ' ').trim();
    return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

function formatTime(ts) {
    if (!ts) return '';
    return new Date(ts * 1000).toLocaleTimeString();
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str == null ? '' : String(str);
    return div.innerHTML;
}

// New task modal
async function showNewTaskModal() {
    const modal = document.getElementById('modal-new-task');
    const select = document.getElementById('new-task-sop');
    const prompt = document.getElementById('new-task-prompt');

    // Reset the prompt each time the modal opens.
    if (prompt) prompt.value = '';

    // Populate SOP options (SOP is optional — the first option is "ad-hoc").
    const sops = store.get('sopTemplates') || [];
    select.innerHTML = '<option value="">— None (ad-hoc question) —</option>';
    sops.forEach(sop => {
        const opt = document.createElement('option');
        opt.value = sop.name;
        opt.textContent = `${sop.name} (${sop.node_count} nodes)`;
        select.appendChild(opt);
    });

    modal.classList.remove('hidden');
    if (prompt) prompt.focus();
}

async function createTaskFromModal() {
    const sopName = document.getElementById('new-task-sop').value;
    const prompt = (document.getElementById('new-task-prompt').value || '').trim();
    const version = document.getElementById('new-task-version').value || '1.0';

    // Require at least one of: a SOP template OR a question.
    if (!sopName && !prompt) {
        alert('Enter a question or pick a SOP template.');
        return;
    }

    try {
        // Ad-hoc question (no SOP) OR SOP task seeded with the prompt — both
        // auto-start so the user immediately sees execution.
        const result = await api.createTask(sopName, {
            sopVersion: version,
            prompt,
            autoStart: true,
        });
        document.getElementById('modal-new-task').classList.add('hidden');

        // Refresh and navigate
        const tasks = await api.listTasks();
        store.set('tasks', tasks);
        location.hash = `#/tasks/${result.task_id}`;
    } catch (e) {
        alert(`Failed to create task: ${e.message}`);
    }
}

// Human approval
let approvalData = null;
window.showApprovalModal = function(taskId, nodeId, nodeName, resultPreview) {
    approvalData = { taskId, nodeId };
    document.getElementById('approval-node-name').textContent = nodeName || 'Unknown node';
    const resultEl = document.getElementById('approval-result');
    let preview = resultPreview;
    if (typeof preview === 'string') {
        try { preview = JSON.parse(preview); } catch (_) { preview = { output: preview }; }
    }
    resultEl.innerHTML = renderApprovalPreview(preview || {});
    document.getElementById('approval-feedback').value = '';
    document.getElementById('modal-human-approval').classList.remove('hidden');
};

// Render a review preview: show the structured artifact prominently (clickable
// Feishu/link, SQL code block), plus the output text, instead of raw JSON.
function renderApprovalPreview(preview) {
    const parts = [];
    const art = preview.artifact;
    if (art && art.value) {
        const type = art.type || 'text';
        if (type === 'feishu_doc' || type === 'link') {
            parts.push(`<div class="approval-artifact"><strong>产物（${escapeHtml(type)}）：</strong> <a href="${escapeAttr(art.value)}" target="_blank" rel="noopener">${escapeHtml(art.value)}</a></div>`);
        } else if (type === 'sql') {
            parts.push(`<div class="approval-artifact"><strong>产物（SQL）：</strong><pre class="artifact-sql"><code>${escapeHtml(art.value)}</code></pre></div>`);
        } else if (type === 'task_id') {
            parts.push(`<div class="approval-artifact"><strong>发布任务ID：</strong> <code>${escapeHtml(art.value)}</code></div>`);
        } else {
            parts.push(`<div class="approval-artifact"><strong>产物：</strong> ${escapeHtml(art.value)}</div>`);
        }
    }
    const output = typeof preview.output === 'string' ? preview.output : '';
    if (output) {
        parts.push(`<div class="approval-output">${escapeHtml(output)}</div>`);
    }
    if (parts.length === 0) {
        parts.push(`<pre>${escapeHtml(JSON.stringify(preview, null, 2))}</pre>`);
    }
    return parts.join('');
}

function escapeAttr(str) {
    return escapeHtml(str).replace(/"/g, '&quot;');
}

async function handleApproval(approved) {
    if (!approvalData) return;
    const feedback = document.getElementById('approval-feedback').value;
    await api.humanRespond(approvalData.taskId, approvalData.nodeId, approved, feedback);
    document.getElementById('modal-human-approval').classList.add('hidden');
    approvalData = null;
}

// Start
init();
