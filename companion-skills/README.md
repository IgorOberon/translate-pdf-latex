# 27 дополнительных скиллов

Это **сами файлы коллекции**, а не только каталог: 27 папок, в каждой — `SKILL.md`
и необходимые для неё `scripts/`, `references/` или `assets/`.
Основной конвейер `translate-pdf-latex` находится отдельно, в корне репозитория.
Вместе с ним получается 28 уникальных скиллов. Для обычного перевода локальной
моделью достаточно основного; устанавливать всю коллекцию не требуется.
В beta.5 обновлены `local-mt-batch`, `local-llm-lmstudio`, `latex-build-doctor`,
`translation-glossary-builder`; добавлен `translation-pipeline-diagnostics`.
Эти пять инструкций также входят в основной ZIP. Дубликаты не устанавливайте.

## Скачать и установить

1. В [Releases](https://github.com/IgorOberon/translate-pdf-latex/releases/tag/v0.1.0-beta.5)
   скачайте `companion-skills.zip` и его `.sha256`. Основной конвейер скачивается
   отдельно как `translate-pdf-latex.zip`.
2. Проверьте SHA-256 архива (`Get-FileHash .\companion-skills.zip -Algorithm SHA256`)
   и распакуйте в новую папку. Получится `companion-skills/<имя-скилла>/SKILL.md`.
3. Перенесите **выбранные папки целиком**, не только `SKILL.md`, в каталог скиллов
   своего клиента — тот же, где установлен основной скилл. Не переносите общую
   папку `companion-skills` как один скилл: собственного `SKILL.md` у неё нет.
   Примеры расположений клиентов — в
   [основной инструкции установки](https://github.com/IgorOberon/translate-pdf-latex/blob/main/УСТАНОВКА.md).
   Существующие одноимённые скиллы сначала сохраните в резервную копию.
4. Обновите список скиллов/перезапустите клиента. Если автоматического обнаружения
   нет, прямо укажите агенту путь к нужному `SKILL.md` и поручите прочитать его.

Модели, Python, OCR, TeX, CUDA и веса в ZIP не входят. Зависимости устанавливаются
для выбранной задачи, а не все разом. Упоминания Claude Code и Windows в отдельных
инструкциях — исходная среда, не требование платной подписки: команды и инструменты
нужно сопоставить с возможностями вашего агента. Не имитируйте отсутствующие API.

## Состав

| Скилл | Назначение |
|---|---|
| [translate-book-to-latex](translate-book-to-latex/SKILL.md) | Методика перевода и перевёрстки книги |
| [multiagent-latex-production](multiagent-latex-production/SKILL.md) | Координация большой работы нескольких агентов |
| [pdf-book-extraction](pdf-book-extraction/SKILL.md) | Извлечение текста, формул и рисунков |
| [pdf-text-layer-trap](pdf-text-layer-trap/SKILL.md) | Скрытые потери в текстовом слое PDF |
| [local-mt-batch](local-mt-batch/SKILL.md) | Локальный пакетный перевод |
| [translation-pipeline-diagnostics](translation-pipeline-diagnostics/SKILL.md) | Прогресс, повторные запросы, сбои и безопасное возобновление |
| [local-llm-lmstudio](local-llm-lmstudio/SKILL.md) | LM Studio и память GPU |
| [windows-python-env](windows-python-env/SKILL.md) | Python/venv, PATH и кодировки Windows |
| [translation-glossary-builder](translation-glossary-builder/SKILL.md) | Подготовка и проверка глоссария |
| [translation-consistency-audit](translation-consistency-audit/SKILL.md) | Единообразие терминов и имён |
| [translation-literary-edit-pass](translation-literary-edit-pass/SKILL.md) | Необязательная стилистическая редактура |
| [scholarly-translation-apparatus](scholarly-translation-apparatus/SKILL.md) | Цитаты и примечания научного издания |
| [extract-pdf-figures](extract-pdf-figures/SKILL.md) | Точная вырезка рисунков |
| [hybrid-figure-triage](hybrid-figure-triage/SKILL.md) | Растр, перерисовка или текстовая плашка |
| [figure-label-translation](figure-label-translation/SKILL.md) | Перевод надписей внутри рисунков |
| [tikz-from-reference-image](tikz-from-reference-image/SKILL.md) | Перерисовка в TikZ |
| [latex-figure-review](latex-figure-review/SKILL.md) | Визуальная проверка рисунков |
| [latex-build-doctor](latex-build-doctor/SKILL.md) | Ошибки сборки LaTeX |
| [latex-pdf-font-audit](latex-pdf-font-audit/SKILL.md) | Векторные и встроенные шрифты |
| [latex-running-heads-overfull](latex-running-heads-overfull/SKILL.md) | Колонтитулы и выход за поля |
| [latex-standalone-build-audit](latex-standalone-build-audit/SKILL.md) | Самостоятельная сборка частей |
| [latex-typography-normalizer](latex-typography-normalizer/SKILL.md) | Контролируемая нормализация типографики |
| [latex-audit-orchestration](latex-audit-orchestration/SKILL.md) | Глубокий аудит LaTeX-проекта |
| [latex-book-index](latex-book-index/SKILL.md) | Именной и предметный указатели |
| [markdown-to-pdf-multiscript](markdown-to-pdf-multiscript/SKILL.md) | Многоязычный Markdown → PDF |
| [transcribe-seminar](transcribe-seminar/SKILL.md) | Распознавание аудио/видео семинара |
| [asr-archive-to-edited-docs](asr-archive-to-edited-docs/SKILL.md) | Редактура ASR-транскриптов |

## Как применять с конвейером

Недорогой или локальный управляющий агент запускает основной `pipeline.py`.
Дополнительные скиллы подключаются только по задаче: например,
`translation-glossary-builder` — при явно выбранной подготовке терминологии,
`latex-build-doctor` — при ошибке сборки. Обязательной полировки и облака нет.
Для глоссария прочитайте также
[практическую инструкцию](https://github.com/IgorOberon/translate-pdf-latex#глоссарий-подготовить-один-раз-использовать-во-всей-книге).

В коллекции есть **исторические автономные вспомогательные скрипты**. Они не
равноценны текущим оркестраторам и не содержат всех современных проверок.
Не заменяйте ими `scripts/` основного конвейера и не обходите его приёмку.
Для штатного OCR/MT используйте основной конвейер; вспомогательные скрипты сначала
проверяйте на отдельном тестовом входе. Все готовые PDF и исходники сохраняйте.
Время работы, версии и объём памяти в старых примерах — наблюдения, не гарантии.
Одна большая модель на GPU за раз; чужие процессы автоматически не завершать.

## Состав поставки и проверка

Историческая коллекция подготовлена из 26 исходных папок, без `evals`, инструментария
`skill-creator`, кэшей и книжных изображений. В генератор глоссария добавлен
недостающий локальный `mask_math.py`; текстовая инструкция о глоссарии уточнена
для нового диспетчера. Структура скиллов и синтаксис Python проверены; это не
полный функциональный тест всех экспертных сценариев. В beta.5 добавлена
одна папка диагностики; четыре обновлённые инструкции используют основной движок,
а сохранённые исторические скрипты не являются рекомендуемым обходным маршрутом.

`collection.manifest.json` содержит список скиллов и SHA-256 каждого файла.
Лицензия авторских инструкций и скриптов — [MIT](LICENSE). Лицензии используемых
моделей, OCR, библиотек, шрифтов и ваших документов остаются отдельными.
