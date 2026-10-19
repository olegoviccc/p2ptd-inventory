"""Фото текущего прибора у каждого пользователя (в памяти).

Фото скачиваются и обрабатываются сразу при получении, пока человек снимает
следующее, поэтому к нажатию «Распознать» всё уже готово, и при сохранении
повторно ничего не скачивается.
"""

import asyncio
from collections import defaultdict

MAX_PHOTOS = 5


class PhotoBuffer:
    def __init__(self):
        self._photos = defaultdict(list)
        self._locks = defaultdict(asyncio.Lock)

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
