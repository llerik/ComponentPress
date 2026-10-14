"""Affine geometry shared by the tree, canvas and renderers."""

from __future__ import annotations

from dataclasses import dataclass
import math

from .nodes import GroupFrame, GroupNode, LineNode, PositionedNode, ShapeNode, Transform


@dataclass(frozen=True)
class Affine:
    a: float = 1.0
    b: float = 0.0
    c: float = 0.0
    d: float = 1.0
    x: float = 0.0
    y: float = 0.0

    @classmethod
    def node(cls, node: PositionedNode) -> "Affine":
        t = node.transform
        return cls(t.m11, t.m12, t.m21, t.m22, node.x_mm, node.y_mm)

    def then(self, child: "Affine") -> "Affine":
        """Compose this parent transform with a transform in its local space."""
        return Affine(
            self.a * child.a + self.b * child.c,
            self.a * child.b + self.b * child.d,
            self.c * child.a + self.d * child.c,
            self.c * child.b + self.d * child.d,
            self.a * child.x + self.b * child.y + self.x,
            self.c * child.x + self.d * child.y + self.y,
        )

    def point(self, x: float, y: float) -> tuple[float, float]:
        return self.a * x + self.b * y + self.x, self.c * x + self.d * y + self.y

    def inverse(self) -> "Affine":
        determinant = self.a * self.d - self.b * self.c
        if determinant <= 0 or not math.isfinite(determinant):
            raise ValueError("transform is not invertible")
        a, b, c, d = self.d / determinant, -self.b / determinant, -self.c / determinant, self.a / determinant
        return Affine(a, b, c, d, -(a * self.x + b * self.y), -(c * self.x + d * self.y))

    def translated(self, dx: float, dy: float) -> "Affine":
        return Affine(self.a, self.b, self.c, self.d, self.x + dx, self.y + dy)

    def rotated_at(self, degrees: float, center_x: float, center_y: float) -> "Affine":
        radians = math.radians(degrees)
        cosine, sine = math.cos(radians), math.sin(radians)
        rotation = Affine(cosine, -sine, sine, cosine)
        around = Affine(1, 0, 0, 1, center_x, center_y).then(rotation).then(
            Affine(1, 0, 0, 1, -center_x, -center_y)
        )
        return around.then(self)

    def transform_model(self) -> Transform:
        return Transform(m11=self.a, m12=self.b, m21=self.c, m22=self.d)

    def bounds(self, width: float, height: float) -> tuple[float, float, float, float]:
        points = (self.point(0, 0), self.point(width, 0), self.point(0, height), self.point(width, height))
        xs, ys = [point[0] for point in points], [point[1] for point in points]
        return min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)


def local_bounds(node: PositionedNode) -> tuple[float, float, float, float]:
    if isinstance(node, LineNode):
        return min(0.0, node.dx_mm), min(0.0, node.dy_mm), abs(node.dx_mm), abs(node.dy_mm)
    if isinstance(node, GroupNode):
        frame = node.frame
        return frame.x_mm, frame.y_mm, frame.width_mm, frame.height_mm
    return 0.0, 0.0, node.width_mm, node.height_mm


def group_frame(children: tuple[PositionedNode, ...]) -> GroupFrame:
    if not children:
        return GroupFrame()
    bounds = []
    for child in children:
        x, y, width, height = local_bounds(child)
        child_to_parent = Affine.node(child)
        bounds.append(child_to_parent.bounds(width, height))
    left = min(item[0] for item in bounds)
    top = min(item[1] for item in bounds)
    right = max(item[0] + item[2] for item in bounds)
    bottom = max(item[1] + item[3] for item in bounds)
    width, height = right - left, bottom - top
    if width == 0:
        left -= 0.05
        width = 0.1
    if height == 0:
        top -= 0.05
        height = 0.1
    return GroupFrame(x_mm=left, y_mm=top, width_mm=width, height_mm=height)
