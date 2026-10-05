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


# ------------------------------------------------------ 作者分成版本


def change_shares(client, work_id, change_id, authors):
    return client.post(
        f"/works/{work_id}/share-versions",
        json={"change_id": change_id, "authors": authors},
    )


def test_work_detail_shows_current_share_version(client):
    make_work(client)
    body = client.get("/works/W1").json()
    assert body["share_version"] == 1
    assert body["authors"] == [{"author_id": "A1", "ratio": "60"}, {"author_id": "A2", "ratio": "40"}]

    r = change_shares(client, "W1", "CHG-1", [{"author_id": "A1", "ratio": 70}, {"author_id": "A2", "ratio": 30}])
    assert r.status_code == 201 and r.json()["version"] == 2
    body = client.get("/works/W1").json()
    assert body["share_version"] == 2 and body["authors"][0]["ratio"] == "70"


def test_share_version_validation_and_404(client):
    make_work(client)
    # 作品不存在 → 404
    r = change_shares(client, "NOPE", "CHG-X", [{"author_id": "A1", "ratio": 100}])
    assert r.status_code == 404
    # 比例合计不为 100 → 422
    r = change_shares(client, "W1", "CHG-1", [{"author_id": "A1", "ratio": 70}])
    assert r.status_code == 422
    # 比例为负 → 422
    r = change_shares(client, "W1", "CHG-2", [{"author_id": "A1", "ratio": -1}, {"author_id": "A2", "ratio": 101}])
    assert r.status_code == 422
    # 作者编号重复 → 422
    r = change_shares(client, "W1", "CHG-3", [{"author_id": "A1", "ratio": 50}, {"author_id": "A1", "ratio": 50}])
    assert r.status_code == 422


def test_share_change_idempotent_replay_and_conflict(client):
    make_work(client)
    new_authors = [{"author_id": "A1", "ratio": 70}, {"author_id": "A2", "ratio": 30}]
    r1 = change_shares(client, "W1", "CHG-1", new_authors)
    assert r1.status_code == 201
    # 同号同内容重试 → 原版本（200）
    r2 = change_shares(client, "W1", "CHG-1", new_authors)
    assert r2.status_code == 200 and r2.json() == r1.json()
    # 之后又提交了第 3 版，原 change_id 重试仍返回第 2 版
    change_shares(client, "W1", "CHG-2", [{"author_id": "A1", "ratio": 100}])
    r3 = change_shares(client, "W1", "CHG-1", new_authors)
    assert r3.status_code == 200 and r3.json()["version"] == 2
    # 同号异内容 → 409
    r4 = change_shares(client, "W1", "CHG-1", [{"author_id": "A1", "ratio": 80}, {"author_id": "A2", "ratio": 20}])
    assert r4.status_code == 409
    # 同号用于其他作品 → 409
    make_work(client, work_id="W2")
    assert change_shares(client, "W2", "CHG-1", new_authors).status_code == 409


def test_share_version_list_and_get(client):
    make_work(client)
    change_shares(client, "W1", "CHG-1", [{"author_id": "A1", "ratio": 70}, {"author_id": "A2", "ratio": 30}])
    change_shares(client, "W1", "CHG-2", [
        {"author_id": "A1", "ratio": 50}, {"author_id": "A2", "ratio": 20}, {"author_id": "A3", "ratio": 30},
    ])
    body = client.get("/works/W1/share-versions").json()
    assert body["current_version"] == 3
    assert [v["version"] for v in body["versions"]] == [1, 2, 3]
    assert body["versions"][0]["change_id"] is None
    assert body["versions"][2]["authors"] == [
        {"author_id": "A1", "ratio": "50"},
        {"author_id": "A2", "ratio": "20"},
        {"author_id": "A3", "ratio": "30"},
    ]
    assert client.get("/works/W1/share-versions/2").json()["change_id"] == "CHG-1"
    assert client.get("/works/W1/share-versions/9").status_code == 404
    assert client.get("/works/NOPE/share-versions").status_code == 404


def test_sales_use_version_at_entry_time_not_business_time(client):
    make_work(client)
    # 变更为 A1 70 / A2 30（版本 2）
    change_shares(client, "W1", "CHG-1", [{"author_id": "A1", "ratio": 70}, {"author_id": "A2", "ratio": 30}])

    # 入账时刻为版本 2：即使业务时间早于变更，仍按版本 2 分账
    r = make_sale(client, "S-OLD", copies=100, price="10.00", ts="2020-01-01T00:00:00")
    assert r.status_code == 201
    body = r.json()
    assert body["share_version"] == 2  # 100 册×10 元×8% = 8000
    assert body["splits"] == [{"author_id": "A1", "amount_cents": 5600},
                              {"author_id": "A2", "amount_cents": 2400}]

    # 变更为 A1 50 / 新作者 A3 50（版本 3）；A2 退出当前分成
    change_shares(client, "W1", "CHG-2", [{"author_id": "A1", "ratio": 50}, {"author_id": "A3", "ratio": 50}])
    r = make_sale(client, "S-NEW", copies=100, price="10.00")
    body = r.json()
    assert body["share_version"] == 3
    assert body["splits"] == [{"author_id": "A1", "amount_cents": 4000},
                              {"author_id": "A3", "amount_cents": 4000}]


def test_refund_after_share_change_uses_original_sale_version(client):
    make_work(client)
    make_sale(client, "S1", copies=100, price="10.00")  # 版本 1：A1 60 / A2 40 → 4800/3200

    # 之后分成变更（A2 退出，A3 加入），旧销售退款仍按版本 1 从末册冲回并分账
    change_shares(client, "W1", "CHG-1", [{"author_id": "A1", "ratio": 50}, {"author_id": "A3", "ratio": 50}])
    r = client.post("/refunds", json={"refund_id": "R1", "sale_id": "S1", "copies": 50})
    assert r.status_code == 201
    body = r.json()
    assert body["share_version"] == 1
    assert body["total_refund_cents"] == 4000  # 50 册×80 分
    assert body["splits"] == [{"author_id": "A1", "amount_cents": 2400},
                              {"author_id": "A2", "amount_cents": 1600}]

    # 同号退款重试 → 原结果（仍为版本 1 分账）
    r2 = client.post("/refunds", json={"refund_id": "R1", "sale_id": "S1", "copies": 50})
    assert r2.status_code == 200 and r2.json() == body


def test_closed_snapshots_and_payments_not_rewritten_by_later_change(client):
    make_work(client)
    make_sale(client, "S1", copies=100, price="10.00")  # A1 4800 / A2 3200
    c1 = client.post("/closes", json={"request_id": "C1"}).json()["close_id"]
    assert pay(client, "P1", "A1", c1, 4800).status_code == 201

    # 关账与付款之后变更分成（A2 退出，A3 加入）
    change_shares(client, "W1", "CHG-1", [{"author_id": "A1", "ratio": 50}, {"author_id": "A3", "ratio": 50}])
    # 旧快照不改写：批次 1 仍是 A1 4800 / A2 3200，行内销售仍标记版本 1
    report = client.get(f"/closes/{c1}").json()
    assert report["author_totals"] == [
        {"author_id": "A1", "amount_cents": 4800},
        {"author_id": "A2", "amount_cents": 3200},
    ]
    assert report["lines"][0]["share_version"] == 1
    # 旧付款不改写，同号重放返回原结算
    old = client.get("/payments/P1").json()
    assert old["accrued_cents"] == 4800
    assert pay(client, "P1", "A1", c1, 4800).json() == old

    # 退出分成的 A2 仍可查询并结算既有余额；新作者 A3 在后续批次中参与
    make_sale(client, "S2", copies=100, price="10.00")  # 版本 3：A1/A3 各 4000
    # 旧销售的退款仍记给 A2
    client.post("/refunds", json={"refund_id": "R1", "sale_id": "S1", "copies": 100})
    c2 = client.post("/closes", json={"request_id": "C2"}).json()["close_id"]

    s2 = client.get("/authors/A2/settlement").json()
    assert s2["accrued_cents"] == 0 and s2["paid_cents"] == 0  # 3200 - 3200
    assert [b["batch_net_cents"] for b in s2["batches"]] == [3200, -3200]

    s3 = client.get("/authors/A3/settlement").json()
    assert s3["accrued_cents"] == 4000 and s3["payable_cents"] == 4000
    assert pay(client, "P2", "A3", c2, 4000).status_code == 201


def test_create_work_replay_returns_creation_result_after_change(client):
    assert make_work(client).status_code == 201
    change_shares(client, "W1", "CHG-1", [{"author_id": "A1", "ratio": 100}])
    # 原 work_id 同内容创建重试仍返回创建时结果：第 1 版 60/40
    r = make_work(client)
    assert r.status_code == 200
    body = r.json()
    assert body["share_version"] == 1
    assert body["authors"] == [{"author_id": "A1", "ratio": "60"}, {"author_id": "A2", "ratio": "40"}]
    # GET 详情仍展示当前版本
    assert client.get("/works/W1").json()["authors"] == [{"author_id": "A1", "ratio": "100"}]


def test_concurrent_share_changes_and_sales_have_determined_order(client):
    make_work(client)

    def change(i):
        return change_shares(client, "W1", f"CHG-{i}", [
            {"author_id": f"A{i % 3}", "ratio": 100}
        ])

    def sale(i):
        return make_sale(client, f"CC{i}", copies=1, price="10.00")

    with ThreadPoolExecutor(max_workers=8) as pool:
        f_changes = [pool.submit(change, i) for i in range(1, 21)]
        f_sales = [pool.submit(sale, i) for i in range(20)]
        change_results = [f.result() for f in f_changes]
        sale_results = [f.result() for f in f_sales]

    assert [r.status_code for r in change_results] == [201] * 20
    assert all(r.status_code == 201 for r in sale_results)
    # 版本号 2..21 无丢无重；每笔销售恰好落在提交时刻的当前版本
    versions = sorted(r.json()["version"] for r in change_results)
    assert versions == list(range(2, 22))
    used = [client.get(f"/sales/CC{i}").json()["share_version"] for i in range(20)]
    assert all(1 <= v <= 21 for v in used)
    # 分账作者必须是该笔销售版本中的唯一 100% 作者
    for i in range(20):
        body = client.get(f"/sales/CC{i}").json()
        v = body["share_version"]
        expected = None
        if v == 1:
            # 版本 1 为 A1 60 / A2 40：80 分总额按既有规则补齐
            assert sum(x["amount_cents"] for x in body["splits"]) == 80
        else:
            chg = client.get(f"/works/W1/share-versions/{v}").json()
            expected = chg["authors"][0]["author_id"]
            assert len(chg["authors"]) == 1
            assert body["splits"] == [{"author_id": expected, "amount_cents": 80}]

