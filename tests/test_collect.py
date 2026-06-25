from decimal import Decimal
from content_factory.models import RawProduct
from content_factory.ingest import collect_offers
from content_factory.ingest.normalize import CatalogFilter

FLT = CatalogFilter(report_category_ids=[2, 6, 7], exclude_title_patterns=["%мульти%"])


def test_collect_merges_db_and_jac(tmp_path):
    db_rows = [RawProduct(source="daichi", nc_code="N1", brand="Daichi", title="Сплит 07",
                          series=None, category_id=2, btu_calc=7,
                          price_wholesale=Decimal("10000"), price_base=None, stock_qty=2,
                          image_urls=["u"], tech={})]
    jac = tmp_path / "jac.json"
    jac.write_text('[{"article":"A1","name":"MDV 07","brand":"MDV","stock_qty":1,'
                   '"price":42000,"warehouse":"Крым","source":"jac_b2b",'
                   '"attributes":{"категория":"Бытовые сплит-системы"}}]', encoding="utf-8")
    offers = collect_offers(raw_db=db_rows, jac_path=jac, flt=FLT,
                            breez_base_lookup=lambda nc: None)
    skus = {o.supplier_sku for o in offers}
    assert "daichi:N1" in skus and "jac:A1" in skus
    daichi = next(o for o in offers if o.supplier_sku == "daichi:N1")
    assert daichi.cost == Decimal("10000")
