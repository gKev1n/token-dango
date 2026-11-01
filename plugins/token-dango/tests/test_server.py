"""运行：/usr/bin/python3 -m unittest discover -s tests（在插件目录下）。"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server  # noqa: E402

USAGE = {"input_tokens": 204800, "cached_input_tokens": 186368, "cache_write_input_tokens": 0,
         "output_tokens": 8704, "reasoning_output_tokens": 4096, "total_tokens": 213504}
LAST = {"input_tokens": 120000, "cached_input_tokens": 110000, "cache_write_input_tokens": 0,
        "output_tokens": 7000, "reasoning_output_tokens": 3000, "total_tokens": 127000}


def line(kind, payload, ts="2026-11-01T10:00:00.000Z"):
    return json.dumps({"timestamp": ts, "type": kind, "payload": payload}) + "\n"


def token_count(ts="2026-11-01T10:00:00.000Z", used=34.0, resets=1793900000):
    return line("event_msg", {
        "type": "token_count",
        "info": {"total_token_usage": USAGE, "last_token_usage": LAST, "model_context_window": 262100},
        "rate_limits": {"primary": {"used_percent": used, "window_minutes": 10080, "resets_at": resets},
                        "secondary": None, "plan_type": "pro"},
    }, ts)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        os.environ["CODEX_HOME"] = str(self.home)
        server._rollouts.clear()
        con = sqlite3.connect(self.home / "state_5.sqlite")
        con.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, title TEXT, name TEXT,"
                    " model TEXT, archived INTEGER DEFAULT 0, updated_at_ms INTEGER)")
        con.commit()
        con.close()

    def tearDown(self):
        os.environ.pop("CODEX_HOME", None)
        self.tmp.cleanup()

    def add_thread(self, tid, text, updated=1, name=None):
        path = self.home / ("%s.jsonl" % tid)
        path.write_text(text, encoding="utf-8")
        con = sqlite3.connect(self.home / "state_5.sqlite")
        con.execute("INSERT INTO threads VALUES (?,?,?,?,?,0,?)", (tid, str(path), "标题" + tid, name, "gpt-test", updated))
        con.commit()
        con.close()
        return path


class SnapshotTests(Base):
    def test_cumulative_and_context_numbers(self):
        self.add_thread("A", token_count())
        s = server.snapshot("A")
        self.assertEqual(s["status"], "ok")
        self.assertEqual(s["total"]["total_tokens"], 213504)
        self.assertEqual(s["total"]["input_tokens"] - s["total"]["cached_input_tokens"], 18432)
        self.assertAlmostEqual(s["total"]["cached_input_tokens"] / s["total"]["input_tokens"], 0.91, places=3)
        self.assertEqual(s["last"]["total_tokens"], 127000)
        self.assertEqual(s["window"], 262100)
        self.assertEqual(s["mode"], "thread")

    def test_waiting_when_no_usage_yet(self):
        self.add_thread("A", line("session_meta", {"id": "A"}))
        self.assertEqual(server.snapshot("A")["status"], "waiting")

    def test_unknown_thread_does_not_fall_back(self):
        self.add_thread("A", token_count())
        s = server.snapshot("missing")
        self.assertEqual(s["status"], "unreadable")
        self.assertNotIn("total", s)

    def test_half_written_tail_line_is_picked_up_later(self):
        full = token_count(ts="2026-11-01T10:05:00.000Z", used=40.0)
        path = self.add_thread("A", token_count() + full[:50])
        self.assertEqual(server.snapshot("A")["quota"]["windows"][0]["used_percent"], 34.0)
        with path.open("a", encoding="utf-8") as f:
            f.write(full[50:])
        self.assertEqual(server.snapshot("A")["quota"]["windows"][0]["used_percent"], 40.0)

    def test_replaced_file_resets_state(self):
        path = self.add_thread("A", token_count())
        self.assertEqual(server.snapshot("A")["status"], "ok")
        path.unlink()
        path.write_text(line("session_meta", {"id": "A"}), encoding="utf-8")
        self.assertEqual(server.snapshot("A")["status"], "waiting")

    def test_quota_uses_latest_across_threads(self):
        self.add_thread("old", token_count(ts="2026-10-28T08:00:00.000Z", used=12.0), updated=1)
        self.add_thread("new", token_count(ts="2026-11-01T11:00:00.000Z", used=61.0), updated=2)
        q = server.snapshot("old")["quota"]
        self.assertEqual(q["windows"][0]["used_percent"], 61.0)
        self.assertEqual(q["as_of"], "2026-11-01T11:00:00.000Z")

    def test_running_turn(self):
        text = token_count() + line("event_msg", {"type": "task_started", "turn_id": "t1", "started_at": 1000})
        self.add_thread("A", text)
        turn = server.snapshot("A", now=1030)["turn"]
        self.assertTrue(turn["running"])
        self.assertEqual(turn["elapsed_ms"], 30000)

    def test_completed_turn_with_usage(self):
        text = (line("event_msg", {"type": "task_started", "turn_id": "t1", "started_at": 1000})
                + line("token_usage_record", {"turn_id": "t1", "turn_token_usage": dict(LAST, output_tokens=1728)})
                + token_count()
                + line("event_msg", {"type": "task_complete", "turn_id": "t1", "duration_ms": 32000,
                                     "time_to_first_token_ms": 2100, "completed_at": 1032}))
        self.add_thread("A", text)
        turn = server.snapshot("A")["turn"]
        self.assertFalse(turn["running"])
        self.assertEqual(turn["duration_ms"], 32000)
        self.assertEqual(turn["usage"]["output_tokens"], 1728)

    def test_tps_excludes_overlapping_tool_time(self):
        # 本轮 60 秒，两个工具调用 10–30 秒与 20–40 秒重叠，实际占用 30 秒，模型时间 30 秒
        text = (line("event_msg", {"type": "task_started", "turn_id": "t1", "started_at": 1000})
                + line("event_msg", {"type": "item_completed", "turn_id": "t1", "item": {"type": "CommandExecution"},
                                     "started_at_ms": 1010000, "completed_at_ms": 1030000})
                + line("event_msg", {"type": "item_completed", "turn_id": "t1", "item": {"type": "McpToolCall"},
                                     "started_at_ms": 1020000, "completed_at_ms": 1040000})
                + line("event_msg", {"type": "item_completed", "turn_id": "t1", "item": {"type": "Reasoning"},
                                     "started_at_ms": 1000000, "completed_at_ms": 1005000})
                + line("token_usage_record", {"turn_id": "t1", "usage": LAST,
                                              "turn_token_usage": dict(LAST, output_tokens=1500)})
                + token_count()
                + line("event_msg", {"type": "task_complete", "turn_id": "t1", "duration_ms": 60000,
                                     "completed_at": 1060}))
        self.add_thread("A", text)
        turn = server.snapshot("A")["turn"]
        self.assertEqual(turn["tool_ms"], 30000)
        self.assertEqual(turn["model_ms"], 30000)
        self.assertEqual(turn["tps"], 50.0)

    def test_tps_hidden_when_model_time_too_short(self):
        text = (line("event_msg", {"type": "task_started", "turn_id": "t1", "started_at": 1000})
                + line("token_usage_record", {"turn_id": "t1", "usage": LAST, "turn_token_usage": dict(LAST, output_tokens=10)})
                + token_count()
                + line("event_msg", {"type": "task_complete", "turn_id": "t1", "duration_ms": 500, "completed_at": 1000.5}))
        self.add_thread("A", text)
        self.assertIsNone(server.snapshot("A")["turn"]["tps"])

    def test_name_preferred_over_title(self):
        self.add_thread("A", token_count(), name="改过的名字")
        self.assertEqual(server.snapshot("A")["title"], "改过的名字")


class CostTests(Base):
    PRICES = {"long_context_threshold": 272000, "models": {"gpt-test": {
        "standard": {"short": [2.0, 0.1, 10.0, 2.5], "long": [4.0, 0.2, 15.0, 5.0]},
        "fast": {"short": [4.0, 0.2, 20.0, 5.0], "long": [8.0, 0.4, 30.0, 10.0]}}}}

    def setUp(self):
        super().setUp()
        self._load = server.load_prices
        server.load_prices = lambda: self.PRICES

    def tearDown(self):
        server.load_prices = self._load
        super().tearDown()

    def record(self, inp, cached, out, turn="t1"):
        u = {"input_tokens": inp, "cached_input_tokens": cached, "cache_write_input_tokens": 0,
             "output_tokens": out, "reasoning_output_tokens": 0, "total_tokens": inp + out}
        return line("token_usage_record", {"turn_id": turn, "usage": u, "turn_token_usage": u})

    def test_each_request_priced_by_its_own_context_size(self):
        # 短请求：100K 输入（90K 缓存）、1K 输出；长请求：300K 输入（280K 缓存）、2K 输出
        self.add_thread("A", self.record(100000, 90000, 1000) + self.record(300000, 280000, 2000) + token_count())
        c = server.snapshot("A")["cost"]
        short = 10000 * 2.0 / 1e6 + 90000 * 0.1 / 1e6 + 1000 * 10.0 / 1e6
        long_ = 20000 * 4.0 / 1e6 + 280000 * 0.2 / 1e6 + 2000 * 15.0 / 1e6
        self.assertAlmostEqual(c["usd"], round(short + long_, 4))
        self.assertEqual(c["basis"], "per_request")
        self.assertEqual(c["tier"], "standard")

    def test_fast_tier_from_config(self):
        (self.home / "config.toml").write_text('model = "x"\nservice_tier = "priority"\n\n[features]\nservice_tier = "no"\n')
        self.add_thread("A", self.record(100000, 0, 0) + token_count())
        c = server.snapshot("A")["cost"]
        self.assertEqual(c["tier"], "fast")
        self.assertAlmostEqual(c["usd"], 0.4)

    def test_unknown_model_has_no_cost(self):
        self.add_thread("A", self.record(1000, 0, 10) + token_count())
        con = sqlite3.connect(self.home / "state_5.sqlite")
        con.execute("UPDATE threads SET model = 'mystery'")
        con.commit()
        con.close()
        self.assertIsNone(server.snapshot("A")["cost"])

    def test_old_sessions_fall_back_to_totals(self):
        self.add_thread("A", token_count())
        c = server.snapshot("A")["cost"]
        expected = (204800 - 186368) * 2.0 / 1e6 + 186368 * 0.1 / 1e6 + 8704 * 10.0 / 1e6
        self.assertAlmostEqual(c["usd"], round(expected, 4))
        self.assertEqual(c["basis"], "totals")

    def test_turn_cost_only_counts_that_turn(self):
        text = (self.record(100000, 0, 0, turn="t0")
                + line("event_msg", {"type": "task_started", "turn_id": "t1", "started_at": 1000})
                + self.record(50000, 0, 0, turn="t1") + token_count())
        self.add_thread("A", text)
        s = server.snapshot("A", now=1010)
        self.assertAlmostEqual(s["turn"]["cost"]["usd"], 0.1)
        self.assertAlmostEqual(s["cost"]["usd"], 0.3)


class ProtocolTests(Base):
    def test_thread_id_comes_from_meta(self):
        self.add_thread("A", token_count(), updated=1)
        self.add_thread("B", token_count(), updated=2)
        r = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "token_dango_snapshot", "arguments": {}, "_meta": {"threadId": "A"}}})
        self.assertEqual(r["result"]["structuredContent"]["snapshot"]["thread_id"], "A")

    def test_entrypoint_declared(self):
        tools = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
        meta = tools[0]["_meta"]
        self.assertEqual(meta["openai/ui"]["entrypoints"], [{"type": "thread"}])
        self.assertEqual(meta["ui"]["resourceUri"], server.UI_URI)

    def test_panel_resource_served(self):
        r = server.handle({"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"uri": server.UI_URI}})
        c = r["result"]["contents"][0]
        self.assertEqual(c["mimeType"], "text/html;profile=mcp-app")
        self.assertIn("<html", c["text"])


if __name__ == "__main__":
    unittest.main()
