from __future__ import annotations

from collections.abc import AsyncIterator

import httpx


class DeepSeekClient:
    REQUEST_TIMEOUT_SECONDS = 120.0

    def __init__(self, api_key: str, base_url: str) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._client: httpx.AsyncClient | None = None

    @property
    def completions_url(self) -> str:
        return f"{self.base_url}/chat/completions"

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def create_completion(self, payload: dict) -> dict:
        response = await self.client.post(
            self.completions_url,
            headers=self.headers,
            json=payload,
            timeout=self.REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response.json()

    async def stream_completion(self, payload: dict) -> AsyncIterator[str]:
        async with self.client.stream(
            "POST",
            self.completions_url,
            headers={**self.headers, "Accept": "text/event-stream"},
            json=payload,
        ) as response:
            if response.status_code >= 400:
                await response.aread()
            response.raise_for_status()
            async for line in response.aiter_lines():
                yield line

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=None)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
