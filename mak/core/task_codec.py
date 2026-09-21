"""One JSON shape for a ``SubTask``, shared by the planner and the task graph.

The planner's plan array and ``.mak/task_graph.json`` are the same objects, and
they used to be serialized by two hand-written dict literals. Adding a field to
one and not the other is how ``--recover`` would silently drop a task's Wave 20
declarations — a recovered body-only task would be locked as undeclared, and a
recovered contract would vanish. One encoder and one decoder remove the chance.

Decoding here is *lenient* (types are coerced, absent keys default): it reads
state MAK itself wrote. Planner output, which a model wrote, is validated by
``mak.planner.planner.parse_plan`` instead.
"""

from __future__ import annotations

from collections.abc import Mapping

from mak.core.types import NodeId, RepairObligation, SubTask


def subtask_to_dict(task: SubTask) -> dict[str, object]:
    """Encode a task.

    Declarations are written only when set.

    Omitting unset declarations keeps a plan that never used them identical to
    what it was before they existed, byte for byte.
    """
    data: dict[str, object] = {
        "task_id": task.task_id,
        "description": task.description,
        "target_nodes": [str(n) for n in task.target_nodes],
        "context_nodes": [str(n) for n in task.context_nodes],
        "depends_on": list(task.depends_on),
        "agent_type": task.agent_type,
    }
    if task.changes_api is not None:
        data["changes_api"] = task.changes_api
    if task.api_targets:
        data["api_targets"] = [str(n) for n in task.api_targets]
    if task.contract:
        data["contract"] = {str(k): v for k, v in task.contract.items()}
    if task.registry_keys:
        data["registry_keys"] = {
            str(k): list(v) for k, v in task.registry_keys.items()
        }
    if task.repair_obligations:
        data["repair_obligations"] = [
            {
                "kind": obligation.kind,
                "file": obligation.file,
                "defining_file": obligation.defining_file,
                "detail": obligation.detail,
                "exact_key": obligation.exact_key,
                "family_key": obligation.family_key,
                "subject": obligation.subject,
                "site": obligation.site,
                "required_provider_symbol": obligation.required_provider_symbol,
            }
            for obligation in task.repair_obligations
        ]
    return data


def subtask_from_dict(raw: Mapping[str, object]) -> SubTask:
    """Decode a task MAK wrote. Raises ``KeyError``/``TypeError`` when malformed."""
    changes_api = raw.get("changes_api")
    return SubTask(
        task_id=str(raw["task_id"]),
        description=str(raw["description"]),
        target_nodes=[NodeId(str(n)) for n in _seq(raw.get("target_nodes"))],
        context_nodes=[NodeId(str(n)) for n in _seq(raw.get("context_nodes"))],
        depends_on=[str(d) for d in _seq(raw.get("depends_on"))],
        agent_type=str(raw.get("agent_type", "")),
        changes_api=changes_api if isinstance(changes_api, bool) else None,
        api_targets=[NodeId(str(n)) for n in _seq(raw.get("api_targets"))],
        contract={
            NodeId(str(k)): str(v) for k, v in _map(raw.get("contract")).items()
        },
        registry_keys={
            NodeId(str(k)): [str(key) for key in _seq(v)]
            for k, v in _map(raw.get("registry_keys")).items()
        },
        repair_obligations=tuple(
            RepairObligation(
                kind=str(item["kind"]),
                file=str(item["file"]),
                defining_file=str(item["defining_file"]),
                detail=str(item["detail"]),
                exact_key=str(item["exact_key"]),
                family_key=str(item["family_key"]),
                subject=str(item.get("subject", "")),
                site=str(item.get("site", "")),
                required_provider_symbol=(
                    str(required)
                    if (required := item.get("required_provider_symbol")) is not None
                    else None
                ),
            )
            for value in _seq(raw.get("repair_obligations"))
            for item in [_map(value)]
        ),
    )


def _seq(value: object) -> list[object]:
    """Return a JSON array as a list; absent is empty, anything else is an error."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise TypeError(f"expected a JSON array, got {type(value).__name__}")
    return value


def _map(value: object) -> dict[object, object]:
    """Return a JSON object as a dict; absent is empty, anything else is an error."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TypeError(f"expected a JSON object, got {type(value).__name__}")
    return value
