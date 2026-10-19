"""Своя модель через OpenAI-совместимый /v1/chat/completions (llama-swap, Ollama, LM Studio, vLLM).

Общий клиент для распознавания шильдиков (recognize.py) и проверки найденных
мануалов и фото (scraper/verify.py): ответ строго по JSON-схеме.
"""

import base64
import json
import re

import aiohttp


class LLMError(Exception):
    pass


def extract_json(text):
    """Объект JSON из ответа модели: без размышлений <think>…</think>, обёртки ``` и пояснений вокруг."""
    text = re.sub(r"<think>.*?(</think>|$)", "", text or "", flags=re.S).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    start = text.find("{")
    while start != -1:  # первый целый объект в тексте
        try:
            return decoder.raw_decode(text, start)[0]
        except ValueError:
            start = text.find("{", start + 1)
    raise ValueError("в ответе нет JSON")


def image_part(jpeg):
    return {"type": "image_url",
            "image_url": {"url": "data:image/jpeg;base64," + base64.standard_b64encode(jpeg).decode()}}


class LocalLLM:
    def __init__(self, base_url, model, api_key="", timeout=300, think=None):
        self.base = base_url.rstrip("/")
        self.url = self.base + "/chat/completions"
        self.model = model
        self.think = think
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session = None

    @property
    def session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def check(self):
        """Сервер отвечает и знает модель. Возвращает текст проблемы или None."""
        try:
            async with self.session.get(self.base + "/models", headers=self.headers,
                                        timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status >= 400:
                    return f"{self.base}/models ответил {resp.status}"
                ids = [m.get("id") for m in (await resp.json(content_type=None)).get("data", [])]
        except (aiohttp.ClientError, TimeoutError, ValueError) as e:
            return f"нет связи с {self.base}: {e}"
        if ids and self.model not in ids:
            return f"модели {self.model} нет на сервере (есть: {', '.join(ids[:8])})"
        return None

    async def complete(self, content, schema, name="answer", max_tokens=8192):
        """Текст ответа модели (JSON по схеме, но разбирать его — дело вызывающего)."""
        body = {
            "model": self.model,
            "temperature": 0,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": max_tokens,
            # strict=False — как в olegovic-llm: llama-server и Ollama держат схему грамматикой
            "response_format": {"type": "json_schema", "json_schema": {"name": name, "strict": False, "schema": schema}},
        }
        if self.think is None or isinstance(self.think, bool):
            # Qwen-шаблоны: для коротких проверок размышления не нужны, без них быстрее и JSON не обрывается.
            # Шаблоны без этой переменной её просто игнорируют.
            body["chat_template_kwargs"] = {"enable_thinking": bool(self.think)}
        else:  # gpt-oss: low / medium / high
            body["reasoning_effort"] = self.think
            body["chat_template_kwargs"] = {"reasoning_effort": self.think}
        try:
            async with self.session.post(self.url, json=body, headers=self.headers) as resp:
                if resp.status >= 400:
                    raise LLMError(f"сервер ответил {resp.status}: {(await resp.text())[:200]}")
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as e:
            raise LLMError(f"нет связи с {self.url}") from e
        try:
            choice = data["choices"][0]
            text = choice["message"].get("content") or ""
        except (KeyError, IndexError, TypeError, AttributeError) as e:
            raise LLMError("неожиданный формат ответа") from e
        if isinstance(text, list):  # некоторые серверы отдают content частями
            text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
        if choice.get("finish_reason") == "length" and "{" not in text:
            raise LLMError("модель упёрлась в лимит токенов, не дописав ответ (обычно из-за размышлений: "
                           "LOCAL_LLM_THINK=false)")
        return text
