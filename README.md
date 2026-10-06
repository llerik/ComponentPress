# ComponentPress

Настольный редактор печатных прототипов настольных игр: визуальные макеты и YAML, данные Excel, изображения компонентов, PDF с линиями реза и архивные версии проекта.

## Техническая основа

Python 3.14, PySide6, openpyxl, ruamel.yaml, Pydantic и Jinja2. Подстановки `{Столбец}` и `{{ vars["ключ"] }}` разбираются с ограниченной грамматикой и разрешаются через Jinja2. Для иконок в тексте Excel согласован словарь `icons` в `project.yaml` и ярлыки `ic_...`. Приложение состоит из модулей с общими моделями и сценариями. Ограниченный пул потоков создаёт PNG, затем одно рабочее задание формирует PDF. Сборка фиксирует модели, значения Excel и используемые ресурсы; временные данные очищаются после завершения задач, а остатки после сбоя проверяются при следующем открытии проекта.

Первый прототип собирается и проверяется на **Windows 10 x64 (1809+)**. Архитектура предусматривает дальнейшие отдельные дистрибутивы для **Windows, Linux и macOS** на общей кодовой базе.

## Состояние

Этапы 1–7 реализованы: доступны формат 2 и миграция, файловый проект, статическая отрисовка, визуальный/YAML-редактор, просмотр экземпляров из XLSX и полная сборка тиража в PNG/PDF. Сборка сохраняет открытые корректные документы, фиксирует строки и только используемые ресурсы, создаёт по одному PNG на экземпляр в ограниченном пуле, затем последовательно формирует PDF с векторными линиями реза и атомарно публикует полный результат. Текущая версия разработки — `0.07`; архивные версии проекта относятся к этапу 8.

## Запуск реализованных этапов

В PowerShell из корня репозитория:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements/windows-py314.lock
.\.venv\Scripts\python.exe -m pip install --no-build-isolation --no-deps -e .
.\.venv\Scripts\python.exe -m componentpress
.\.venv\Scripts\python.exe -m componentpress new .\my-game --name "Моя игра"
.\.venv\Scripts\python.exe -m componentpress add-component .\my-game card "Карта"
.\.venv\Scripts\python.exe -m componentpress validate .\my-game
.\.venv\Scripts\python.exe -m componentpress migrate .\old-format-game
.\.venv\Scripts\python.exe -m componentpress set-name .\my-game "Новое название"
.\.venv\Scripts\python.exe -m componentpress set-size .\my-game card 70 90
.\.venv\Scripts\python.exe -m componentpress import-image .\my-game C:\path\to\image.png --into animals/enemy
.\.venv\Scripts\python.exe -m componentpress render .\examples\demo-game --component forest-card --output .\demo-card.png --pdf .\demo-card.pdf
.\.venv\Scripts\python.exe -m componentpress render .\examples\demo-game --component forest-card --output .\demo-card-150.png --dpi 150
.\.venv\Scripts\python.exe -m componentpress render .\examples\demo-excel-game --component forest-card --instance-id wolf --output .\demo-wolf.png
.\.venv\Scripts\python.exe -m componentpress render .\examples\demo-excel-game --component event-card --row 2 --output .\demo-event.png
.\.venv\Scripts\python.exe -m componentpress build .\examples\demo-excel-game
.\.venv\Scripts\python.exe -m componentpress build .\examples\demo-excel-game --component forest-card --workers 4
```

Запуск без аргументов открывает настольное окно. В каждой вкладке можно переключаться между «Макет» и «Текст». Для компонента с блоком `data` панель «Данные Excel» показывает цепочку `источник → лист → столбец`, ID, тираж и текущую строку. «Обновить» перечитывает сохранённый XLSX; несохранённые изменения открытого Excel недоступны. Openpyxl не вычисляет формулы: книгу нужно пересчитать и сохранить в Excel или совместимом редакторе. Пример `examples/demo-excel-game/` содержит два листа и воспроизводится командой `.\.venv\Scripts\python.exe .\examples\generate_demo_xlsx.py`.

Корректный YAML обновляет макет и дерево, визуальные команды обновляют YAML с сохранением комментариев, а ошибочный черновик блокирует сохранение и сборку. Меню «Сборка» позволяет собрать весь проект или активный компонент, изменить бумагу, ориентацию, поля, зазор, DPI, толщину линий реза и число потоков, отменить работу и открыть результат. CLI публикует результат в `output/build-<дата>-<job>/`; завершённая папка содержит `images/`, `print.pdf` и `build-report.json`. При 300 DPI компонент 63 × 88 мм создаёт PNG 744 × 1039 пикселей. Проверки: `.\.venv\Scripts\python.exe -m pytest -q`.

## Выпуск этапа 7

`releases/0.07/ComponentPress-0.07-win-x64.exe` — один GUI-файл размером 53 824 826 байт для Windows 10 x64, запускаемый без Python, Excel и сопутствующих файлов. SHA-256: `6dd53de866f6ed76c52dfae5b61dff169de224fe1320e675d9a1e97e84603da9`. Встроенный frozen-smoke прошёл редактор, Excel, потоковую PDF-сборку, отмену и очистку; обычный запуск без аргументов оставался активным 10 секунд. Исполняемый файл пока не подписан Authenticode, поэтому Windows может показать предупреждение SmartScreen.

Для сборки следующего этапа сначала обновляют версию в `packaging/common.py` и Windows-метаданных, затем выполняют `.\.venv\Scripts\python.exe .\packaging\build_release.py`. Скрипт не перезаписывает существующий выпуск и публикует EXE только после встроенной smoke-проверки.
