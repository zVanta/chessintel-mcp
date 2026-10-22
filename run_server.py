"""Entry point for both the compiled .exe and `python run_server.py`.

Transport comes from CHESSINTEL_TRANSPORT (default: stdio, which is what
LibreChat uses for command-style MCP servers).

    python run_server.py                          # stdio
    CHESSINTEL_TRANSPORT=http python run_server.py  # streamable HTTP on /mcp
"""
from chessintel.server import main

if __name__ == "__main__":
    main()
