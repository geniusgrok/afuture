"""自动候选运行时辅助：把慢速 CTP 元数据查询移出 Tick 关键路径。"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from threading import Lock

from .models import ContractSpec


class MetadataPrefetcher:
    """单工作线程的有界元数据预取器。

    Auto 扫描只负责提交真实柜台查询请求；真正的 CTP 保证金/手续费等待发生在后台线程。
    确定性的 SimBroker 元数据是内存读取，继续同步完成，避免研究回放引入线程调度噪声。
    """

    def __init__(self, timeout_seconds: float = 10.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.timeout_seconds = timeout_seconds
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="afuture-metadata")
        self._futures: dict[tuple[str, ...], Future] = {}
        self._cache: dict[str, ContractSpec] = {}
        self._errors: dict[tuple[str, ...], str] = {}
        self._lock = Lock()

    @staticmethod
    def _key(symbols) -> tuple[str, ...]:
        return tuple(sorted(set(str(symbol) for symbol in symbols)))

    def request(self, broker, symbols) -> bool:
        """缓存已齐全返回 True；真实慢查询只安排后台任务并立即返回 False。"""
        key = self._key(symbols)
        if not key:
            return True
        with self._lock:
            self._harvest_locked(key)
            if all(symbol in self._cache for symbol in key):
                return True

        # The Broker capability, not its implementation name, declares cache-only metadata.
        if not bool(getattr(broker, "metadata_query_blocks", True)):
            try:
                rows = broker.get_live_contract_specs(list(key), self.timeout_seconds)
            except Exception as exc:
                with self._lock:
                    self._errors[key] = str(exc)
                return False
            with self._lock:
                self._apply_rows_locked(key, rows)
                return all(symbol in self._cache for symbol in key)

        with self._lock:
            future = self._futures.get(key)
            if future is None:
                self._errors.pop(key, None)
                self._futures[key] = self._executor.submit(
                    broker.get_live_contract_specs,
                    list(key),
                    self.timeout_seconds,
                )
            return False

    def get(self, symbols) -> dict[str, ContractSpec] | None:
        key = self._key(symbols)
        with self._lock:
            self._harvest_locked(key)
            if not all(symbol in self._cache for symbol in key):
                return None
            return {symbol: self._cache[symbol] for symbol in key}

    def wait(self, symbols, timeout_seconds: float | None = None) -> dict[str, ContractSpec] | None:
        """测试/启动路径可显式等待；生产 Tick 路径不调用。"""
        key = self._key(symbols)
        with self._lock:
            self._harvest_locked(key)
            future = self._futures.get(key)
            if future is None:
                if all(symbol in self._cache for symbol in key):
                    return {symbol: self._cache[symbol] for symbol in key}
                return None
        try:
            rows = future.result(
                timeout=self.timeout_seconds if timeout_seconds is None else timeout_seconds
            )
        except TimeoutError:
            return None
        except Exception as exc:
            with self._lock:
                self._errors[key] = str(exc)
                self._futures.pop(key, None)
            return None
        with self._lock:
            self._apply_rows_locked(key, rows)
            return {symbol: self._cache[symbol] for symbol in key if symbol in self._cache} or None

    def error(self, symbols) -> str:
        key = self._key(symbols)
        with self._lock:
            self._harvest_locked(key)
            return self._errors.get(key, "")

    def invalidate(self) -> None:
        """交易日变化时清空缓存；进行中的旧日查询结果也不再复用。"""
        with self._lock:
            self._cache.clear()
            self._errors.clear()
            self._futures.clear()

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _harvest_locked(self, key: tuple[str, ...]) -> None:
        future = self._futures.get(key)
        if future is None or not future.done():
            return
        self._futures.pop(key, None)
        try:
            rows = future.result()
        except Exception as exc:
            self._errors[key] = str(exc)
            return
        self._apply_rows_locked(key, rows)

    def _apply_rows_locked(self, key: tuple[str, ...], rows) -> None:
        rows = dict(rows or {})
        missing = [symbol for symbol in key if symbol not in rows]
        if missing:
            self._errors[key] = f"live contract spec missing: {', '.join(missing)}"
            return
        self._cache.update({symbol: rows[symbol] for symbol in key})
        self._errors.pop(key, None)
        self._futures.pop(key, None)
