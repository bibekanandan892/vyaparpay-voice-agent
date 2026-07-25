"""Frozen shared contract: domain types (types.py) and Protocols (interfaces.py).

Every other package in app/ imports from here instead of defining its own
ToolResult/Message/etc. This package is the single place those shapes are
allowed to change — treat edits here as breaking changes to everything
downstream.
"""
