"""Tests for SOP definition models."""

import pytest
from symphony.sop.sop_definition import (
    NodeDefinition,
    NodeRetry,
    RetryStrategy,
    SOPDefinition,
)


class TestSOPDefinition:
    def test_basic_parsing(self):
        sop = SOPDefinition(
            name="test-sop",
            nodes=[
                NodeDefinition(id="a", name="Node A", skill="skill-a"),
                NodeDefinition(id="b", name="Node B", skill="skill-b", depends_on=["a"]),
            ],
        )
        assert sop.name == "test-sop"
        assert len(sop.nodes) == 2
        assert sop.get_node("a").name == "Node A"

    def test_topological_order_simple(self):
        sop = SOPDefinition(
            name="test",
            nodes=[
                NodeDefinition(id="a", name="A", skill="s"),
                NodeDefinition(id="b", name="B", skill="s", depends_on=["a"]),
                NodeDefinition(id="c", name="C", skill="s", depends_on=["b"]),
            ],
        )
        levels = sop.topological_order()
        assert len(levels) == 3
        assert levels[0][0].id == "a"
        assert levels[1][0].id == "b"
        assert levels[2][0].id == "c"

    def test_topological_order_parallel(self):
        sop = SOPDefinition(
            name="test",
            nodes=[
                NodeDefinition(id="a", name="A", skill="s"),
                NodeDefinition(id="b", name="B", skill="s"),
                NodeDefinition(id="c", name="C", skill="s", depends_on=["a", "b"]),
            ],
        )
        levels = sop.topological_order()
        assert len(levels) >= 1
        first_ids = {n.id for level in levels for n in level}
        assert first_ids == {"a", "b", "c"}

    def test_parallel_groups(self):
        sop = SOPDefinition(
            name="test",
            nodes=[
                NodeDefinition(id="a", name="A", skill="s", parallel_group="g1"),
                NodeDefinition(id="b", name="B", skill="s", parallel_group="g1"),
                NodeDefinition(id="c", name="C", skill="s"),
            ],
        )
        levels = sop.topological_order()
        # a and b should be in the same group because they share parallel_group
        all_ids = [n.id for level in levels for n in level]
        assert "a" in all_ids
        assert "b" in all_ids
        assert "c" in all_ids

    def test_get_dependencies(self):
        sop = SOPDefinition(
            name="test",
            nodes=[
                NodeDefinition(id="a", name="A", skill="s"),
                NodeDefinition(id="b", name="B", skill="s", depends_on=["a"]),
            ],
        )
        assert sop.get_dependencies("b") == ["a"]
        assert sop.get_dependencies("a") == []
        assert sop.get_dependents("a") == ["b"]

    def test_empty_nodes_raises(self):
        with pytest.raises(Exception):
            SOPDefinition(name="test", nodes=[])


class TestNodeDefinition:
    def test_defaults(self):
        node = NodeDefinition(id="test", name="Test", skill="test-skill")
        assert node.retry.max_attempts == 3
        assert node.retry.backoff == RetryStrategy.EXPONENTIAL
        assert node.human_intervention is False
        assert node.timeout == 300
        assert node.input_requirements == ""
        assert node.output_requirements == ""

    def test_four_part_sop_contract(self):
        sop = SOPDefinition(
            name="technical-plan",
            description="Draft a technical plan",
            input_requirements="Input must include background, goals, constraints, and stakeholders.",
            output_requirements="Output must include architecture, API, risks, tests, and rollout plan.",
            nodes=[
                NodeDefinition(
                    id="draft",
                    name="Draft",
                    skill="",
                    input_requirements="Use the SOP input requirements.",
                    output_requirements="Return the technical plan document.",
                )
            ],
        )
        assert sop.input_requirements.startswith("Input must")
        assert sop.nodes[0].output_requirements == "Return the technical plan document."

    def test_custom_retry(self):
        node = NodeDefinition(
            id="test",
            name="Test",
            skill="s",
            retry=NodeRetry(max_attempts=5, backoff=RetryStrategy.FIXED, initial_delay=2.0),
        )
        assert node.retry.max_attempts == 5
        assert node.retry.backoff == RetryStrategy.FIXED
