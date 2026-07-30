"""CodeAct 沙盒内客户端模板目录。

``starring_tools.py`` 是注入沙盒的独立模块（仅标准库），不参与宿主侧 import，
由 ``CodeActMiddleware`` 读取源码后上传到沙盒 workspace/.codeact/ 下。
"""

from pathlib import Path

_TEMPLATE_PATH = Path(__file__).parent / "starring_tools.py"


def load_client_template() -> str:
    """读取沙盒客户端模块源码。"""
    return _TEMPLATE_PATH.read_text(encoding="utf-8")
