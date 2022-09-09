"""Module for handling symbolic function registration."""

import functools
import warnings
from typing import (
    Callable,
    Collection,
    Dict,
    Generic,
    Optional,
    Sequence,
    Set,
    TypeVar,
    Union,
)

from torch.onnx import _constants, errors
from torch.onnx._internal import _beartype

OpsetVersion = int


def _dispatch_opset_version(
    target: OpsetVersion, available_opsets: Collection[OpsetVersion]
) -> Optional[OpsetVersion]:
    """Finds the registered opset given a target opset version and the available opsets.

    Args:
        target: The target opset version.
        available_opsets: The available opsets.

    Returns:
        The registered opset version.
    """
    if not available_opsets:
        return None
    available_versions = sorted(available_opsets)
    # Linear search for the opset version, which is fine since the number of opset
    # versions is small.

    # Always round toward opset 9 (ONNX_BASE_OPSET).
    # Count down until opset 9 is reached.
    for version in reversed(available_versions):
        if _constants.ONNX_BASE_OPSET <= version <= target:
            return version

    for version in available_versions:
        # Count back up until _constants.ONNX_BASE_OPSET
        if target <= version <= _constants.ONNX_BASE_OPSET:
            return version

    assert (
        not available_versions
        or _constants.ONNX_BASE_OPSET <= target < available_versions[0]
        or available_versions[-1] < _constants.ONNX_BASE_OPSET < target
    )
    return None


_K = TypeVar("_K")
_V = TypeVar("_V")


class OverrideDict(Generic[_K, _V], Collection[_K]):
    """A dictionary that merges built-in and custom symbolic functions.

    It supports overriding and un-overriding built-in symbolic functions with custom
    ones.
    """

    def __init__(self):
        self._base: Dict[_K, _V] = {}
        self._overrides: Dict[_K, _V] = {}
        self._merged: Dict[_K, _V] = {}

    def override(self, key: _K, value: _V) -> None:
        """Overrides a base key-value with a new pair."""
        self._overrides[key] = value
        self._merged[key] = value

    def remove_override(self, key: _K) -> None:
        """Un-overrides a key-value pair."""
        self._overrides.pop(key, None)  # type: ignore[arg-type]
        self._merged = {**self._base, **self._overrides}

    def overrides(self):
        """Returns the overridden keys."""
        return self._overrides.keys()

    def overridden(self, key: _K) -> bool:
        """Checks if a key-value pair is overridden."""
        return key in self._overrides

    def in_base(self, key: _K) -> bool:
        """Checks if a key is in the base dictionary."""
        return key in self._base

    def __getitem__(self, key: _K) -> _V:
        return self._merged[key]

    def __setitem__(self, key: _K, value: _V) -> None:
        self._base[key] = value
        self._merged = {**self._base, **self._overrides}

    def get(self, key: _K, default: Optional[_V] = None):
        return self._merged.get(key, default)

    def __contains__(self, key: object) -> bool:
        return key in self._merged

    def __iter__(self):
        return iter(self._merged)

    def __len__(self) -> int:
        return len(self._merged)

    def __repr__(self) -> str:
        return f"OverrideDict(base={self._base}, overrides={self._overrides})"

    def __bool__(self) -> bool:
        return bool(self._merged)


class _SymbolicFunctionGroup:
    """Different versions of symbolic functions registered to the same name.

    O(n) search is performed to find the most recent version of the op.
    The results are cached for faster lookup.

    The registration is delayed until op is used to improve startup time.

    Function overloads with different arguments are not allowed.
    Custom op overrides are supported.
    """

    def __init__(self, name: str) -> None:
        self._name = name
        # A dictionary of functions, keyed by the opset version.
        self._functions: OverrideDict[OpsetVersion, Callable] = OverrideDict()

    def __repr__(self) -> str:
        return f"_SymbolicFunctionGroup({self._name}, registered={self._functions})"

    def __getitem__(self, key: OpsetVersion) -> Callable:
        result = self.get(key)
        if result is None:
            raise KeyError(key)
        return result

    # NOTE: Remember to clear the cache when the _functions dictionary is updated.
    @functools.lru_cache(maxsize=None)
    def get(self, opset: OpsetVersion) -> Optional[Callable]:
        """Find the most recent version of the function."""
        version = _dispatch_opset_version(opset, self._functions)
        if version is None:
            return None

        return self._functions[version]

    def add(self, func: Callable, opset: OpsetVersion) -> None:
        """Adds a symbolic function.

        Args:
            func: The function to add.
            opset: The opset version of the function to add.
        """
        if self._functions.in_base(opset):
            warnings.warn(
                f"Symbolic function '{self._name}' already registered for opset {opset}. "
                f"Replacing the existing function with new function. This is unexpected. "
                f"Please report it on {_constants.PYTORCH_GITHUB_ISSUES_URL}.",
                errors.OnnxExporterWarning,
            )
        self._functions[opset] = func
        self.get.cache_clear()

    def add_custom(self, func: Callable, opset: OpsetVersion) -> None:
        """Adds a custom symbolic function.

        Args:
            func: The symbolic function to register.
            opset: The corresponding opset version.
        """
        self._functions.override(opset, func)
        self.get.cache_clear()

    def remove_custom(self, opset: OpsetVersion) -> None:
        """Removes a custom symbolic function.

        Args:
            opset: The opset version of the custom function to remove.
        """
        if not self._functions.overridden(opset):
            warnings.warn(
                f"No custom function registered for '{self._name}' opset {opset}"
            )
            return
        self._functions.remove_override(opset)
        self.get.cache_clear()

    def get_min_supported(self) -> OpsetVersion:
        """Returns the lowest built-in opset version supported by the function."""
        return min(self._functions)


class SymbolicRegistry:
    """Registry for symbolic functions.

    The registry maintains a mapping from qualified names to symbolic functions.
    It is used to register new symbolic functions and to dispatch calls to
    the appropriate function.
    """

    def __init__(self) -> None:
        self._registry: Dict[str, _SymbolicFunctionGroup] = {}

    def register(
        self, name: str, opset: OpsetVersion, func: Callable, custom=False
    ) -> None:
        """Registers a symbolic function.

        Args:
            name: the qualified name of the function to register.
            opset: the opset version of the function to register.
            func: the symbolic function to register.
            custom: whether the function is a custom function that overrides existing ones.
        """
        if "::" not in name:
            raise ValueError(
                f"The name must be in the form of 'domain::op', not '{name}'"
            )
        symbolic_functions = self._registry.setdefault(
            name, _SymbolicFunctionGroup(name)
        )
        if custom:
            symbolic_functions.add_custom(func, opset)
        else:
            symbolic_functions.add(func, opset)

    def unregister(self, name: str, opset: OpsetVersion) -> None:
        """Unregisters a symbolic function.

        Args:
            name: the qualified name of the function to unregister.
            opset: the opset version of the function to unregister.
        """
        if name not in self._registry:
            return
        self._registry[name].remove_custom(opset)

    def get_function_group(self, name: str) -> Optional[_SymbolicFunctionGroup]:
        """Returns the function group for the given name."""
        return self._registry.get(name)

    def is_registered_op(self, name: str, version: int) -> bool:
        """Returns whether the given op is registered for the given opset version."""
        functions = self.get_function_group(name)
        if functions is None:
            return False
        return functions.get(version) is not None

    def all_functions(self) -> Set[str]:
        """Returns the set of all registered function names."""
        return set(self._registry)


@_beartype.beartype
def onnx_symbolic(
    name: str,
    opset: Union[OpsetVersion, Sequence[OpsetVersion]],
    decorate: Optional[Sequence[Callable]] = None,
    custom: bool = False,
) -> Callable:
    """Registers a symbolic function.

    Usage::

    ```
    @onnx_symbolic("aten::symbolic_b", opset=10, decorate=[quantized_aten_handler(scale=1/128, zero_point=0)])
    @symbolic_helper.parse_args("v", "v", "b")
    def symbolic_b(g: _C.Graph, x: _C.Value, y: _C.Value, arg1: bool) -> _C.Value:
        ...
    ```

    Args:
        name: The qualified name of the function.
        opset: The opset versions of the function to register at.
        decorate: A sequence of decorators to apply to the function.
        custom: Whether the function is a custom symbolic function.
    """

    def wrapper(func: Callable) -> Callable:
        decorated = func
        if decorate is not None:
            for decorate_func in decorate:
                decorated = decorate_func(decorated)

        global registry
        nonlocal opset
        if isinstance(opset, OpsetVersion):
            opset = (opset,)
        for opset_version in opset:
            registry.register(name, opset_version, decorated, custom=custom)

        # Return the original function because the decorators in "decorate" are only
        # specific to the instance being registered.
        return func

    return wrapper


@_beartype.beartype
def custom_onnx_symbolic(
    name: str,
    opset: Union[OpsetVersion, Sequence[OpsetVersion]],
    decorate: Optional[Sequence[Callable]] = None,
) -> Callable:
    """Registers a custom symbolic function.

    Args:
        name: the qualified name of the function.
        opset: the opset version of the function.
        decorate: a sequence of decorators to apply to the function.

    Returns:
        The decorator.
    """
    return onnx_symbolic(name, opset, decorate, custom=True)


# The registry for all symbolic functions.
registry = SymbolicRegistry()