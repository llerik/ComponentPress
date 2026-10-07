"""Regenerate demo-game/data/game.xlsx with a real cached formula result."""

from datetime import date
from pathlib import Path
import re
from zipfile import ZIP_DEFLATED, ZipFile

from openpyxl import Workbook


ROOT = Path(__file__).resolve().parent / "demo-excel-game"
TARGET = ROOT / "data" / "game.xlsx"


def main() -> None:
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    book = Workbook()
    cards = book.active
    cards.title = "Карты"
    cards.append(["ID", "Название", "Описание", "Изображение", "Сила", "Количество", "Дата", "Флаг", "Prod", "Debug", "Заголовок HTML"])
    cards.append(["wolf", "Волк", r"Атака ic_leaf_6 и ic_leaf_3; буквально \ic_leaf_3", "assets/images/animals/enemy/leaf.png", "=3+4", 2, date(2026, 10, 3), True, 2, 1, "<p><b>Волк</b></p>"])
    cards.append(["fox", "Лиса", "Тихая карта ic_leaf_3", "assets/images/animals/enemy/leaf.png", 2.5, 1, None, False, 0, 3, "<p><b>Лиса</b></p>"])
    events = book.create_sheet("События")
    events.append(["ID", "Название", "Prod", "Debug", "Заголовок HTML"])
    events.append(["event-1", "Лесной дождь", 1, 0, "<p><b>Лесной дождь</b></p>"])
    events.append(["event-2", "Солнечная поляна", 1, 1, "<p><b>Солнечная поляна</b></p>"])
    book.save(TARGET)
    book.close()

    temporary = TARGET.with_suffix(".cached.xlsx")
    with ZipFile(TARGET) as source, ZipFile(temporary, "w", ZIP_DEFLATED) as destination:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                text = data.decode("utf-8")
                pattern = r'(<c[^>]*r="E2"[^>]*>.*?<f[^>]*>.*?</f>)(?:<v(?:>.*?</v>|\s*/>))?(</c>)'
                text, count = re.subn(pattern, r"\1<v>7</v>\2", text, count=1)
                if count != 1:
                    raise RuntimeError("formula E2 not found")
                data = text.encode("utf-8")
            destination.writestr(item, data)
    temporary.replace(TARGET)


if __name__ == "__main__":
    main()
