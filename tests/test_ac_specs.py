"""Мощность и морозостойкость кондиционера из названия/УТП каталога SplitHub."""
import pytest

from content_factory.ingest.ac_specs import min_outdoor_temp, size_kbtu


@pytest.mark.parametrize("name, sku, size", [
    ("Кондиционер MIDEA PRIMARY MSAG3-09N8C2S-I", "midea-primary-inv-09", 9),
    ("Кондиционер FUNAI AKOYA RAC-I-AK25HP.D01", "funai-rac-i-ak25hp-d01", 9),      # 2,5 кВт
    ("Кондиционер ROYAL CLIMA ROYAL FRESH STANDARD RCI-RFS35HN", "", 12),            # 3,5 кВт
    ("Кондиционер HISENSE EXPERT PRO 2.0 AS-10UW4RLCHC00", "", 9),                  # Hisense «10» = типоразмер 9
    ("Кондиционер HITACHI X-Comfort RAK-35REF/RAC-35WEF", "", 12),
    ("Кондиционер LG MEGACOOL P07EP2", "lg-p07ep2", 7),
    ("Кондиционер FUNAI KADZOKU 2025 RAC-I-KD75HP.D03", "", 24),
    ("Кондиционер THAICON Comfort Plus", "thaicon-comfort-plus-inverter-9", 9),
    ("Кондиционер без модели", "", None),
])
def test_size_kbtu(name, sku, size):
    assert size_kbtu(name, sku) == size


def test_min_outdoor_temp_takes_deepest_frost():
    assert min_outdoor_temp("✓ Работа до -15 °С\n✓ Тихий") == 15
    assert min_outdoor_temp("Обогрев до −25°С, охлаждение до -10 °С") == 25
    assert min_outdoor_temp("✓ Режим iECO") is None
