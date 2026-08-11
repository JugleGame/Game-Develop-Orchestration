"""HumanEscalation node: reached after 5 failed dev/QA iterations (§3, §7.2).

Alert delivery (Slack/Email) is a separate, not-yet-specified integration;
this node only records the terminal state for operators to act on.
"""

from app.graph.state import GraphState, Stage
from app.models.schemas import JobStatus


async def human_escalation_node(state: GraphState) -> dict:
    return {
        "current_stage": Stage.HUMAN_ESCALATION,
        "status": JobStatus.ESCALATED.value,
    }
