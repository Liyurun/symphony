// Task detail view — node graph + agent output stream + controls
import { store } from '../store.js';
import { ws } from '../ws-client.js';
import { api } from '../api-client.js';

const STATUS_ICONS = {
    pending: '○', running: '▶', completed: '✓', failed: '✗',
    cancelled: '⊘', retrying: '↻', skipped: '⊘', waiting_human: '⏸',
};

// Per-render node metadata / artifacts (reset each time a task detail loads).
let nodeArtifactTypes = {};
let currentArtifacts = {};

export async function render(main, params, { store: s, ws: w, api: a }) {
    const taskId = params.id;
    if (!taskId) {
        main.innerHTML = '<div class="empty-state">Select a task from the sidebar</div>';
        return;
    }

    s.set('activeTaskId', taskId);
    w.subscribeTask(taskId);

    // Reset per-render artifact state for this task.
    nodeArtifactTypes = {};
    currentArtifacts = {};

    let task = s.getTask(taskId);
    let fullTask = task;
    try {
        fullTask = await api.getTask(taskId);
        task = { ...(task || {}), ...(fullTask || {}) };
        if (task) s.addOrUpdateTask(task);
    } catch (e) {
        console.warn('Could not load task details:', e);
    }

    const prompt = task?.metadata?.prompt || task?.metadata?.input || '';
    const title = prompt ? compactText(prompt, 96) : (task?.sop_name || 'Task');
    const status = task?.status || 'pending';

    main.innerHTML = `
        <div class="task-detail">
            <div class="task-detail-header">
                <div>
                    <div class="breadcrumb"><a href="#/tasks">Tasks</a> / <span>${escapeHtml(taskId)}</span></div>
                    <h2>${escapeHtml(title)}</h2>
                    <div class="task-meta-row">
                        <span class="badge badge-${status}">${status}</span>
                        <span>SOP: ${escapeHtml(task?.sop_name || 'ad-hoc')}</span>
                        <span>Created: ${formatDateTime(task?.created_at)}</span>
                        ${task?.updated_at ? `<span>Updated: ${formatDateTime(task.updated_at)}</span>` : ''}
                    </div>
                </div>
                <div class="task-controls">
                    <button class="btn btn-success btn-start" ${task?.status === 'running' ? 'disabled' : ''}>▶ Start</button>
                    <button class="btn btn-warning btn-pause" ${task?.status !== 'running' ? 'disabled' : ''}>⏸ Pause</button>
                    <button class="btn btn-danger btn-cancel" ${['completed','failed','cancelled'].includes(task?.status) ? 'disabled' : ''}>■ Cancel</button>
                    ${task?.claimed_by ? `<button class="btn btn-ghost btn-release">🔓 Release</button>` : `<button class="btn btn-ghost btn-claim">🔒 Claim</button>`}
                </div>
            </div>

            ${prompt ? `<div class="detail-summary-card">
                <div class="summary-label">User Input</div>
                <div class="summary-text">${escapeHtml(prompt)}</div>
            </div>` : ''}

            ${renderPromptContextCard(task)}

            <div class="detail-grid">
                <div class="detail-panel">
                    <div class="panel-header"><span>SOP Nodes</span><span id="node-summary" class="panel-subtitle">Loading…</span></div>
                    <div class="panel-body">
                        <div id="node-graph" class="node-graph">
                            <div class="empty-state">Loading SOP nodes...</div>
                        </div>
                    </div>
                </div>
                <div class="detail-panel">
                    <div class="panel-header"><span>Task Timeline</span><span class="panel-subtitle">live events & output</span></div>
                    <div class="panel-body" id="event-stream-container">
                        <div id="event-stream" class="event-stream">
                            <div class="empty-state">Waiting for events...</div>
                        </div>
                    </div>
                </div>
            </div>

            <div class="input-bar-container">
                <input type="text" class="form-input" id="task-input" placeholder="Type a message and press Enter..."
                       autocomplete="off">
                <button class="btn btn-primary" id="btn-send">Send</button>
            </div>
        </div>
    `;

    // Load SOP nodes for this task. The task GET response now carries the
    // resolved node graph (works for both registry-backed and ad-hoc tasks).
    try {
        if (fullTask && Array.isArray(fullTask.nodes) && fullTask.nodes.length) {
            renderNodeGraph({ nodes: fullTask.nodes }, main, taskId);
        } else {
            const sopList = s.get('sopTemplates') || [];
            const sop = sopList.find(sp => sp.name === task?.sop_name);
            if (sop && Array.isArray(sop.nodes) && sop.nodes.length) {
                renderNodeGraph(sop, main, taskId);
            } else if (task) {
                const fullSop = await api.getSOP(task.sop_name);
                renderNodeGraph(fullSop, main, taskId);
            }
        }
    } catch (e) {
        console.warn('Could not load SOP nodes:', e);
    }

    // Load historical events
    try {
        const events = await api.getTaskEvents(taskId);
        const container = main.querySelector('#event-stream');
        if (container && events.length > 0) {
            container.innerHTML = '';
            events.forEach(evt => appendEvent(container, evt));
            const scrollContainer = main.querySelector('#event-stream-container');
            if (scrollContainer) scrollContainer.scrollTop = scrollContainer.scrollHeight;
        }
    } catch (e) {
        console.warn('Could not load events:', e);
    }

    // Load per-node artifacts and paint them onto the node cards.
    try {
        const artifacts = await api.getTaskArtifacts(taskId);
        Object.entries(artifacts || {}).forEach(([nid, art]) => paintNodeArtifact(nid, art));
    } catch (e) {
        console.debug('Could not load artifacts:', e);
    }

    // Listen for live events
    s.subscribe(`events.${taskId}`, (events) => {
        const container = main.querySelector('#event-stream');
        if (container) {
            if (!events || events.length === 0) return;
            const latest = events[events.length - 1];
            appendEvent(container, latest);
            // Auto-scroll
            const scrollContainer = main.querySelector('#event-stream-container');
            if (scrollContainer) {
                scrollContainer.scrollTop = scrollContainer.scrollHeight;
            }
        }
    });

    // Button handlers
    const btnStart = main.querySelector('.btn-start');
    const btnPause = main.querySelector('.btn-pause');
    const btnCancel = main.querySelector('.btn-cancel');
    const btnClaim = main.querySelector('.btn-claim');
    const btnRelease = main.querySelector('.btn-release');
    const btnSend = main.querySelector('#btn-send');
    const input = main.querySelector('#task-input');

    btnStart?.addEventListener('click', async () => {
        await api.startTask(taskId);
        refreshTaskDetail(main, taskId);
    });
    btnPause?.addEventListener('click', async () => {
        await api.pauseTask(taskId);
        refreshTaskDetail(main, taskId);
    });
    btnCancel?.addEventListener('click', async () => {
        await api.cancelTask(taskId);
        refreshTaskDetail(main, taskId);
    });
    btnClaim?.addEventListener('click', async () => {
        await api.claimTask(taskId, w.clientId);
        refreshTaskDetail(main, taskId);
    });
    btnRelease?.addEventListener('click', async () => {
        await api.releaseTask(taskId);
        refreshTaskDetail(main, taskId);
    });

    input?.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && input.value.trim()) {
            w.sendUserInput(taskId, input.value.trim());
            input.value = '';
        }
    });
    btnSend?.addEventListener('click', () => {
        if (input?.value.trim()) {
            w.sendUserInput(taskId, input.value.trim());
            input.value = '';
        }
    });
}

function renderNodeGraph(sop, main, taskId = null) {
    const container = main.querySelector('#node-graph');
    if (!container || !sop.nodes) return;

    const summary = main.querySelector('#node-summary');
    if (summary) summary.textContent = `${sop.nodes.length} node${sop.nodes.length === 1 ? '' : 's'}`;

    // Remember each node's output artifact type so the "Mark done" form can
    // default its type dropdown and validate the value client-side.
    nodeArtifactTypes = {};
    sop.nodes.forEach(n => { nodeArtifactTypes[n.id] = n.output_artifact_type || 'text'; });

    // Build levels from dependencies
    const levels = computeLevels(sop.nodes);

    let html = '';
    levels.forEach((level, idx) => {
        if (idx > 0) {
            html += '<div class="graph-arrow">→</div>';
        }
        html += '<div class="graph-level">';
        level.forEach(node => {
            const atype = node.output_artifact_type || 'text';
            html += `
                <div class="node-card" data-node-id="${escapeHtml(node.id)}">
                    <div class="node-status-dot" title="pending"></div>
                    <div class="node-name">${escapeHtml(node.name || node.id)}</div>
                    <div class="node-id">${escapeHtml(node.id)}</div>
                    <div class="node-skill">${escapeHtml(node.skill || 'default')}</div>
                    ${atype !== 'text' ? `<div class="node-artifact-type">产物: ${escapeHtml(atype)}</div>` : ''}
                    ${node.human_intervention ? '<div class="node-status" style="color:var(--magenta)">👤 Human check</div>' : ''}
                    <div class="node-artifact" data-node-artifact="${escapeHtml(node.id)}"></div>
                    ${taskId ? `<div class="node-actions">
                        <button class="btn btn-sm btn-ghost node-redirect" data-node-id="${escapeHtml(node.id)}" title="Interrupt & rerun this node (auto-cascades downstream)">↻ Redo</button>
                        <button class="btn btn-sm btn-ghost node-complete" data-node-id="${escapeHtml(node.id)}" title="Manually mark done and fill the output artifact">✓ Mark done</button>
                    </div>` : ''}
                </div>
            `;
        });
        html += '</div>';
    });

    container.innerHTML = html;

    // Wire node action buttons — only when we have a task.
    if (taskId) {
        container.querySelectorAll('.node-redirect').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                openRedirectForm(btn.closest('.node-card'), taskId, btn.getAttribute('data-node-id'));
            });
        });
        container.querySelectorAll('.node-complete').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                openCompleteForm(btn.closest('.node-card'), taskId, btn.getAttribute('data-node-id'));
            });
        });
    }

    // Paint any artifacts already known for this task.
    Object.entries(currentArtifacts).forEach(([nid, art]) => paintNodeArtifact(nid, art));
}

const ARTIFACT_TYPE_OPTIONS = ['text', 'feishu_doc', 'sql', 'task_id', 'link'];

function openRedirectForm(card, taskId, nodeId) {
    if (!card || card.querySelector('.node-inline-form')) return;
    const form = document.createElement('div');
    form.className = 'node-inline-form';
    form.innerHTML = `
        <textarea class="form-textarea" rows="2" placeholder="可选：给出重跑指令（打断并重来，下游自动重跑）"></textarea>
        <div class="node-inline-actions">
            <button class="btn btn-sm btn-primary node-inline-submit">Rerun</button>
            <button class="btn btn-sm btn-ghost node-inline-cancel">Cancel</button>
        </div>
    `;
    card.appendChild(form);
    form.querySelector('.node-inline-cancel').addEventListener('click', (e) => { e.stopPropagation(); form.remove(); });
    form.querySelector('.node-inline-submit').addEventListener('click', async (e) => {
        e.stopPropagation();
        const instruction = form.querySelector('textarea').value.trim();
        const submit = form.querySelector('.node-inline-submit');
        submit.disabled = true;
        try {
            await api.redirectNode(taskId, nodeId, instruction);
            form.remove();
        } catch (err) {
            alert(`Redirect failed: ${err.message}`);
            submit.disabled = false;
        }
    });
}

function openCompleteForm(card, taskId, nodeId) {
    if (!card || card.querySelector('.node-inline-form')) return;
    const defType = nodeArtifactTypes[nodeId] || 'text';
    const form = document.createElement('div');
    form.className = 'node-inline-form';
    form.innerHTML = `
        <label class="node-inline-label">产物类型</label>
        <select class="form-select node-complete-type">
            ${ARTIFACT_TYPE_OPTIONS.map(t => `<option value="${t}" ${t === defType ? 'selected' : ''}>${t}</option>`).join('')}
        </select>
        <label class="node-inline-label">产物值</label>
        <textarea class="form-textarea node-complete-value" rows="2" placeholder="填入该节点的产物（如飞书文档链接 / SQL / 发布任务ID）"></textarea>
        <input type="text" class="form-input node-complete-label" placeholder="可选：产物说明">
        <div class="node-inline-actions">
            <button class="btn btn-sm btn-success node-inline-submit">Mark done</button>
            <button class="btn btn-sm btn-ghost node-inline-cancel">Cancel</button>
        </div>
    `;
    card.appendChild(form);
    form.querySelector('.node-inline-cancel').addEventListener('click', (e) => { e.stopPropagation(); form.remove(); });
    form.querySelector('.node-inline-submit').addEventListener('click', async (e) => {
        e.stopPropagation();
        const artifactType = form.querySelector('.node-complete-type').value;
        const artifactValue = form.querySelector('.node-complete-value').value.trim();
        const label = form.querySelector('.node-complete-label').value.trim() || null;
        const clientErr = validateArtifactClient(artifactType, artifactValue);
        if (clientErr) { alert(clientErr); return; }
        const submit = form.querySelector('.node-inline-submit');
        submit.disabled = true;
        try {
            await api.completeNode(taskId, { nodeId, artifactType, artifactValue, label });
            form.remove();
        } catch (err) {
            alert(`Mark done failed: ${err.message}`);
            submit.disabled = false;
        }
    });
}

// Client-side mirror of backend artifact.validate_artifact_format (fast feedback).
function validateArtifactClient(atype, value) {
    const v = (value || '').trim();
    if (!v) return '产物值不能为空';
    const feishu = /^https?:\/\/[\w.-]*(feishu\.cn|larksuite\.com|feishu-pre\.net)\/(docx|docs|sheets|wiki|base|file|drive|sheet|mindnote)\/\S+$/i;
    const url = /^https?:\/\/\S+$/i;
    if (atype === 'feishu_doc' && !feishu.test(v)) return '必须是合法的飞书文档链接';
    if (atype === 'link' && !url.test(v)) return '必须是合法的 URL';
    return '';
}

function paintNodeArtifact(nodeId, art) {
    if (!art) return;
    currentArtifacts[nodeId] = art;
    const el = document.querySelector(`.node-artifact[data-node-artifact="${cssEscape(nodeId)}"]`);
    if (el) el.innerHTML = renderArtifactHtml(art);
}

function renderQuestionCard(evt, data, time) {
    const questions = Array.isArray(data.questions) ? data.questions : [];
    const reason = data.reason ? `<div class="question-reason">${escapeHtml(data.reason)}</div>` : '';
    const fields = questions.map((q, i) => `
        <div class="question-field">
            <label>${escapeHtml(q.question || ('问题 ' + (i + 1)))}</label>
            <textarea class="form-textarea question-input" rows="2"
                data-q-key="${escapeAttr(q.key || ('q' + (i + 1)))}"
                data-q-question="${escapeAttr(q.question || '')}"
                placeholder="请输入…"></textarea>
        </div>
    `).join('');
    return `<span class="event-time">${time}</span>
        <span class="event-type">❓ 需要你的输入</span>
        <div class="question-card" data-node-id="${escapeAttr(evt.node_id || '')}">
            ${reason}
            ${fields}
            <div class="node-inline-actions">
                <button class="btn btn-sm btn-primary question-submit">提交回答</button>
            </div>
        </div>`;
}

function wireQuestionCard(div, evt) {
    const card = div.querySelector('.question-card');
    if (!card) return;
    const btn = card.querySelector('.question-submit');
    btn?.addEventListener('click', async () => {
        const parts = [];
        card.querySelectorAll('.question-input').forEach(inp => {
            const q = inp.getAttribute('data-q-question') || inp.getAttribute('data-q-key');
            const v = inp.value.trim();
            if (v) parts.push(`${q}：${v}`);
        });
        if (parts.length === 0) { alert('请至少回答一个问题'); return; }
        btn.disabled = true;
        try {
            await api.answerQuestion(evt.task_id, evt.node_id, parts.join('\n'));
            card.classList.add('answered');
            btn.textContent = '已提交';
        } catch (e) {
            alert(`提交失败: ${e.message}`);
            btn.disabled = false;
        }
    });
}

function renderArtifactHtml(art) {
    if (!art || !art.value) return '';
    const type = art.type || 'text';
    const label = art.label ? ` <span class="artifact-label">${escapeHtml(art.label)}</span>` : '';
    if (type === 'feishu_doc' || type === 'link') {
        return `<a class="artifact-link" href="${escapeAttr(art.value)}" target="_blank" rel="noopener">🔗 ${escapeHtml(type === 'feishu_doc' ? '飞书文档' : '链接')}</a>${label}`;
    }
    if (type === 'sql') {
        return `<pre class="artifact-sql"><code>${escapeHtml(art.value)}</code></pre>${label}`;
    }
    if (type === 'task_id') {
        return `<span class="artifact-taskid">🆔 ${escapeHtml(art.value)}</span>${label}`;
    }
    return `<span class="artifact-text">${escapeHtml(compactText(art.value, 120))}</span>${label}`;
}

function computeLevels(nodes) {
    const inDegree = {};
    const adj = {};
    nodes.forEach(n => {
        inDegree[n.id] = (n.depends_on || []).length;
        adj[n.id] = [];
    });
    nodes.forEach(n => {
        (n.depends_on || []).forEach(dep => {
            if (adj[dep]) adj[dep].push(n.id);
        });
    });

    const queue = Object.keys(inDegree).filter(id => inDegree[id] === 0);
    const levels = [];
    const visited = new Set();

    while (queue.length > 0) {
        const level = [];
        const nextQueue = [];
        for (const nid of queue) {
            if (visited.has(nid)) continue;
            visited.add(nid);
            const node = nodes.find(n => n.id === nid);
            if (node) level.push(node);
            for (const dep of (adj[nid] || [])) {
                inDegree[dep]--;
                if (inDegree[dep] === 0) nextQueue.push(dep);
            }
        }
        if (level.length > 0) levels.push(level);
        queue.length = 0;
        queue.push(...nextQueue);
    }

    return levels;
}

function appendEvent(container, evt) {
    const time = evt.timestamp ? new Date(evt.timestamp * 1000).toLocaleTimeString() : '';
    const type = evt.event_type;
    const data = parseEventData(evt.data);

    const div = document.createElement('div');
    div.className = `event-item event-${type}`;

    switch (type) {
        case 'node_started':
        case 'node_retry':
            div.innerHTML = `<span class="event-time">${time}</span>
                <span class="event-type">${type}</span>
                ▶ Node <strong>${escapeHtml(data.node_name || evt.node_id)}</strong> started
                (attempt ${data.attempt || 1}/${data.max_attempts || '?'})`;
            updateNodeStatus(evt.node_id, 'running');
            break;

        case 'node_completed':
            div.innerHTML = `<span class="event-time">${time}</span>
                <span class="event-type">${type}</span>
                ✓ Node <strong>${escapeHtml(data.node_name || evt.node_id)}</strong> completed${data.manual ? ' <span class="manual-badge">手动置成功</span>' : ''}
                ${data.artifact ? `<div class="event-artifact">${renderArtifactHtml(data.artifact)}</div>` : ''}`;
            updateNodeStatus(evt.node_id, 'completed');
            if (data.artifact) paintNodeArtifact(evt.node_id, data.artifact);
            break;

        case 'node_failed':
            div.innerHTML = `<span class="event-time">${time}</span>
                <span class="event-type">${type}</span>
                ✗ Node <strong>${escapeHtml(evt.node_id)}</strong> failed:
                ${escapeHtml(data.error || data.reason || 'Unknown error')}`;
            updateNodeStatus(evt.node_id, 'failed');
            break;

        case 'tool_call_start':
            div.innerHTML = createToolCallCard(data, time);
            div.querySelector('.tool-call-header')?.addEventListener('click', () => {
                div.querySelector('.tool-call-card')?.classList.toggle('expanded');
            });
            break;

        case 'agent_message_delta':
            renderAgentMessageDelta(container, evt, data, time);
            return;

        case 'node_prompt_prepared':
            div.innerHTML = createPromptContextEvent(data, time);
            div.querySelector('.prompt-context-header')?.addEventListener('click', () => {
                div.querySelector('.prompt-context-card')?.classList.toggle('expanded');
            });
            break;

        case 'human_intervention_required':
            div.innerHTML = `<span class="event-time">${time}</span>
                <span class="event-type">⚠️ Human Approval</span>
                <strong>${escapeHtml(data.node_name || evt.node_id)}</strong>
                <button class="btn btn-sm btn-success" onclick="showApprovalModal('${escapeJs(evt.task_id)}','${escapeJs(evt.node_id)}','${escapeJs(data.node_name || evt.node_id)}','${escapeJs(JSON.stringify(data.result_preview || {}))}')">
                    Review
                </button>`;
            updateNodeStatus(evt.node_id, 'waiting_human');
            break;

        case 'human_intervention_response':
            div.innerHTML = `<span class="event-time">${time}</span>
                <span class="event-type">${type}</span>
                ${data.approved ? '✓ Approved' : '✗ Rejected'} ${data.feedback ? ': ' + escapeHtml(data.feedback) : ''}`;
            break;

        case 'user_question_required':
            div.innerHTML = renderQuestionCard(evt, data, time);
            wireQuestionCard(div, evt);
            updateNodeStatus(evt.node_id, 'waiting_human');
            break;

        case 'user_question_answered':
            div.innerHTML = `<span class="event-time">${time}</span>
                <span class="event-type">💬 已回答</span>
                <div class="agent-output">${escapeHtml(data.answer || '')}</div>`;
            // Mark any still-open question card for this node as answered.
            document.querySelectorAll(`.question-card[data-node-id="${cssEscape(evt.node_id || '')}"]`).forEach(card => {
                card.classList.add('answered');
                const btn = card.querySelector('.question-submit');
                if (btn) { btn.disabled = true; btn.textContent = '已提交'; }
            });
            break;

        case 'task_started':
        case 'task_completed':
        case 'task_failed':
        case 'task_cancelled':
        case 'task_paused':
            div.innerHTML = `<span class="event-time">${time}</span>
                <span class="event-type">${type}</span>
                <strong>${type.replace('task_', '').toUpperCase()}</strong>`;
            break;

        case 'error':
            div.innerHTML = `<span class="event-time">${time}</span>
                <span class="event-type">❌ Error</span>
                ${escapeHtml(data.error || 'Unknown error')}`;
            break;

        default:
            div.innerHTML = `<span class="event-time">${time}</span>
                <span class="event-type">${type}</span>
                ${escapeHtml(JSON.stringify(data).slice(0, 200))}`;
    }

    container.appendChild(div);
}

function renderAgentMessageDelta(container, evt, data, time) {
    const text = data.text || '';
    if (!text) return;

    const nodeId = evt.node_id || 'task';
    const streamKey = `${evt.task_id || ''}:${nodeId}`;
    let item = container.querySelector(`.event-agent-message-stream[data-stream-key="${cssEscape(streamKey)}"]`);

    if (!item) {
        item = document.createElement('div');
        item.className = 'event-item event-agent_message_delta event-agent-message-stream';
        item.dataset.streamKey = streamKey;
        item.dataset.renderedText = '';
        item.innerHTML = `<span class="event-time">${time}</span>
            <span class="event-type">pi</span>
            <div class="agent-output markdown-content"></div>`;
        container.appendChild(item);
    }

    const out = item.querySelector('.agent-output');
    if (!out) return;

    const previous = item.dataset.renderedText || '';
    let next;
    if (data.replace || text.startsWith(previous)) {
        next = text;
    } else {
        next = previous + text;
    }
    if (next === previous) return;

    item.dataset.renderedText = next;
    out.innerHTML = renderMarkdown(next);
}

function createToolCallCard(data, time) {
    const args = data.arguments || data.input || {};
    const name = data.tool_name || data.name || 'unknown';
    const summary = summarizeToolArgs(args);
    return `<span class="event-time">${time}</span>
        <span class="event-type">tool_call</span>
        <div class="tool-call-card">
            <div class="tool-call-header">🔧 <strong>${escapeHtml(name)}</strong>${summary ? `<span class="tool-call-summary">${escapeHtml(summary)}</span>` : ''}</div>
            <div class="tool-call-body"><pre>${escapeHtml(JSON.stringify(args, null, 2))}</pre></div>
        </div>`;
}

function renderPromptContextCard(task) {
    const files = task?.metadata?.pi_context_files || [];
    const cwd = task?.metadata?.pi_cwd || '';
    if (!cwd && files.length === 0) return '';
    const agents = files.filter(f => String(f.name || '').toUpperCase().startsWith('AGENTS'));
    return `<div class="detail-summary-card prompt-evidence-card">
        <div class="summary-label">Pi prompt context evidence</div>
        <div class="summary-text">
            <div><strong>Pi cwd:</strong> ${escapeHtml(cwd || '-')}</div>
            <div><strong>AGENTS.md in scope:</strong> ${agents.length ? 'yes' : 'no'}</div>
            ${files.map(f => `<div class="context-file-row">${escapeHtml(f.path || '')}${f.sha256_short ? ` <code>${escapeHtml(f.sha256_short)}</code>` : ''}${f.bytes ? ` · ${escapeHtml(f.bytes)} bytes` : ''}</div>`).join('')}
        </div>
    </div>`;
}

function createPromptContextEvent(data, time) {
    const files = data.context_files || [];
    const agents = files.filter(f => String(f.name || '').toUpperCase().startsWith('AGENTS'));
    return `<span class="event-time">${time}</span>
        <span class="event-type">prompt_context</span>
        <div class="prompt-context-card">
            <div class="prompt-context-header">📌 pi prompt prepared · AGENTS.md: ${agents.length ? 'in scope' : 'not found'} · prompt <code>${escapeHtml(data.prompt_sha256_short || '')}</code></div>
            <div class="prompt-context-body">
                <div><strong>pi cwd:</strong> ${escapeHtml(data.pi_cwd || '-')}</div>
                <div><strong>context files:</strong></div>
                ${files.length ? `<ul>${files.map(f => `<li>${escapeHtml(f.path || '')}${f.sha256_short ? ` <code>${escapeHtml(f.sha256_short)}</code>` : ''}${f.bytes ? ` · ${escapeHtml(f.bytes)} bytes` : ''}</li>`).join('')}</ul>` : '<div class="form-help">No AGENTS.md / CLAUDE.md discovered from pi cwd.</div>'}
                <details>
                    <summary>Prompt preview sent to pi</summary>
                    <pre>${escapeHtml(data.prompt_preview || '')}</pre>
                </details>
            </div>
        </div>`;
}

function updateNodeStatus(nodeId, status) {
    const card = document.querySelector(`.node-card[data-node-id="${nodeId}"]`);
    if (card) {
        card.className = `node-card ${status}`;
        card.querySelector('.node-status-dot')?.setAttribute('title', status);
    }
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str == null ? '' : String(str);
    return div.innerHTML;
}

function escapeAttr(str) {
    return escapeHtml(str).replace(/"/g, '&quot;');
}

function escapeJs(str) {
    return String(str == null ? '' : str).replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/\n/g, '\\n');
}

function parseEventData(raw) {
    if (!raw) return {};
    if (typeof raw !== 'string') return raw;
    try {
        return JSON.parse(raw || '{}');
    } catch (_) {
        return { text: raw };
    }
}

function renderMarkdown(text) {
    const lines = String(text || '').split('\n');
    const html = [];
    let inCode = false;
    let codeLines = [];
    let inList = false;

    const closeList = () => {
        if (inList) {
            html.push('</ul>');
            inList = false;
        }
    };
    const closeCode = () => {
        if (inCode) {
            html.push(`<pre><code>${escapeHtml(codeLines.join('\n'))}</code></pre>`);
            codeLines = [];
            inCode = false;
        }
    };

    for (const rawLine of lines) {
        const line = rawLine.replace(/\s+$/, '');
        if (line.trim().startsWith('```')) {
            if (inCode) closeCode();
            else { closeList(); inCode = true; codeLines = []; }
            continue;
        }
        if (inCode) {
            codeLines.push(rawLine);
            continue;
        }
        if (!line.trim()) {
            closeList();
            html.push('<br>');
            continue;
        }
        const heading = line.match(/^(#{1,3})\s*(.+)$/);
        if (heading) {
            closeList();
            const level = heading[1].length;
            html.push(`<h${level}>${renderInlineMarkdown(heading[2])}</h${level}>`);
            continue;
        }
        const bullet = line.match(/^\s*(?:[-*•]|\d+[.)])\s+(.+)$/);
        if (bullet) {
            if (!inList) { html.push('<ul>'); inList = true; }
            html.push(`<li>${renderInlineMarkdown(bullet[1])}</li>`);
            continue;
        }
        closeList();
        html.push(`<p>${renderInlineMarkdown(line)}</p>`);
    }
    closeCode();
    closeList();
    return html.join('');
}

function renderInlineMarkdown(text) {
    let out = escapeHtml(text);
    out = out.replace(/`([^`]+)`/g, '<code>$1</code>');
    out = out.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    out = out.replace(/\*([^*]+)\*/g, '<em>$1</em>');
    return out;
}

function summarizeToolArgs(args) {
    if (!args || typeof args !== 'object') return '';
    const preferred = ['command', 'cmd', 'path', 'file_path', 'uri', 'url', 'query', 'prompt', 'Identifier'];
    for (const key of preferred) {
        if (args[key]) return `${key}: ${compactText(args[key], 90)}`;
    }
    const entries = Object.entries(args).slice(0, 2).map(([key, value]) => `${key}: ${compactText(value, 48)}`);
    return entries.join(' · ');
}

function compactText(value, max = 80) {
    const text = typeof value === 'string' ? value : JSON.stringify(value);
    const compact = String(text || '').replace(/\s+/g, ' ').trim();
    return compact.length > max ? `${compact.slice(0, max - 1)}…` : compact;
}

function formatDateTime(ts) {
    if (!ts) return '-';
    return new Date(ts * 1000).toLocaleString();
}

function cssEscape(str) {
    if (window.CSS && typeof window.CSS.escape === 'function') {
        return window.CSS.escape(str);
    }
    return String(str).replace(/[^a-zA-Z0-9_-]/g, '\\$&');
}

async function refreshTaskDetail(main, taskId) {
    try {
        const tasks = await api.listTasks();
        store.set('tasks', tasks);
        location.reload();
    } catch (e) {
        console.warn('Refresh failed:', e);
    }
}
