"""Провайдеры LLM по ролям: gen — разбор и генерация, eval — оценка (002 §5.3)."""

import asyncio
from dataclasses import dataclass

from sobesnik_api.settings import ROLES, Role, Settings
from sobesnik_llm import LLMHealth, LLMProvider, make_provider


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
