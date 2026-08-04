"""回归测试: Free 档自选实时标的按批次轮换 (自选股轮换)。

Free 档每轮实时轮询最多查 5 只 (tiers.yaml free.quote.by_symbol batch=5)。
preferences.advance_realtime_watchlist_symbols() 每轮返回下一批自选标的并推进
持久化游标, 使超过单批上限的自选列表也能被逐轮覆盖; get_realtime_watchlist_symbols()
只读当前批次, 不推进游标。
"""
from unittest.mock import patch

import pytest

from app.services import preferences


@pytest.fixture
def prefs_file(tmp_path):
    """把 preferences 读写重定向到临时文件, 隔离真实用户数据。"""
    prefs_path = tmp_path / "preferences.json"
    with patch("app.services.preferences._path", return_value=prefs_path):
        yield prefs_path


def _rows(symbols: list[str]) -> list[dict]:
    return [{"symbol": s, "added_at": "", "note": ""} for s in symbols]


def _patch_watchlist(symbols: list[str]):
    return patch("app.services.watchlist.list_symbols", return_value=_rows(symbols))


def test_advance_rotates_batches_and_wraps(prefs_file):
    """自选 12 只 / 每批 5 → 逐轮轮换, 覆盖全部后回到开头。"""
    symbols = [f"600{i:03d}.SH" for i in range(12)]
    with _patch_watchlist(symbols):
        assert preferences.advance_realtime_watchlist_symbols() == symbols[0:5]
        assert preferences.advance_realtime_watchlist_symbols() == symbols[5:10]
        assert preferences.advance_realtime_watchlist_symbols() == symbols[10:12] + symbols[0:3]
        assert preferences.advance_realtime_watchlist_symbols() == symbols[3:8]
        # 三轮内全部 12 只都被覆盖过
        covered = set()
        for _ in range(4):
            covered.update(preferences.advance_realtime_watchlist_symbols())
        assert covered == set(symbols)


def test_advance_persists_cursor(prefs_file):
    """游标写入 preferences.json, 进程重启后继续轮换。"""
    symbols = [f"600{i:03d}.SH" for i in range(12)]
    with _patch_watchlist(symbols):
        preferences.advance_realtime_watchlist_symbols()          # 游标 0 → 5
        assert preferences.load()["realtime_watchlist_cursor"] == 5
        # 模拟重启: 游标从磁盘恢复, 继续从 5 开始
        assert preferences.advance_realtime_watchlist_symbols()[0] == symbols[5]


def test_get_is_read_only(prefs_file):
    """get_realtime_watchlist_symbols 不推进游标 (状态/设置页频繁调用安全)。"""
    symbols = [f"600{i:03d}.SH" for i in range(12)]
    with _patch_watchlist(symbols):
        assert preferences.get_realtime_watchlist_symbols() == symbols[0:5]
        assert preferences.get_realtime_watchlist_symbols() == symbols[0:5]
        assert "realtime_watchlist_cursor" not in preferences.load()


def test_advance_below_batch_returns_all(prefs_file):
    """自选不足一批 → 返回全部, 不写游标。"""
    symbols = ["600000.SH", "600001.SH", "600002.SH"]
    with _patch_watchlist(symbols):
        assert preferences.advance_realtime_watchlist_symbols() == symbols
        assert "realtime_watchlist_cursor" not in preferences.load()


def test_advance_empty_watchlist(prefs_file):
    """空自选 → 空批次, 不写游标。"""
    with _patch_watchlist([]):
        assert preferences.advance_realtime_watchlist_symbols() == []
        assert preferences.get_realtime_watchlist_symbols() == []
        assert "realtime_watchlist_cursor" not in preferences.load()


def test_advance_cursor_out_of_range_clamps(prefs_file):
    """自选被删除后游标越界 → 从 0 重新开始, 不越界崩溃。"""
    symbols = [f"600{i:03d}.SH" for i in range(6)]
    prefs_file.write_text('{"realtime_watchlist_cursor": 999}', encoding="utf-8")
    with _patch_watchlist(symbols):
        assert preferences.advance_realtime_watchlist_symbols() == symbols[0:5]
        assert preferences.load()["realtime_watchlist_cursor"] == 5


def test_advance_dedup_and_uppercase(prefs_file):
    """重复/小写标的去重并统一大写。"""
    with _patch_watchlist(["600000.sh", "600000.SH", "600001.sh"]):
        assert preferences.advance_realtime_watchlist_symbols() == ["600000.SH", "600001.SH"]


def test_advance_custom_batch(prefs_file):
    """batch 参数生效 (供 capability 批次变化时复用)。"""
    symbols = [f"600{i:03d}.SH" for i in range(6)]
    with _patch_watchlist(symbols):
        assert preferences.advance_realtime_watchlist_symbols(batch=2) == symbols[0:2]
        assert preferences.advance_realtime_watchlist_symbols(batch=2) == symbols[2:4]


def test_advance_watchlist_shrinks_mid_rotation(prefs_file):
    """轮换中自选被删 → 新列表内继续轮换, 不多不少。"""
    symbols = [f"600{i:03d}.SH" for i in range(10)]
    with _patch_watchlist(symbols):
        preferences.advance_realtime_watchlist_symbols()          # 游标 → 5
    remaining = symbols[5:]
    with _patch_watchlist(remaining):
        # 游标 5 已越界 (len=5), 从头返回全部 5 只
        assert preferences.advance_realtime_watchlist_symbols() == remaining
