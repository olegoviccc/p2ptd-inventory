"""Этикетки: InvenTree рисует их по своему шаблону, бот только присылает файл.

Шаблон ставится скриптом install_label_template.py; его имя — BOT_LABEL_TEMPLATE.
Если шаблона нет или InvenTree не справился, бот рисует PNG сам (images.label_png),
чтобы этикетка была в любом случае.
"""

import asyncio
import logging
import time

from . import images
from .inventree import InvenTreeError

log = logging.getLogger(__name__)


class LabelError(Exception):
    pass


class Labels:
    def __init__(self, template_name, timeout=30, poll=0.5):
        self.template_name = template_name
        self.timeout = timeout
        self.poll = poll
        self._template_pk = None

    async def template_pk(self, it):
        if self._template_pk is None:
            found = [t for t in await it.label_templates() if t["name"] == self.template_name]
            if not found:
                raise LabelError(f"в InvenTree нет шаблона «{self.template_name}» (install_label_template.py)")
            self._template_pk = found[0]["pk"]
        return self._template_pk

    async def from_inventree(self, it, stock_pk):
        """PDF этикетки, отрисованный InvenTree."""
        output = await it.print_label(await self.template_pk(it), [stock_pk])
        deadline = time.monotonic() + self.timeout
        while not output.get("complete"):
            if output.get("errors"):
                raise LabelError(f"InvenTree не отрисовал этикетку: {output['errors']}")
            if time.monotonic() > deadline:
                raise LabelError("InvenTree не успел отрисовать этикетку (работает ли фоновый обработчик задач?)")
            await asyncio.sleep(self.poll)
            output = await it.data_output(output["pk"])
        if not output.get("output"):
            raise LabelError(f"InvenTree вернул пустой результат: {output.get('errors')}")
        return await it.fetch_bytes(output["output"])

    async def make(self, it, stock_pk, number, title, subtitle=""):
        """(имя файла, данные) — из InvenTree, а при сбое нарисованная ботом."""
        if self.template_name:
            try:
                return f"{number}.pdf", await self.from_inventree(it, stock_pk)
            except (LabelError, InvenTreeError) as e:
                self._template_pk = None  # шаблон могли переименовать — в следующий раз ищем заново
                log.warning("этикетка из InvenTree не получилась, рисую сама: %s", e)
        png = await asyncio.to_thread(images.label_png, stock_pk, number, title, subtitle)
        return f"{number}.png", png
