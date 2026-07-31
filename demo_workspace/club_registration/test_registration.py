import pytest

from registration import list_registrations, register, registrations


def setup_function():
    registrations.clear()


def test_register_student():
    result = register("张同学", "2026001")
    assert result["name"] == "张同学"
    assert list_registrations() == [{"name": "张同学", "student_id": "2026001"}]


def test_duplicate_student_id_is_rejected():
    register("张同学", "2026001")
    with pytest.raises(ValueError, match="学号已报名"):
        register("李同学", "2026001")
