"""Qt-based component rendering shared by preview and export."""

__all__ = ["RenderResult", "render_component", "render_component_image"]


def __getattr__(name: str):
    if name == "RenderResult":
        from .exporter import RenderResult

        return RenderResult
    if name == "render_component":
        from .exporter import render_component

        return render_component
    if name == "render_component_image":
        from .exporter import render_component_image

        return render_component_image
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
