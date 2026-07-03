// Task list view (sidebar)
import { store } from '../store.js';
import { api } from '../api-client.js';

export async function render(main, params, { store: s, ws: w, api: a }) {
    const tasks = s.get('tasks') || [];
    let sops = s.get('sopTemplates') || [];
    if (sops.length === 0) {
        try {
            sops = await api.listSOPs();
            store.set('sopTemplates', sops);
        } catch (e) {
            console.warn('Could not load SOP templates:', e);
        }
    }

    main.innerHTML = `
        <div class="task-detail">
            <div class="page-header">
                <h2>📋 Tasks</h2>
                <div>
                    <button class="btn btn-secondary" id="btn-toggle-sop-task">+ New from SOP</button>
                </div>
            </div>

            <!-- 方案A · Claude-style ask bar: type a question and press Enter to
                 create + run an ad-hoc task, no SOP required. -->
            <div class="ask-bar">
                <input type="text" class="form-input ask-input" id="ask-input"
                       placeholder="Ask anything — type a question and press Enter to start a task…"
                       autocomplete="off" autofocus>
                <button class="btn btn-primary" id="ask-send">Ask ➤</button>
            </div>

            <div class="sop-task-launcher hidden" id="sop-task-launcher">
                <div class="run-sop-header">
                    <div>
                        <strong>Run a complete SOP task</strong>
                        <div class="form-help">选择 SOP 模板，填写本次任务输入，提交后会创建并自动运行整个 SOP 节点流。</div>
                    </div>
                    <button class="btn btn-primary" id="btn-create-sop-task">▶ Create & Run</button>
                </div>
                <div class="sop-task-grid">
                    <div class="form-group">
                        <label>SOP Template</label>
                        <select class="form-select" id="sop-task-select">
                            <option value="">Select a SOP...</option>
                            ${sops.map(sp => `<option value="${escapeAttr(sp.name)}">${escapeHtml(sp.name)} · v${escapeHtml(sp.version || '1.0')}</option>`).join('')}
                        </select>
                    </div>
                    <div class="form-group sop-task-requirements" id="sop-task-requirements">
                        <label>Required Input</label>
                        <div class="form-help">选择 SOP 后会显示输入要求。</div>
                    </div>
                </div>
                <div class="form-group">
                    <label>Task Input</label>
                    <textarea class="form-textarea" id="sop-task-input" rows="5" placeholder="按所选 SOP 的 Required Input 填写本次完整任务输入。"></textarea>
                </div>
            </div>

            <div class="task-list-full">
                ${tasks.length === 0 ? '<div class="empty-state">No tasks yet. Type a question above to get started.</div>' : ''}
                <div class="task-cards">
                    ${tasks.map(t => renderTaskCard(t)).join('')}
                </div>
            </div>
        </div>
    `;

    // ── Wire the ask bar (方案A ad-hoc task) ──────────────
    const askInput = main.querySelector('#ask-input');
    const askSend = main.querySelector('#ask-send');
    const submitAsk = () => {
        const text = (askInput.value || '').trim();
        if (!text) return;
        w.askQuestion(text);           // create + auto-start; ws-client navigates in.
        askInput.value = '';
    };
    if (askInput) {
        askInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') { e.preventDefault(); submitAsk(); }
        });
        // Focus so the user can just start typing (Claude-style).
        setTimeout(() => askInput.focus(), 0);
    }
    if (askSend) askSend.addEventListener('click', submitAsk);

    // ── Wire SOP-backed complete task launcher ──────────────
    const launcher = main.querySelector('#sop-task-launcher');
    const toggleSopTask = main.querySelector('#btn-toggle-sop-task');
    const sopSelect = main.querySelector('#sop-task-select');
    const sopInput = main.querySelector('#sop-task-input');
    const sopReq = main.querySelector('#sop-task-requirements');
    const createSopTask = main.querySelector('#btn-create-sop-task');

    toggleSopTask?.addEventListener('click', () => {
        launcher?.classList.toggle('hidden');
        if (!launcher?.classList.contains('hidden')) sopSelect?.focus();
    });
    sopSelect?.addEventListener('change', () => {
        const selected = sops.find(sp => sp.name === sopSelect.value);
        if (sopReq) {
            sopReq.innerHTML = `<label>Required Input</label><div class="form-help">${escapeHtml(selected?.input_requirements || '该 SOP 未声明额外输入要求。')}</div>`;
        }
    });
    createSopTask?.addEventListener('click', async () => {
        const sopName = sopSelect?.value || '';
        const prompt = sopInput?.value?.trim() || '';
        if (!sopName) { alert('请先选择 SOP 模板'); return; }
        if (!prompt) { alert('请填写 Task Input'); return; }
        createSopTask.disabled = true;
        try {
            const selected = sops.find(sp => sp.name === sopName);
            const result = await api.createTask(sopName, {
                sopVersion: selected?.version || '1.0',
                prompt,
                autoStart: true,
            });
            const updated = await api.listTasks();
            store.set('tasks', updated);
            location.hash = `#/tasks/${result.task_id}`;
        } catch (e) {
            alert(`Create SOP task failed: ${e.message}`);
            createSopTask.disabled = false;
        }
    });

    // Attach click handlers
    wireTaskCards(main);

    // Listen for task updates
    s.subscribe('tasks', () => {
        const updated = s.get('tasks') || [];
        const container = main.querySelector('.task-cards');
        if (container) {
            container.innerHTML = updated.length === 0
                ? '<div class="empty-state">No tasks yet.</div>'
                : updated.map(t => renderTaskCard(t)).join('');

            wireTaskCards(container);
        }
    });
}

function renderTaskCard(task) {
    const statusIcon = {
        pending: '○', running: '▶', completed: '✓', failed: '✗',
        cancelled: '⊘', paused: '⏸', waiting_human: '⏸',
    }[task.status] || '?';

    const statusClass = task.status === 'waiting_human' ? 'waiting_human' : task.status;

    const prompt = task.metadata?.prompt || task.metadata?.input || '';
    const title = prompt ? compactText(prompt, 110) : (task.sop_name || 'Task');
    const meta = [
        task.sop_name ? `SOP: ${task.sop_name}` : 'Ad-hoc task',
        task.claimed_by ? `Claimed by ${task.claimed_by}` : '',
    ].filter(Boolean).join(' · ');

    return `
        <div class="task-card-full sop-card" data-task-id="${escapeHtml(task.task_id)}" role="button" tabindex="0" title="Open task details">
            <div class="sop-name">
                <span>${statusIcon}</span>
                <span class="badge badge-${statusClass}">${task.status}</span>
                ${escapeHtml(title)}
            </div>
            <div class="sop-desc">
                <span class="task-id">${escapeHtml(task.task_id)}</span>
                ${task.claimed_by ? `<span class="claimed-badge">🔒 ${task.claimed_by}</span>` : ''}
            </div>
            <div class="sop-meta">
                <span>${escapeHtml(meta)}</span>
                <span>${formatTime(task.created_at)}</span>
                <span class="task-open-hint">Open details →</span>
            </div>
        </div>
    `;
}

function wireTaskCards(root) {
    root.querySelectorAll('.task-card-full').forEach(card => {
        const open = () => { location.hash = `#/tasks/${card.dataset.taskId}`; };
        card.addEventListener('click', open);
        card.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                open();
            }
        });
    });
}

function compactText(value, max = 80) {
    const text = String(value || '').replace(/\s+/g, ' ').trim();
    return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

function formatTime(ts) {
    if (!ts) return '';
    const d = new Date(ts * 1000);
    return d.toLocaleTimeString();
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str == null ? '' : String(str);
    return div.innerHTML;
}

function escapeAttr(str) {
    return escapeHtml(str).replace(/"/g, '&quot;');
}
