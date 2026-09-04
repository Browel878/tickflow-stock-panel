"""Custom realtime data source 全市场分页循环测试。

覆盖: 按 total 翻页取完、无 total 按单页不足判定末页、page_size 为空不分页、
total 读取容错、配置 round-trip 保留分页字段。
"""
from __future__ import annotations

from app.data_providers.custom.config import DatasetConfig
from app.data_providers.custom.loader import _config_to_dict, _sanitize_for_yaml
from app.data_providers.custom.provider import GenericHTTPProvider, _lookup_total


def _provider(dataset: DatasetConfig) -> GenericHTTPProvider:
    from app.data_providers.custom.config import CustomSourceConfig
    return GenericHTTPProvider(CustomSourceConfig(
        name="test_source",
        display_name="Test Source",
        datasets={"realtime": dataset},
    ))


def _base_dataset(**overrides) -> DatasetConfig:
    return DatasetConfig(
        url="https://example.test/realtime",
        response_path="data.item",
        field_map={
            "thscode": "symbol",
            "ticker": "symbol",
            "last_price": "last_price",
            "prev_price": "prev_close",
            "open_price": "open",
            "high_price": "high",
            "low_price": "low",
            "volume": "volume",
            "turnover": "amount",
            "price_change_ratio_pct": "change_pct",
        },
        **overrides,
    )


def _fake_payload(rows, total=None):
    data = {"timestamp": 1, "item": rows}
    if total is not None:
        data["total"] = total
    return {"code": 0, "message": "success", "data": data}


def _make_row(i: int) -> dict:
    return {
        "thscode": f"{600000 + i:06d}.SH",
        "ticker": f"{600000 + i:06d}",
        "last_price": float(i + 1),
        "prev_price": 1.0,
        "open_price": 1.0,
        "high_price": 1.0,
        "low_price": 1.0,
        "volume": i,
        "turnover": i * 100,
        "price_change_ratio_pct": 1.5,
    }


def _install_paginated_client(monkeypatch, payloads):
    """把 provider._request_payload 换成按 offset 顺序返回 payload 的桩。"""
    calls: list[int] = []

    def fake_request_payload(cfg, *, symbols=None, start_time=None, end_time=None,
                             override_params=None, override_body=None):
        calls.append(override_params or {})
        offset = (override_params or {}).get("offset", 0)
        return payloads[offset]

    provider = _provider(_base_dataset(
        page_size=100, offset_param="offset", limit_param="limit", total_path="data.total", rpm=0,
    ))
    monkeypatch.setattr(provider, "_request_payload", fake_request_payload)
    return provider, calls


def test_pagination_sends_limit_param(monkeypatch):
    """分页循环必须显式发送 limit=page_size, 否则上游可能一次返回全量不分页。"""
    payloads = {0: _fake_payload([_make_row(i) for i in range(100)], total=100)}
    provider, calls = _install_paginated_client(monkeypatch, payloads)

    provider.get_realtime()
    provider.close()

    assert len(calls) == 1
    assert calls[0] == {"offset": 0, "limit": 100}


def test_pagination_fetches_all_pages_using_total(monkeypatch):
    # 3 页: 100 + 100 + 50 = 250 只, total=250
    payloads = {
        0: _fake_payload([_make_row(i) for i in range(100)], total=250),
        100: _fake_payload([_make_row(i) for i in range(100, 200)], total=250),
        200: _fake_payload([_make_row(i) for i in range(200, 250)], total=250),
    }
    provider, calls = _install_paginated_client(monkeypatch, payloads)

    out = provider.get_realtime()
    provider.close()

    assert len(out) == 250
    assert [c.get("offset") for c in calls] == [0, 100, 200]
    assert all(c.get("limit") == 100 for c in calls)
    assert out[0]["symbol"] == "600000.SH"
    assert out[-1]["symbol"] == "600249.SH"


def test_pagination_ends_on_partial_last_page_without_total(monkeypatch):
    # 无 total: 末页不足 page_size 即结束
    payloads = {
        0: _fake_payload([_make_row(i) for i in range(100)]),
        100: _fake_payload([_make_row(i) for i in range(100, 150)]),
    }
    provider, calls = _install_paginated_client(monkeypatch, payloads)

    out = provider.get_realtime()
    provider.close()

    assert len(out) == 150
    assert [c.get("offset") for c in calls] == [0, 100]


def test_no_pagination_when_page_size_unset(monkeypatch):
    provider = _provider(_base_dataset(rpm=0))
    monkeypatch.setattr(provider, "_request_payload", lambda cfg, **kw: _fake_payload([_make_row(0) for _ in range(5)]))

    out = provider.get_realtime()
    provider.close()

    assert len(out) == 5


def test_empty_first_page_returns_empty(monkeypatch):
    provider = _provider(_base_dataset(page_size=100, rpm=0))
    monkeypatch.setattr(provider, "_request_payload", lambda cfg, **kw: _fake_payload([]))

    assert provider.get_realtime() == []
    provider.close()


def test_test_dataset_realtime_pulls_full_market(monkeypatch):
    """试拉测试 realtime 应走分页路径取全量, 而非单页 100 行。"""
    payloads = {
        0: _fake_payload([_make_row(i) for i in range(100)], total=250),
        100: _fake_payload([_make_row(i) for i in range(100, 200)], total=250),
        200: _fake_payload([_make_row(i) for i in range(200, 250)], total=250),
    }
    provider, calls = _install_paginated_client(monkeypatch, payloads)

    result = provider.test_dataset("realtime", ["600000.SH"])
    provider.close()

    assert result["rows"] == 250
    assert [c.get("offset") for c in calls] == [0, 100, 200]


def test_lookup_total_handles_missing_and_invalid():
    assert _lookup_total({"data": {"total": 5}}, "data.total") == 5
    assert _lookup_total({"data": {}}, "data.total") is None
    assert _lookup_total({"data": {"total": "abc"}}, "data.total") is None
    assert _lookup_total({"data": [1, 2]}, "data.total") is None
    assert _lookup_total({"data": {"total": 5}}, "") is None


def test_pagination_config_survives_round_trip():
    cleaned = _sanitize_for_yaml({
        "name": "ths_full_market",
        "display_name": "THS",
        "datasets": {
            "realtime": {
                "url": "https://example.test/realtime",
                "page_size": 100,
                "page_delay": 0.5,
                "offset_param": "offset",
                "limit_param": "limit",
                "total_path": "data.total",
            },
        },
    })
    from app.data_providers.custom.config import _dataset_from_dict
    parsed = _dataset_from_dict(cleaned["datasets"]["realtime"])

    assert parsed.page_size == 100
    assert parsed.page_delay == 0.5
    assert parsed.offset_param == "offset"
    assert parsed.limit_param == "limit"
    assert parsed.total_path == "data.total"

    exposed = _config_to_dict(provider_config_for(parsed))
    ds = exposed["datasets"]["realtime"]
    assert ds["page_size"] == 100
    assert ds["page_delay"] == 0.5
    # 默认值不写进 YAML / 不回显
    assert "offset_param" not in ds
    assert "limit_param" not in ds
    assert "total_path" not in ds


def test_pagination_config_default_omitted():
    from app.data_providers.custom.config import CustomSourceConfig
    parsed = _base_dataset()  # page_size None
    exposed = _config_to_dict(CustomSourceConfig(
        name="test_source", display_name="Test", datasets={"realtime": parsed},
    ))
    ds = exposed["datasets"]["realtime"]
    assert "page_size" not in ds
    assert "offset_param" not in ds
    assert "limit_param" not in ds
    assert "total_path" not in ds


def provider_config_for(parsed: DatasetConfig):
    """构造一个含 parsed DatasetConfig 的 CustomSourceConfig 供 _config_to_dict。"""
    from app.data_providers.custom.config import CustomSourceConfig
    return CustomSourceConfig(
        name="test_source", display_name="Test", datasets={"realtime": parsed},
    )