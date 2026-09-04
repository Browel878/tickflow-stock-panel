"""Generic HTTP provider for custom market data sources."""
from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import polars as pl

from app.config import settings
from app.data_providers.base import AssetType
from app.data_providers.custom.config import CustomSourceConfig, DatasetConfig
from app.data_providers.custom.mapper import (
    apply_transforms,
    datetime_payload,
    extract_rows,
    map_rows,
)
from app.data_providers.normalizer import normalize_adj_factors, normalize_daily
from app.tickflow.rate_limits import chunked, sleep_between_batches

logger = logging.getLogger(__name__)

# 分页上限兜底: 防止 total_path 指向空/错误字段时, 单页又恒满 page_size
# 导致无限翻页打爆上游。正常全市场快照约 56 页, 远超实际页数。
_MAX_PAGES = 500

_REQUIRED = {
    "daily": {"symbol", "date", "open", "high", "low", "close", "volume", "amount"},
    "adj_factor": {"symbol", "trade_date", "ex_factor"},
    "realtime": {"symbol", "last_price", "prev_close", "open", "high", "low", "volume"},
    "minute": {"symbol", "datetime", "open", "high", "low", "close", "volume", "amount"},
    # financial 字段由数据源决定, 只要求能映射出 symbol
    "financial": {"symbol"},
}


class GenericHTTPProvider:
    """HTTP-backed custom source. It only handles fetching and schema mapping."""

    def __init__(self, config: CustomSourceConfig) -> None:
        self.config = config
        self.name = config.name
        self._client = httpx.Client(timeout=30.0)

    def close(self) -> None:
        self._client.close()

    def validate(self) -> list[str]:
        errors: list[str] = []
        for dataset, cfg in self.config.datasets.items():
            if not cfg.url:
                errors.append(f"{dataset}: url is required")
            required = _REQUIRED.get(dataset)
            if required:
                mapped = set(cfg.field_map.values())
                missing = sorted(required - mapped)
                if missing:
                    errors.append(f"{dataset}: missing mapped fields: {', '.join(missing)}")
            if dataset != "realtime":
                request_params = [cfg.symbols_param, cfg.start_param, cfg.end_param]
                if dataset == "minute":
                    request_params.extend(
                        name for name in (cfg.asset_type_param, cfg.freq_param) if name
                    )
                duplicates = sorted({
                    name for name in request_params if request_params.count(name) > 1
                })
                if duplicates:
                    errors.append(
                        f"{dataset}: duplicate request parameter names: "
                        f"{', '.join(duplicates)}"
                    )
        return errors

    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",  # noqa: ARG002
        on_chunk_done=None,
    ) -> pl.DataFrame:
        cfg = self._dataset("daily")
        frames: list[pl.DataFrame] = []
        chunks = chunked(symbols, cfg.batch)
        for i, chunk in enumerate(chunks):
            sleep_between_batches(i, cfg.rpm)
            rows = self._request_rows(cfg, symbols=chunk, start_time=start_time, end_time=end_time)
            df = self._mapped_frame(cfg, rows)
            df = normalize_daily(df, source=self.name)
            if not df.is_empty():
                frames.append(df)
            if on_chunk_done:
                on_chunk_done(i + 1, len(chunks))
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    def get_adj_factors(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",  # noqa: ARG002
        on_chunk_done=None,
    ) -> pl.DataFrame:
        cfg = self._dataset("adj_factor")
        frames: list[pl.DataFrame] = []
        chunks = chunked(symbols, cfg.batch)
        for i, chunk in enumerate(chunks):
            sleep_between_batches(i, cfg.rpm)
            rows = self._request_rows(cfg, symbols=chunk, start_time=start_time, end_time=end_time)
            df = self._mapped_frame(cfg, rows)
            df = normalize_adj_factors(df, source=self.name)
            if not df.is_empty():
                frames.append(df)
            if on_chunk_done:
                on_chunk_done(i + 1, len(chunks))
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    def get_realtime(self) -> list[dict]:
        """拉取实时行情。

        返回内部标准字段的 dict 列表。默认单次拉取全量快照;
        配置 `page_size` 后按 offset/limit 分页取完整个快照 (如全市场 5566 只)。
        用 `total_path` 从响应信封读取总条数估算页数, 无 total 或 total 缺失时
        按「单页不足 page_size」判定结束, 兜底防死循环 (fail-closed: 不超限取数)。
        """
        cfg = self._dataset("realtime")
        rows = self._fetch_realtime_rows(cfg)
        df = self._mapped_frame(cfg, rows)
        if df.is_empty():
            return []
        return df.to_dicts()

    def _fetch_realtime_rows(self, cfg: DatasetConfig) -> list[dict]:
        """拉取 realtime 的原始行列表 (未映射)。

        - 未配置 page_size: 单次请求。
        - 配置 page_size: 按 offset/limit 翻页, 显式发送 limit=page_size,
          每页间按 page_delay(秒) 或 rpm 节流, 合并所有页返回。
        「试拉测试」也走此路径, 以便验证全量行数。
        """
        if cfg.page_size is None or cfg.page_size <= 0:
            return self._request_rows(cfg)

        all_rows: list[dict] = []
        total: int | None = None
        offset = 0
        for page_index in range(_MAX_PAGES):
            if page_index > 0:
                # 按需节流: 优先用 page_delay(应对按秒限频的上游), 否则回退 rpm 槽位表
                if cfg.page_delay and cfg.page_delay > 0:
                    time.sleep(cfg.page_delay)
                else:
                    sleep_between_batches(page_index + 1, cfg.rpm)
            page_params = {cfg.offset_param: offset, cfg.limit_param: cfg.page_size}
            payload = self._request_payload(cfg, override_params=page_params)
            rows = extract_rows(payload, cfg.response_path)
            if not rows:
                break
            all_rows.extend(rows)
            if total is None:
                total = _lookup_total(payload, cfg.total_path)
            offset += len(rows)
            # 分页结束判定: 拿到 total 则按 total 判; 否则当本页不足 page_size 视为末页
            if total is not None:
                if offset >= total:
                    break
            elif len(rows) < cfg.page_size:
                break

        return all_rows

    def get_minute(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: AssetType = "stock",
        freq: str = "1m",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """拉取分钟 K。

        asset_type / freq 默认不传上游 (minute dataset URL 应返回 1m 数据)。
        在 dataset 配置中设置 asset_type_param / freq_param 后, 这两个参数会以
        配置的参数名注入请求 (GET → params, POST → body), 用于上游需区分
        stock/ETF/index 或固定频率的场景。
        """
        cfg = self._dataset("minute")
        override: dict[str, Any] = {}
        if cfg.asset_type_param:
            override[cfg.asset_type_param] = asset_type
        if cfg.freq_param:
            override[cfg.freq_param] = freq
        frames: list[pl.DataFrame] = []
        chunks = chunked(symbols, cfg.batch)
        for i, chunk in enumerate(chunks):
            sleep_between_batches(i, cfg.rpm)
            rows = self._request_rows(
                cfg, symbols=chunk, start_time=start_time, end_time=end_time,
                override_params=override or None, override_body=override or None,
            )
            df = self._mapped_frame(cfg, rows)
            df = self._normalize_minute(df)
            if not df.is_empty():
                frames.append(df)
            if on_chunk_done:
                on_chunk_done(i + 1, len(chunks))
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    def get_financials(
        self,
        table: str,
        symbols: list[str],
        latest_only: bool = True,
    ) -> pl.DataFrame:
        """拉取财务数据。table 包含四张财务报表及 shares 股本表。

        custom 源用一个 'financial' dataset 配置覆盖全部财务表; 请求时把 table 作为参数传给上游,
        上游根据 table 返回对应数据。字段由数据源决定, 这里只确保有 symbol 列。
        """
        cfg = self._dataset("financial")
        frames: list[pl.DataFrame] = []
        chunks = chunked(symbols, cfg.batch)
        for i, chunk in enumerate(chunks):
            sleep_between_batches(i, cfg.rpm)
            # 把 table 注入到请求参数 (上游据此区分财务表)
            extra_params = {**cfg.params, "table": table}
            extra_body = {**cfg.body, "table": table}
            if table == "shares":
                extra_params["latest"] = latest_only
                extra_body["latest"] = latest_only
            rows = self._request_rows(
                cfg, symbols=chunk,
                override_params=extra_params, override_body=extra_body,
            )
            df = self._mapped_frame(cfg, rows)
            if not df.is_empty():
                frames.append(df)
        if not frames:
            return pl.DataFrame()
        return pl.concat(frames, how="diagonal_relaxed")

    @staticmethod
    def _normalize_minute(df: pl.DataFrame) -> pl.DataFrame:
        """把映射后的 df 规范成 minute canonical 列。"""
        if df.is_empty():
            return df
        if "datetime" in df.columns and df.schema["datetime"] != pl.Datetime("us"):
            df = df.with_columns(pl.col("datetime").cast(pl.Datetime("us"), strict=False))
        for col in ("open", "high", "low", "close", "volume", "amount"):
            if col in df.columns:
                df = df.with_columns(pl.col(col).cast(pl.Float64, strict=False))
        keep = [c for c in ("symbol", "datetime", "open", "high", "low", "close", "volume", "amount") if c in df.columns]
        return df.select(keep) if keep else pl.DataFrame()

    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        cfg = self._dataset(dataset)
        test_symbols = symbols or ["000001.SZ"]
        end_time = datetime.now()
        start_time = end_time - timedelta(days=7)
        if dataset == "realtime":
            rows = self._fetch_realtime_rows(cfg)
        elif dataset == "minute":
            override: dict[str, Any] = {}
            if cfg.asset_type_param:
                override[cfg.asset_type_param] = "stock"
            if cfg.freq_param:
                override[cfg.freq_param] = "1m"
            rows = self._request_rows(
                cfg,
                symbols=test_symbols,
                start_time=start_time,
                end_time=end_time,
                override_params=override or None,
                override_body=override or None,
            )
        elif dataset in {"daily", "adj_factor"}:
            rows = self._request_rows(
                cfg,
                symbols=test_symbols,
                start_time=start_time,
                end_time=end_time,
            )
        else:
            rows = self._request_rows(cfg, symbols=test_symbols)
        df = self._mapped_frame(cfg, rows)
        return {
            "provider": self.name,
            "dataset": dataset,
            "rows": len(rows),
            "columns": df.columns,
            "preview": df.head(5).to_dicts() if not df.is_empty() else [],
        }

    def _dataset(self, name: str) -> DatasetConfig:
        cfg = self.config.datasets.get(name)
        if not cfg:
            raise ValueError(f"Custom data source '{self.name}' does not configure dataset '{name}'")
        return cfg

    def _mapped_frame(self, cfg: DatasetConfig, rows: list[dict]) -> pl.DataFrame:
        df = map_rows(rows, cfg.field_map)
        return apply_transforms(df, cfg.transforms)

    def _request_rows(
        self,
        cfg: DatasetConfig,
        *,
        symbols: list[str] | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        override_params: dict[str, Any] | None = None,
        override_body: dict[str, Any] | None = None,
    ) -> list[dict]:
        payload = self._request_payload(
            cfg, symbols=symbols, start_time=start_time, end_time=end_time,
            override_params=override_params, override_body=override_body,
        )
        return extract_rows(payload, cfg.response_path)

    def _request_payload(
        self,
        cfg: DatasetConfig,
        *,
        symbols: list[str] | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        override_params: dict[str, Any] | None = None,
        override_body: dict[str, Any] | None = None,
    ) -> Any:
        """发送一次请求, 返回完整解析后的响应 JSON (信封), 不抽取 item。

        `get_realtime` 分页时需要读取信封上的 total 字段, 因此把抽取与请求分离,
        便于分页循环直接拿信封计算页数。
        """
        headers, auth_params = self._auth_parts()
        params = dict(cfg.params)
        params.update(auth_params)
        if override_params:
            params.update(override_params)
        body = dict(cfg.body)
        if override_body:
            body.update(override_body)
        if symbols:
            body[cfg.symbols_param] = symbols
            params.setdefault(cfg.symbols_param, ",".join(symbols))
        start_value = datetime_payload(start_time)
        end_value = datetime_payload(end_time)
        if start_value:
            body[cfg.start_param] = start_value
            params.setdefault(cfg.start_param, start_value)
        if end_value:
            body[cfg.end_param] = end_value
            params.setdefault(cfg.end_param, end_value)

        method = cfg.method.upper()
        request_kwargs: dict[str, Any] = {"headers": headers, "timeout": cfg.timeout}
        if method == "GET":
            request_kwargs["params"] = params
        else:
            request_kwargs["params"] = auth_params
            request_kwargs["json"] = body
        resp = self._client.request(method, cfg.url, **request_kwargs)
        resp.raise_for_status()
        return resp.json()

    def _auth_parts(self) -> tuple[dict[str, str], dict[str, str]]:
        auth = self.config.auth
        if auth.type == "none":
            return {}, {}
        token = _token_from_env(auth.token_env) if auth.token_env else None
        if not token:
            logger.warning("custom data source %s auth token is not set", self.name)
            return {}, {}
        if auth.type == "bearer":
            return {auth.header: f"Bearer {token}"}, {}
        if auth.type == "header":
            return {auth.header: token}, {}
        if auth.type == "query":
            return {}, {auth.param: token}
        return {}, {}


def _lookup_total(payload: Any, total_path: str) -> int | None:
    """按点路径从响应信封读取总条数 (如 THS 快照的 data.total)。

    读取失败/非数字时返回 None, 分页循环据此走「单页不足 page_size 判定末页」。
    """
    if not total_path:
        return None
    data = payload
    for part in total_path.split("."):
        if not part:
            continue
        if isinstance(data, dict):
            data = data.get(part)
        else:
            return None
    try:
        return int(data)
    except (TypeError, ValueError):
        return None


def _token_from_env(name: str | None) -> str | None:
    if not name:
        return None
    token = os.getenv(name)
    if token:
        return token
    candidates = [settings.data_dir.parent / ".env", Path.cwd() / ".env", Path.cwd().parent / ".env"]
    env_path = next((path for path in candidates if path.exists()), None)
    if env_path is None:
        return None
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text or text.startswith("#") or "=" not in text:
                continue
            key, value = text.split("=", 1)
            if key.strip() == name:
                return value.strip().strip('"').strip("'")
    except Exception:  # noqa: BLE001
        return None
    return None
