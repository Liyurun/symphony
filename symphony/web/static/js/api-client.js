// REST API client wrapper
import { store } from './store.js';

const BASE = '/api';

class APIClient {
    async _fetch(path, options = {}) {
        const url = `${BASE}${path}`;
        const res = await fetch(url, {
            headers: { 'Content-Type': 'application/json', ...options.headers },
            ...options,
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({ detail: res.statusText }));
            throw new Error(err.detail || `HTTP ${res.status}`);
        }
        return res.json();
    }

    // Tasks
    async listTasks(status = null) {
        const qs = status ? `?status=${status}` : '';
        return this._fetch(`/tasks${qs}`);
    }

    async createTask(sopName, { sopVersion = '1.0', prompt = '', inputs = null, autoStart = true } = {}) {
        const body = {
            sop_name: sopName || '',
            sop_version: sopVersion,
            auto_start: autoStart,
        };
        if (prompt) body.prompt = prompt;
        if (inputs) body.inputs = inputs;
        return this._fetch('/tasks', {
            method: 'POST',
            body: JSON.stringify(body),
        });
    }

    // 方案A: ask a single-turn question as a one-node task.
    async ask(prompt, skill = '') {
        return this._fetch('/ask', {
            method: 'POST',
            body: JSON.stringify({ prompt, skill }),
        });
    }

    // 方案A multi-turn: append a new turn to an existing ad-hoc task.
    async followUp(taskId, prompt, skill = '') {
        return this._fetch(`/tasks/${taskId}/follow-up`, {
            method: 'POST',
            body: JSON.stringify({ prompt, skill }),
        });
    }

    // Interrupt & rerun a node ("打断并重来"), auto-cascading downstream.
    async redirectNode(taskId, nodeId, instruction = '') {
        return this._fetch(`/tasks/${taskId}/redirect`, {
            method: 'POST',
            body: JSON.stringify({ node_id: nodeId, instruction }),
        });
    }

    // Manually mark a node completed with an operator-supplied artifact.
    async completeNode(taskId, { nodeId, artifactType, artifactValue, label = null, rerunDownstream = true }) {
        return this._fetch(`/tasks/${taskId}/complete-node`, {
            method: 'POST',
            body: JSON.stringify({
                node_id: nodeId,
                artifact_type: artifactType,
                artifact_value: artifactValue,
                label,
                rerun_downstream: rerunDownstream,
            }),
        });
    }

    // Each node's latest artifact for a task.
    async getTaskArtifacts(taskId) {
        return this._fetch(`/tasks/${taskId}/artifacts`);
    }

    async getTask(taskId) {
        return this._fetch(`/tasks/${taskId}`);
    }

    async startTask(taskId) {
        return this._fetch(`/tasks/${taskId}/start`, { method: 'POST' });
    }

    async cancelTask(taskId) {
        return this._fetch(`/tasks/${taskId}/cancel`, { method: 'POST' });
    }

    async pauseTask(taskId) {
        return this._fetch(`/tasks/${taskId}/pause`, { method: 'POST' });
    }

    async resumeTask(taskId) {
        return this._fetch(`/tasks/${taskId}/resume`, { method: 'POST' });
    }

    async claimTask(taskId, clientId) {
        return this._fetch(`/tasks/${taskId}/claim`, {
            method: 'POST',
            body: JSON.stringify({ client_id: clientId }),
        });
    }

    async releaseTask(taskId) {
        return this._fetch(`/tasks/${taskId}/release`, { method: 'POST' });
    }

    async deleteTask(taskId) {
        return this._fetch(`/tasks/${taskId}`, { method: 'DELETE' });
    }

    async getTaskEvents(taskId, afterSeq = 0) {
        return this._fetch(`/tasks/${taskId}/events?after_seq=${afterSeq}`);
    }

    async exportTaskEvents(taskId) {
        return this._fetch(`/tasks/${taskId}/export`);
    }

    // SOP
    async listSOPs() {
        return this._fetch('/sop');
    }

    async getSOP(name) {
        return this._fetch(`/sop/${encodeURIComponent(name)}`);
    }

    async saveSOP(data) {
        return this._fetch('/sop', {
            method: 'POST',
            body: JSON.stringify(data),
        });
    }

    async deleteSOP(name) {
        return this._fetch(`/sop/${encodeURIComponent(name)}`, { method: 'DELETE' });
    }

    async validateSOP(definition) {
        return this._fetch('/sop/validate', {
            method: 'POST',
            body: JSON.stringify({ definition }),
        });
    }

    // Config
    async getConfig() {
        return this._fetch('/config');
    }

    async updateConfig(partial) {
        return this._fetch('/config', {
            method: 'PUT',
            body: JSON.stringify(partial),
        });
    }

    async resetConfig() {
        return this._fetch('/config/reset', { method: 'POST' });
    }

    // Skills
    async listSkills() {
        return this._fetch('/skills');
    }

    async getPiState() {
        return this._fetch('/pi/state');
    }

    async getPiModels() {
        return this._fetch('/pi/models');
    }

    // Logs
    async searchLogs(filters = {}) {
        const params = new URLSearchParams(filters);
        return this._fetch(`/logs?${params}`);
    }

    async getLogStats() {
        return this._fetch('/logs/stats');
    }

    // Human
    async humanRespond(taskId, nodeId, approved, feedback = '') {
        return this._fetch('/human/respond', {
            method: 'POST',
            body: JSON.stringify({ task_id: taskId, node_id: nodeId, approved, feedback }),
        });
    }

    // Answer a node's pending needs_user_input question.
    async answerQuestion(taskId, nodeId, answer) {
        return this._fetch('/human/answer', {
            method: 'POST',
            body: JSON.stringify({ task_id: taskId, node_id: nodeId, answer }),
        });
    }
}

export const api = new APIClient();
