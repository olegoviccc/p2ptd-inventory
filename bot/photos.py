"""Фото текущего прибора у каждого пользователя (в памяти).

Фото скачиваются и обрабатываются сразу при получении. Распознавание запускается само,
когда новых фото нет пару секунд (альбом приходит отдельными сообщениями). Новое фото
отменяет запланированное или идущее распознавание, и оно начинается заново со всеми фото.
"""

import asyncio
from collections import defaultdict

MAX_PHOTOS = 5


class PhotoBuffer:
    def __init__(self):
        self._photos = defaultdict(list)
        self._locks = defaultdict(asyncio.Lock)
        self._tasks = {}
        self.downloading = defaultdict(int)  # фото, которые ещё качаются: распознавать рано

    def lock(self, user_id):
        return self._locks[user_id]

    def get(self, user_id):
        return list(self._photos.get(user_id, []))

    def add(self, user_id, photo):
        if len(self._photos[user_id]) >= MAX_PHOTOS:
            return False
        self._photos[user_id].append(photo)
        return True

    def clear(self, user_id):
        self._photos.pop(user_id, None)

    def schedule(self, user_id, coro):
        """Запускает распознавание вместо прежнего (если было)."""
        self.cancel(user_id)
        self._tasks[user_id] = asyncio.create_task(coro)

    def cancel(self, user_id):
        task = self._tasks.pop(user_id, None)
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()
