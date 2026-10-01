#!/bin/bash
# Required parameters:
# @raycast.schemaVersion 1
# @raycast.title Спросить наставника
# @raycast.mode fullOutput

# Optional parameters:
# @raycast.icon 🎓
# @raycast.argument1 { "type": "text", "placeholder": "Вопрос" }
# @raycast.packageName ДедЛайнер

# Documentation:
# @raycast.description Задать вопрос личному наставнику anti_laziness_bot прямо с Mac, без Telegram
# @raycast.author ilnursafin

cd "$(dirname "$(readlink -f "$0")")/../.." || exit 1
venv/bin/python3 -c "
import asyncio
from mentor_qa import ask_mentor
import sys, re

text = asyncio.run(ask_mentor(sys.argv[1]))
# Raycast fullOutput — обычный текст, без HTML-тегов
print(re.sub(r'</?[bi]>', '', text))
" "$1"
