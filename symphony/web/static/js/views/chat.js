// Browser Q&A view — create an ad-hoc pi-agent task from a browser prompt.
import { api } from '../api-client.js';
import { store } from '../store.js';

export async function render(main) {
    main.innerHTML = `
        <div class="chat-page">
            <div class="page-header">
                <div>
                    <h2>Browser Q&A</h2>
                    <p class="form-help">在浏览器里输入问题后，会创建并自动运行一个 ad-hoc pi-agent task；进入详情页可继续追问。</p>
                </div>
            </div>
            <div class="chat-card">
                <label for="chat-prompt">Question</label>
                <textarea id="chat-prompt" class="form-textarea chat-prompt" rows="8"
                    placeholder="请输入要让 pi-agent 回答或执行的任务，例如：帮我评审当前改动并指出风险。"></textarea>
                <div class="chat-actions">
                    <button class="btn btn-primary" id="chat-submit">Ask</button>
                    <span class="chat-hint">提交后自动跳转到 task 详情页查看流式回答、工具调用和后续追问。</span>
                </div>
            </div>
        </div>
    `;

    const input = main.querySelector('#chat-prompt');
    const submit = main.querySelector('#chat-submit');
    const ask = async () => {
        const prompt = (input?.value || '').trim();
        if (!prompt) return;
        submit.disabled = true;
        try {
            const result = await api.ask(prompt);
            const tasks = await api.listTasks();
            store.set('tasks', tasks);
            location.hash = `#/tasks/${result.task_id}`;
        } catch (e) {
            alert(`Ask failed: ${e.message}`);
            submit.disabled = false;
        }
    };

    input?.addEventListener('keydown', (e) => {
        if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
            e.preventDefault();
            ask();
        }
    });
    submit?.addEventListener('click', ask);
    setTimeout(() => input?.focus(), 0);
}
