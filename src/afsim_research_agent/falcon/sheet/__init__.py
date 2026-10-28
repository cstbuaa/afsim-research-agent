"""Falcon v2 generation layer: the tactic sheet (design falcon-surface-formats-v2).

The model writes a tactic sheet — fenced blocks in formats it knows well (YAML,
Mermaid state diagrams, Python expressions, BehaviorTree.CPP XML). The compiler in
this package parses, links and completes it into the same skeleton and section
records the v1 generator produces, so assembly, basis enforcement, IR validation,
review and confirmation are shared. Nothing the model writes is executed.
"""
