#!/usr/bin/env python3
"""中台启动入口：python run_webapp.py [--port 8765] [--open]"""
import sys

from webapp.server import main

if __name__ == "__main__":
    sys.exit(main() or 0)
