# ComponentPress

Настольный редактор печатных прототипов настольных игр: визуальные макеты и YAML, данные Excel, изображения компонентов, PDF с линиями реза и архивные версии проекта.

## Техническая основа

Python 3.14, PySide6, openpyxl, ruamel.yaml, Pydantic и Jinja2. Подстановки `{Столбец}` и `{{ vars["ключ"] }}` разбираются с ограниченной грамматикой и разрешаются через Jinja2. Для иконок в тексте Excel согласован словарь `icons` в `project.yaml` и ярлыки `ic_...`. Приложение состоит из модулей с общими моделями и сценариями. Ограниченный пул потоков создаёт PNG, затем одно рабочее задание формирует PDF. Сборка фиксирует модели, значения Excel и используемые ресурсы; временные данные очищаются после завершения задач, а остатки после сбоя проверяются при следующем открытии проекта.

Первый прототип собирается и проверяется на **Windows 10 x64 (1809+)**. Архитектура предусматривает дальнейшие отдельные дистрибутивы для **Windows, Linux и macOS** на общей кодовой базе.

## Состояние

Этапы 1–12 реализованы. Проект и компоненты используют общий `schema_version: 5`, заданный `SCHEMA_VERSION`; предыдущие версии отклоняются без миграции и изменения файлов. Условные группы управляют видимостью элементов по точному значению Excel, редактор поддерживает прямоугольники, эллипсы и линии, PNG ZIP доступен из меню экспорта. Этап 11 добавил асинхронный детализированный предпросмотр, оценку эффективного dpi и lossless-вывод растров в PDF. Текущая версия — `0.12`; полный набор: 187 passed, 1 skipped; frozen smoke прошёл на Windows 10 x64.

## Запуск реализованных этапов

На Windows из PowerShell в корне репозитория:

```powershell
.\project.cmd
.\project.cmd cli new .\my-game --name "Моя игра"
.\project.cmd cli add-component .\my-game card "Карта"
.\project.cmd cli validate .\my-game
.\project.cmd cli set-name .\my-game "Новое название"
.\project.cmd cli set-size .\my-game card 70 90
.\project.cmd cli import-image .\my-game C:\path\to\image.png --into animals/enemy
.\project.cmd cli render .\examples\demo-game --component forest-card --output .\demo-card.png --pdf .\demo-card.pdf
.\project.cmd cli render .\examples\demo-game --component forest-card --output .\demo-card-150.png --dpi 150
.\project.cmd cli render .\examples\demo-excel-game --component forest-card --row 2 --output .\demo-forest.png
.\project.cmd cli render .\examples\demo-excel-game --component event-card --row 2 --output .\demo-event.png
.\project.cmd cli build .\examples\demo-excel-game
.\project.cmd cli build .\examples\demo-excel-game --component forest-card --workers 4
.\project.cmd cli build .\examples\demo-excel-game --mode test
.\project.cmd cli render .\examples\demo-excel-game --component forest-card --row 3 --mode test --output .\demo-forest-test.png
```

При первом вызове Windows-команда создаёт `.venv` и устанавливает зависимости из lock-файла. Доступны `.\project.cmd setup`, `.\project.cmd test` и `.\project.cmd release`. Сборка релиза работает на Windows, использует версию из `packaging/common.py` и не перезаписывает существующий выпуск. После изменения зависимостей повторите `setup`.

На macOS и Linux с установленным Python 3.14 используйте Makefile:

```sh
make run
make cli ARGS="validate examples/demo-game"
make test
```

Makefile сам создаёт `.venv` и устанавливает зависимости проекта. Выпуски для macOS и Linux пока не собираются; для них потребуются отдельная упаковка и проверка на целевых системах.

Запуск без аргументов открывает настольное окно. В каждой вкладке можно переключаться между «Макет» и «Текст». Проект задаёт один XLSX и имена столбцов Prod/Test, а компонент выбирает лист. Панель данных показывает проверенные строки; команды «Валидировать» и «Валидировать всё» проверяют все используемые значения и ресурсы, включая строки с нулевым количеством. Общий переключатель Prod/Test управляет превью, экспортом PNG и сборкой. PNG ZIP содержит manifest.json и один PNG на экземпляр. Несохранённые изменения открытого Excel недоступны. Openpyxl не вычисляет формулы: книгу нужно пересчитать и сохранить в Excel или совместимом редакторе. Пример `examples/demo-excel-game/` содержит два листа и воспроизводится командой `.\.venv\Scripts\python.exe .\examples\generate_demo_xlsx.py`.

Корректный YAML обновляет макет и дерево, визуальные команды обновляют YAML с сохранением комментариев, а ошибочный черновик блокирует сохранение и сборку. Меню «Сборка» позволяет собрать весь проект или активный компонент, изменить бумагу, ориентацию, поля, зазор, DPI, толщину линий реза и число потоков, отменить работу и открыть результат. CLI публикует результат в `output/build-<дата>-<job>/`; завершённая папка содержит `images/`, `print.pdf` и `build-report.json`. При 300 DPI компонент 63 × 88 мм создаёт PNG 744 × 1039 пикселей. Проверки: `.\project.cmd test` на Windows или `make test` на macOS/Linux.

## Выпуск этапа 7

`releases/0.07/ComponentPress-0.07-win-x64.exe` — один GUI-файл размером 53 824 826 байт для Windows 10 x64, запускаемый без Python, Excel и сопутствующих файлов. SHA-256: `6dd53de866f6ed76c52dfae5b61dff169de224fe1320e675d9a1e97e84603da9`. Встроенный frozen-smoke прошёл редактор, Excel, потоковую PDF-сборку, отмену и очистку; обычный запуск без аргументов оставался активным 10 секунд. Исполняемый файл пока не подписан Authenticode, поэтому Windows может показать предупреждение SmartScreen.

## Выпуск этапа 8

`releases/0.08/ComponentPress-0.08-win-x64.exe` — один GUI-файл размером 54 449 633 байта для Windows 10 x64. SHA-256: `99193d0a81f3f73f3ed5a37b19aad4772bd59455fdb572d94ef4abeddef88d58`. Frozen smoke проверил GUI, Excel, редактор, Prod/Test превью, PNG/PDF-сборку, отмену и очистку. Версии PE: 0.08; файл не подписан Authenticode.

## Выпуск этапа 9

`releases/0.09/ComponentPress-0.09-win-x64.exe` — один GUI-файл размером 54 462 443 байта для Windows 10 x64. SHA-256: `5370636085e35f22fe04d616d2ed5113f9fc44b094d2442c7f074408ad7641a9`. Frozen smoke проверил Prod/Test-сборки, Test PNG ZIP, Excel, редактор и очистку. Версии PE: 0.09; файл не подписан Authenticode.

## Выпуск этапа 10

`releases/0.10/ComponentPress-0.10-win-x64.exe` — один GUI-файл размером 54 475 430 байт для Windows 10 x64. SHA-256: `294b1e944873d56c2f15ff35f584d823ffec37fc97688fa50449dc506f92fc2a`. Frozen smoke прошёл; версии PE: 0.10. Полный набор проверок: 158 passed, 1 skipped. Файл не подписан Authenticode.

## Выпуск этапа 11

`releases/0.11/ComponentPress-0.11-win-x64.exe` — один GUI-файл размером 54 500 189 байт для Windows 10 x64 22H2. SHA-256: `537f17951b7354e8552b47080a43c3b7802421e610b58a1c599f69932885a67d`. Frozen smoke прошёл; версии PE: 0.11. Полный набор проверок: 166 passed, 1 skipped. Подробности изменений находятся в `releases/0.11/CHANGES.txt`. Файл не подписан Authenticode.

## Выпуск этапа 12

`releases/0.12/ComponentPress-0.12-win-x64.exe` — автономный GUI-файл для Windows 10 x64. SHA-256: `99a0960f8cbb7ceb71c4a29b78c30356cad3cc668edd4fb4478ca3315b338aec`. Frozen smoke проверил schema 5, Excel, фигуры, YAML, PDF и PNG ZIP; полный набор: 187 passed, 1 skipped. Подробности изменений находятся в `releases/0.12/CHANGES.txt`.

Для сборки следующего этапа обновите версию в `packaging/common.py` и Windows-метаданных, затем выполните `.\project.cmd release`. Скрипт не перезаписывает существующий выпуск и публикует EXE только после встроенной smoke-проверки.
