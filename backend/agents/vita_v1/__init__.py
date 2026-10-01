"""VITA — the Vendor Interoperability Troubleshooting Agent.

First-class agent implementation of the new shell + pluggable-agent
architecture. The ``VitaAgent`` class is exported so
``app.agents.registry.discover_agents()`` can find and register it at
startup.
"""
from .agent import VitaAgent

__all__ = ["VitaAgent"]
