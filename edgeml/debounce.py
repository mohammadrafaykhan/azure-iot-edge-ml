"""Turn per-window anomaly flags into raised / cleared transitions."""

from __future__ import annotations

RAISED = "raised"
CLEARED = "cleared"


class Debouncer:
    """Raise after ``raise_after`` anomalous windows in a row; clear after ``clear_after`` normal ones."""

    def __init__(self, raise_after: int = 3, clear_after: int = 6) -> None:
        if raise_after < 1 or clear_after < 1:
            raise ValueError("raise_after and clear_after must be at least 1")
        self.raise_after = raise_after
        self.clear_after = clear_after
        self.active = False
        self.streak = 0  # consecutive windows pointing away from the current state

    def update(self, anomalous: bool) -> str | None:
        if anomalous != self.active:
            self.streak += 1
        else:
            self.streak = 0
        if not self.active and self.streak >= self.raise_after:
            self.active, self.streak = True, 0
            return RAISED
        if self.active and self.streak >= self.clear_after:
            self.active, self.streak = False, 0
            return CLEARED
        return None
