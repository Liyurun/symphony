// Reactive state store with pub/sub
class Store {
    constructor() {
        this._state = {
            tasks: [],
            activeTaskId: null,
            sopTemplates: [],
            eventCache: {},      // task_id -> events[]
            config: {},
            skills: [],
            connected: false,
        };
        this._listeners = new Map();  // path -> Set<callback>
    }

    get(path) {
        const keys = path.split('.');
        let val = this._state;
        for (const k of keys) {
            if (val === null || val === undefined) return undefined;
            val = val[k];
        }
        return val;
    }

    set(path, value) {
        const keys = path.split('.');
        let obj = this._state;
        for (let i = 0; i < keys.length - 1; i++) {
            if (!(keys[i] in obj)) obj[keys[i]] = {};
            obj = obj[keys[i]];
        }
        const oldValue = obj[keys[keys.length - 1]];
        obj[keys[keys.length - 1]] = value;

        if (oldValue !== value) {
            this._notify(path, value);
        }
    }

    subscribe(path, callback) {
        if (!this._listeners.has(path)) {
            this._listeners.set(path, new Set());
        }
        this._listeners.get(path).add(callback);

        // Return unsubscribe function
        return () => {
            const set = this._listeners.get(path);
            if (set) set.delete(callback);
        };
    }

    _notify(path, value) {
        // Notify exact path listeners
        const set = this._listeners.get(path);
        if (set) {
            for (const cb of set) cb(value);
        }
        // Notify wildcard listeners
        this._listeners.forEach((callbacks, listenerPath) => {
            if (listenerPath === '*') {
                for (const cb of callbacks) cb(this._state);
            }
        });
    }

    // Convenience methods

    getTask(taskId) {
        return this._state.tasks.find(t => t.task_id === taskId);
    }

    updateTask(taskId, updates) {
        const tasks = this._state.tasks.map(t =>
            t.task_id === taskId ? { ...t, ...updates } : t
        );
        this.set('tasks', tasks);
    }

    addOrUpdateTask(task) {
        const idx = this._state.tasks.findIndex(t => t.task_id === task.task_id);
        const tasks = [...this._state.tasks];
        if (idx >= 0) {
            tasks[idx] = { ...tasks[idx], ...task };
        } else {
            tasks.unshift(task);
        }
        this.set('tasks', tasks);
    }

    appendEvent(taskId, event) {
        const cache = { ...this._state.eventCache };
        if (!cache[taskId]) cache[taskId] = [];
        cache[taskId] = [...cache[taskId], event];
        this._state.eventCache = cache;
        this._notify(`events.${taskId}`, cache[taskId]);
    }
}

export const store = new Store();
