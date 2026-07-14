from dotenv import load_dotenv

load_dotenv(".env", override=True)

from concurrent.futures import ThreadPoolExecutor  # noqa: E402
from importlib import import_module  # noqa: E402

from starring.config import config as config  # noqa: E402

try:
    from importlib.metadata import version

    __version__ = version("starring")
except Exception:
    __version__ = "unknown"

executor = ThreadPoolExecutor()  # noqa: E402


def get_version():
    """Return the starring version."""
    return __version__


def __getattr__(name: str):
    if name == "knowledge_base":
        knowledge = import_module("starring.knowledge")
        return getattr(knowledge, name)
    raise AttributeError(f"module 'starring' has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | {"knowledge_base"})
