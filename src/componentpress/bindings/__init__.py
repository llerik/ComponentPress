"""Restricted project variable, Excel-column and icon bindings."""

__all__ = ["BindingResolver"]


def __getattr__(name: str):
    if name == "BindingResolver":
        from .resolver import BindingResolver

        return BindingResolver
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
