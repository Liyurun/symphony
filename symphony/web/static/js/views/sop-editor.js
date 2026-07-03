// SOP Editor view — create and edit SOP templates
import { store } from '../store.js';
import { api } from '../api-client.js';

const ARTIFACT_TYPES = [
    { value: 'text', label: '纯文本 (text)' },
    { value: 'feishu_doc', label: '飞书文档 (feishu_doc)' },
    { value: 'sql', label: 'SQL (sql)' },
    { value: 'task_id', label: '发布任务ID (task_id)' },
    { value: 'link', label: '链接 (link)' },
];

const CONDITION_PLACEHOLDER = {
    feishu_doc: '产物为飞书文档链接。约束示例：文档必须包含[背景][实现逻辑][SQL][DAG画板]等章节。',
    sql: '产物为可执行 SQL。约束示例：必须是查询语句，字段与目标表对齐，禁止 DROP/DELETE。',
    task_id: '产物为发布任务 ID。约束示例：对应一个已提交的发布单/任务编号。',
    link: '产物为可访问链接。约束示例：必须是可点击的 https URL。',
    text: '对产物内容的自然语言约束（可留空）。',
};

let currentSop = null;
let currentNodes = [];

export async function render(main, params, { store: s, ws: w, api: a }) {
    const name = params.name;
    let editSop = null;

    if (name) {
        try {
            editSop = await api.getSOP(name);
            currentSop = editSop;
            currentNodes = normalizeNodes(editSop.nodes || [], editSop);
        } catch (e) {
            console.warn('SOP not found:', name);
        }
    }

    const sops = s.get('sopTemplates') || [];

    main.innerHTML = `
        <div class="sop-editor">
            <div>
                <div class="page-header">
                    <h2>SOP Templates</h2>
                    <button class="btn btn-primary btn-new-sop">+ New Template</button>
                </div>
                <div class="sop-list" id="sop-list">
                    ${sops.length === 0 ? '<div class="empty-state">No templates yet</div>' : ''}
                    ${sops.map(sp => renderSopCard(sp, sp.name === name)).join('')}
                </div>
            </div>
            <div id="sop-form-container">
                ${editSop ? renderSopForm(editSop) : renderSopForm(null)}
            </div>
        </div>
    `;

    // SOP card click handlers
    main.querySelectorAll('.sop-card').forEach(card => {
        card.addEventListener('click', () => {
            if (card.dataset.delete) return;
            location.hash = `#/sop/${card.dataset.sopName}`;
        });
        card.querySelector('.btn-delete')?.addEventListener('click', async (e) => {
            e.stopPropagation();
            if (confirm(`Delete SOP "${card.dataset.sopName}"?`)) {
                await api.deleteSOP(card.dataset.sopName);
                refreshSopList();
                location.hash = '#/sop';
            }
        });
    });

    // New SOP button
    main.querySelector('.btn-new-sop')?.addEventListener('click', () => {
        currentSop = { name: '', version: '1.0', description: '', input_requirements: '', output_requirements: '', nodes: [] };
        currentNodes = [createDefaultNode(0, currentSop)];
        currentSop.nodes = currentNodes;
        renderSopFormInline(main, currentSop);
    });

    // Form handlers
    attachFormHandlers(main);
}

function renderSopCard(sop, selected) {
    return `
        <div class="sop-card ${selected ? 'selected' : ''}" data-sop-name="${escapeAttr(sop.name)}">
            <div style="display:flex;justify-content:space-between;align-items:start">
                <div class="sop-name">${escapeHtml(sop.name)}</div>
                <button class="btn btn-sm btn-danger btn-delete" data-delete="1" title="Delete">×</button>
            </div>
            <div class="sop-desc">${escapeHtml(sop.description || 'No description')}</div>
            <div class="sop-meta">v${escapeHtml(sop.version)} · ${sop.node_count} nodes</div>
        </div>
    `;
}

function renderSopForm(sop) {
    if (!sop) {
        return `<div class="sop-form">
            <div class="empty-state">Select a template to edit, or create a new one</div>
        </div>`;
    }

    return `<div class="sop-form" id="sop-edit-form">
        <h3>${sop.name ? `Edit: ${escapeHtml(sop.name)}` : 'New Template'}</h3>
        <p class="form-help">SOP 创建只需要四个核心部分：名称、描述、要求的输入、要求的输出。输入/输出要求需要明确说明内容限制、字段、章节或格式。</p>
        <div class="form-group">
            <label>Name</label>
            <input type="text" class="form-input" id="sop-name" value="${escapeAttr(sop.name || '')}" ${sop.name ? 'readonly' : ''}>
        </div>
        <div class="form-group">
            <label>Version</label>
            <input type="text" class="form-input" id="sop-version" value="${escapeAttr(sop.version || '1.0')}">
        </div>
        <div class="form-group">
            <label>Description</label>
            <textarea class="form-textarea" id="sop-desc" rows="3" placeholder="说明这个 SOP 要完成什么任务、适用场景和边界。">${escapeHtml(sop.description || '')}</textarea>
        </div>
        <div class="form-group">
            <label>Required Input</label>
            <textarea class="form-textarea" id="sop-input-req" rows="5" placeholder="描述输入必须包含什么、允许/不允许什么、格式限制。例如：输入必须包含需求背景、目标用户、现有系统约束、非功能要求。">${escapeHtml(sop.input_requirements || '')}</textarea>
        </div>
        <div class="form-group">
            <label>Required Output</label>
            <textarea class="form-textarea" id="sop-output-req" rows="5" placeholder="描述输出必须包含什么、结构和质量标准。例如：技术方案文档必须包含背景、目标、架构设计、接口/数据模型、风险、测试方案和上线计划。">${escapeHtml(sop.output_requirements || '')}</textarea>
        </div>

        <div class="node-builder-summary">
            <div>
                <strong>Node Flow</strong>
                <div class="form-help">默认单节点执行完整 SOP；如需拆步骤，点击“Add Sequential Node”会自动生成 ID、名称和前置依赖。</div>
            </div>
            <div class="node-builder-actions">
                <span class="node-count-badge" id="node-count-badge">${(sop.nodes || []).length || 1} node${((sop.nodes || []).length || 1) === 1 ? '' : 's'}</span>
                <button class="btn btn-sm" id="btn-add-seq-node" type="button">+ Add Sequential Node</button>
            </div>
        </div>

        <details class="advanced-section" ${(sop.nodes || []).length !== 1 || !sop.name ? 'open' : ''}>
        <summary>Advanced: Edit Nodes</summary>
        <p class="form-help">节点默认继承 SOP 的输入/输出要求。串行节点会自动依赖上一个节点；也可以在这里手动调整为并行或复杂 DAG。</p>
        <div class="sop-node-list" id="sop-nodes">
            ${normalizeNodes(sop.nodes || [], sop).map((n, i) => renderNodeItem(n, i)).join('')}
        </div>
        <button class="btn btn-sm" id="btn-add-node" type="button">+ Add Custom Node</button>
        </details>

        ${sop.name ? renderRunTaskPanel(sop) : ''}

        <div style="margin-top:16px;display:flex;gap:8px">
            <button class="btn btn-primary" id="btn-save-sop">Save</button>
            ${sop.name ? `<button class="btn btn-danger" id="btn-delete-sop">Delete</button>` : ''}
        </div>
    </div>`;
}

function renderNodeItem(node, index) {
    return `
        <div class="sop-node-item" data-node-index="${index}">
            <div class="sop-node-header">
                <span class="sop-node-id">${escapeHtml(node.id || `step-${index + 1}`)}</span>
                <div class="sop-node-controls">
                    <button class="btn btn-sm btn-danger btn-remove-node">×</button>
                </div>
            </div>
            <div class="sop-node-fields">
                <div class="form-group">
                    <label>ID</label>
                    <input type="text" class="form-input node-id" value="${escapeAttr(node.id || `step-${index + 1}`)}">
                </div>
                <div class="form-group">
                    <label>Name</label>
                    <input type="text" class="form-input node-name" value="${escapeAttr(node.name || `Step ${index + 1}`)}">
                </div>
                <div class="form-group">
                    <label>Skill</label>
                    <input type="text" class="form-input node-skill" value="${escapeAttr(node.skill || '')}">
                </div>
                <div class="form-group">
                    <label>Depends On (comma-separated)</label>
                    <input type="text" class="form-input node-deps" value="${escapeAttr((node.depends_on || []).join(', '))}" placeholder="留空表示根节点；串行节点默认依赖上一步">
                </div>
                <div class="form-group">
                    <label>Max Retries</label>
                    <input type="number" class="form-input node-retry" value="${node.retry?.max_attempts || 3}" min="1" max="10">
                </div>
                <div class="form-group form-checkbox">
                    <input type="checkbox" class="node-human" ${node.human_intervention ? 'checked' : ''}>
                    <label>Human Intervention</label>
                </div>
                <div class="form-group">
                    <label>Timeout (seconds)</label>
                    <input type="number" class="form-input node-timeout" value="${node.timeout || 300}" min="10">
                </div>
                <div class="form-group span-2">
                    <label>Description</label>
                    <textarea class="form-textarea node-desc" rows="2" placeholder="说明该节点负责的具体步骤。">${escapeHtml(node.description || '')}</textarea>
                </div>
                <div class="form-group span-2">
                    <label>Required Input</label>
                    <textarea class="form-textarea node-input-req" rows="3" placeholder="留空则保存时继承 SOP Required Input。">${escapeHtml(node.input_requirements || '')}</textarea>
                </div>
                <div class="form-group span-2">
                    <label>Required Output</label>
                    <textarea class="form-textarea node-output-req" rows="3" placeholder="留空则保存时继承 SOP Required Output。">${escapeHtml(node.output_requirements || '')}</textarea>
                </div>
                <div class="form-group">
                    <label>Input Artifact Type</label>
                    <select class="form-select node-input-artifact">
                        ${renderArtifactOptions(node.input_artifact_type)}
                    </select>
                </div>
                <div class="form-group">
                    <label>Output Artifact Type</label>
                    <select class="form-select node-output-artifact">
                        ${renderArtifactOptions(node.output_artifact_type)}
                    </select>
                </div>
                <div class="form-group span-2">
                    <label>Input Conditions（对输入产物的约束）</label>
                    <textarea class="form-textarea node-input-conditions" rows="2" placeholder="${escapeAttr(CONDITION_PLACEHOLDER[node.input_artifact_type || 'text'] || '')}">${escapeHtml(node.input_conditions || '')}</textarea>
                </div>
                <div class="form-group span-2">
                    <label>Output Conditions（对输出产物的约束）</label>
                    <textarea class="form-textarea node-output-conditions" rows="2" placeholder="${escapeAttr(CONDITION_PLACEHOLDER[node.output_artifact_type || 'text'] || '')}">${escapeHtml(node.output_conditions || '')}</textarea>
                </div>
            </div>
        </div>
    `;
}

function renderArtifactOptions(selected) {
    const sel = selected || 'text';
    return ARTIFACT_TYPES.map(t =>
        `<option value="${t.value}" ${t.value === sel ? 'selected' : ''}>${escapeHtml(t.label)}</option>`
    ).join('');
}

function renderRunTaskPanel(sop) {
    return `
        <div class="run-sop-panel">
            <div class="run-sop-header">
                <div>
                    <strong>Run Complete SOP Task</strong>
                    <div class="form-help">填写本次任务输入后，将创建并自动运行完整 SOP，所有节点会按依赖顺序执行。</div>
                </div>
                <button class="btn btn-success" id="btn-run-sop" type="button">▶ Run SOP</button>
            </div>
            <label for="sop-run-input">Task Input</label>
            <textarea class="form-textarea" id="sop-run-input" rows="5" placeholder="按 Required Input 填写本次任务输入；会作为 root node 的 prompt 传入。"></textarea>
        </div>
    `;
}

function renderSopFormInline(main, sop) {
    const container = main.querySelector('#sop-form-container');
    if (container) {
        container.innerHTML = renderSopForm(sop);
        attachFormHandlers(main);
    }
}

function attachFormHandlers(main) {
    attachNodeFieldHandlers(main);

    main.querySelector('#btn-add-seq-node')?.addEventListener('click', () => {
        addNode(main, { sequential: true });
    });

    // Add node
    main.querySelector('#btn-add-node')?.addEventListener('click', () => {
        addNode(main, { sequential: false });
    });

    // Remove node handlers
    attachNodeRemoveHandlers(main);

    // Save SOP
    main.querySelector('#btn-save-sop')?.addEventListener('click', async () => {
        await saveSop(main);
    });

    main.querySelector('#btn-run-sop')?.addEventListener('click', async () => {
        await runSopTask(main);
    });

    // Delete SOP
    main.querySelector('#btn-delete-sop')?.addEventListener('click', async () => {
        const sopName = main.querySelector('#sop-name')?.value;
        if (sopName && confirm(`Delete SOP "${sopName}"?`)) {
            await api.deleteSOP(sopName);
            await refreshSopList();
            location.hash = '#/sop';
        }
    });
}

function addNode(main, { sequential }) {
    currentNodes = collectNodesFromForm(main, { includeBlank: true });
    const sopSnapshot = getSopSnapshot(main);
    const index = currentNodes.length;
    const newNode = createDefaultNode(index, sopSnapshot);
    if (sequential && currentNodes.length > 0) {
        const prev = currentNodes[currentNodes.length - 1];
        if (prev?.id) newNode.depends_on = [prev.id];
    }
    currentNodes.push(newNode);
    const nodeList = main.querySelector('#sop-nodes');
    if (nodeList) {
        nodeList.insertAdjacentHTML('beforeend', renderNodeItem(newNode, index));
        attachNodeRemoveHandlers(main);
        attachNodeFieldHandlers(main);
        updateNodeCount(main);
        nodeList.closest('details')?.setAttribute('open', '');
        nodeList.querySelector(`.sop-node-item[data-node-index="${index}"] .node-name`)?.focus();
    }
}

function attachNodeRemoveHandlers(main) {
    main.querySelectorAll('.btn-remove-node').forEach(btn => {
        btn.onclick = (e) => {
            const item = e.target.closest('.sop-node-item');
            const idx = parseInt(item.dataset.nodeIndex);
            if (!isNaN(idx)) {
                currentNodes.splice(idx, 1);
                item.remove();
                // Re-index
                const nodeList = main.querySelector('#sop-nodes');
                nodeList?.querySelectorAll('.sop-node-item').forEach((el, i) => {
                    el.dataset.nodeIndex = i;
                });
                updateNodeCount(main);
            }
        };
    });
}

function attachNodeFieldHandlers(main) {
    main.querySelectorAll('.sop-node-item').forEach(item => {
        const refreshTitle = () => {
            const idx = parseInt(item.dataset.nodeIndex) || 0;
            const id = item.querySelector('.node-id')?.value?.trim() || `step-${idx + 1}`;
            const title = item.querySelector('.sop-node-id');
            if (title) title.textContent = id;
        };
        item.querySelector('.node-id')?.addEventListener('input', refreshTitle);
    });
}

async function saveSop(main) {
    const name = main.querySelector('#sop-name')?.value?.trim();
    const version = main.querySelector('#sop-version')?.value?.trim() || '1.0';
    const description = main.querySelector('#sop-desc')?.value?.trim() || '';
    const inputRequirements = main.querySelector('#sop-input-req')?.value?.trim() || '';
    const outputRequirements = main.querySelector('#sop-output-req')?.value?.trim() || '';

    let nodes = collectNodesFromForm(main, { inputRequirements, outputRequirements });

    if (!name || !description || !inputRequirements || !outputRequirements) {
        alert('Name, Description, Required Input and Required Output are required');
        return;
    }

    if (nodes.length === 0) {
        nodes.push(createDefaultNode(0, { name, description, input_requirements: inputRequirements, output_requirements: outputRequirements }));
    }

    const validationError = validateNodes(nodes);
    if (validationError) {
        alert(validationError);
        return;
    }

    try {
        await api.saveSOP({ name, version, description, input_requirements: inputRequirements, output_requirements: outputRequirements, nodes });
        await refreshSopList();
        location.hash = `#/sop/${name}`;
    } catch (e) {
        alert(`Failed to save SOP: ${e.message}`);
    }
}

async function runSopTask(main) {
    const sopName = main.querySelector('#sop-name')?.value?.trim();
    const sopVersion = main.querySelector('#sop-version')?.value?.trim() || '1.0';
    const prompt = main.querySelector('#sop-run-input')?.value?.trim() || '';
    if (!sopName) return;
    if (!prompt) {
        alert('请先填写 Task Input，再发起完整 SOP 任务');
        return;
    }
    const button = main.querySelector('#btn-run-sop');
    if (button) button.disabled = true;
    try {
        const result = await api.createTask(sopName, { sopVersion, prompt, autoStart: true });
        const tasks = await api.listTasks();
        store.set('tasks', tasks);
        location.hash = `#/tasks/${result.task_id}`;
    } catch (e) {
        alert(`Failed to run SOP: ${e.message}`);
        if (button) button.disabled = false;
    }
}

function getSopSnapshot(main) {
    return {
        name: main.querySelector('#sop-name')?.value?.trim() || '',
        description: main.querySelector('#sop-desc')?.value?.trim() || '',
        input_requirements: main.querySelector('#sop-input-req')?.value?.trim() || '',
        output_requirements: main.querySelector('#sop-output-req')?.value?.trim() || '',
    };
}

function normalizeNodes(nodes, sop = {}) {
    const base = nodes && nodes.length ? nodes : [createDefaultNode(0, sop)];
    return base.map((node, index) => ({
        ...createDefaultNode(index, sop),
        ...node,
        id: node.id || `step-${index + 1}`,
        name: node.name || `Step ${index + 1}`,
        depends_on: node.depends_on || [],
        retry: node.retry || { max_attempts: 3, backoff: 'exponential' },
        input_requirements: node.input_requirements || sop.input_requirements || '',
        output_requirements: node.output_requirements || sop.output_requirements || '',
    }));
}

function createDefaultNode(index, sop = {}) {
    return {
        id: `step-${index + 1}`,
        name: index === 0 && sop.name ? sop.name : `Step ${index + 1}`,
        skill: '',
        depends_on: [],
        description: index === 0 ? (sop.description || '') : '',
        input_requirements: sop.input_requirements || '',
        output_requirements: sop.output_requirements || '',
        input_artifact_type: 'text',
        output_artifact_type: 'text',
        input_conditions: '',
        output_conditions: '',
        retry: { max_attempts: 3, backoff: 'exponential' },
        human_intervention: false,
        timeout: 300,
    };
}

function collectNodesFromForm(main, options = {}) {
    const inputRequirements = options.inputRequirements ?? main.querySelector('#sop-input-req')?.value?.trim() ?? '';
    const outputRequirements = options.outputRequirements ?? main.querySelector('#sop-output-req')?.value?.trim() ?? '';
    const nodes = [];
    main.querySelectorAll('.sop-node-item').forEach((item, index) => {
        const fallbackId = `step-${index + 1}`;
        const id = item.querySelector('.node-id')?.value?.trim() || (options.includeBlank ? fallbackId : fallbackId);
        nodes.push({
            id,
            name: item.querySelector('.node-name')?.value?.trim() || id,
            skill: item.querySelector('.node-skill')?.value?.trim() || '',
            depends_on: (item.querySelector('.node-deps')?.value || '')
                .split(',').map(s => s.trim()).filter(Boolean),
            retry: {
                max_attempts: parseInt(item.querySelector('.node-retry')?.value) || 3,
                backoff: 'exponential',
            },
            human_intervention: item.querySelector('.node-human')?.checked || false,
            timeout: parseInt(item.querySelector('.node-timeout')?.value) || 300,
            description: item.querySelector('.node-desc')?.value?.trim() || '',
            input_requirements: item.querySelector('.node-input-req')?.value?.trim() || inputRequirements,
            output_requirements: item.querySelector('.node-output-req')?.value?.trim() || outputRequirements,
            input_artifact_type: item.querySelector('.node-input-artifact')?.value || 'text',
            output_artifact_type: item.querySelector('.node-output-artifact')?.value || 'text',
            input_conditions: item.querySelector('.node-input-conditions')?.value?.trim() || '',
            output_conditions: item.querySelector('.node-output-conditions')?.value?.trim() || '',
        });
    });
    return nodes;
}

function validateNodes(nodes) {
    const ids = new Set();
    for (const node of nodes) {
        if (!node.id) return '每个节点都需要 ID';
        if (ids.has(node.id)) return `节点 ID 重复：${node.id}`;
        ids.add(node.id);
    }
    for (const node of nodes) {
        for (const dep of node.depends_on || []) {
            if (!ids.has(dep)) return `节点 ${node.id} 依赖了不存在的节点：${dep}`;
            if (dep === node.id) return `节点 ${node.id} 不能依赖自己`;
        }
    }
    const visiting = new Set();
    const visited = new Set();
    const byId = new Map(nodes.map(node => [node.id, node]));
    const visit = (nodeId, path = []) => {
        if (visited.has(nodeId)) return '';
        if (visiting.has(nodeId)) return `节点依赖存在环：${[...path, nodeId].join(' -> ')}`;
        visiting.add(nodeId);
        const node = byId.get(nodeId);
        for (const dep of node.depends_on || []) {
            const err = visit(dep, [...path, nodeId]);
            if (err) return err;
        }
        visiting.delete(nodeId);
        visited.add(nodeId);
        return '';
    };
    for (const node of nodes) {
        const err = visit(node.id, []);
        if (err) return err;
    }
    return '';
}

function updateNodeCount(main) {
    const count = main.querySelectorAll('.sop-node-item').length;
    const badge = main.querySelector('#node-count-badge');
    if (badge) badge.textContent = `${count} node${count === 1 ? '' : 's'}`;
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str == null ? '' : String(str);
    return div.innerHTML;
}

function escapeAttr(str) {
    return escapeHtml(str).replace(/"/g, '&quot;');
}

async function refreshSopList() {
    try {
        const sops = await api.listSOPs();
        store.set('sopTemplates', sops);
    } catch (e) {
        console.warn('Refresh failed:', e);
    }
}
