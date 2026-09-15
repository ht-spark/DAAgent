"""Agent 工具层共享的异常类型。"""


class DuplicateToolError(ValueError):
    """注册表中已经存在同名工具。"""


class ToolNotFoundError(LookupError):
    """注册表中不存在指定工具。"""


class ToolArgumentsError(ValueError):
    """工具收到不符合约定的调用参数。"""
