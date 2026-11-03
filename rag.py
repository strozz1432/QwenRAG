#!/usr/bin/env python3
"""Launcher: `uv run rag.py`. The code lives in the localdocs/ package."""
import asyncio

from localdocs.tui.app import main

if __name__ == "__main__":
    asyncio.run(main())
