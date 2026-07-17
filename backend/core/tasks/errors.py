from __future__ import annotations


class ActiveTaskError(Exception):
    pass


class ActiveTaskNotFoundError(ActiveTaskError):
    pass


class ActiveTaskConflictError(ActiveTaskError):
    pass


class ActiveTaskVersionConflictError(ActiveTaskConflictError):
    current_generation_id: str
    current_revision: int

    def __init__(
        self,
        current_generation_id: str,
        current_revision: int,
    ) -> None:
        super().__init__("active task version changed")
        self.current_generation_id = str(current_generation_id)
        self.current_revision = int(current_revision)


class TaskContextDisabledError(ActiveTaskError):
    pass
