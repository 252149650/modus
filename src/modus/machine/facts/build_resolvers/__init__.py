"""Language-specific build-unit discovery strategies.

BuildContextResolver composes these strategies over one shared inventory.
Provider execution belongs to the compiler package, after discovery.
"""

from .cpp_resolver import CppResolverMixin
from .go_resolver import GoResolverMixin
from .java_resolver import JavaResolverMixin
from .python_resolver import PythonResolverMixin
from .typescript_resolver import TypeScriptResolverMixin

__all__ = [
    "CppResolverMixin",
    "GoResolverMixin",
    "JavaResolverMixin",
    "PythonResolverMixin",
    "TypeScriptResolverMixin",
]
