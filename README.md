# MXL Merge Tool

Инструмент сравнения и трёхстороннего слияния табличных документов 1С в
формате `.mxl` (`MOXCEL`). Подключается к Git как diff- и merge-драйвер,
автоматически объединяет независимые изменения и открывает локальный редактор
для конфликтов.

Платформа 1С не требуется для diff и слияния. Она используется только для
точного HTML-предпросмотра документов.

![Разрешение конфликта MXL в визуальном редакторе](assets/mxl-merge-ui.png)

## Возможности

- текстовый diff для бинарных `.mxl`;
- автоматическое трёхстороннее слияние Base, Local и Remote;
- разрешение конфликтов ячеек и строк в браузере;
- обработка добавления, удаления и перестановки строк;
- выбор Base, Local, Remote или ручного значения;
- переопределение автоматически выбранных изменений;
- одновременный предпросмотр источников и результата;
- координаты ячеек, навигация по конфликтам и Undo/Redo;
- проверка структуры перед записью результата;
- интеграция с Git, Git Extensions и другими Git-клиентами.

## Ограничения

- Поэлементное структурное слияние поддерживается для строк. Изменения колонок и
  нераспознанная структура разрешаются выбором целого Base, Local или Remote.
- Комбинация решений по строкам должна соответствовать одной из целостных
  структур исходных документов. Несовместимый вариант нельзя сохранить.
- Удаление строки, изменённой в другой ветке, всегда требует явного решения.
- Подсветка в HTML сопоставляется по видимым значениям. Пустые, скрытые или
  повторяющиеся поля могут отображаться только в панели решений.

## Требования

- Git;
- Python 3.10 или новее;
- современный браузер;
- Windows и 1С:Предприятие — только для точного HTML-предпросмотра.

## Установка

Из корня Git-репозитория:

```bash
python3 mxl_tool.py install
```

На Windows:

```bat
python mxl_tool.py install
```

Команда настраивает локальные diff-, merge- и mergetool-драйверы и добавляет в
`.gitattributes`:

```gitattributes
*.mxl -text diff=mxl merge=mxl
```

Добавьте `.gitattributes` в репозиторий. Сам инструмент каждый разработчик
устанавливает локально один раз.

Проверка:

```bash
git check-attr diff merge -- path/to/template.mxl
```

Ожидаются атрибуты `diff: mxl` и `merge: mxl`.

### Глобальная установка

```bash
python3 /absolute/path/to/mxl_tool.py install --global
```

На Windows:

```bat
python C:\mxl_tool.py install --global
```

Git сохраняет абсолютный путь к `mxl_tool.py`, поэтому после установки каталог
инструмента нельзя перемещать.

## Предпросмотр через 1С

Передайте установщику путь к тонкому клиенту:

```bat
python mxl_tool.py install ^
  --onec-client "C:\Program Files\1cv8\8.3.27.2074\bin\1cv8c.exe"
```

`1cv8.exe` должен находиться рядом с `1cv8c.exe`. При первом запуске инструмент
автоматически создаст служебную базу и подключит встроенную обработку. Base,
Local и Remote преобразуются в HTML пакетно за один запуск 1С.

Проверка конвертера:

```bat
python mxl_tool.py render-onec ^
  path\to\sample.mxl %TEMP%\sample-mxl.html
```

Свою базу или обработку можно задать параметрами `--onec-infobase`,
`--onec-epf` и `--onec-username`. Пароль передаётся через
`MXL_ONEC_PASSWORD`. Для собственного EPF с поддержкой пакетного манифеста
добавьте `--onec-batch-capable`.

## Использование

Git вызывает merge-драйвер автоматически:

```bash
git merge feature/my-branch
```

Если файл остался конфликтным, откройте редактор:

```bash
git mergetool --tool=mxl path/to/template.mxl
```

Локальная сессия завершится после `Save` или `Cancel`.

### Источники

- `Base` — общий предок веток;
- `Local` — текущая ветка;
- `Remote` — вливаемая ветка;
- `Merged result` — итоговый документ.

Клик по изменённой ячейке или строке открывает выбор источника. Односторонние
изменения выбираются автоматически, но их можно переопределить.

- `Use Base/Local/Remote` использует соответствующий исходный документ целиком.
- `Resolve pending` выбирает сторону только для конфликтов без решения.
- `All local/remote` заменяет все решения выбранной стороной.
- `Render exact` создаёт промежуточный MXL и обновляет его предпросмотр через 1С.
- `Undo` и `Redo` работают также через `Ctrl/Cmd+Z`,
  `Ctrl/Cmd+Shift+Z` и `Ctrl/Cmd+Y`.
- `Save` проверяет и записывает результат; `Cancel` ничего не записывает.

## Git Extensions

После установки Git Extensions использует настройки из Git config. Для
конфликтного `.mxl` запустите mergetool и выберите `mxl`, если клиент запросит
инструмент. После `Save` файл будет отмечен как разрешённый.

Для диагностики используйте ту же команду в терминале:

```bash
git mergetool --tool=mxl relative/path/to/file.mxl
```

## Ручной запуск

Редактор можно открыть без Git-конфликта:

```bash
python3 mxl_tool.py ui \
  base.mxl local.mxl remote.mxl \
  --output merged.mxl
```

На Windows:

```bat
python mxl_tool.py ui ^
  C:\Temp\base.mxl C:\Temp\local.mxl C:\Temp\remote.mxl ^
  --output C:\Temp\merged.mxl
```

`--no-browser` выводит URL сессии без автоматического открытия браузера.

## Команды

Проверка структуры:

```bash
python3 mxl_tool.py validate file.mxl
```

Текстовое представление для diff:

```bash
python3 mxl_tool.py textconv file.mxl
```

Merge с явными файлами и отчётом:

```bash
python3 mxl_tool.py merge \
  base.mxl local.mxl remote.mxl \
  --output merged.mxl \
  --report conflict.json
```

## Свой HTML-конвертер

Вместо 1С можно подключить доверенный конвертер:

```bash
git config --local mxl.previewCommand \
  'path/to/converter {input} {output}'
```

Команда должна принять входной MXL, записать самодостаточный HTML и завершиться
после полной записи файла. Она запускается без shell.

Пакетный вариант:

```bash
git config --local mxl.previewBatchCommand \
  'path/to/batch-converter {manifest}'
```

## Диагностика

Проверка настроек Git:

```bash
git check-attr diff merge -- path/to/file.mxl
git config --get merge.mxl.driver
git config --get mergetool.mxl.cmd
```

Проверка настроек предпросмотра:

```bash
git config --get mxl.onecClient
git config --get mxl.previewCommand
git config --get mxl.previewBatchCommand
```

Если Git-репозиторий не найден, проверьте текущий каталог:

```bash
git rev-parse --show-toplevel
```

Ошибки HTML-конвертера не блокируют семантический diff и разрешение конфликтов.
Отчёты merge-драйвера находятся в `.git/mxl-merge/reports`.

## Тесты

```bash
python3 -m unittest discover -s tests -v
```

Тесты также выполняются в CI на Linux и Windows.

## Безопасность

- локальный сервер слушает только loopback-интерфейс и использует случайный
  токен сессии;
- предпросмотры не загружают внешние сетевые ресурсы;
- исходные файлы не меняются до `Save`;
- результат проверяется и записывается атомарно;
- неоднозначные структурные решения не сохраняются.
