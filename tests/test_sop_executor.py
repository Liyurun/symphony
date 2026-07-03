"""Tests for the SOPExecutor."""

import asyncio

import pytest

from symphony.sop.sop_definition import NodeDefinition, SOPDefinition
from symphony.sop.sop_executor import SOPExecutor, NodeStatus


@pytest.mark.asyncio
async def test_execute_linear_sop(event_bus, event_log, pi_bridge, sample_sop):
    await event_log.create_task("task-1", "test-sop")
    executor = SOPExecutor(pi_bridge, event_log, event_bus)

    cancel = asyncio.Event()
    pause = asyncio.Event()
    pause.set()

    # Execute — nodes will fail because pi bridge is not running,
    # but the executor should attempt them all
    results = await executor.execute("task-1", sample_sop, cancel, pause)

    # All nodes should have attempted execution
    assert len(results) == 3
    assert "analyze" in results
    assert "review" in results
    assert "report" in results


@pytest.mark.asyncio
async def test_execute_cancelled(event_bus, event_log, pi_bridge, sample_sop):
    await event_log.create_task("task-1", "test-sop")
    executor = SOPExecutor(pi_bridge, event_log, event_bus)

    cancel = asyncio.Event()
    cancel.set()  # Already cancelled
    pause = asyncio.Event()
    pause.set()

    results = await executor.execute("task-1", sample_sop, cancel, pause)

    # All nodes should be skipped
    for node_id, result in results.items():
        assert result["status"] == NodeStatus.SKIPPED


@pytest.mark.asyncio
async def test_execute_single_node(event_bus, event_log, pi_bridge):
    await event_log.create_task("task-1", "test-sop")
    sop = SOPDefinition(
        name="single",
        nodes=[NodeDefinition(id="only", name="Only", skill="test-skill")],
    )

    executor = SOPExecutor(pi_bridge, event_log, event_bus)
    cancel = asyncio.Event()
    pause = asyncio.Event()
    pause.set()

    results = await executor.execute("task-1", sop, cancel, pause)
    assert "only" in results


@pytest.mark.asyncio
async def test_prepare_input(event_bus, event_log, pi_bridge):
    executor = SOPExecutor(pi_bridge, event_log, event_bus)

    node = NodeDefinition(id="review", name="Review", skill="s", depends_on=["analyze"])
    sop = SOPDefinition(name="s", nodes=[
        NodeDefinition(id="analyze", name="Analyze", skill="s"),
        node,
    ])
    node_results = {
        "analyze": {"status": NodeStatus.COMPLETED, "result": {"files": ["a.py"]}},
    }

    prepared = executor._prepare_input(sop, node, node_results)
    assert "analyze" in prepared
    assert prepared["analyze"]["files"] == ["a.py"]


@pytest.mark.asyncio
async def test_prepare_input_dependency_failed(event_bus, event_log, pi_bridge):
    executor = SOPExecutor(pi_bridge, event_log, event_bus)

    node = NodeDefinition(id="review", name="Review", skill="s", depends_on=["analyze"])
    sop = SOPDefinition(name="s", nodes=[
        NodeDefinition(id="analyze", name="Analyze", skill="s"),
        node,
    ])
    node_results = {
        "analyze": {"status": NodeStatus.FAILED, "error": "failed"},
    }

    prepared = executor._prepare_input(sop, node, node_results)
    # Failed dependency should not contribute its results
    assert "analyze" not in prepared


class _FakePiBridge:
    """A pi bridge stub that converges a fake turn and forwards events."""

    def __init__(self):
        from symphony.core.pi_bridge import PiTurnResult
        from symphony.core.pi_bridge import PiBridgeConfig
        self._PiTurnResult = PiTurnResult
        self.config = PiBridgeConfig(pi_binary="echo", cwd="/tmp/nonexistent-symphony-test")
        self.invoked = []
        self._started = True  # dispatch treats a started bridge as pi-available

    async def run_skill_to_completion(self, skill_name, task_description="",
                                      *, on_event=None, timeout=None):
        self.invoked.append((skill_name, task_description))
        # Simulate pi streaming its execution.
        if on_event:
            on_event({"type": "agent_start"})
            on_event({"type": "message_update",
                      "message": {"role": "assistant", "content": "working"}})
            on_event({"type": "tool_execution_start", "toolCallId": "t1",
                      "toolName": "bash", "args": {"command": "ls"}})
            on_event({"type": "tool_execution_end", "toolCallId": "t1",
                      "toolName": "bash", "result": "ok", "isError": False})
            on_event({"type": "message_end",
                      "message": {"role": "assistant", "content": "all done"}})
            on_event({"type": "agent_end", "willRetry": False, "messages": []})
        return self._PiTurnResult(
            text="all done",
            tool_calls=[{"name": "bash", "args": {"command": "ls"},
                         "result": "ok", "is_error": False}],
            command_id="cmd-99",
        )


@pytest.mark.asyncio
async def test_pi_path_converges_and_forwards_events(event_bus, event_log):
    """Node via pi bridge should actually complete with pi's real output,
    and forward pi's execution to the EventBus (not fake-succeed)."""
    await event_log.create_task("task-1", "single")
    sop = SOPDefinition(
        name="single",
        nodes=[NodeDefinition(id="only", name="Only", skill="reviewer")],
    )

    # Capture events published to the bus.
    seen = []

    class _Sub:
        async def on_event(self, evt):
            seen.append((evt.event_type, evt.node_id))

    event_bus.subscribe(_Sub())

    fake = _FakePiBridge()
    executor = SOPExecutor(fake, event_log, event_bus)

    cancel = asyncio.Event()
    pause = asyncio.Event()
    pause.set()

    results = await executor.execute("task-1", sop, cancel, pause)
    # Let scheduled publish tasks flush.
    await asyncio.sleep(0.05)

    # Node actually COMPLETED with pi's real output (not a fake command_id).
    assert results["only"]["status"] == NodeStatus.COMPLETED
    assert results["only"]["result"]["output"] == "all done"
    assert results["only"]["result"]["provider"] == "pi"
    assert results["only"]["result"]["tool_calls"][0]["name"] == "bash"

    # The skill was invoked with the node description.
    assert fake.invoked[0][0] == "reviewer"

    # pi's execution was forwarded to the EventBus for the TUI/Web.
    event_types = {et for et, _ in seen}
    assert "agent_message_delta" in event_types
    assert "tool_call_start" in event_types
    assert "tool_call_end" in event_types
    assert "node_completed" in event_types


@pytest.mark.asyncio
async def test_pi_prompt_includes_sop_input_output_requirements(event_bus, event_log):
    await event_log.create_task("task-1", "contract")
    sop = SOPDefinition(
        name="contract",
        description="Create a technical plan",
        input_requirements="Input must include background and constraints.",
        output_requirements="Output must include architecture and rollout plan.",
        nodes=[
            NodeDefinition(
                id="plan",
                name="Plan",
                skill="planner",
                description="Draft the plan",
                input_requirements="Node input must include a user request.",
                output_requirements="Node output must be a complete plan document.",
            )
        ],
    )
    fake = _FakePiBridge()
    executor = SOPExecutor(fake, event_log, event_bus)
    cancel = asyncio.Event()
    pause = asyncio.Event()
    pause.set()

    await executor.execute("task-1", sop, cancel, pause, initial_input={"prompt": "build qa"})

    prompt = fake.invoked[0][1]
    assert "Input must include background and constraints." in prompt
    assert "Output must include architecture and rollout plan." in prompt
    assert "Node input must include a user request." in prompt
    assert "Node output must be a complete plan document." in prompt
    assert '"prompt": "build qa"' in prompt


@pytest.mark.asyncio
async def test_pi_execution_publishes_prompt_context_evidence(event_bus, event_log, tmp_path):
    agents = tmp_path / "AGENTS.md"
    agents.write_text("# Test instructions\n\n- Be concise.", encoding="utf-8")

    await event_log.create_task("task-ctx", "ctx")
    sop = SOPDefinition(
        name="ctx",
        nodes=[NodeDefinition(id="only", name="Only", skill="")],
    )

    seen = []

    class _Sub:
        async def on_event(self, evt):
            seen.append(evt)

    event_bus.subscribe(_Sub())
    fake = _FakePiBridge()
    fake.config.cwd = str(tmp_path)
    executor = SOPExecutor(fake, event_log, event_bus)
    cancel = asyncio.Event()
    pause = asyncio.Event()
    pause.set()

    await executor.execute("task-ctx", sop, cancel, pause, initial_input={"prompt": "hi"})
    await asyncio.sleep(0.05)

    prompt_events = [e for e in seen if e.event_type == "node_prompt_prepared"]
    assert prompt_events
    data = prompt_events[0].data
    assert data["pi_cwd"] == str(tmp_path)
    assert data["prompt_sha256_short"]
    assert data["context_files"][0]["path"] == str(agents)
    assert data["context_files"][0]["sha256_short"]


class _AvailableLLM:
    """A minimal LLM provider stub that is available and streams one chunk."""

    is_available = True

    class _Cfg:
        model = "stub-llm"
        type = "openai"

    config = _Cfg()

    async def chat(self, messages):
        yield {"type": "text", "content": "llm-did-this"}
        yield {"type": "done"}


class _FakePiCounter(_FakePiBridge):
    pass


class _AvailableMiraLLM(_AvailableLLM):
    class _Cfg:
        model = "re-o-48"
        type = "mira"

    config = _Cfg()


@pytest.mark.asyncio
async def test_default_executor_is_pi_even_when_llm_available(event_bus, event_log):
    """CORE of task A: when both pi and a direct LLM are available, a node
    defaults to executor=pi (full agent loop), NOT the single-shot LLM."""
    await event_log.create_task("task-1", "single")
    sop = SOPDefinition(
        name="single",
        nodes=[NodeDefinition(id="only", name="Only", skill="reviewer")],
    )
    fake_pi = _FakePiBridge()
    llm = _AvailableLLM()
    executor = SOPExecutor(fake_pi, event_log, event_bus, llm_provider=llm)

    cancel = asyncio.Event()
    pause = asyncio.Event()
    pause.set()
    results = await executor.execute("task-1", sop, cancel, pause)

    # pi ran (provider=pi), NOT the llm.
    assert results["only"]["result"]["provider"] == "pi"
    assert fake_pi.invoked and fake_pi.invoked[0][0] == "reviewer"


@pytest.mark.asyncio
async def test_executor_llm_forces_direct_llm(event_bus, event_log):
    """executor=llm forces the single-shot path even when pi is available."""
    await event_log.create_task("task-1", "single")
    from symphony.sop.sop_definition import NodeExecutor
    sop = SOPDefinition(
        name="single",
        nodes=[NodeDefinition(id="only", name="Only", skill="reviewer",
                              executor=NodeExecutor.LLM)],
    )
    fake_pi = _FakePiBridge()
    llm = _AvailableLLM()
    executor = SOPExecutor(fake_pi, event_log, event_bus, llm_provider=llm)

    cancel = asyncio.Event()
    pause = asyncio.Event()
    pause.set()
    results = await executor.execute("task-1", sop, cancel, pause)

    # llm ran; pi was NOT invoked.
    assert results["only"]["result"].get("provider") != "pi"
    assert fake_pi.invoked == []


@pytest.mark.asyncio
async def test_executor_auto_prefers_pi_when_started(event_bus, event_log):
    """executor=auto uses pi when the bridge is started."""
    await event_log.create_task("task-1", "single")
    from symphony.sop.sop_definition import NodeExecutor
    sop = SOPDefinition(
        name="single",
        nodes=[NodeDefinition(id="only", name="Only", skill="reviewer",
                              executor=NodeExecutor.AUTO)],
    )
    fake_pi = _FakePiBridge()
    llm = _AvailableLLM()
    executor = SOPExecutor(fake_pi, event_log, event_bus, llm_provider=llm)

    cancel = asyncio.Event()
    pause = asyncio.Event()
    pause.set()
    results = await executor.execute("task-1", sop, cancel, pause)
    assert results["only"]["result"]["provider"] == "pi"


@pytest.mark.asyncio
async def test_mira_provider_prefers_direct_llm_even_when_pi_started(event_bus, event_log):
    """When active provider type is mira, all nodes should use direct LLM so
    execution honors the user's selected Mira model instead of pi's saved one."""
    await event_log.create_task("task-1", "single")
    sop = SOPDefinition(
        name="single",
        nodes=[NodeDefinition(id="only", name="Only", skill="reviewer")],
    )
    fake_pi = _FakePiBridge()
    llm = _AvailableMiraLLM()
    executor = SOPExecutor(fake_pi, event_log, event_bus, llm_provider=llm)

    cancel = asyncio.Event()
    pause = asyncio.Event()
    pause.set()
    results = await executor.execute("task-1", sop, cancel, pause)

    assert results["only"]["result"]["provider"] == "re-o-48"
    assert fake_pi.invoked == []


class _SchemaOkPi(_FakePiBridge):
    """Pi stub whose output is valid JSON matching an object schema."""

    async def run_skill_to_completion(self, skill_name, task_description="",
                                      *, on_event=None, timeout=None):
        self.invoked.append((skill_name, task_description))
        return self._PiTurnResult(text='{"files": ["a.py"]}', tool_calls=[], command_id="c1")


class _SchemaBadPi(_FakePiBridge):
    """Pi stub whose output is free text that cannot satisfy an object schema."""

    async def run_skill_to_completion(self, skill_name, task_description="",
                                      *, on_event=None, timeout=None):
        self.invoked.append((skill_name, task_description))
        return self._PiTurnResult(text="sorry, no structured output", tool_calls=[], command_id="c2")


@pytest.mark.asyncio
async def test_output_schema_valid_passes(event_bus, event_log):
    await event_log.create_task("task-1", "single")
    sop = SOPDefinition(
        name="single",
        nodes=[NodeDefinition(id="only", name="Only", skill="s",
                              output_schema={"type": "object", "required": ["files"]})],
    )
    executor = SOPExecutor(_SchemaOkPi(), event_log, event_bus)
    cancel = asyncio.Event(); pause = asyncio.Event(); pause.set()
    results = await executor.execute("task-1", sop, cancel, pause)
    assert results["only"]["status"] == NodeStatus.COMPLETED
    # recovered structured payload is attached for downstream consumption
    assert results["only"]["result"]["validated"] == {"files": ["a.py"]}


@pytest.mark.asyncio
async def test_output_schema_invalid_retries_then_fails(event_bus, event_log):
    await event_log.create_task("task-1", "single")
    from symphony.sop.sop_definition import NodeRetry
    pi = _SchemaBadPi()
    sop = SOPDefinition(
        name="single",
        nodes=[NodeDefinition(id="only", name="Only", skill="s",
                              output_schema={"type": "object", "required": ["files"]},
                              retry=NodeRetry(max_attempts=2, initial_delay=0.1))],
    )
    executor = SOPExecutor(pi, event_log, event_bus)
    cancel = asyncio.Event(); pause = asyncio.Event(); pause.set()
    results = await executor.execute("task-1", sop, cancel, pause)
    # invalid output -> retried up to max_attempts -> FAILED
    assert results["only"]["status"] == NodeStatus.FAILED
    assert len(pi.invoked) == 2  # retried


@pytest.mark.asyncio
async def test_input_schema_blocks_bad_upstream(event_bus, event_log):
    """B declares an input_schema; A produced output missing the required field,
    so B fails its input validation (retried then FAILED) instead of running."""
    await event_log.create_task("task-1", "chain")
    from symphony.sop.sop_definition import NodeRetry
    piB = _FakePiBridge()  # B would return "all done" if it ran
    sop = SOPDefinition(
        name="chain",
        nodes=[
            NodeDefinition(id="A", name="A", skill="sa"),  # no schema, free text ok
            NodeDefinition(id="B", name="B", skill="sb", depends_on=["A"],
                           input_schema={"type": "object", "required": ["files"]},
                           retry=NodeRetry(max_attempts=1, initial_delay=0.1)),
        ],
    )
    executor = SOPExecutor(piB, event_log, event_bus)
    cancel = asyncio.Event(); pause = asyncio.Event(); pause.set()
    results = await executor.execute("task-1", sop, cancel, pause)
    assert results["A"]["status"] == NodeStatus.COMPLETED
    assert results["B"]["status"] == NodeStatus.FAILED


# ── Structured artifacts + context accumulation + manual completion ──

def test_ancestor_ids_in_order_diamond():
    """Diamond DAG A -> B,C -> D: D should see A, B, C in topological order."""
    sop = SOPDefinition(
        name="diamond",
        nodes=[
            NodeDefinition(id="A", name="A", skill=""),
            NodeDefinition(id="B", name="B", skill="", depends_on=["A"]),
            NodeDefinition(id="C", name="C", skill="", depends_on=["A"]),
            NodeDefinition(id="D", name="D", skill="", depends_on=["B", "C"]),
        ],
    )
    anc = SOPExecutor._ancestor_ids_in_order(sop, "D")
    assert set(anc) == {"A", "B", "C"}
    assert anc.index("A") < anc.index("B")
    assert anc.index("A") < anc.index("C")
    assert len(anc) == len(set(anc))  # de-duplicated


@pytest.mark.asyncio
async def test_prepare_input_accumulates_ancestors(event_bus, event_log, pi_bridge):
    executor = SOPExecutor(pi_bridge, event_log, event_bus)
    sop = SOPDefinition(
        name="chain",
        nodes=[
            NodeDefinition(id="A", name="A", skill=""),
            NodeDefinition(id="B", name="B", skill="", depends_on=["A"]),
            NodeDefinition(id="C", name="C", skill="", depends_on=["B"]),
        ],
    )
    node_results = {
        "A": {"status": NodeStatus.COMPLETED, "result": {"output": "outA", "artifact": {"type": "text", "value": "outA"}}},
        "B": {"status": NodeStatus.COMPLETED, "result": {"output": "outB"}},
    }
    prepared = executor._prepare_input(sop, sop.get_node("C"), node_results)
    ctx = prepared["_ancestor_context"]
    ids = [c["node_id"] for c in ctx]
    assert ids == ["A", "B"]  # C sees BOTH ancestors, in order


def test_build_node_prompt_has_artifact_section_and_pops_context():
    from symphony.sop.artifact import ArtifactType
    sop = SOPDefinition(name="s", nodes=[NodeDefinition(id="n", name="N", skill="")])
    node = NodeDefinition(
        id="n", name="N", skill="",
        output_artifact_type=ArtifactType.FEISHU_DOC,
        output_conditions="必须包含背景/SQL/DAG",
    )
    node_input = {"_ancestor_context": [{"node_id": "A", "artifact": {"type": "sql", "value": "SELECT 1"}, "output": "x"}]}
    prompt = SOPExecutor._build_node_prompt(sop, node, node_input)
    assert "feishu_doc" in prompt
    assert "必须包含背景/SQL/DAG" in prompt
    assert "上游已完成节点的产物" in prompt
    assert "结构化产物" in prompt
    # The reserved key must NOT leak into the "实际输入" JSON.
    assert "_ancestor_context" not in prompt
    # Caller's dict is not mutated.
    assert "_ancestor_context" in node_input


@pytest.mark.asyncio
async def test_validate_output_artifact_ok_and_missing(event_bus, event_log, pi_bridge):
    from symphony.sop.artifact import ArtifactType
    from symphony.sop import schema_validator
    executor = SOPExecutor(pi_bridge, event_log, event_bus)
    node = NodeDefinition(id="n", name="N", skill="", output_artifact_type=ArtifactType.FEISHU_DOC)

    # Valid Feishu URL in output -> artifact attached.
    good = {"status": "completed", "output": "见 https://bytedance.feishu.cn/docx/abc"}
    out = executor._validate_output("task-1", node, good)
    assert out["artifact"]["type"] == "feishu_doc"
    assert out["artifact"]["value"].endswith("/docx/abc")

    # No URL -> raises (triggers retry in the real loop).
    bad = {"status": "completed", "output": "no link"}
    with pytest.raises(schema_validator.SchemaValidationError):
        executor._validate_output("task-1", node, bad)


@pytest.mark.asyncio
async def test_complete_node_manually(event_bus, event_log, pi_bridge):
    from symphony.sop.artifact import ArtifactType
    await event_log.create_task("task-1", "chain")
    executor = SOPExecutor(pi_bridge, event_log, event_bus)
    sop = SOPDefinition(
        name="chain",
        nodes=[
            NodeDefinition(id="A", name="A", skill="", output_artifact_type=ArtifactType.FEISHU_DOC),
            NodeDefinition(id="B", name="B", skill="", depends_on=["A"]),
        ],
    )
    executor._task_node_results["task-1"] = {}
    cancel = asyncio.Event(); pause = asyncio.Event(); pause.set()

    # Illegal artifact -> raises.
    with pytest.raises(ValueError):
        await executor.complete_node_manually(
            "task-1", sop, "A", {"type": "feishu_doc", "value": "not-a-url"},
            cancel, pause, rerun_downstream=False,
        )

    # Legal artifact -> node A marked COMPLETED with the manual artifact.
    await executor.complete_node_manually(
        "task-1", sop, "A",
        {"type": "feishu_doc", "value": "https://bytedance.feishu.cn/docx/xyz"},
        cancel, pause, rerun_downstream=False,
    )
    res = executor._task_node_results["task-1"]["A"]
    assert res["status"] == NodeStatus.COMPLETED
    assert res["result"]["manual"] is True
    assert res["result"]["artifact"]["value"].endswith("/docx/xyz")

    # node_completed event carries the artifact + manual marker.
    events = await event_log.get_events("task-1")
    nc = [e for e in events if e["event_type"] == "node_completed" and e["node_id"] == "A"]
    assert nc and nc[-1]["data"]["manual"] is True
    assert nc[-1]["data"]["artifact"]["type"] == "feishu_doc"


# ── needs_user_input pause + answer feedback ──

class _AskThenDonePi:
    """Pi stub: first turn asks the user; after an answer is fed back
    (extra instruction present), it completes."""

    def __init__(self):
        from symphony.core.pi_bridge import PiTurnResult, PiBridgeConfig
        self._PiTurnResult = PiTurnResult
        self.config = PiBridgeConfig(pi_binary="echo", cwd="/tmp/x")
        self._started = True
        self.calls = 0

    async def run_skill_to_completion(self, skill_name, task_description="", *, on_event=None, timeout=None):
        self.calls += 1
        if on_event:
            on_event({"type": "agent_end", "willRetry": False, "messages": []})
        if "用户已回答" in task_description:
            return self._PiTurnResult(text="完成了", command_id="c2")
        return self._PiTurnResult(
            text='{"needs_user_input": {"questions": [{"key":"db","question":"库名?"}], "reason":"缺少库名"}}',
            command_id="c1",
        )


@pytest.mark.asyncio
async def test_needs_user_input_pauses_then_answer_resumes(event_bus, event_log):
    await event_log.create_task("task-q", "single")
    pi = _AskThenDonePi()
    executor = SOPExecutor(pi, event_log, event_bus)
    sop = SOPDefinition(name="single", nodes=[NodeDefinition(id="only", name="Only", skill="s")])
    cancel = asyncio.Event(); pause = asyncio.Event(); pause.set()

    run = asyncio.create_task(executor.execute("task-q", sop, cancel, pause))
    # Wait until the node is waiting for the user's answer.
    for _ in range(100):
        await asyncio.sleep(0.02)
        if executor.human_manager._pending_questions.get("task-q:only"):
            break
    assert executor.human_manager._pending_questions.get("task-q:only")

    # Answer the question -> node re-runs with the answer appended and completes.
    await executor.human_manager.answer("task-q", "only", "库名是 prod_db")
    results = await asyncio.wait_for(run, timeout=5)
    assert results["only"]["status"] == NodeStatus.COMPLETED
    assert pi.calls == 2  # asked once, completed on the answered re-run

    # A user_question_required event was published.
    events = await event_log.get_events("task-q")
    assert any(e["event_type"] == "user_question_required" for e in events)


@pytest.mark.asyncio
async def test_reject_feedback_feeds_back_into_rerun(event_bus, event_log):
    """Rejecting a human-intervention node must append the feedback to the
    re-run prompt (previously feedback was dead data)."""
    await event_log.create_task("task-r", "single")
    pi = _FakePiBridge()
    executor = SOPExecutor(pi, event_log, event_bus)
    sop = SOPDefinition(name="single", nodes=[
        NodeDefinition(id="only", name="Only", skill="s", human_intervention=True),
    ])
    cancel = asyncio.Event(); pause = asyncio.Event(); pause.set()

    run = asyncio.create_task(executor.execute("task-r", sop, cancel, pause))
    # First approval request -> reject with feedback.
    for _ in range(100):
        await asyncio.sleep(0.02)
        if executor.human_manager._pending.get("task-r:only"):
            break
    await executor.human_manager.respond("task-r", "only", False, "请改用测试库")

    # Second approval request (the re-run) -> approve so the task settles.
    for _ in range(100):
        await asyncio.sleep(0.02)
        if executor.human_manager._pending.get("task-r:only"):
            break
    await executor.human_manager.respond("task-r", "only", True, "")
    results = await asyncio.wait_for(run, timeout=5)

    assert results["only"]["status"] == NodeStatus.COMPLETED
    # The re-run prompt must contain the reviewer feedback.
    assert any("请改用测试库" in desc for _, desc in pi.invoked)
