"""Local, deterministic post-solver selection; no device dispatch."""
from m4.selection.contracts import SelectionPolicy, SelectionResult
from m4.selection.service import select_candidate

__all__ = ['SelectionPolicy', 'SelectionResult', 'select_candidate']
