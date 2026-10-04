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
