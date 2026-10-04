"""Strata 0.1.38+ answers /metrics with one JSON document; strata_json_into maps it onto the engine card."""
import speculum


def test_strata_json_fills_window_rates_mtp():
    e = {"window": None, "queue": None, "rates": None, "mtp": None, "counters": None}
    sj = {"engine": {"max_context": 524288, "kv": "q4_0", "kv_resident": 32768, "expert_slots": 12702,
                     "vram_free_mib": 389, "version": "0.1.38"},
          "live": {"state": "decode", "queued": 1, "tok_s": 131.5, "prefill_tok_s_mean": 3400.0},
          "totals": {"requests": 29, "prompt_tokens": 1826849, "reused": 945692, "output_tokens": 5530,
                     "drafts_offered": 4450, "drafts_accepted": 3193}}
    speculum.strata_json_into(e, sj)
    assert e["window"] == 524288
    assert e["queue"] == 1
    assert e["rates"]["decode_tps"] == 131.5 and e["rates"]["state"] == "decode"
    assert e["mtp"] == 71.8
    assert e["counters"]["requests"] == 29 and e["counters"]["kv"] == "q4_0"
    assert not any(k.startswith("{") for k in e["counters"])


def test_strata_json_idle_and_no_drafts():
    e = {"window": 262144}
    speculum.strata_json_into(e, {"engine": {}, "live": {"state": "idle", "queued": 0, "tok_s": None},
                                  "totals": {}})
    assert e["window"] == 262144           # kept when the engine does not say
    assert e["mtp"] is None and e["rates"]["decode_tps"] is None


def test_strata_requests_map_to_records():
    sj = {"engine": {"model": "qwen3.8-flash-next-iq2_xs", "max_context": 524288},
          "requests": [{"time": 1791083326.39, "duration_s": 5.9, "prompt_tokens": 16759, "reused": 16523,
                        "output_tokens": 811, "prompt_read": 236, "prompt_ms": 423.5, "decode_tok_s": 149.3,
                        "drafts_offered": 697, "drafts_accepted": 557},
                       {"time": None}]}
    recs = speculum.strata_requests(sj)
    assert len(recs) == 1
    r = recs[0]
    assert r["id"] == "strata-1791083326.390" and r["origin"] == "strata" and r["window"] == 524288
    assert (r["prompt"], r["cache"], r["fresh"], r["output"]) == (16759, 16523, 236, 811)
    assert r["cache_pct"] == 98.6 and r["decode_tps"] == 149.3
    assert (r["mtp_acc"], r["mtp_tot"]) == (557, 697)
    assert r["prefill_tps"] == round(236 / 0.4235, 1)


def test_strata_cache_never_exceeds_prompt():
    sj = {"engine": {}, "requests": [{"time": 1.0, "prompt_tokens": 63900, "reused": 68000, "prompt_read": 120,
                                      "output_tokens": 10, "prompt_ms": 50.0}]}
    r = speculum.strata_requests(sj)[0]
    assert r["cache"] == 63780 and r["fresh"] == 120 and r["cache_pct"] <= 100.0


def test_ninfer_port_from_argv():
    assert speculum.ninfer_port_of(["/home/x/ninfer-serve-46645ada", "model.ninfer", "--port", "5803",
                                    "--kv-dtype", "rk4v4-e8"]) == 5803
    assert speculum.ninfer_port_of(["ninfer-serve", "m.ninfer", "--port=18099"]) == 18099
    assert speculum.ninfer_port_of(["ninfer-serve", "m.ninfer"]) == 8080
    assert speculum.ninfer_port_of(["ninfer-serve", "--port", "--host"]) == 8080
