"""Compatibility facade for the project round-trip YAML codec."""

from componentpress.domain.html_resources import html_image_sources
from .yaml_parser import parse_component, parse_project
from .yaml_read import _load
from .yaml_tree import dump_yaml, update_tree

__all__ = ["dump_yaml", "html_image_sources", "parse_component", "parse_project", "update_tree"]
