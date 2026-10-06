"""Qt-based component rendering shared by preview and export."""

from .exporter import RenderResult, render_component, render_component_image

__all__ = ["RenderResult", "render_component", "render_component_image"]
