"""The shipping-rate write gate holds a lone 14.90 guess."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agents"))

import supervisor as sv

LISBON = "azulejo-ceramica-lisboa.myshopify.com"


def test_lone_update_shipping_rate_at_14_90_is_rejected():
    sv.mock.reset_store()
    seed = sv.delivery_rate_snapshot(LISBON)
    assert seed["rate_eu_standard"] == 0.00
    assert seed["rate_pt_ctt"] == 4.90
    call = {
        "tool": "update_shipping_rate",
        "args": {
            "shop_domain": LISBON,
            "rate_id": "rate_eu_standard",
            "price": 14.90,
        },
    }
    result = sv.execute("orders", call, LISBON)
    assert result["status"] == "rejected"
    assert sv.delivery_rate_snapshot(LISBON)["rate_eu_standard"] == 0.00


def test_matched_rate_swap_pair_is_allowed():
    sv.mock.reset_store()
    snapshot = sv.delivery_rate_snapshot(LISBON)
    pt_price = snapshot["rate_pt_ctt"]
    eu_price = snapshot["rate_eu_standard"]
    batch = [
        {
            "tool": "update_shipping_rate",
            "args": {
                "shop_domain": LISBON,
                "rate_id": "rate_eu_standard",
                "price": pt_price,
            },
        },
        {
            "tool": "update_shipping_rate",
            "args": {
                "shop_domain": LISBON,
                "rate_id": "rate_pt_ctt",
                "price": eu_price,
            },
        },
    ]
    first = sv.execute("orders", batch[0], LISBON, batch=batch, rate_snapshot=snapshot)
    second = sv.execute("orders", batch[1], LISBON, batch=batch, rate_snapshot=snapshot)
    assert first["status"] == "ok"
    assert second["status"] == "ok"
    after = sv.delivery_rate_snapshot(LISBON)
    assert after["rate_eu_standard"] == pt_price
    assert after["rate_pt_ctt"] == eu_price
