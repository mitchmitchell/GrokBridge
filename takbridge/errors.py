"""Errors that reach a bot as {"ok": false, "error": ...}."""


class ToolError(Exception):
    """A refused request. The message is shown to the bot as is."""
