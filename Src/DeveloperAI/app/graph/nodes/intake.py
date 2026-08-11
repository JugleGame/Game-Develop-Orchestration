"""Intake node: marks the job as received and resets iteration counters."""

from app.graph.state import GraphState, Stage
from app.models.schemas import JobStatus


async def intake_node(state: GraphState) -> dict:
    return {
        "current_stage": Stage.INTAKE,
        "status": JobStatus.PLANNING.value,
        "dev_iteration_count": 0,
        "qa_iteration_count": 0,
        "replan_count": 0,
        "planning_feedback": None,
    }
