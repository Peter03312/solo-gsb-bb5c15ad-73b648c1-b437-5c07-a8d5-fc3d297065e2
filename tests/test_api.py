"""端到端 API 测试：阶梯计提、分账舍入、退款冲销、关账批次、幂等与并发。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import db, main


@pytest.fixture()
def client():
    db.open_database(":memory:")
    db.init_db()
    with TestClient(main.app) as c:
        yield c


def make_work(client, work_id="W1", tiers=None, authors=None):
    body = {
        "work_id": work_id,
        "title": "测试作品",
        "tiers": tiers
        or [{"threshold": 0, "rate": 0.08}, {"threshold": 1000, "rate": 0.10}],
        "authors": authors
        or [{"author_id": "A1", "ratio": 60}, {"author_id": "A2", "ratio": 40}],
    }
    return client.post("/works", json=body)


def make_sale(client, sale_id, work_id="W1", copies=1, price="33.33", ts="2026-09-01T10:00:00"):
    return client.post(
        "/sales",
        json={
            "sale_id": sale_id,
            "work_id": work_id,
            "business_time": ts,
            "copies": copies,
            "net_price": price,
        },
    )


# ---------------------------------------------------------------- 作品校验


def test_work_ratio_sum_must_be_100(client):
    r = make_work(client, authors=[{"author_id": "A1", "ratio": 60}])
    assert r.status_code == 422


def test_work_thresholds_must_increase(client):
    r = make_work(client, tiers=[{"threshold": 0, "rate": 0.08}, {"threshold": 0, "rate": 0.1}])
    assert r.status_code == 422


def test_work_first_threshold_must_be_zero(client):
    r = make_work(client, tiers=[{"threshold": 100, "rate": 0.08}])
    assert r.status_code == 422


def test_work_idempotent_replay_and_conflict(client):
    assert make_work(client).status_code == 201
    r = make_work(client)
    assert r.status_code == 200  # 同号同内容 → 返回原结果
    assert r.json()["work_id"] == "W1"
    r = make_work(client, authors=[{"author_id": "A1", "ratio": 100}])
    assert r.status_code == 409  # 同号异内容 → 拒绝


# ---------------------------------------------------------------- 阶梯计提与分账


def test_sale_royalty_across_tiers(client):
    make_work(client)
    r = make_sale(client, "S1", copies=800)
    assert r.status_code == 201
    body = r.json()
    # 800 册全部落在第一档：33.33*0.08=2.6664 → 每册 267 分
    assert body["cum_before"] == 0 and body["cum_after"] == 800
    assert body["segments"] == [
        {
            "segment_index": 0,
            "tier_index": 0,
            "rate": "0.08",
            "copies": 800,
            "per_copy_royalty_cents": 267,
            "amount_cents": 213600,
            "refunded_copies": 0,
        }
    ]
    assert body["total_royalty_cents"] == 213600
    assert body["splits"] == [
        {"author_id": "A1", "amount_cents": 128160},
        {"author_id": "A2", "amount_cents": 85440},
    ]

    # 跨档：累计销量 800→1200，前 200 册 8%，后 200 册 10%（每册 333 分）
    r = make_sale(client, "S2", copies=400)
    body = r.json()
    assert body["cum_before"] == 800 and body["cum_after"] == 1200
    assert [(s["tier_index"], s["copies"], s["per_copy_royalty_cents"]) for s in body["segments"]] == [
        (0, 200, 267),
        (1, 200, 333),
    ]
    assert body["total_royalty_cents"] == 200 * 267 + 200 * 333
    assert body["splits"] == [
        {"author_id": "A1", "amount_cents": 72000},
        {"author_id": "A2", "amount_cents": 48000},
    ]


def test_split_remainder_filled_by_author_order(client):
    make_work(
        client,
        work_id="W2",
        tiers=[{"threshold": 0, "rate": 0.07}],
        authors=[
            {"author_id": "A3", "ratio": 33.34},
            {"author_id": "A1", "ratio": 33.33},
            {"author_id": "A2", "ratio": 33.33},
        ],
    )
    r = make_sale(client, "S9", work_id="W2", copies=1, price="10.00")
    body = r.json()
    assert body["total_royalty_cents"] == 70  # 10*0.07=0.70
    # 70*33.33%=23.31→23，70*33.34%=23.38→23，余 1 分按作者编号顺序补 A1
    assert body["splits"] == [
        {"author_id": "A1", "amount_cents": 24},
        {"author_id": "A2", "amount_cents": 23},
        {"author_id": "A3", "amount_cents": 23},
    ]


def test_sale_idempotency(client):
    make_work(client)
    r1 = make_sale(client, "S1", copies=10)
    assert r1.status_code == 201
    r2 = make_sale(client, "S1", copies=10)
    assert r2.status_code == 200
    assert r1.json() == r2.json()  # 同号同内容 → 原结果
    r3 = make_sale(client, "S1", copies=11)
    assert r3.status_code == 409  # 同号异内容 → 拒绝


def test_sale_unknown_work_404(client):
    r = make_sale(client, "S1", work_id="NOPE")
    assert r.status_code == 404


# ---------------------------------------------------------------- 退款


def test_refund_reverses_from_last_copy(client):
    make_work(client)
    make_sale(client, "S1", copies=800)
    make_sale(client, "S2", copies=400)  # 200 册@267 + 200 册@333

    # 退 150 册：全部来自最后一档（每册 333 分）
    r = client.post("/refunds", json={"refund_id": "R1", "sale_id": "S2", "copies": 150})
    assert r.status_code == 201
    body = r.json()
    assert body["total_refund_cents"] == 150 * 333
    assert [(s["sale_segment_index"], s["copies"]) for s in body["segments"]] == [(1, 150)]
    assert body["splits"] == [
        {"author_id": "A1", "amount_cents": 29970},
        {"author_id": "A2", "amount_cents": 19980},
    ]

    # 再退 100 册：最后一档剩 50 册，继续冲倒数第二档 50 册
    r = client.post("/refunds", json={"refund_id": "R2", "sale_id": "S2", "copies": 100})
    body = r.json()
    assert body["total_refund_cents"] == 50 * 333 + 50 * 267
    assert [(s["sale_segment_index"], s["copies"]) for s in body["segments"]] == [(0, 50), (1, 50)]

    # 累计销量不回退
    sale = client.get("/sales/S2").json()
    assert sale["cum_after"] == 1200
    assert sale["refunded_copies"] == 250

    # 累计退款不得超过原册数：已退 250，再退 200 → 450 > 400
    r = client.post("/refunds", json={"refund_id": "R3", "sale_id": "S2", "copies": 200})
    assert r.status_code == 400
    # 退满 400 册允许
    r = client.post("/refunds", json={"refund_id": "R3", "sale_id": "S2", "copies": 150})
    assert r.status_code == 201


def test_refund_idempotency_and_unknown_sale(client):
    make_work(client)
    make_sale(client, "S1", copies=10)
    r1 = client.post("/refunds", json={"refund_id": "R1", "sale_id": "S1", "copies": 3})
    assert r1.status_code == 201
    r2 = client.post("/refunds", json={"refund_id": "R1", "sale_id": "S1", "copies": 3})
    assert r2.status_code == 200
    assert r1.json() == r2.json()
    r3 = client.post("/refunds", json={"refund_id": "R1", "sale_id": "S1", "copies": 4})
    assert r3.status_code == 409
    r4 = client.post("/refunds", json={"refund_id": "R9", "sale_id": "NOPE", "copies": 1})
    assert r4.status_code == 404


# ---------------------------------------------------------------- 关账


def test_close_batches_and_immutable_report(client):
    make_work(client)
    make_sale(client, "S1", copies=800)
    make_sale(client, "S2", copies=400)
    client.post("/refunds", json={"refund_id": "R1", "sale_id": "S2", "copies": 150})

    r = client.post("/closes", json={"request_id": "CLOSE-1"})
    assert r.status_code == 201
    report1 = r.json()
    assert report1["sales_count"] == 2 and report1["refunds_count"] == 1
    assert [l["type"] for l in report1["lines"]] == ["sale", "sale", "refund"]
    assert report1["total_sales_royalty_cents"] == 213600 + 120000
    assert report1["total_refund_cents"] == 49950
    assert report1["net_royalty_cents"] == 283650
    assert report1["author_totals"] == [
        {"author_id": "A1", "amount_cents": 128160 + 72000 - 29970},
        {"author_id": "A2", "amount_cents": 85440 + 48000 - 19980},
    ]

    # 同 request_id 重试 → 返回原报表
    r = client.post("/closes", json={"request_id": "CLOSE-1"})
    assert r.status_code == 200 and r.json()["close_id"] == report1["close_id"]

    # 关账后补录的旧业务流水进入下一批
    make_sale(client, "S3", copies=10, ts="2020-01-01T00:00:00")
    client.post("/refunds", json={"refund_id": "R2", "sale_id": "S2", "copies": 100})
    r = client.post("/closes")
    report2 = r.json()
    assert report2["sales_count"] == 1 and report2["refunds_count"] == 1
    assert [(l["type"], l["sale_id"] if l["type"] == "sale" else l["refund_id"])
            for l in report2["lines"]] == [("sale", "S3"), ("refund", "R2")]
    assert report2["entry_seq_from"] == report1["entry_seq_to"] + 1

    # 第一批报表不可改：再次读取完全一致
    assert client.get(f"/closes/{report1['close_id']}").json() == report1
    # 流水归属批次已固定
    assert client.get("/sales/S1").json()["close_id"] == report1["close_id"]
    assert client.get("/sales/S3").json()["close_id"] == report2["close_id"]


def test_close_empty_batch(client):
    r = client.post("/closes")
    assert r.status_code == 201
    body = r.json()
    assert body["lines"] == [] and body["author_totals"] == []
    assert body["net_royalty_cents"] == 0


# ---------------------------------------------------------------- 并发


def test_concurrent_sales_and_close(client):
    make_work(client)
    n = 40

    def post_sale(i):
        return client.post(
            "/sales",
            json={
                "sale_id": f"CS{i}",
                "work_id": "W1",
                "business_time": "2026-09-01T10:00:00",
                "copies": 1,
                "net_price": "10.00",
            },
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(post_sale, i) for i in range(n)]
        mid_close = client.post("/closes")  # 与入账并发，归属由提交顺序决定
        results = [f.result() for f in futures]
    assert all(r.status_code == 201 for r in results)
    final_close = client.post("/closes")

    # 累计销量无丢无重：cum_after 恰好是 1..n 的排列
    cums = sorted(client.get(f"/sales/CS{i}").json()["cum_after"] for i in range(n))
    assert cums == list(range(1, n + 1))

    # 每笔流水恰好归属一个批次，且两批区间首尾相接
    c1, c2 = mid_close.json(), final_close.json()
    assert c1["sales_count"] + c2["sales_count"] == n
    assert c2["entry_seq_from"] == c1["entry_seq_to"] + 1
    ids = [l["sale_id"] for l in c1["lines"]] + [l["sale_id"] for l in c2["lines"]]
    assert sorted(ids) == sorted(f"CS{i}" for i in range(n))


# ---------------------------------------------------------------- 付款结算


def pay(client, payment_id, author_id, close_id, amount):
    return client.post(
        "/payments",
        json={
            "payment_id": payment_id,
            "author_id": author_id,
            "close_id": close_id,
            "amount_cents": amount,
        },
    )


def test_payment_happy_path_and_replay(client):
    make_work(client)
    make_sale(client, "S1", copies=800)  # A1 应计 128160 分
    r = client.post("/closes", json={"request_id": "C1"})
    close_id = r.json()["close_id"]

    s = client.get("/authors/A1/settlement").json()
    assert s["latest_close_id"] == close_id
    assert s["accrued_cents"] == 128160 and s["paid_cents"] == 0
    assert s["payable_cents"] == 128160 and s["negative_balance_cents"] == 0
    assert s["batches"] == [
        {"close_id": close_id, "batch_net_cents": 128160, "accrued_cents": 128160,
         "created_at": s["batches"][0]["created_at"]}
    ]

    r = pay(client, "P1", "A1", close_id, 100000)
    assert r.status_code == 201
    body = r.json()
    assert body["close_id"] == close_id and body["amount_cents"] == 100000
    assert body["accrued_cents"] == 128160
    assert body["paid_before_cents"] == 0 and body["payable_cents"] == 128160
    assert body["paid_after_cents"] == 100000 and body["negative_balance_cents"] == 0

    # 同付款编号同内容重试 → 原结算结果
    r2 = pay(client, "P1", "A1", close_id, 100000)
    assert r2.status_code == 200 and r2.json() == body
    # 同号异内容（金额不同）→ 409
    assert pay(client, "P1", "A1", close_id, 100001).status_code == 409

    # 结清剩余可付额 28160
    r = pay(client, "P2", "A1", close_id, 28160)
    assert r.status_code == 201
    assert r.json()["paid_before_cents"] == 100000 and r.json()["payable_cents"] == 28160

    # 可付额已为 0，任何正金额均被拒
    assert pay(client, "P3", "A1", close_id, 1).status_code == 400

    s = client.get("/authors/A1/settlement").json()
    assert s["accrued_cents"] == 128160 and s["paid_cents"] == 128160
    assert s["payable_cents"] == 0 and s["negative_balance_cents"] == 0
    assert [p["payment_id"] for p in s["payments"]] == ["P1", "P2"]
    assert client.get("/payments/P1").json() == body


def test_payment_validation_errors(client):
    # 作者不存在 → 404
    assert pay(client, "P1", "NOBODY", 99, 100).status_code == 404
    make_work(client)
    client.post("/closes", json={"request_id": "C1"})
    # 批次不存在 → 404
    assert pay(client, "P1", "A1", 999, 100).status_code == 404
    # 再关一批，旧批次不再是当前最新 → 409
    client.post("/closes", json={"request_id": "C2"})
    assert pay(client, "P1", "A1", 1, 100).status_code == 409
    # 当前最新批次该作者应计为 0 → 可付额 0，正金额 → 400
    assert pay(client, "P1", "A1", 2, 100).status_code == 400
    # 金额非正 → 422（请求体校验）
    assert pay(client, "P2", "A1", 2, 0).status_code == 422


def test_payment_negative_balance_recovered_by_later_close(client):
    make_work(client)
    # 100 册 ×33.33×8% = 每册 267 分 → 26700；A1 60% = 16020
    make_sale(client, "S1", copies=100)
    c1 = client.post("/closes", json={"request_id": "C1"}).json()["close_id"]
    assert pay(client, "P1", "A1", c1, 16020).status_code == 201

    # 关账后全额退款 26700（A1 -16020），再关账
    client.post("/refunds", json={"refund_id": "R1", "sale_id": "S1", "copies": 100})
    c2 = client.post("/closes", json={"request_id": "C2"}).json()["close_id"]

    s = client.get("/authors/A1/settlement").json()
    assert s["accrued_cents"] == 0 and s["paid_cents"] == 16020
    assert s["negative_balance_cents"] == 16020 and s["payable_cents"] == 0
    assert [(b["close_id"], b["batch_net_cents"], b["accrued_cents"]) for b in s["batches"]] == [
        (c1, 16020, 16020),
        (c2, -16020, 0),
    ]
    # 负余额时拒绝任何付款
    assert pay(client, "P2", "A1", c2, 1).status_code == 400

    # 后续关账的正额先抵扣：300 册全部在 8% 档 → 80100；A1 48060
    make_sale(client, "S2", copies=300)
    c3 = client.post("/closes", json={"request_id": "C3"}).json()["close_id"]
    s = client.get("/authors/A1/settlement").json()
    assert s["accrued_cents"] == 48060 and s["paid_cents"] == 16020
    assert s["negative_balance_cents"] == 0 and s["payable_cents"] == 32040
    assert pay(client, "P3", "A1", c3, 32040).status_code == 201

    # 旧报表与旧付款均未被改写；旧付款重放返回原始结算
    assert client.get(f"/closes/{c1}").json()["author_totals"][0]["amount_cents"] == 16020
    old = client.get("/payments/P1").json()
    assert old["close_id"] == c1 and old["accrued_cents"] == 16020
    assert pay(client, "P1", "A1", c1, 16020).json() == old


def test_payment_replay_after_new_close_and_conflict(client):
    make_work(client)
    make_sale(client, "S1", copies=800)  # A1 128160
    c1 = client.post("/closes", json={"request_id": "C1"}).json()["close_id"]
    body = pay(client, "P1", "A1", c1, 100000).json()

    # 之后又关账（空批），原批次已不是最新
    c2 = client.post("/closes", json={"request_id": "C2"}).json()["close_id"]
    # 同号同内容重放仍返回原结算结果（不校验最新批次）
    r = pay(client, "P1", "A1", c1, 100000)
    assert r.status_code == 200 and r.json() == body
    # 同号异内容（金额/批次）→ 409
    assert pay(client, "P1", "A1", c1, 100001).status_code == 409
    assert pay(client, "P1", "A1", c2, 100000).status_code == 409
    # 新批次上可结算剩余可付额
    assert pay(client, "P2", "A1", c2, 28160).status_code == 201
    assert pay(client, "P3", "A1", c2, 1).status_code == 400


def test_concurrent_payments_never_overpay(client):
    make_work(
        client,
        tiers=[{"threshold": 0, "rate": 0.08}],
        authors=[{"author_id": "A1", "ratio": 60}, {"author_id": "A2", "ratio": 40}],
    )
    make_sale(client, "S1", copies=20, price="10.00")  # 20×80=1600；A1=960
    close_id = client.post("/closes").json()["close_id"]

    def do_pay(i):
        return pay(client, f"CP{i}", "A1", close_id, 50)  # 20×50=1000 > 可付 960

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(do_pay, range(20)))
    assert sum(1 for r in results if r.status_code == 201) == 19  # 19×50=950
    assert sum(1 for r in results if r.status_code == 400) == 1
    s = client.get("/authors/A1/settlement").json()
    assert s["paid_cents"] == 950 and s["payable_cents"] == 10
    assert pay(client, "CPX", "A1", close_id, 10).status_code == 201
    assert pay(client, "CPY", "A1", close_id, 1).status_code == 400


def test_payment_and_author_edge_cases(client):
    assert client.get("/payments/NOPE").status_code == 404
    make_work(client)
    # 作者存在但从未关账
    s = client.get("/authors/A1/settlement").json()
    assert s["latest_close_id"] is None
    assert s["accrued_cents"] == 0 and s["paid_cents"] == 0
    assert s["negative_balance_cents"] == 0 and s["payable_cents"] == 0
    assert s["batches"] == [] and s["payments"] == []
    assert client.get("/authors/NOBODY/settlement").status_code == 404
