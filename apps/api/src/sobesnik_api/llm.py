"""Провайдеры LLM по ролям: gen — разбор и генерация, eval — оценка (002 §5.3)."""

import asyncio
import logging
from dataclasses import dataclass

from sobesnik_api.settings import ROLES, Role, Settings
from sobesnik_llm import LLMHealth, LLMProvider, make_provider

log = logging.getLogger(__name__)


@dataclass
class LLMRoles:
    gen: LLMProvider
    eval: LLMProvider

    def get(self, role: Role) -> LLMProvider:
        return self.gen if role == "gen" else self.eval

    async def health(self) -> dict[Role, LLMHealth]:
        if self.gen is self.eval:
            result = await self.gen.health()
            return dict.fromkeys(ROLES, result)
        gen, eval_ = await asyncio.gather(self.gen.health(), self.eval.health())
        return {"gen": gen, "eval": eval_}

    async def aclose(self) -> None:
        await self.gen.aclose()
        if self.eval is not self.gen:
            await self.eval.aclose()


def make_roles(settings: Settings) -> LLMRoles:
    """Одинаковые конфигурации ролей получают один общий провайдер."""
    gen_config = settings.llm_config("gen")
    eval_config = settings.llm_config("eval")
    gen = make_provider(gen_config)
    eval_ = gen if eval_config == gen_config else make_provider(eval_config)
    return LLMRoles(gen=gen, eval=eval_)


async def log_llm_health(roles: LLMRoles) -> None:
    """Одна строка в лог при старте: модель готова или что сделать (008 §7). Старт не блокирует."""
    seen: set[tuple[str, str]] = set()
    for role, health in (await roles.health()).items():
        if (health.provider, health.model) in seen:
            continue
        seen.add((health.provider, health.model))
        extra = {"role": role, "provider": health.provider, "model": health.model}
        if health.ok:
            log.info("LLM готова", extra=extra)
        else:
            log.warning(health.hint or f"LLM недоступна: {health.detail}", extra=extra)
