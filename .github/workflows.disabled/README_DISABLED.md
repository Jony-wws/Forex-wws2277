# ⏸ ВСЕ GITHUB ACTIONS ОТКЛЮЧЕНЫ (11.06.2026)

По запросу JONY все 29 автоматических задач (workflows) остановлены:
папка `.github/workflows/` переименована в `.github/workflows.disabled/`,
поэтому GitHub их больше не видит и НЕ запускает — ни по расписанию,
ни по push.

## Как включить обратно (всё сразу)
Переименовать папку назад:
```
git mv .github/workflows.disabled .github/workflows
git commit -m "re-enable workflows"
git push
```

## Как включить только один workflow
Переместить нужный файл:
```
git mv .github/workflows.disabled/имя_файла.yml .github/workflows/имя_файла.yml
```

⚠️ Сайт forex-wws2277.fly.dev остаётся в сети, но данные на нём
перестали обновляться с момента отключения. Telegram-бот FOREX AI 2026
не зависит от этих Actions и продолжает работать.

— Viktor AI (viktor.com)
