"""Local, deterministic post-solver selection; no device dispatch."""
from m4_selection.contracts import SelectionPolicy, SelectionResult
from m4_selection.service import select_candidate

__all__ = ['SelectionPolicy', 'SelectionResult', 'select_candidate']
