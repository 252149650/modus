"""Cross-cutting resource plane: workers, memory budgets, scheduling.

Resource policy shapes cost, never output facts: the same corpus must yield
the same bundle under any profile. Stages depend on this package; it depends
on no stage.
"""
