// Hash-based SPA router
import { store } from './store.js';
import { ws } from './ws-client.js';
import { api } from './api-client.js';

const ASSET_VERSION = '20260704-interaction-v1';

class Router {
    constructor() {
        this.routes = {
            chat: () => this.loadView('chat'),
            dashboard: () => this.loadView('dashboard'),
            tasks: () => this.loadView('task-list'),
            'tasks/:id': (params) => this.loadView('task-detail', params),
            sop: () => this.loadView('sop-editor'),
            'sop/:name': (params) => this.loadView('sop-editor', params),
            logs: () => this.loadView('log-viewer'),
            'logs/:taskId': (params) => this.loadView('log-viewer', params),
            settings: () => this.loadView('settings'),
        };
    }

    start() {
        window.addEventListener('hashchange', () => this.navigate());
        this.navigate();
    }

    navigate() {
        const hash = location.hash.replace('#/', '') || 'tasks';
        const [routeName, ...paramParts] = hash.split('/');
        // `location.hash` percent-encodes non-ASCII values (e.g. Chinese SOP
        // names), so decode here — otherwise callers that re-encode (like
        // api.getSOP) double-encode and the request 404s.
        const rawParam = paramParts[0];
        const param = rawParam ? safeDecode(rawParam) : rawParam;
        const params = { id: param, taskId: param, name: param };

        // Update sidebar nav
        document.querySelectorAll('.nav-item').forEach(item => {
            const route = item.dataset.route;
            item.classList.toggle('active', routeName.startsWith(route));
        });

        // Find matching route. A path segment after the route name (params.id)
        // means the parameterized route wins — e.g. `#/tasks/<id>` must resolve
        // to `task-detail`, not fall back to the bare `tasks` list. We therefore
        // resolve parameterized routes FIRST when an id is present, then fall
        // back to an exact match for bare routes like `#/logs` / `#/settings`.
        let handler = null;

        if (params.id) {
            for (const [pattern, fn] of Object.entries(this.routes)) {
                if (!pattern.includes(':')) continue;
                if (routeName === pattern.split('/')[0]) {
                    handler = fn;
                    break;
                }
            }
        }

        if (!handler) {
            handler = this.routes[routeName] || this.routes.tasks;
        }

        handler(params);
    }

    async loadView(viewName, params = {}) {
        const main = document.getElementById('main-content');
        if (!main) return;

        main.innerHTML = '<div class="loading">Loading...</div>';

        try {
            const module = await import(`./views/${viewName}.js?v=${ASSET_VERSION}`);
            await module.render(main, params, { store, ws, api });
        } catch (e) {
            console.error(`Failed to load view ${viewName}:`, e);
            main.innerHTML = `<div class="error-state">
                <h2>Error loading view</h2>
                <p>${e.message}</p>
            </div>`;
        }
    }
}

export const router = new Router();

function safeDecode(value) {
    try {
        return decodeURIComponent(value);
    } catch (_) {
        return value;
    }
}
