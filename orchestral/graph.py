from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Sequence

from .error import OrchestralCycleError, OrchestralGraphError
from .step import Step


@dataclass(frozen=True)
class ExecutionPlan:
    """Validated wave-based execution plan for a workflow or group."""

    waves: list[list[Step[Any]]]
    upstream_step_ids: dict[int, tuple[int, ...]]
    step_by_id: dict[int, Step[Any]]

    def execution_order(self) -> list[list[str]]:
        return [[step.name for step in wave] for wave in self.waves]


def _find_cycle(
    steps: list[Step[Any]],
    upstream_adj: list[list[int]],
    remaining: set[int],
) -> list[str]:
    """Extract a concrete dependency cycle path among unresolved nodes."""
    # color: 0 = unvisited, 1 = visiting, 2 = visited
    color: dict[int, int] = {idx: 0 for idx in remaining}
    path: list[int] = []

    def dfs(node: int) -> list[int] | None:
        color[node] = 1
        path.append(node)
        for dep in upstream_adj[node]:
            if dep not in remaining:
                continue
            if color[dep] == 1:
                start = path.index(dep)
                return path[start:] + [dep]
            if color[dep] == 0:
                found = dfs(dep)
                if found is not None:
                    return found
        path.pop()
        color[node] = 2
        return None

    for idx in sorted(remaining):
        if color[idx] == 0:
            cycle_indices = dfs(idx)
            if cycle_indices is not None:
                return [steps[i].name for i in cycle_indices]

    # Unreachable for any non-empty cyclic subgraph, fallback safely.
    return [steps[i].name for i in sorted(remaining)]


def build_execution_plan(
    groups: Sequence[Sequence[Step[Any]]],
) -> ExecutionPlan:
    """Validate dependencies across groups and compute parallel execution waves.

    - Groups execute sequentially in order (group barriers are preserved).
    - Within each group, steps are topologically sorted into concurrent waves
      using Kahn's algorithm so every step runs only after all of its
      dependencies have completed.
    - A step may depend on steps in the same group or in any earlier group.
    - Missing dependencies, forward references to later groups, and dependency
      cycles raise ``OrchestralGraphError`` / ``OrchestralCycleError``.
    """
    step_lists: list[list[Step[Any]]] = [list(group) for group in groups]

    group_index_by_step_id: dict[int, int] = {}
    name_to_group_indices: dict[str, set[int]] = defaultdict(set)
    step_by_id: dict[int, Step[Any]] = {}

    for group_idx, steps in enumerate(step_lists):
        for step in steps:
            sid = id(step)
            step_by_id[sid] = step
            group_index_by_step_id[sid] = group_idx
            name_to_group_indices[step.name].add(group_idx)

    waves: list[list[Step[Any]]] = []
    upstream_step_ids: dict[int, tuple[int, ...]] = {}
    prior_step_ids: set[int] = set()
    prior_steps_by_name: dict[str, list[Step[Any]]] = defaultdict(list)

    for group_idx, steps in enumerate(step_lists):
        if not steps:
            continue

        n = len(steps)
        local_idx_by_step_id: dict[int, int] = {id(s): i for i, s in enumerate(steps)}
        local_indices_by_name: dict[str, list[int]] = defaultdict(list)
        for i, step in enumerate(steps):
            local_indices_by_name[step.name].append(i)

        dependents: list[list[int]] = [[] for _ in range(n)]
        upstream_adj: list[list[int]] = [[] for _ in range(n)]
        in_degree: list[int] = [0] * n

        for i, step in enumerate(steps):
            resolved_local_deps: set[int] = set()
            resolved_upstream_ids: list[int] = []
            seen_upstream_ids: set[int] = set()

            for ref in step._dependency_refs:
                if isinstance(ref, Step):
                    ref_id = id(ref)
                    if ref_id in local_idx_by_step_id:
                        dep_idx = local_idx_by_step_id[ref_id]
                        if dep_idx == i:
                            raise OrchestralCycleError([step.name, step.name])
                        resolved_local_deps.add(dep_idx)
                        if ref_id not in seen_upstream_ids:
                            seen_upstream_ids.add(ref_id)
                            resolved_upstream_ids.append(ref_id)
                    elif ref_id in prior_step_ids:
                        if ref_id not in seen_upstream_ids:
                            seen_upstream_ids.add(ref_id)
                            resolved_upstream_ids.append(ref_id)
                    elif ref_id in group_index_by_step_id:
                        later_idx = group_index_by_step_id[ref_id]
                        raise OrchestralGraphError(
                            f"Step {step.name!r} in group {group_idx} cannot depend on "
                            f"step {ref.name!r} in later group {later_idx}"
                        )
                    else:
                        raise OrchestralGraphError(
                            f"Step {step.name!r} depends on step {ref.name!r}, "
                            f"which is not in this workflow"
                        )
                else:
                    local_matches = local_indices_by_name.get(ref, [])
                    prior_matches = prior_steps_by_name.get(ref, [])

                    if local_matches or prior_matches:
                        if i in local_matches and not prior_matches:
                            raise OrchestralCycleError([step.name, step.name])
                        for dep_idx in local_matches:
                            if dep_idx == i:
                                continue
                            resolved_local_deps.add(dep_idx)
                            dep_id = id(steps[dep_idx])
                            if dep_id not in seen_upstream_ids:
                                seen_upstream_ids.add(dep_id)
                                resolved_upstream_ids.append(dep_id)
                        for prior_step in prior_matches:
                            prior_id = id(prior_step)
                            if prior_id not in seen_upstream_ids:
                                seen_upstream_ids.add(prior_id)
                                resolved_upstream_ids.append(prior_id)
                    elif ref in name_to_group_indices:
                        later_idx = min(name_to_group_indices[ref])
                        raise OrchestralGraphError(
                            f"Step {step.name!r} in group {group_idx} cannot depend on "
                            f"step {ref!r} in later group {later_idx}"
                        )
                    else:
                        raise OrchestralGraphError(
                            f"Step {step.name!r} depends on unknown step {ref!r}"
                        )

            for dep_idx in sorted(resolved_local_deps):
                dependents[dep_idx].append(i)
                upstream_adj[i].append(dep_idx)
                in_degree[i] += 1

            upstream_step_ids[id(step)] = tuple(resolved_upstream_ids)

        ready = [i for i in range(n) if in_degree[i] == 0]
        scheduled = 0
        while ready:
            wave_indices = sorted(ready)
            waves.append([steps[i] for i in wave_indices])
            scheduled += len(wave_indices)

            next_ready: list[int] = []
            for u in wave_indices:
                for v in dependents[u]:
                    in_degree[v] -= 1
                    if in_degree[v] == 0:
                        next_ready.append(v)
            ready = next_ready

        if scheduled < n:
            remaining = {i for i in range(n) if in_degree[i] > 0}
            cycle = _find_cycle(steps, upstream_adj, remaining)
            raise OrchestralCycleError(cycle)

        for step in steps:
            prior_step_ids.add(id(step))
            prior_steps_by_name[step.name].append(step)

    return ExecutionPlan(
        waves=waves,
        upstream_step_ids=upstream_step_ids,
        step_by_id=step_by_id,
    )
