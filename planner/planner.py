
from dataclasses import dataclass, field
from typing import Literal
from uuid import uuid4


TaskStatus = Literal[
    "pending",
    "in_progress",
    "completed",
    "failed",
    "blocked",
]


@dataclass
class Task:
    description: str
    task_id: str = field(
        default_factory=lambda: uuid4().hex[:12]
    )
    status: TaskStatus = "pending"
    dependencies: list[str] = field(default_factory=list)
    result: str | None = None


@dataclass
class Plan:
    goal: str
    plan_id: str = field(
        default_factory=lambda: uuid4().hex[:12]
    )
    tasks: list[Task] = field(default_factory=list)
    status: Literal[
        "active",
        "completed",
        "failed",
    ] = "active"

class Planner:
    def __init__(self):
        self.plans: dict[str, Plan] = {}

    def create_plan(self, goal: str) -> Plan:
        """Create and store a new plan."""
        if not goal.strip():
            raise ValueError("Plan goal cannot be empty.")

        plan = Plan(goal=goal.strip())
        self.plans[plan.plan_id] = plan
        return plan

    def get_plan(self, plan_id: str) -> Plan:
        """Retrieve a plan by its ID."""
        plan = self.plans.get(plan_id)

        if plan is None:
            raise ValueError(f"Plan not found: {plan_id}")

        return plan

    def add_task(
        self,
        plan_id: str,
        description: str,
        dependencies: list[str] | None = None,
    ) -> Task:
        """Add a task with optional prerequisite task IDs."""
        plan = self.get_plan(plan_id)

        if plan.status != "active":
            raise ValueError("Cannot add tasks to an inactive plan.")

        if not description.strip():
            raise ValueError("Task description cannot be empty.")

        dependencies = dependencies or []
        existing_ids = {task.task_id for task in plan.tasks}

        if len(dependencies) != len(set(dependencies)):
            raise ValueError("Duplicate task dependencies are not allowed.")

        for dependency_id in dependencies:
            if dependency_id not in existing_ids:
                raise ValueError(
                    f"Dependency does not exist: {dependency_id}"
                )

        task = Task(
            description=description.strip(),
            dependencies=dependencies.copy(),
        )

        plan.tasks.append(task)
        return task

    def get_next_task(self, plan_id: str) -> Task | None:
        """Return the first pending task whose dependencies are complete."""
        plan = self.get_plan(plan_id)

        for task in plan.tasks:
            if task.status != "pending":
                continue

            dependencies_complete = all(
                next(
                    dependency
                    for dependency in plan.tasks
                    if dependency.task_id == dependency_id
                ).status == "completed"
                for dependency_id in task.dependencies
            )

            if dependencies_complete:
                return task

        return None

    def update_task(
        self,
        plan_id: str,
        task_id: str,
        status: TaskStatus,
        result: str | None = None,
    ) -> Task:
        """Update task status and optionally store its result."""
        plan = self.get_plan(plan_id)

        task = next(
            (item for item in plan.tasks if item.task_id == task_id),
            None,
        )

        if task is None:
            raise ValueError(f"Task not found: {task_id}")

        if status == "in_progress":
            if task.status != "pending":
                raise ValueError(
                    "Only pending tasks can be started."
                )

            for dependency_id in task.dependencies:
                dependency = next(
                    item
                    for item in plan.tasks
                    if item.task_id == dependency_id
                )

                if dependency.status != "completed":
                    raise ValueError(
                        f"Dependency is not complete: {dependency_id}"
                    )

        task.status = status

        if result is not None:
            task.result = result

        self._refresh_plan_status(plan)
        return task

    def complete_task(
        self,
        plan_id: str,
        task_id: str,
        result: str,
    ) -> Task:
        """Mark a task complete and record its result."""
        if not result.strip():
            raise ValueError(
                "A result is required to complete a task."
            )

        return self.update_task(
            plan_id=plan_id,
            task_id=task_id,
            status="completed",
            result=result.strip(),
        )

    def _refresh_plan_status(self, plan: Plan) -> None:
        """Update overall plan status based on task states."""
        if plan.tasks and all(
            task.status == "completed" for task in plan.tasks
        ):
            plan.status = "completed"
        elif any(task.status == "failed" for task in plan.tasks):
            plan.status = "failed"
        else:
            plan.status = "active"

    def task_to_dict(self, task: Task) -> dict:
        """Convert a task into a serializable dictionary."""
        return {
            "task_id": task.task_id,
            "description": task.description,
            "status": task.status,
            "dependencies": task.dependencies,
            "result": task.result,
        }

    def plan_to_dict(self, plan_id: str) -> dict:
        """Convert a complete plan into a serializable dictionary."""
        plan = self.get_plan(plan_id)

        return {
            "plan_id": plan.plan_id,
            "goal": plan.goal,
            "status": plan.status,
            "tasks": [
                self.task_to_dict(task)
                for task in plan.tasks
            ],
        }

if __name__ == "__main__":
    planner = Planner()

    # 1. Create a plan
    plan = planner.create_plan("Build an LLM backend")
    print("\n1. PLAN CREATED")
    print(planner.plan_to_dict(plan.plan_id))

    # 2. Add the first task
    task = planner.add_task(
        plan_id=plan.plan_id,
        description="Design the architecture",
    )
    print("\n2. TASK ADDED")
    print(planner.task_to_dict(task))

    # 3. Find the next task
    next_task = planner.get_next_task(plan.plan_id)
    print("\n3. NEXT TASK")
    print(planner.task_to_dict(next_task))

    # 4. Start the task
    planner.update_task(
        plan_id=plan.plan_id,
        task_id=task.task_id,
        status="in_progress",
    )
    print("\n4. TASK STARTED")

    # 5. Complete the task
    planner.complete_task(
        plan_id=plan.plan_id,
        task_id=task.task_id,
        result="Architecture designed successfully.",
    )
    print("\n5. TASK COMPLETED")
    print(planner.plan_to_dict(plan.plan_id))
