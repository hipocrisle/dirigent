"""Brain (планировщик) — сменный. StaticBrain = прозрачный план из YAML (MVP).

LLM-планировщик подключается через ту же функцию plan(); brain выбирается env
DIRIGENT_BRAIN (AD-2). Ядро от brain не зависит.
"""
from __future__ import annotations

import os
import subprocess
from typing import Protocol

import yaml

from .models import OrderSpec


class Brain(Protocol):
    def plan(self, intent_text: str, repo: str | None = None) -> OrderSpec: ...


class StaticBrain:
    """intent_text — путь к YAML-файлу заказа или сам YAML."""

    def plan(self, intent_text: str, repo: str | None = None) -> OrderSpec:
        text = intent_text
        if os.path.exists(intent_text):
            text = open(intent_text, encoding="utf-8").read()
        data = yaml.safe_load(text)
        spec = OrderSpec.model_validate(data)
        if repo and not spec.repo:
            spec = spec.model_copy(update={"repo": repo})
        return spec


class KimiBrain:
    """Свободная формулировка -> YAML заказа через `kimi -p`.

    Временно (до 12.12); заменяется на codex/deepseek без изменения ядра.
    Осторожно: вывод LLM недоверенный — валидируем схемой, при провале — эскалация.
    """

    PROMPT = (
        "Ты — планировщик оркестратора «Дирижёр». Преобразуй задачу пользователя в YAML "
        "по строгой схеме: поля goal (строка), steps (список). Каждый шаг: id, type "
        "(codex|opencode), prompt (что сделать), depends_on (список id), retry "
        "{max_attempts: 2}, acceptance {checks:[...]}. Разбивай на маленькие шаги с "
        "machine-проверяемым результатом. Ответь ТОЛЬКО YAML без пояснений."
    )

    def __init__(self, cli: str = "kimi"):
        self.cli = cli

    def plan(self, intent_text: str, repo: str | None = None) -> OrderSpec:
        p = subprocess.run(
            [self.cli, "-p", f"{self.PROMPT}\n\nЗАДАЧА:\n{intent_text}"],
            capture_output=True, text=True, timeout=600)
        out = p.stdout
        # вырезаем fenced block если есть
        if "```" in out:
            out = out.split("```")[1]
            if out.startswith("yaml"):
                out = out[4:]
        spec = OrderSpec.model_validate(yaml.safe_load(out))
        if repo and not spec.repo:
            spec = spec.model_copy(update={"repo": repo})
        return spec


def get_brain() -> Brain:
    which = os.environ.get("DIRIGENT_BRAIN", "static")
    if which == "cli":
        from .planning import CLIBrain
        return CLIBrain()
    if which == "kimi":
        return KimiBrain()
    return StaticBrain()
