// WebSocket client with structured message protocol and auto-reconnect
import { store } from './store.js';

class WSClient {
    constructor() {
        this.ws = null;
        this.reconnectDelay = 1000;
        this.maxReconnectDelay = 30000;
        this.currentDelay = this.reconnectDelay;
        this.shouldReconnect = true;
        this.clientId = 'web-' + Math.random().toString(36).slice(2, 10);
    }

    connect() {
        const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const url = `${protocol}//${location.host}/ws`;

        store.set('connected', false);
        this._updateStatus('connecting');

        try {
            this.ws = new WebSocket(url);

            this.ws.onopen = () => {
                console.log('[WS] Connected');
                this.currentDelay = this.reconnectDelay;
                store.set('connected', true);
                this._updateStatus('connected');
            };

            this.ws.onmessage = (event) => {
                try {
                    const msg = JSON.parse(event.data);
                    this._dispatch(msg);
                } catch (e) {
                    console.debug('[WS] Invalid message:', event.data.slice(0, 100));
                }
            };

            this.ws.onclose = (event) => {
                console.log(`[WS] Disconnected: code=${event.code}`);
                store.set('connected', false);
                this._updateStatus('disconnected');
                if (this.shouldReconnect) this._scheduleReconnect();
            };

            this.ws.onerror = (error) => {
                console.debug('[WS] Error:', error);
            };
        } catch (e) {
            console.error('[WS] Connection failed:', e);
            if (this.shouldReconnect) this._scheduleReconnect();
        }
    }

    disconnect() {
        this.shouldReconnect = false;
        if (this.ws) {
            this.ws.close();
            this.ws = null;
        }
    }

    send(msg) {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify({ ...msg, client_id: this.clientId }));
        }
    }

    subscribeTask(taskId) {
        this.send({ type: 'subscribe_task', task_id: taskId });
    }

    unsubscribeTask(taskId) {
        this.send({ type: 'unsubscribe_task', task_id: taskId });
    }

    sendUserInput(taskId, message) {
        this.send({ type: 'user_input', task_id: taskId, message });
    }

    sendHumanResponse(taskId, nodeId, approved, feedback) {
        this.send({
            type: 'human_response',
            task_id: taskId,
            node_id: nodeId,
            approved,
            feedback,
        });
    }

    createTask(sopName) {
        this.send({ type: 'create_task', sop_name: sopName });
    }

    // 方案A · ad-hoc: create + auto-start a one-node Q&A task from a free-form
    // question (no SOP). The backend synthesizes a single-node SOP and starts it.
    askQuestion(prompt) {
        this._awaitAdhoc = true;
        this.send({ type: 'create_task', prompt });
    }

    startTask(taskId) {
        this.send({ type: 'start_task', task_id: taskId });
    }

    cancelTask(taskId) {
        this.send({ type: 'cancel_task', task_id: taskId });
    }

    claimTask(taskId) {
        this.send({ type: 'claim_task', task_id: taskId });
    }

    releaseTask(taskId) {
        this.send({ type: 'release_task', task_id: taskId });
    }

    _dispatch(msg) {
        switch (msg.type) {
            case 'event':
                store.appendEvent(msg.task_id, msg);
                break;
            case 'task_update':
                store.updateTask(msg.task_id, msg.data);
                // 方案A: an ad-hoc "ask" just created & auto-started a task —
                // navigate straight into its detail so the user watches it run.
                if (this._awaitAdhoc && msg.data && msg.data.status === 'created') {
                    this._awaitAdhoc = false;
                    location.hash = `#/tasks/${msg.task_id}`;
                }
                break;
            case 'initial_state':
                store.set('tasks', msg.data.tasks || []);
                store.set('sopTemplates', msg.data.sops || []);
                break;
            case 'error':
                console.error('[WS] Server error:', msg.message);
                break;
        }
    }

    _updateStatus(status) {
        const el = document.getElementById('connection-status');
        if (el) {
            const dot = el.querySelector('.status-dot');
            const text = el.querySelector('.status-text');
            if (dot) {
                dot.className = 'status-dot ' + status;
            }
            if (text) {
                text.textContent = status === 'connected' ? 'Connected' :
                    status === 'connecting' ? 'Connecting...' : 'Disconnected';
            }
        }
    }

    _scheduleReconnect() {
        console.log(`[WS] Reconnecting in ${this.currentDelay}ms...`);
        this._updateStatus('connecting');
        setTimeout(() => {
            if (this.shouldReconnect) this.connect();
        }, this.currentDelay);
        this.currentDelay = Math.min(this.currentDelay * 2, this.maxReconnectDelay);
    }
}

export const ws = new WSClient();
