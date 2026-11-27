"""Unofficial Blinkit MCP server: drives blinkit.com in a local Chrome window with Playwright.

Blinkit has no official MCP server. This is a safety-focused rewrite of the approach in
https://github.com/hereisSwapnil/blinkit-mcp (MIT): stdio only (no network listener),
labelled read-only/destructive tools, an encrypted session file, no third-party IP
geolocation, no automatic payment-method choice, and a total-amount check before any
order is placed.
"""
