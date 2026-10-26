"""Create a self-contained three-sheet load-test project."""
from __future__ import annotations

import math
from pathlib import Path

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPolygonF


ROOT = Path(__file__).resolve().parent
SHEETS = ("ComponentOne", "ComponentTwo", "ComponentThree")
TEXTS = (
    "Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore et dolore magna aliqua.",
    "Sed ut perspiciatis unde omnis iste natus error sit voluptatem accusantium doloremque laudantium, totam rem aperiam.",
    "At vero eos et accusamus et iusto odio dignissimos ducimus qui blanditiis praesentium voluptatum deleniti atque corrupti.",
    "Ut enim ad minima veniam, quis nostrum exercitationem ullam corporis suscipit laboriosam, nisi ut aliquid ex ea commodi consequatur.",
    "Nam libero tempore, cum soluta nobis est eligendi optio cumque nihil impedit quo minus id quod maxime placeat facere possimus.",
)
PALETTES = (
    ("#214E78", "#D7ECFF", "#E8AC43"),
    ("#426A43", "#E6F3D8", "#C96555"),
    ("#70466E", "#F2DDF1", "#D79848"),
)


def image_for(index: int, palette: tuple[str, str, str]) -> QImage:
    image = QImage(384, 384, QImage.Format.Format_RGB32)
    image.fill(QColor(palette[1]))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(palette[0]))
    center = 192 + (index % 5 - 2) * 13
    radius = 78 + (index % 4) * 11
    if index % 3 == 0:
        painter.drawEllipse(QPointF(center, center), radius, radius)
    elif index % 3 == 1:
        sides = 5 + index % 4
        points = [QPointF(center + radius * math.cos(2 * math.pi * n / sides), center + radius * math.sin(2 * math.pi * n / sides)) for n in range(sides)]
        painter.drawPolygon(QPolygonF(points))
    else:
        painter.drawRoundedRect(65 + index % 6 * 9, 65, 250, 250, 30 + index % 5 * 5, 30 + index % 5 * 5)
    painter.setBrush(QColor(palette[2]))
    painter.drawEllipse(QPointF(192, 192), 28 + index % 4 * 8, 28 + index % 4 * 8)
    painter.end()
    return image


def main() -> None:
    (ROOT / "data").mkdir(parents=True, exist_ok=True)
    (ROOT / "components").mkdir(parents=True, exist_ok=True)
    (ROOT / "assets" / "images").mkdir(parents=True, exist_ok=True)
    for sheet_index, sheet in enumerate(SHEETS):
        for column in range(2):
            for row in range(25):
                image = image_for(row + column * 11, PALETTES[sheet_index])
                path = ROOT / "assets" / "images" / f"set-{sheet_index + 1}_field-{column + 1}_row-{row + 1:02}.png"
                if not image.save(str(path), "PNG"):
                    raise RuntimeError(f"Could not save image in {path}")

        safe = sheet.lower()
        color, pale, accent = PALETTES[sheet_index]
        component = f'''schema_version: 9
id: {safe}
name: "{sheet}"
size_mm:
  width: 63.0
  height: 88.0
background: "#FFFFFF"
data:
  sheet: "{sheet}"
elements:
- id: illustration-a
  name: Image 1
  type: image
  x_mm: 5.0
  y_mm: 5.0
  width_mm: 25.0
  height_mm: 22.0
  source: {{ mode: column, column: "Image 1" }}
  fit: cover
  locked: false
  transform: {{ m11: 1.0, m12: 0.0, m21: 0.0, m22: 1.0 }}
  optional: false
- id: illustration-b
  name: Image 2
  type: image
  x_mm: 33.0
  y_mm: 5.0
  width_mm: 25.0
  height_mm: 22.0
  source: {{ mode: column, column: "Image 2" }}
  fit: cover
  locked: false
  transform: {{ m11: 1.0, m12: 0.0, m21: 0.0, m22: 1.0 }}
  optional: false
- id: text-title
  name: Title
  type: html
  x_mm: 5.0
  y_mm: 31.0
  width_mm: 53.0
  height_mm: 14.0
  font_family: Arial
  font_size_pt: 7.0
  color: "{color}"
  content: {{ mode: column, column: Title }}
  locked: false
  text_align: center
  transform: {{ m11: 1.0, m12: 0.0, m21: 0.0, m22: 1.0 }}
- id: text-subtitle
  name: Subtitle
  type: html
  x_mm: 5.0
  y_mm: 48.0
  width_mm: 53.0
  height_mm: 13.0
  font_family: Arial
  font_size_pt: 6.0
  color: "{accent}"
  content: {{ mode: column, column: Subtitle }}
  locked: false
  text_align: center
  transform: {{ m11: 1.0, m12: 0.0, m21: 0.0, m22: 1.0 }}
- id: text-body
  name: Body
  type: html
  x_mm: 6.0
  y_mm: 64.0
  width_mm: 51.0
  height_mm: 20.0
  font_family: Arial
  font_size_pt: 5.5
  color: "#222222"
  content: {{ mode: column, column: Body }}
  locked: false
  text_align: left
  transform: {{ m11: 1.0, m12: 0.0, m21: 0.0, m22: 1.0 }}
'''
        (ROOT / "components" / f"{safe}.yaml").write_text(component, encoding="utf-8")

    refs = "\n".join(f"- id: {sheet.lower()}\n  path: components/{sheet.lower()}.yaml" for sheet in SHEETS)
    image_names = sorted(path.name for path in (ROOT / "assets" / "images").glob("*.png"))
    images = "\n".join(f"- assets/images/{name}" for name in image_names)
    project = f'''schema_version: 9
id: lorem-ipsum-load-test
name: "Lorem Ipsum load test"
version: "0.1.0"
variables: {{}}
images:
{images}
icons: {{}}
data_source:
  path: data/load-test.xlsx
copies_columns:
  prod: Prod
  test: Test
components:
{refs}
print:
  paper: A4
  orientation: portrait
  margin_mm: 5.0
  gap_mm: 3.0
  dpi: 300
  cut_lines: true
  cut_line_width_mm: 0.2
'''
    (ROOT / "project.cpress").write_text(project, encoding="utf-8")
    source = (
        "Lorem Ipsum test data uses the standard Lorem Ipsum passage and sections 1.10.32–1.10.33.\n"
        "Source: https://www.lipsum.com/\n"
    )
    (ROOT / "SOURCE.txt").write_text(source, encoding="utf-8")


if __name__ == "__main__":
    main()
